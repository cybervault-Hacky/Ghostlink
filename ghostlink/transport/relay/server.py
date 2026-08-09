"""Reference GhostLink relay.

A real, minimal relay used for local development and the test suite. It
accepts WebSocket clients, enforces the HELLO/WELCOME handshake, answers
PING with PONG, acknowledges HEARTBEAT, tracks one session per client with
inactivity expiry, closes politely with DISCONNECT, and hosts the invite
authority (v3) and the group lifecycle authority (v4).

Run it locally:

    python -m ghostlink.transport.relay.server --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import collections
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import cast

from ghostlink.constants.net import (
    CHANNEL_CAPACITY,
    DEFAULT_CRYPTO_SUITE,
    DEFAULT_RELAY_PORT,
    GROUP_EVENT_RATE_OPS,
    GROUP_EVENT_RATE_WINDOW_SECONDS,
    GROUP_FORWARD_RATE_BURST,
    GROUP_FORWARD_RATE_PER_SECOND,
    GROUP_MAX_ACTIVE_INVITES,
    GROUP_PROTOCOL_VERSION,
    HEARTBEAT_INTERVAL_SECONDS,
    INVITE_PROTOCOL_VERSION,
    RELAY_SESSION_TTL_SECONDS,
    SERVER_NAME,
    SESSION_SWEEP_INTERVAL_SECONDS,
)
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.groups import (
    GroupConflictError,
    GroupDissolvedError,
    GroupEpochError,
    GroupError,
    GroupFullError,
    GroupJoinTimeoutError,
    GroupNotMemberError,
    GroupPermissionError,
    GroupUnknownError,
    GroupValidationError,
)
from ghostlink.exceptions.transport import (
    ConnectionTimeoutError,
    HandshakeError,
    PacketValidationError,
    TransportError,
)
from ghostlink.groups.authority import CommittedOp, GroupAuthority
from ghostlink.invites.authority import (
    InviteAuthority,
    RedemptionResult,
    RedemptionVerdict,
)
from ghostlink.invites.tokens import invite_id_for_token, is_valid_invite_token
from ghostlink.transport.relay.protocol import (
    Packet,
    PacketType,
    attached_packet,
    decode_packet,
    disconnect_packet,
    encode_packet,
    error_packet,
    forward_packet,
    group_attested_packet,
    group_event_packet,
    group_granted_packet,
    group_roster_packet,
    group_sign_request_packet,
    heartbeat_packet,
    invite_state_packet,
    peer_packet,
    pong_packet,
    redeemed_packet,
    welcome_packet,
)
from ghostlink.transport.session import Session, SessionRegistry
from ghostlink.transport.websocket.protocol import (
    ConnectionClosed,
    WebSocketConnection,
    perform_server_handshake,
)

SERVER_PROTOCOL_VERSION = 4
HELLO_TIMEOUT_SECONDS = 5.0
HANDSHAKE_TIMEOUT_SECONDS = 5.0
GROUP_SWEEP_INTERVAL_SECONDS = 5.0
GROUP_SIGN_REOFFER_SECONDS = 15.0


@dataclass(slots=True)
class _ClientContext:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    remote: str
    connection: WebSocketConnection | None = None
    session: Session | None = None
    handler_task: asyncio.Task[None] | None = field(default=None, repr=False)
    # channel -> role this connection is attached as (rendezvous routing)
    attachments: dict[str, str] = field(default_factory=dict)
    protocol_version: int = 1
    attest_nonce: str = ""  # per-session group attestation challenge (v4)

    @property
    def conn(self) -> WebSocketConnection:
        if self.connection is None:
            raise TransportError("Client channel used before the upgrade completed.")
        return self.connection


@dataclass(slots=True)
class _Channel:
    """One rendezvous: at most two attached connections (host + guest)."""

    channel_id: str
    members: dict[int, str] = field(default_factory=dict)  # client_id -> role

    def counterparty(self, client_id: int) -> tuple[int, str] | None:
        for other_id, other_role in self.members.items():
            if other_id != client_id:
                return other_id, other_role
        return None


class RelayServer:
    """A real relay endpoint for development and integration tests."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_RELAY_PORT,
        session_ttl_seconds: float = RELAY_SESSION_TTL_SECONDS,
        heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
        server_name: str = SERVER_NAME,
        invite_authority: InviteAuthority | None = None,
        group_authority: GroupAuthority | None = None,
    ) -> None:
        self._host = host
        self._requested_port = port
        self._session_ttl = session_ttl_seconds
        self._heartbeat_interval = heartbeat_interval_seconds
        self._server_name = server_name
        self._logger = get_logger("transport.relay.server")
        self._server: asyncio.AbstractServer | None = None
        self._clients: dict[int, _ClientContext] = {}
        self.sessions = SessionRegistry(
            sweep_interval_seconds=SESSION_SWEEP_INTERVAL_SECONDS,
            logger_name="transport.relay.server.sessions",
        )
        self._next_client_id = 0
        self._channels: dict[str, _Channel] = {}
        self._invites = invite_authority if invite_authority is not None else InviteAuthority()
        self._groups = group_authority if group_authority is not None else GroupAuthority()
        self._contexts_by_session: dict[str, _ClientContext] = {}
        self._last_sign_delivery: dict[str, str] = {}  # group_id -> "op:session"
        self._group_sweeper: asyncio.Task[None] | None = None
        # Phase 6C abuse brakes (§32): token bucket per (group, session) for
        # GROUP_FORWARD, sliding window per session for membership ops.
        self._forward_tokens: dict[tuple[str, str], tuple[float, float]] = {}
        self._event_windows: dict[str, collections.deque[float]] = {}

    # ------------------------------------------------------------- invites

    @property
    def invites(self) -> InviteAuthority:
        """The relay-side invite authority (observability/tests)."""

        return self._invites

    @property
    def groups(self) -> GroupAuthority:
        """The relay-side group authority (observability/tests)."""

        return self._groups

    # ------------------------------------------------------------- properties

    @property
    def port(self) -> int:
        server = self._server
        sockets = None if server is None else cast("asyncio.base_events.Server", server).sockets
        if not sockets:
            return self._requested_port
        return int(sockets[0].getsockname()[1])

    @property
    def host(self) -> str:
        return self._host

    @property
    def url(self) -> str:
        return f"ws://{self._host}:{self.port}/relay"

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def channel_count(self) -> int:
        return len(self._channels)

    def channel_peers(self, channel_id: str) -> tuple[str, ...]:
        """Roles currently attached to ``channel_id`` (observability/tests)."""

        channel = self._channels.get(channel_id)
        if channel is None:
            return ()
        return tuple(sorted(channel.members.values()))

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Bind the listener and start the session sweeper."""

        self._server = await asyncio.start_server(
            self._on_connection, self._host, self._requested_port
        )
        self.sessions.start_sweeper()
        self._group_sweeper = asyncio.create_task(self._sweep_groups())
        self._logger.info("relay listening on %s (ttl=%.0fs)", self.url, self._session_ttl)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def aclose(self) -> None:
        """Stop accepting, say goodbye to clients, and settle all tasks."""

        await self.sessions.stop_sweeper()
        if self._group_sweeper is not None:
            self._group_sweeper.cancel()
            with suppress(asyncio.CancelledError):
                await self._group_sweeper
            self._group_sweeper = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        clients = list(self._clients.items())
        tasks = [context.handler_task for _, context in clients if context.handler_task is not None]
        for _, context in clients:
            await self._disconnect_client(context, "relay shutting down")
        if tasks:
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3.0)
        self._logger.info("relay stopped")

    async def __aenter__(self) -> RelayServer:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -------------------------------------------------------------- accept loop

    def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._next_client_id += 1
        client_id = self._next_client_id
        peer = writer.get_extra_info("peername")
        remote = f"{peer[0]}:{peer[1]}" if peer else "unknown"
        context = _ClientContext(reader=reader, writer=writer, remote=remote)
        self._clients[client_id] = context
        task = asyncio.create_task(
            self._serve_client(client_id, context),
            name=f"ghostlink-relay-client-{client_id}",
        )
        context.handler_task = task

    async def _serve_client(self, client_id: int, context: _ClientContext) -> None:
        try:
            await self._handle_client(client_id, context)
        except ConnectionClosed as exc:
            self._logger.debug("client %s dropped — %s", context.remote, exc)
        except (ConnectionTimeoutError, HandshakeError) as exc:
            self._logger.debug("client %s handshake failed — %s", context.remote, exc)
        except PacketValidationError as exc:
            self._logger.debug("client %s protocol error — %s", context.remote, exc.message)
        except TransportError as exc:
            self._logger.debug("client %s transport error — %s", context.remote, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a single client must never kill the relay
            self._logger.info("client %s failed unexpectedly — %s", context.remote, exc)
        finally:
            await self._detach_client(client_id, context)

    async def _handle_client(self, client_id: int, context: _ClientContext) -> None:
        await perform_server_handshake(
            context.reader, context.writer, timeout_seconds=HANDSHAKE_TIMEOUT_SECONDS
        )
        context.connection = WebSocketConnection(
            context.reader, context.writer, mask_outgoing=False, expect_masked=True
        )
        self._logger.debug("websocket upgraded — %s", context.remote)

        session = await self._expect_hello(client_id, context)
        context.session = session

        while True:
            raw = await context.conn.read_message()
            if raw is None:
                self._logger.debug("websocket closed — %s", context.remote)
                return
            data = raw.data.encode("utf-8") if isinstance(raw.data, str) else raw.data
            await self._dispatch(client_id, context, data)

    async def _expect_hello(self, client_id: int, context: _ClientContext) -> Session:
        try:
            raw = await asyncio.wait_for(context.conn.read_message(), timeout=HELLO_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise ConnectionTimeoutError(
                "Client never sent HELLO after upgrading.",
                hint="Clients must open the session with a HELLO packet.",
            ) from exc
        if raw is None:
            raise HandshakeError("Client closed before completing HELLO.")
        data = raw.data.encode("utf-8") if isinstance(raw.data, str) else raw.data
        packet = decode_packet(data, peer=context.remote)
        if packet.type is not PacketType.HELLO:
            await self._send(
                context,
                error_packet("protocol/expected-hello", "First packet must be HELLO."),
            )
            raise HandshakeError(
                f"Client {context.remote} opened with {packet.type.value}, expected HELLO."
            )
        session = self.sessions.register(
            Session.create(
                metadata={
                    "remote": context.remote,
                    "client": str(packet.payload["client"]),
                    "server": self._server_name,
                    "protocol": str(SERVER_PROTOCOL_VERSION),
                },
                ttl_seconds=self._session_ttl,
            )
        )
        protocol_raw = packet.payload.get("protocol", 1)
        context.protocol_version = protocol_raw if isinstance(protocol_raw, int) else 1
        import secrets as _secrets

        context.attest_nonce = _secrets.token_hex(16)
        self._contexts_by_session[session.session_id] = context
        await self._send(
            context,
            welcome_packet(
                session.session_id,
                server_name=self._server_name,
                heartbeat_interval_seconds=self._heartbeat_interval,
                attest_nonce=context.attest_nonce,
            ),
        )
        self._logger.info("client attached — %s as %s", context.remote, session.session_id)
        return session

    async def _dispatch(self, client_id: int, context: _ClientContext, data: bytes) -> None:
        try:
            packet = decode_packet(data, peer=context.remote)
        except PacketValidationError as exc:
            self._logger.debug("invalid packet from %s — %s", context.remote, exc.message)
            await self._send(
                context,
                error_packet("protocol/invalid-packet", f"Invalid packet: {exc.message}"),
            )
            return

        if context.session is not None:
            context.session.touch(activity=packet.type.value)
        self._logger.debug(
            "packet from %s — %s id=%s", context.remote, packet.type.value, packet.packet_id
        )

        if packet.type is PacketType.PING:
            await self._send(
                context,
                pong_packet(
                    str(packet.payload["nonce"]),
                    sent_at=float(packet.payload["sent_at"]),
                ),
            )
        elif packet.type is PacketType.HEARTBEAT:
            await self._send(context, heartbeat_packet(int(packet.payload["seq"])))
        elif packet.type is PacketType.DISCONNECT:
            self._logger.info(
                "client %s says goodbye — %s",
                context.remote,
                packet.payload.get("reason", ""),
            )
            await self._disconnect_client(context, "client requested disconnect")
            raise ConnectionClosed("Client requested disconnect.")
        elif packet.type is PacketType.ATTACH:
            await self._handle_attach(client_id, context, packet)
        elif packet.type is PacketType.DETACH:
            await self._handle_detach(client_id, context, str(packet.payload["channel"]))
        elif packet.type is PacketType.FORWARD:
            await self._handle_forward(client_id, context, packet)
        elif packet.type is PacketType.INVITE_CREATE:
            await self._handle_invite_create(client_id, context, packet)
        elif packet.type is PacketType.INVITE_QUERY:
            await self._handle_invite_query(client_id, context, packet)
        elif packet.type is PacketType.INVITE_REVOKE:
            await self._handle_invite_revoke(client_id, context, packet)
        elif packet.type is PacketType.REDEEM:
            await self._handle_redeem(client_id, context, packet)
        elif packet.type is PacketType.GROUP_CREATE:
            await self._handle_group_create(client_id, context, packet)
        elif packet.type is PacketType.GROUP_ATTEST:
            await self._handle_group_attest(client_id, context, packet)
        elif packet.type is PacketType.GROUP_STATE:
            await self._handle_group_state(client_id, context, packet)
        elif packet.type is PacketType.GROUP_LEAVE:
            await self._handle_group_leave(client_id, context, packet)
        elif packet.type is PacketType.GROUP_REMOVE:
            await self._handle_group_remove(client_id, context, packet)
        elif packet.type is PacketType.GROUP_DISSOLVE:
            await self._handle_group_dissolve(client_id, context, packet)
        elif packet.type is PacketType.GROUP_SIGN:
            await self._handle_group_sign(client_id, context, packet)
        elif packet.type is PacketType.GROUP_FORWARD:
            await self._handle_group_forward(client_id, context, packet)
        elif packet.type in (PacketType.HELLO, PacketType.WELCOME):
            await self._send(
                context,
                error_packet(
                    "protocol/unexpected-type",
                    f"{packet.type.value} is not valid mid-session.",
                ),
            )
        else:
            await self._send(
                context,
                error_packet(
                    "protocol/unsupported",
                    f"Packet type {packet.type.value} is not handled by this relay.",
                ),
            )

    # --------------------------------------------------------- invites (v3)

    def _invite_session_id(self, context: _ClientContext) -> str:
        return context.session.session_id if context.session is not None else "none"

    @staticmethod
    def _public_invite_id(token: str) -> str:
        """Public id for correlation — derivable from any token-shaped string."""

        import hashlib

        normalized = token.strip().upper()
        if is_valid_invite_token(normalized):
            return invite_id_for_token(normalized)
        digest = hashlib.sha256(normalized.encode("utf-8", "ignore")).hexdigest()
        return f"gi_{digest[:10]}"

    async def _invite_error(
        self, context: _ClientContext, code: str, message: str, invite_id: str = ""
    ) -> None:
        """ERROR packet scoped to an invite — carries the public id only."""

        payload: dict[str, object] = {"code": code, "message": message}
        if invite_id:
            payload["invite"] = invite_id
        await self._send(context, Packet(PacketType.ERROR, payload))
        self._logger.info("invite refused — %s (%s)", code, invite_id or "unidentified")

    @staticmethod
    def invite_correlation(packet: Packet) -> str | None:
        """The public invite id carried by an invite-scoped packet, if any."""

        invite = packet.payload.get("invite")
        return invite if isinstance(invite, str) else None

    async def _handle_invite_create(
        self, client_id: int, context: _ClientContext, packet: Packet
    ) -> None:
        if context.protocol_version < INVITE_PROTOCOL_VERSION:
            token_hint = ""
            if "token" in packet.payload:
                token_hint = self._public_invite_id(str(packet.payload["token"]))
            await self._invite_error(
                context,
                "protocol/unsupported",
                "Invite operations require relay protocol v3.",
                token_hint,
            )
            return
        kind = str(packet.payload.get("kind", "chat"))
        group_id = str(packet.payload.get("group", ""))
        if kind == "group":
            # Group invites: owner-only, capacity headroom, bounded invite
            # fan-out — all enforced before the invite is even registered.
            if context.protocol_version < GROUP_PROTOCOL_VERSION:
                await self._invite_error(
                    context,
                    "protocol/unsupported",
                    "Group invites require relay protocol v4.",
                    self._public_invite_id(str(packet.payload["token"])),
                )
                return
            try:
                self._groups.allow_invite_mint(
                    group_id, self._invite_session_id(context), int(packet.payload["uses"])
                )
                if self._invites.count_active_group_invites(group_id) >= GROUP_MAX_ACTIVE_INVITES:
                    raise GroupConflictError(
                        f"This group already has {GROUP_MAX_ACTIVE_INVITES} active invites.",
                        hint="Revoke or let expire an old invite before minting another.",
                    )
            except GroupError as exc:
                await self._invite_error(
                    context,
                    self._group_error_code(exc),
                    exc.message,
                    self._public_invite_id(str(packet.payload["token"])),
                )
                return
        try:
            grant = self._invites.register(
                str(packet.payload["token"]),
                room_id=str(packet.payload.get("room", "")),
                ttl_seconds=float(packet.payload["ttl"]),
                max_redemptions=int(packet.payload["uses"]),
                creator_session=self._invite_session_id(context),
                kind=kind,
                group_id=group_id,
            )
        except ValueError as exc:
            await self._invite_error(
                context,
                "invite/invalid",
                f"Invite refused: {exc}.",
                self._public_invite_id(str(packet.payload["token"])),
            )
            return
        except GroupError as exc:
            await self._invite_error(
                context,
                self._group_error_code(exc),
                exc.message,
                self._public_invite_id(str(packet.payload["token"])),
            )
            return
        await self._send(
            context,
            Packet(
                PacketType.INVITE_GRANTED,
                {
                    "invite": grant.invite_id,
                    "expires_at": grant.expires_at.timestamp(),
                    "uses": grant.max_redemptions,
                },
            ),
        )
        self._logger.info(
            "invite registered — %s on %s (creator %s)",
            grant.invite_id,
            str(packet.payload["room"]),
            self._invite_session_id(context),
        )

    async def _handle_invite_query(
        self, client_id: int, context: _ClientContext, packet: Packet
    ) -> None:
        public_id = self._public_invite_id(str(packet.payload["token"]))
        if context.protocol_version < INVITE_PROTOCOL_VERSION:
            await self._invite_error(
                context,
                "protocol/unsupported",
                "Invite operations require relay protocol v3.",
                public_id,
            )
            return
        status = self._invites.status(
            str(packet.payload["token"]),
            requester_session=self._invite_session_id(context),
        )
        if status is None:
            await self._invite_error(
                context,
                "invite/unknown",
                "The relay has no record of that invite.",
                public_id,
            )
            return
        await self._send(
            context,
            invite_state_packet(
                status.invite_id,
                status.state,
                uses=status.redemptions,
                max_uses=status.max_redemptions,
                expires_in=status.remaining_seconds,
                room_id=status.room_id,
            ),
        )

    async def _handle_invite_revoke(
        self, client_id: int, context: _ClientContext, packet: Packet
    ) -> None:
        public_id = self._public_invite_id(str(packet.payload["token"]))
        if context.protocol_version < INVITE_PROTOCOL_VERSION:
            await self._invite_error(
                context,
                "protocol/unsupported",
                "Invite operations require relay protocol v3.",
                public_id,
            )
            return
        try:
            status = self._invites.revoke(
                str(packet.payload["token"]),
                requester_session=self._invite_session_id(context),
            )
        except PermissionError:
            await self._invite_error(
                context,
                "invite/not-creator",
                "Only the inviting session may revoke this invite.",
                public_id,
            )
            return
        if status is None:
            await self._invite_error(
                context, "invite/unknown", "The relay has no record of that invite.", public_id
            )
            return
        await self._send(
            context,
            invite_state_packet(
                status.invite_id,
                status.state,
                uses=status.redemptions,
                max_uses=status.max_redemptions,
                expires_in=status.remaining_seconds,
                room_id=status.room_id,
            ),
        )
        self._logger.info("invite revoked — %s", status.invite_id)

    async def _handle_redeem(self, client_id: int, context: _ClientContext, packet: Packet) -> None:
        if context.protocol_version < INVITE_PROTOCOL_VERSION:
            await self._invite_error(
                context,
                "protocol/unsupported",
                "Invite operations require relay protocol v3.",
                self._public_invite_id(str(packet.payload["token"])),
            )
            return
        token = str(packet.payload["token"])
        # Group-kind branch: the capacity check and the invite consumption
        # must settle in ONE await-free section. A lost capacity race rolls
        # the pending-join slot back so the invite is never burned.
        peek = self._invites.lookup(token)
        if peek is not None and peek.kind == "group":
            await self._handle_group_redeem(client_id, context, packet, peek.group_id)
            return
        # Atomic consumption: check-and-consume happen with no awaits in
        # between, so two simultaneous REDEEM packets can never both win.
        result = self._invites.redeem(token, redeem_session=self._invite_session_id(context))
        if result.verdict is not RedemptionVerdict.REDEEMED or result.room_id is None:
            code = {
                RedemptionVerdict.UNKNOWN: "invite/unknown",
                RedemptionVerdict.EXPIRED: "invite/expired",
                RedemptionVerdict.REVOKED: "invite/revoked",
                RedemptionVerdict.ALREADY_USED: "invite/already-used",
            }[result.verdict]
            await self._invite_error(context, code, self._verdict_message(result), result.invite_id)
            return
        room_id = result.room_id
        # Phase 2 — attach synchronously so concurrent redeems on sibling
        # invites to the same room race-safe against the capacity check.
        membership_error = self._attach_membership(client_id, context, room_id, "guest")
        if membership_error is not None:
            await self._invite_error(
                context, "invite/room-full", membership_error, result.invite_id
            )
            return
        await self._send(
            context,
            redeemed_packet(
                result.invite_id,
                room_id,
                result.expires_at.timestamp() if result.expires_at else 0.0,
            ),
        )
        channel = self._channels.get(room_id)
        other_count = len(channel.members) - 1 if channel is not None else 0
        await self._send(context, attached_packet(room_id, "guest", other_count))
        await self._notify_peer_joined(client_id, room_id)
        self._logger.info(
            "invite redeemed — %s → %s (session %s)",
            result.invite_id,
            room_id,
            self._invite_session_id(context),
        )

    async def _handle_group_redeem(
        self, client_id: int, context: _ClientContext, packet: Packet, group_id: str
    ) -> None:
        """Group-kind redemption: capacity reservation + atomic invite consume.

        No room channel is attached — group membership is established by the
        subsequent GROUP_ATTEST + owner countersign (docs/GROUPS.md §13).
        """

        token = str(packet.payload["token"])
        session = self._invite_session_id(context)
        invite_id = self._public_invite_id(token)
        if context.protocol_version < GROUP_PROTOCOL_VERSION:
            await self._invite_error(
                context,
                "protocol/unsupported",
                "Group invites require relay protocol v4.",
                invite_id,
            )
            return
        # 1) Group-side admission gate (no invite consumption yet).
        try:
            snapshot = self._groups.begin_join_redemption(
                group_id, redeem_session=session, invite_id=invite_id
            )
        except GroupError as exc:
            await self._invite_error(context, self._group_error_code(exc), exc.message, invite_id)
            return
        # 2) Atomic invite consumption in the same await-free stretch.
        result = self._invites.redeem(token, redeem_session=session)
        if result.verdict is not RedemptionVerdict.REDEEMED:
            self._groups.cancel_pending_join(group_id, session)
            code = {
                RedemptionVerdict.UNKNOWN: "invite/unknown",
                RedemptionVerdict.EXPIRED: "invite/expired",
                RedemptionVerdict.REVOKED: "invite/revoked",
                RedemptionVerdict.ALREADY_USED: "invite/already-used",
            }[result.verdict]
            await self._invite_error(context, code, self._verdict_message(result), result.invite_id)
            return
        # 3) Redeemed: the joiner pins this roster snapshot (docs/GROUPS.md §12.2).
        await self._send(
            context,
            redeemed_packet(
                result.invite_id,
                "",
                result.expires_at.timestamp() if result.expires_at else 0.0,
                kind="group",
                group_id=snapshot.group_id,
                group_name=snapshot.name,
                owner_fingerprint=snapshot.owner_fingerprint,
                owner_public_key_hex=snapshot.owner_public_key_hex,
                members=snapshot.members_payload(),
                epoch=snapshot.epoch,
                crypto_suite=snapshot.crypto_suite,
            ),
        )
        self._logger.info(
            "group invite redeemed — %s → %s (session %s)", result.invite_id, group_id, session
        )

    @staticmethod
    def _verdict_message(result: RedemptionResult) -> str:
        return {
            RedemptionVerdict.UNKNOWN: "The relay has no record of that invite.",
            RedemptionVerdict.EXPIRED: "This invite has expired.",
            RedemptionVerdict.REVOKED: "This invite was revoked by its creator.",
            RedemptionVerdict.ALREADY_USED: "This invite has already been used.",
            RedemptionVerdict.REDEEMED: "Redeemed.",
        }[result.verdict]

    # ---------------------------------------------------------- groups (v4)

    _GROUP_ERROR_CODES: tuple[tuple[type[GroupError], str], ...] = (
        (GroupJoinTimeoutError, "group/join-timeout"),
        (GroupEpochError, "group/state"),
        (GroupConflictError, "group/conflict"),
        (GroupDissolvedError, "group/dissolved"),
        (GroupFullError, "group/full"),
        (GroupPermissionError, "group/not-owner"),
        (GroupNotMemberError, "group/not-member"),
        (GroupUnknownError, "group/unknown"),
        (GroupValidationError, "group/invalid"),
    )

    def _group_error_code(self, exc: GroupError) -> str:
        for error_type, code in self._GROUP_ERROR_CODES:
            if isinstance(exc, error_type):
                return code
        return "group/invalid"

    async def _group_error(
        self, context: _ClientContext, code: str, message: str, group_id: str = ""
    ) -> None:
        """ERROR packet scoped to a group — carries the group id only."""

        payload: dict[str, object] = {"code": code, "message": message}
        if group_id:
            payload["group"] = group_id
        await self._send(context, Packet(PacketType.ERROR, payload))
        self._logger.info("group op refused — %s (%s)", code, group_id or "unidentified")

    async def _group_protocol_gate(self, context: _ClientContext, group_id: str) -> bool:
        if context.protocol_version >= GROUP_PROTOCOL_VERSION:
            return True
        await self._group_error(
            context,
            "protocol/unsupported",
            "Group operations require relay protocol v4.",
            group_id,
        )
        return False

    def _session_id(self, context: _ClientContext) -> str:
        return context.session.session_id if context.session is not None else "none"

    async def _send_to_session(self, session_id: str, packet: Packet) -> bool:
        """Push one packet to a session-bound client (best effort)."""

        context = self._contexts_by_session.get(session_id)
        if context is None or context.connection is None or context.connection.closed:
            return False
        try:
            await self._send(context, packet)
        except (TransportError, ConnectionError, OSError) as exc:
            self._logger.debug("push to session %s failed: %s", session_id, exc)
            return False
        return True

    async def _handle_group_create(
        self, client_id: int, context: _ClientContext, packet: Packet
    ) -> None:
        if context.protocol_version < GROUP_PROTOCOL_VERSION:
            await self._group_error(
                context, "protocol/unsupported", "Group operations require relay protocol v4."
            )
            return
        try:
            snapshot = self._groups.create_group(
                session_id=self._session_id(context),
                name=str(packet.payload["name"]),
                public_key_hex=str(packet.payload["pubkey"]),
                pop_signature_b64=str(packet.payload["pop"]),
                attest_nonce=context.attest_nonce,
                handle=str(packet.payload["handle"]),
                display_name=str(packet.payload["display"]),
                crypto_suite=str(packet.payload.get("suite", DEFAULT_CRYPTO_SUITE)),
            )
        except GroupError as exc:
            await self._group_error(context, self._group_error_code(exc), exc.message)
            return
        await self._send(
            context,
            group_granted_packet(snapshot.group_id, snapshot.epoch, snapshot.crypto_suite),
        )

    async def _handle_group_attest(
        self, client_id: int, context: _ClientContext, packet: Packet
    ) -> None:
        group_id = str(packet.payload["group"])
        if not await self._group_protocol_gate(context, group_id):
            return
        try:
            outcome = self._groups.attest(
                session_id=self._session_id(context),
                group_id=group_id,
                public_key_hex=str(packet.payload["pubkey"]),
                signature_b64=str(packet.payload["sig"]),
                attest_nonce=context.attest_nonce,
                handle=str(packet.payload["handle"]),
                display_name=str(packet.payload["display"]),
            )
        except GroupError as exc:
            await self._group_error(context, self._group_error_code(exc), exc.message, group_id)
            return
        members = outcome.snapshot.members_payload() if outcome.snapshot is not None else None
        events = (
            [dict(event) for event in outcome.snapshot.events]
            if outcome.snapshot is not None
            else None
        )
        suite = (
            outcome.snapshot.crypto_suite if outcome.snapshot is not None else DEFAULT_CRYPTO_SUITE
        )
        await self._send(
            context,
            group_attested_packet(
                outcome.group_id,
                outcome.role,
                outcome.epoch,
                members=members,
                events=events,
                crypto_suite=suite,
            ),
        )
        # A signer (owner/candidate) may now be reachable for pending ops.
        await self._deliver_sign_task(group_id)

    async def _handle_group_state(
        self, client_id: int, context: _ClientContext, packet: Packet
    ) -> None:
        group_id = str(packet.payload["group"])
        if not await self._group_protocol_gate(context, group_id):
            return
        try:
            snapshot = self._groups.roster_view(group_id, self._session_id(context))
        except GroupError as exc:
            await self._group_error(context, self._group_error_code(exc), exc.message, group_id)
            return
        await self._send(
            context,
            group_roster_packet(
                snapshot.group_id,
                snapshot.epoch,
                snapshot.state,
                snapshot.members_payload(),
                [dict(event) for event in snapshot.events],
                snapshot.crypto_suite,
            ),
        )

    async def _group_event_rate_gate(self, context: _ClientContext, group_id: str) -> bool:
        """§32 GROUP_EVENT_RATE brake on leave/remove/dissolve bursts."""

        if self._event_rate_allowed(self._session_id(context)):
            return True
        await self._group_error(
            context,
            "group/rate",
            "Too many membership operations — slow down (rate limit per §32).",
            group_id,
        )
        return False

    async def _handle_group_leave(
        self, client_id: int, context: _ClientContext, packet: Packet
    ) -> None:
        group_id = str(packet.payload["group"])
        if not await self._group_protocol_gate(context, group_id):
            return
        if not await self._group_event_rate_gate(context, group_id):
            return
        try:
            self._groups.request_leave(group_id, self._session_id(context))
        except GroupError as exc:
            await self._group_error(context, self._group_error_code(exc), exc.message, group_id)
            return
        await self._deliver_sign_task(group_id)

    async def _handle_group_remove(
        self, client_id: int, context: _ClientContext, packet: Packet
    ) -> None:
        group_id = str(packet.payload["group"])
        if not await self._group_protocol_gate(context, group_id):
            return
        if not await self._group_event_rate_gate(context, group_id):
            return
        try:
            self._groups.request_remove(
                group_id, self._session_id(context), str(packet.payload["subject"])
            )
        except GroupError as exc:
            await self._group_error(context, self._group_error_code(exc), exc.message, group_id)
            return
        await self._deliver_sign_task(group_id)

    async def _handle_group_dissolve(
        self, client_id: int, context: _ClientContext, packet: Packet
    ) -> None:
        group_id = str(packet.payload["group"])
        if not await self._group_protocol_gate(context, group_id):
            return
        if not await self._group_event_rate_gate(context, group_id):
            return
        try:
            self._groups.request_dissolve(group_id, self._session_id(context))
        except GroupError as exc:
            await self._group_error(context, self._group_error_code(exc), exc.message, group_id)
            return
        await self._deliver_sign_task(group_id)

    async def _handle_group_sign(
        self, client_id: int, context: _ClientContext, packet: Packet
    ) -> None:
        group_id = str(packet.payload["group"])
        if not await self._group_protocol_gate(context, group_id):
            return
        try:
            committed = self._groups.submit_signature(
                group_id,
                self._session_id(context),
                str(packet.payload["op"]),
                str(packet.payload["sig"]),
            )
        except GroupError as exc:
            await self._group_error(context, self._group_error_code(exc), exc.message, group_id)
            return
        await self._broadcast_group_event(committed)
        self._last_sign_delivery.pop(group_id, None)
        await self._deliver_sign_task(group_id)

    async def _deliver_sign_task(self, group_id: str) -> None:
        """Offer the queue-head signature request to its online signer."""

        task = self._groups.next_sign_task(group_id)
        if task is None or task.signer_session is None:
            return
        delivery_key = f"{task.op_id}:{task.signer_session}"
        if self._last_sign_delivery.get(group_id) == delivery_key:
            return  # already offered; the response (or the sweeper) settles it
        delivered = await self._send_to_session(
            task.signer_session,
            group_sign_request_packet(
                task.group_id,
                task.op_id,
                task.kind,
                task.subject_fingerprint,
                task.epoch,
                task.message_b64,
            ),
        )
        if delivered:
            self._last_sign_delivery[group_id] = delivery_key

    async def _broadcast_group_event(self, committed: CommittedOp) -> None:
        event = committed.event
        member_payload = committed.join_member.to_payload() if committed.join_member else None
        packet = group_event_packet(
            event.group_id,
            event.epoch,
            event.kind.value,
            event.subject_fingerprint,
            event.wall_ts.isoformat(),
            event.signer_fingerprint,
            base64.b64encode(event.signature).decode("ascii"),
            member=member_payload,
        )
        for session_id in committed.notify_sessions:
            await self._send_to_session(session_id, packet)

    # ------------------------------------------------- group messaging (6C)

    async def _group_forward_error(
        self,
        context: _ClientContext,
        code: str,
        message: str,
        group_id: str,
        member: str,
    ) -> None:
        """GROUP_FORWARD-scoped refusal; correlates via group + member.

        Forward errors always name the addressed member so the sender's
        per-recipient ledger can mark exactly that recipient — and so the
        client never confuses them with lifecycle round-trip failures.
        """

        await self._send(
            context,
            Packet(
                PacketType.ERROR,
                {"code": code, "message": message, "group": group_id, "member": member},
            ),
        )
        self._logger.info(
            "group forward refused — %s (%s → %s)", code, group_id or "unidentified", member
        )

    def _forward_rate_allowed(self, group_id: str, session_id: str) -> bool:
        """Token bucket: 20 envelopes/s (+burst) per (group, sender) — §32."""

        now = asyncio.get_running_loop().time()
        key = (group_id, session_id)
        tokens, stamped = self._forward_tokens.get(key, (float(GROUP_FORWARD_RATE_BURST), now))
        tokens = min(
            float(GROUP_FORWARD_RATE_BURST),
            tokens + (now - stamped) * GROUP_FORWARD_RATE_PER_SECOND,
        )
        if tokens < 1.0:
            self._forward_tokens[key] = (tokens, now)
            return False
        self._forward_tokens[key] = (tokens - 1.0, now)
        return True

    def _event_rate_allowed(self, session_id: str) -> bool:
        """Sliding window: 4 membership ops per 10 s per session — §32."""

        now = asyncio.get_running_loop().time()
        window = self._event_windows.setdefault(session_id, collections.deque())
        while window and now - window[0] > GROUP_EVENT_RATE_WINDOW_SECONDS:
            window.popleft()
        if len(window) >= GROUP_EVENT_RATE_OPS:
            return False
        window.append(now)
        return True

    async def _handle_group_forward(
        self, client_id: int, context: _ClientContext, packet: Packet
    ) -> None:
        """Route one opaque group envelope (§28): ACL + rate gates only.

        The relay is never a group-message processor: it checks that both
        endpoints are attested ACTIVE members (from == the sending
        session's attested fingerprint), applies the §32 flood brake, and
        forwards the untouched envelope. Offline recipients are NOT
        queued relay-side (§29) — the sender learns `group/offline`.
        """

        group_id = str(packet.payload["group"])
        to_fingerprint = str(packet.payload["to"])
        from_fingerprint = str(packet.payload["from"])
        if not await self._group_protocol_gate(context, group_id):
            return
        session_id = self._session_id(context)
        try:
            attested = self._groups.session_fingerprint(group_id, session_id)
            if attested is None or attested != from_fingerprint:
                raise GroupNotMemberError(
                    "The sending session does not match the envelope sender.",
                    hint="Attest first; the 'from' label is bound at routing time.",
                )
            target_session = self._groups.authorize_forward(
                group_id, session_id, to_fingerprint, int(str(packet.payload["epoch"]))
            )
        except GroupError as exc:
            await self._group_forward_error(
                context, self._group_error_code(exc), exc.message, group_id, to_fingerprint
            )
            return
        if not self._forward_rate_allowed(group_id, session_id):
            await self._group_forward_error(
                context,
                "group/rate",
                "Too many group envelopes — slow down (rate limit per §32).",
                group_id,
                to_fingerprint,
            )
            return
        if target_session is None:
            # No relay-side queue (§29): the sender retries within its
            # bounded offline queue; nothing is stored here.
            await self._group_forward_error(
                context,
                "group/offline",
                "The addressed member is not currently connected.",
                group_id,
                to_fingerprint,
            )
            return
        delivered = await self._send_to_session(target_session, packet)
        if not delivered:
            await self._group_forward_error(
                context,
                "group/offline",
                "The addressed member could not be reached.",
                group_id,
                to_fingerprint,
            )

    async def _sweep_groups(self) -> None:
        """Periodic group-authority maintenance (timeouts, tombstone purges)."""

        try:
            while True:
                await asyncio.sleep(GROUP_SWEEP_INTERVAL_SECONDS)
                for notice in self._groups.sweep():
                    if notice.session_id is not None:
                        await self._send_to_session(
                            notice.session_id,
                            Packet(
                                PacketType.ERROR,
                                {
                                    "code": notice.code,
                                    "message": notice.message,
                                    "group": notice.group_id,
                                },
                            ),
                        )
                    self._last_sign_delivery.pop(notice.group_id, None)
                    await self._deliver_sign_task(notice.group_id)
                # Liveness: re-offer signature requests whose first delivery
                # may have raced a client that attached its signer late.
                for group_id in self._groups.group_ids():
                    age = self._groups.sign_task_age(group_id)
                    if age is not None and age > GROUP_SIGN_REOFFER_SECONDS:
                        self._last_sign_delivery.pop(group_id, None)
                        await self._deliver_sign_task(group_id)
        except asyncio.CancelledError:
            raise

    # ---------------------------------------------------- rendezvous routing

    def _attach_membership(
        self, client_id: int, context: _ClientContext, channel_id: str, role: str
    ) -> str | None:
        """Register channel membership synchronously; returns an error or None.

        Shared by ATTACH and REDEEM: keeping the capacity check and the
        membership write in one await-free call makes concurrent admissions
        to the same two-slot channel race-free.
        """

        channel = self._channels.setdefault(channel_id, _Channel(channel_id))
        if client_id in channel.members:
            channel.members[client_id] = role  # re-attach refreshes the role
        elif len(channel.members) >= CHANNEL_CAPACITY:
            return f"Channel {channel_id} already has {CHANNEL_CAPACITY} participants."
        else:
            channel.members[client_id] = role
        context.attachments[channel_id] = role
        return None

    async def _handle_attach(self, client_id: int, context: _ClientContext, packet: Packet) -> None:
        channel_id = str(packet.payload["channel"])
        role = str(packet.payload["role"])
        membership_error = self._attach_membership(client_id, context, channel_id, role)
        if membership_error is not None:
            await self._channel_error(context, "relay/channel-full", membership_error, channel_id)
            return

        channel = self._channels[channel_id]
        other_count = len(channel.members) - 1
        await self._send(context, attached_packet(channel_id, role, other_count))
        self._logger.info(
            "channel attach — %s as %s on %s (%d member(s))",
            context.remote,
            role,
            channel_id,
            len(channel.members),
        )
        counterparty = channel.counterparty(client_id)
        if counterparty is not None:
            await self._notify_peer_joined(client_id, channel_id)

    async def _handle_detach(
        self, client_id: int, context: _ClientContext, channel_id: str
    ) -> None:
        if channel_id not in context.attachments:
            await self._channel_error(
                context, "relay/not-attached", f"Not attached to channel {channel_id}.", channel_id
            )
            return
        await self._leave_channel(client_id, channel_id)

    async def _handle_forward(
        self, client_id: int, context: _ClientContext, packet: Packet
    ) -> None:
        channel_id = str(packet.payload["channel"])
        channel = self._channels.get(channel_id)
        if channel is None or client_id not in channel.members:
            await self._channel_error(
                context,
                "relay/not-attached",
                f"Attach to channel {channel_id} before forwarding.",
                channel_id,
            )
            return
        counterparty = channel.counterparty(client_id)
        if counterparty is None:
            await self._channel_error(
                context,
                "relay/no-peer",
                f"No peer is attached to channel {channel_id}; payload dropped.",
                channel_id,
            )
            return
        target = self._clients.get(counterparty[0])
        if target is None or target.connection is None or target.connection.closed:
            await self._channel_error(
                context,
                "relay/no-peer",
                f"The peer on channel {channel_id} is unreachable; payload dropped.",
                channel_id,
            )
            return
        # The body is end-to-end ciphertext by contract; the relay routes it
        # untouched and never inspects it.
        await self._send(target, forward_packet(channel_id, str(packet.payload["body"])))
        self._logger.debug(
            "forwarded %d bytes on %s — %s → %s",
            len(str(packet.payload["body"])),
            channel_id,
            context.remote,
            target.remote,
        )

    async def _notify_peer_joined(self, client_id: int, channel_id: str) -> None:
        channel = self._channels.get(channel_id)
        if channel is None:
            return
        counterparty = channel.counterparty(client_id)
        if counterparty is None:
            return
        target = self._clients.get(counterparty[0])
        if target is not None:
            with suppress(TransportError, ConnectionError, OSError):
                await self._send(target, peer_packet(channel_id, "joined"))

    async def _notify_peer_left(self, client_id: int, channel_id: str) -> None:
        channel = self._channels.get(channel_id)
        if channel is None:
            return
        remaining = next(iter(channel.members), None)
        if remaining is None:
            return
        target = self._clients.get(remaining)
        if target is not None:
            with suppress(TransportError, ConnectionError, OSError):
                await self._send(target, peer_packet(channel_id, "left"))

    async def _leave_channel(self, client_id: int, channel_id: str) -> None:
        channel = self._channels.get(channel_id)
        if channel is None or client_id not in channel.members:
            return
        del channel.members[client_id]
        context = self._clients.get(client_id)
        if context is not None:
            context.attachments.pop(channel_id, None)
        self._logger.debug("channel detach — client %d left %s", client_id, channel_id)
        if not channel.members:
            del self._channels[channel_id]
            return
        await self._notify_peer_left(client_id, channel_id)

    # ---------------------------------------------------------------- teardown

    async def _channel_error(
        self, context: _ClientContext, code: str, message: str, channel_id: str
    ) -> None:
        """ERROR packet scoped to a channel — the channel id travels so the
        client can reject the matching pending operation."""

        await self._send(
            context,
            Packet(
                PacketType.ERROR,
                {"code": code, "message": message, "channel": channel_id},
            ),
        )

    async def _send(self, context: _ClientContext, packet: Packet) -> None:
        await context.conn.send_text(encode_packet(packet).decode("utf-8"))

    async def _disconnect_client(self, context: _ClientContext, reason: str) -> None:
        if context.connection is None or context.connection.closed:
            return
        if context.session is not None:
            with suppress(TransportError, ConnectionError, OSError):
                await self._send(context, disconnect_packet(reason))
        await context.conn.close()

    async def _detach_client(self, client_id: int, context: _ClientContext) -> None:
        self._clients.pop(client_id, None)
        for channel_id in list(context.attachments):
            await self._leave_channel(client_id, channel_id)
        context.attachments.clear()
        if context.session is not None:
            # Group lifecycle: drop session bindings (memberships remain on
            # the roster; pending joins bound to the dead session expire).
            session_id = context.session.session_id
            self._groups.disbind_session(session_id)
            self._contexts_by_session.pop(session_id, None)
            self.sessions.remove(session_id)
            context.session = None
            self._event_windows.pop(session_id, None)
            for key in [key for key in self._forward_tokens if key[1] == session_id]:
                del self._forward_tokens[key]
        if context.connection is not None and not context.connection.closed:
            await context.connection.close()


def main(argv: list[str] | None = None) -> int:
    """Console entry for ``python -m ghostlink.transport.relay.server``."""

    parser = argparse.ArgumentParser(
        prog="ghostlink-relay",
        description="Run the reference GhostLink relay (development use).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_RELAY_PORT, help="bind port (0 = ephemeral)"
    )
    parser.add_argument(
        "--session-ttl",
        type=float,
        default=RELAY_SESSION_TTL_SECONDS,
        metavar="SECONDS",
        help="idle session time-to-live",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = get_logger("transport.relay.server")
    server = RelayServer(host=args.host, port=args.port, session_ttl_seconds=args.session_ttl)
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        logger.info("relay interrupted; exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
