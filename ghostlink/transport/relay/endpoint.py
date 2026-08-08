"""Relay endpoint parsing and normalization."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from ghostlink.exceptions.config import ConfigValidationError


@dataclass(frozen=True, slots=True)
class RelayEndpoint:
    """A normalized ``ws://`` or ``wss://`` relay address."""

    host: str
    port: int
    secure: bool
    resource: str

    @classmethod
    def from_url(cls, url: str) -> RelayEndpoint:
        """Parse and strictly validate a relay URL."""

        cleaned = url.strip()
        if not cleaned:
            raise ConfigValidationError(
                "Relay URL must not be empty.",
                hint="Example: wss://relay.example.org or ws://127.0.0.1:8787",
            )
        parsed = urlparse(cleaned)
        if parsed.scheme not in {"ws", "wss"}:
            raise ConfigValidationError(
                f"Relay URL '{cleaned}' must use the ws:// or wss:// scheme.",
                hint="Example: wss://relay.example.org or ws://127.0.0.1:8787",
            )
        host = parsed.hostname
        if not host:
            raise ConfigValidationError(
                f"Relay URL '{cleaned}' is missing a host.",
                hint="Example: wss://relay.example.org:443",
            )
        secure = parsed.scheme == "wss"
        try:
            port = parsed.port or (443 if secure else 80)
        except ValueError as exc:
            raise ConfigValidationError(
                f"Relay URL '{cleaned}' has an invalid port.",
                hint="Use a port between 1 and 65535.",
            ) from exc
        if not (1 <= port <= 65535):
            raise ConfigValidationError(
                f"Relay URL '{cleaned}' has an out-of-range port {port}.",
                hint="Use a port between 1 and 65535.",
            )
        resource = parsed.path or "/"
        if parsed.query:
            resource = f"{resource}?{parsed.query}"
        return cls(host=host, port=port, secure=secure, resource=resource)

    @property
    def scheme(self) -> str:
        return "wss" if self.secure else "ws"

    @property
    def display(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}{self.resource}"
