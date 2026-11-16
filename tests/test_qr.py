"""The built-in QR encoder.

It was verified once against independent tools (byte-identical to the `segno`
encoder for every version/level/mask at full capacity; 1600/1600 symbols decoded by
zxing-cpp). Those tools are not dependencies, so the suite pins known-good output
and checks the structural invariants instead.
"""

import hashlib
import unittest

from . import helpers  # noqa: F401
from foldtop_host import qr, qrgen

# total codewords per version, ISO/IEC 18004 table 1
TOTAL_CODEWORDS = [26, 44, 70, 100, 134, 172, 196, 242, 292, 346,
                   404, 466, 532, 581, 655, 733, 815, 901, 991, 1085]

GOLDEN_V1_M = [
    "#######..#.#..#######",
    "#.....#..###..#.....#",
    "#.###.#.#.#.#.#.###.#",
    "#.###.#.###.#.#.###.#",
    "#.###.#.#...#.#.###.#",
    "#.....#.#.#.#.#.....#",
    "#######.#.#.#.#######",
    "........##...........",
    "#.#####.....#.#####..",
    "##.....##..#..#.##..#",
    ".#.##.#..#.##.##.#.#.",
    "..##.......##..#.##.#",
    "###...####....##...#.",
    "........#.#..##.##..#",
    "#######....####...##.",
    "#.....#.#####...###.#",
    "#.###.#.#...#.#.#..##",
    "#.###.#.#....######..",
    "#.###.#.#.#####...#..",
    "#.....#...#.##.#.##..",
    "#######.#.#.###..#.#.",
]


def _text(matrix):
    return ["".join("#" if v else "." for v in row) for row in matrix]


class QrEncoderTests(unittest.TestCase):
    def test_block_tables_add_up_to_the_standard_totals(self):
        for level in ("L", "M"):
            for version in range(1, qrgen.MAX_VERSION + 1):
                ec, n1, d1, n2, d2 = qrgen._BLOCKS[level][version]
                self.assertEqual(n1 * d1 + n2 * d2 + ec * (n1 + n2), TOTAL_CODEWORDS[version - 1], (level, version))
                if n2:
                    self.assertEqual(d2, d1 + 1, (level, version))

    def test_data_modules_match_codeword_count(self):
        # matrix_for() asserts that the free modules hold exactly the codewords (+ ≤7 remainder bits)
        for version in range(1, qrgen.MAX_VERSION + 1):
            matrix = qrgen.matrix_for(b"x", version, "M", 0)
            self.assertEqual(len(matrix), version * 4 + 17)
            self.assertTrue(all(len(row) == len(matrix) for row in matrix))

    def test_known_answer_v1(self):
        self.assertEqual(_text(qrgen.encode(b"foldtop://pair", "M")), GOLDEN_V1_M)

    def test_known_answer_v7_with_version_info(self):
        matrix = qrgen.matrix_for(b"x" * 100, 7, "L", 2)
        digest = hashlib.sha256("".join(_text(matrix)).encode()).hexdigest()
        self.assertEqual(digest, "c9e3f6d7fcd3cb83831ec9d50077d240de8ddbddcdfd669e3160160e40a4545a")

    def test_function_patterns(self):
        for version in (1, 6, 7, 10, 14, 20):
            m = qrgen.encode(b"a" * qrgen.data_capacity(version, "M"), "M")
            size = len(m)
            self.assertEqual(size, version * 4 + 17)
            finder = ["#######", "#.....#", "#.###.#", "#.###.#", "#.###.#", "#.....#", "#######"]
            rows = _text(m)
            for r in range(7):
                self.assertEqual(rows[r][:7], finder[r])                      # top-left
                self.assertEqual(rows[r][size - 7:], finder[r])               # top-right
                self.assertEqual(rows[size - 7 + r][:7], finder[r])           # bottom-left
            self.assertTrue(all(rows[7][c] == "." for c in range(8)))         # separators
            self.assertTrue(all(rows[r][7] == "." for r in range(8)))
            for i in range(8, size - 8):                                      # timing patterns
                self.assertEqual(m[6][i], i % 2 == 0, (version, i))
                self.assertEqual(m[i][6], i % 2 == 0, (version, i))
            self.assertTrue(m[size - 8][8])                                   # dark module

    def test_format_information_is_a_valid_bch_codeword(self):
        for level, level_bits in (("L", 1), ("M", 0)):
            for mask in range(8):
                m = qrgen.matrix_for(b"hello", 2, level, mask)
                size = len(m)
                first = [m[i][8] for i in range(6)] + [m[7][8], m[8][8], m[8][7]] + [m[8][14 - i] for i in range(9, 15)]
                second = [m[8][size - 1 - i] for i in range(8)] + [m[size - 15 + i][8] for i in range(8, 15)]
                self.assertEqual(first, second)
                bits = sum(1 << i for i, v in enumerate(first) if v) ^ 0x5412
                self.assertEqual(bits >> 10, level_bits << 3 | mask)
                rem = bits
                for shift in range(4, -1, -1):                                # divide by the generator 0x537
                    if rem & (1 << (shift + 10)):
                        rem ^= 0x537 << shift
                self.assertEqual(rem, 0)

    def test_capacity_and_version_choice(self):
        self.assertEqual(qrgen.data_capacity(1, "L"), 17)
        self.assertEqual(qrgen.data_capacity(1, "M"), 14)
        self.assertEqual(qrgen.data_capacity(9, "L"), 230)
        self.assertEqual(qrgen.data_capacity(10, "L"), 271)
        self.assertEqual(qrgen.data_capacity(10, "M"), 213)
        self.assertEqual(qrgen.pick_version(14, "M"), 1)
        self.assertEqual(qrgen.pick_version(15, "M"), 2)
        self.assertEqual(qrgen.pick_version(211, "M"), 10)
        with self.assertRaises(ValueError):
            qrgen.pick_version(5000, "M")

    def test_reed_solomon_known_answer(self):
        # ISO/IEC 18004 Annex I worked example: "01234567" at 1-M
        data = bytes([0x10, 0x20, 0x0C, 0x56, 0x61, 0x80, 0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11, 0xEC, 0x11])
        self.assertEqual(qrgen._rs_remainder(data, 10).hex(), "a524d4c1ed36c7872c55")
        # a codeword polynomial followed by its remainder is divisible by the generator
        self.assertEqual(qrgen._rs_remainder(data + qrgen._rs_remainder(data, 10), 10), bytes(10))


class QrRenderTests(unittest.TestCase):
    LINK = ("foldtop://pair?host=192.168.0.120,100.64.10.20&port=47800&fp=" + "ab" * 32 +
            "&code=K7M2QX9D&id=2d2a6cd9-91e9-4406-bb78-0506e8fec230&name=workstation-1")

    def test_pairing_link_fits(self):
        m = qr.matrix(self.LINK)
        self.assertIsNotNone(m)
        self.assertLessEqual(len(m), 65)        # version ≤ 12: fits an 80-column terminal

    def test_render_shape(self):
        m = qr.matrix(self.LINK)
        art = qr.render(self.LINK, quiet=2, indent="")
        lines = art.split("\n")
        self.assertEqual(len(lines), (len(m) + 4 + 1) // 2)
        for line in lines:
            body = line.replace("\x1b[30;47m", "").replace("\x1b[0m", "")
            self.assertEqual(len(body), len(m) + 4)
            self.assertTrue(set(body) <= set("█▀▄ "))
        # the quiet zone is light: first text row has no full/upper blocks in its first two columns
        first = lines[0].replace("\x1b[30;47m", "").replace("\x1b[0m", "")
        self.assertEqual(first[:2], "  ")

    def test_render_reproduces_the_matrix(self):
        m = qr.matrix("foldtop")
        lines = [l.replace("\x1b[30;47m", "").replace("\x1b[0m", "") for l in qr.render("foldtop", quiet=0, indent="").split("\n")]
        for r, row in enumerate(m):
            for c, dark in enumerate(row):
                ch = lines[r // 2][c]
                shown = ch in ("█", "▀") if r % 2 == 0 else ch in ("█", "▄")
                self.assertEqual(shown, dark, (r, c))

    def test_too_long_returns_none(self):
        self.assertIsNone(qr.matrix("x" * 5000))
        self.assertIsNone(qr.render("x" * 5000))


if __name__ == "__main__":
    unittest.main()
