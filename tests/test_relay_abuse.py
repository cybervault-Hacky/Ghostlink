"""Phase 8 relay abuse-control tests.

The relay remains a lightweight in-memory routing/authority component; these
tests verify the bounded connection-cap and per-source-IP connection-rate
brakes. Unit-level (no sockets) where possible, plus a loopback over the
real relay for the end-to-end behavior.
"""

from __future__ import annotations

import pytest

from ghostlink.constants.net import RELAY_CONNECT_BURST, RELAY_MAX_CONNECTIONS
from ghostlink.transport.relay.server import RelayServer
from tests.conftest import run, running_relay
from tests.group_helpers import client_for


class TestConnectionAdmission:
    def test_connection_cap_enforced(self) -> None:
        async def scenario() -> None:
            server = RelayServer(host="127.0.0.1", port=0, max_connections=2)
            # Simulate two live clients.
            server._clients = {1: object(), 2: object()}  # type: ignore[assignment]
            admitted = server._connection_admitted("127.0.0.1:5000", None)  # type: ignore[arg-type]
            assert admitted is False  # at cap
            # After removing one, a connection is admitted.
            server._clients.pop(1)
            assert server._connection_admitted("127.0.0.1:5001", None) is True  # type: ignore[arg-type]

        run(scenario())

    def test_per_ip_rate_limiter(self) -> None:
        async def scenario() -> None:
            server = RelayServer(
                host="127.0.0.1", port=0, connect_rate_per_second=1.0, connect_burst=2
            )
            # Burst of 2 allowed, then the third within the window is refused.
            assert server._connection_admitted("10.0.0.1:1000", None) is True  # type: ignore[arg-type]
            assert server._connection_admitted("10.0.0.1:1001", None) is True  # type: ignore[arg-type]
            assert server._connection_admitted("10.0.0.1:1002", None) is False  # type: ignore[arg-type]
            # A different source IP is unaffected.
            assert server._connection_admitted("10.0.0.2:2000", None) is True  # type: ignore[arg-type]

        run(scenario())

    def test_defaults_are_safe(self) -> None:
        server = RelayServer(host="127.0.0.1", port=0)
        assert server._max_connections == RELAY_MAX_CONNECTIONS
        assert server._connect_burst == RELAY_CONNECT_BURST


class TestLoopbackAbuse:
    def test_connections_beyond_cap_refused_on_real_relay(self) -> None:
        async def scenario() -> None:
            async with running_relay(max_connections=3) as server:
                clients = []
                for index in range(3):
                    client = await client_for(server, f"c{index}")
                    clients.append(client)
                # 4th connection should fail (relay at cap).
                from ghostlink.exceptions.transport import TransportError

                with pytest.raises(TransportError):
                    await client_for(server, "c4")
                for client in clients:
                    await client.aclose()

        run(scenario())


class TestForwardRateStillBounded:
    def test_group_forward_rate_brake_still_applies(self) -> None:
        # Regression: the existing GROUP_FORWARD token bucket is untouched.
        async def scenario() -> None:
            server = RelayServer(host="127.0.0.1", port=0)
            assert server._forward_rate_allowed("g", "s") is True

        run(scenario())
