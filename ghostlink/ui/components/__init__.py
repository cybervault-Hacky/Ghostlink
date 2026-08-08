"""Reusable, theme-aware UI building blocks.

Every component renders exclusively through semantic ``gl.*`` styles, so the
active theme re-skins them automatically. Screens compose these primitives;
they never assemble ad-hoc markup for common patterns.
"""

from __future__ import annotations

from ghostlink.ui.components.badges import BadgeTone, badge, badge_row
from ghostlink.ui.components.dialogs import (
    confirm,
    notice_dialog,
    roadmap_dialog,
    wait_for_enter,
)
from ghostlink.ui.components.notifications import NotificationCenter, NotificationLevel
from ghostlink.ui.components.panels import app_panel, section_panel
from ghostlink.ui.components.progress import StepProgress
from ghostlink.ui.components.tables import info_table, kv_grid

__all__ = [
    "BadgeTone",
    "NotificationCenter",
    "NotificationLevel",
    "StepProgress",
    "app_panel",
    "badge",
    "badge_row",
    "confirm",
    "info_table",
    "kv_grid",
    "notice_dialog",
    "roadmap_dialog",
    "section_panel",
    "wait_for_enter",
]
