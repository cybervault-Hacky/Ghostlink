"""Group identifiers (Phase 6B): ``gl-group-XXXX-XXXX-XXXX``.

Same alphabet and shape discipline as room ids, with a distinct prefix so
groups can never be confused with one-to-one ``gl-room-…`` channels. The id
is public routing metadata — it contains no secret material of any kind.
"""

from __future__ import annotations

import re
import secrets

from ghostlink.constants.net import GROUP_ID_PREFIX, ROOM_ID_ALPHABET

_GROUP_ID_PATTERN: re.Pattern[str] = re.compile(
    rf"^{GROUP_ID_PREFIX}-(?:[{ROOM_ID_ALPHABET}]{{4}}-){{2}}[{ROOM_ID_ALPHABET}]{{4}}$"
)


def generate_group_id() -> str:
    """Create a canonical group identifier: ``gl-group-XXXX-XXXX-XXXX``.

    ~60 bits of id space from a CSPRNG. Callers that keep a registry (the
    relay authority) must still check for collisions and regenerate — the
    registry check, not probability, is what guarantees uniqueness.
    """

    groups = ("".join(secrets.choice(ROOM_ID_ALPHABET) for _ in range(4)) for _ in range(3))
    return f"{GROUP_ID_PREFIX}-" + "-".join(groups)


def normalize_group_id(candidate: str) -> str:
    """Return ``candidate`` in canonical form: lowercase prefix, upper groups."""

    cleaned = candidate.strip().upper().replace(" ", "")
    marker = f"{GROUP_ID_PREFIX.upper()}-"
    if cleaned.startswith(marker):
        cleaned = cleaned[len(marker) :]
    return f"{GROUP_ID_PREFIX}-{cleaned}"


def is_valid_group_id(candidate: str) -> bool:
    """Strict format validation against the canonical group identifier form."""

    return bool(_GROUP_ID_PATTERN.fullmatch(candidate))
