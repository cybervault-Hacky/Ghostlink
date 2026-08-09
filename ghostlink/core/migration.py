"""State & configuration schema versioning (Phase 9).

GhostLink persists configuration (``config.toml``) and state documents
(``<namespace>.json`` under the state directory). Every document is written
with an explicit schema version so that a future GhostLink that changes the
shape of a document can detect it and migrate — and, critically, so that a
*newer* document is never silently reinterpreted by an older build.

Migration rules (docs/ROADMAP.md Phase 9):

1. Never destroy user data silently.
2. Never silently reinterpret cryptographic state.
3. Never migrate private cryptographic material into a weaker format.
4. Create atomic backups before destructive migrations where necessary.
5. Migration must be idempotent.
6. A failed migration must leave recoverable state.
7. Migration status is reported clearly.

Because GhostLink has shipped only schema version 1 for config and state,
there is nothing to migrate yet — the module exists to make the contract
explicit and to **fail closed** (reject, not guess) if a future document
with an unsupported version is encountered. If cryptographic state is ever
incompatible, GhostLink fails closed rather than inventing conversion
logic for secrets.
"""

from __future__ import annotations

from ghostlink.exceptions.config import ConfigValidationError

# Bump these only when the on-disk shape of the document changes, and write a
# forward-migration path before bumping. Versions are 1-based; a document
# without a version field is treated as version 1 (backward compatible).
CONFIG_SCHEMA_VERSION: int = 1
STATE_SCHEMA_VERSION: int = 1

# Human-readable labels for error messages.
_CONFIG_LABEL = "configuration"
_STATE_LABEL = "state"


def validate_config_version(raw_version: object) -> None:
    """Fail closed if the config declares an unsupported schema version.

    ``None`` (no ``config_version`` key) means version 1 — accepted. A
    numeric version ≤ ``CONFIG_SCHEMA_VERSION`` is accepted. A *future*
    version is rejected with a clear upgrade hint instead of being
    misinterpreted.
    """
    _validate_version(raw_version, _CONFIG_LABEL, CONFIG_SCHEMA_VERSION)


def validate_state_version(raw_version: object) -> None:
    """Fail closed if a state document declares an unsupported version.

    Raises ``ConfigValidationError`` to match the convention used by the
    state-record parsers (e.g. ``LocalGroupRecord.from_dict``), which surface
    every corrupt-document problem as a typed config/parse error.
    """
    _validate_version(raw_version, _STATE_LABEL, STATE_SCHEMA_VERSION)


def _validate_version(raw_version: object, kind: str, supported: int) -> None:
    if raw_version is None:
        return  # absent = version 1 (backward compatible)
    if isinstance(raw_version, bool) or not isinstance(raw_version, int):
        raise ConfigValidationError(
            f"A stored {kind} document declares a non-integer schema version.",
            hint="The document is corrupt or was written by an unsupported tool.",
        )
    if raw_version > supported:
        raise ConfigValidationError(
            f"The stored {kind} was written by a newer GhostLink "
            f"(schema v{raw_version} > supported v{supported}).",
            hint="Upgrade GhostLink before reading this data — refusing to "
            "reinterpret it silently.",
        )
