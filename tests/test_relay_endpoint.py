"""Relay endpoint parsing and the networking settings schema."""

from __future__ import annotations

from pathlib import Path

import pytest

from ghostlink.config.loader import load_config_file, validate_sections
from ghostlink.exceptions.config import ConfigValidationError
from ghostlink.models.settings import (
    AppSettings,
    InvitesSettings,
    RelaySettings,
    RoomsSettings,
)
from ghostlink.transport.relay.endpoint import RelayEndpoint


class TestRelayEndpoint:
    @pytest.mark.parametrize(
        ("url", "host", "port", "secure", "resource"),
        [
            ("ws://relay.example.org", "relay.example.org", 80, False, "/"),
            ("wss://relay.example.org", "relay.example.org", 443, True, "/"),
            ("ws://127.0.0.1:8787/relay", "127.0.0.1", 8787, False, "/relay"),
            ("wss://10.0.0.9:9000", "10.0.0.9", 9000, True, "/"),
            ("ws://[::1]:8787", "::1", 8787, False, "/"),
            ("ws://host.tld/relay?token=abc", "host.tld", 80, False, "/relay?token=abc"),
        ],
    )
    def test_valid_urls(self, url: str, host: str, port: int, secure: bool, resource: str) -> None:
        endpoint = RelayEndpoint.from_url(url)
        assert endpoint.host == host
        assert endpoint.port == port
        assert endpoint.secure is secure
        assert endpoint.resource == resource

    def test_display_round_trips(self) -> None:
        endpoint = RelayEndpoint.from_url("wss://relay.example.org:8443/socket")
        assert endpoint.display == "wss://relay.example.org:8443/socket"
        assert endpoint.scheme == "wss"

    def test_whitespace_is_trimmed(self) -> None:
        endpoint = RelayEndpoint.from_url("  ws://127.0.0.1:8787/relay  ")
        assert endpoint.host == "127.0.0.1"

    @pytest.mark.parametrize(
        ("url", "reason"),
        [
            ("", "must not be empty"),
            ("   \t  ", "must not be empty"),
            ("http://relay.example.org", "ws:// or wss://"),
            ("relay.example.org:8787", "ws:// or wss://"),
            ("ws://", "missing a host"),
            ("ws://:8787/relay", "missing a host"),
            ("ws://host.tld:99999/x", "invalid port"),
        ],
    )
    def test_invalid_urls_rejected(self, url: str, reason: str) -> None:
        with pytest.raises(ConfigValidationError, match=reason):
            RelayEndpoint.from_url(url)

    def test_errors_carry_hints(self) -> None:
        with pytest.raises(ConfigValidationError) as captured:
            RelayEndpoint.from_url("telnet://x")
        assert "wss://relay.example.org" in (captured.value.hint or "")


class TestRelaySettings:
    def test_defaults_leave_relay_unconfigured(self) -> None:
        relay = RelaySettings()
        assert relay.url == ""
        assert relay.connect_timeout_seconds > 0
        assert relay.reconnect_attempts >= 0

    def test_app_defaults_embed_network_sections(self) -> None:
        settings = AppSettings.defaults()
        assert settings.relay.heartbeat_interval_seconds > 0
        assert settings.rooms.default_lifetime_minutes == 60
        assert settings.invites.default_lifetime_minutes == 15
        assert settings.invites.one_time is True

    @pytest.mark.parametrize("url", ["http://x", "relay.example.org", "tcp://x"])
    def test_rejects_non_websocket_urls(self, url: str) -> None:
        with pytest.raises(ConfigValidationError, match="ws:// or wss://"):
            RelaySettings(url=url)

    def test_accepts_empty_url(self) -> None:
        assert RelaySettings(url="  ").url == "  "  # blank means "unconfigured"

    def test_accepts_ws_and_wss_urls(self) -> None:
        assert RelaySettings(url="ws://127.0.0.1:8787").url.startswith("ws://")
        assert RelaySettings(url="wss://relay.example.org").url.startswith("wss://")

    @pytest.mark.parametrize(
        "field",
        [
            "connect_timeout_seconds",
            "handshake_timeout_seconds",
            "heartbeat_interval_seconds",
            "heartbeat_timeout_seconds",
            "reconnect_base_delay_seconds",
        ],
    )
    def test_non_positive_timeouts_rejected(self, field: str) -> None:
        with pytest.raises(ConfigValidationError, match=field):
            RelaySettings(**{field: 0})

    def test_negative_reconnect_attempts_rejected(self) -> None:
        with pytest.raises(ConfigValidationError, match="reconnect_attempts"):
            RelaySettings(reconnect_attempts=-1)


class TestRoomInviteSettings:
    def test_rooms_rejects_negative_lifetime(self) -> None:
        with pytest.raises(ConfigValidationError, match="default_lifetime_minutes"):
            RoomsSettings(default_lifetime_minutes=-1)

    def test_rooms_accepts_zero_lifetime(self) -> None:
        assert RoomsSettings(default_lifetime_minutes=0).default_lifetime_minutes == 0

    def test_invites_requires_positive_lifetime(self) -> None:
        with pytest.raises(ConfigValidationError, match="default_lifetime_minutes"):
            InvitesSettings(default_lifetime_minutes=0)

    def test_invites_requires_boolean_one_time(self) -> None:
        with pytest.raises(ConfigValidationError, match="one_time"):
            InvitesSettings(one_time="yes")  # type: ignore[arg-type]


class TestConfigSchema:
    def test_relay_section_and_keys_are_allowed(self) -> None:
        data = {
            "relay": {"url": "ws://127.0.0.1:8787", "reconnect_attempts": 2},
            "rooms": {"default_lifetime_minutes": 30},
            "invites": {"default_lifetime_minutes": 5, "one_time": False},
        }
        cleaned = validate_sections(data, source="test")
        assert cleaned["relay"]["reconnect_attempts"] == 2
        assert cleaned["invites"]["one_time"] is False

    def test_unknown_relay_key_rejected_with_guidance(self) -> None:
        with pytest.raises(ConfigValidationError) as captured:
            validate_sections({"relay": {"host": "x"}}, source="test")
        assert "url" in (captured.value.hint or "")

    def test_full_file_loads_through_the_schema(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            '[relay]\nurl = "ws://127.0.0.1:8787"\n[rooms]\ndefault_lifetime_minutes = 45\n',
            encoding="utf-8",
        )
        data = validate_sections(load_config_file(path), source=str(path))
        assert data["relay"]["url"] == "ws://127.0.0.1:8787"
        assert data["rooms"]["default_lifetime_minutes"] == 45
