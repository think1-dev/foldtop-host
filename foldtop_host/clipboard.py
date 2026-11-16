"""The workstation's clipboard, shared with the phone — CLIPBOARD in docs/PROTOCOL.md.

X11 has no clipboard that stores anything: an application that "copies" only announces
that it owns the CLIPBOARD selection, and hands the data over when another application
asks to paste. So putting the phone's text on the clipboard means owning that selection
and answering those requests for as long as nobody copies something else; and noticing
that something was copied on the workstation means being told (by XFIXES) that the
selection has a new owner, then asking that owner for the text.

One thread owns one X connection and does every X call on it; other threads hand it
work through a queue.
"""

from __future__ import annotations

import logging
import os
import queue
import select
import threading
import time
from typing import Callable, Optional

from . import x11

log = logging.getLogger("foldtop.clipboard")

MAX_TEXT_BYTES = 1_048_576      # the most text carried in either direction (protocol limit)
FETCH_TIMEOUT = 3.0             # an owner that does not answer a paste request in this time is given up on
REPEAT_WINDOW = 2.0             # the same text again within this time is one copy (clipboard managers re-own it)


class ClipboardError(RuntimeError):
    pass


class _Fetch:
    """Reading the clipboard from whichever application owns it."""

    def __init__(self) -> None:
        self.deadline = time.monotonic() + FETCH_TIMEOUT
        self.incremental = False
        self.chunks: list[bytes] = []
        self.size = 0
        self.too_large = False


class Clipboard:
    """Sets the X clipboard and reports when something else is copied.

    `on_change(text)` is called on the clipboard thread with what was copied on the
    workstation. It is not called for text set through `set_text`.
    """

    def __init__(self, display_name: Optional[str], on_change: Callable[[str], None]) -> None:
        self._display_name = display_name
        self._on_change = on_change
        self._thread: Optional[threading.Thread] = None
        self._commands: "queue.SimpleQueue[tuple[Callable[[], object], threading.Event, list]]" = queue.SimpleQueue()
        self._wake_r, self._wake_w = -1, -1    # a pipe that wakes the thread; open while it runs
        self._wake_lock = threading.Lock()
        self._stopping = threading.Event()
        self._started = threading.Event()
        self._start_error: Optional[str] = None

        # Everything below belongs to the clipboard thread.
        self._display: Optional[x11.Display] = None
        self._window = 0
        self._atoms: dict[str, int] = {}
        self._xfixes_base = 0
        self._text: Optional[str] = None       # what we serve while we own a selection
        self._data = b""
        self._owned: set[int] = set()
        self._fetch: Optional[_Fetch] = None
        self._fetch_again = False
        self._from_phone: Optional[str] = None  # the last text set through set_text
        self._reported: Optional[str] = None
        self._reported_at = 0.0
        self._reported_is_current = False       # the clipboard still holds exactly `_reported`

    # ------------------------------------------------------------------ public (any thread)

    @property
    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive() and self._started.is_set() and self._start_error is None

    def start(self) -> None:
        """Raises ClipboardError if the clipboard cannot be shared on this display."""
        if self._thread is not None:
            raise ClipboardError("already started")
        self._wake_r, self._wake_w = os.pipe()
        os.set_blocking(self._wake_r, False)
        os.set_blocking(self._wake_w, False)
        self._thread = threading.Thread(target=self._run, name="foldtop-clipboard", daemon=True)
        self._thread.start()
        self._started.wait(10.0)
        if not self._started.is_set():
            raise ClipboardError("the X server did not answer")
        if self._start_error is not None:
            raise ClipboardError(self._start_error)

    def stop(self) -> None:
        self._stopping.set()
        self._wake()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(5.0)

    def set_text(self, text: str, primary: bool = False, timeout: float = 3.0) -> bool:
        """Put `text` on the clipboard (and, with `primary`, on the PRIMARY selection that
        terminals paste with Shift+Insert). Returns once the X server has made this
        process the owner, so a paste keystroke sent afterwards finds it there."""
        text = text.replace("\x00", "")
        data = text.encode("utf-8", "replace")
        if not data or len(data) > MAX_TEXT_BYTES:
            return False
        return self._call(lambda: self._take_ownership(text, data, primary), timeout) is True

    def owned_text(self, timeout: float = 3.0) -> Optional[str]:
        """The text being served while this process owns the clipboard (for tests and status)."""
        result = self._call(lambda: self._text if self._atoms.get("CLIPBOARD") in self._owned else None, timeout)
        return result if isinstance(result, str) else None

    # ------------------------------------------------------------------ plumbing

    def _wake(self) -> None:
        with self._wake_lock:
            if self._wake_w < 0:
                return
            try:
                os.write(self._wake_w, b"\0")
            except (BlockingIOError, OSError):
                pass

    def _close_wake_pipe(self) -> None:
        with self._wake_lock:
            for fd in (self._wake_r, self._wake_w):
                if fd >= 0:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
            self._wake_r, self._wake_w = -1, -1

    def _call(self, fn: Callable[[], object], timeout: float) -> object:
        if not self.running:
            return None
        done = threading.Event()
        box: list = []
        self._commands.put((fn, done, box))
        self._wake()
        if not done.wait(timeout):
            return None
        return box[0] if box else None

    def _run(self) -> None:
        try:
            self._open()
        except Exception as exc:
            self._start_error = str(exc) or exc.__class__.__name__
            self._close()
            self._close_wake_pipe()
            self._started.set()
            return
        self._started.set()
        try:
            display = self._display
            assert display is not None
            fd = display.connection_fd()
            event = x11.XEvent()
            while not self._stopping.is_set():
                self._run_commands()
                while display.pending() > 0:
                    display.next_event(event)
                    self._handle(event)
                self._check_timeout()
                if display.pending() > 0 or not self._commands.empty():
                    continue
                select.select([fd, self._wake_r], [], [], 0.5)
                try:
                    while os.read(self._wake_r, 256):
                        pass
                except (BlockingIOError, OSError):
                    pass
        except Exception:  # pragma: no cover - defensive: never take the daemon down
            log.exception("clipboard sharing stopped")
            self._start_error = "stopped after an error"
        finally:
            self._close()
            self._fail_commands()
            self._close_wake_pipe()

    def _run_commands(self) -> None:
        while True:
            try:
                fn, done, box = self._commands.get_nowait()
            except queue.Empty:
                return
            try:
                box.append(fn())
            except Exception:
                log.exception("clipboard command failed")
            done.set()

    def _fail_commands(self) -> None:
        while True:
            try:
                _fn, done, _box = self._commands.get_nowait()
            except queue.Empty:
                return
            done.set()

    def _open(self) -> None:
        display = x11.Display(self._display_name)
        self._display = display
        base = display.xfixes_event_base()
        if base is None:
            raise ClipboardError("the X server has no XFIXES extension")
        self._xfixes_base = base
        for name in ("CLIPBOARD", "PRIMARY", "TARGETS", "UTF8_STRING", "STRING", "TEXT", "INCR",
                     "text/plain;charset=utf-8", "text/plain", "FOLDTOP_CLIPBOARD"):
            self._atoms[name] = display.intern_atom(name)
        self._window = display.create_window()
        display.select_input(self._window, x11.PropertyChangeMask)
        display.watch_selection_owner(self._window, self._atoms["CLIPBOARD"])
        display.sync()

    def _close(self) -> None:
        display, self._display = self._display, None
        if display is None:
            return
        try:
            if self._window:
                display.destroy_window(self._window)   # gives up every selection we own
                display.sync()
        except Exception:  # pragma: no cover
            pass
        self._window = 0
        self._owned.clear()
        display.close()

    # ------------------------------------------------------------------ owning (phone → workstation)

    def _take_ownership(self, text: str, data: bytes, primary: bool) -> bool:
        display = self._display
        if display is None:
            return False
        if len(data) > display.max_property_bytes():
            log.warning("clipboard text of %d bytes is more than this X server passes in one piece", len(data))
            return False
        # If this is the very text an application here copied (and we passed on), the
        # clipboard already holds it — with that application's richer formats, which
        # taking it over would throw away. Leave it where it is.
        already_there = self._reported_is_current and text == self._reported
        self._text, self._data = text, data
        self._from_phone = None if already_there else text
        wanted = ([] if already_there else [self._atoms["CLIPBOARD"]]) + ([self._atoms["PRIMARY"]] if primary else [])
        for selection in wanted:
            display.set_selection_owner(selection, self._window)
        display.sync()   # the server has processed it before we report back
        for selection in wanted:
            if display.selection_owner(selection) == self._window:
                self._owned.add(selection)
        if not already_there:
            self._reported_is_current = False
        return already_there or self._atoms["CLIPBOARD"] in self._owned

    def _serve(self, request) -> None:
        """Another application is pasting: hand it what we hold."""
        display = self._display
        assert display is not None
        atoms = self._atoms
        requestor, target = int(request.requestor), int(request.target)
        # Very old clients leave the property out; the convention is to use the target.
        prop = int(request.property) or target
        ok = False
        if int(request.selection) in self._owned and self._text is not None:
            if target == atoms["TARGETS"]:
                display.change_property_atoms(requestor, prop, [
                    atoms["TARGETS"], atoms["UTF8_STRING"], atoms["text/plain;charset=utf-8"],
                    atoms["text/plain"], atoms["STRING"], atoms["TEXT"]])
                ok = True
            elif target in (atoms["UTF8_STRING"], atoms["text/plain;charset=utf-8"], atoms["text/plain"]):
                display.change_property_bytes(requestor, prop, target, self._data)
                ok = True
            elif target == atoms["TEXT"]:      # "whatever text encoding you like"
                display.change_property_bytes(requestor, prop, atoms["UTF8_STRING"], self._data)
                ok = True
            elif target == atoms["STRING"]:    # Latin-1 by definition
                display.change_property_bytes(requestor, prop, target, self._text.encode("latin-1", "replace"))
                ok = True
        display.send_selection_notify(requestor, int(request.selection), target, prop if ok else 0,
                                      int(request.time))

    # ------------------------------------------------------------------ watching (workstation → phone)

    def _handle(self, event: x11.XEvent) -> None:
        kind = event.type
        if kind == x11.SelectionRequest:
            self._serve(event.xselectionrequest)
        elif kind == x11.SelectionClear:
            self._owned.discard(int(event.xselectionclear.selection))
        elif kind == x11.SelectionNotify:
            self._selection_arrived(event.xselection)
        elif kind == x11.PropertyNotify:
            self._property_changed(event.xproperty)
        elif kind == self._xfixes_base + x11.XFixesSelectionNotify:
            self._owner_changed(event.xfixesselection)

    def _owner_changed(self, event) -> None:
        if int(event.selection) != self._atoms["CLIPBOARD"] or event.subtype != x11.XFixesSetSelectionOwnerNotify:
            return
        owner = int(event.owner)
        self._reported_is_current = False
        if owner == 0 or owner == self._window:
            return      # the clipboard was emptied, or it is our own text
        self._begin_fetch(int(event.timestamp))

    def _begin_fetch(self, timestamp: int) -> None:
        display = self._display
        assert display is not None
        if self._fetch is not None:
            self._fetch_again = True    # copied again while we were still reading
            return
        prop = self._atoms["FOLDTOP_CLIPBOARD"]
        display.delete_property(self._window, prop)
        display.convert_selection(self._atoms["CLIPBOARD"], self._atoms["UTF8_STRING"], prop, self._window, timestamp)
        self._fetch = _Fetch()

    def _selection_arrived(self, event) -> None:
        fetch, display = self._fetch, self._display
        if fetch is None or display is None or fetch.incremental:
            return
        if int(event.requestor) != self._window or int(event.selection) != self._atoms["CLIPBOARD"]:
            return
        prop = self._atoms["FOLDTOP_CLIPBOARD"]
        if not int(event.property):
            self._finish_fetch(None)    # the owner has no text (an image, a file, …)
            return
        value = display.get_property(self._window, prop, MAX_TEXT_BYTES + 4, delete=True)
        if value is None:
            self._finish_fetch(None)
            return
        type_atom, fmt, data, left = value
        if type_atom == self._atoms["INCR"]:
            # Too big for one piece: the owner now sends it in parts, each announced by a
            # PropertyNotify, each acknowledged by deleting the property (done above).
            fetch.incremental = True
            fetch.deadline = time.monotonic() + FETCH_TIMEOUT
            return
        if fmt != 8 or left > 0 or len(data) > MAX_TEXT_BYTES:
            display.delete_property(self._window, prop)
            if left > 0 or len(data) > MAX_TEXT_BYTES:
                log.info("the workstation's clipboard is too large to send to the phone")
            self._finish_fetch(None)
            return
        self._finish_fetch(data)

    def _property_changed(self, event) -> None:
        fetch, display = self._fetch, self._display
        if fetch is None or display is None or not fetch.incremental:
            return
        prop = self._atoms["FOLDTOP_CLIPBOARD"]
        if int(event.window) != self._window or int(event.atom) != prop or event.state != x11.PropertyNewValue:
            return
        value = display.get_property(self._window, prop, MAX_TEXT_BYTES + 4, delete=True)
        if value is None:
            return
        _type_atom, fmt, data, left = value
        if left > 0:
            display.delete_property(self._window, prop)
            fetch.too_large = True
        if not data and left == 0:      # an empty part ends the transfer
            if fetch.too_large or fetch.size > MAX_TEXT_BYTES:
                log.info("the workstation's clipboard is too large to send to the phone")
                self._finish_fetch(None)
            else:
                self._finish_fetch(b"".join(fetch.chunks))
            return
        fetch.deadline = time.monotonic() + FETCH_TIMEOUT
        fetch.size += len(data)
        if fmt == 8 and not fetch.too_large and fetch.size <= MAX_TEXT_BYTES:
            fetch.chunks.append(data)
        else:
            fetch.too_large = True      # keep acknowledging so the owner can finish
            fetch.chunks.clear()

    def _check_timeout(self) -> None:
        fetch, display = self._fetch, self._display
        if fetch is None or display is None or time.monotonic() < fetch.deadline:
            return
        display.delete_property(self._window, self._atoms["FOLDTOP_CLIPBOARD"])
        self._finish_fetch(None)

    def _finish_fetch(self, data: Optional[bytes]) -> None:
        self._fetch = None
        if data:
            text = data.decode("utf-8", "replace").replace("\x00", "")
            self._report(text)
        if self._fetch_again:
            self._fetch_again = False
            self._begin_fetch(0)

    def _report(self, text: str) -> None:
        if not text:
            return
        now = time.monotonic()
        if text == self._from_phone:
            # A clipboard manager took over the text the phone just sent. Not news.
            return
        self._from_phone = None
        self._reported_is_current = True
        if text == self._reported and now - self._reported_at < REPEAT_WINDOW:
            return
        self._reported, self._reported_at = text, now
        try:
            self._on_change(text)
        except Exception:
            log.exception("clipboard listener failed")
