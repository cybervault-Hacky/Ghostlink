"""RoomService: persistence, settings defaults, and lifecycle operations."""

from __future__ import annotations

from pathlib import Path

from ghostlink.models.invite import Invite
from ghostlink.models.room import Room
from ghostlink.models.settings import AppSettings
from ghostlink.services.rooms import RoomService
from ghostlink.storage.json_store import JsonFileStorage
from ghostlink.storage.manager import StorageManager


def _service(root: Path, settings: AppSettings) -> RoomService:
    return RoomService(StorageManager(root), settings)


class TestHostRoom:
    def test_host_room_persists_room_and_invite(
        self, tmp_path: Path, app_settings: AppSettings
    ) -> None:
        service = _service(tmp_path / "state", app_settings)
        room, invite = service.host_room(name="Lounge")

        room_store = JsonFileStorage(tmp_path / "state" / "rooms.json")
        invite_store = JsonFileStorage(tmp_path / "state" / "invites.json")
        assert room.room_id in room_store
        assert invite.token in invite_store
        assert room.name == "Lounge"
        assert invite.room_id == room.room_id

    def test_host_room_uses_configured_defaults(
        self, tmp_path: Path, app_settings: AppSettings
    ) -> None:
        service = _service(tmp_path / "state", app_settings)
        room, invite = service.host_room()
        # default configuration: 60-minute rooms, 15-minute one-time invites
        assert room.expires_at is not None
        assert 3500 <= (room.expires_at - room.created_at).total_seconds() <= 3600
        assert invite.one_time is True
        assert (invite.expires_at - invite.created_at).total_seconds() == 900

    def test_host_room_honours_overrides(self, tmp_path: Path, app_settings: AppSettings) -> None:
        service = _service(tmp_path / "state", app_settings)
        room, invite = service.host_room(
            name="Vault",
            room_lifetime_minutes=0,
            invite_one_time=False,
            invite_lifetime_minutes=30,
        )
        assert room.expires_at is None
        assert invite.one_time is False
        assert (invite.expires_at - invite.created_at).total_seconds() == 1800

    def test_host_id_is_stable_per_service(self, tmp_path: Path, app_settings: AppSettings) -> None:
        service = _service(tmp_path / "state", app_settings)
        first = service.create_room()
        second = service.create_room()
        assert service.host_id == first.host_id == second.host_id


class TestQueries:
    def test_list_rooms_newest_first(self, tmp_path: Path, app_settings: AppSettings) -> None:
        service = _service(tmp_path / "state", app_settings)
        first = service.create_room(name="first")
        second = service.create_room(name="second")
        rooms = service.list_rooms()
        assert [room.name for room in rooms] == ["second", "first"]
        assert isinstance(rooms[0], Room)
        assert second.created_at >= first.created_at

    def test_list_invites_matches_created_invites(
        self, tmp_path: Path, app_settings: AppSettings
    ) -> None:
        service = _service(tmp_path / "state", app_settings)
        room = service.create_room()
        invite = service.create_invite(room)
        invites = service.list_invites()
        assert [entry.token for entry in invites] == [invite.token]
        assert isinstance(invites[0], Invite)

    def test_get_room_round_trips(self, tmp_path: Path, app_settings: AppSettings) -> None:
        service = _service(tmp_path / "state", app_settings)
        room = service.create_room(name="Lounge", lifetime_minutes=5)
        fetched = service.get_room(room.room_id)
        assert fetched == room

    def test_get_room_unknown_returns_none(self, tmp_path: Path, app_settings: AppSettings) -> None:
        service = _service(tmp_path / "state", app_settings)
        assert service.get_room("gl-room-ABCD-EFGH-JKMN") is None

    def test_close_room_persists_transition(
        self, tmp_path: Path, app_settings: AppSettings
    ) -> None:
        state = tmp_path / "state"
        service = _service(state, app_settings)
        room = service.create_room()
        closed = service.close_room(room.room_id)
        assert closed is not None and closed.state.value == "closed"
        # a fresh service over the same store sees the closed record
        reloaded = _service(state, app_settings).get_room(room.room_id)
        assert reloaded is not None and reloaded.state.value == "closed"

    def test_close_room_unknown_returns_none(
        self, tmp_path: Path, app_settings: AppSettings
    ) -> None:
        service = _service(tmp_path / "state", app_settings)
        assert service.close_room("gl-room-ABCD-EFGH-JKMN") is None
