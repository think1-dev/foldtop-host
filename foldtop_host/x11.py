"""Minimal ctypes bindings for the parts of Xlib/XTest/XFixes/XRandR/XKB Foldtop needs.

No third-party dependency: the shared libraries below ship with every X11 desktop.
Each `Display` owns one connection and a lock; use one `Display` per subsystem.
"""

from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import (POINTER, Structure, byref, c_char, c_char_p, c_int, c_long, c_short, c_ubyte,
                    c_uint, c_ulong, c_ushort, c_void_p)
from dataclasses import dataclass
from typing import Optional

log = logging.getLogger("foldtop.x11")


class X11Error(RuntimeError):
    pass


def _load(name: str) -> ctypes.CDLL:
    try:
        return ctypes.CDLL(name)
    except OSError as exc:
        raise X11Error(f"cannot load {name}: {exc}") from None


_x11 = _load("libX11.so.6")
_xtst = _load("libXtst.so.6")
_xfixes = _load("libXfixes.so.3")
_xrandr = _load("libXrandr.so.2")

_DisplayP = c_void_p
Window = c_ulong
Atom = c_ulong
KeySym = c_ulong

NoSymbol = 0
XkbUseCoreKbd = 0x0100
ShiftMask = 0x01
LockMask = 0x02


class _XErrorEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("display", _DisplayP),
        ("resourceid", c_ulong),
        ("serial", c_ulong),
        ("error_code", c_ubyte),
        ("request_code", c_ubyte),
        ("minor_code", c_ubyte),
    ]


class _XkbStateRec(Structure):
    _fields_ = [
        ("group", c_ubyte),
        ("locked_group", c_ubyte),
        ("base_group", c_ushort),
        ("latched_group", c_ushort),
        ("mods", c_ubyte),
        ("base_mods", c_ubyte),
        ("latched_mods", c_ubyte),
        ("locked_mods", c_ubyte),
        ("compat_state", c_ubyte),
        ("grab_mods", c_ubyte),
        ("compat_grab_mods", c_ubyte),
        ("lookup_mods", c_ubyte),
        ("compat_lookup_mods", c_ubyte),
        ("ptr_buttons", c_ushort),
    ]


class _XFixesCursorImage(Structure):
    _fields_ = [
        ("x", c_short),
        ("y", c_short),
        ("width", c_ushort),
        ("height", c_ushort),
        ("xhot", c_ushort),
        ("yhot", c_ushort),
        ("cursor_serial", c_ulong),
        ("pixels", POINTER(c_ulong)),
        ("atom", Atom),
        ("name", c_char_p),
    ]


class _XRRMonitorInfo(Structure):
    _fields_ = [
        ("name", Atom),
        ("primary", c_int),
        ("automatic", c_int),
        ("noutput", c_int),
        ("x", c_int),
        ("y", c_int),
        ("width", c_int),
        ("height", c_int),
        ("mwidth", c_int),
        ("mheight", c_int),
        ("outputs", POINTER(c_ulong)),
    ]


# Event types and constants used for selections (the clipboard).
PropertyNotify = 28
SelectionClear = 29
SelectionRequest = 30
SelectionNotify = 31
PropertyChangeMask = 1 << 22
PropertyNewValue = 0
XA_ATOM = 4
XA_STRING = 31
XFixesSelectionNotify = 0                    # offset from the XFIXES event base
XFixesSetSelectionOwnerNotify = 0            # subtype
XFixesSetSelectionOwnerNotifyMask = 1 << 0


class _XSelectionRequestEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", c_int),
        ("display", _DisplayP),
        ("owner", Window),
        ("requestor", Window),
        ("selection", Atom),
        ("target", Atom),
        ("property", Atom),
        ("time", c_ulong),
    ]


class _XSelectionEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", c_int),
        ("display", _DisplayP),
        ("requestor", Window),
        ("selection", Atom),
        ("target", Atom),
        ("property", Atom),
        ("time", c_ulong),
    ]


class _XSelectionClearEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", c_int),
        ("display", _DisplayP),
        ("window", Window),
        ("selection", Atom),
        ("time", c_ulong),
    ]


class _XPropertyEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", c_int),
        ("display", _DisplayP),
        ("window", Window),
        ("atom", Atom),
        ("time", c_ulong),
        ("state", c_int),
    ]


class _XFixesSelectionNotifyEvent(Structure):
    _fields_ = [
        ("type", c_int),
        ("serial", c_ulong),
        ("send_event", c_int),
        ("display", _DisplayP),
        ("window", Window),
        ("subtype", c_int),
        ("owner", Window),
        ("selection", Atom),
        ("timestamp", c_ulong),
        ("selection_timestamp", c_ulong),
    ]


class XEvent(ctypes.Union):
    """Xlib's XEvent: `type` says which member is valid."""

    _fields_ = [
        ("type", c_int),
        ("xselectionrequest", _XSelectionRequestEvent),
        ("xselection", _XSelectionEvent),
        ("xselectionclear", _XSelectionClearEvent),
        ("xproperty", _XPropertyEvent),
        ("xfixesselection", _XFixesSelectionNotifyEvent),
        ("pad", c_long * 24),
    ]


def _sig(lib: ctypes.CDLL, name: str, restype, *argtypes) -> None:
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)


_sig(_x11, "XInitThreads", c_int)
_sig(_x11, "XOpenDisplay", _DisplayP, c_char_p)
_sig(_x11, "XCloseDisplay", c_int, _DisplayP)
_sig(_x11, "XDefaultScreen", c_int, _DisplayP)
_sig(_x11, "XRootWindow", Window, _DisplayP, c_int)
_sig(_x11, "XDisplayWidth", c_int, _DisplayP, c_int)
_sig(_x11, "XDisplayHeight", c_int, _DisplayP, c_int)
_sig(_x11, "XFlush", c_int, _DisplayP)
_sig(_x11, "XSync", c_int, _DisplayP, c_int)
_sig(_x11, "XFree", c_int, c_void_p)
_sig(_x11, "XGetAtomName", c_void_p, _DisplayP, Atom)
_sig(_x11, "XQueryPointer", c_int, _DisplayP, Window, POINTER(Window), POINTER(Window),
     POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_uint))
_sig(_x11, "XDisplayKeycodes", c_int, _DisplayP, POINTER(c_int), POINTER(c_int))
_sig(_x11, "XGetKeyboardMapping", POINTER(KeySym), _DisplayP, c_ubyte, c_int, POINTER(c_int))
_sig(_x11, "XChangeKeyboardMapping", c_int, _DisplayP, c_int, c_int, POINTER(KeySym), c_int)
_sig(_x11, "XkbKeycodeToKeysym", KeySym, _DisplayP, c_ubyte, c_uint, c_uint)
_sig(_x11, "XkbGetState", c_int, _DisplayP, c_uint, POINTER(_XkbStateRec))
_sig(_x11, "XQueryKeymap", c_int, _DisplayP, POINTER(c_char * 32))
_sig(_x11, "XKeysymToKeycode", c_ubyte, _DisplayP, KeySym)

_sig(_x11, "XConnectionNumber", c_int, _DisplayP)
_sig(_x11, "XInternAtom", Atom, _DisplayP, c_char_p, c_int)
_sig(_x11, "XCreateSimpleWindow", Window, _DisplayP, Window, c_int, c_int, c_uint, c_uint, c_uint, c_ulong, c_ulong)
_sig(_x11, "XDestroyWindow", c_int, _DisplayP, Window)
_sig(_x11, "XSelectInput", c_int, _DisplayP, Window, c_long)
_sig(_x11, "XPending", c_int, _DisplayP)
_sig(_x11, "XNextEvent", c_int, _DisplayP, POINTER(XEvent))
_sig(_x11, "XSendEvent", c_int, _DisplayP, Window, c_int, c_long, POINTER(XEvent))
_sig(_x11, "XSetSelectionOwner", c_int, _DisplayP, Atom, Window, c_ulong)
_sig(_x11, "XGetSelectionOwner", Window, _DisplayP, Atom)
_sig(_x11, "XConvertSelection", c_int, _DisplayP, Atom, Atom, Atom, Window, c_ulong)
_sig(_x11, "XChangeProperty", c_int, _DisplayP, Window, Atom, Atom, c_int, c_int, c_void_p, c_int)
_sig(_x11, "XGetWindowProperty", c_int, _DisplayP, Window, Atom, c_long, c_long, c_int, Atom,
     POINTER(Atom), POINTER(c_int), POINTER(c_ulong), POINTER(c_ulong), POINTER(c_void_p))
_sig(_x11, "XDeleteProperty", c_int, _DisplayP, Window, Atom)
_sig(_x11, "XMaxRequestSize", c_long, _DisplayP)
_sig(_x11, "XExtendedMaxRequestSize", c_long, _DisplayP)

_ERROR_HANDLER_T = ctypes.CFUNCTYPE(c_int, _DisplayP, POINTER(_XErrorEvent))
_sig(_x11, "XSetErrorHandler", c_void_p, _ERROR_HANDLER_T)

_sig(_xtst, "XTestQueryExtension", c_int, _DisplayP, POINTER(c_int), POINTER(c_int), POINTER(c_int), POINTER(c_int))
_sig(_xtst, "XTestFakeKeyEvent", c_int, _DisplayP, c_uint, c_int, c_ulong)
_sig(_xtst, "XTestFakeButtonEvent", c_int, _DisplayP, c_uint, c_int, c_ulong)
_sig(_xtst, "XTestFakeMotionEvent", c_int, _DisplayP, c_int, c_int, c_int, c_ulong)
_sig(_xtst, "XTestFakeRelativeMotionEvent", c_int, _DisplayP, c_int, c_int, c_ulong)

_sig(_xfixes, "XFixesQueryExtension", c_int, _DisplayP, POINTER(c_int), POINTER(c_int))
_sig(_xfixes, "XFixesGetCursorImage", POINTER(_XFixesCursorImage), _DisplayP)
_sig(_xfixes, "XFixesSelectSelectionInput", None, _DisplayP, Window, Atom, c_ulong)

_sig(_xrandr, "XRRQueryExtension", c_int, _DisplayP, POINTER(c_int), POINTER(c_int))
_sig(_xrandr, "XRRGetMonitors", POINTER(_XRRMonitorInfo), _DisplayP, Window, c_int, POINTER(c_int))
_sig(_xrandr, "XRRFreeMonitors", None, POINTER(_XRRMonitorInfo))


# Xlib's default error handler terminates the process; a daemon must not die because
# of one rejected request (e.g. a keycode that vanished after a layout change).
@_ERROR_HANDLER_T
def _on_x_error(_dpy, event):  # pragma: no cover - only reached on X protocol errors
    try:
        ev = event.contents
        log.warning("X error: code=%d request=%d.%d resource=0x%x",
                    ev.error_code, ev.request_code, ev.minor_code, ev.resourceid)
    except Exception:
        pass
    return 0


_init_lock = threading.Lock()
_initialised = False


def _ensure_init() -> None:
    global _initialised
    with _init_lock:
        if not _initialised:
            _x11.XInitThreads()
            _x11.XSetErrorHandler(_on_x_error)
            _initialised = True


@dataclass(frozen=True)
class Monitor:
    name: str
    x: int
    y: int
    width: int
    height: int
    primary: bool = False

    def contains(self, px: int, py: int) -> bool:
        return self.x <= px < self.x + self.width and self.y <= py < self.y + self.height


@dataclass(frozen=True)
class CursorImage:
    x: int
    y: int
    width: int
    height: int
    hot_x: int
    hot_y: int
    serial: int
    rgba: bytes  # premultiplied RGBA, width*height*4


class Display:
    """One X11 connection. All methods are thread-safe via an internal lock."""

    def __init__(self, name: Optional[str] = None) -> None:
        _ensure_init()
        self.name = name
        self.lock = threading.RLock()
        self._dpy = _x11.XOpenDisplay(name.encode() if name else None)
        if not self._dpy:
            raise X11Error(f"cannot open X display {name or '$DISPLAY'}")
        self.screen = _x11.XDefaultScreen(self._dpy)
        self.root = _x11.XRootWindow(self._dpy, self.screen)

    def close(self) -> None:
        with self.lock:
            if self._dpy:
                _x11.XCloseDisplay(self._dpy)
                self._dpy = None

    def __enter__(self) -> "Display":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def _handle(self) -> int:
        if not self._dpy:
            raise X11Error("display is closed")
        return self._dpy

    # ------------------------------------------------------------------ basics

    def flush(self) -> None:
        with self.lock:
            _x11.XFlush(self._handle())

    def sync(self) -> None:
        with self.lock:
            _x11.XSync(self._handle(), 0)

    def screen_size(self) -> tuple[int, int]:
        with self.lock:
            d = self._handle()
            return _x11.XDisplayWidth(d, self.screen), _x11.XDisplayHeight(d, self.screen)

    def query_pointer(self) -> tuple[int, int, int]:
        """(root_x, root_y, modifier/button mask)."""
        root_ret, child_ret = Window(), Window()
        rx, ry, wx, wy = c_int(), c_int(), c_int(), c_int()
        mask = c_uint()
        with self.lock:
            _x11.XQueryPointer(self._handle(), self.root, byref(root_ret), byref(child_ret),
                               byref(rx), byref(ry), byref(wx), byref(wy), byref(mask))
        return rx.value, ry.value, mask.value

    # ------------------------------------------------------------------ monitors

    def has_randr(self) -> bool:
        ev, err = c_int(), c_int()
        with self.lock:
            return bool(_xrandr.XRRQueryExtension(self._handle(), byref(ev), byref(err)))

    def monitors(self) -> list[Monitor]:
        """Active RandR monitors in root-window coordinates, primary first.
        Falls back to one monitor covering the whole screen."""
        result: list[Monitor] = []
        with self.lock:
            d = self._handle()
            if self.has_randr():
                count = c_int(0)
                infos = _xrandr.XRRGetMonitors(d, self.root, 1, byref(count))
                if infos:
                    try:
                        for i in range(max(count.value, 0)):
                            info = infos[i]
                            if info.width <= 0 or info.height <= 0:
                                continue
                            result.append(Monitor(
                                name=self._atom_name(info.name) or f"monitor-{i}",
                                x=info.x, y=info.y, width=info.width, height=info.height,
                                primary=bool(info.primary),
                            ))
                    finally:
                        _xrandr.XRRFreeMonitors(infos)
            if not result:
                w, h = self.screen_size()
                result.append(Monitor(name="screen", x=0, y=0, width=w, height=h, primary=True))
        if not any(m.primary for m in result):
            first = result[0]
            result[0] = Monitor(first.name, first.x, first.y, first.width, first.height, True)
        result.sort(key=lambda m: (not m.primary, m.x, m.y))
        return result

    def _atom_name(self, atom: int) -> str:
        if not atom:
            return ""
        ptr = _x11.XGetAtomName(self._handle(), atom)
        if not ptr:
            return ""
        try:
            return ctypes.string_at(ptr).decode("utf-8", "replace")
        finally:
            _x11.XFree(ptr)

    # ------------------------------------------------------------------ XTest

    def has_xtest(self) -> bool:
        a, b, c, e = c_int(), c_int(), c_int(), c_int()
        with self.lock:
            return bool(_xtst.XTestQueryExtension(self._handle(), byref(a), byref(b), byref(c), byref(e)))

    def fake_motion(self, x: int, y: int) -> None:
        with self.lock:
            _xtst.XTestFakeMotionEvent(self._handle(), self.screen, int(x), int(y), 0)

    def fake_relative_motion(self, dx: int, dy: int) -> None:
        with self.lock:
            _xtst.XTestFakeRelativeMotionEvent(self._handle(), int(dx), int(dy), 0)

    def fake_button(self, button: int, press: bool) -> None:
        with self.lock:
            _xtst.XTestFakeButtonEvent(self._handle(), int(button), 1 if press else 0, 0)

    def fake_key(self, keycode: int, press: bool) -> None:
        with self.lock:
            _xtst.XTestFakeKeyEvent(self._handle(), int(keycode), 1 if press else 0, 0)

    # ------------------------------------------------------------------ keyboard

    def keycode_range(self) -> tuple[int, int]:
        lo, hi = c_int(), c_int()
        with self.lock:
            _x11.XDisplayKeycodes(self._handle(), byref(lo), byref(hi))
        return lo.value, hi.value

    def keyboard_mapping(self) -> tuple[int, list[list[int]]]:
        """(first keycode, one keysym list per keycode) — the core keymap."""
        lo, hi = self.keycode_range()
        count = hi - lo + 1
        per = c_int()
        with self.lock:
            syms = _x11.XGetKeyboardMapping(self._handle(), lo, count, byref(per))
            if not syms:
                return lo, []
            try:
                width = per.value
                table = [[int(syms[i * width + j]) for j in range(width)] for i in range(count)]
            finally:
                _x11.XFree(syms)
        return lo, table

    def change_keyboard_mapping(self, keycode: int, keysyms: list[int]) -> None:
        arr = (KeySym * len(keysyms))(*keysyms)
        with self.lock:
            _x11.XChangeKeyboardMapping(self._handle(), int(keycode), len(keysyms), arr, 1)

    def keysym_at(self, keycode: int, group: int, level: int) -> int:
        with self.lock:
            return int(_x11.XkbKeycodeToKeysym(self._handle(), keycode & 0xFF, group, level))

    def keysym_to_keycode(self, keysym: int) -> int:
        with self.lock:
            return int(_x11.XKeysymToKeycode(self._handle(), keysym))

    def xkb_state(self) -> tuple[int, int]:
        """(active layout group, locked modifier mask)."""
        state = _XkbStateRec()
        with self.lock:
            status = _x11.XkbGetState(self._handle(), XkbUseCoreKbd, byref(state))
        if status != 0:
            return 0, 0
        return int(state.group), int(state.locked_mods)

    def pressed_keycodes(self) -> set[int]:
        keys = (c_char * 32)()
        with self.lock:
            _x11.XQueryKeymap(self._handle(), byref(keys))
        raw = bytes(keys)
        return {i * 8 + bit for i, byte in enumerate(raw) for bit in range(8) if byte & (1 << bit)}

    # ------------------------------------------------------------------ cursor

    def has_xfixes(self) -> bool:
        ev, err = c_int(), c_int()
        with self.lock:
            return bool(_xfixes.XFixesQueryExtension(self._handle(), byref(ev), byref(err)))

    def cursor_serial(self) -> Optional[int]:
        with self.lock:
            img = _xfixes.XFixesGetCursorImage(self._handle())
            if not img:
                return None
            try:
                return int(img.contents.cursor_serial)
            finally:
                _x11.XFree(img)

    def cursor_image(self) -> Optional[CursorImage]:
        with self.lock:
            img = _xfixes.XFixesGetCursorImage(self._handle())
            if not img:
                return None
            try:
                c = img.contents
                width, height = int(c.width), int(c.height)
                count = width * height
                # Each pixel is an `unsigned long` holding premultiplied ARGB32, so on
                # little-endian LP64 its bytes are B, G, R, A, 0, 0, 0, 0.
                step = ctypes.sizeof(c_ulong)
                raw = ctypes.string_at(c.pixels, count * step) if count else b""
                rgba = bytearray(count * 4)
                rgba[0::4] = raw[2::step]
                rgba[1::4] = raw[1::step]
                rgba[2::4] = raw[0::step]
                rgba[3::4] = raw[3::step]
                return CursorImage(x=int(c.x), y=int(c.y), width=width, height=height,
                                   hot_x=int(c.xhot), hot_y=int(c.yhot),
                                   serial=int(c.cursor_serial) & 0xFFFFFFFF, rgba=bytes(rgba))
            finally:
                _x11.XFree(img)

    # ------------------------------------------------------------------ selections

    def connection_fd(self) -> int:
        """The socket of this connection, for select()."""
        with self.lock:
            return int(_x11.XConnectionNumber(self._handle()))

    def intern_atom(self, name: str) -> int:
        with self.lock:
            return int(_x11.XInternAtom(self._handle(), name.encode("ascii"), 0))

    def create_window(self) -> int:
        """An unmapped 1×1 window: something to own selections and receive events with."""
        with self.lock:
            return int(_x11.XCreateSimpleWindow(self._handle(), self.root, -10, -10, 1, 1, 0, 0, 0))

    def destroy_window(self, window: int) -> None:
        with self.lock:
            _x11.XDestroyWindow(self._handle(), window)

    def select_input(self, window: int, mask: int) -> None:
        with self.lock:
            _x11.XSelectInput(self._handle(), window, mask)

    def pending(self) -> int:
        """Events waiting to be read. Also sends anything still buffered."""
        with self.lock:
            return int(_x11.XPending(self._handle()))

    def next_event(self, event: XEvent) -> None:
        """Blocks until an event arrives; call `pending` first to avoid that."""
        with self.lock:
            _x11.XNextEvent(self._handle(), byref(event))

    def set_selection_owner(self, selection: int, window: int, time: int = 0) -> None:
        with self.lock:
            _x11.XSetSelectionOwner(self._handle(), selection, window, time)

    def selection_owner(self, selection: int) -> int:
        with self.lock:
            return int(_x11.XGetSelectionOwner(self._handle(), selection))

    def convert_selection(self, selection: int, target: int, prop: int, window: int, time: int = 0) -> None:
        with self.lock:
            _x11.XConvertSelection(self._handle(), selection, target, prop, window, time)

    def change_property_bytes(self, window: int, prop: int, type_atom: int, data: bytes) -> None:
        """Replace a property with 8-bit data."""
        buf = ctypes.create_string_buffer(data, len(data)) if data else None
        with self.lock:
            _x11.XChangeProperty(self._handle(), window, prop, type_atom, 8, 0, buf, len(data))

    def change_property_atoms(self, window: int, prop: int, atoms: list[int]) -> None:
        """Replace a property with a list of atoms (format 32, which Xlib takes as C longs)."""
        arr = (c_long * len(atoms))(*atoms)
        with self.lock:
            _x11.XChangeProperty(self._handle(), window, prop, XA_ATOM, 32, 0, arr, len(atoms))

    def get_property(self, window: int, prop: int, max_bytes: int,
                     delete: bool) -> Optional[tuple[int, int, bytes, int]]:
        """(type atom, format, data, bytes left unread), or None if the property does not
        exist. With `delete` the property is removed once it has been read completely."""
        actual_type, actual_format = Atom(), c_int()
        nitems, bytes_after = c_ulong(), c_ulong()
        data = c_void_p()
        with self.lock:
            status = _x11.XGetWindowProperty(
                self._handle(), window, prop, 0, (max_bytes + 3) // 4, 1 if delete else 0, 0,
                byref(actual_type), byref(actual_format), byref(nitems), byref(bytes_after), byref(data))
            try:
                if status != 0 or not actual_type.value:
                    return None
                # Xlib hands 32-bit items back as C longs.
                item = {8: 1, 16: 2, 32: ctypes.sizeof(c_long)}.get(actual_format.value, 0)
                raw = ctypes.string_at(data.value, nitems.value * item) if data.value and item else b""
                return int(actual_type.value), int(actual_format.value), raw, int(bytes_after.value)
            finally:
                if data.value:
                    _x11.XFree(data)

    def delete_property(self, window: int, prop: int) -> None:
        with self.lock:
            _x11.XDeleteProperty(self._handle(), window, prop)

    def send_selection_notify(self, requestor: int, selection: int, target: int, prop: int, time: int) -> None:
        """Answer a SelectionRequest; `prop` 0 means the request was refused."""
        event = XEvent()
        reply = event.xselection
        reply.type = SelectionNotify
        reply.display = self._handle()
        reply.requestor = requestor
        reply.selection = selection
        reply.target = target
        reply.property = prop
        reply.time = time
        with self.lock:
            _x11.XSendEvent(self._handle(), requestor, 0, 0, byref(event))

    def max_property_bytes(self) -> int:
        """The most data one property change can carry on this connection."""
        with self.lock:
            units = _x11.XExtendedMaxRequestSize(self._handle()) or _x11.XMaxRequestSize(self._handle())
        return max(int(units) * 4 - 1024, 0)

    def xfixes_event_base(self) -> Optional[int]:
        """The first event number of XFIXES, or None without the extension."""
        ev, err = c_int(), c_int()
        with self.lock:
            if not _xfixes.XFixesQueryExtension(self._handle(), byref(ev), byref(err)):
                return None
        return ev.value

    def watch_selection_owner(self, window: int, selection: int) -> None:
        """Ask XFIXES to tell `window` whenever `selection` gets a new owner."""
        with self.lock:
            _xfixes.XFixesSelectSelectionInput(self._handle(), window, selection,
                                               XFixesSetSelectionOwnerNotifyMask)
