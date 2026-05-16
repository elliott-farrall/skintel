# Skintel

Automated CS2 inventory price tracker. Polls your Steam inventory on a schedule, stores price history, and uses Claude to identify when a skin has risen substantially and is near a peak — then DMs you on Discord.

## How it works

```
            ┌────────────────────────┐
            │  APScheduler (in-app)  │  every SCHEDULE_HOURS (default 6h)
            └──────────┬─────────────┘
                       │ triggers run_collect()
                       ▼
            ┌────────────────────────┐
            │  steamcommunity.com    │  fetch inventory (public API)
            │  /market/priceoverview │  fetch current GBP price per item
            └──────────┬─────────────┘
                       │
            ┌────────────────────────┐
            │  SQLite (/data/...)    │  upsert skins, append a row to prices
            └──────────┬─────────────┘
                       │ for each skin priced >= MIN_SELL_PRICE
                       ▼
            ┌────────────────────────┐
            │  Claude (haiku-4-5)    │  analyse history → recommend sell/hold
            └──────────┬─────────────┘
                       │ if recommend_sell
                       ▼
            ┌────────────────────────┐
            │  Discord DM (bot API)  │  embed with current £, avg £, reason
            └────────────────────────┘
```

Separately, on demand:

- **`Run Now`** (dashboard button) — fires a one-off `run_collect()` immediately.
- **`Backfill History`** (dashboard button) — for every tracked skin with fewer than 30 stored data points, fetches up to a year of daily prices from Steam's market `pricehistory` endpoint (requires `STEAM_SESSION_COOKIE`) and inserts them. This populates the sparkline charts and gives Claude more signal to work with.

The dashboard auto-refreshes every 5 minutes via `setInterval`, but that only re-renders from the DB — it does not trigger a new inventory fetch.

## Dashboard

Cards are ordered with AI sell-signal alerts first, then by recent momentum (largest recent rise on top), then by price.

Each card may show one or more badges, computed from the last ~30 stored price points:

| Badge       | Meaning                                                            | Source |
| ----------- | ------------------------------------------------------------------ | ------ |
| **Sell**    | Claude has flagged this as a peak — also sent as a Discord DM      | AI     |
| **ATH**     | Current price is within 3% of its all-time high in stored history  | static |
| **↑ X%**    | Avg of last 5 prices is ≥10% higher than the prior history         | static |
| **↓ X%**    | Avg of last 5 prices is ≥10% lower than the prior history          | static |

Static badges update every collect cycle; they don't fire Discord notifications.

## Data model

```
skins (id, market_hash, first_seen, image_url, rarity_color)
prices (id, skin_id, fetched_at, lowest_price, median_price, volume)
alerts (id, skin_id, alerted_at, alert_type, current, reference, pct_above, reason)
```

Prices are stored in **GBP** (converted from USD on ingest, or fetched in GBP for backfill via `currency=2`). One `prices` row per skin per `run_collect()`.

Alerts have a 24-hour per-(skin, type) cooldown to prevent spam.

## Schedule cheat sheet

| Event                  | When                           | Hits external APIs        |
| ---------------------- | ------------------------------ | ------------------------- |
| Scheduled collect      | every `SCHEDULE_HOURS` (6h)    | steamwebapi, Claude, exchange-rate |
| Manual "Run Now"       | button click                   | same as above             |
| Manual "Backfill"      | button click                   | Steam market pricehistory |
| Dashboard auto-refresh | every 5 minutes                | none — just re-renders    |
| GBP rate refresh       | first request after 1h TTL     | open.er-api.com           |

## Environment variables

| Variable               | Required | Default          | Purpose                                                    |
| ---------------------- | -------- | ---------------- | ---------------------------------------------------------- |
| `STEAM_ID`             | yes      | —                | Your 64-bit Steam ID                                       |
| `ANTHROPIC_API_KEY`    | yes      | —                | Claude API key for sell-signal analysis                    |
| `DASHBOARD_PASS`       | yes      | (empty = locked) | HTTP Basic Auth password                                   |
| `STEAM_SESSION_COOKIE` | recommended | —             | `steamLoginSecure` cookie — improves price-fetch rate limits and enables history backfill |
| `DISCORD_BOT_TOKEN`    | for alerts | —              | Bot token (must share a server with you or be user-installed) |
| `DISCORD_USER_ID`      | for alerts | —              | Your Discord user ID (DM target)                           |
| `DASHBOARD_USER`       | no       | `admin`          | HTTP Basic Auth username                                   |
| `SKINTEL_DB`           | no       | `skintel.db`     | SQLite path (Fly volume mounts `/data/skintel.db`)         |
| `SCHEDULE_HOURS`       | no       | `6`              | Collect interval in hours                                  |
| `MIN_SELL_PRICE`       | no       | `1.00`           | Skip Claude analysis for skins below this £ value          |
| `SKINTEL_CLAUDE_MODEL` | no       | `claude-haiku-4-5` | Model used for sell-signal analysis                      |
| `PORT`                 | no       | `8080`           | HTTP port                                                  |

## Local development

```bash
pip install -r requirements.txt
export STEAM_ID=... STEAMWEBAPI_KEY=... ANTHROPIC_API_KEY=... DASHBOARD_PASS=...
python web.py
```

Then open <http://localhost:8080> and log in with `admin` / your password.

CLI for inspecting the DB:

```bash
python tracker.py history "AWP | Atheris (Field-Tested)"
python tracker.py alerts --limit 20
```

## Deployment (Fly.io)

`Dockerfile` and `fly.toml` are pre-configured for a single 256MB VM in `lhr` with a persistent volume mounted at `/data`. Secrets are set via `fly secrets set KEY=VALUE`.

Production runs **gunicorn** (`1 worker, 4 threads`) — not Flask's dev server. APScheduler runs inside the gunicorn worker process. The SQLite DB uses WAL mode + a 10s busy-timeout so the scheduler thread, request threads, and any manual run/backfill can interleave safely.
