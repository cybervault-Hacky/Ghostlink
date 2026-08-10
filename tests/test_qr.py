"""Tests for the pure-Python QR code generator and renderer."""

from __future__ import annotations

from ghostlink.utils.qr import QRCode, render_invite_qr_panel


class TestQRCodeGeneration:
    def test_qr_matrix_generation_simple(self) -> None:
        qr = QRCode("gl://join/gl-room-ABCD-EFGH-1234")
        assert qr.version >= 1
        assert len(qr.matrix) >= 21
        assert len(qr.matrix[0]) == len(qr.matrix)

    def test_qr_ascii_rendering(self) -> None:
        qr = QRCode("gl://join/gl-room-ABCD-EFGH-1234")
        rendered = qr.render_ascii()
        assert len(rendered.splitlines()) >= 10
        # Check standard half-block characters
        assert any(char in rendered for char in ("█", "▀", "▄", " "))

    def test_invite_qr_panel_rendering(self) -> None:
        panel = render_invite_qr_panel(
            "gl://join/gl-room-TEST-1234",
            title="Test Invite QR",
        )
        assert panel.title is not None
        assert "Test Invite QR" in str(panel.title)
