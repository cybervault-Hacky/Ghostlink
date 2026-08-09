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
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from portal_server.app import create_wsgi_app
from portal_server.config import ConfigError, load_config
from portal_server.db import Database
from portal_server.emailing import get_email_provider


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
    parser.add_argument("--db", default=None, help="override DATABASE_URL (dev/testing)")
    parser.add_argument(
        "--email-enabled", action="store_true", default=os.environ.get("EMAIL_PROVIDER") == "dev"
    )
    args = parser.parse_args(argv)

    # Load and validate configuration (fails closed in production).
    try:
        config = load_config()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    db_url = args.db or config.db_url
    db = Database(db_url)
    # Build the email provider from configuration (dev adapter or SMTP).
    # Explicit ``--email-enabled`` forces the dev adapter on for local testing.
    from portal_server.emailing import EmailProvider

    emails: EmailProvider
    if args.email_enabled:
        from portal_server.emailing import DevEmailProvider

        emails = DevEmailProvider(enabled=True)
    else:
        emails = get_email_provider(config)
    app = create_wsgi_app(
        db,
        secure_cookies=config.secure_cookies,
        emails=emails,
        config=config,
    )

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
