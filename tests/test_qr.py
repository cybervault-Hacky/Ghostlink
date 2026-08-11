"""Tests for the pure-Python QR code generator, terminal renderer, and decoders."""

from __future__ import annotations

import io
from typing import Any

from rich.console import Console
from rich.text import Text

from ghostlink.ui.themes import ThemeEngine
from ghostlink.utils.qr import (
    _ALIGNMENT_POSITIONS,
    QRCode,
    _bch_format_info,
    _bch_version_info,
    _calculate_mask_penalty,
    _gf_mul,
    _rs_encode_block,
    _rs_generator_poly,
    copy_to_clipboard,
    render_invite_qr_panel,
)

try:
    import zxingcpp
    from PIL import Image

    HAS_DECODER = True
except ImportError:
    HAS_DECODER = False


def _decode_from_ascii_art(rendered_ascii: str) -> str | None:
    """Decode a half-block rendered QR code string using optical ZXing decoder."""
    if not HAS_DECODER:
        return None

    lines = [line for line in rendered_ascii.splitlines() if line]
    matrix: list[list[bool]] = []

    for line in lines:
        top_row: list[bool] = []
        bot_row: list[bool] = []
        for ch in line:
            if ch == "█":
                top_row.append(True)
                bot_row.append(True)
            elif ch == "▀":
                top_row.append(True)
                bot_row.append(False)
            elif ch == "▄":
                top_row.append(False)
                bot_row.append(True)
            elif ch == " ":
                top_row.append(False)
                bot_row.append(False)
            else:
                raise ValueError(f"Unexpected character in half-block QR: {ch!r}")
        matrix.append(top_row)
        matrix.append(bot_row)

    h = len(matrix)
    w = len(matrix[0])
    scale = 8
    img = Image.new("L", (w * scale, h * scale), 255)
    for y in range(h):
        for x in range(w):
            if matrix[y][x]:
                for dy in range(scale):
                    for dx in range(scale):
                        img.putpixel((x * scale + dx, y * scale + dy), 0)

    result = zxingcpp.read_barcode(img)
    return result.text if result else None


def _decode_from_matrix(matrix: list[list[bool]], border: int = 4) -> str | None:
    """Decode directly from 2D boolean matrix with quiet zone."""
    if not HAS_DECODER:
        return None

    h = len(matrix)
    w = len(matrix[0])
    scale = 8
    img_w = (w + 2 * border) * scale
    img_h = (h + 2 * border) * scale
    img = Image.new("L", (img_w, img_h), 255)

    for y in range(h):
        for x in range(w):
            if matrix[y][x]:
                for dy in range(scale):
                    for dx in range(scale):
                        img.putpixel(((x + border) * scale + dx, (y + border) * scale + dy), 0)

    result = zxingcpp.read_barcode(img)
    return result.text if result else None


class TestGaloisFieldAndReedSolomon:
    def test_gf256_multiplication(self) -> None:
        assert _gf_mul(0, 50) == 0
        assert _gf_mul(50, 0) == 0
        assert _gf_mul(1, 123) == 123
        assert _gf_mul(2, 1) == 2
        # Primitive polynomial check
        assert _gf_mul(128, 2) == 0x11D ^ 256  # 285 ^ 256 = 29

    def test_rs_generator_poly_degree_10(self) -> None:
        poly = _rs_generator_poly(10)
        assert len(poly) == 11
        assert poly[0] == 1  # Leading coefficient must be 1

    def test_rs_encode_block_deterministic(self) -> None:
        data = [0x10, 0x20, 0x0C, 0x56, 0x61, 0x80, 0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11]
        ec = _rs_encode_block(data, 10)
        assert len(ec) == 10
        assert all(0 <= b <= 255 for b in ec)


class TestQRCodeStructure:
    def test_qr_matrix_dimensions(self) -> None:
        for version in (1, 2, 3, 4, 5, 10):
            qr = QRCode("test", version=version)
            expected_size = version * 4 + 17
            assert qr.size == expected_size
            assert len(qr.matrix) == expected_size
            assert all(len(row) == expected_size for row in qr.matrix)

    def test_finder_patterns_geometry(self) -> None:
        qr = QRCode("gl://join/KN9DPDPYLYDCKF5GEESS")
        size = qr.size
        matrix = qr.matrix

        # Check all 3 finder patterns (7x7)
        finder_origins = [(0, 0), (0, size - 7), (size - 7, 0)]
        for r_orig, c_orig in finder_origins:
            # Top/bottom edges must be all dark
            assert all(matrix[r_orig][c_orig + dc] for dc in range(7))
            assert all(matrix[r_orig + 6][c_orig + dc] for dc in range(7))
            # Left/right edges must be all dark
            assert all(matrix[r_orig + dr][c_orig] for dr in range(7))
            assert all(matrix[r_orig + dr][c_orig + 6] for dr in range(7))
            # Inner 3x3 box must be all dark
            for dr in range(2, 5):
                for dc in range(2, 5):
                    assert matrix[r_orig + dr][c_orig + dc]
            # Hollow ring (1 and 5)
            for dc in range(1, 6):
                assert not matrix[r_orig + 1][c_orig + dc]
                assert not matrix[r_orig + 5][c_orig + dc]

    def test_timing_patterns(self) -> None:
        qr = QRCode("gl://join/KN9DPDPYLYDCKF5GEESS")
        size = qr.size
        matrix = qr.matrix

        # Row 6 and Column 6 alternating
        for i in range(8, size - 8):
            assert matrix[6][i] == (i % 2 == 0)
            assert matrix[i][6] == (i % 2 == 0)

    def test_dark_module(self) -> None:
        qr = QRCode("gl://join/KN9DPDPYLYDCKF5GEESS")
        assert qr.matrix[qr.size - 8][8] is True

    def test_alignment_patterns_v2_and_above(self) -> None:
        # V2 (25x25) has 1 alignment pattern at (18, 18)
        qr_v2 = QRCode("gl://join/V2TEST", version=2)
        assert _ALIGNMENT_POSITIONS[1] == [6, 18]
        # (18, 18) is the alignment pattern center (skipping corners with finders)
        r, c = 18, 18
        # Outer 5x5 border is dark
        assert all(qr_v2.matrix[r - 2][c + dc] for dc in range(-2, 3))
        assert all(qr_v2.matrix[r + 2][c + dc] for dc in range(-2, 3))
        assert all(qr_v2.matrix[r + dr][c - 2] for dr in range(-2, 3))
        assert all(qr_v2.matrix[r + dr][c + 2] for dr in range(-2, 3))
        # Center is dark
        assert qr_v2.matrix[r][c] is True
        # Middle ring is light
        for dr in (-1, 1):
            for dc in (-1, 0, 1):
                assert not qr_v2.matrix[r + dr][c + dc]

    def test_format_info_bch(self) -> None:
        for ec in ("L", "M", "Q", "H"):
            for mask in range(8):
                bits = _bch_format_info(ec, mask)  # type: ignore[arg-type]
                assert 0 <= bits <= 0x7FFF

    def test_version_info_bch(self) -> None:
        for v in range(7, 41):
            bits = _bch_version_info(v)
            assert 0 <= bits <= 0x3FFFF


class TestQuietZoneAndRendering:
    def test_quiet_zone_four_modules(self) -> None:
        qr = QRCode("gl://join/KN9DPDPYLYDCKF5GEESS")
        padded = qr.render_matrix(border=4)
        size = qr.size
        total = size + 8

        assert len(padded) == total
        assert len(padded[0]) == total

        # Top 4 rows and bottom 4 rows must be entirely light (False)
        for r in range(4):
            assert all(not cell for cell in padded[r])
            assert all(not cell for cell in padded[total - 1 - r])

        # Left 4 cols and right 4 cols of all rows must be light (False)
        for r in range(total):
            for c in range(4):
                assert not padded[r][c]
                assert not padded[r][total - 1 - c]

    def test_ascii_line_lengths_consistent(self) -> None:
        qr = QRCode("gl://join/KN9DPDPYLYDCKF5GEESS")
        lines = qr.render_lines(border=4)
        expected_width = qr.size + 8  # 4 modules on left + 4 on right
        for line_idx, line in enumerate(lines):
            assert len(line) == expected_width, (
                f"Line {line_idx} length {len(line)} != expected {expected_width}"
            )

    def test_ascii_characters_valid_halfblocks(self) -> None:
        qr = QRCode("gl://join/KN9DPDPYLYDCKF5GEESS")
        rendered = qr.render_ascii(border=4)
        valid_chars = {"█", "▀", "▄", " ", "\n"}
        for ch in rendered:
            assert ch in valid_chars, f"Invalid character {ch!r} in ASCII QR"

    def test_ansi_rendering_formatting(self) -> None:
        qr = QRCode("gl://join/KN9DPDPYLYDCKF5GEESS")
        ansi_out = qr.render_ansi(border=4)
        for line in ansi_out.splitlines():
            assert line.startswith("\033[30;47m")
            assert line.endswith("\033[0m")

    def test_rich_text_rendering(self) -> None:
        qr = QRCode("gl://join/KN9DPDPYLYDCKF5GEESS")
        rich_text = qr.render_rich(border=4)
        assert rich_text.no_wrap is True
        assert "bold black on white" in str(rich_text.style)


class TestRoundTripOpticalDecoding:
    def test_short_invite_payload(self) -> None:
        uri = "gl://join/ABC123XYZ"
        qr = QRCode(uri, ec_level="M")
        if HAS_DECODER:
            decoded = _decode_from_ascii_art(qr.render_ascii(border=4))
            assert decoded == uri

    def test_normal_ghostlink_invite_payload(self) -> None:
        uri = "gl://join/KN9DPDPYLYDCKF5GEESS"
        qr = QRCode(uri, ec_level="M")
        assert qr.version == 3  # Standard 30-char invite fits in V3-M (or V2)
        if HAS_DECODER:
            # Direct matrix decode
            assert _decode_from_matrix(qr.matrix, border=4) == uri
            # Terminal half-block decode
            assert _decode_from_ascii_art(qr.render_ascii(border=4)) == uri

    def test_multiple_ghostlink_invite_tokens(self) -> None:
        test_tokens = [
            "gl://join/23456789ABCDEFGHJKMN",
            "gl://join/PQRSVWXYZ23456789ABC",
            "gl://join/HACKYSECUREINVITE1234",
            "gl://join/99999999999999999999",
            "gl://join/AAAAAAAAAAAAAAAAAAAA",
        ]
        for token_uri in test_tokens:
            qr = QRCode(token_uri, ec_level="M")
            if HAS_DECODER:
                decoded = _decode_from_ascii_art(qr.render_ascii(border=4))
                assert decoded == token_uri, f"Failed round trip for {token_uri}"

    def test_all_error_correction_levels(self) -> None:
        uri = "gl://join/KN9DPDPYLYDCKF5GEESS"
        for ec in ("L", "M", "Q", "H"):
            qr = QRCode(uri, ec_level=ec)  # type: ignore[arg-type]
            if HAS_DECODER:
                decoded = _decode_from_ascii_art(qr.render_ascii(border=4))
                assert decoded == uri, f"Failed for EC level {ec}"

    def test_long_payload(self) -> None:
        long_uri = (
            "gl://join/KN9DPDPYLYDCKF5GEESS?"
            "relay=wss://relay.ghostlink.internal:8443/channel"
            "&room_id=gl-room-SECURE-ALPHA-BRAVO-CHARLIE"
            "&session_tag=abcdef0123456789"
        )
        qr = QRCode(long_uri, ec_level="M")
        assert qr.version >= 5
        if HAS_DECODER:
            decoded = _decode_from_ascii_art(qr.render_ascii(border=4))
            assert decoded == long_uri

    def test_unicode_payload(self) -> None:
        unicode_str = "gl://join/ROOM-🔒-SECRET-KEY-1234"
        qr = QRCode(unicode_str, ec_level="M")
        if HAS_DECODER:
            decoded = _decode_from_ascii_art(qr.render_ascii(border=4))
            assert decoded == unicode_str


class TestTermuxCompatibilityAndLayout:
    def test_panel_dimensions_fit_termux(self) -> None:
        """Verify the invite panel fits comfortably in a standard 80x24 Termux terminal."""
        uri = "gl://join/KN9DPDPYLYDCKF5GEESS"
        panel = render_invite_qr_panel(
            uri,
            title="ROOM: TEST-ROOM",
            expires_minutes=10,
        )

        for width, height in [(80, 24), (100, 30), (120, 35)]:
            buf = io.StringIO()
            engine = ThemeEngine()
            rich_theme = ThemeEngine.rich_theme(engine.default())
            console = Console(
                theme=rich_theme,
                file=buf,
                width=width,
                height=height,
                force_terminal=True,
                color_system="standard",
            )
            console.print(panel)
            output = buf.getvalue()

            assert "ROOM: TEST-ROOM" in output
            assert "gl://join/KN9DPDPYLYDCKF5GEESS" in output
            assert "10m" in output

            # Ensure no individual line exceeded terminal width (no line wrapping)
            lines = output.splitlines()
            assert len(lines) <= height, (
                f"Panel height {len(lines)} exceeds terminal height {height}"
            )
            for line in lines:
                stripped = Text.from_ansi(line).plain
                assert len(stripped) <= width, f"Line width {len(stripped)} exceeds {width}"

    def test_copy_to_clipboard_safe(self, monkeypatch: Any) -> None:
        # Verify copy_to_clipboard runs without raising exceptions
        assert isinstance(copy_to_clipboard("gl://join/KN9DPDPYLYDCKF5GEESS"), bool)

    def test_clipboard_termux_tool_invoked(self, monkeypatch: Any) -> None:
        # Test termux-clipboard-set subprocess invocation
        called: list[str] = []

        def mock_which(cmd: str) -> str | None:
            if cmd == "termux-clipboard-set":
                return "/data/data/com.termux/files/usr/bin/termux-clipboard-set"
            return None

        def mock_run(args: list[str], **kwargs: Any) -> Any:
            called.append(args[0])
            return None

        import shutil
        import subprocess

        monkeypatch.setattr(shutil, "which", mock_which)
        monkeypatch.setattr(subprocess, "run", mock_run)

        res = copy_to_clipboard("gl://join/TESTTOKEN1234567890")
        assert res is True
        assert "termux-clipboard-set" in called


class TestMaskPenalty:
    def test_mask_penalty_calculation(self) -> None:
        matrix = [[False] * 21 for _ in range(21)]
        pen = _calculate_mask_penalty(matrix, 21)
        assert pen > 0  # Solid white has high N1 and N4 penalty
