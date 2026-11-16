"""A small QR Code encoder (ISO/IEC 18004, byte mode, versions 1–20, levels L and M).

Just enough to print the pairing link in a terminal without adding a dependency.
`encode()` returns the symbol as rows of booleans (True = dark module).
"""

from __future__ import annotations

# (ec codewords per block, blocks in group 1, data codewords each, blocks in group 2, data codewords each)
_BLOCKS = {
    "L": [None,
          (7, 1, 19, 0, 0), (10, 1, 34, 0, 0), (15, 1, 55, 0, 0), (20, 1, 80, 0, 0), (26, 1, 108, 0, 0),
          (18, 2, 68, 0, 0), (20, 2, 78, 0, 0), (24, 2, 97, 0, 0), (30, 2, 116, 0, 0), (18, 2, 68, 2, 69),
          (20, 4, 81, 0, 0), (24, 2, 92, 2, 93), (26, 4, 107, 0, 0), (30, 3, 115, 1, 116), (22, 5, 87, 1, 88),
          (24, 5, 98, 1, 99), (28, 1, 107, 5, 108), (30, 5, 120, 1, 121), (28, 3, 113, 4, 114), (28, 3, 107, 5, 108)],
    "M": [None,
          (10, 1, 16, 0, 0), (16, 1, 28, 0, 0), (26, 1, 44, 0, 0), (18, 2, 32, 0, 0), (24, 2, 43, 0, 0),
          (16, 4, 27, 0, 0), (18, 4, 31, 0, 0), (22, 2, 38, 2, 39), (22, 3, 36, 2, 37), (26, 4, 43, 1, 44),
          (30, 1, 50, 4, 51), (22, 6, 36, 2, 37), (22, 8, 37, 1, 38), (24, 4, 40, 5, 41), (24, 5, 41, 5, 42),
          (28, 7, 45, 3, 46), (28, 10, 46, 1, 47), (26, 9, 43, 4, 44), (26, 3, 44, 11, 45), (26, 3, 41, 13, 42)],
}
_FORMAT_BITS = {"L": 1, "M": 0}
_ALIGNMENT = [None, [], [6, 18], [6, 22], [6, 26], [6, 30], [6, 34], [6, 22, 38], [6, 24, 42], [6, 26, 46],
              [6, 28, 50], [6, 30, 54], [6, 32, 58], [6, 34, 62], [6, 26, 46, 66], [6, 26, 48, 70],
              [6, 26, 50, 74], [6, 30, 54, 78], [6, 30, 56, 82], [6, 30, 58, 86], [6, 34, 62, 90]]
MAX_VERSION = 20

# GF(256) log/antilog tables for the Reed–Solomon code (primitive polynomial 0x11D)
_EXP = [0] * 512
_LOG = [0] * 256
_v = 1
for _i in range(255):
    _EXP[_i] = _v
    _LOG[_v] = _i
    _v <<= 1
    if _v & 0x100:
        _v ^= 0x11D
for _i in range(255, 512):
    _EXP[_i] = _EXP[_i - 255]


def _rs_remainder(data: bytes, degree: int) -> bytes:
    # generator polynomial (x - a^0)(x - a^1)…(x - a^(degree-1)), highest power first, monic term dropped
    gen = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            gen[j] = _EXP[_LOG[gen[j]] + _LOG[root]] if gen[j] and root else 0
            if j + 1 < degree:
                gen[j] ^= gen[j + 1]
        root = _EXP[_LOG[root] + 1]
    rem = [0] * degree
    for byte in data:
        factor = byte ^ rem.pop(0)
        rem.append(0)
        if factor:
            for j in range(degree):
                if gen[j]:
                    rem[j] ^= _EXP[_LOG[gen[j]] + _LOG[factor]]
    return bytes(rem)


def data_capacity(version: int, level: str) -> int:
    """Payload bytes that fit in byte mode."""
    _ec, n1, d1, n2, d2 = _BLOCKS[level][version]
    bits = (n1 * d1 + n2 * d2) * 8 - 4 - (8 if version < 10 else 16)
    return bits // 8


def pick_version(length: int, level: str) -> int:
    for version in range(1, MAX_VERSION + 1):
        if length <= data_capacity(version, level):
            return version
    raise ValueError(f"{length} bytes do not fit in a version-{MAX_VERSION} QR code at level {level}")


def _codewords(data: bytes, version: int, level: str) -> bytes:
    ec, n1, d1, n2, d2 = _BLOCKS[level][version]
    capacity = n1 * d1 + n2 * d2
    bits: list[int] = []

    def put(value: int, width: int) -> None:
        bits.extend((value >> shift) & 1 for shift in range(width - 1, -1, -1))

    put(0b0100, 4)                                   # byte mode
    put(len(data), 8 if version < 10 else 16)
    for byte in data:
        put(byte, 8)
    put(0, min(4, capacity * 8 - len(bits)))         # terminator
    put(0, -len(bits) % 8)
    stream = bytearray(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))
    pad = (0xEC, 0x11)
    while len(stream) < capacity:
        stream.append(pad[(len(stream) - (len(bits) // 8)) % 2])

    blocks, pos = [], 0
    for size in [d1] * n1 + [d2] * n2:
        blocks.append(bytes(stream[pos:pos + size]))
        pos += size
    ec_blocks = [_rs_remainder(block, ec) for block in blocks]
    out = bytearray()
    for i in range(max(d1, d2)):
        out.extend(block[i] for block in blocks if i < len(block))
    for i in range(ec):
        out.extend(block[i] for block in ec_blocks)
    return bytes(out)


def _function_patterns(version: int) -> tuple[list[list[bool]], list[list[bool]]]:
    size = version * 4 + 17
    modules = [[False] * size for _ in range(size)]
    reserved = [[False] * size for _ in range(size)]

    def put(row: int, col: int, dark: bool) -> None:
        if 0 <= row < size and 0 <= col < size:
            modules[row][col] = dark
            reserved[row][col] = True

    for i in range(size):                            # timing patterns
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cr, cc in ((3, 3), (3, size - 4), (size - 4, 3)):   # finders with separators
        for dr in range(-4, 5):
            for dc in range(-4, 5):
                dist = max(abs(dr), abs(dc))
                put(cr + dr, cc + dc, dist not in (2, 4))
    centres = _ALIGNMENT[version]
    for i, r in enumerate(centres):                  # alignment patterns
        for j, c in enumerate(centres):
            if (i == 0 and j == 0) or (i == 0 and j == len(centres) - 1) or (i == len(centres) - 1 and j == 0):
                continue
            for dr in range(-2, 3):
                for dc in range(-2, 3):
                    put(r + dr, c + dc, max(abs(dr), abs(dc)) != 1)
    for i in range(9):                               # format information areas
        if i != 6:                                   # (6 is the timing pattern)
            put(8, i, False)
            put(i, 8, False)
    for i in range(8):
        put(8, size - 1 - i, False)
        put(size - 1 - i, 8, False)
    put(size - 8, 8, True)                           # the dark module
    if version >= 7:                                 # version information
        rem = version
        for _ in range(12):
            rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
        bits = version << 12 | rem
        for i in range(18):
            bit = bool((bits >> i) & 1)
            a, b = size - 11 + i % 3, i // 3
            put(b, a, bit)
            put(a, b, bit)
    return modules, reserved


def _draw_format(modules: list[list[bool]], level: str, mask: int) -> None:
    size = len(modules)
    data = _FORMAT_BITS[level] << 3 | mask
    rem = data
    for _ in range(10):
        rem = (rem << 1) ^ ((rem >> 9) * 0x537)
    bits = (data << 10 | rem) ^ 0x5412

    def bit(i: int) -> bool:
        return bool((bits >> i) & 1)

    for i in range(6):
        modules[i][8] = bit(i)
    modules[7][8] = bit(6)
    modules[8][8] = bit(7)
    modules[8][7] = bit(8)
    for i in range(9, 15):
        modules[8][14 - i] = bit(i)
    for i in range(8):
        modules[8][size - 1 - i] = bit(i)
    for i in range(8, 15):
        modules[size - 15 + i][8] = bit(i)
    modules[size - 8][8] = True


_MASKS = (
    lambda r, c: (r + c) % 2 == 0,
    lambda r, c: r % 2 == 0,
    lambda r, c: c % 3 == 0,
    lambda r, c: (r + c) % 3 == 0,
    lambda r, c: (r // 2 + c // 3) % 2 == 0,
    lambda r, c: (r * c) % 2 + (r * c) % 3 == 0,
    lambda r, c: ((r * c) % 2 + (r * c) % 3) % 2 == 0,
    lambda r, c: ((r + c) % 2 + (r * c) % 3) % 2 == 0,
)


def _penalty(m: list[list[bool]]) -> int:
    size = len(m)
    score = 0
    lines = [row for row in m] + [[m[r][c] for r in range(size)] for c in range(size)]
    for line in lines:
        run, prev = 0, None
        for value in line:
            if value == prev:
                run += 1
            else:
                if run >= 5:
                    score += run - 2
                run, prev = 1, value
        if run >= 5:
            score += run - 2
        text = "".join("1" if v else "0" for v in line)
        score += 40 * (_count(text, "10111010000") + _count(text, "00001011101"))
    for r in range(size - 1):
        for c in range(size - 1):
            if m[r][c] == m[r][c + 1] == m[r + 1][c] == m[r + 1][c + 1]:
                score += 3
    dark = sum(sum(row) for row in m)
    score += 10 * (abs(dark * 20 - size * size * 10) // (size * size))
    return score


def _count(text: str, pattern: str) -> int:
    count, start = 0, text.find(pattern)
    while start != -1:
        count += 1
        start = text.find(pattern, start + 1)
    return count


def matrix_for(data: bytes, version: int, level: str, mask: int) -> list[list[bool]]:
    """The symbol for an explicit version / level / mask (used to verify the encoder)."""
    modules, reserved = _function_patterns(version)
    size = len(modules)
    codewords = _codewords(data, version, level)
    total_bits = len(codewords) * 8
    index = 0
    right = size - 1
    while right >= 1:
        if right == 6:
            right = 5
        for vert in range(size):
            for j in range(2):
                col = right - j
                upward = ((right + 1) & 2) == 0
                row = size - 1 - vert if upward else vert
                if reserved[row][col]:
                    continue
                bit = False
                if index < total_bits:
                    bit = bool((codewords[index >> 3] >> (7 - (index & 7))) & 1)
                index += 1
                modules[row][col] = bit != _MASKS[mask](row, col)
        right -= 2
    if not total_bits <= index <= total_bits + 7:
        raise AssertionError(f"version {version}: {index} data modules for {total_bits} codeword bits")
    _draw_format(modules, level, mask)
    return modules


def encode(data: bytes, level: str = "M") -> list[list[bool]]:
    """Smallest symbol that holds `data`, with the mask the standard's penalty rules prefer."""
    version = pick_version(len(data), level)
    best = None
    for mask in range(8):
        candidate = matrix_for(data, version, level, mask)
        score = _penalty(candidate)
        if best is None or score < best[0]:
            best = (score, candidate)
    return best[1]
