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
MIN_SELL_PRICE       = float(os.getenv("MIN_SELL_PRICE", "1.00"))  # GBP — skip cheaper skins entirely

# Lazy singleton — constructed once on first use to avoid startup overhead
_anthropic_client: "anthropic.Anthropic | None" = None


def _get_anthropic_client() -> "anthropic.Anthropic":
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _anthropic_client

APPID = 730

# CS2 standard rarity colours — used as a fallback when the API doesn't return a hex value
_RARITY_NAME_TO_COLOR: dict[str, str] = {
    "consumer":    "b0c3d9",
    "industrial":  "5e98d9",
    "mil-spec":    "4b69ff",
    "milspec":     "4b69ff",
    "restricted":  "8847ff",
    "classified":  "d32ce6",
    "covert":      "eb4b4b",
    "contraband":  "e4ae39",
    "extraordinary": "ffd700",
    "master":      "ffd700",
    "high grade":  "4b69ff",
    "remarkable":  "8847ff",
    "exotic":      "d32ce6",
    "distinguished": "5e98d9",
}

ALERT_COLORS = {
    "sell_signal":  0x4ADE80,  # green
}
ALERT_LABELS = {
    "sell_signal":  "Sell Signal",
}

_STEAM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; Skintel)",
}


def _steam_headers() -> dict:
    h = dict(_STEAM_HEADERS)
    if STEAM_SESSION_COOKIE:
        h["Cookie"] = f"steamLoginSecure={STEAM_SESSION_COOKIE}"
    return h


def _parse_gbp(val: str | float | None) -> float | None:
    if val is None:
        return None
    try:
        return round(float(str(val).replace("£", "").replace(",", "").strip()), 4)
    except (ValueError, AttributeError):
        return None


def connect(path: str | None = None) -> sqlite3.Connection:
    """Open a SQLite connection with WAL + busy_timeout for safe concurrent access."""
    conn = sqlite3.connect(path or DB_PATH, timeout=30.0)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


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

        CREATE INDEX IF NOT EXISTS idx_prices_skin_fetched
            ON prices(skin_id, fetched_at);
    """)
    for sql in [
        "ALTER TABLE alerts ADD COLUMN alert_type TEXT NOT NULL DEFAULT 'spike'",
        "ALTER TABLE alerts ADD COLUMN reference REAL",
        "ALTER TABLE skins ADD COLUMN image_url TEXT",
        "ALTER TABLE alerts ADD COLUMN reason TEXT",
        "ALTER TABLE skins ADD COLUMN rarity_color TEXT",
    ]:
        try:
            conn.execute(sql)
        except Exception:
            pass
    conn.commit()


def get_inventory(steam_id: str) -> list[dict]:
    """Fetch CS2 inventory from Steam and current GBP prices from Steam Market."""
    # Step 1: public inventory endpoint — no auth required for public profiles
    inv_resp = requests.get(
        f"https://steamcommunity.com/inventory/{steam_id}/730/2",
        params={"l": "english", "count": 5000},
        headers=_steam_headers(),
        timeout=30,
    )
    log.info("Inventory: HTTP %d", inv_resp.status_code)
    inv_resp.raise_for_status()
    inv_data = inv_resp.json()

    if not inv_data.get("success"):
        log.warning("Steam inventory returned success=false — profile may be private")
        return []

    desc_map = {
        (d["classid"], d["instanceid"]): d
        for d in inv_data.get("descriptions", [])
    }

    seen: set[str] = set()
    to_price: list[dict] = []
    for asset in inv_data.get("assets", []):
        desc = desc_map.get((asset["classid"], asset["instanceid"]))
        if not desc or not desc.get("marketable"):
            continue
        mh = desc.get("market_hash_name")
        if not mh or mh in seen:
            continue
        seen.add(mh)

        rarity_color: str | None = None
        rarity_name: str = ""
        for tag in desc.get("tags", []):
            if tag.get("category") == "Rarity":
                rarity_color = tag.get("color", "").lower().lstrip("#") or None
                rarity_name = tag.get("localized_tag_name", "")
                break
        # Name-based fallback in case the tag has no colour hex
        if not rarity_color:
            rarity_lower = rarity_name.lower()
            for keyword, hex_color in _RARITY_NAME_TO_COLOR.items():
                if keyword in rarity_lower:
                    rarity_color = hex_color
                    break

        icon = desc.get("icon_url", "")
        image_url = f"https://steamcommunity-a.akamaihd.net/economy/image/{icon}" if icon else None

        to_price.append({
            "market_hash": mh,
            "image_url": image_url,
            "rarity_color": rarity_color,
        })

    log.info("Found %d unique marketable items — fetching prices", len(to_price))

    # Step 2: fetch current GBP price for each item from Steam Market
    items: list[dict] = []
    for item in to_price:
        try:
            pr = requests.get(
                "https://steamcommunity.com/market/priceoverview/",
                params={"appid": APPID, "currency": 2, "market_hash_name": item["market_hash"]},
                headers=_steam_headers(),
                timeout=15,
            )
            if pr.status_code == 429:
                log.warning("Rate limited fetching price for %s — skipping", item["market_hash"])
                time.sleep(5)
                continue
            if not pr.ok:
                log.warning("Price HTTP %d for %s", pr.status_code, item["market_hash"])
                continue
            p = pr.json()
            item["price"] = {
                "lowest_price": _parse_gbp(p.get("lowest_price")),
                "median_price": _parse_gbp(p.get("median_price")),
                "volume": int(str(p["volume"]).replace(",", "")) if p.get("volume") else None,
            }
            items.append(item)
        except Exception as exc:
            log.warning("Price fetch failed for %s: %s", item["market_hash"], exc)
        time.sleep(0.5)

    log.info("Priced %d/%d items", len(items), len(to_price))
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


def upsert_skin(
    conn: sqlite3.Connection,
    market_hash: str,
    image_url: str | None = None,
    rarity_color: str | None = None,
) -> int:
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
    if rarity_color:
        conn.execute(
            "UPDATE skins SET rarity_color = ? WHERE market_hash = ? AND (rarity_color IS NULL OR rarity_color != ?)",
            (rarity_color, market_hash, rarity_color),
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
    """Use Claude to detect if a skin has risen significantly and is near its peak."""
    if not ANTHROPIC_API_KEY or current_price is None:
        return False

    # Skip cheap skins — not worth the noise or API call
    if current_price < MIN_SELL_PRICE:
        return False

    rows = conn.execute(
        """SELECT fetched_at, median_price, volume FROM prices
           WHERE skin_id = ? AND median_price IS NOT NULL
           ORDER BY fetched_at DESC LIMIT 60""",
        (skin_id,),
    ).fetchall()

    if len(rows) < 7:
        return False

    prices = [r[1] for r in rows]   # newest first

    avg_all    = sum(prices) / len(prices)
    avg_recent = sum(prices[:5]) / 5
    avg_older  = sum(prices[5:]) / (len(prices) - 5)
    ath        = max(prices)
    pct_vs_avg = (current_price - avg_all) / avg_all * 100
    pct_vs_ath = (current_price - ath) / ath * 100           # negative means below ATH
    trend_pct  = (avg_recent - avg_older) / avg_older * 100  # positive = rising recently

    history_lines = "\n".join(
        f"{r[0][:10]}: £{r[1]:.2f}" + (f" (vol {r[2]})" if r[2] else "")
        for r in reversed(rows)
    )

    context = (
        f"Skin: {market_hash}\n"
        f"Current price: £{current_price:.2f}\n"
        f"Historical average ({len(rows)} data points): £{avg_all:.2f} "
        f"(current is {pct_vs_avg:+.1f}% vs average)\n"
        f"All-time high in history: £{ath:.2f} "
        f"(current is {pct_vs_ath:+.1f}% vs ATH)\n"
        f"Recent trend: avg of last 5 = £{avg_recent:.2f} vs prior avg = £{avg_older:.2f} "
        f"({trend_pct:+.1f}%)\n\n"
        f"Price history (oldest → newest):\n{history_lines}"
    )

    try:
        resp = _get_anthropic_client().messages.create(
            model=CLAUDE_MODEL,
            max_tokens=200,
            system=(
                "You are a CS2 skin market analyst. Your job is to identify the optimal moment to sell: "
                "when a skin has risen substantially from its baseline price and appears to be at or near a peak.\n\n"
                "Recommend SELL only if:\n"
                "- The current price is notably above its historical average (a real rise, not noise)\n"
                "- The price appears to be levelling off or showing early signs of a reversal after rising\n"
                "- Selling now captures most of the gain before a likely pullback\n\n"
                "Recommend HOLD if:\n"
                "- The price is still actively climbing — don't sell too early\n"
                "- The price is near its average or recent lows — the rise hasn't happened yet\n"
                "- The movement looks like normal daily noise rather than a meaningful spike\n\n"
                'Reply ONLY with JSON: {"recommend_sell": true, "reason": "one concise sentence"}'
            ),
            messages=[{"role": "user", "content": context}],
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
        log.info("SELL SIGNAL  %s  £%.2f (+%.1f%% vs avg)  reason=%s",
                 market_hash, current_price, pct_vs_avg, reason)
        _insert_alert(conn, skin_id, "sell_signal", current_price, avg_all, pct_vs_avg, reason=reason)
    return recommend_sell


def ingest(
    items: list[dict],
    conn: sqlite3.Connection | None = None,
) -> dict:
    """Store prices and run sell-signal analysis."""
    close_after = conn is None
    if conn is None:
        conn = connect()
        init_db(conn)

    stored = 0
    alert_count = 0

    for item in items:
        mh = item["market_hash"]
        price = item.get("price")
        if not price:
            continue

        skin_id = upsert_skin(conn, mh, item.get("image_url"), item.get("rarity_color"))
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
    conn = connect()
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
    conn = connect()
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
