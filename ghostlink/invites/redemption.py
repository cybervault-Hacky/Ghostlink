"""Invite redemption flow — the joiner's side (Phase 5).

:func:`redeem_invite` validates the link locally (scheme, token format),
then asks the relay authority to consume it. The authority's verdict is
mapped onto typed exceptions so the CLI can render exactly the right
screen (expired / already used / revoked / unknown) and exit code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from ghostlink.core.logging import get_logger
from ghostlink.exceptions.invites import (
    InviteAlreadyUsedError,
    InviteExpiredError,
    InviteRevokedError,
    InviteUnknownError,
)
from ghostlink.invites.tokens import invite_id_for_token, parse_invite_link
from ghostlink.transport.relay.endpoint import RelayEndpoint

if TYPE_CHECKING:
    from ghostlink.transport.relay.client import RelayClientConfig

_logger = get_logger("invites.redemption")

_JOINER_CLIENT_NAME: str = "ghostlink-joiner"


@dataclass(frozen=True, slots=True)
class RedemptionOutcome:
    """A successful redemption: the room to join and the public invite id."""

    invite_id: str
    room_id: str
    expires_at: datetime
    token: str  # kept in memory for status queries; never persisted/logged


async def redeem_invite(
    link: str,
    *,
    relay_url: str,
    client_config: RelayClientConfig | None = None,
) -> RedemptionOutcome:
    """Redeem ``gl://join/<token>`` against the relay authority.

    Local validation runs first (no network on a malformed link); the
    authority's atomic transition decides acceptance.
    """

    # Deferred import: this package is also pulled in by the relay
    # client/server (the authority), so a module-level dependency on the
    # client would close an import cycle.
    from ghostlink.transport.relay.client import RelayClient

    token = parse_invite_link(link)  # raises InviteValidationError locally
    invite_id = invite_id_for_token(token)
    _logger.info("redeeming invite %s via %s", invite_id, relay_url)

    endpoint = RelayEndpoint.from_url(relay_url)
    client = RelayClient(endpoint, client_name=_JOINER_CLIENT_NAME, config=client_config)
    try:
        await client.connect()
        room_id, expires_at = await client.redeem_invite(token)
    except InviteExpiredError:
        _logger.info("invite %s was already expired", invite_id)
        raise InviteExpiredError(
            "This invite has expired.",
            hint="Ask your peer for a fresh invite — they can tune --expires.",
        ) from None
    except InviteAlreadyUsedError:
        _logger.info("invite %s was already used", invite_id)
        raise InviteAlreadyUsedError(
            "This invite has already been used.",
            hint="One-time invites are consumed by the first peer who joins.",
        ) from None
    except InviteRevokedError:
        _logger.info("invite %s was revoked", invite_id)
        raise InviteRevokedError(
            "This invite was revoked by its creator.",
            hint="Ask your peer to issue a new invite.",
        ) from None
    except InviteUnknownError:
        _logger.info("invite %s is unknown to the relay", invite_id)
        raise
    finally:
        # The probe connection is always closed; the chat session dials anew.
        try:
            await client.disconnect(reason="redemption complete")
        except Exception as exc:  # never mask the redemption outcome
            _logger.debug("redemption probe disconnect failed: %s", exc)
    _logger.info("invite %s redeemed for %s", invite_id, room_id)
    return RedemptionOutcome(
        invite_id=invite_id, room_id=room_id, expires_at=expires_at, token=token
    )
