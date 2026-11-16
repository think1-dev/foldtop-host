import unittest

from . import helpers  # noqa: F401
from foldtop_host import keymap
from foldtop_host.keymap import (KeymapIndex, KeyStroke, ScrollAccumulator, hid_to_evdev, hid_to_x11_keycode,
                                 is_cased_letter, keysym_for_char, text_keysyms)

# linux/input-event-codes.h
KEY = dict(ESC=1, N1=2, N0=11, MINUS=12, EQUAL=13, BACKSPACE=14, TAB=15, Q=16, ENTER=28, LEFTCTRL=29, A=30,
           GRAVE=41, LEFTSHIFT=42, BACKSLASH=43, Z=44, SLASH=53, RIGHTSHIFT=54, KPASTERISK=55, LEFTALT=56,
           SPACE=57, CAPSLOCK=58, F1=59, F10=68, NUMLOCK=69, KP7=71, KP0=82, F11=87, F12=88, KPENTER=96,
           RIGHTCTRL=97, KPSLASH=98, SYSRQ=99, RIGHTALT=100, HOME=102, UP=103, PAGEUP=104, LEFT=105,
           RIGHT=106, END=107, DOWN=108, PAGEDOWN=109, INSERT=110, DELETE=111, MUTE=113, VOLUMEDOWN=114,
           VOLUMEUP=115, PAUSE=119, LEFTMETA=125, RIGHTMETA=126, COMPOSE=127, F13=183, F24=194)


class HidTableTests(unittest.TestCase):
    def test_letters(self):
        # HID usages 0x04…0x1D are A…Z; evdev codes follow the QWERTY rows.
        qwerty_rows = ["QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM"]
        first = {0: 16, 1: 30, 2: 44}
        for row_index, row in enumerate(qwerty_rows):
            for column, letter in enumerate(row):
                usage = 0x04 + ord(letter) - ord("A")
                self.assertEqual(hid_to_evdev(usage), first[row_index] + column, letter)

    def test_digits(self):
        for i in range(9):
            self.assertEqual(hid_to_evdev(0x1E + i), KEY["N1"] + i)
        self.assertEqual(hid_to_evdev(0x27), KEY["N0"])

    def test_named_keys(self):
        expected = {
            0x28: "ENTER", 0x29: "ESC", 0x2A: "BACKSPACE", 0x2B: "TAB", 0x2C: "SPACE", 0x2D: "MINUS",
            0x2E: "EQUAL", 0x31: "BACKSLASH", 0x35: "GRAVE", 0x38: "SLASH", 0x39: "CAPSLOCK",
            0x3A: "F1", 0x43: "F10", 0x44: "F11", 0x45: "F12", 0x46: "SYSRQ", 0x48: "PAUSE",
            0x49: "INSERT", 0x4A: "HOME", 0x4B: "PAGEUP", 0x4C: "DELETE", 0x4D: "END", 0x4E: "PAGEDOWN",
            0x4F: "RIGHT", 0x50: "LEFT", 0x51: "DOWN", 0x52: "UP", 0x53: "NUMLOCK", 0x54: "KPSLASH",
            0x55: "KPASTERISK", 0x58: "KPENTER", 0x5F: "KP7", 0x62: "KP0", 0x65: "COMPOSE",
            0x68: "F13", 0x73: "F24", 0x7F: "MUTE", 0x80: "VOLUMEUP", 0x81: "VOLUMEDOWN",
            0xE0: "LEFTCTRL", 0xE1: "LEFTSHIFT", 0xE2: "LEFTALT", 0xE3: "LEFTMETA",
            0xE4: "RIGHTCTRL", 0xE5: "RIGHTSHIFT", 0xE6: "RIGHTALT", 0xE7: "RIGHTMETA",
        }
        for usage, name in expected.items():
            self.assertEqual(hid_to_evdev(usage), KEY[name], f"usage 0x{usage:02x} → {name}")

    def test_function_keys_are_contiguous(self):
        self.assertEqual([hid_to_evdev(u) for u in range(0x3A, 0x44)], list(range(59, 69)))    # F1–F10
        self.assertEqual([hid_to_evdev(u) for u in range(0x68, 0x74)], list(range(183, 195)))  # F13–F24

    def test_x11_keycode_is_evdev_plus_8(self):
        self.assertEqual(hid_to_x11_keycode(0x04), 38)   # A
        self.assertEqual(hid_to_x11_keycode(0xE1), 50)   # Left Shift
        self.assertEqual(hid_to_x11_keycode(0x28), 36)   # Return
        self.assertEqual(hid_to_x11_keycode(0x2C), 65)   # space

    def test_unmapped(self):
        for usage in (0, 1, 2, 3, 0x82, 0xA5, 0xFC, 256, 0xFFFF, -1):
            self.assertEqual(hid_to_evdev(usage), 0)
            self.assertEqual(hid_to_x11_keycode(usage), 0)

    def test_every_mapped_usage_fits_an_x11_keycode(self):
        for usage, code in enumerate(keymap.HID_TO_EVDEV):
            if code:
                self.assertTrue(9 <= code + 8 <= 255, hex(usage))

    def test_full_main_block_is_mapped(self):
        missing = [hex(u) for u in list(range(0x04, 0x68)) + list(range(0xE0, 0xE8)) if not hid_to_evdev(u)]
        self.assertEqual(missing, [])


class KeysymTests(unittest.TestCase):
    def test_latin1_and_unicode(self):
        self.assertEqual(keysym_for_char("a"), 0x61)
        self.assertEqual(keysym_for_char("~"), 0x7E)
        self.assertEqual(keysym_for_char("é"), 0xE9)
        self.assertEqual(keysym_for_char("€"), 0x010020AC)
        self.assertEqual(keysym_for_char("🌍"), 0x0101F30D)
        self.assertEqual(keysym_for_char("日"), 0x010065E5)

    def test_control_characters(self):
        self.assertEqual(keysym_for_char("\n"), 0xFF0D)
        self.assertEqual(keysym_for_char("\t"), 0xFF09)
        self.assertEqual(keysym_for_char("\b"), 0xFF08)
        self.assertIsNone(keysym_for_char("\x00"))
        self.assertIsNone(keysym_for_char("\x85"))

    def test_text_keysyms(self):
        self.assertEqual(text_keysyms("a\r\nb"), [0x61, 0xFF0D, 0x62])      # CRLF → one Return
        self.assertEqual(text_keysyms("a\n\nb"), [0x61, 0xFF0D, 0xFF0D, 0x62])
        self.assertEqual(text_keysyms("x\x00y"), [0x78, 0x79])             # untypable dropped
        self.assertEqual(text_keysyms(""), [])

    def test_cased_letters(self):
        self.assertTrue(is_cased_letter(0x61))
        self.assertTrue(is_cased_letter(0xE9))                  # é
        self.assertTrue(is_cased_letter(0x01000436))            # Cyrillic ж
        self.assertFalse(is_cased_letter(0x31))                 # 1
        self.assertFalse(is_cased_letter(0xFF0D))               # Return
        self.assertFalse(is_cased_letter(0x0101F30D))           # emoji


class KeymapIndexTests(unittest.TestCase):
    US = {
        38: (0x61, 0x41),    # a A
        10: (0x31, 0x21),    # 1 !
        36: (0xFF0D, 0),     # Return
        65: (0x20, 0x20),    # space (same on both levels)
        50: (0xFFE1, 0xFFE1),
        104: (0xFF0D, 0),    # a second key producing Return (keypad Enter in some maps)
        200: (0x7A, 0x5A),   # z Z on a scratch keycode
    }

    def test_lookup(self):
        index = KeymapIndex(self.US)
        self.assertEqual(index.lookup(0x61), KeyStroke(38, False))
        self.assertEqual(index.lookup(0x41), KeyStroke(38, True))
        self.assertEqual(index.lookup(0x21), KeyStroke(10, True))
        self.assertEqual(index.lookup(0x20), KeyStroke(65, False))   # unshifted preferred
        self.assertEqual(index.lookup(0xFF0D), KeyStroke(36, False))  # lowest keycode wins
        self.assertIsNone(index.lookup(0xE9))

    def test_unshifted_beats_shifted_across_keys(self):
        # '<' is Shift+comma on US but its own key on ISO boards: prefer the plain key.
        index = KeymapIndex({59: (0x2C, 0x3C), 94: (0x3C, 0x3E)})
        self.assertEqual(index.lookup(0x3C), KeyStroke(94, False))
        self.assertEqual(index.lookup(0x3E), KeyStroke(94, True))

    def test_excluded_keycodes(self):
        index = KeymapIndex(self.US, exclude=frozenset({200}))
        self.assertIsNone(index.lookup(0x7A))
        self.assertIsNotNone(KeymapIndex(self.US).lookup(0x7A))


class ScrollTests(unittest.TestCase):
    def test_accumulates_fractions(self):
        acc = ScrollAccumulator()
        clicks = [acc.add(0, 40) for _ in range(6)]
        self.assertEqual(clicks, [(0, 0), (0, 0), (0, 1), (0, 0), (0, 0), (0, 1)])

    def test_direction_and_remainder(self):
        acc = ScrollAccumulator()
        self.assertEqual(acc.add(0, 250), (0, 2))     # 10 left over
        self.assertEqual(acc.add(0, -20), (0, 0))     # now -10
        self.assertEqual(acc.add(0, -110), (0, -1))
        self.assertEqual(acc.add(-360, 0), (-3, 0))
        self.assertEqual(acc.add(119, 119), (0, 0))
        self.assertEqual(acc.add(1, 1), (1, 1))

    def test_reversal_does_not_fire_early(self):
        acc = ScrollAccumulator()
        self.assertEqual(acc.add(0, 100), (0, 0))
        self.assertEqual(acc.add(0, -100), (0, 0))
        self.assertEqual(acc.add(0, -119), (0, 0))
        self.assertEqual(acc.add(0, -1), (0, -1))

    def test_reset(self):
        acc = ScrollAccumulator()
        acc.add(100, 100)
        acc.reset()
        self.assertEqual(acc.add(20, 20), (0, 0))


if __name__ == "__main__":
    unittest.main()
