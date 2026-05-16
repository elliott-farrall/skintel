"""Flask web app — dashboard, scheduler, and API."""

import os
import threading
import sqlite3
import logging
import functools

from flask import Flask, render_template, jsonify, request, Response
from apscheduler.schedulers.background import BackgroundScheduler

import tracker as tr

log = logging.getLogger(__name__)

DASHBOARD_USER   = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS   = os.getenv("DASHBOARD_PASS", "")
STEAM_ID         = os.getenv("STEAM_ID", "")
STEAMWEBAPI_KEY  = os.getenv("STEAMWEBAPI_KEY", "")
SCHEDULE_HOURS   = int(os.getenv("SCHEDULE_HOURS", "6"))

app = Flask(__name__)
_run_lock = threading.Lock()
_backfill_lock = threading.Lock()


def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or auth.username != DASHBOARD_USER or auth.password != DASHBOARD_PASS:
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="Skintel"'},
            )
        return f(*args, **kwargs)
    return decorated


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(tr.DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def run_collect() -> None:
    if not _run_lock.acquire(blocking=False):
        log.info("Collect already running — skipping")
        return
    try:
        items = tr.get_inventory(STEAM_ID, STEAMWEBAPI_KEY)
        if not items:
            log.info("No marketable items — nothing to ingest")
            return
        payload = [
            {"market_hash": i["market_hash"], "price": i["price"], "image_url": i.get("image_url")}
            for i in items if i.get("price")
        ]
        conn = sqlite3.connect(tr.DB_PATH)
        tr.init_db(conn)
        tr.ingest(payload, conn=conn)
        conn.close()
    except Exception:
        log.exception("Collect failed")
    finally:
        _run_lock.release()


def run_backfill() -> None:
    if not _backfill_lock.acquire(blocking=False):
        log.info("Backfill already running — skipping")
        return
    try:
        conn = sqlite3.connect(tr.DB_PATH)
        tr.init_db(conn)
        result = tr.backfill_history(conn)
        conn.close()
        log.info("Backfill complete: %s", result)
    except Exception:
        log.exception("Backfill failed")
    finally:
        _backfill_lock.release()


@app.route("/")
@require_auth
def dashboard():
    return render_template("dashboard.html", steam_id=STEAM_ID)


@app.route("/api/skins")
@require_auth
def api_skins():
    conn = get_db()

    skins = conn.execute("""
        SELECT s.id, s.market_hash, s.image_url,
               p.lowest_price, p.median_price, p.volume, p.fetched_at
        FROM skins s
        LEFT JOIN prices p ON p.id = (
            SELECT id FROM prices WHERE skin_id = s.id
            ORDER BY fetched_at DESC LIMIT 1
        )
    """).fetchall()

    recent_alerts = conn.execute("""
        SELECT skin_id, alert_type, current, reference, pct_above, alerted_at
        FROM alerts
        WHERE alerted_at >= datetime('now', '-24 hours')
        ORDER BY alerted_at DESC
    """).fetchall()

    # Fetch chart history for all skins in one query (newest 90 per skin)
    skin_ids = [s["id"] for s in skins]
    history_by_skin: dict[int, list] = {}
    if skin_ids:
        placeholders = ",".join("?" * len(skin_ids))
        hist_rows = conn.execute(f"""
            SELECT skin_id, fetched_at, median_price
            FROM (
                SELECT skin_id, fetched_at, median_price,
                       ROW_NUMBER() OVER (PARTITION BY skin_id ORDER BY fetched_at DESC) AS rn
                FROM prices
                WHERE skin_id IN ({placeholders}) AND median_price IS NOT NULL
            )
            WHERE rn <= 90
            ORDER BY skin_id, fetched_at ASC
        """, skin_ids).fetchall()
        for row in hist_rows:
            history_by_skin.setdefault(row["skin_id"], []).append({
                "fetched_at": row["fetched_at"],
                "median_price": row["median_price"],
            })

    conn.close()

    alerts_by_skin: dict[int, list] = {}
    for a in recent_alerts:
        sid = a["skin_id"]
        alerts_by_skin.setdefault(sid, []).append(dict(a))

    result = []
    for s in skins:
        sid = s["id"]
        result.append({
            "id": sid,
            "market_hash": s["market_hash"],
            "image_url": s["image_url"],
            "lowest_price": s["lowest_price"],
            "median_price": s["median_price"],
            "volume": s["volume"],
            "fetched_at": s["fetched_at"],
            "alerts": alerts_by_skin.get(sid, []),
            "history": history_by_skin.get(sid, []),
        })

    result.sort(key=lambda x: (-len(x["alerts"]), -(x["median_price"] or 0)))
    return jsonify(result)


@app.route("/api/skins/<int:skin_id>/history")
@require_auth
def api_history(skin_id: int):
    conn = get_db()
    rows = conn.execute(
        """SELECT fetched_at, lowest_price, median_price, volume
           FROM prices WHERE skin_id = ?
           ORDER BY fetched_at ASC LIMIT 365""",
        (skin_id,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/run", methods=["POST"])
@require_auth
def api_run():
    if _run_lock.locked():
        return jsonify({"status": "already_running"})
    t = threading.Thread(target=run_collect, daemon=True)
    t.start()
    return jsonify({"status": "started"})


@app.route("/api/backfill", methods=["POST"])
@require_auth
def api_backfill():
    if _backfill_lock.locked():
        return jsonify({"status": "already_running"})
    t = threading.Thread(target=run_backfill, daemon=True)
    t.start()
    return jsonify({"status": "started"})


@app.route("/api/status")
@require_auth
def api_status():
    conn = get_db()
    row = conn.execute("SELECT MAX(fetched_at) as last_run FROM prices").fetchone()
    conn.close()
    return jsonify({
        "last_run": row["last_run"] if row else None,
        "running": _run_lock.locked(),
        "backfilling": _backfill_lock.locked(),
    })


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    conn = sqlite3.connect(tr.DB_PATH)
    tr.init_db(conn)
    conn.close()

    scheduler = BackgroundScheduler()
    scheduler.add_job(run_collect, "interval", hours=SCHEDULE_HOURS)
    scheduler.start()

    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
