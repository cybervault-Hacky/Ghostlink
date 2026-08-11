"""Standards-compliant pure-Python QR Code generator and terminal renderer.

Generates ISO/IEC 18004 compliant QR Code symbols (Versions 1-40, Error
Correction levels L, M, Q, H) and renders them cleanly to terminals using
Unicode half-block characters (▀, ▄, █, space) with full quiet zones, high
contrast, and exact square module geometry.

Zero external binary dependencies — fully compatible with Android/Termux,
Linux, macOS, and standard monospace terminal fonts.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
from typing import Final, Literal

from rich.align import Align
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.text import Text

# Type alias for error correction levels
QRECLevel = Literal["L", "M", "Q", "H"]

# ISO/IEC 18004 Galois Field 2^8 tables (primitive polynomial x^8 + x^4 + x^3 + x^2 + 1 = 0x11D)
_GF_EXP: Final[list[int]] = [0] * 512
_GF_LOG: Final[list[int]] = [0] * 256

_val = 1
for _i in range(255):
    _GF_EXP[_i] = _val
    _GF_LOG[_val] = _i
    _val <<= 1
    if _val & 0x100:
        _val ^= 0x11D
for _i in range(255, 512):
    _GF_EXP[_i] = _GF_EXP[_i - 255]


def _gf_mul(x: int, y: int) -> int:
    """Multiply two numbers in GF(2^8)."""
    if x == 0 or y == 0:
        return 0
    return _GF_EXP[_GF_LOG[x] + _GF_LOG[y]]


def _rs_generator_poly(degree: int) -> list[int]:
    """Compute Reed-Solomon generator polynomial for a given EC degree."""
    poly = [1]
    for i in range(degree):
        root = _GF_EXP[i]
        new_poly = [0] * (len(poly) + 1)
        for j in range(len(poly)):
            new_poly[j] ^= poly[j]
            new_poly[j + 1] ^= _gf_mul(poly[j], root)
        poly = new_poly
    return poly


def _rs_encode_block(data: list[int], num_ec: int) -> list[int]:
    """Compute Reed-Solomon error correction codewords for a data block."""
    gen = _rs_generator_poly(num_ec)
    rem = [0] * num_ec
    for b in data:
        factor = b ^ rem[0]
        rem = [*rem[1:], 0]
        if factor != 0:
            for j in range(num_ec):
                rem[j] ^= _gf_mul(gen[j + 1], factor)
    return rem


# Alignment pattern center coordinates per version (ISO/IEC 18004 Table E.1)
_ALIGNMENT_POSITIONS: Final[list[list[int]]] = [
    [],  # V1
    [6, 18],  # V2
    [6, 22],  # V3
    [6, 26],  # V4
    [6, 30],  # V5
    [6, 34],  # V6
    [6, 22, 38],  # V7
    [6, 24, 42],  # V8
    [6, 26, 46],  # V9
    [6, 28, 50],  # V10
    [6, 30, 54],  # V11
    [6, 32, 58],  # V12
    [6, 34, 62],  # V13
    [6, 26, 46, 66],  # V14
    [6, 26, 48, 70],  # V15
    [6, 26, 50, 74],  # V16
    [6, 30, 54, 78],  # V17
    [6, 30, 56, 82],  # V18
    [6, 30, 58, 86],  # V19
    [6, 34, 62, 90],  # V20
    [6, 28, 50, 72, 94],  # V21
    [6, 26, 50, 74, 98],  # V22
    [6, 30, 54, 78, 102],  # V23
    [6, 28, 54, 80, 106],  # V24
    [6, 32, 58, 84, 110],  # V25
    [6, 30, 58, 86, 114],  # V26
    [6, 34, 62, 90, 118],  # V27
    [6, 26, 50, 74, 98, 122],  # V28
    [6, 30, 54, 78, 102, 126],  # V29
    [6, 26, 52, 78, 104, 130],  # V30
    [6, 30, 56, 82, 108, 134],  # V31
    [6, 34, 60, 86, 112, 138],  # V32
    [6, 30, 58, 86, 114, 142],  # V33
    [6, 34, 62, 90, 118, 146],  # V34
    [6, 30, 54, 78, 102, 126, 150],  # V35
    [6, 24, 50, 76, 102, 128, 154],  # V36
    [6, 28, 54, 80, 106, 132, 158],  # V37
    [6, 32, 58, 84, 110, 136, 162],  # V38
    [6, 26, 54, 82, 110, 138, 166],  # V39
    [6, 30, 58, 86, 114, 142, 170],  # V40
]

# ISO/IEC 18004 Error Correction Block Table (Versions 1-40, L/M/Q/H)
# Tuple format per entry: (block_count, total_codewords, data_codewords, ...)
_RS_BLOCK_TABLE: Final[tuple[tuple[int, ...], ...]] = (
    (1, 26, 19),
    (1, 26, 16),
    (1, 26, 13),
    (1, 26, 9),  # V1
    (1, 44, 34),
    (1, 44, 28),
    (1, 44, 22),
    (1, 44, 16),  # V2
    (1, 70, 55),
    (1, 70, 44),
    (2, 35, 17),
    (2, 35, 13),  # V3
    (1, 100, 80),
    (2, 50, 32),
    (2, 50, 24),
    (4, 25, 9),  # V4
    (1, 134, 108),
    (2, 67, 43),
    (2, 33, 15, 2, 34, 16),
    (2, 33, 11, 2, 34, 12),  # V5
    (2, 86, 68),
    (4, 43, 27),
    (4, 43, 19),
    (4, 43, 15),  # V6
    (2, 98, 78),
    (4, 49, 31),
    (2, 32, 14, 4, 33, 15),
    (4, 39, 13, 1, 40, 14),  # V7
    (2, 121, 97),
    (2, 60, 38, 2, 61, 39),
    (4, 40, 18, 2, 41, 19),
    (4, 40, 14, 2, 41, 15),  # V8
    (2, 146, 116),
    (3, 58, 36, 2, 59, 37),
    (4, 36, 16, 4, 37, 17),
    (4, 36, 12, 4, 37, 13),  # V9
    (2, 86, 68, 2, 87, 69),
    (4, 69, 43, 1, 70, 44),
    (6, 43, 19, 2, 44, 20),
    (6, 43, 15, 2, 44, 16),  # V10
    (4, 101, 81),
    (1, 80, 50, 4, 81, 51),
    (4, 50, 22, 4, 51, 23),
    (3, 36, 12, 8, 37, 13),  # V11
    (2, 116, 92, 2, 117, 93),
    (6, 58, 36, 2, 59, 37),
    (4, 46, 20, 6, 47, 21),
    (7, 42, 14, 4, 43, 15),  # V12
    (4, 133, 107),
    (8, 59, 37, 1, 60, 38),
    (8, 44, 20, 4, 45, 21),
    (12, 33, 11, 4, 34, 12),  # V13
    (3, 145, 115, 1, 146, 116),
    (4, 64, 40, 5, 65, 41),
    (11, 36, 16, 5, 37, 17),
    (11, 36, 12, 5, 37, 13),  # V14
    (5, 109, 87, 1, 110, 88),
    (5, 65, 41, 5, 66, 42),
    (5, 54, 24, 7, 55, 25),
    (11, 36, 12, 7, 37, 13),  # V15
    (5, 122, 98, 1, 123, 99),
    (7, 73, 45, 3, 74, 46),
    (15, 43, 19, 2, 44, 20),
    (3, 45, 15, 13, 46, 16),  # V16
    (1, 135, 107, 5, 136, 108),
    (10, 74, 46, 1, 75, 47),
    (1, 50, 22, 15, 51, 23),
    (2, 42, 14, 17, 43, 15),  # V17
    (5, 150, 120, 1, 151, 121),
    (9, 69, 43, 4, 70, 44),
    (17, 50, 22, 1, 51, 23),
    (2, 42, 14, 19, 43, 15),  # V18
    (3, 141, 113, 4, 142, 114),
    (3, 70, 44, 11, 71, 45),
    (17, 47, 21, 4, 48, 22),
    (9, 39, 13, 16, 40, 14),  # V19
    (3, 135, 107, 5, 136, 108),
    (3, 67, 41, 13, 68, 42),
    (15, 54, 24, 5, 55, 25),
    (15, 43, 15, 10, 44, 16),  # V20
    (4, 144, 116, 4, 145, 117),
    (17, 68, 42),
    (17, 50, 22, 6, 51, 23),
    (19, 46, 16, 6, 47, 17),  # V21
    (2, 139, 111, 7, 140, 112),
    (17, 74, 46),
    (7, 54, 24, 16, 55, 25),
    (34, 37, 13),  # V22
    (4, 151, 121, 5, 152, 122),
    (4, 75, 47, 14, 76, 48),
    (11, 54, 24, 14, 55, 25),
    (16, 45, 15, 14, 46, 16),  # V23
    (6, 147, 117, 4, 148, 118),
    (6, 73, 45, 14, 74, 46),
    (11, 54, 24, 16, 55, 25),
    (30, 46, 16, 2, 47, 17),  # V24
    (8, 132, 106, 4, 133, 107),
    (8, 75, 47, 13, 76, 48),
    (7, 54, 24, 22, 55, 25),
    (22, 45, 15, 13, 46, 16),  # V25
    (10, 142, 114, 2, 143, 115),
    (19, 74, 46, 4, 75, 47),
    (28, 50, 22, 6, 51, 23),
    (33, 46, 16, 4, 47, 17),  # V26
    (8, 152, 122, 4, 153, 123),
    (22, 73, 45, 3, 74, 46),
    (8, 53, 23, 26, 54, 24),
    (12, 45, 15, 28, 46, 16),  # V27
    (3, 147, 117, 10, 148, 118),
    (3, 73, 45, 23, 74, 46),
    (4, 54, 24, 31, 55, 25),
    (11, 45, 15, 31, 46, 16),  # V28
    (7, 146, 116, 7, 147, 117),
    (21, 73, 45, 7, 74, 46),
    (1, 53, 23, 37, 54, 24),
    (19, 45, 15, 26, 46, 16),  # V29
    (5, 145, 115, 10, 146, 116),
    (19, 75, 47, 10, 76, 48),
    (15, 54, 24, 25, 55, 25),
    (23, 45, 15, 25, 46, 16),  # V30
    (13, 145, 115, 3, 146, 116),
    (2, 74, 46, 29, 75, 47),
    (42, 54, 24, 1, 55, 25),
    (23, 45, 15, 28, 46, 16),  # V31
    (17, 145, 115),
    (10, 74, 46, 23, 75, 47),
    (10, 54, 24, 35, 55, 25),
    (19, 45, 15, 35, 46, 16),  # V32
    (17, 145, 115, 1, 146, 116),
    (14, 74, 46, 21, 75, 47),
    (29, 54, 24, 19, 55, 25),
    (11, 45, 15, 46, 46, 16),  # V33
    (13, 145, 115, 6, 146, 116),
    (14, 74, 46, 23, 75, 47),
    (44, 54, 24, 7, 55, 25),
    (59, 46, 16, 1, 47, 17),  # V34
    (12, 151, 121, 7, 152, 122),
    (12, 75, 47, 26, 76, 48),
    (39, 54, 24, 14, 55, 25),
    (22, 45, 15, 41, 46, 16),  # V35
    (6, 151, 121, 14, 152, 122),
    (6, 75, 47, 34, 76, 48),
    (46, 54, 24, 10, 55, 25),
    (2, 45, 15, 64, 46, 16),  # V36
    (17, 152, 122, 4, 153, 123),
    (29, 74, 46, 14, 75, 47),
    (49, 54, 24, 10, 55, 25),
    (24, 45, 15, 46, 46, 16),  # V37
    (4, 152, 122, 18, 153, 123),
    (13, 74, 46, 32, 75, 47),
    (48, 54, 24, 14, 55, 25),
    (42, 45, 15, 32, 46, 16),  # V38
    (20, 147, 117, 4, 148, 118),
    (40, 75, 47, 7, 76, 48),
    (43, 54, 24, 22, 55, 25),
    (10, 45, 15, 67, 46, 16),  # V39
    (19, 148, 118, 6, 149, 119),
    (18, 75, 47, 31, 76, 48),
    (34, 54, 24, 34, 55, 25),
    (20, 45, 15, 61, 46, 16),  # V40
)

_EC_OFFSETS: Final[dict[QRECLevel, int]] = {"L": 0, "M": 1, "Q": 2, "H": 3}
_EC_INDICATORS: Final[dict[QRECLevel, int]] = {"L": 1, "M": 0, "Q": 3, "H": 2}


def _get_rs_blocks(version: int, ec_level: QRECLevel) -> list[tuple[int, int]]:
    """Retrieve the list of (total_codewords, data_codewords) for each EC block."""
    offset = _EC_OFFSETS[ec_level]
    entry = _RS_BLOCK_TABLE[(version - 1) * 4 + offset]
    blocks: list[tuple[int, int]] = []
    for i in range(0, len(entry), 3):
        count, total_count, data_count = entry[i : i + 3]
        for _ in range(count):
            blocks.append((total_count, data_count))
    return blocks


def _find_min_version(data_bytes_len: int, ec_level: QRECLevel) -> int:
    """Find the smallest QR version (1-40) that accommodates the byte payload."""
    for v in range(1, 41):
        blocks = _get_rs_blocks(v, ec_level)
        total_data_bytes = sum(b[1] for b in blocks)
        # Byte mode: 4 bits mode + (8 bits if v < 10 else 16 bits) count indicator
        count_bits = 8 if v < 10 else 16
        header_bits = 4 + count_bits
        required_bits = header_bits + data_bytes_len * 8
        if required_bits <= total_data_bytes * 8:
            return v
    raise ValueError(f"Payload of {data_bytes_len} bytes exceeds QR code capacity at EC={ec_level}")


def _bch_format_info(ec_level: QRECLevel, mask_idx: int) -> int:
    """Compute 15-bit BCH (15, 5) Format Information codeword."""
    d = (_EC_INDICATORS[ec_level] << 3) | mask_idx
    b = d << 10
    g = 0x537
    for i in range(14, 9, -1):
        if (b >> i) & 1:
            b ^= g << (i - 10)
    return ((d << 10) | b) ^ 0x5412


def _bch_version_info(version: int) -> int:
    """Compute 18-bit BCH (18, 6) Version Information codeword (V >= 7)."""
    d = version << 12
    g = 0x1F25
    for i in range(17, 11, -1):
        if (d >> i) & 1:
            d ^= g << (i - 12)
    return (version << 12) | d


def _calculate_mask_penalty(matrix: list[list[bool]], size: int) -> int:
    """Calculate ISO/IEC 18004 mask evaluation penalty score (N1 + N2 + N3 + N4)."""
    penalty = 0

    # N1: 5 or more consecutive modules of the same color in rows and columns
    for r in range(size):
        row = matrix[r]
        count = 1
        for c in range(1, size):
            if row[c] == row[c - 1]:
                count += 1
            else:
                if count >= 5:
                    penalty += 3 + (count - 5)
                count = 1
        if count >= 5:
            penalty += 3 + (count - 5)

    for c in range(size):
        count = 1
        for r in range(1, size):
            if matrix[r][c] == matrix[r - 1][c]:
                count += 1
            else:
                if count >= 5:
                    penalty += 3 + (count - 5)
                count = 1
        if count >= 5:
            penalty += 3 + (count - 5)

    # N2: 2x2 blocks of same color
    for r in range(size - 1):
        for c in range(size - 1):
            if matrix[r][c] == matrix[r + 1][c] == matrix[r][c + 1] == matrix[r + 1][c + 1]:
                penalty += 3

    # N3: 1:1:3:1:1 pattern with 4 light modules on either side
    p1 = [False, False, False, False, True, False, True, True, True, False, True]
    p2 = [True, False, True, True, True, False, True, False, False, False, False]
    for r in range(size):
        for c in range(size - 10):
            sub = matrix[r][c : c + 11]
            if sub in (p1, p2):
                penalty += 40

    for c in range(size):
        for r in range(size - 10):
            sub = [matrix[r + i][c] for i in range(11)]
            if sub in (p1, p2):
                penalty += 40

    # N4: Dark module proportion deviation from 50%
    dark_count = sum(sum(1 for cell in row if cell) for row in matrix)
    ratio = (dark_count * 100) / (size * size)
    penalty += int(abs(ratio - 50) // 5) * 10

    return penalty


class QRCode:
    """Standards-compliant pure-Python QR Code generator and renderer."""

    def __init__(
        self,
        data: str | bytes,
        *,
        ec_level: QRECLevel = "M",
        version: int | None = None,
    ) -> None:
        self.data_raw = data if isinstance(data, bytes) else data.encode("utf-8")
        self.data_str = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
        self.ec_level: QRECLevel = ec_level

        if version is not None:
            if not 1 <= version <= 40:
                raise ValueError("QR version must be between 1 and 40")
            self.version = version
        else:
            self.version = _find_min_version(len(self.data_raw), self.ec_level)

        self.size = self.version * 4 + 17
        self.mask_pattern: int = 0
        self.matrix: list[list[bool]] = self._generate()

    def _generate(self) -> list[list[bool]]:
        """Construct the standards-compliant QR Code matrix."""
        version = self.version
        ec_level = self.ec_level
        size = self.size
        data_bytes = self.data_raw

        rs_blocks = _get_rs_blocks(version, ec_level)
        total_data_bytes = sum(b[1] for b in rs_blocks)

        # 1. Build Byte mode bit stream
        bit_stream: list[int] = [0, 1, 0, 0]  # Mode indicator 0100 (Byte mode)
        char_count_bits = 8 if version < 10 else 16
        for i in range(char_count_bits - 1, -1, -1):
            bit_stream.append((len(data_bytes) >> i) & 1)

        for b in data_bytes:
            for i in range(7, -1, -1):
                bit_stream.append((b >> i) & 1)

        # 2. Terminator (up to 4 zeros)
        max_bits = total_data_bytes * 8
        needed_zeros = min(4, max_bits - len(bit_stream))
        bit_stream.extend([0] * needed_zeros)

        # 3. Pad to byte boundary
        while len(bit_stream) % 8 != 0:
            bit_stream.append(0)

        # 4. Convert bits to data codewords
        data_codewords: list[int] = []
        for i in range(0, len(bit_stream), 8):
            cw = 0
            for j in range(8):
                cw = (cw << 1) | bit_stream[i + j]
            data_codewords.append(cw)

        # 5. Fill with alternating pad bytes (0xEC, 0x11)
        pad_bytes = [0xEC, 0x11]
        pad_idx = 0
        while len(data_codewords) < total_data_bytes:
            data_codewords.append(pad_bytes[pad_idx])
            pad_idx = (pad_idx + 1) % 2

        # 6. Partition into RS blocks and compute error correction codewords
        offset = 0
        dc_blocks: list[list[int]] = []
        ec_blocks: list[list[int]] = []
        max_dc = 0
        max_ec = 0
        for total_c, data_c in rs_blocks:
            ec_c = total_c - data_c
            max_dc = max(max_dc, data_c)
            max_ec = max(max_ec, ec_c)
            block_dc = data_codewords[offset : offset + data_c]
            offset += data_c
            block_ec = _rs_encode_block(block_dc, ec_c)
            dc_blocks.append(block_dc)
            ec_blocks.append(block_ec)

        # 7. Interleave data codewords across all blocks, then EC codewords
        interleaved: list[int] = []
        for i in range(max_dc):
            for block_item in dc_blocks:
                if i < len(block_item):
                    interleaved.append(block_item[i])
        for i in range(max_ec):
            for block_item in ec_blocks:
                if i < len(block_item):
                    interleaved.append(block_item[i])

        # 8. Setup function patterns on base matrix
        base_matrix: list[list[bool | None]] = [[None] * size for _ in range(size)]

        # Finder patterns + 1-module quiet separators
        def add_finder(r: int, c: int) -> None:
            for dr in range(-1, 8):
                for dc in range(-1, 8):
                    rr, cc = r + dr, c + dc
                    if 0 <= rr < size and 0 <= cc < size:
                        if (
                            (0 <= dr <= 6 and dc in (0, 6))
                            or (0 <= dc <= 6 and dr in (0, 6))
                            or (2 <= dr <= 4 and 2 <= dc <= 4)
                        ):
                            base_matrix[rr][cc] = True
                        else:
                            base_matrix[rr][cc] = False

        add_finder(0, 0)
        add_finder(0, size - 7)
        add_finder(size - 7, 0)

        # Alignment patterns
        pos = _ALIGNMENT_POSITIONS[version - 1]
        for pr in pos:
            for pc in pos:
                if base_matrix[pr][pc] is not None:
                    continue
                for dr in range(-2, 3):
                    for dc in range(-2, 3):
                        if dr in (-2, 2) or dc in (-2, 2) or (dr == 0 and dc == 0):
                            base_matrix[pr + dr][pc + dc] = True
                        else:
                            base_matrix[pr + dr][pc + dc] = False

        # Timing patterns
        for i in range(8, size - 8):
            if base_matrix[i][6] is None:
                base_matrix[i][6] = i % 2 == 0
            if base_matrix[6][i] is None:
                base_matrix[6][i] = i % 2 == 0

        # Dark module
        base_matrix[size - 8][8] = True

        # Reserve format info areas
        for i in range(15):
            if i < 6:
                base_matrix[i][8] = False
            elif i < 8:
                base_matrix[i + 1][8] = False
            else:
                base_matrix[size - 15 + i][8] = False

            if i < 8:
                base_matrix[8][size - i - 1] = False
            elif i < 9:
                base_matrix[8][15 - i - 1 + 1] = False
            else:
                base_matrix[8][15 - i - 1] = False

        # Reserve version info areas (V >= 7)
        if version >= 7:
            for i in range(18):
                base_matrix[i // 3][i % 3 + size - 11] = False
                base_matrix[i % 3 + size - 11][i // 3] = False

        # 9. Evaluate all 8 masks and pick the mask with the lowest penalty
        best_matrix: list[list[bool]] = []
        min_penalty = float("inf")
        best_mask = 0

        for mask_idx in range(8):
            m: list[list[bool | None]] = [row[:] for row in base_matrix]

            # Place Format Information
            f_bits = _bch_format_info(ec_level, mask_idx)
            for i in range(15):
                bit_val = ((f_bits >> i) & 1) == 1
                if i < 6:
                    m[i][8] = bit_val
                elif i < 8:
                    m[i + 1][8] = bit_val
                else:
                    m[size - 15 + i][8] = bit_val

                if i < 8:
                    m[8][size - i - 1] = bit_val
                elif i < 9:
                    m[8][15 - i - 1 + 1] = bit_val
                else:
                    m[8][15 - i - 1] = bit_val

            # Place Version Information (V >= 7)
            if version >= 7:
                v_bits = _bch_version_info(version)
                for i in range(18):
                    v_val = ((v_bits >> i) & 1) == 1
                    m[i // 3][i % 3 + size - 11] = v_val
                    m[i % 3 + size - 11][i // 3] = v_val

            # Zigzag module placement with mask
            def mask_func(r: int, c: int, p: int = mask_idx) -> bool:
                if p == 0:
                    return (r + c) % 2 == 0
                if p == 1:
                    return r % 2 == 0
                if p == 2:
                    return c % 3 == 0
                if p == 3:
                    return (r + c) % 3 == 0
                if p == 4:
                    return (r // 2 + c // 3) % 2 == 0
                if p == 5:
                    return ((r * c) % 2) + ((r * c) % 3) == 0
                if p == 6:
                    return (((r * c) % 2) + ((r * c) % 3)) % 2 == 0
                if p == 7:
                    return (((r + c) % 2) + ((r * c) % 3)) % 2 == 0
                return False

            inc = -1
            row = size - 1
            bit_idx = 7
            byte_idx = 0
            data_len = len(interleaved)

            for col in range(size - 1, 0, -2):
                if col <= 6:
                    col -= 1
                col_range = (col, col - 1)
                while True:
                    for c in col_range:
                        if m[row][c] is None:
                            dark = False
                            if byte_idx < data_len:
                                dark = ((interleaved[byte_idx] >> bit_idx) & 1) == 1
                            if mask_func(row, c):
                                dark = not dark
                            m[row][c] = dark
                            bit_idx -= 1
                            if bit_idx == -1:
                                byte_idx += 1
                                bit_idx = 7
                    row += inc
                    if row < 0 or size <= row:
                        row -= inc
                        inc = -inc
                        break

            # Penalty evaluation
            m_bool = [[bool(cell) for cell in row] for row in m]
            penalty = _calculate_mask_penalty(m_bool, size)

            if penalty < min_penalty:
                min_penalty = penalty
                best_mask = mask_idx
                best_matrix = m_bool

        self.mask_pattern = best_mask
        return best_matrix

    def render_matrix(self, border: int = 4) -> list[list[bool]]:
        """Return the 2D boolean QR matrix with quiet zone padding."""
        size = self.size
        total_size = size + 2 * border
        padded: list[list[bool]] = [[False] * total_size for _ in range(total_size)]
        for r in range(size):
            for c in range(size):
                padded[r + border][c + border] = self.matrix[r][c]
        return padded

    def render_lines(self, border: int = 4) -> list[str]:
        """Render QR code rows using Unicode half-block characters (▀, ▄, █, space)."""
        padded = self.render_matrix(border=border)
        total_rows = len(padded)
        total_cols = len(padded[0])
        lines: list[str] = []

        for r in range(0, total_rows, 2):
            line: list[str] = []
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
        return lines

    def render_ascii(self, border: int = 4) -> str:
        """Render raw Unicode half-block QR code string."""
        return "\n".join(self.render_lines(border=border))

    def render_rich(self, border: int = 4) -> Text:
        """Render as a Rich Text object with high-contrast black-on-white styling."""
        lines = self.render_lines(border=border)
        full_text = "\n".join(lines)
        return Text(full_text, style="bold black on white", no_wrap=True)

    def render_ansi(self, border: int = 4) -> str:
        """Render as ANSI string with explicit black-on-white styling on each line."""
        lines = self.render_lines(border=border)
        return "\n".join(f"\033[30;47m{line}\033[0m" for line in lines)


def copy_to_clipboard(text: str) -> bool:
    """Attempt to copy text to system/terminal clipboard using OSC 52 or platform tools."""
    # 1. OSC 52 terminal clipboard escape sequence (Termux, tmux, modern terminals)
    try:
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        sys.stdout.write(f"\033]52;c;{b64}\x07")
        sys.stdout.flush()
    except Exception:
        pass

    # 2. Platform CLI clipboard tools (Termux, Wayland, X11)
    tools = [
        ("termux-clipboard-set", []),
        ("wl-copy", []),
        ("xclip", ["-selection", "clipboard"]),
        ("xsel", ["--clipboard", "--input"]),
    ]
    for tool, args in tools:
        if shutil.which(tool):
            try:
                subprocess.run(
                    [tool, *args],
                    input=text.encode("utf-8"),
                    check=True,
                    capture_output=True,
                    timeout=1,
                )
                return True
            except Exception:
                continue
    return False


def render_invite_qr_panel(
    invite_url: str,
    *,
    title: str = "ROOM INVITE",
    subtitle: str = "Scan this code with another device",
    expires_minutes: int | None = None,
    theme_accent: str = "gl.accent",
) -> Panel:
    """Render a clean, minimal QR code panel for sharing a GhostLink invite link."""
    qr = QRCode(invite_url, ec_level="M")
    qr_text = qr.render_rich(border=4)

    body_elements: list[RenderableType] = [
        Align.center(qr_text),
        Text(""),
        Align.center(Text(subtitle, style="gl.muted")),
    ]

    info_parts = [f"Link: {invite_url}"]
    if expires_minutes is not None:
        info_parts.append(f"Expires: {expires_minutes}m")

    body_elements.append(Align.center(Text("  ·  ".join(info_parts), style=theme_accent)))

    return Panel(
        Group(*body_elements),
        title=f"[{theme_accent}]{title}[/]",
        border_style=theme_accent,
        padding=(0, 2),
        expand=False,
    )
