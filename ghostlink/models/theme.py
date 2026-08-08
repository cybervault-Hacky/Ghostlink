"""Theme specification model.

A :class:`ThemeSpec` is a palette contract: every theme must define the same
semantic tokens so UI components stay theme-agnostic. The
:meth:`ThemeSpec.style_map` projects the tokens onto ``gl.*`` style names that
are registered with Rich's theming system.
"""

from __future__ import annotations

from dataclasses import dataclass

STYLE_PREFIX = "gl"


@dataclass(frozen=True, slots=True)
class ThemeSpec:
    """A complete, immutable color theme."""

    name: str
    description: str
    primary: str
    accent: str
    success: str
    warning: str
    error: str
    info: str
    muted: str
    text: str
    border: str
    highlight: str
    gradient: tuple[str, str]
    on_accent: str

    def style_map(self) -> dict[str, str]:
        """Project palette tokens onto Rich style names (``gl.*``)."""

        return {
            f"{STYLE_PREFIX}.primary": f"bold {self.primary}",
            f"{STYLE_PREFIX}.accent": self.accent,
            f"{STYLE_PREFIX}.success": self.success,
            f"{STYLE_PREFIX}.warning": self.warning,
            f"{STYLE_PREFIX}.error": self.error,
            f"{STYLE_PREFIX}.info": self.info,
            f"{STYLE_PREFIX}.muted": self.muted,
            f"{STYLE_PREFIX}.text": self.text,
            f"{STYLE_PREFIX}.border": self.border,
            f"{STYLE_PREFIX}.highlight": f"bold {self.highlight}",
            f"{STYLE_PREFIX}.title": f"bold {self.text}",
        }
