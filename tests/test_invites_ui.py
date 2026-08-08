"""Invite & verification terminal screens: content and narrow-termux safety."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ghostlink.invites.formatter import (
    invite_created_panel,
    invite_expired_panel,
    invite_info_panel,
    invite_refused_panel,
    invite_waiting_line,
    invites_table,
    verification_panel,
)
from ghostlink.invites.models import InviteRecord, InviteState
from ghostlink.invites.tokens import (
    format_invite_link,
    generate_invite_token,
    invite_id_for_token,
    token_hash_for,
)
from ghostlink.models.room import generate_room_id

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def _record(state: InviteState = InviteState.ACTIVE, **overrides: object) -> InviteRecord:
    token = generate_invite_token()
    base: dict[str, object] = {
        "invite_id": invite_id_for_token(token),
        "room_id": generate_room_id(),
        "token_hash": token_hash_for(token),
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=15),
        "max_redemptions": 1,
        "state": state,
    }
    base.update(overrides)
    return InviteRecord(**base)  # type: ignore[arg-type]


def _render(renderable: object, width: int = 100) -> str:
    from ghostlink.ui.console import ConsoleManager
    from ghostlink.ui.themes import ThemeEngine

    manager = ConsoleManager(ThemeEngine().get("phantom"), no_color=True, record=True, width=width)
    manager.console.print(renderable)  # type: ignore[arg-type]
    text = manager.console.export_text(clear=False)
    return text


class TestInviteCreatedPanel:
    def test_contains_the_brief_fields(self) -> None:
        link = format_invite_link(generate_invite_token())
        text = _render(
            invite_created_panel(
                link=link,
                expires_seconds=2,
                max_redemptions=1,
                status=InviteState.ACTIVE,
                console_width=80,
            ),
            width=80,
        )
        assert "SECURE GHOSTLINK INVITE" in text
        assert "Invite:" in text and link in text
        assert "Expires:" in text and "2 seconds" in text
        assert "Uses:" in text and "1" in text
        assert "Status:" in text and "ACTIVE" in text

    def test_fits_narrow_termux_widths(self) -> None:
        link = format_invite_link(generate_invite_token())
        for width in (40, 46, 60):
            text = _render(
                invite_created_panel(
                    link=link,
                    expires_seconds=900,
                    max_redemptions=1,
                    status=InviteState.ACTIVE,
                    console_width=width,
                ),
                width=width,
            )
            longest = max(len(line) for line in text.splitlines())
            assert longest <= width, f"panel overflowed width {width}: {longest}"

    def test_countdown_line(self) -> None:
        text = _render(invite_waiting_line(4.0)).strip()
        assert "Waiting for peer" in text
        assert "00:04" in text


class TestRefusalPanels:
    def test_expired_panel_matches_the_brief(self) -> None:
        text = _render(invite_expired_panel(80))
        assert "INVITE EXPIRED" in text
        assert "This invitation is no longer" in text
        assert "valid." in text

    def test_refused_panel_carries_reason(self) -> None:
        text = _render(
            invite_refused_panel(
                reason="Invite already used",
                detail="This one-time invite was already consumed.",
                console_width=80,
            )
        )
        assert "INVITE ALREADY USED" in text
        assert "already" in text and "consumed." in text  # wraps on the panel width


class TestListAndInfo:
    def test_invites_table_columns_and_states(self) -> None:
        records = [
            _record(InviteState.ACTIVE),
            _record(InviteState.REVOKED),
            _record(InviteState.REDEEMED, redemptions=1),
        ]
        text = _render(invites_table(records, now=NOW + timedelta(minutes=5)), width=100)
        assert "Invite ID" in text
        assert "Status" in text
        assert "Uses" in text
        assert "ACTIVE" in text and "REVOKED" in text and "REDEEMED" in text
        for record in records:
            assert record.invite_id in text

    def test_active_rows_show_countdown_inactive_show_dash(self) -> None:
        records = [_record(InviteState.ACTIVE), _record(InviteState.REVOKED)]
        text = _render(invites_table(records, now=NOW + timedelta(minutes=5)), width=100)
        assert "10:00" in text  # 15m - 5m remaining
        assert "—" in text

    def test_info_panel_hides_the_token(self) -> None:
        token = generate_invite_token()
        record = _record(token_hash=token_hash_for(token))
        text = _render(invite_info_panel(record, console_width=90), width=90)
        assert record.invite_id in text
        assert token not in text
        assert "Room" in text
        assert "Protocol" in text
        assert "secret token" in text  # subtitle truncates gracefully on narrow panels

    def test_lists_fit_narrow_widths(self) -> None:
        records = [_record(), _record(InviteState.EXPIRED)]
        _render(invites_table(records), width=50)  # must not raise


class TestVerificationPanel:
    def _panel(self, width: int = 90) -> str:
        return _render(
            verification_panel(
                peer_name="ShadowUser",
                peer_fingerprint="GLFP-7A92-31CF-88B4",
                own_fingerprint="GLFP-1122-AABB-CCDD",
                own_name="Nova (GL-7K3M)",
                safety_code="222E 630F 3245 7A45",
                identity_bound=True,
                console_width=width,
            ),
            width=width,
        )

    def test_fields_present(self) -> None:
        text = self._panel()
        assert "PEER VERIFICATION" in text
        assert "ShadowUser" in text
        assert "GLFP-7A92-31CF-88B4" in text
        assert "GLFP-1122-AABB-CCDD" in text
        assert "out-of-band" in text

    def test_honest_limitations_stated(self) -> None:
        text = self._panel()
        assert "does" in text and "anonymity" in text

    def test_narrow_widths(self) -> None:
        for width in (44, 56):
            text = self._panel(width)
            longest = max(len(line) for line in text.splitlines())
            assert longest <= width
