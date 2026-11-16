"""Tests against the real X display.

These run in someone's live session, so they are deliberately gentle: they read
state, move the pointer and put it back, and tap Left Shift. Nothing printable is
typed and nothing is clicked — those paths are exercised against a nested X server
in test_xephyr.py.
"""

import time
import unittest

from . import helpers
from foldtop_host import protocol as P

if helpers.HAVE_DISPLAY:
    from foldtop_host import x11
    from foldtop_host.cursor import CursorTracker
    from foldtop_host.inject import Injector
    from foldtop_host.keymap import KeyStroke, hid_to_x11_keycode


def _wait(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


@unittest.skipUnless(helpers.HAVE_DISPLAY, "no X display")
class DisplayTests(unittest.TestCase):
    def setUp(self):
        self.d = x11.Display()
        self.addCleanup(self.d.close)

    def test_extensions_present(self):
        self.assertTrue(self.d.has_xtest())
        self.assertTrue(self.d.has_randr())
        self.assertTrue(self.d.has_xfixes())

    def test_monitors_cover_the_screen(self):
        mons = self.d.monitors()
        self.assertGreaterEqual(len(mons), 1)
        self.assertTrue(mons[0].primary)
        self.assertEqual(sum(m.primary for m in mons), 1)
        sw, sh = self.d.screen_size()
        for m in mons:
            self.assertTrue(m.name)
            self.assertGreater(m.width, 0)
            self.assertGreater(m.height, 0)
            self.assertTrue(0 <= m.x and m.x + m.width <= sw and 0 <= m.y and m.y + m.height <= sh, m)

    def test_cursor_image_is_consistent(self):
        img = self.d.cursor_image()
        self.assertIsNotNone(img)
        self.assertEqual(len(img.rgba), img.width * img.height * 4)
        self.assertEqual(img.serial, self.d.cursor_serial() & 0xFFFFFFFF)
        # premultiplied alpha: no colour channel may exceed alpha
        for i in range(0, len(img.rgba), 4):
            a = img.rgba[i + 3]
            self.assertTrue(img.rgba[i] <= a and img.rgba[i + 1] <= a and img.rgba[i + 2] <= a)

    def test_keymap_matches_hid_table(self):
        """The evdev+8 assumption holds on this server: HID 'A' → keysym a, etc."""
        lo, table = self.d.keyboard_mapping()
        self.assertEqual(lo, 8)

        def sym(usage):
            return table[hid_to_x11_keycode(usage) - lo][0]

        self.assertEqual(sym(0xE1), 0xFFE1)   # Shift_L
        self.assertEqual(sym(0xE0), 0xFFE3)   # Control_L
        self.assertEqual(sym(0x28), 0xFF0D)   # Return
        self.assertEqual(sym(0x29), 0xFF1B)   # Escape
        self.assertEqual(sym(0x2A), 0xFF08)   # BackSpace
        self.assertEqual(sym(0x2B), 0xFF09)   # Tab
        self.assertEqual(sym(0x2C), 0x20)     # space
        self.assertEqual(sym(0x4F), 0xFF53)   # Right
        self.assertEqual(sym(0x50), 0xFF51)   # Left
        self.assertEqual(sym(0x51), 0xFF54)   # Down
        self.assertEqual(sym(0x52), 0xFF52)   # Up
        self.assertEqual(sym(0x3A), 0xFFBE)   # F1
        self.assertEqual(sym(0x45), 0xFFC9)   # F12
        self.assertEqual(sym(0x4C), 0xFFFF)   # Delete
        self.assertEqual(sym(0x4A), 0xFF50)   # Home


@unittest.skipUnless(helpers.HAVE_DISPLAY, "no X display")
class InjectorTests(unittest.TestCase):
    def setUp(self):
        self.d = x11.Display()
        self.addCleanup(self.d.close)
        self.inj = Injector()
        self.addCleanup(self.inj.close)
        self.start = self.d.query_pointer()[:2]
        self.addCleanup(self._restore_pointer)

    def _restore_pointer(self):
        self.d.fake_motion(*self.start)
        self.d.sync()

    def _pointer(self):
        return self.d.query_pointer()[:2]

    def test_move_abs_hits_the_exact_pixel(self):
        mon = self.d.monitors()[0]
        self.inj.set_region(mon.x, mon.y, mon.width, mon.height)
        for nx, ny in ((0, 0), (65535, 65535), (32768, 32768), (10000, 50000)):
            want = (P.norm_to_pixel(nx, mon.x, mon.width), P.norm_to_pixel(ny, mon.y, mon.height))
            self.inj.apply([P.MoveAbs(nx, ny)])
            self.assertTrue(_wait(lambda: self._pointer() == want), f"{(nx, ny)} → {self._pointer()} != {want}")
            self.assertTrue(self.inj.was_recent_abs(*want))
        self.assertEqual(P.norm_to_pixel(65535, mon.x, mon.width), mon.x + mon.width - 1)

    def test_move_abs_respects_region_origin(self):
        mon = self.d.monitors()[0]
        # a sub-rectangle stands in for a second monitor at an offset
        rx, ry, rw, rh = mon.x + 200, mon.y + 100, 640, 480
        self.inj.set_region(rx, ry, rw, rh)
        self.inj.apply([P.MoveAbs(0, 0)])
        self.assertTrue(_wait(lambda: self._pointer() == (rx, ry)))
        self.inj.apply([P.MoveAbs(65535, 65535)])
        self.assertTrue(_wait(lambda: self._pointer() == (rx + rw - 1, ry + rh - 1)))

    def test_move_rel(self):
        mon = self.d.monitors()[0]
        self.inj.set_region(mon.x, mon.y, mon.width, mon.height)
        self.inj.apply([P.MoveAbs(32768, 32768)])
        base = (P.norm_to_pixel(32768, mon.x, mon.width), P.norm_to_pixel(32768, mon.y, mon.height))
        self.assertTrue(_wait(lambda: self._pointer() == base))
        self.inj.apply([P.MoveRel(25, -40), P.MoveRel(-5, 10)])
        want = (base[0] + 20, base[1] - 30)
        self.assertTrue(_wait(lambda: self._pointer() == want), f"{self._pointer()} != {want}")
        self.assertFalse(self.inj.was_recent_abs(*want))

    def test_modifier_key_press_release_and_release_all(self):
        shift = hid_to_x11_keycode(0xE1)
        self.assertNotIn(shift, self.d.pressed_keycodes(), "Left Shift is physically held; cannot test")
        try:
            self.inj.apply([P.Key(0xE1, True)])
            self.assertTrue(_wait(lambda: shift in self.d.pressed_keycodes()))
            self.assertEqual(self.inj.held_keys, frozenset({shift}))
            self.inj.apply([P.Key(0xE1, False)])
            self.assertTrue(_wait(lambda: shift not in self.d.pressed_keycodes()))
            self.assertEqual(self.inj.held_keys, frozenset())

            self.inj.apply([P.Key(0xE1, True)])
            self.assertTrue(_wait(lambda: shift in self.d.pressed_keycodes()))
            self.inj.apply([P.ReleaseAll()])
            self.assertTrue(_wait(lambda: shift not in self.d.pressed_keycodes()))
            self.assertEqual(self.inj.held_keys, frozenset())
        finally:
            self.inj.release_all()

    def test_release_of_a_key_we_never_pressed_is_ignored(self):
        self.inj.apply([P.Key(0xE1, False), P.Key(0x04, False)])
        self.assertEqual(self.inj.held_keys, frozenset())

    def test_unmapped_usages_and_buttons_are_ignored(self):
        self.inj.key(0x00, True)
        self.inj.key(0xFFFF, True)
        self.inj.button(9, True)       # only 1–3 are client-controllable
        self.inj.button(4, True)       # scroll buttons cannot be held by the client
        self.assertEqual(self.inj.held_keys, frozenset())
        self.assertEqual(self.inj.held_buttons, frozenset())

    def test_text_plan_uses_the_live_layout(self):
        """Planning only — nothing is typed into the live session."""
        plan = dict(self.inj.plan_text("a"))
        lo, table = self.d.keyboard_mapping()
        group, _ = self.d.xkb_state()
        col = 2 * group if group in (0, 1) else 0
        stroke = plan[0x61]
        if stroke is not None:  # the active layout has a plain 'a'
            self.assertIn(0x61, table[stroke.keycode - lo][col:col + 2])
        for keysym, stroke in self.inj.plan_text("Hello, World! 123\n"):
            if stroke is not None:
                row = table[stroke.keycode - lo]
                self.assertEqual(row[col + (1 if stroke.shift else 0)], keysym)
        # characters no keyboard layout carries need a scratch keycode
        self.assertEqual(self.inj.plan_text("🌍"), [(0x0101F30D, None)])
        self.assertEqual(self.inj.scratch_bindings, {})   # planning binds nothing

    def test_scratch_pool_candidates_are_really_unused(self):
        self.inj._prepare_scratch()
        lo, table = self.d.keyboard_mapping()
        self.assertGreaterEqual(len(self.inj._scratch_free), 1)
        for keycode in self.inj._scratch_free:
            self.assertGreater(keycode, 8)
            self.assertFalse(any(table[keycode - lo]), keycode)


@unittest.skipUnless(helpers.HAVE_DISPLAY, "no X display")
class CursorTrackerTests(unittest.TestCase):
    def test_reports_shape_and_motion(self):
        d = x11.Display()
        self.addCleanup(d.close)
        start = d.query_pointer()[:2]
        shapes, positions = [], []
        tracker = CursorTracker(None, on_shape=shapes.append, on_position=lambda x, y: positions.append((x, y)))
        tracker.start()
        try:
            self.assertTrue(_wait(lambda: shapes and positions, 2.0))
            self.assertEqual(positions[0], start)
            self.assertEqual(len(shapes[0].rgba), shapes[0].width * shapes[0].height * 4)

            mon = d.monitors()[0]
            target = (mon.x + mon.width // 3, mon.y + mon.height // 3)
            d.fake_motion(*target)
            d.sync()
            self.assertTrue(_wait(lambda: positions[-1] == target, 2.0), positions[-3:])

            count = len(shapes)
            tracker.resend()
            self.assertTrue(_wait(lambda: len(shapes) > count, 2.0))
        finally:
            tracker.stop()
            tracker.join(2.0)
            d.fake_motion(*start)
            d.sync()
        self.assertFalse(tracker.is_alive())


if __name__ == "__main__":
    unittest.main()
