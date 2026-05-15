"""Flask web app + APScheduler background tracker."""

import os
import sqlite3
import logging
import threading
import functools

from flask import Flask, render_template, jsonify, request, Response
from apscheduler.schedulers.background import BackgroundScheduler

import tracker as tr

log = logging.getLogger(__name__)

DASHBOARD_USER = os.getenv("DASHBOARD_USER", "admin")
DASHBOARD_PASS = os.getenv("DASHBOARD_PASS", "")
STEAM_ID = os.getenv("STEAM_ID", "")
TRACKER_HOURS = int(os.getenv("TRACKER_INTERVAL_HOURS", "6"))
SPIKE_THRESHOLD = float(os.getenv("SPIKE_THRESHOLD", "20"))
ROLLING_DAYS = int(os.getenv("ROLLING_DAYS", "7"))

app = Flask(__name__)
_run_lock = threading.Lock()


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


@app.route("/")
@require_auth
def dashboard():
    return render_template("dashboard.html", steam_id=STEAM_ID)


@app.route("/api/skins")
@require_auth
def api_skins():
    conn = get_db()

    skins = conn.execute("""
        SELECT s.id, s.market_hash,
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
            "lowest_price": s["lowest_price"],
            "median_price": s["median_price"],
            "volume": s["volume"],
            "fetched_at": s["fetched_at"],
            "alerts": alerts_by_skin.get(sid, []),
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
           ORDER BY fetched_at DESC LIMIT 120""",
        (skin_id,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in reversed(rows)])


@app.route("/api/run", methods=["POST"])
@require_auth
def api_run():
    if not _run_lock.acquire(blocking=False):
        return jsonify({"status": "already_running"}), 409

    def do_run():
        try:
            tr.run(STEAM_ID, SPIKE_THRESHOLD, ROLLING_DAYS)
        except Exception as exc:
            log.error("Manual run failed: %s", exc)
        finally:
            _run_lock.release()

    threading.Thread(target=do_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/status")
@require_auth
def api_status():
    acquired = _run_lock.acquire(blocking=False)
    if acquired:
        _run_lock.release()
    return jsonify({
        "running": not acquired,
        "interval_hours": TRACKER_HOURS,
    })


def scheduled_run():
    if not _run_lock.acquire(blocking=False):
        log.info("Skipping scheduled run — already running")
        return
    try:
        tr.run(STEAM_ID, SPIKE_THRESHOLD, ROLLING_DAYS)
    except Exception as exc:
        log.error("Scheduled run failed: %s", exc)
    finally:
        _run_lock.release()


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
    scheduler.add_job(scheduled_run, "interval", hours=TRACKER_HOURS)
    scheduler.start()

    port = int(os.getenv("PORT", "8080"))
    app.run(host="0.0.0.0", port=port, use_reloader=False)
