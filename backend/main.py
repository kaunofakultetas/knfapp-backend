#!/usr/bin/env python3
"""knfapp-backend entry point."""

import argparse
import os

from app import create_app


def main():
    parser = argparse.ArgumentParser(description="knfapp-backend")
    parser.add_argument("--http", action="store_true", help="Run HTTP server")
    parser.add_argument("--port", type=int, default=8000, help="Port (default 8000)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Host (default 0.0.0.0)")
    args = parser.parse_args()

    app = create_app()

    if args.http:
        debug = os.environ.get("APP_DEBUG", "0") == "1"
        app.run(host=args.host, port=args.port, debug=debug)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
