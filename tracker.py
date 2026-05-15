"""CS2 skin price tracker with spike detection and SQLite persistence."""

import os
import sys
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
STEAM_API_KEY = os.getenv("STEAM_API_KEY", "")
STEAM_ID = os.getenv("STEAM_ID", "")

# Steam endpoints
INVENTORY_URL = "https://steamcommunity.com/inventory/{steam_id}/730/2"
PRICE_URL = "https://steamcommunity.com/market/priceoverview/"

# App ID for CS2 (730)
APPID = 730

# Rate limit: Steam market allows ~20 req/min for unauthenticated calls
PRICE_FETCH_DELAY = 3.5  # seconds between price requests


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
            average     REAL    NOT NULL,
            pct_above   REAL    NOT NULL
        );
    """)
    conn.commit()


def get_inventory(steam_id: str) -> list[dict]:
    """Fetch public CS2 inventory. Returns list of items."""
    url = INVENTORY_URL.format(steam_id=steam_id)
    resp = requests.get(url, params={"l": "english", "count": 5000}, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if not data.get("success"):
        raise RuntimeError(f"Inventory fetch failed: {data}")

    assets = {a["assetid"]: a for a in data.get("assets", [])}
    descriptions = {
        (d["classid"], d["instanceid"]): d for d in data.get("descriptions", [])
    }

    items = []
    for asset in assets.values():
        key = (asset["classid"], asset["instanceid"])
        desc = descriptions.get(key, {})
        if not desc.get("marketable"):
            continue
        items.append({
            "market_hash": desc["market_hash_name"],
            "name": desc.get("name", desc["market_hash_name"]),
        })

    log.info("Found %d marketable items in inventory", len(items))
    return items


def fetch_price(market_hash: str) -> dict | None:
    """Fetch current market price for one item. Returns None on failure."""
    params = {
        "appid": APPID,
        "market_hash_name": market_hash,
        "currency": 1,  # USD
    }
    try:
        resp = requests.get(PRICE_URL, params=params, timeout=15)
        if resp.status_code == 429:
            log.warning("Rate limited fetching %s — skipping", market_hash)
            return None
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success"):
            log.warning("No price data for %s", market_hash)
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
    row = conn.execute(
        "SELECT id FROM skins WHERE market_hash = ?", (market_hash,)
    ).fetchone()
    return row[0]


def record_price(conn: sqlite3.Connection, skin_id: int, price: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO prices (skin_id, fetched_at, lowest_price, median_price, volume)
           VALUES (?, ?, ?, ?, ?)""",
        (skin_id, now, price["lowest_price"], price["median_price"], price["volume"]),
    )
    conn.commit()


def rolling_average(conn: sqlite3.Connection, skin_id: int, days: int) -> float | None:
    """Return average median_price over the last N days (excluding nulls)."""
    row = conn.execute(
        """SELECT AVG(median_price)
           FROM prices
           WHERE skin_id = ?
             AND median_price IS NOT NULL
             AND fetched_at >= datetime('now', ?)""",
        (skin_id, f"-{days} days"),
    ).fetchone()
    return row[0] if row else None


def check_spike(
    conn: sqlite3.Connection,
    skin_id: int,
    market_hash: str,
    current: float,
    threshold_pct: float,
    rolling_days: int,
) -> bool:
    avg = rolling_average(conn, skin_id, rolling_days)
    if avg is None or avg == 0:
        return False

    pct_above = (current - avg) / avg * 100
    if pct_above >= threshold_pct:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """INSERT INTO alerts (skin_id, alerted_at, current, average, pct_above)
               VALUES (?, ?, ?, ?, ?)""",
            (skin_id, now, current, avg, pct_above),
        )
        conn.commit()
        log.warning(
            "SPIKE %s  current=$%.2f  avg=$%.2f  +%.1f%%",
            market_hash, current, avg, pct_above,
        )
        return True
    return False


def run(
    steam_id: str,
    threshold_pct: float,
    rolling_days: int,
    dry_run: bool,
) -> None:
    conn = sqlite3.connect(DB_PATH)
    init_db(conn)

    items = get_inventory(steam_id)
    if not items:
        log.info("No marketable items found — nothing to do")
        return

    # Deduplicate by market_hash (same skin can appear multiple times)
    seen: set[str] = set()
    unique_items = []
    for item in items:
        if item["market_hash"] not in seen:
            seen.add(item["market_hash"])
            unique_items.append(item)

    log.info("Fetching prices for %d unique skins", len(unique_items))
    spike_count = 0

    for i, item in enumerate(unique_items):
        mh = item["market_hash"]
        price = fetch_price(mh)

        if price is None:
            log.info("[%d/%d] %s — no price", i + 1, len(unique_items), mh)
        else:
            log.info(
                "[%d/%d] %s  lowest=$%s  median=$%s  vol=%s",
                i + 1, len(unique_items), mh,
                price["lowest_price"], price["median_price"], price["volume"],
            )

            if not dry_run:
                skin_id = upsert_skin(conn, mh)
                record_price(conn, skin_id, price)

                if price["median_price"] is not None:
                    spiked = check_spike(
                        conn, skin_id, mh,
                        price["median_price"], threshold_pct, rolling_days,
                    )
                    if spiked:
                        spike_count += 1

        # Respect Steam's rate limit between requests (not needed after last item)
        if i < len(unique_items) - 1:
            time.sleep(PRICE_FETCH_DELAY)

    log.info("Done. %d spike(s) detected.", spike_count)
    conn.close()


def cmd_history(market_hash: str, limit: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT p.fetched_at, p.lowest_price, p.median_price, p.volume
           FROM prices p
           JOIN skins s ON s.id = p.skin_id
           WHERE s.market_hash = ?
           ORDER BY p.fetched_at DESC
           LIMIT ?""",
        (market_hash, limit),
    ).fetchall()
    conn.close()

    if not rows:
        print(f"No history for: {market_hash}")
        return

    print(f"\nPrice history for: {market_hash}")
    print(f"{'Date':25}  {'Lowest':>8}  {'Median':>8}  {'Volume':>8}")
    print("-" * 56)
    for fetched_at, lowest, median, volume in rows:
        print(f"{fetched_at[:19]:25}  {f'${lowest:.2f}' if lowest else 'N/A':>8}  "
              f"{f'${median:.2f}' if median else 'N/A':>8}  {volume or 'N/A':>8}")


def cmd_alerts(limit: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT a.alerted_at, s.market_hash, a.current, a.average, a.pct_above
           FROM alerts a
           JOIN skins s ON s.id = a.skin_id
           ORDER BY a.alerted_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()

    if not rows:
        print("No alerts recorded.")
        return

    print(f"\n{'Date':19}  {'Skin':45}  {'Current':>8}  {'Avg':>8}  {'%Above':>7}")
    print("-" * 95)
    for alerted_at, mh, current, avg, pct in rows:
        print(f"{alerted_at[:19]:19}  {mh[:45]:45}  ${current:>7.2f}  ${avg:>7.2f}  {pct:>6.1f}%")


def main() -> None:
    parser = argparse.ArgumentParser(description="CS2 skin price tracker")
    sub = parser.add_subparsers(dest="cmd")

    run_p = sub.add_parser("run", help="Fetch inventory + prices and detect spikes")
    run_p.add_argument("--steam-id", default=STEAM_ID, help="Steam64 ID (or set STEAM_ID env)")
    run_p.add_argument("--threshold", type=float, default=20.0,
                       help="Alert when price is this %% above rolling avg (default 20)")
    run_p.add_argument("--days", type=int, default=7,
                       help="Rolling average window in days (default 7)")
    run_p.add_argument("--dry-run", action="store_true",
                       help="Fetch prices but don't write to DB")

    hist_p = sub.add_parser("history", help="Show price history for a skin")
    hist_p.add_argument("market_hash", help="Market hash name (e.g. 'AK-47 | Redline (Field-Tested)')")
    hist_p.add_argument("--limit", type=int, default=30)

    alerts_p = sub.add_parser("alerts", help="Show recent spike alerts")
    alerts_p.add_argument("--limit", type=int, default=50)

    args = parser.parse_args()

    if args.cmd == "run":
        if not args.steam_id:
            parser.error("Provide --steam-id or set the STEAM_ID environment variable")
        run(
            steam_id=args.steam_id,
            threshold_pct=args.threshold,
            rolling_days=args.days,
            dry_run=args.dry_run,
        )
    elif args.cmd == "history":
        cmd_history(args.market_hash, args.limit)
    elif args.cmd == "alerts":
        cmd_alerts(args.limit)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
