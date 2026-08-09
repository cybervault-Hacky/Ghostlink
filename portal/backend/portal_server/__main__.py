"""Run the Developer Portal backend for local development.

    python -m portal_server            # wsgiref dev server on 127.0.0.1:8788

Production uses a real WSGI server (gunicorn / waitress) — see
docs/DEPLOYMENT.md. The dev server proxies /api to the frontend dev server
(or serve the built frontend via the reverse proxy in production).
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from portal_server.app import EmailSender, create_wsgi_app
from portal_server.db import Database


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        if os.environ.get("PORTAL_VERBOSE"):
            super().log_message(_format, *args)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="portal_server", description="GhostLink Developer Portal backend"
    )
    parser.add_argument("--host", default=os.environ.get("PORTAL_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORTAL_PORT", "8788")))
    parser.add_argument("--db", default=os.environ.get("PORTAL_DB", str(Path("portal.db"))))
    parser.add_argument(
        "--email-enabled", action="store_true", default=os.environ.get("EMAIL_PROVIDER") == "dev"
    )
    args = parser.parse_args(argv)

    db = Database(args.db)
    emails = EmailSender(enabled=args.email_enabled)
    app = create_wsgi_app(db, secure_cookies=False, emails=emails)

    server = make_server(
        args.host, args.port, app, server_class=WSGIServer, handler_class=_QuietHandler
    )
    print(f"GhostLink Developer Portal backend on http://{args.host}:{args.port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
