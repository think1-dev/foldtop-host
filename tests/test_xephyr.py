"""Full input-injection tests against a private nested X server (Xephyr).

Everything that must never be done to a live desktop — typing, clicking, scrolling,
rebinding keycodes — happens inside the nested server and is verified from the
receiving side with `xev`, i.e. as a real X client sees it.

Opt-in, because Xephyr opens a (small) window on the current display:

    FOLDTOP_TEST_XEPHYR=1 python3 -m unittest tests.test_xephyr -v
"""

import os
import queue
import re
import shutil
import subprocess
import threading
import time
import unittest

from . import helpers  # noqa: F401
from foldtop_host import protocol as P
from foldtop_host.keymap import hid_to_x11_keycode, text_keysyms

ENABLED = (os.environ.get("FOLDTOP_TEST_XEPHYR") == "1" and bool(os.environ.get("DISPLAY"))
           and all(shutil.which(t) for t in ("Xephyr", "xev", "setxkbmap", "stdbuf")))

_KEY = re.compile(r"state 0x([0-9a-f]+), keycode (\d+) \(keysym 0x([0-9a-f]+), ([^)]*)\)")
_BUTTON = re.compile(r"state 0x([0-9a-f]+), button (\d+),")
_POS = re.compile(r"root:\((-?\d+),(-?\d+)\)")
_MB = re.compile(r'XmbLookupString gives (\d+) bytes: \(([0-9a-f ]*)\)')


class XevWatcher:
    """Parses `xev -root` output into (kind, detail) events."""

    def __init__(self, display: str) -> None:
        env = dict(os.environ, DISPLAY=display, LC_ALL="C.UTF-8")
        self.proc = subprocess.Popen(["stdbuf", "-oL", "xev", "-root", "-display", display],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env)
        self.events: "queue.Queue[dict]" = queue.Queue()
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self) -> None:
        # xev prints a blank line *before* each event, so an event is complete when its
        # own last line arrives, not when the next one starts.
        current = None
        for line in self.proc.stdout:
            line = line.rstrip("\n")
            head = line.split(" event,")[0] if " event," in line else None
            if head is not None:
                current = {"kind": head, "text": b""} if head in (
                    "KeyPress", "KeyRelease", "ButtonPress", "ButtonRelease", "MotionNotify") else None
            if current is None:
                continue
            m = _POS.search(line)
            if m:
                current["pos"] = (int(m.group(1)), int(m.group(2)))
            m = _KEY.search(line)
            if m:
                current.update(state=int(m.group(1), 16), keycode=int(m.group(2)),
                               keysym=int(m.group(3), 16), name=m.group(4).strip())
            m = _BUTTON.search(line)
            if m:
                current.update(state=int(m.group(1), 16), button=int(m.group(2)))
            m = _MB.search(line)
            if m and int(m.group(1)) > 0:
                current["text"] = bytes.fromhex(m.group(2).replace(" ", ""))
            is_key = current["kind"].startswith("Key")
            if ("XFilterEvent returns" in line) if is_key else ("same_screen" in line):
                self.events.put(current)
                current = None

    def drain(self, settle: float = 0.25, timeout: float = 5.0) -> list[dict]:
        """Everything received until the stream has been quiet for `settle` seconds."""
        out = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                out.append(self.events.get(timeout=settle))
            except queue.Empty:
                break
        return out

    def stop(self) -> None:
        self.proc.terminate()
        try:
            self.proc.wait(2)
        except subprocess.TimeoutExpired:
            self.proc.kill()


@unittest.skipUnless(ENABLED, "set FOLDTOP_TEST_XEPHYR=1 (opens a nested X server window)")
class XephyrInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from foldtop_host import x11

        cls.display = None
        for number in range(97, 110):
            if os.path.exists(f"/tmp/.X11-unix/X{number}") or os.path.exists(f"/tmp/.X{number}-lock"):
                continue
            cls.display = f":{number}"
            break
        assert cls.display, "no free X display number"
        cls.server = subprocess.Popen(
            ["Xephyr", cls.display, "-screen", "640x480", "-ac", "-br", "-noreset", "-title", "Foldtop input tests"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        cls.x = None
        while time.monotonic() < deadline:
            try:
                cls.x = x11.Display(cls.display)
                break
            except x11.X11Error:
                time.sleep(0.1)
        if cls.x is None:
            cls.server.kill()
            raise unittest.SkipTest("Xephyr did not start")
        # Give the nested server the same keycode scheme as a real session (evdev).
        subprocess.run(["setxkbmap", "-display", cls.display, "-rules", "evdev", "-model", "pc105", "-layout", "us"],
                       check=True, capture_output=True)
        time.sleep(0.3)
        cls.watcher = XevWatcher(cls.display)
        time.sleep(0.5)
        cls.watcher.drain(settle=0.3)

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "watcher", None):
            cls.watcher.stop()
        if getattr(cls, "x", None):
            cls.x.close()
        if getattr(cls, "server", None):
            cls.server.terminate()
            try:
                cls.server.wait(5)
            except subprocess.TimeoutExpired:
                cls.server.kill()

    def setUp(self):
        from foldtop_host.inject import Injector

        self.inj = Injector(self.display)
        self.addCleanup(self.inj.close)
        self.inj.set_region(0, 0, 640, 480)
        self.inj.apply([P.MoveAbs(32768, 32768)])
        self.watcher.drain(settle=0.2)

    # -- helpers ---------------------------------------------------------------

    def presses(self, events):
        return [e for e in events if e["kind"] == "KeyPress"]

    def typed(self, text: str) -> list[dict]:
        self.inj.apply([P.Text(text)])
        return self.presses(self.watcher.drain(settle=0.4, timeout=20))

    # -- keys --------------------------------------------------------------------

    def test_hid_keys_arrive_as_the_right_keysyms(self):
        cases = {
            0x04: 0x61, 0x1D: 0x7A, 0x1E: 0x31, 0x27: 0x30, 0x28: 0xFF0D, 0x29: 0xFF1B, 0x2A: 0xFF08,
            0x2B: 0xFF09, 0x2C: 0x20, 0x2D: 0x2D, 0x2E: 0x3D, 0x2F: 0x5B, 0x30: 0x5D, 0x31: 0x5C,
            0x33: 0x3B, 0x34: 0x27, 0x35: 0x60, 0x36: 0x2C, 0x37: 0x2E, 0x38: 0x2F,
            0x3A: 0xFFBE, 0x45: 0xFFC9, 0x49: 0xFF63, 0x4A: 0xFF50, 0x4B: 0xFF55, 0x4C: 0xFFFF,
            0x4D: 0xFF57, 0x4E: 0xFF56, 0x4F: 0xFF53, 0x50: 0xFF51, 0x51: 0xFF54, 0x52: 0xFF52,
            0xE0: 0xFFE3, 0xE1: 0xFFE1, 0xE2: 0xFFE9, 0xE3: 0xFFEB, 0xE4: 0xFFE4, 0xE5: 0xFFE2,
        }
        for usage, keysym in cases.items():
            self.inj.apply([P.Key(usage, True), P.Key(usage, False)])
        events = self.watcher.drain(settle=0.4)
        got = [(e["keycode"], e["keysym"]) for e in self.presses(events)]
        want = [(hid_to_x11_keycode(u), k) for u, k in cases.items()]
        self.assertEqual(got, want)
        self.assertEqual(len([e for e in events if e["kind"] == "KeyRelease"]), len(cases))

    def test_shift_modifies_keys_and_is_tracked(self):
        self.inj.apply([P.Key(0xE1, True), P.Key(0x04, True), P.Key(0x04, False), P.Key(0x1E, True),
                        P.Key(0x1E, False), P.Key(0xE1, False), P.Key(0x04, True), P.Key(0x04, False)])
        syms = [e["keysym"] for e in self.presses(self.watcher.drain())]
        self.assertEqual(syms, [0xFFE1, 0x41, 0x21, 0x61])          # Shift, A, !, a
        self.assertEqual(self.inj.held_keys, frozenset())

    def test_release_all_releases_keys_and_buttons(self):
        self.inj.apply([P.Key(0xE0, True), P.Key(0x04, True), P.Button(1, True)])
        self.watcher.drain()
        held = self.x.pressed_keycodes()
        self.assertTrue({hid_to_x11_keycode(0xE0), hid_to_x11_keycode(0x04)} <= held)
        self.assertTrue(self.x.query_pointer()[2] & 0x100)           # Button1Mask
        self.inj.apply([P.ReleaseAll()])
        events = self.watcher.drain()
        self.assertEqual(sorted(e["keycode"] for e in events if e["kind"] == "KeyRelease"),
                         sorted([hid_to_x11_keycode(0xE0), hid_to_x11_keycode(0x04)]))
        self.assertEqual([e["button"] for e in events if e["kind"] == "ButtonRelease"], [1])
        self.assertEqual(self.x.pressed_keycodes(), set())
        self.assertFalse(self.x.query_pointer()[2] & 0x100)

    def test_close_releases_everything(self):
        from foldtop_host.inject import Injector

        inj = Injector(self.display)
        inj.apply([P.Key(0xE2, True), P.Button(3, True)])
        self.watcher.drain()
        self.assertIn(hid_to_x11_keycode(0xE2), self.x.pressed_keycodes())
        inj.close()
        self.watcher.drain()
        self.assertEqual(self.x.pressed_keycodes(), set())
        self.assertFalse(self.x.query_pointer()[2] & 0x400)          # Button3Mask

    # -- pointer -------------------------------------------------------------------

    def test_click_lands_where_the_pointer_was_put(self):
        self.inj.apply([P.MoveAbs(0, 0), P.Button(1, True), P.Button(1, False),
                        P.MoveAbs(65535, 65535), P.Button(3, True), P.Button(3, False),
                        P.MoveAbs(32768, 16384), P.Button(2, True), P.Button(2, False)])
        events = self.watcher.drain()
        presses = [(e["button"], e["pos"]) for e in events if e["kind"] == "ButtonPress"]
        self.assertEqual(presses, [(1, (0, 0)), (3, (639, 479)),
                                   (2, (P.norm_to_pixel(32768, 0, 640), P.norm_to_pixel(16384, 0, 480)))])
        self.assertEqual([e["button"] for e in events if e["kind"] == "ButtonRelease"], [1, 3, 2])

    def test_drag_keeps_the_button_down(self):
        self.inj.apply([P.MoveAbs(10000, 10000), P.Button(1, True), P.MoveRel(50, 30), P.MoveRel(10, 0)])
        events = self.watcher.drain()
        motions = [e for e in events if e["kind"] == "MotionNotify"]
        self.assertTrue(motions)
        start = (P.norm_to_pixel(10000, 0, 640), P.norm_to_pixel(10000, 0, 480))
        self.assertEqual(motions[-1]["pos"], (start[0] + 60, start[1] + 30))
        self.assertEqual(self.inj.held_buttons, frozenset({1}))
        self.inj.apply([P.Button(1, False)])
        self.assertEqual([e["button"] for e in self.watcher.drain() if e["kind"] == "ButtonRelease"], [1])

    def test_scroll_directions_and_fraction_accumulation(self):
        def clicks(*events):
            self.inj.apply(list(events))
            return [e["button"] for e in self.watcher.drain() if e["kind"] == "ButtonPress"]

        self.assertEqual(clicks(P.Scroll(0, 360)), [5, 5, 5])        # down
        self.assertEqual(clicks(P.Scroll(0, -120)), [4])             # up
        self.assertEqual(clicks(P.Scroll(240, 0)), [7, 7])           # right
        self.assertEqual(clicks(P.Scroll(-120, 0)), [6])             # left
        self.assertEqual(clicks(P.Scroll(0, 50), P.Scroll(0, 50)), [])
        self.assertEqual(clicks(P.Scroll(0, 30)), [5])               # 130/120 → one detent, 10 carried
        self.assertEqual(self.inj.held_buttons, frozenset())         # wheel clicks are never "held"

    # -- text ------------------------------------------------------------------------

    def test_plain_text(self):
        text = "Hello, World! 123 ~{}|<>?_+\n\tok"
        events = self.typed(text)
        self.assertEqual([e["keysym"] for e in events if e["keysym"] not in (0xFFE1, 0xFFE2)], text_keysyms(text))
        printable = b"".join(e["text"] for e in events)
        self.assertEqual(printable.decode("utf-8").replace("\r", "\n"), text)
        self.assertEqual(self.inj.scratch_bindings, {})               # the layout covered everything
        self.assertEqual(self.x.pressed_keycodes(), set())            # Shift was let go

    def test_characters_outside_the_layout_use_scratch_keycodes(self):
        text = "naïve café → 日本語 🌍"
        _lo, before = self.x.keyboard_mapping()
        events = self.typed(text)
        self.assertEqual([e["keysym"] for e in events if e["keysym"] not in (0xFFE1, 0xFFE2)], text_keysyms(text))
        self.assertEqual(b"".join(e["text"] for e in events).decode("utf-8"), text)
        bound = self.inj.scratch_bindings
        self.assertEqual(set(bound), {0xEF, 0xE9, 0x01002192, 0x010065E5, 0x0100672C, 0x01008A9E, 0x0101F30D})
        self.assertEqual(len(set(bound.values())), len(bound))
        self.inj.cleanup_idle(max_age=0.0)                             # give the keycodes back
        self.assertEqual(self.inj.scratch_bindings, {})
        _lo, after = self.x.keyboard_mapping()
        self.assertEqual(after, before, "the keyboard mapping was not restored")

    def test_more_distinct_characters_than_scratch_keycodes(self):
        text = "".join(chr(0x1F600 + i) for i in range(20)) + "αβγδ"   # 24 distinct, pool is 8
        events = self.typed(text)
        self.assertEqual([e["keysym"] for e in events], text_keysyms(text))
        self.assertEqual(b"".join(e["text"] for e in events).decode("utf-8"), text)
        self.assertLessEqual(len(self.inj.scratch_bindings), 8)

    def test_text_while_shift_is_held_and_with_caps_lock(self):
        # the client is holding Shift (e.g. a sticky modifier) when dictated text arrives
        self.inj.apply([P.Key(0xE1, True)])
        self.watcher.drain()
        events = self.typed("aB1!")
        self.assertEqual(b"".join(e["text"] for e in events).decode(), "aB1!")
        self.assertIn(hid_to_x11_keycode(0xE1), self.x.pressed_keycodes())   # and Shift is still held after
        self.inj.apply([P.Key(0xE1, False)])
        self.watcher.drain()

        caps = hid_to_x11_keycode(0x39)
        self.inj.apply([P.Key(0x39, True), P.Key(0x39, False)])        # Caps Lock on
        self.watcher.drain()
        try:
            self.assertTrue(self.x.xkb_state()[1] & 0x02, "Caps Lock did not engage in the nested server")
            events = self.typed("aB1!z")
            self.assertEqual(b"".join(e["text"] for e in events).decode(), "aB1!z")
        finally:
            self.inj.apply([P.Key(0x39, True), P.Key(0x39, False)])
            self.watcher.drain()
        self.assertFalse(self.x.xkb_state()[1] & 0x02)
        self.assertIsNotNone(caps)

    def test_crlf_and_untypable_characters(self):
        events = self.typed("a\r\nb\x00c")
        self.assertEqual([e["keysym"] for e in events], [0x61, 0xFF0D, 0x62, 0x63])


if __name__ == "__main__":
    unittest.main()
