"""Connection lifecycle management.

:class:`ConnectionManager` owns one logical connection across physical
transports: connecting → connected → (reconnecting) → disconnected/closed.
It enforces legal transitions, applies connect/handshake timeouts, restarts
dropped connections with exponential backoff, runs the inbound reader loop,
and accumulates lifecycle statistics for the dashboards.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum

from ghostlink.core.logging import get_logger
from ghostlink.exceptions.transport import ConnectionTimeoutError, TransportError
from ghostlink.transport.transport import Transport, TransportState


class ConnectionState(str, Enum):
    """Public lifecycle states of a managed connection."""

    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    TIMED_OUT = "timed_out"
    DISCONNECTED = "disconnected"
    CLOSED = "closed"


_ALLOWED_TRANSITIONS: dict[ConnectionState, frozenset[ConnectionState]] = {
    ConnectionState.CONNECTING: frozenset(
        {
            ConnectionState.CONNECTED,
            ConnectionState.TIMED_OUT,
            ConnectionState.DISCONNECTED,
            ConnectionState.CLOSED,
        }
    ),
    ConnectionState.CONNECTED: frozenset(
        {
            ConnectionState.RECONNECTING,
            ConnectionState.TIMED_OUT,
            ConnectionState.DISCONNECTED,
            ConnectionState.CLOSED,
        }
    ),
    ConnectionState.RECONNECTING: frozenset(
        {
            ConnectionState.CONNECTED,
            ConnectionState.TIMED_OUT,
            ConnectionState.DISCONNECTED,
            ConnectionState.CLOSED,
        }
    ),
    ConnectionState.TIMED_OUT: frozenset(
        {
            ConnectionState.RECONNECTING,
            ConnectionState.DISCONNECTED,
            ConnectionState.CLOSED,
        }
    ),
    ConnectionState.DISCONNECTED: frozenset(
        {
            ConnectionState.CONNECTING,
            ConnectionState.RECONNECTING,
            ConnectionState.CLOSED,
        }
    ),
    ConnectionState.CLOSED: frozenset(),
}


@dataclass(frozen=True, slots=True)
class StateChange:
    """One recorded lifecycle transition."""

    state: ConnectionState
    at: datetime
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """Exponential backoff with jitter for automatic reconnection."""

    attempts: int = 3
    base_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    jitter: float = 0.25

    def delay_for(self, attempt: int) -> float:
        """Backoff for retry ``attempt`` (1-based), with ±``jitter`` wobble."""

        raw = min(self.base_delay_seconds * (2 ** (attempt - 1)), self.max_delay_seconds)
        wobble = raw * self.jitter
        return float(max(0.0, raw + random.uniform(-wobble, wobble)))


@dataclass(slots=True)
class ConnectionStats:
    """Lifecycle counters and history for one managed connection."""

    state_history: list[StateChange] = field(default_factory=list)
    connect_count: int = 0
    successful_connects: int = 0
    reconnect_attempts_made: int = 0
    last_error: str = ""
    connected_since: datetime | None = None

    @property
    def uptime_seconds(self) -> float:
        if self.connected_since is None:
            return 0.0
        return max(0.0, (datetime.now(UTC) - self.connected_since).total_seconds())


class ConnectionManager:
    """State machine managing one logical connection over fresh transports."""

    def __init__(
        self,
        *,
        transport_factory: Callable[[], Awaitable[Transport]],
        on_bytes: Callable[[bytes], None],
        reconnect_policy: ReconnectPolicy | None = None,
        name: str = "connection",
    ) -> None:
        self._transport_factory = transport_factory
        self._on_bytes = on_bytes
        self._policy = reconnect_policy or ReconnectPolicy(attempts=0)
        self._name = name
        self._logger = get_logger(f"transport.connection.{name}")
        self._state = ConnectionState.DISCONNECTED
        self._transport: Transport | None = None
        self._handshake: Callable[[Transport], Awaitable[None]] | None = None
        self._on_connected: Callable[[Transport], Awaitable[None]] | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None
        self._close_requested = False
        self._closed_event = asyncio.Event()
        self._listeners: list[Callable[[StateChange], None]] = []
        self.stats = ConnectionStats()

    # ------------------------------------------------------------- properties

    @property
    def state(self) -> ConnectionState:
        return self._state

    @property
    def transport(self) -> Transport | None:
        return self._transport

    @property
    def is_usable(self) -> bool:
        return (
            self._state is ConnectionState.CONNECTED
            and self._transport is not None
            and self._transport.state is TransportState.OPEN
        )

    # ------------------------------------------------------------ state machine

    def add_state_listener(self, listener: Callable[[StateChange], None]) -> None:
        self._listeners.append(listener)

    def remove_state_listener(self, listener: Callable[[StateChange], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _transition(self, new_state: ConnectionState, *, reason: str = "") -> StateChange:
        allowed = _ALLOWED_TRANSITIONS[self._state]
        if new_state not in allowed:
            raise TransportError(
                f"Illegal connection transition {self._state.value} → {new_state.value}.",
                hint="This is a defect in the connection manager; please report it.",
            )
        change = StateChange(state=new_state, at=datetime.now(UTC), reason=reason)
        self._state = new_state
        self.stats.state_history.append(change)
        self._logger.debug("state → %s%s", new_state.value, f" ({reason})" if reason else "")
        for listener in self._listeners:
            listener(change)
        return change

    # -------------------------------------------------------------- lifecycle

    async def open(
        self,
        *,
        handshake: Callable[[Transport], Awaitable[None]] | None = None,
        on_connected: Callable[[Transport], Awaitable[None]] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        """Establish the connection: transport open → handshake → reader loop."""

        if self._state is not ConnectionState.DISCONNECTED:
            raise TransportError(
                f"Connection '{self._name}' cannot open from state {self._state.value}.",
                hint="Close the connection before opening it again.",
            )
        self._transition(ConnectionState.CONNECTING, reason="open requested")
        self._close_requested = False
        self._closed_event.clear()
        self._handshake = handshake
        self._on_connected = on_connected
        self.stats.connect_count += 1

        transport = await self._transport_factory()
        try:
            await asyncio.wait_for(transport.open(), timeout=timeout_seconds)
            if handshake is not None:
                await asyncio.wait_for(handshake(transport), timeout=timeout_seconds)
        except (TimeoutError, ConnectionTimeoutError) as exc:
            await transport.close()
            self._transition(ConnectionState.TIMED_OUT, reason="deadline exceeded")
            self._transition(ConnectionState.DISCONNECTED, reason="timed out")
            self.stats.last_error = "timed out"
            self._closed_event.set()
            raise ConnectionTimeoutError(
                f"Connection '{self._name}' timed out after {timeout_seconds:.1f}s.",
                hint="Check network reachability, or raise the connect timeout.",
            ) from exc
        except Exception as exc:
            await transport.close()
            self._transition(ConnectionState.DISCONNECTED, reason=str(exc))
            self.stats.last_error = str(exc)
            self._closed_event.set()
            raise

        self._transport = transport
        self.stats.successful_connects += 1
        self.stats.connected_since = datetime.now(UTC)
        self._transition(ConnectionState.CONNECTED, reason="handshake complete")
        self._logger.info(
            "connection opened — %s",
            transport.remote_description,
        )
        if on_connected is not None:
            await on_connected(transport)
        self._reader_task = asyncio.create_task(
            self._reader_loop(), name=f"ghostlink-reader-{self._name}"
        )

    async def send(self, data: bytes) -> None:
        """Send framed bytes; only valid while CONNECTED."""

        if not self.is_usable or self._transport is None:
            raise TransportError(
                f"Connection '{self._name}' is not connected (state: {self._state.value}).",
                hint="Connect before sending, or wait for reconnection to finish.",
            )
        await self._transport.send(data)

    async def close(self, *, reason: str = "shutdown") -> None:
        """Graceful shutdown: stop loops, close transport, mark CLOSED."""

        if self._state is ConnectionState.CLOSED:
            return
        self._close_requested = True
        reconnect_task = self._reconnect_task
        self._reconnect_task = None
        if reconnect_task is not None and not reconnect_task.done():
            reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await reconnect_task

        reader_task = self._reader_task
        self._reader_task = None
        transport = self._transport
        self._transport = None

        if reader_task is not None and not reader_task.done():
            reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await reader_task

        if transport is not None and transport.state is TransportState.OPEN:
            await transport.close()
            self._logger.info("connection closed — %s (%s)", transport.remote_description, reason)

        # CLOSED was already handled by the early return above; anything else
        # travels through DISCONNECTED (unless it already is) into CLOSED.
        if self._state is not ConnectionState.DISCONNECTED:
            self._transition(ConnectionState.DISCONNECTED, reason=reason)
        self._transition(ConnectionState.CLOSED, reason=reason)
        self._closed_event.set()

    async def wait_closed(self) -> None:
        """Wait until the connection reaches CLOSED."""

        await self._closed_event.wait()

    async def __aenter__(self) -> ConnectionManager:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.close(reason="context exit")

    # -------------------------------------------------------------- reader loop

    async def _reader_loop(self) -> None:
        transport = self._transport
        try:
            while not self._close_requested and transport is not None:
                data = await transport.receive()
                if data is None:
                    reason = "peer closed the channel"
                    if not self._close_requested:
                        self._logger.info("connection lost — %s", reason)
                        await self._handle_unexpected_close(reason)
                    return
                self._on_bytes(data)
        except asyncio.CancelledError:
            raise
        except TransportError as exc:
            if not self._close_requested:
                self._logger.info("connection error — %s", exc)
                self.stats.last_error = str(exc)
                await self._handle_unexpected_close(str(exc))
        except Exception as exc:  # defensive: never let the reader die silently
            self._logger.debug("unexpected reader failure: %s", exc)
            self.stats.last_error = str(exc)
            if not self._close_requested:
                await self._handle_unexpected_close(str(exc))

    async def _handle_unexpected_close(self, reason: str) -> None:
        """Transition out of CONNECTED and kick off recovery if allowed."""

        transport = self._transport
        if transport is not None and transport.state is TransportState.OPEN:
            await transport.close()
        if self._state is ConnectionState.CONNECTED:
            self._transition(ConnectionState.DISCONNECTED, reason=reason)
        if self._policy.attempts > 0 and not self._close_requested:
            self._reconnect_task = asyncio.create_task(
                self._reconnect_loop(reason), name=f"ghostlink-reconnect-{self._name}"
            )
        else:
            if self._state is ConnectionState.DISCONNECTED:
                self._transition(ConnectionState.CLOSED, reason=reason)
            self._closed_event.set()

    # -------------------------------------------------------------- reconnection

    async def _reconnect_loop(self, reason: str) -> None:
        self._transition(ConnectionState.RECONNECTING, reason=reason)
        for attempt in range(1, self._policy.attempts + 1):
            if self._close_requested:
                return
            self.stats.reconnect_attempts_made += 1
            delay = self._policy.delay_for(attempt)
            self._logger.info(
                "reconnect attempt %d/%d in %.2fs", attempt, self._policy.attempts, delay
            )
            try:
                await asyncio.sleep(delay)
                transport = await self._transport_factory()
                await transport.open()
                if self._handshake is not None:
                    await self._handshake(transport)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._logger.info(
                    "reconnect attempt %d/%d failed: %s", attempt, self._policy.attempts, exc
                )
                continue
            self._transport = transport
            self.stats.successful_connects += 1
            self.stats.connected_since = datetime.now(UTC)
            self._transition(
                ConnectionState.CONNECTED, reason=f"recovered after {attempt} attempt(s)"
            )
            self._logger.info("connection recovered — %s", transport.remote_description)
            if self._on_connected is not None:
                await self._on_connected(transport)
            self._reader_task = asyncio.create_task(
                self._reader_loop(), name=f"ghostlink-reader-{self._name}"
            )
            return

        self._logger.info("reconnect attempts exhausted — giving up")
        if self._state is ConnectionState.RECONNECTING:
            self._transition(ConnectionState.DISCONNECTED, reason="reconnect exhausted")
            self._transition(ConnectionState.CLOSED, reason="reconnect exhausted")
        self._closed_event.set()

    async def reconnect(self) -> None:
        """Force one supervised reconnect cycle (used by CLI and tests)."""

        if self._policy.attempts <= 0:
            raise TransportError(
                f"Connection '{self._name}' has reconnection disabled.",
                hint="Configure reconnect_attempts > 0 to enable it.",
            )
        reconnect_task = self._reconnect_task
        if reconnect_task is not None and not reconnect_task.done():
            raise TransportError(
                f"Connection '{self._name}' is already reconnecting.",
                hint="Wait for the in-flight supervised reconnect to finish.",
            )
        # Detach the old transport and its reader deterministically, so the
        # reader cannot start a competing recovery cycle mid-reconnect.
        reader_task = self._reader_task
        self._reader_task = None
        transport = self._transport
        self._transport = None
        if reader_task is not None and not reader_task.done():
            reader_task.cancel()
            with suppress(asyncio.CancelledError):
                await reader_task
        if transport is not None and transport.state is TransportState.OPEN:
            await transport.close()
        if self._state is ConnectionState.CONNECTED:
            self._transition(ConnectionState.DISCONNECTED, reason="manual reconnect")
        await self._reconnect_loop("manual reconnect")
