"""The shared clipboard: the message codec, the server's handling of it (with a stand-in
clipboard, so nothing touches the real one), and — opt-in — the real X11 implementation
inside a private nested X server, checked from the outside with GTK as a second,
independent clipboard client.

    FOLDTOP_TEST_XEPHYR=1 python3 -m unittest tests.test_clipboard -v
"""

import asyncio
import os
import shutil
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from . import helpers
from foldtop_host import clipboard, server
from foldtop_host import protocol as P
from foldtop_host.protocol import Msg

CLIP_TOOL = [sys.executable, str(Path(__file__).resolve().parent / "clip_tool.py")]


def _have_gtk() -> bool:
    try:
        import gi

        gi.require_version("Gtk", "3.0")
        return True
    except Exception:
        return False


XEPHYR = (os.environ.get("FOLDTOP_TEST_XEPHYR") == "1" and bool(os.environ.get("DISPLAY"))
          and all(shutil.which(t) for t in ("Xephyr", "xev", "setxkbmap", "stdbuf")) and _have_gtk())


async def _eventually(predicate, timeout=3.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# --------------------------------------------------------------------------- codec

class CodecTests(unittest.TestCase):
    def test_round_trip(self):
        for flags, text in ((0, "plain"), (P.CLIPBOARD_FLAG_PASTE, "naïve café → 日本語 🌍\n\tline 2")):
            payload = P.pack_clipboard(flags, text)
            self.assertEqual(payload[0], flags)
            self.assertEqual(payload[1:], text.encode("utf-8"))
            self.assertEqual(P.unpack_clipboard(payload), (flags, text))

    def test_size_limits(self):
        largest = "x" * P.MAX_CLIPBOARD_BYTES
        self.assertEqual(len(P.pack_clipboard(0, largest)), P.MAX_CLIPBOARD_BYTES + 1)
        with self.assertRaises(P.ProtocolError):
            P.pack_clipboard(0, largest + "x")
        with self.assertRaises(P.ProtocolError):
            P.pack_clipboard(0, "")
        # the limit is in bytes, not characters
        with self.assertRaises(P.ProtocolError):
            P.pack_clipboard(0, "é" * (P.MAX_CLIPBOARD_BYTES // 2 + 1))

    def test_malformed(self):
        with self.assertRaises(P.ProtocolError):
            P.unpack_clipboard(b"")
        with self.assertRaises(P.ProtocolError):
            P.unpack_clipboard(b"\x00\xff\xfe")
        self.assertEqual(P.unpack_clipboard(b"\x01"), (1, ""))      # no text: for the receiver to ignore

    def test_paste_keystrokes(self):
        down_up = lambda *usages: ([P.Key(u, True) for u in usages]                       # noqa: E731
                                   + [P.Key(u, False) for u in reversed(usages)])
        self.assertEqual(server.paste_events(None), down_up(0xE1, 0x49))                  # Shift+Insert
        self.assertEqual(server.paste_events("shift+insert"), down_up(0xE1, 0x49))
        self.assertEqual(server.paste_events("Ctrl+V"), down_up(0xE0, 0x19))
        self.assertEqual(server.paste_events("ctrl + shift + v"), down_up(0xE0, 0xE1, 0x19))
        self.assertEqual(server.paste_events("something else"), down_up(0xE1, 0x49))


# --------------------------------------------------------------------------- without a display

class NoDisplayTests(unittest.TestCase):
    def test_a_clipboard_that_cannot_start_says_so_and_leaves_nothing_open(self):
        def open_fds():
            return len(os.listdir("/proc/self/fd"))

        before = open_fds()
        for _ in range(20):
            shared = clipboard.Clipboard(":4999", lambda _text: None)      # no such display
            with self.assertRaises(clipboard.ClipboardError):
                shared.start()
            self.assertFalse(shared.running)
            self.assertFalse(shared.set_text("x"))
            shared.stop()
        self.assertLessEqual(open_fds(), before + 1)


# --------------------------------------------------------------------------- server, stand-in clipboard

class FakeClipboard:
    """Stands in for the X11 clipboard so these tests never touch the real one."""

    def __init__(self) -> None:
        self.running = True
        self.calls: list[tuple[str, bool]] = []
        self.accept = True

    def set_text(self, text: str, primary: bool = False, timeout: float = 3.0) -> bool:
        self.calls.append((text, primary))
        return self.accept

    def stop(self) -> None:
        self.running = False


class ServerClipboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = helpers.ServerHarness()
        await self.h.__aenter__()
        self.addAsyncCleanup(self.h.__aexit__)
        self.fake = FakeClipboard()
        self.h.server.clipboard = self.fake
        self.inputs: list[tuple] = []
        real_submit = self.h.server.submit_input

        def submit(fn_name, *args):
            if fn_name == "apply":
                self.inputs.append(tuple(args[0]))       # never type on the real desktop
            elif fn_name != "set_region":
                real_submit(fn_name, *args)

        self.h.server.submit_input = submit
        self.clients = []
        self.addAsyncCleanup(self._close_clients)

    async def _close_clients(self):
        for c in self.clients:
            await c.close()

    async def client(self, name="phone"):
        c, creds = await self.h.paired_client(name)
        self.clients.append(c)
        return c, creds

    async def test_host_announces_the_clipboard(self):
        c, _ = await self.client()
        self.assertIs(c.auth_ok.get("clipboard"), True)

    async def test_text_from_the_phone_is_put_on_the_clipboard(self):
        c, _ = await self.client()
        c.send_clipboard("copied on the phone ✓")
        self.assertTrue(await _eventually(lambda: self.fake.calls == [("copied on the phone ✓", False)]))
        await asyncio.sleep(0.1)
        self.assertEqual(self.inputs, [])                 # no keystroke unless a paste was asked for
        self.assertEqual(self.h.server.active.clipboard_received, 1)

    async def test_paste_sets_both_selections_then_presses_the_paste_keys(self):
        c, _ = await self.client()
        c.send_clipboard("pasted", paste=True)
        self.assertTrue(await _eventually(lambda: len(self.inputs) == 1))
        self.assertEqual(self.fake.calls, [("pasted", True)])
        self.assertEqual(list(self.inputs[0]), server.paste_events(None))

    async def test_paste_keys_follow_the_configuration(self):
        self.h.server.config.paste_keys = "ctrl+shift+v"
        c, _ = await self.client()
        c.send_clipboard("x", paste=True)
        self.assertTrue(await _eventually(lambda: len(self.inputs) == 1))
        self.assertEqual(list(self.inputs[0]), server.paste_events("ctrl+shift+v"))

    async def test_nothing_is_pasted_when_the_clipboard_could_not_be_set(self):
        self.fake.accept = False
        c, _ = await self.client()
        c.send_clipboard("x", paste=True)
        self.assertTrue(await _eventually(lambda: len(self.fake.calls) == 1))
        await asyncio.sleep(0.1)
        self.assertEqual(self.inputs, [])
        self.assertEqual(self.h.server.active.clipboard_received, 0)

    async def test_empty_and_oversized_text_is_ignored_and_the_session_survives(self):
        c, _ = await self.client()
        c.send_raw(Msg.CLIPBOARD, b"\x01")                                             # no text
        c.send_raw(Msg.CLIPBOARD, b"\x00" + b"x" * (P.MAX_CLIPBOARD_BYTES + 1))        # too long
        value = c.ping(7)
        self.assertTrue(await _eventually(lambda: c.pongs == [value], timeout=5))
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.inputs, [])
        self.assertIsNone(c.bye)

    async def test_malformed_clipboard_is_a_protocol_error(self):
        for payload in (b"", b"\x00\xff\xfe"):
            c, _ = await self.client()
            c.send_raw(Msg.CLIPBOARD, payload)
            self.assertTrue(await _eventually(c.eof.is_set), payload)
            self.assertEqual(c.bye, {"reason": "protocol"})
        self.assertEqual(self.fake.calls, [])

    async def test_workstation_copies_reach_the_phone_only_when_asked_for(self):
        c, _ = await self.client()
        srv = self.h.server
        srv._clipboard_copied("before asking")            # as the clipboard thread would
        await asyncio.sleep(0.1)
        self.assertEqual(c.clipboard, [])

        c.clipboard_control(receive=True)
        self.assertTrue(await _eventually(lambda: srv.active.clipboard_receive))
        srv._clipboard_copied("copied on the workstation ✓")
        self.assertTrue(await _eventually(lambda: c.clipboard == [(0, "copied on the workstation ✓")]))

        c.clipboard_control(receive=False)
        self.assertTrue(await _eventually(lambda: not srv.active.clipboard_receive))
        srv._clipboard_copied("after switching off")
        await asyncio.sleep(0.1)
        self.assertEqual(len(c.clipboard), 1)

    async def test_copies_go_to_the_active_client_only_and_start_off_on_every_connection(self):
        first, creds = await self.client("phone-a")
        first.clipboard_control(receive=True)
        self.assertTrue(await _eventually(lambda: self.h.server.active.clipboard_receive))
        second, _ = await self.client("phone-b")          # takes over; has not asked
        self.assertTrue(await _eventually(first.eof.is_set))
        self.h.server._clipboard_copied("secret")
        await asyncio.sleep(0.1)
        self.assertEqual(first.clipboard, [])
        self.assertEqual(second.clipboard, [])

    async def test_oversized_workstation_copy_is_not_sent(self):
        c, _ = await self.client()
        c.clipboard_control(receive=True)
        self.assertTrue(await _eventually(lambda: self.h.server.active.clipboard_receive))
        self.h.server._clipboard_copied("x" * (P.MAX_CLIPBOARD_BYTES + 1))
        self.h.server._clipboard_copied("fits")
        self.assertTrue(await _eventually(lambda: c.clipboard == [(0, "fits")]))

    async def test_status_reports_the_clipboard(self):
        c, _ = await self.client()
        c.clipboard_control(receive=True)
        c.send_clipboard("one")
        self.assertTrue(await _eventually(lambda: self.h.server.active.clipboard_received == 1))
        info = self.h.server.active.describe()
        self.assertIs(info["clipboard"], True)
        self.assertEqual((info["clipboardReceived"], info["clipboardSent"]), (1, 0))


class ClipboardSwitchedOffTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_host_with_the_clipboard_off_neither_offers_nor_accepts_it(self):
        async with helpers.ServerHarness(clipboard=False) as h:
            inputs = []
            h.server.submit_input = lambda fn_name, *args: inputs.append(fn_name) if fn_name == "apply" else None
            c, _ = await h.paired_client()
            try:
                self.assertIs(c.auth_ok.get("clipboard"), False)
                c.clipboard_control(receive=True)
                c.send_clipboard("x", paste=True)
                value = c.ping(9)
                self.assertTrue(await _eventually(lambda: c.pongs == [value], timeout=5))
                self.assertIsNone(h.server.clipboard)      # never even started
                self.assertFalse(h.server.active.clipboard_receive)
                self.assertEqual(inputs, [])
            finally:
                await c.close()


# --------------------------------------------------------------------------- the real thing, in Xephyr

def _tool(display, *args, text=None, timeout=20):
    env = dict(os.environ, DISPLAY=display)
    return subprocess.run(CLIP_TOOL + list(args), env=env, input=text, capture_output=True, text=True,
                          timeout=timeout)


class _GtkOwner:
    """Another application that has copied something (and stays around to hand it over)."""

    def __init__(self, display, text, hold=4.0, extra=()):
        env = dict(os.environ, DISPLAY=display)
        self.proc = subprocess.Popen(CLIP_TOOL + ["set", "--hold", str(hold), *extra], env=env,
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
        self.proc.stdin.write(text)
        self.proc.stdin.close()
        assert self.proc.stdout.readline().strip() == "owned"

    def close(self):
        self.proc.terminate()
        try:
            self.proc.wait(3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.proc.stdout.close()


@unittest.skipUnless(XEPHYR, "set FOLDTOP_TEST_XEPHYR=1 (opens a nested X server window; needs GTK 3)")
class XephyrClipboardTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from foldtop_host import x11

        cls.display = None
        for number in range(110, 125):
            if os.path.exists(f"/tmp/.X11-unix/X{number}") or os.path.exists(f"/tmp/.X{number}-lock"):
                continue
            cls.display = f":{number}"
            break
        assert cls.display, "no free X display number"
        cls.server = subprocess.Popen(
            ["Xephyr", cls.display, "-screen", "320x240", "-ac", "-br", "-noreset", "-title",
             "Foldtop clipboard tests"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 10
        probe = None
        while time.monotonic() < deadline:
            try:
                probe = x11.Display(cls.display)
                break
            except x11.X11Error:
                time.sleep(0.1)
        if probe is None:
            cls.server.kill()
            raise unittest.SkipTest("Xephyr did not start")
        probe.close()

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "server", None):
            cls.server.terminate()
            try:
                cls.server.wait(5)
            except subprocess.TimeoutExpired:
                cls.server.kill()

    def setUp(self):
        self.copied: list[str] = []
        self.signal = threading.Event()

        def on_change(text):
            self.copied.append(text)
            self.signal.set()

        self.clip = clipboard.Clipboard(self.display, on_change)
        self.clip.start()
        self.addCleanup(self.clip.stop)
        self.owners = []
        self.addCleanup(lambda: [o.close() for o in self.owners])

    def copy_elsewhere(self, text, **kw):
        self.signal.clear()
        owner = _GtkOwner(self.display, text, **kw)
        self.owners.append(owner)
        return owner

    def wait_copied(self, timeout=6.0):
        return self.signal.wait(timeout)

    # -- phone → workstation ---------------------------------------------------

    def test_another_application_pastes_the_phones_text(self):
        text = "Hello from the phone — naïve café 日本語 🌍\nline 2\ttabbed"
        self.assertTrue(self.clip.set_text(text))
        got = _tool(self.display, "get")
        self.assertEqual((got.returncode, got.stdout), (0, text))
        self.assertEqual(self.clip.owned_text(), text)
        self.assertEqual(self.copied, [])                  # our own text is not "a copy on the workstation"

    def test_formats_offered(self):
        self.assertTrue(self.clip.set_text("x"))
        offered = _tool(self.display, "targets").stdout.split()
        for wanted in ("UTF8_STRING", "STRING", "TEXT", "text/plain", "text/plain;charset=utf-8", "TARGETS"):
            self.assertIn(wanted, offered)

    def test_primary_is_only_taken_for_a_paste(self):
        self.assertTrue(self.clip.set_text("clipboard only"))
        self.assertEqual(_tool(self.display, "get", "--primary").returncode, 3)
        self.assertTrue(self.clip.set_text("both", primary=True))
        self.assertEqual(_tool(self.display, "get", "--primary").stdout, "both")
        self.assertEqual(_tool(self.display, "get").stdout, "both")

    def test_a_megabyte_survives_and_more_is_refused(self):
        big = ("0123456789abcdef" * 65536)[:clipboard.MAX_TEXT_BYTES]
        self.assertTrue(self.clip.set_text(big))
        self.assertEqual(_tool(self.display, "get").stdout, big)
        self.assertFalse(self.clip.set_text(big + "x"))
        self.assertFalse(self.clip.set_text(""))
        self.assertEqual(_tool(self.display, "get").stdout, big)      # still the last good text

    def test_replacing_the_text(self):
        self.assertTrue(self.clip.set_text("first"))
        self.assertTrue(self.clip.set_text("second"))
        self.assertEqual(_tool(self.display, "get").stdout, "second")

    def test_stopping_gives_the_clipboard_up(self):
        self.assertTrue(self.clip.set_text("temporary"))
        self.clip.stop()
        self.assertEqual(_tool(self.display, "get").returncode, 3)
        self.assertFalse(self.clip.set_text("after stop"))

    # -- workstation → phone ---------------------------------------------------

    def test_a_copy_on_the_workstation_is_reported(self):
        self.copy_elsewhere("copied on the workstation ✓ é 日本")
        self.assertTrue(self.wait_copied())
        self.assertEqual(self.copied, ["copied on the workstation ✓ é 日本"])

    def test_losing_the_clipboard_to_another_application(self):
        self.assertTrue(self.clip.set_text("from the phone"))
        self.copy_elsewhere("from the workstation")
        self.assertTrue(self.wait_copied())
        self.assertIsNone(self.clip.owned_text())
        self.assertEqual(_tool(self.display, "get").stdout, "from the workstation")
        # and the phone can take it back
        self.assertTrue(self.clip.set_text("phone again"))
        self.assertEqual(_tool(self.display, "get").stdout, "phone again")

    def test_a_large_copy_arrives_in_pieces(self):
        # GTK hands over anything above 256 KiB incrementally (INCR).
        text = "".join(f"{i:07d} some text on a line\n" for i in range(20000))
        self.assertGreater(len(text), 500_000)
        self.copy_elsewhere(text, hold=8.0)
        self.assertTrue(self.wait_copied(10.0))
        self.assertEqual(len(self.copied), 1)
        self.assertEqual(self.copied[0], text)

    def test_a_copy_that_is_too_large_is_not_reported_and_later_copies_still_are(self):
        self.copy_elsewhere("y" * (clipboard.MAX_TEXT_BYTES + 4096), hold=8.0)
        self.assertFalse(self.wait_copied(4.0))
        self.copy_elsewhere("small again")
        self.assertTrue(self.wait_copied())
        self.assertEqual(self.copied, ["small again"])

    def test_a_clipboard_manager_taking_over_the_phones_text_is_not_news(self):
        self.assertTrue(self.clip.set_text("from the phone"))
        self.copy_elsewhere("from the phone")               # what a clipboard manager does
        self.assertFalse(self.wait_copied(1.5))
        self.copy_elsewhere("something new")
        self.assertTrue(self.wait_copied())
        self.assertEqual(self.copied, ["something new"])

    def test_the_same_text_copied_twice_in_a_row_is_reported_once(self):
        self.copy_elsewhere("twice")
        self.assertTrue(self.wait_copied())
        self.copy_elsewhere("twice")
        self.assertFalse(self.wait_copied(1.0))
        self.assertEqual(self.copied, ["twice"])

    def test_text_echoed_back_does_not_take_the_clipboard_from_the_application_that_copied_it(self):
        self.copy_elsewhere("copied here, with formats only its owner has", hold=8.0)
        self.assertTrue(self.wait_copied())
        gtk_only = "COMPOUND_TEXT"      # GTK offers it; Foldtop does not
        self.assertIn(gtk_only, _tool(self.display, "targets").stdout.split())

        # a client sends the same text straight back
        self.assertTrue(self.clip.set_text(self.copied[0]))
        self.assertIn(gtk_only, _tool(self.display, "targets").stdout.split())
        self.assertIsNone(self.clip.owned_text())

        # a paste of that text still needs PRIMARY (for terminals), and only that
        self.assertTrue(self.clip.set_text(self.copied[0], primary=True))
        self.assertEqual(_tool(self.display, "get", "--primary").stdout, self.copied[0])
        self.assertIn(gtk_only, _tool(self.display, "targets").stdout.split())

        # different text is a real copy on the phone and does take over
        self.assertTrue(self.clip.set_text("something else"))
        self.assertNotIn(gtk_only, _tool(self.display, "targets").stdout.split())
        self.assertEqual(_tool(self.display, "get").stdout, "something else")
        # …after which the first text is no longer "already there"
        self.assertTrue(self.clip.set_text(self.copied[0]))
        self.assertEqual(self.clip.owned_text(), self.copied[0])

    def test_a_copy_with_no_text_is_ignored(self):
        self.copy_elsewhere("", extra=("--image",))
        self.assertFalse(self.wait_copied(1.5))
        self.copy_elsewhere("text after an image")
        self.assertTrue(self.wait_copied())
        self.assertEqual(self.copied, ["text after an image"])


@unittest.skipUnless(XEPHYR and helpers.HAVE_GST, "set FOLDTOP_TEST_XEPHYR=1 (nested X server, GTK 3, GStreamer)")
class XephyrPasteTests(unittest.IsolatedAsyncioTestCase):
    """The whole path: a client's CLIPBOARD message → the daemon → a real X server."""

    @classmethod
    def setUpClass(cls):
        from .test_xephyr import XevWatcher

        XephyrClipboardTests.setUpClass.__func__(cls)
        # Give the nested server the same keycode scheme as a real session (evdev).
        subprocess.run(["setxkbmap", "-display", cls.display, "-rules", "evdev", "-model", "pc105", "-layout", "us"],
                       check=True, capture_output=True)
        time.sleep(0.3)
        cls.watcher = XevWatcher(cls.display)
        time.sleep(0.5)
        cls.watcher.drain(settle=0.3)

    @classmethod
    def tearDownClass(cls):
        cls.watcher.stop()
        XephyrClipboardTests.tearDownClass.__func__(cls)

    def setUp(self):
        self.watcher.drain(settle=0.2)       # blocking, so not in asyncSetUp

    async def asyncSetUp(self):
        self.h = helpers.ServerHarness(display=self.display)
        await self.h.__aenter__()
        self.addAsyncCleanup(self.h.__aexit__)
        self.client, _ = await self.h.paired_client("phone")
        self.addAsyncCleanup(self.client.close)

    async def test_paste_from_the_phone(self):
        loop = asyncio.get_running_loop()
        self.assertIs(self.client.auth_ok.get("clipboard"), True)
        text = "pasted from the phone ✓"
        self.client.send_clipboard(text, paste=True)
        self.assertTrue(await _eventually(lambda: self.h.server.active.clipboard_received == 1, timeout=5))

        events = await loop.run_in_executor(None, lambda: self.watcher.drain(settle=0.5))
        keys = [(e["kind"], e["name"]) for e in events if e["kind"].startswith("Key")]
        self.assertEqual(keys, [("KeyPress", "Shift_L"), ("KeyPress", "Insert"),
                                ("KeyRelease", "Insert"), ("KeyRelease", "Shift_L")])
        insert = next(e for e in events if e["kind"] == "KeyPress" and e["name"] == "Insert")
        self.assertTrue(insert["state"] & 0x1)             # Shift was down when Insert arrived

        for args in (("get",), ("get", "--primary")):
            got = await loop.run_in_executor(None, lambda a=args: _tool(self.display, *a))
            self.assertEqual((got.returncode, got.stdout), (0, text), args)

    async def test_copy_on_the_workstation_reaches_the_client(self):
        loop = asyncio.get_running_loop()
        self.client.clipboard_control(receive=True)
        self.assertTrue(await _eventually(lambda: self.h.server.active.clipboard_receive))
        owner = await loop.run_in_executor(None, lambda: _GtkOwner(self.display, "copied on the workstation ✓"))
        self.addCleanup(owner.close)
        self.assertTrue(await _eventually(lambda: self.client.clipboard == [(0, "copied on the workstation ✓")],
                                          timeout=6))
        # what the phone itself sent does not come back to it
        self.client.send_clipboard("from the phone")
        self.assertTrue(await _eventually(lambda: self.h.server.active.clipboard_received == 1, timeout=5))
        await asyncio.sleep(0.5)
        self.assertEqual(len(self.client.clipboard), 1)


if __name__ == "__main__":
    unittest.main()
