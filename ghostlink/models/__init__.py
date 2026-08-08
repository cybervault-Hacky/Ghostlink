"""Typed domain models used across every GhostLink layer."""

from __future__ import annotations

from ghostlink.models.environment import (
    ColorSupport,
    EnvironmentInfo,
    PlatformKind,
)
from ghostlink.models.invite import (
    Invite,
    InviteEffectiveState,
    InviteState,
    is_valid_invite_token,
)
from ghostlink.models.room import (
    Room,
    RoomEffectiveState,
    RoomState,
    is_valid_room_id,
    normalize_room_id,
)
from ghostlink.models.session import SessionInfo
from ghostlink.models.settings import (
    AppSettings,
    DiagnosticsSettings,
    InvitesSettings,
    NotificationSettings,
    RelaySettings,
    RoomsSettings,
    StorageSettings,
    UISettings,
)
from ghostlink.models.theme import ThemeSpec

__all__ = [
    "AppSettings",
    "ColorSupport",
    "DiagnosticsSettings",
    "EnvironmentInfo",
    "Invite",
    "InviteEffectiveState",
    "InviteState",
    "InvitesSettings",
    "NotificationSettings",
    "PlatformKind",
    "RelaySettings",
    "Room",
    "RoomEffectiveState",
    "RoomState",
    "RoomsSettings",
    "SessionInfo",
    "StorageSettings",
    "ThemeSpec",
    "UISettings",
    "is_valid_invite_token",
    "is_valid_room_id",
    "normalize_room_id",
]
