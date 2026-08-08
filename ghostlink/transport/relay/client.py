"""The GhostLink relay client.

Real networking, no stubs: the client dials a WebSocket transport to a relay
endpoint, performs the HELLO/WELCOME handshake, runs the connection state
machine with automatic reconnection, drives heartbeat + RTT measurement, and
validates every packet in both directions. Sessions, latency, and lifecycle
statistics are exposed for the dashboards.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, ClassVar

from ghostlink.constants.net import PROTOCOL_VERSION
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.invites import (
    InviteAlreadyUsedError,
    InviteError,
    InviteExpiredError,
    InvitePermissionError,
    InviteRevokedError,
    InviteUnknownError,
)
from ghostlink.exceptions.transport import (
    ConnectionTimeoutError,
    HandshakeError,
    PacketValidationError,
    RelayError,
    TransportError,
)
from ghostlink.invites.authority import InviteGrant
from ghostlink.models.room import is_valid_room_id
from ghostlink.transport.connection import (
    ConnectionManager,
    ConnectionState,
    ReconnectPolicy,
    StateChange,
)
from ghostlink.transport.heartbeat import HeartbeatMonitor, HeartbeatStats
from ghostlink.transport.relay.endpoint import RelayEndpoint
from ghostlink.transport.relay.protocol import (
    Packet,
    PacketType,
    attach_packet,
    decode_packet,
    detach_packet,
    disconnect_packet,
    encode_packet,
    forward_packet,
    heartbeat_packet,
    hello_packet,
    invite_create_packet,
    invite_query_packet,
    invite_revoke_packet,
    ping_packet,
    pong_packet,
    redeem_packet,
)
from ghostlink.transport.session import Session
from ghostlink.transport.transport import Transport, TransportStats
from ghostlink.transport.websocket.transport import WebSocketTransport

PING_TIMEOUT_SECONDS = 5.0
PROBE_PING_INTERVAL_SECONDS = 0.5
PROBE_HEARTBEAT_WINDOW_SECONDS = 3.0
MAX_ERROR_PACKETS_KEPT = 20


@dataclass(frozen=True, slots=True)
class RelayInviteStatus:
    """Safe invite metadata answered by the relay authority (v3)."""

    invite_id: str
    state: str
    uses: int
    max_uses: int
    expires_in_seconds: float
    room_id: str | None = None


@dataclass(frozen=True, slots=True)
class RelayClientConfig:
    """Tuning for one relay client, derived from settings or CLI flags."""

    connect_timeout_seconds: float = 5.0
    handshake_timeout_seconds: float = 5.0
    heartbeat_interval_seconds: float = 10.0
    heartbeat_timeout_seconds: float = 5.0
    reconnect_attempts: int = 0
    reconnect_base_delay_seconds: float = 1.0


@dataclass(frozen=True, slots=True)
class RelayProbeReport:
    """The result of a full relay status probe, rendered by dashboards."""

    endpoint: str
    secure: bool
    session_id: str
    server_name: str
    protocol_version: int
    rtt_samples_ms: tuple[float, ...]
    heartbeats_acked: int
    packets_sent: int
    packets_received: int
    bytes_sent: int
    bytes_received: int
    duration_seconds: float
    state_timeline: tuple[tuple[float, str], ...]
    graceful_disconnect: bool


class RelayClient:
    """A live, fully validated relay protocol client."""

    def __init__(
        self,
        endpoint: RelayEndpoint,
        *,
        client_name: str,
        config: RelayClientConfig | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._client_name = client_name
        self._config = config or RelayClientConfig()
        self._logger = get_logger("transport.relay.client")
        self._session: Session | None = None
        self._pending_pings: dict[str, asyncio.Future[float]] = {}
        self._pending_attaches: dict[str, asyncio.Future[Packet]] = {}
        self._pending_invites: dict[str, asyncio.Future[Packet]] = {}
        self._attachments: dict[str, str] = {}  # channel -> role
        self._on_forward: Callable[[str, str], None] | None = None
        self._on_peer: Callable[[str, str], None] | None = None
        self._error_packets: list[Packet] = []
        self._state_changes: list[StateChange] = []

        self._heartbeat = HeartbeatMonitor(
            interval_seconds=self._config.heartbeat_interval_seconds,
            timeout_seconds=self._config.heartbeat_timeout_seconds,
            send_heartbeat=self._send_heartbeat,
            send_ping=self._send_heartbeat_ping,
            on_missed=self._heartbeat_missed,
        )
        self._manager = ConnectionManager(
            transport_factory=self._new_transport,
            on_bytes=self._handle_bytes,
            reconnect_policy=ReconnectPolicy(
                attempts=self._config.reconnect_attempts,
                base_delay_seconds=self._config.reconnect_base_delay_seconds,
            ),
            name="relay",
        )
        self._manager.add_state_listener(self._state_changes.append)

    # ------------------------------------------------------------- properties

    @property
    def state(self) -> ConnectionState:
        return self._manager.state

    @property
    def session(self) -> Session | None:
        return self._session

    @property
    def endpoint(self) -> RelayEndpoint:
        return self._endpoint

    @property
    def manager(self) -> ConnectionManager:
        return self._manager

    @property
    def heartbeat_stats(self) -> HeartbeatStats:
        return self._heartbeat.stats

    @property
    def state_changes(self) -> tuple[StateChange, ...]:
        return tuple(self._state_changes)

    @property
    def error_packets(self) -> tuple[Packet, ...]:
        return tuple(self._error_packets)

    # -------------------------------------------------------------- transport

    async def _new_transport(self) -> Transport:
        """Factory handed to the connection manager for each (re)dial."""

        return WebSocketTransport.dial(
            self._endpoint.host,
            self._endpoint.port,
            resource=self._endpoint.resource,
            secure=self._endpoint.secure,
            timeout_seconds=self._config.connect_timeout_seconds,
        )

    # -------------------------------------------------------------- lifecycle

    async def connect(self) -> Session:
        """Dial, handshake, start heartbeat, and return the live session."""

        await self._manager.open(
            handshake=self._handshake,
            on_connected=self._post_connected,
            timeout_seconds=self._config.connect_timeout_seconds,
        )
        if self._session is None:  # defensive; _handshake always assigns it
            raise HandshakeError("Handshake completed without a session.")
        return self._session

    async def disconnect(self, *, reason: str = "client shutdown") -> None:
        """Politely say goodbye (DISCONNECT), then close the transport."""

        await self._heartbeat.stop()
        for future in self._pending_pings.values():
            future.cancel()
        self._pending_pings.clear()
        for pending in self._pending_invites.values():
            pending.cancel()
        self._pending_invites.clear()
        previous_sid = self._session.session_id if self._session else "none"
        if self._manager.is_usable:
            try:
                await self.send_packet(disconnect_packet(reason))
            except TransportError as exc:
                self._logger.debug("goodbye packet could not be sent: %s", exc)
        await self._manager.close(reason=reason)
        if self._session is not None:
            self._session.close()
        self._logger.info(
            "relay client disconnected — %s (session %s)",
            self._endpoint.display,
            previous_sid,
        )

    async def reconnect(self) -> None:
        """Force one supervised reconnect cycle."""

        await self._manager.reconnect()

    async def aclose(self) -> None:
        """Async context-manager friendly alias for :meth:`disconnect`."""

        await self.disconnect()

    # --------------------------------------------------------------- handshake

    async def _handshake(self, transport: Transport) -> None:
        """HELLO → WELCOME against one freshly-opened transport."""

        await transport.send(encode_packet(hello_packet(self._client_name)))
        raw = await asyncio.wait_for(
            transport.receive(), timeout=self._config.handshake_timeout_seconds
        )
        if raw is None:
            raise HandshakeError(
                "Relay closed the connection during the handshake.",
                hint="The endpoint is reachable but refused the session.",
            )
        try:
            packet = decode_packet(raw, peer=self._endpoint.display)
        except PacketValidationError as exc:
            raise HandshakeError(
                f"Relay answered the handshake with an invalid packet: {exc.message}",
                hint="The endpoint does not speak relay protocol v1.",
            ) from exc
        if packet.type is PacketType.ERROR:
            raise HandshakeError(
                f"Relay rejected the handshake: {packet.payload.get('message', 'unknown')}.",
                hint="Check the client name and protocol compatibility.",
            )
        if packet.type is not PacketType.WELCOME:
            raise HandshakeError(
                f"Expected WELCOME after HELLO, received {packet.type.value}.",
                hint="The endpoint does not follow relay protocol v1.",
            )
        payload = packet.payload
        self._session = Session.create(
            session_id=str(payload["session_id"]),
            metadata={
                "remote": self._endpoint.display,
                "server": str(payload["server"]),
                "protocol": str(self._protocol_version(payload)),
                "client": self._client_name,
            },
        )
        self._logger.info(
            "relay handshake complete — session=%s server=%s",
            self._session.session_id,
            payload["server"],
        )

    @staticmethod
    def _protocol_version(payload: dict[str, Any]) -> int:
        raw = payload.get("protocol", 1)
        return raw if isinstance(raw, int) else 1

    async def _post_connected(self, transport: Transport) -> None:
        """Start heartbeat and re-establish channel attachments on (re)connect."""

        self._heartbeat.start()
        for channel, role in list(self._attachments.items()):
            # New relay session: channel membership must be re-declared.
            self._logger.debug("re-attaching channel %s as %s", channel, role)
            await self._attach_packet(channel, role)

    # ----------------------------------------------------------- rendezvous

    def set_forward_listener(self, listener: Callable[[str, str], None] | None) -> None:
        """Route inbound FORWARD bodies (channel, body) to ``listener``."""

        self._on_forward = listener

    def set_peer_listener(self, listener: Callable[[str, str], None] | None) -> None:
        """Route PEER events (channel, "joined"|"left") to ``listener``."""

        self._on_peer = listener

    @property
    def attachments(self) -> dict[str, str]:
        return dict(self._attachments)

    async def _attach_packet(self, channel: str, role: str) -> None:
        """Send ATTACH and track it locally; used by attach() and recovery."""

        await self.send_packet(attach_packet(channel, role))

    async def attach(self, channel: str, role: str, *, timeout_seconds: float = 10.0) -> int:
        """Attach to a rendezvous channel; returns peers already present."""

        if role not in ("host", "guest"):
            raise RelayError(
                f"Role must be 'host' or 'guest', got {role!r}.",
                hint="Pick the host side when you created the room, guest when joining.",
            )
        if not is_valid_room_id(channel):
            raise RelayError(
                f"'{channel}' is not a valid room identifier.",
                hint="Channels use the gl-room-XXXX-XXXX-XXXX format.",
            )
        if not self._manager.is_usable:
            raise TransportError(
                f"Cannot attach to {channel}: connection is {self._manager.state.value}.",
                hint="Connect to the relay before attaching to a channel.",
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Packet] = loop.create_future()
        self._pending_attaches[channel] = future
        try:
            await self.send_packet(attach_packet(channel, role))
            packet = await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError as exc:
            raise ConnectionTimeoutError(
                f"Relay did not confirm the attach to {channel} in time.",
                hint="The relay may be overloaded; check relay-status and retry.",
            ) from exc
        finally:
            self._pending_attaches.pop(channel, None)
        self._attachments[channel] = role
        peers = packet.payload.get("peers", 0)
        peers_count = peers if isinstance(peers, int) else 0
        self._logger.info("attached to %s as %s (%d peer(s) waiting)", channel, role, peers_count)
        return peers_count

    async def detach(self, channel: str) -> None:
        """Leave a channel politely; local bookkeeping drops immediately."""

        self._attachments.pop(channel, None)
        if self._manager.is_usable:
            try:
                await self.send_packet(detach_packet(channel))
            except TransportError as exc:
                self._logger.debug("detach packet could not be sent: %s", exc)

    async def send_forward(self, channel: str, body: str) -> None:
        """Route one opaque (end-to-end encrypted) body to the channel peer."""

        await self.send_packet(forward_packet(channel, body))

    # ---------------------------------------------------------- invites (v3)

    async def _invite_roundtrip(self, invite_id: str, packet: Packet, timeout: float) -> Packet:
        """Send one invite packet and await its correlated response."""

        if not self._manager.is_usable:
            raise TransportError(
                f"Cannot send {packet.type.value}: connection is {self._manager.state.value}.",
                hint="Connect to the relay before issuing invite operations.",
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[Packet] = loop.create_future()
        self._pending_invites[invite_id] = future
        try:
            await self.send_packet(packet)
            return await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError as exc:
            raise ConnectionTimeoutError(
                f"Relay did not answer the {packet.type.value} request in time.",
                hint="The relay may be overloaded; check relay-status and retry.",
            ) from exc
        finally:
            self._pending_invites.pop(invite_id, None)

    async def create_invite(
        self,
        token: str,
        *,
        room_id: str,
        ttl_seconds: float,
        max_redemptions: int,
        timeout_seconds: float = 10.0,
    ) -> InviteGrant:
        """Register an invite with the relay authority; returns the grant."""

        from ghostlink.invites.tokens import invite_id_for_token, normalize_invite_token

        normalized = normalize_invite_token(token)
        invite_id = invite_id_for_token(normalized)
        packet = await self._invite_roundtrip(
            invite_id,
            invite_create_packet(normalized, room_id, ttl_seconds, max_redemptions),
            timeout_seconds,
        )
        if packet.type is not PacketType.INVITE_GRANTED:
            raise InviteError(
                f"Unexpected relay answer to INVITE_CREATE: {packet.type.value}.",
                hint="The relay may run an older protocol — upgrade it.",
            )
        expires_at = datetime.fromtimestamp(float(packet.payload["expires_at"]), tz=UTC)
        self._logger.info("invite registered with relay — %s", invite_id)
        return InviteGrant(
            invite_id=invite_id,
            expires_at=expires_at,
            max_redemptions=max_redemptions,
        )

    async def query_invite(self, token: str, *, timeout_seconds: float = 10.0) -> RelayInviteStatus:
        """Safe invite metadata from the authority (never the token back)."""

        from ghostlink.invites.tokens import invite_id_for_token, normalize_invite_token

        normalized = normalize_invite_token(token)
        invite_id = invite_id_for_token(normalized)
        packet = await self._invite_roundtrip(
            invite_id, invite_query_packet(normalized), timeout_seconds
        )
        payload = packet.payload
        room = payload.get("room")
        return RelayInviteStatus(
            invite_id=str(payload["invite"]),
            state=str(payload["state"]),
            uses=int(str(payload["uses"])),
            max_uses=int(str(payload["max_uses"])),
            expires_in_seconds=float(payload["expires_in"]),
            room_id=str(room) if isinstance(room, str) else None,
        )

    async def revoke_invite(self, token: str, *, timeout_seconds: float = 10.0) -> str:
        """Revoke one of this session's invites; returns the invite id."""

        from ghostlink.invites.tokens import invite_id_for_token, normalize_invite_token

        normalized = normalize_invite_token(token)
        invite_id = invite_id_for_token(normalized)
        packet = await self._invite_roundtrip(
            invite_id, invite_revoke_packet(normalized), timeout_seconds
        )
        self._logger.info("invite revoked at relay — %s", invite_id)
        return str(packet.payload["invite"])

    async def redeem_invite(
        self, token: str, *, timeout_seconds: float = 10.0
    ) -> tuple[str, datetime]:
        """Consume an invite atomically; returns (room to join, expiry)."""

        from ghostlink.invites.tokens import invite_id_for_token, normalize_invite_token

        normalized = normalize_invite_token(token)
        invite_id = invite_id_for_token(normalized)
        packet = await self._invite_roundtrip(invite_id, redeem_packet(normalized), timeout_seconds)
        room_id = str(packet.payload["room"])
        expires_at = datetime.fromtimestamp(float(packet.payload["expires_at"]), tz=UTC)
        # The relay attached us as guest as part of the REDEEMED exchange.
        self._attachments[room_id] = "guest"
        self._logger.info("invite %s redeemed — joined %s as guest", invite_id, room_id)
        return room_id, expires_at

    # ------------------------------------------------------------------- I/O

    def _handle_bytes(self, data: bytes) -> None:
        """Reader-loop callback: validate and route one inbound packet."""

        try:
            packet = decode_packet(data, peer=self._endpoint.display)
        except PacketValidationError as exc:
            self._logger.info("dropping invalid inbound packet: %s", exc.message)
            return
        if self._session is not None:
            self._session.touch(activity=packet.type.value)
        self._logger.debug("packet received — %s id=%s", packet.type.value, packet.packet_id)

        if packet.type is PacketType.PONG:
            self._resolve_pong(packet)
        elif packet.type is PacketType.PING:
            self._schedule(self._reply_ping(packet))
        elif packet.type is PacketType.HEARTBEAT:
            sequence = int(packet.payload["seq"])
            self._heartbeat.ack_heartbeat(sequence)
        elif packet.type is PacketType.ATTACHED:
            self._resolve_attached(packet)
        elif packet.type is PacketType.FORWARD:
            self._route_forward(packet)
        elif packet.type is PacketType.PEER:
            self._route_peer(packet)
        elif packet.type in (
            PacketType.INVITE_GRANTED,
            PacketType.INVITE_STATE,
            PacketType.REDEEMED,
        ):
            self._resolve_invite(packet)
        elif packet.type is PacketType.ERROR:
            self._error_packets.append(packet)
            del self._error_packets[:-MAX_ERROR_PACKETS_KEPT]
            self._logger.info(
                "relay ERROR packet — %s: %s",
                packet.payload.get("code"),
                packet.payload.get("message"),
            )
            self._reject_pending_attach(packet)
            self._reject_pending_invite(packet)
        elif packet.type is PacketType.DISCONNECT:
            self._logger.info("relay requested disconnect — %s", packet.payload.get("reason", ""))

    def _resolve_attached(self, packet: Packet) -> None:
        channel = str(packet.payload["channel"])
        future = self._pending_attaches.get(channel)
        if future is not None and not future.done():
            future.set_result(packet)
        else:
            self._logger.debug("ATTACHED for %s with no pending attach", channel)

    # ------------------------------------------------------------ invites (v3)

    _INVITE_ERROR_MAP: ClassVar[dict[str, type[InviteError]]] = {
        "invite/expired": InviteExpiredError,
        "invite/already-used": InviteAlreadyUsedError,
        "invite/revoked": InviteRevokedError,
        "invite/unknown": InviteUnknownError,
        "invite/not-creator": InvitePermissionError,
    }

    def _resolve_invite(self, packet: Packet) -> None:
        """Match an INVITE_GRANTED / INVITE_STATE / REDEEMED to its waiter."""

        invite = packet.payload.get("invite")
        if not isinstance(invite, str):
            self._logger.debug("invite response without an invite id — dropped")
            return
        future = self._pending_invites.get(invite)
        if future is None or future.done():
            self._logger.debug("unsolicited %s for %s — dropped", packet.type.value, invite)
            return
        future.set_result(packet)

    def _reject_pending_invite(self, packet: Packet) -> None:
        """Fail a pending invite operation when the relay answers ERROR."""

        invite = packet.payload.get("invite")
        if not isinstance(invite, str):
            return
        future = self._pending_invites.pop(invite, None)
        if future is None or future.done():
            return
        code = str(packet.payload.get("code", "invite/error"))
        message = str(packet.payload.get("message", "Invite operation refused."))
        error_type = self._INVITE_ERROR_MAP.get(code, InviteError)
        hint = "Ask the host for a fresh invite."
        future.set_exception(error_type(message, hint=hint))
        self._logger.info("invite %s refused — %s", invite, code)

    def _reject_pending_attach(self, packet: Packet) -> None:
        """Fail a pending attach when the relay answers with a channel ERROR."""

        channel = packet.payload.get("channel")
        if not isinstance(channel, str):
            return
        future = self._pending_attaches.pop(channel, None)
        if future is not None and not future.done():
            future.set_exception(
                RelayError(
                    f"Relay refused attachment to {channel}: {packet.payload.get('message')}.",
                    hint="Verify the room identifier and that fewer than two peers are attached.",
                )
            )
            self._logger.info("attach to %s refused — %s", channel, packet.payload.get("code"))

    def _route_forward(self, packet: Packet) -> None:
        listener = self._on_forward
        if listener is None:
            self._logger.debug("FORWARD with no listener registered — dropped")
            return
        try:
            listener(str(packet.payload["channel"]), str(packet.payload["body"]))
        except Exception as exc:  # a listener bug must not kill the reader loop
            self._logger.debug("forward listener failed: %s", exc)

    def _route_peer(self, packet: Packet) -> None:
        listener = self._on_peer
        channel = str(packet.payload["channel"])
        event = str(packet.payload["event"])
        self._logger.info("peer %s — channel %s", event, channel)
        if listener is None:
            return
        try:
            listener(channel, event)
        except Exception as exc:  # a listener bug must not kill the reader loop
            self._logger.debug("peer listener failed: %s", exc)

    def _schedule(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        task.add_done_callback(self._observe_background_failure)

    def _observe_background_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            self._logger.debug("background send failed: %s", failure)

    async def _reply_ping(self, packet: Packet) -> None:
        await self.send_packet(
            pong_packet(
                str(packet.payload["nonce"]),
                sent_at=float(packet.payload["sent_at"]),
            )
        )

    def _resolve_pong(self, packet: Packet) -> None:
        nonce = str(packet.payload["nonce"])
        rtt_ms = self._heartbeat.ack_pong(nonce)
        future = self._pending_pings.pop(nonce, None)
        if future is not None and not future.done():
            future.set_result(rtt_ms if rtt_ms is not None else -1.0)

    # ----------------------------------------------------------------- actions

    async def send_packet(self, packet: Packet) -> None:
        """Validate and send one packet over the live connection."""

        if not self._manager.is_usable:
            raise TransportError(
                f"Cannot send {packet.type.value}: connection is {self._manager.state.value}.",
                hint="Connect to the relay before sending packets.",
            )
        await self._manager.send(encode_packet(packet))
        self._logger.debug("packet sent — %s id=%s", packet.type.value, packet.packet_id)

    async def ping(self, *, timeout_seconds: float = PING_TIMEOUT_SECONDS) -> float:
        """Measure one real round trip; returns latency in milliseconds."""

        nonce = secrets.token_hex(8)
        loop = asyncio.get_running_loop()
        future: asyncio.Future[float] = loop.create_future()
        self._pending_pings[nonce] = future
        started = time.perf_counter()
        await self.send_packet(ping_packet(nonce))
        try:
            rtt_ms = await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError as exc:
            self._pending_pings.pop(nonce, None)
            raise RelayError(
                f"Relay did not answer PING within {timeout_seconds:.1f}s.",
                hint="The relay may be overloaded or the connection degraded.",
            ) from exc
        if rtt_ms < 0.0:  # heartbeat monitor did not pair this nonce
            rtt_ms = (time.perf_counter() - started) * 1000.0
        self._logger.debug("manual ping — rtt=%.1fms", rtt_ms)
        return rtt_ms

    async def heartbeat_tick(self) -> None:
        """Send one HEARTBEAT immediately through the monitor (probes/tests)."""

        await self._heartbeat.tick_now()

    # ------------------------------------------------------------ heartbeat glue

    async def _send_heartbeat(self, sequence: int) -> None:
        await self.send_packet(heartbeat_packet(sequence))

    async def _send_heartbeat_ping(self, nonce: str, sent_at: float) -> None:
        await self.send_packet(ping_packet(nonce, sent_at=sent_at))

    def _heartbeat_missed(self) -> None:
        self._logger.info("heartbeat missed limit — closing for supervised recovery")
        self._schedule(self._close_transport_for_recovery())

    async def _close_transport_for_recovery(self) -> None:
        transport = self._manager.transport
        if transport is not None:
            await transport.close()


async def probe_relay(
    url: str,
    *,
    client_name: str,
    config: RelayClientConfig | None = None,
    pings: int = 4,
    ping_interval_seconds: float = PROBE_PING_INTERVAL_SECONDS,
) -> RelayProbeReport:
    """Full relay status probe: connect, handshake, measure, disconnect.

    This drives the identical code path the interactive client uses — nothing
    is simulated. The returned report feeds the relay dashboard.
    """

    if pings < 1:
        raise RelayError(
            "A probe needs at least one ping.",
            hint="Pass --pings with a value between 1 and 20.",
        )
    endpoint = RelayEndpoint.from_url(url)
    timeline: list[tuple[float, str]] = []
    started = time.perf_counter()
    origin = datetime.now(UTC)

    client = RelayClient(endpoint, client_name=client_name, config=config)

    def record(change: StateChange) -> None:
        offset = (change.at - origin).total_seconds()
        timeline.append((max(0.0, offset), change.state.value))

    client.manager.add_state_listener(record)

    session = await client.connect()

    rtt_samples: list[float] = []
    acked_before = client.heartbeat_stats.heartbeats_acked
    try:
        await client.heartbeat_tick()
        for _ in range(pings):
            rtt_samples.append(await client.ping())
            if ping_interval_seconds > 0:
                await asyncio.sleep(ping_interval_seconds)
        # Give the server a brief window to acknowledge the probe heartbeat.
        deadline = time.monotonic() + PROBE_HEARTBEAT_WINDOW_SECONDS
        while (
            client.heartbeat_stats.heartbeats_acked == acked_before and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.02)
        transport_stats = (
            client.manager.transport.stats
            if client.manager.transport is not None
            else TransportStats()
        )
    finally:
        await client.disconnect(reason="probe complete")

    return RelayProbeReport(
        endpoint=endpoint.display,
        secure=endpoint.secure,
        session_id=session.session_id,
        server_name=session.metadata.get("server", "unknown"),
        protocol_version=PROTOCOL_VERSION,
        rtt_samples_ms=tuple(rtt_samples),
        heartbeats_acked=client.heartbeat_stats.heartbeats_acked - acked_before,
        packets_sent=transport_stats.messages_sent,
        packets_received=transport_stats.messages_received,
        bytes_sent=transport_stats.bytes_sent,
        bytes_received=transport_stats.bytes_received,
        duration_seconds=time.perf_counter() - started,
        state_timeline=tuple(timeline),
        graceful_disconnect=client.state is ConnectionState.CLOSED,
    )
