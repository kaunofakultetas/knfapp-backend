#!/usr/bin/env python3
############################################################
#  [*] Main — the knfapp-backend process entry point
#
#  Builds the Flask app through create_app() and serves it
#  with socketio.run() — Flask-SocketIO's wrapper around the
#  Werkzeug dev server — so plain HTTP under /api/* and the
#  Socket.IO long-polling traffic under /socket.io/* share
#  one port. There is no production WSGI server in front of
#  it: gunicorn sits in requirements.txt but nothing runs
#  it, which is why allow_unsafe_werkzeug=True is passed —
#  in a TTY-less container Flask-SocketIO refuses to start
#  Werkzeug in threading mode without that flag.
#
#  This module also owns everything that must happen once
#  per SERVING process and nowhere else: the logging
#  configuration (without it every logger.info in the app
#  was discarded), the scraper_runs reconciliation, the
#  scraper scheduler start, and the shutdown hooks.
#
#    --http           serve; without it the argparse help is
#                     printed and the process exits 0 —
#                     without building an app, migrating the
#                     database or starting a scrape
#    --host / --port  bind address, default 0.0.0.0:8000.
#                     Nothing passes them: the Dockerfile CMD
#                     is "python main.py --http", and both its
#                     EXPOSE line and the Caddyfile upstream
#                     (knfapp-backend:8000) assume the default
#    APP_DEBUG=1      Flask debug flag ONLY — never the
#                     interactive debugger (it would be
#                     reachable through the public /api
#                     ingress) and never the reloader (it
#                     started every scheduler and timer twice)
#    LOG_LEVEL        basicConfig level, default INFO
#    SCRAPER_ENABLED  "0" leaves the background scrapers off;
#                     anything else (default) starts them
#
#  Used by:
#    - backend/Dockerfile — CMD ["python", "main.py", "--http"]
############################################################


import argparse
import atexit
import logging
import os
import signal
import sys

from app import create_app, socketio
from app.database import get_db, utc_now_iso

logger = logging.getLogger(__name__)








############################################################
# _configure_logging
############################################################
#
# The one logging setup in the backend. Nothing called
# basicConfig anywhere, so every logger.info in the app —
# scrape results, rate-limit hits, the first-boot admin
# password — went to the root logger's do-nothing handler
# and vanished; only WARNING and above got through, and
# unformatted at that. Level from LOG_LEVEL (INFO default),
# stream stdout so docker logs collects it.
#
# Used by:
#   - main (below) — first statement, before create_app()
############################################################

def _configure_logging():
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )








############################################################
# _reconcile_scraper_runs
############################################################
#
# Marks every scraper_runs row still in status 'running' as
# failed, once, at boot. A scrape interrupted by a restart
# (or by the old non-daemon startup timers) left its row
# 'running' forever, so /api/scraper/status reported a
# scrape in flight that no thread was doing. Runs AFTER
# create_app, which is what pins get_db()'s path.
#
# Used by:
#   - main (below) — the --http branch, before the scheduler
############################################################

def _reconcile_scraper_runs(app):
    with app.app_context():
        db = get_db()
        try:
            cursor = db.execute(
                """UPDATE scraper_runs
                      SET status = 'failed', error_message = 'interrupted', finished_at = ?
                    WHERE status = 'running'""",
                (utc_now_iso(),),
            )
            db.commit()
            if cursor.rowcount:
                logger.warning("Reconciled %d scraper run(s) left 'running' by an interrupted process", cursor.rowcount)
        finally:
            db.close()








############################################################
# _stop_scrapers
############################################################
#
# Shuts the scraper scheduler down on the way out — atexit
# and SIGTERM both land here, so `docker compose stop` no
# longer kills the process mid-scrape with jobs still
# queued. stop_scraper_scheduler lives in
# scraper/scheduler.py (the scrapers package owns that
# file); until it exists there is simply nothing to stop —
# the scheduler thread and its timers are daemons and die
# with the process.
#
# Used by:
#   - main (below) — atexit.register
#   - _handle_sigterm (below), through the atexit chain
############################################################

def _stop_scrapers():
    try:
        from app.scraper.scheduler import stop_scraper_scheduler
    except ImportError:
        return

    try:
        stop_scraper_scheduler()
    except Exception:
        logger.exception("Scraper scheduler shutdown failed")








############################################################
# _handle_sigterm
############################################################
#
# Turns the container stop signal into a normal interpreter
# exit, so the atexit hook above actually runs. Without it
# SIGTERM's default disposition kills the process outright
# and nothing gets a chance to shut down.
#
# Used by:
#   - main (below) — signal.signal(SIGTERM, ...)
############################################################

def _handle_sigterm(signum, frame):
    logger.info("SIGTERM received — shutting down")
    raise SystemExit(0)








############################################################
# main
############################################################
#
# Parse the CLI, then either print the help and stop, or
# configure logging, build the app and serve it. Nothing is
# built before --http is checked: create_app() migrates the
# database and `python main.py` with no flags used to do
# all of that (and sit through a full scrape round) just to
# print a usage message.
#
# Debug is gated on APP_DEBUG being exactly "1": the
# commented-out "APP_DEBUG=true" line in docker-compose.yml
# would NOT turn it on, and the APP_DEBUG=0 in .env never
# reaches the container (compose does not forward it). Both
# of the dangerous halves of that flag are now pinned off
# regardless of its value:
#
#   - use_debugger=False — the Werkzeug interactive debugger
#     would otherwise be an arbitrary-code-execution console
#     on a process the ingress publishes at /api
#   - use_reloader=False — the reloader re-executes this
#     script in a child, which meant two app builds, two
#     schedulers, two sets of startup timers and two
#     concurrent init_db runs
#
# Used by:
#   - the __main__ block below — nothing imports this module
############################################################

def main():
    # STEP 1: CLI first — no app, no database, no threads
    # ===================================================
    parser = argparse.ArgumentParser(description="knfapp-backend")
    parser.add_argument("--http", action="store_true", help="Run HTTP server")
    parser.add_argument("--port", type=int, default=8000, help="Port (default 8000)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host (default 0.0.0.0)")
    args = parser.parse_args()

    if not args.http:
        parser.print_help()
        return


    # STEP 2: logging before anything can log, then the app
    # (schema, blueprints, socket events, hooks)
    # =====================================================
    _configure_logging()
    app = create_app()
    _reconcile_scraper_runs(app)


    # STEP 3: the background scrapers, in this process only —
    # and a way to stop them again
    # =======================================================
    if os.environ.get("SCRAPER_ENABLED", "1") != "0":
        from app.scraper.scheduler import start_scraper_scheduler

        start_scraper_scheduler(app)
        atexit.register(_stop_scrapers)
        signal.signal(signal.SIGTERM, _handle_sigterm)
    else:
        logger.info("SCRAPER_ENABLED=0 — background scrapers disabled")


    # STEP 4: serve
    # =============
    debug = os.environ.get("APP_DEBUG", "0") == "1"
    # threading mode without simple-websocket: Socket.IO clients can
    # only long-poll (the mobile socket client disables upgrades for
    # this reason). allow_unsafe_werkzeug lifts Flask-SocketIO's
    # "not designed to run in production" refusal for non-TTY runs;
    # use_debugger / use_reloader are forwarded to app.run through
    # socketio.run's **kwargs (threading branch)
    socketio.run(
        app,
        host=args.host,
        port=args.port,
        debug=debug,
        use_debugger=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )








# Script entry — the Dockerfile CMD lands here
if __name__ == "__main__":
    main()
