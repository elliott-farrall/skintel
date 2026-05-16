"""CS2 skin price tracker — core library."""

import os
import json
import time
import sqlite3
import logging
import argparse
from datetime import datetime, timezone

import requests
import anthropic

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger(__name__)

DB_PATH              = os.getenv("SKINTEL_DB", "skintel.db")
DISCORD_BOT_TOKEN    = os.getenv("DISCORD_BOT_TOKEN", "")
DISCORD_USER_ID      = os.getenv("DISCORD_USER_ID", "")
STEAM_SESSION_COOKIE = os.getenv("STEAM_SESSION_COOKIE", "")
ANTHROPIC_API_KEY    = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL         = os.getenv("SKINTEL_CLAUDE_MODEL", "claude-haiku-4-5")

APPID = 730

ALERT_COLORS = {
    "sell_signal":  0x4ADE80,  # green
}
ALERT_LABELS = {
    "sell_signal":  "Sell Signal",
}

# GBP exchange rate cache (1-hour TTL)
_gbp_rate: float | None = None
_gbp_rate_ts: float = 0.0


def _get_gbp_rate() -> float:
    global _gbp_rate, _gbp_rate_ts
    now = time.time()
    if _gbp_rate is not None and now - _gbp_rate_ts < 3600:
        return _gbp_rate
    try:
        resp = requests.get("https://open.er-api.com/v6/latest/USD", timeout=10)
        resp.raise_for_status()
        rate = float(resp.json()["rates"]["GBP"])
        _gbp_rate, _gbp_rate_ts = rate, now
        log.info("GBP rate updated: 1 USD = %.4f GBP", rate)
        return rate
    except Exception as exc:
        log.warning("Failed to fetch GBP rate (%s) — using fallback", exc)
        return _gbp_rate if _gbp_rate else 0.79


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
        "ALTER TABLE skins ADD COLUMN image_url TEXT",
        "ALTER TABLE alerts ADD COLUMN reason TEXT",
    ]:
        try:
            conn.execute(sql)
        except Exception:
            pass
    conn.commit()


def get_inventory(steam_id: str, api_key: str) -> list[dict]:
    """Fetch CS2 inventory with embedded prices from steamwebapi.com (prices in GBP)."""
    url = "https://api.steamwebapi.com/steam/api/inventory"
    resp = requests.get(
        url,
        params={"key": api_key, "steam_id": steam_id, "game": "csgo", "parse": 1},
        timeout=30,
    )
    log.info("Inventory: HTTP %d", resp.status_code)
    resp.raise_for_status()
    raw = resp.json()

    entries = raw if isinstance(raw, list) else raw.get("items", raw.get("data", []))
    if entries and not isinstance(entries, list):
        log.warning("Unexpected response shape: %s", list(raw.keys()) if isinstance(raw, dict) else type(raw))
        return []

    if entries:
        log.info("Sample item keys: %s", list(entries[0].keys()))

    gbp = _get_gbp_rate()

    seen: set[str] = set()
    items = []
    for entry in entries:
        if not entry.get("marketable", 1):
            continue
        mh = entry.get("market_hash_name") or entry.get("markethashname")
        if not mh or mh in seen:
            continue
        seen.add(mh)

        def _price(val: str | float | None) -> float | None:
            if val is None:
                return None
            try:
                usd = float(str(val).replace("$", "").replace(",", "").strip())
                return round(usd * gbp, 4)
            except (ValueError, AttributeError):
                return None

        price = {
            "lowest_price": _price(entry.get("pricemin") or entry.get("pricelowest") or entry.get("price_lowest")),
            "median_price": _price(entry.get("priceavg") or entry.get("pricemedian") or entry.get("price") or entry.get("price_avg")),
            "volume":       entry.get("volume") or entry.get("pricesold"),
        }
        image_url = (
            entry.get("image")
            or entry.get("icon_url")
            or entry.get("image_url")
            or entry.get("icon")
        )
        items.append({
            "market_hash": mh,
            "name": entry.get("name", mh),
            "price": price,
            "image_url": image_url,
        })

    log.info("Found %d unique marketable items", len(items))
    return items


def fetch_price_history(market_hash: str) -> list[dict]:
    """Fetch price history from Steam market in GBP (requires steamLoginSecure cookie)."""
    if not STEAM_SESSION_COOKIE:
        log.warning("STEAM_SESSION_COOKIE not set — skipping history fetch")
        return []
    resp = requests.get(
        "https://steamcommunity.com/market/pricehistory/",
        params={"appid": APPID, "market_hash_name": market_hash, "currency": 2},
        headers={
            "User-Agent": "Mozilla/5.0",
            "Cookie": f"steamLoginSecure={STEAM_SESSION_COOKIE}",
        },
        timeout=30,
    )
    if resp.status_code == 429:
        log.warning("Rate limited on history for %s", market_hash)
        return []
    if not resp.ok:
        log.warning("History HTTP %d for %s", resp.status_code, market_hash)
        return []

    data = resp.json()
    if not data.get("success"):
        return []

    rows = []
    for entry in data.get("prices", []):
        try:
            # entry format: ["Nov 7 2013 01:+0", 12.5, "3"] or ["Nov 27 2013 01:+0", ...]
            # Use split to handle both single and double digit days
            date_part = " ".join(str(entry[0]).split()[:3])
            dt = datetime.strptime(date_part, "%b %d %Y").replace(hour=12, tzinfo=timezone.utc)
            rows.append({
                "fetched_at": dt.isoformat(),
                "median_price": float(entry[1]) if entry[1] is not None else None,
                "lowest_price": None,
                "volume": int(float(entry[2])) if entry[2] else None,
            })
        except Exception:
            continue
    return rows


def backfill_history(conn: sqlite3.Connection) -> dict:
    """Fetch historical prices for all tracked skins and insert missing rows."""
    skins = conn.execute("SELECT id, market_hash FROM skins").fetchall()
    total_inserted = 0

    for skin_id, market_hash in skins:
        existing = conn.execute(
            "SELECT COUNT(*) FROM prices WHERE skin_id = ?", (skin_id,)
        ).fetchone()[0]

        if existing >= 30:
            log.info("Skipping backfill for %s — already has %d rows", market_hash, existing)
            continue

        log.info("Backfilling %s", market_hash)
        try:
            rows = fetch_price_history(market_hash)
        except Exception as exc:
            log.warning("History fetch failed for %s: %s", market_hash, exc)
            rows = []

        inserted = 0
        for row in rows:
            try:
                conn.execute(
                    """INSERT INTO prices (skin_id, fetched_at, lowest_price, median_price, volume)
                       VALUES (?, ?, ?, ?, ?)""",
                    (skin_id, row["fetched_at"], row["lowest_price"], row["median_price"], row["volume"]),
                )
                inserted += 1
            except Exception:
                continue
        conn.commit()
        total_inserted += inserted
        log.info("  inserted %d rows for %s", inserted, market_hash)
        time.sleep(0.5)

    return {"skins": len(skins), "inserted": total_inserted}


def upsert_skin(conn: sqlite3.Connection, market_hash: str, image_url: str | None = None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT OR IGNORE INTO skins (market_hash, first_seen) VALUES (?, ?)",
        (market_hash, now),
    )
    if image_url:
        conn.execute(
            "UPDATE skins SET image_url = ? WHERE market_hash = ? AND (image_url IS NULL OR image_url != ?)",
            (image_url, market_hash, image_url),
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


_dm_channel_id: str | None = None


def _send_discord_alert(
    market_hash: str,
    alert_type: str,
    current: float,
    reference: float | None,
    pct_above: float | None,
    image_url: str | None = None,
    reason: str | None = None,
) -> None:
    global _dm_channel_id
    if not DISCORD_BOT_TOKEN or not DISCORD_USER_ID:
        return

    label = ALERT_LABELS.get(alert_type, alert_type.replace("_", " ").title())
    color = ALERT_COLORS.get(alert_type, 0x4ADE80)

    fields = [{"name": "Current price", "value": f"£{current:.2f}", "inline": True}]
    if reference is not None:
        fields.append({"name": "Reference", "value": f"£{reference:.2f}", "inline": True})
    if pct_above is not None:
        sign = "+" if pct_above >= 0 else ""
        fields.append({"name": "Change", "value": f"{sign}{pct_above:.1f}%", "inline": True})
    if reason:
        fields.append({"name": "Analysis", "value": reason, "inline": False})

    embed: dict = {
        "title": f"{label}: {market_hash}",
        "color": color,
        "fields": fields,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if image_url:
        embed["thumbnail"] = {"url": image_url}

    headers = {"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
    try:
        if _dm_channel_id is None:
            dm = requests.post(
                "https://discord.com/api/v10/users/@me/channels",
                json={"recipient_id": DISCORD_USER_ID},
                headers=headers,
                timeout=10,
            )
            dm.raise_for_status()
            _dm_channel_id = dm.json()["id"]

        for attempt in range(2):
            msg = requests.post(
                f"https://discord.com/api/v10/channels/{_dm_channel_id}/messages",
                json={"embeds": [embed]},
                headers=headers,
                timeout=10,
            )
            if msg.status_code == 429:
                retry_after = float(msg.json().get("retry_after", 5))
                log.warning("Discord rate limited — waiting %.1fs", retry_after)
                time.sleep(retry_after)
                continue
            if msg.status_code not in (200, 201):
                log.warning("Discord DM returned %d: %s", msg.status_code, msg.text[:200])
            break
    except Exception as exc:
        log.warning("Discord DM failed: %s", exc)


ALERT_COOLDOWN_HOURS = 24


def _insert_alert(
    conn: sqlite3.Connection,
    skin_id: int,
    alert_type: str,
    current: float,
    reference: float | None,
    pct_above: float | None,
    reason: str | None = None,
) -> None:
    # Suppress if same skin+type alerted within the cooldown window
    recent = conn.execute(
        """SELECT 1 FROM alerts
           WHERE skin_id = ? AND alert_type = ?
             AND alerted_at >= datetime('now', ?)
           LIMIT 1""",
        (skin_id, alert_type, f"-{ALERT_COOLDOWN_HOURS} hours"),
    ).fetchone()
    if recent:
        return

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO alerts (skin_id, alerted_at, alert_type, current, reference, pct_above, reason)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (skin_id, now, alert_type, current, reference, pct_above, reason),
    )
    conn.commit()

    row = conn.execute("SELECT market_hash, image_url FROM skins WHERE id = ?", (skin_id,)).fetchone()
    if row:
        _send_discord_alert(row[0], alert_type, current, reference, pct_above, row[1], reason)


def check_sell_signal(
    conn: sqlite3.Connection,
    skin_id: int,
    market_hash: str,
    current_price: float,
) -> bool:
    """Use Claude to analyse price history and decide if now is a good time to sell."""
    if not ANTHROPIC_API_KEY or current_price is None:
        return False

    rows = conn.execute(
        """SELECT fetched_at, median_price, volume FROM prices
           WHERE skin_id = ? AND median_price IS NOT NULL
           ORDER BY fetched_at DESC LIMIT 30""",
        (skin_id,),
    ).fetchall()

    if len(rows) < 5:
        return False

    history_lines = "\n".join(
        f"{r[0][:10]}: £{r[1]:.2f}" + (f" (vol {r[2]})" if r[2] else "")
        for r in reversed(rows)
    )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    try:
        resp = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=256,
            system=(
                "You are a CS2 skin market analyst helping a player decide when to sell. "
                "The player wants to sell at or near price peaks and avoid selling during dips. "
                "Analyse the price history trend and current price. "
                "Recommend selling if the price is near a recent high, has risen significantly from its average, "
                "or shows signs of reversing after a rise. "
                "Recommend holding if the price is stable, near a recent low, or still in an uptrend. "
                "Reply ONLY with valid JSON in this exact format: "
                '{\"recommend_sell\": true, \"reason\": \"one sentence explanation\"}'
            ),
            messages=[{
                "role": "user",
                "content": (
                    f"Skin: {market_hash}\n"
                    f"Current price: £{current_price:.2f}\n\n"
                    f"Price history (oldest to newest):\n{history_lines}"
                ),
            }],
        )
        text = resp.content[0].text.strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        data = json.loads(text[start:end])
        recommend_sell = bool(data.get("recommend_sell", False))
        reason = str(data.get("reason", ""))
    except Exception as exc:
        log.warning("Claude sell check failed for %s: %s", market_hash, exc)
        return False

    if recommend_sell:
        log.info("SELL SIGNAL  %s  £%.2f  reason=%s", market_hash, current_price, reason)
        _insert_alert(conn, skin_id, "sell_signal", current_price, None, None, reason=reason)
    return recommend_sell


def ingest(
    items: list[dict],
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Store prices and run sell-signal analysis."""
    close_after = conn is None
    if conn is None:
        conn = sqlite3.connect(DB_PATH)
        init_db(conn)

    stored = 0
    alert_count = 0

    for item in items:
        mh = item["market_hash"]
        price = item.get("price")
        if not price:
            continue

        skin_id = upsert_skin(conn, mh, item.get("image_url"))
        record_price(conn, skin_id, price)
        stored += 1

        if price.get("median_price") is not None:
            if check_sell_signal(conn, skin_id, mh, price["median_price"]):
                alert_count += 1

    if close_after:
        conn.close()
    log.info("Ingest complete. %d stored, %d sell signal(s).", stored, alert_count)
    return {"stored": stored, "alerts": alert_count}


# ── CLI (local debugging only) ────────────────────────────────────────────────

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
            f"{f'£{lowest:.2f}' if lowest else 'N/A':>8}  "
            f"{f'£{median:.2f}' if median else 'N/A':>8}  "
            f"{volume or 'N/A':>8}"
        )


def _cmd_alerts(limit: int) -> None:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute(
        """SELECT a.alerted_at, a.alert_type, s.market_hash, a.current, a.reference, a.pct_above, a.reason
           FROM alerts a JOIN skins s ON s.id = a.skin_id
           ORDER BY a.alerted_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    if not rows:
        print("No alerts recorded.")
        return
    print(f"\n{'Date':19}  {'Type':12}  {'Skin':40}  {'Current':>8}  {'Reason'}")
    print("-" * 110)
    for alerted_at, alert_type, mh, current, reference, pct, reason in rows:
        print(
            f"{alerted_at[:19]:19}  {(alert_type or 'spike'):12}  {mh[:40]:40}  "
            f"£{current:>7.2f}  {reason or ''}"
        )


def main() -> None:
    import sys
    parser = argparse.ArgumentParser(description="CS2 skin price tracker")
    sub = parser.add_subparsers(dest="cmd")

    hist_p = sub.add_parser("history", help="Show price history for a skin")
    hist_p.add_argument("market_hash")
    hist_p.add_argument("--limit", type=int, default=30)

    alerts_p = sub.add_parser("alerts", help="Show recent alerts")
    alerts_p.add_argument("--limit", type=int, default=50)

    args = parser.parse_args()

    if args.cmd == "history":
        _cmd_history(args.market_hash, args.limit)
    elif args.cmd == "alerts":
        _cmd_alerts(args.limit)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
