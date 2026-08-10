"""Pure-Python QR code generator and terminal renderer.

Generates standard QR code matrix representations and renders them using
half-block Unicode characters for high-density terminal rendering without
requiring external binary C-libraries on Termux or desktop Linux.
"""

from __future__ import annotations

from typing import Final

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.text import Text

# QR Code specification tables for Version 1 through 10
_GF256_EXP: Final[list[int]] = [0] * 512
_GF256_LOG: Final[list[int]] = [0] * 256

# Initialize Galois Field 2^8 tables for Reed-Solomon polynomial math
_x = 1
for _i in range(255):
    _GF256_EXP[_i] = _x
    _GF256_LOG[_x] = _i
    _x <<= 1
    if _x & 0x100:
        _x ^= 0x11D
for _i in range(255, 512):
    _GF256_EXP[_i] = _GF256_EXP[_i - 255]


def _gf_mul(x: int, y: int) -> int:
    if x == 0 or y == 0:
        return 0
    return _GF256_EXP[_GF256_LOG[x] + _GF256_LOG[y]]


def _rs_generator_poly(degree: int) -> list[int]:
    """Generate Reed-Solomon error correction polynomial."""

    poly = [1]
    for i in range(degree):
        root = _GF256_EXP[i]
        new_poly = [0] * (len(poly) + 1)
        for j in range(len(poly)):
            new_poly[j] ^= _gf_mul(poly[j], root)
            new_poly[j + 1] ^= poly[j]
        poly = new_poly
    return poly


def _rs_encode(data: list[int], num_ec: int) -> list[int]:
    """Compute Reed-Solomon error correction bytes."""

    gen = _rs_generator_poly(num_ec)
    padded = data + [0] * num_ec
    for i in range(len(data)):
        lead = padded[i]
        if lead != 0:
            for j in range(len(gen)):
                padded[i + j] ^= _gf_mul(gen[j], lead)
    return padded[len(data) :]


# Total capacity and error correction count for QR version 1-4 Medium (M)
_QR_CAPACITY_M: Final[dict[int, tuple[int, int, int]]] = {
    # Version: (data_bytes, ec_bytes, total_modules_per_side)
    1: (16, 10, 21),
    2: (28, 16, 25),
    3: (44, 26, 29),
    4: (64, 36, 33),
    5: (86, 48, 37),
    6: (108, 64, 41),
}


class QRCode:
    """Pure-Python QR code matrix generator."""

    def __init__(self, data: str) -> None:
        self.data = data
        self.version, self.matrix = self._generate(data)

    def _generate(self, text: str) -> tuple[int, list[list[bool]]]:
        raw_bytes = text.encode("utf-8")
        byte_len = len(raw_bytes)

        # Select best QR version
        chosen_version = 1
        for version, (capacity, _ec_len, _size) in _QR_CAPACITY_M.items():
            if byte_len + 3 <= capacity:
                chosen_version = version
                break
        else:
            chosen_version = 6

        capacity, ec_len, size = _QR_CAPACITY_M[chosen_version]

        # Byte mode header: Mode indicator 0100 (4 bits) + Character count indicator (8 bits)
        bit_stream: list[int] = [0, 1, 0, 0]
        char_count_bits = 8 if chosen_version < 10 else 16
        for i in range(char_count_bits - 1, -1, -1):
            bit_stream.append((byte_len >> i) & 1)

        for b in raw_bytes:
            for i in range(7, -1, -1):
                bit_stream.append((b >> i) & 1)

        # Terminator up to 4 zero bits
        needed_pad = min(4, capacity * 8 - len(bit_stream))
        bit_stream.extend([0] * needed_pad)

        # Pad to multiple of 8
        while len(bit_stream) % 8 != 0:
            bit_stream.append(0)

        # Convert to bytes
        data_codewords: list[int] = []
        for i in range(0, len(bit_stream), 8):
            codeword = 0
            for j in range(8):
                codeword = (codeword << 1) | bit_stream[i + j]
            data_codewords.append(codeword)

        # Pad codewords
        pad_bytes = [0xEC, 0x11]
        pad_idx = 0
        while len(data_codewords) < capacity:
            data_codewords.append(pad_bytes[pad_idx])
            pad_idx = (pad_idx + 1) % 2

        # Error correction codewords
        ec_codewords = _rs_encode(data_codewords, ec_len)
        final_stream = data_codewords + ec_codewords

        # Build matrix
        matrix = [[False] * size for _ in range(size)]
        reserved = [[False] * size for _ in range(size)]

        def add_finder(r: int, c: int) -> None:
            for dr in range(7):
                for dc in range(7):
                    if dr in (0, 6) or dc in (0, 6) or (2 <= dr <= 4 and 2 <= dc <= 4):
                        matrix[r + dr][c + dc] = True
                    reserved[r + dr][c + dc] = True
            # Quiet separator
            for dr in range(-1, 8):
                for dc in range(-1, 8):
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < size and 0 <= cc < size:
                        reserved[rr][cc] = True

        add_finder(0, 0)
        add_finder(0, size - 7)
        add_finder(size - 7, 0)

        # Timing patterns
        for i in range(8, size - 8):
            matrix[6][i] = i % 2 == 0
            reserved[6][i] = True
            matrix[i][6] = i % 2 == 0
            reserved[i][6] = True

        # Dark module
        matrix[size - 8][8] = True
        reserved[size - 8][8] = True

        # Placement of data bits
        bit_idx = 0
        total_bits: list[int] = []
        for cw in final_stream:
            for b in range(7, -1, -1):
                total_bits.append((cw >> b) & 1)

        row = size - 1
        col = size - 1
        dir_up = True

        while col > 0:
            if col == 6:
                col -= 1
            for _ in range(size):
                for dc in (0, -1):
                    cc = col + dc
                    if 0 <= row < size and 0 <= cc < size and not reserved[row][cc]:
                        bit_val = total_bits[bit_idx] if bit_idx < len(total_bits) else 0
                        bit_idx += 1
                        # Mask 0: (row + col) % 2 == 0
                        mask = (row + cc) % 2 == 0
                        matrix[row][cc] = bool(bit_val ^ (1 if mask else 0))
                row += -1 if dir_up else 1
            dir_up = not dir_up
            row = 0 if dir_up else size - 1
            col -= 2

        return chosen_version, matrix

    def render_ascii(self) -> str:
        """Render QR code using standard Unicode half-block characters."""

        size = len(self.matrix)
        border = 2
        # Pad matrix with quiet border
        padded: list[list[bool]] = []
        for _ in range(border):
            padded.append([False] * (size + 2 * border))
        for row in self.matrix:
            padded.append([False] * border + list(row) + [False] * border)
        for _ in range(border):
            padded.append([False] * (size + 2 * border))

        total_rows = len(padded)
        total_cols = len(padded[0])
        lines: list[str] = []

        # 2 vertical modules per character using upper/lower block
        for r in range(0, total_rows, 2):
            line = []
            for c in range(total_cols):
                top = padded[r][c]
                bottom = padded[r + 1][c] if r + 1 < total_rows else False
                if top and bottom:
                    line.append("█")
                elif top and not bottom:
                    line.append("▀")
                elif not top and bottom:
                    line.append("▄")
                else:
                    line.append(" ")
            lines.append("".join(line))
        return "\n".join(lines)


def render_invite_qr_panel(
    invite_url: str,
    *,
    title: str = "GhostLink Invite QR",
    theme_accent: str = "gl.accent",
) -> Panel:
    """Render a clean QR code panel for sharing a GhostLink invite link."""

    qr = QRCode(invite_url)
    qr_text = Text(qr.render_ascii(), style="bold white on black")
    hint = Text(
        "Scan with any camera or terminal scanner · Never share private keys",
        style="gl.muted",
        justify="center",
    )
    url_line = Text(f"Link: {invite_url}", style="gl.accent", justify="center")

    body = Group(
        Align.center(qr_text),
        Text(""),
        Align.center(url_line),
        Text(""),
        Align.center(hint),
    )

    return Panel(
        body,
        title=f"[{theme_accent}]{title}[/]",
        border_style="gl.accent",
        padding=(1, 2),
        expand=False,
    )
