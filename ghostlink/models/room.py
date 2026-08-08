"""Room foundation model (Phase 2).

Rooms are rendezvous identities: a room records who hosts it, when it was
created, and when it expires. The one-to-one conversation itself lives in
the messaging layer (Phase 3); the persisted guest roster stays a Phase-4+
concern.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import Enum

from ghostlink.constants.net import ROOM_ID_ALPHABET, ROOM_ID_PREFIX
from ghostlink.exceptions.config import ConfigValidationError

ROOM_ID_PATTERN = re.compile(
    rf"^{ROOM_ID_PREFIX}-(?:[{ROOM_ID_ALPHABET}]{{4}}-){{2}}[{ROOM_ID_ALPHABET}]{{4}}$"
)
MAX_ROOM_NAME_LENGTH = 48


class RoomState(str, Enum):
    """Persisted lifecycle states of a room."""

    OPEN = "open"
    CLOSED = "closed"


class RoomEffectiveState(str, Enum):
    """Derived state returned by :meth:`Room.effective_state`."""

    OPEN = "open"
    EXPIRED = "expired"
    CLOSED = "closed"


def generate_room_id() -> str:
    """Create a canonical room identifier: ``gl-room-XXXX-XXXX-XXXX``."""

    groups = ("".join(secrets.choice(ROOM_ID_ALPHABET) for _ in range(4)) for _ in range(3))
    return f"{ROOM_ID_PREFIX}-" + "-".join(groups)


def normalize_room_id(candidate: str) -> str:
    """Return ``candidate`` in canonical form: lowercase prefix, upper groups."""

    cleaned = candidate.strip().upper().replace(" ", "")
    marker = f"{ROOM_ID_PREFIX.upper()}-"
    if cleaned.startswith(marker):
        cleaned = cleaned[len(marker) :]
    return f"{ROOM_ID_PREFIX}-{cleaned}"


def is_valid_room_id(candidate: str) -> bool:
    """Strict format validation against the canonical room identifier form."""

    return bool(ROOM_ID_PATTERN.fullmatch(candidate.strip()))


@dataclass(frozen=True, slots=True)
class Room:
    """An immutable room record.

    Conversations today are one-to-one over the relay; a persisted guest
    roster is intentionally left to the group-chat milestone.
    """

    room_id: str
    name: str | None
    created_at: datetime
    expires_at: datetime | None
    state: RoomState
    host_id: str
    guests: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def create(
        cls,
        *,
        name: str | None = None,
        lifetime_minutes: int = 0,
        host_id: str | None = None,
        now: datetime | None = None,
    ) -> Room:
        """Create a fresh OPEN room. ``lifetime_minutes`` 0 means no expiry."""

        moment = now if now is not None else datetime.now(UTC)
        if lifetime_minutes < 0:
            raise ConfigValidationError(
                f"Room lifetime must be ≥ 0 minutes, got {lifetime_minutes}.",
                hint="Use 0 for a room that never expires.",
            )
        cleaned_name: str | None = None
        if name is not None:
            cleaned_name = name.strip()
            if len(cleaned_name) > MAX_ROOM_NAME_LENGTH:
                raise ConfigValidationError(
                    f"Room name must be at most {MAX_ROOM_NAME_LENGTH} characters.",
                    hint="Shorten the name or omit it for an anonymous room.",
                )
            if not cleaned_name:
                cleaned_name = None
        return cls(
            room_id=generate_room_id(),
            name=cleaned_name,
            created_at=moment,
            expires_at=moment + timedelta(minutes=lifetime_minutes)
            if lifetime_minutes > 0
            else None,
            state=RoomState.OPEN,
            host_id=host_id or f"host_{secrets.token_hex(6)}",
        )

    # ------------------------------------------------------------------ state

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.expires_at is None:
            return False
        moment = now if now is not None else datetime.now(UTC)
        return moment >= self.expires_at

    def effective_state(self, now: datetime | None = None) -> RoomEffectiveState:
        """Fold persisted state and expiry into a single derived state."""

        if self.state is RoomState.CLOSED:
            return RoomEffectiveState.CLOSED
        if self.is_expired(now):
            return RoomEffectiveState.EXPIRED
        return RoomEffectiveState.OPEN

    def close(self) -> Room:
        """Return a copy of this room marked CLOSED."""

        return replace(self, state=RoomState.CLOSED)

    @property
    def display_name(self) -> str:
        return self.name if self.name else "Unnamed room"

    @property
    def ttl_seconds(self) -> float | None:
        if self.expires_at is None:
            return None
        return max(0.0, (self.expires_at - datetime.now(UTC)).total_seconds())

    # ----------------------------------------------------------- serialization

    def to_dict(self) -> dict[str, object]:
        return {
            "v": 1,
            "room_id": self.room_id,
            "name": self.name,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "state": self.state.value,
            "host_id": self.host_id,
            "guests": list(self.guests),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Room:
        """Rebuild a room from storage, rejecting malformed documents."""

        expected = {"room_id", "created_at", "expires_at", "state", "host_id"}
        missing = expected - set(data)
        if missing:
            raise ConfigValidationError(
                f"Stored room document is missing field(s): {', '.join(sorted(missing))}.",
                hint="The rooms store is corrupt; remove state/rooms.json to reset.",
            )
        room_id = str(data["room_id"])
        if not is_valid_room_id(room_id):
            raise ConfigValidationError(
                f"Stored room id '{room_id}' is malformed.",
                hint="The rooms store is corrupt; remove state/rooms.json to reset.",
            )
        created_at = datetime.fromisoformat(str(data["created_at"]))
        expires_raw = data["expires_at"]
        expires_at = datetime.fromisoformat(str(expires_raw)) if expires_raw else None
        guests_raw = data.get("guests") or ()
        guest_list = guests_raw if isinstance(guests_raw, list | tuple) else ()
        return cls(
            room_id=room_id,
            name=str(data["name"]) if data.get("name") else None,
            created_at=created_at,
            expires_at=expires_at,
            state=RoomState(str(data["state"])),
            host_id=str(data["host_id"]),
            guests=tuple(str(guest) for guest in guest_list),
        )
