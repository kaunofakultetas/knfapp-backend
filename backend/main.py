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
#    --http           serve; without it the argparse help is
#                     printed and the process exits 0
#    --host / --port  bind address, default 0.0.0.0:8000.
#                     Nothing passes them: the Dockerfile CMD
#                     is "python main.py --http", and both its
#                     EXPOSE line and the Caddyfile upstream
#                     (knfapp-backend:8000) assume the default
#    APP_DEBUG=1      Flask debug + the Werkzeug reloader
#
#  Used by:
#    - backend/Dockerfile — CMD ["python", "main.py", "--http"]
#    - .github/workflows/ci.yml — compileall only, never run
############################################################


import argparse
import os

from app import create_app, socketio








############################################################
# main
############################################################
#
# Parse the CLI, build the app, serve or print help. Debug
# is gated on APP_DEBUG being exactly "1": the commented-out
# "APP_DEBUG=true" line in docker-compose.yml would NOT turn
# it on, and the APP_DEBUG=0 in .env never reaches the
# container (compose does not forward it).
#
# APP_DEBUG=1 gotcha: socketio.run() defaults use_reloader
# to the debug flag, and the Werkzeug reloader re-executes
# this script in a child process — create_app() then runs
# in BOTH the monitor and the served process, so init_db()
# runs twice and scraper/scheduler.py (no WERKZEUG_RUN_MAIN
# guard) starts its jobs and startup timers twice.
#
# Used by:
#   - the __main__ block below — nothing imports this module
############################################################

def main():
    parser = argparse.ArgumentParser(description="knfapp-backend")
    parser.add_argument("--http", action="store_true", help="Run HTTP server")
    parser.add_argument("--port", type=int, default=8000, help="Port (default 8000)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host (default 0.0.0.0)")
    args = parser.parse_args()

    # Schema, blueprints, socket events and the scraper scheduler all
    # start here — before we know whether --http was even given
    app = create_app()

    if args.http:
        debug = os.environ.get("APP_DEBUG", "0") == "1"
        # threading mode without simple-websocket: Socket.IO clients can
        # only long-poll (the mobile socket client disables upgrades for
        # this reason). allow_unsafe_werkzeug lifts Flask-SocketIO's
        # "not designed to run in production" refusal for non-TTY runs
        socketio.run(app, host=args.host, port=args.port, debug=debug, allow_unsafe_werkzeug=True)
    else:
        parser.print_help()








# Script entry — the Dockerfile CMD lands here
if __name__ == "__main__":
    main()
