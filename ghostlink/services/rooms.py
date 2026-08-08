"""Room and invite lifecycle service (Phase 2).

Owns creation, persistence, and lookup of rooms and invites in the durable
``rooms`` and ``invites`` stores. The service never invents network state:
it records what the user created and derives expiry from wall-clock time.
"""

from __future__ import annotations

import secrets

from ghostlink.core.logging import get_logger
from ghostlink.models.invite import Invite
from ghostlink.models.room import Room
from ghostlink.models.settings import AppSettings
from ghostlink.storage.manager import StorageManager


class RoomService:
    """Creates and persists rooms and their invites."""

    ROOMS_NAMESPACE = "rooms"
    INVITES_NAMESPACE = "invites"

    def __init__(self, storage: StorageManager, settings: AppSettings) -> None:
        self._storage = storage
        self._settings = settings
        self._logger = get_logger("services.rooms")
        self._host_id = f"host_{secrets.token_hex(6)}"

    @property
    def host_id(self) -> str:
        """Stable per-process host identity used for hosted rooms."""

        return self._host_id

    # ------------------------------------------------------------------ create

    def create_room(
        self,
        *,
        name: str | None = None,
        lifetime_minutes: int | None = None,
    ) -> Room:
        """Create and persist a room, using configured defaults when omitted."""

        lifetime = (
            lifetime_minutes
            if lifetime_minutes is not None
            else self._settings.rooms.default_lifetime_minutes
        )
        room = Room.create(name=name, lifetime_minutes=lifetime, host_id=self._host_id)
        self._storage.store(self.ROOMS_NAMESPACE)[room.room_id] = room.to_dict()
        self._logger.info("Room created — %s (lifetime=%dm)", room.room_id, lifetime)
        return room

    def create_invite(
        self,
        room: Room,
        *,
        one_time: bool | None = None,
        lifetime_minutes: int | None = None,
    ) -> Invite:
        """Create and persist an invite bound to ``room``."""

        invite = Invite.create(
            room_id=room.room_id,
            lifetime_minutes=(
                lifetime_minutes
                if lifetime_minutes is not None
                else self._settings.invites.default_lifetime_minutes
            ),
            one_time=one_time if one_time is not None else self._settings.invites.one_time,
        )
        self._storage.store(self.INVITES_NAMESPACE)[invite.token] = invite.to_dict()
        self._logger.info(
            "Invite created — %s… for %s (one_time=%s)",
            invite.token[:12],
            room.room_id,
            invite.one_time,
        )
        return invite

    def host_room(
        self,
        *,
        name: str | None = None,
        room_lifetime_minutes: int | None = None,
        invite_one_time: bool | None = None,
        invite_lifetime_minutes: int | None = None,
    ) -> tuple[Room, Invite]:
        """Convenience: create a room and its first invite in one call."""

        room = self.create_room(name=name, lifetime_minutes=room_lifetime_minutes)
        invite = self.create_invite(
            room, one_time=invite_one_time, lifetime_minutes=invite_lifetime_minutes
        )
        return room, invite

    # ------------------------------------------------------------------ query

    def list_rooms(self) -> tuple[Room, ...]:
        """All persisted rooms, newest first."""

        store = self._storage.store(self.ROOMS_NAMESPACE)
        rooms = [Room.from_dict(store[key]) for key in store]
        return tuple(sorted(rooms, key=lambda room: room.created_at, reverse=True))

    def list_invites(self) -> tuple[Invite, ...]:
        """All persisted invites, newest first."""

        store = self._storage.store(self.INVITES_NAMESPACE)
        invites = [Invite.from_dict(store[key]) for key in store]
        return tuple(sorted(invites, key=lambda invite: invite.created_at, reverse=True))

    def get_room(self, room_id: str) -> Room | None:
        """Fetch a room by canonical id; ``None`` when unknown."""

        store = self._storage.store(self.ROOMS_NAMESPACE)
        raw = store.get(room_id)
        return Room.from_dict(raw) if raw is not None else None

    def close_room(self, room_id: str) -> Room | None:
        """Mark a room CLOSED and persist the transition."""

        room = self.get_room(room_id)
        if room is None:
            return None
        closed = room.close()
        self._storage.store(self.ROOMS_NAMESPACE)[room_id] = closed.to_dict()
        self._logger.info("Room closed — %s", room_id)
        return closed
