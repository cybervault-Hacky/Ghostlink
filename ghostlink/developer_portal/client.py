"""Developer-portal API client (Phase 12).

A small stdlib-only HTTP client that talks to the portal's
``/api/v1/developer/*`` endpoints. It never logs tokens or credentials,
never puts tokens in URLs, and fails closed on network errors (a network
failure is never treated as successful authentication).
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class PortalClientError(Exception):
    """A portal-client failure. Carries no secret material."""

    def __init__(self, message: str, *, code: str = "error", status: int = 0) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class PortalClient:
    def __init__(self, base_url: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        access_token: str | None = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if access_token:
            req.add_header("Authorization", f"Bearer {access_token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                try:
                    return json.loads(raw) if raw else {}
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    # A 2xx response that is not valid JSON is never treated
                    # as successful authentication (fail closed).
                    raise PortalClientError(
                        "The portal returned an invalid response.",
                        code="invalid_response",
                        status=200,
                    ) from exc
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8") or "{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
            err = payload.get("error", {}) if isinstance(payload, dict) else {}
            raise PortalClientError(
                str(err.get("message", "Portal request failed.")),
                code=str(err.get("code", "error")),
                status=exc.code,
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            # Network failure must never be mistaken for successful auth.
            raise PortalClientError(
                f"Could not reach the portal at {self.base_url}.",
                code="network",
                status=0,
            ) from exc

    # ------------------------------------------------------------ auth

    def pair_begin(self, device_name: str, platform: str, client_version: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/developer/auth/pair-begin",
            body={
                "device_name": device_name,
                "platform": platform,
                "client_version": client_version,
            },
        )

    def pair_complete(
        self,
        pairing_code: str,
        device_name: str = "Termux Android",
        platform: str = "termux",
        client_version: str = "0.17.0",
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/developer/auth/pair-complete",
            body={
                "pairing_code": pairing_code,
                "device_name": device_name,
                "platform": platform,
                "client_version": client_version,
            },
        )

    def token_exchange(self, credential_id: str, credential_secret: str) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/v1/developer/auth/token",
            body={"credential_id": credential_id, "credential_secret": credential_secret},
        )

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        return self._request(
            "POST", "/api/v1/developer/auth/refresh", body={"refresh_token": refresh_token}
        )

    # ------------------------------------------------------------ resources

    def devices_list(self, access_token: str) -> dict[str, Any]:
        return self._request("GET", "/api/v1/developer/devices", access_token=access_token)

    def device_revoke(self, access_token: str, device_id: str) -> dict[str, Any]:
        return self._request(
            "POST", f"/api/v1/developer/devices/{device_id}/revoke", access_token=access_token
        )

    def projects_list(self, access_token: str) -> dict[str, Any]:
        return self._request("GET", "/api/v1/developer/projects", access_token=access_token)

    def credentials_list(self, access_token: str) -> dict[str, Any]:
        return self._request("GET", "/api/v1/developer/credentials", access_token=access_token)

    def credentials_rotate(self, access_token: str, credential_id: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/v1/developer/credentials/{credential_id}/rotate",
            access_token=access_token,
        )

    def activity(self, access_token: str) -> dict[str, Any]:
        return self._request(
            "GET", "/api/v1/developer/security/activity", access_token=access_token
        )

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/v1/developer/health")


__all__ = ["PortalClient", "PortalClientError"]
