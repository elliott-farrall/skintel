"""CS2 skin price tracker — core library and collect CLI."""

import os
import time
import sqlite3
import logging
import argparse
import requests
from datetime import datetime, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

DB_PATH = os.getenv("SKINTEL_DB", "skintel.db")

PRICE_URL = "https://steamcommunity.com/market/priceoverview/"
APPID = 730

PRICE_FETCH_DELAY = 3.5  # Steam market: ~20 req/min unauthenticated
ATH_PROXIMITY = 0.95     # alert if current >= 95% of all-time high
VOLUME_SURGE_MULT = 2.0  # alert if volume >= 2x rolling average

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://steamcommunity.com/",
}


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS skins (
            id          INTEGER PRIMARY KEY,
            market_hash TEXT    UNIQUE NOT NULL,
            first_seen  TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS prices (
            id              INTEGER PRIMARY KEY,
            skin_id         INTEGER NOT NULL REFERENCES skins(id),
            fetched_at      TEXT    NOT NULL,
            lowest_price    REAL,
            median_price    REAL,
            volume          INTEGER
        );

        CREATE TABLE IF NOT EXISTS alerts (
            id          INTEGER PRIMARY KEY,
            skin_id     INTEGER NOT NULL REFERENCES skins(id),
            alerted_at  TEXT    NOT NULL,
            current     REAL    NOT NULL,
            average     REAL,
            pct_above   REAL
        );
    """)
    for sql in [
        "ALTER TABLE alerts ADD COLUMN alert_type TEXT NOT NULL DEFAULT 'spike'",
        "ALTER TABLE alerts ADD COLUMN reference REAL",
    ]:
        try:
            conn.execute(sql)
        except Exception:
            pass
    conn.commit()


def get_inventory(steam_id: str) -> list[dict]:
    url = f"https://steamcommunity.com/inventory/{steam_id}/730/2"
    resp = requests.get(
        url,
        params={"l": "english", "count": 5000},
        headers=HEADERS,
        timeout=30,
    )
    log.info("Inventory: HTTP %d", resp.status_code)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Inventory fetch failed: {data}")

    desc_map = {
        (d["classid"], d["instanceid"]): d
        for d in data.get("descriptions", [])
    }
    items = []
    seen: set[str] = set()
    for asset in data.get("assets", []):
        desc = desc_map.get((asset["classid"], asset["instanceid"]), {})
        if not desc.get("marketable"):
            continue
        mh = desc["market_hash_name"]
        if mh not in seen:
            seen.add(mh)
            items.append({"market_hash": mh, "name": desc.get("name", mh)})

    log.info("Found %d unique marketable items", len(items))
    return items


def fetch_price(market_hash: str) -> dict | None:
    params = {"appid": APPID, "market_hash_name": market_hash, "currency": 1}
    try:
        resp = requests.get(PRICE_URL, params=params, headers=HEADERS, timeout=15)
        if resp.status_code == 429:
            log.warning("Rate limited fetching %s — skipping", market_hash)
            return None
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            return None

        def parse_usd(s: str | None) -> float | None:
            if not s:
                return None
            return float(s.replace("$", "").replace(",", "").strip())

        return {
            "lowest_price": parse_usd(data.get("lowest_price")),
            "median_price": parse_usd(data.get("median_price")),
            "volume": int(data["volume"].replace(",", "")) if data.get("volume") else None,
        }
    except Exception as exc:
        log.warning("Price fetch error for %s: %s", market_hash, exc)
        return None


def upsert_skin(conn: sqlite3.Connection, market_hash: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO skins (market_hash, first_seen) VALUES (?, ?)",
        (market_hash, now),
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM skins WHERE market_hash = ?", (market_hash,)
    ).fetchone()[0]


def record_price(conn: sqlite3.Connection, skin_id: int, price: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO prices (skin_id, fetched_at, lowest_price, median_price, volume)
           VALUES (?, ?, ?, ?, ?)""",
        (skin_id, now, price["lowest_price"], price["median_price"], price["volume"]),
    )
    conn.commit()


def rolling_average(conn: sqlite3.Connection, skin_id: int, days: int) -> float | None:
    row = conn.execute(
        """SELECT AVG(median_price) FROM prices
           WHERE skin_id = ? AND median_price IS NOT NULL
             AND fetched_at >= datetime('now', ?)""",
        (skin_id, f"-{days} days"),
    ).fetchone()
    return row[0] if row else None


def _insert_alert(
    conn: sqlite3.Connection,
    skin_id: int,
    alert_type: str,
    current: float,
    reference: float | None,
    pct_above: float | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO alerts (skin_id, alerted_at, alert_type, current, reference, pct_above)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (skin_id, now, alert_type, current, reference, pct_above),
    )
    conn.commit()


def check_spike(
    conn: sqlite3.Connection,
    skin_id: int,
    market_hash: str,
    current: float,
    threshold_pct: float,
    rolling_days: int,
) -> bool:
    avg = rolling_average(conn, skin_id, rolling_days)
    if not avg:
        return False
    pct_above = (current - avg) / avg * 100
    if pct_above < threshold_pct:
        return False
    _insert_alert(conn, skin_id, "spike", current, avg, pct_above)
    log.warning("SPIKE  %s  $%.2f  avg=$%.2f  +%.1f%%", market_hash, current, avg, pct_above)
    return True


def check_all_time_high(
    conn: sqlite3.Connection,
    skin_id: int,
    market_hash: str,
    current: float,
) -> bool:
    row = conn.execute(
        "SELECT MAX(median_price) FROM prices WHERE skin_id = ? AND median_price IS NOT NULL",
        (skin_id,),
    ).fetchone()
    if not row or not row[0]:
        return False
    ath = row[0]
    if current < ath * ATH_PROXIMITY:
        return False
    pct = (current - ath) / ath * 100
    _insert_alert(conn, skin_id, "all_time_high", current, ath, pct)
    log.warning("ATH  %s  $%.2f  all-time-high=$%.2f", market_hash, current, ath)
    return True


def check_volume_surge(
    conn: sqlite3.Connection,
    skin_id: int,
    market_hash: str,
    current_volume: int,
    rolling_days: int,
) -> bool:
    row = conn.execute(
        """SELECT AVG(volume) FROM prices
           WHERE skin_id = ? AND volume IS NOT NULL
             AND fetched_at >= datetime('now', ?)""",
        (skin_id, f"-{rolling_days} days"),
    ).fetchone()
    avg_vol = row[0] if row and row[0] else None
    if not avg_vol or current_volume < avg_vol * VOLUME_SURGE_MULT:
        return False
    pct = (current_volume - avg_vol) / avg_vol * 100
    _insert_alert(conn, skin_id, "volume_surge", float(current_volume), avg_vol, pct)
    log.warning("VOL SURGE  %s  vol=%d  avg=%.0f  +%.1f%%", market_hash, current_volume, avg_vol, pct)
    return True


def ingest(items: list[dict], threshold_pct: float, rolling_days: int) -> dict:
    """Store prices and run detection. Used by /api/ingest and local runs."""
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    stored = 0
    alert_count = 0

    for item in items:
        mh = item["market_hash"]
        price = item.get("price")
        if not price:
            continue

        skin_id = upsert_skin(conn, mh)
        record_price(conn, skin_id, price)
        stored += 1

        if price.get("median_price") is not None:
            if check_spike(conn, skin_id, mh, price["median_price"], threshold_pct, rolling_days):
                alert_count += 1
            if check_all_time_high(conn, skin_id, mh, price["median_price"]):
                alert_count += 1

        if price.get("volume") is not None:
            if check_volume_surge(conn, skin_id, mh, price["volume"], rolling_days):
                alert_count += 1

    conn.close()
    log.info("Ingest complete. %d stored, %d alert(s).", stored, alert_count)
    return {"stored": stored, "alerts": alert_count}


def collect(
    steam_id: str,
    push_url: str,
    push_token: str,
    threshold_pct: float,
    rolling_days: int,
) -> None:
    """Fetch inventory + prices then POST to Fly.io /api/ingest."""
    items = get_inventory(steam_id)
    if not items:
        log.info("No marketable items — nothing to collect")
        return

    payload_items = []
    for i, item in enumerate(items):
        price = fetch_price(item["market_hash"])
        if price:
            payload_items.append({"market_hash": item["market_hash"], "price": price})
            log.info(
                "[%d/%d] %s  median=$%s  vol=%s",
                i + 1, len(items), item["market_hash"],
                price["median_price"], price["volume"],
            )
        else:
            log.info("[%d/%d] %s — no price", i + 1, len(items), item["market_hash"])

        if i < len(items) - 1:
            time.sleep(PRICE_FETCH_DELAY)

    payload = {
        "items": payload_items,
        "threshold_pct": threshold_pct,
        "rolling_days": rolling_days,
    }
    resp = requests.post(
        push_url,
        json=payload,
        headers={"Authorization": f"Bearer {push_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    result = resp.json()
    log.info("Ingest response: %s", result)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cmd_history(market_hash: str, limit: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT p.fetched_at, p.lowest_price, p.median_price, p.volume
           FROM prices p JOIN skins s ON s.id = p.skin_id
           WHERE s.market_hash = ? ORDER BY p.fetched_at DESC LIMIT ?""",
        (market_hash, limit),
    ).fetchall()
    conn.close()
    if not rows:
        print(f"No history for: {market_hash}")
        return
    print(f"\nPrice history — {market_hash}")
    print(f"{'Date':19}  {'Lowest':>8}  {'Median':>8}  {'Volume':>8}")
    print("-" * 52)
    for fetched_at, lowest, median, volume in rows:
        print(
            f"{fetched_at[:19]:19}  "
            f"{f'${lowest:.2f}' if lowest else 'N/A':>8}  "
            f"{f'${median:.2f}' if median else 'N/A':>8}  "
            f"{volume or 'N/A':>8}"
        )


def _cmd_alerts(limit: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT a.alerted_at, a.alert_type, s.market_hash, a.current, a.reference, a.pct_above
           FROM alerts a JOIN skins s ON s.id = a.skin_id
           ORDER BY a.alerted_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    if not rows:
        print("No alerts recorded.")
        return
    print(f"\n{'Date':19}  {'Type':12}  {'Skin':40}  {'Current':>8}  {'Ref':>8}  {'%':>7}")
    print("-" * 100)
    for alerted_at, alert_type, mh, current, reference, pct in rows:
        ref_str = f"${reference:.2f}" if reference else "N/A"
        pct_str = f"{pct:.1f}%" if pct is not None else "N/A"
        print(
            f"{alerted_at[:19]:19}  {(alert_type or 'spike'):12}  {mh[:40]:40}  "
            f"${current:>7.2f}  {ref_str:>8}  {pct_str:>7}"
        )


def main() -> None:
    import sys
    parser = argparse.ArgumentParser(description="CS2 skin price tracker")
    sub = parser.add_subparsers(dest="cmd")

    collect_p = sub.add_parser("collect", help="Fetch inventory+prices and POST to ingest URL")
    collect_p.add_argument("--steam-id", required=True)
    collect_p.add_argument("--push-url", required=True, help="URL of /api/ingest on Fly.io")
    collect_p.add_argument("--push-token", required=True, help="INGEST_TOKEN secret")
    collect_p.add_argument("--threshold", type=float, default=20.0)
    collect_p.add_argument("--days", type=int, default=7)

    hist_p = sub.add_parser("history", help="Show price history for a skin")
    hist_p.add_argument("market_hash")
    hist_p.add_argument("--limit", type=int, default=30)

    alerts_p = sub.add_parser("alerts", help="Show recent alerts")
    alerts_p.add_argument("--limit", type=int, default=50)

    args = parser.parse_args()

    if args.cmd == "collect":
        collect(args.steam_id, args.push_url, args.push_token, args.threshold, args.days)
    elif args.cmd == "history":
        _cmd_history(args.market_hash, args.limit)
    elif args.cmd == "alerts":
        _cmd_alerts(args.limit)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
