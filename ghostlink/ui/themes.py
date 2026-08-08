"""Color theme engine.

Themes are immutable :class:`ThemeSpec` palettes registered by name in the
:class:`ThemeEngine`. UI components reference only semantic ``gl.*`` styles,
so switching themes re-skins the entire application without touching any
component code.
"""

from __future__ import annotations

from rich.theme import Theme

from ghostlink.constants.app import DEFAULT_THEME
from ghostlink.exceptions.themes import ThemeNotFoundError
from ghostlink.models.theme import ThemeSpec


def _phantom() -> ThemeSpec:
    return ThemeSpec(
        name="phantom",
        description="Signature GhostLink palette — cyan surfacing from deep violet.",
        primary="#22D3EE",
        accent="#A78BFA",
        success="#34D399",
        warning="#FBBF24",
        error="#FB7185",
        info="#60A5FA",
        muted="#64748B",
        text="#E2E8F0",
        border="#334155",
        highlight="#22D3EE",
        gradient=("#22D3EE", "#A78BFA"),
        on_accent="#0F172A",
    )


def _emerald() -> ThemeSpec:
    return ThemeSpec(
        name="emerald",
        description="Organic greens with teal depth.",
        primary="#34D399",
        accent="#2DD4BF",
        success="#4ADE80",
        warning="#FACC15",
        error="#F87171",
        info="#38BDF8",
        muted="#6B7280",
        text="#ECFDF5",
        border="#1F5143",
        highlight="#34D399",
        gradient=("#34D399", "#2DD4BF"),
        on_accent="#052E1B",
    )


def _ember() -> ThemeSpec:
    return ThemeSpec(
        name="ember",
        description="Warm ambers and sunset rose.",
        primary="#FBBF24",
        accent="#FB923C",
        success="#A3E635",
        warning="#FDE047",
        error="#F87171",
        info="#FCD34D",
        muted="#78716C",
        text="#FFFBEB",
        border="#57534E",
        highlight="#FBBF24",
        gradient=("#FBBF24", "#F87171"),
        on_accent="#451A03",
    )


def _mono() -> ThemeSpec:
    return ThemeSpec(
        name="mono",
        description="Grayscale palette for low-color or accessibility contexts.",
        primary="#D4D4D4",
        accent="#A3A3A3",
        success="#D4D4D4",
        warning="#D4D4D4",
        error="#D4D4D4",
        info="#D4D4D4",
        muted="#737373",
        text="#E5E5E5",
        border="#525252",
        highlight="#FFFFFF",
        gradient=("#E5E5E5", "#A3A3A3"),
        on_accent="#171717",
    )


class ThemeEngine:
    """Registry of the color themes available to GhostLink."""

    def __init__(self, themes: dict[str, ThemeSpec] | None = None) -> None:
        self._themes: dict[str, ThemeSpec] = (
            dict(themes)
            if themes is not None
            else {spec.name: spec for spec in (_phantom(), _emerald(), _ember(), _mono())}
        )

    @property
    def default_name(self) -> str:
        return DEFAULT_THEME if DEFAULT_THEME in self._themes else sorted(self._themes)[0]

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._themes))

    def get(self, name: str) -> ThemeSpec:
        """Resolve a theme by name, raising :class:`ThemeNotFoundError` otherwise."""

        key = name.strip().lower()
        spec = self._themes.get(key)
        if spec is None:
            raise ThemeNotFoundError(
                f"Theme '{name}' is not registered.",
                hint=f"Available themes: {', '.join(self.names)}.",
            )
        return spec

    def default(self) -> ThemeSpec:
        return self._themes[self.default_name]

    @staticmethod
    def rich_theme(spec: ThemeSpec) -> Theme:
        """Project a theme onto a Rich ``Theme`` usable by any console."""

        return Theme(spec.style_map())
