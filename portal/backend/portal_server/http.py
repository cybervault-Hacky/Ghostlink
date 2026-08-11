"""Minimal WSGI request/response helpers (Phase 10B).

A small, dependency-free HTTP layer built on the standard library. It parses
requests, renders JSON responses, sets cookies, and applies security
headers — production can run the same WSGI application under gunicorn or
waitress (see docs/DEPLOYMENT.md).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

from portal_server.security import SECURITY_HEADERS

COOKIE_NAME = "gl_portal_session"


class Request:
    def __init__(self, environ: dict[str, Any]) -> None:
        self.method = environ.get("REQUEST_METHOD", "GET").upper()
        self.path = environ.get("PATH_INFO", "/")
        self.environ = environ
        self._query: dict[str, list[str]] | None = None
        self._cookies: dict[str, str] | None = None
        self._json: Any = None
        self._json_parsed = False

    @property
    def query(self) -> dict[str, list[str]]:
        if self._query is None:
            self._query = parse_qs(self.environ.get("QUERY_STRING", ""))
        return self._query

    def query_param(self, name: str) -> str | None:
        values = self.query.get(name)
        return values[0] if values else None

    @property
    def cookies(self) -> dict[str, str]:
        if self._cookies is None:
            raw = self.environ.get("HTTP_COOKIE", "")
            parsed: dict[str, str] = {}
            for part in raw.split(";"):
                if "=" in part:
                    k, _, v = part.strip().partition("=")
                    parsed[k] = v
            self._cookies = parsed
        return self._cookies

    def header(self, name: str) -> str:
        key = "HTTP_" + name.upper().replace("-", "_")
        value = self.environ.get(key, "")
        return str(value)

    def client_ip(self) -> str:
        # Best-effort client IP for rate limiting; never trusted for auth.
        # ``X-Forwarded-For`` is honoured only when the caller explicitly
        # flagged a trusted reverse proxy (13H); otherwise it is ignored so a
        # client cannot spoof a different IP for rate limiting.
        trusted = self.environ.get("ghostlink.trusted_proxy") in (True, "1", "true", 1)
        forwarded = self.environ.get("HTTP_X_FORWARDED_FOR", "")
        if forwarded and trusted:
            return str(forwarded).split(",")[0].strip()
        return str(self.environ.get("REMOTE_ADDR", "unknown"))

    @property
    def body_json(self) -> Any:
        if not self._json_parsed:
            self._json_parsed = True
            length = int(self.environ.get("CONTENT_LENGTH") or 0)
            if length <= 0:
                self._json = {}
            else:
                body = self.environ["wsgi.input"].read(length)
                try:
                    self._json = json.loads(body.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    self._json = None
            if not isinstance(self._json, dict):
                self._json = None
        return self._json


@dataclass
class Response:
    status: int = 200
    body: Any = None  # dict -> JSON; str -> text; None -> empty
    headers: dict[str, str] = field(default_factory=dict)

    def set_cookie(self, name: str, value: str, *, secure: bool, **attrs: Any) -> None:
        parts = [f"{name}={value}", "Path=/"]
        for key, val in attrs.items():
            label = key.replace("_", "-")
            if val is True:
                parts.append(label)
            elif val not in (False, None):
                parts.append(f"{label}={val}")
        if secure:
            # Only include Secure when true; emitting `Secure=false` is
            # interpreted by clients as a Secure cookie and breaks HTTP dev.
            parts.append("Secure")
        self.headers["Set-Cookie"] = "; ".join(parts)


def json_response(status: int, data: dict[str, Any]) -> Response:
    return Response(status=status, body=data, headers={"Content-Type": "application/json"})


def error_response(
    status: int, code: str, message: str, *, detail: dict[str, Any] | None = None
) -> Response:
    payload: dict[str, Any] = {"error": {"code": code, "message": message}}
    if detail:
        payload["error"]["detail"] = detail
    return json_response(status, payload)


def ok(data: dict[str, Any]) -> Response:
    return json_response(200, data)


def created(data: dict[str, Any]) -> Response:
    return json_response(201, data)


def app_response(resp: Response, start_response: Any) -> list[bytes]:
    """Convert a Response into a WSGI start_response call."""
    if resp.status in (204,):
        body = b""
    elif isinstance(resp.body, dict):
        body = json.dumps(resp.body, separators=(",", ":")).encode("utf-8")
    elif isinstance(resp.body, str):
        body = resp.body.encode("utf-8")
    else:
        body = b""
    headers = [("Content-Type", resp.headers.get("Content-Type", "application/json"))]
    for name, value in resp.headers.items():
        if name.lower() == "content-type":
            continue
        headers.append((name, value))
    for name, value in SECURITY_HEADERS.items():
        headers.append((name, value))
    headers.append(("Content-Length", str(len(body))))
    start_response(f"{resp.status} OK", headers)
    return [body]


__all__ = [
    "COOKIE_NAME",
    "Request",
    "Response",
    "app_response",
    "created",
    "error_response",
    "json_response",
    "ok",
]
