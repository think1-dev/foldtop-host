"""Pointer and keyboard injection through the XTest extension.

XTest needs no privileges (unlike /dev/uinput) and works for every X11 session.
All state that must be undone when a client goes away — held keys, held buttons,
temporary keycode bindings — is tracked here.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict, deque
from typing import Iterable, Optional

from . import protocol as P
from . import x11
from .keymap import (XK_Shift_L, KeymapIndex, KeyStroke, ScrollAccumulator, hid_to_x11_keycode,
                     is_cased_letter, text_keysyms)

log = logging.getLogger("foldtop.inject")

# X core pointer buttons
_BTN_SCROLL_UP, _BTN_SCROLL_DOWN, _BTN_SCROLL_LEFT, _BTN_SCROLL_RIGHT = 4, 5, 6, 7
_MAX_CLICKS_PER_EVENT = 40

# Scratch keycodes: unused keycodes temporarily bound to characters the current layout
# cannot produce (emoji, other scripts). Clients re-read the keymap asynchronously, so
# a binding needs a moment before use and must not be recycled while still in flight.
_SCRATCH_POOL_SIZE = 8
_SCRATCH_SETTLE_SECONDS = 0.03
_SCRATCH_MIN_AGE_SECONDS = 0.15


class Injector:
    def __init__(self, display_name: Optional[str] = None) -> None:
        self._d = x11.Display(display_name)
        if not self._d.has_xtest():
            self._d.close()
            raise x11.X11Error("the X server has no XTEST extension; cannot inject input")
        w, h = self._d.screen_size()
        self._screen = (w, h)
        self._region = (0, 0, w, h)
        self._held_keys: set[int] = set()
        self._held_buttons: set[int] = set()
        self._scroll = ScrollAccumulator()
        self._recent_abs: deque[tuple[int, int]] = deque(maxlen=64)
        self._recent_lock = threading.Lock()
        self._shift_keycodes = self._find_shift_keycodes()
        self._scratch: "OrderedDict[int, tuple[int, float]]" = OrderedDict()  # keysym -> (keycode, last used)
        self._scratch_free: list[int] = []
        self._scratch_ready = False
        self._index: Optional[KeymapIndex] = None
        self._index_group = -1
        self._index_built_at = 0.0

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        try:
            self.release_all()
            self._restore_scratch()
            self._d.sync()
        except x11.X11Error:
            pass
        self._d.close()

    def set_region(self, x: int, y: int, width: int, height: int) -> None:
        """The monitor rectangle MOVE_ABS coordinates refer to (root-window pixels)."""
        self._region = (x, y, max(width, 1), max(height, 1))

    @property
    def region(self) -> tuple[int, int, int, int]:
        return self._region

    def was_recent_abs(self, x: int, y: int) -> bool:
        """True if (x, y) is a position this injector recently moved the pointer to."""
        with self._recent_lock:
            return (x, y) in self._recent_abs

    # ------------------------------------------------------------------ events

    def apply(self, events: Iterable[P.InputEvent]) -> None:
        """Apply events in order, then flush once."""
        for ev in events:
            if isinstance(ev, P.MoveAbs):
                self.move_abs(ev.x, ev.y)
            elif isinstance(ev, P.MoveRel):
                self.move_rel(ev.dx, ev.dy)
            elif isinstance(ev, P.Button):
                self.button(ev.button, ev.down)
            elif isinstance(ev, P.Scroll):
                self.scroll(ev.dx, ev.dy)
            elif isinstance(ev, P.Key):
                self.key(ev.usage, ev.down)
            elif isinstance(ev, P.Text):
                self.type_text(ev.text)
            elif isinstance(ev, P.ReleaseAll):
                self.release_all()
        self._d.flush()

    def abs_to_pixel(self, x: int, y: int) -> tuple[int, int]:
        rx, ry, rw, rh = self._region
        return P.norm_to_pixel(x, rx, rw), P.norm_to_pixel(y, ry, rh)

    def move_abs(self, x: int, y: int) -> None:
        px, py = self.abs_to_pixel(x, y)
        with self._recent_lock:
            self._recent_abs.append((px, py))
        self._d.fake_motion(px, py)

    def move_rel(self, dx: int, dy: int) -> None:
        if dx or dy:
            self._d.fake_relative_motion(dx, dy)

    def button(self, button: int, down: bool) -> None:
        if button not in (1, 2, 3):
            return
        if down:
            self._held_buttons.add(button)
        else:
            self._held_buttons.discard(button)
        self._d.fake_button(button, down)

    def scroll(self, dx: int, dy: int) -> None:
        cx, cy = self._scroll.add(dx, dy)
        self._click(_BTN_SCROLL_DOWN if cy > 0 else _BTN_SCROLL_UP, abs(cy))
        self._click(_BTN_SCROLL_RIGHT if cx > 0 else _BTN_SCROLL_LEFT, abs(cx))

    def _click(self, button: int, times: int) -> None:
        for _ in range(min(times, _MAX_CLICKS_PER_EVENT)):
            self._d.fake_button(button, True)
            self._d.fake_button(button, False)

    def key(self, usage: int, down: bool) -> None:
        keycode = hid_to_x11_keycode(usage)
        lo, hi = 8, 255
        if not lo <= keycode <= hi:
            return
        if down:
            self._held_keys.add(keycode)
        elif keycode in self._held_keys:
            self._held_keys.discard(keycode)
        else:
            return  # never pressed by us: do not release someone else's key
        self._d.fake_key(keycode, down)

    def release_all(self) -> None:
        for keycode in sorted(self._held_keys):
            self._d.fake_key(keycode, False)
        for button in sorted(self._held_buttons):
            self._d.fake_button(button, False)
        self._held_keys.clear()
        self._held_buttons.clear()
        self._scroll.reset()
        self._d.flush()

    @property
    def held_keys(self) -> frozenset[int]:
        return frozenset(self._held_keys)

    @property
    def held_buttons(self) -> frozenset[int]:
        return frozenset(self._held_buttons)

    # ------------------------------------------------------------------ text

    def _find_shift_keycodes(self) -> tuple[int, ...]:
        codes = [kc for kc in (hid_to_x11_keycode(0xE1), hid_to_x11_keycode(0xE5)) if kc]
        primary = self._d.keysym_to_keycode(XK_Shift_L)
        if primary and primary not in codes:
            codes.insert(0, primary)
        return tuple(codes)

    def _keymap_index(self) -> KeymapIndex:
        group, _locked = self._d.xkb_state()
        now = time.monotonic()
        if self._index is None or group != self._index_group or now - self._index_built_at > 5.0:
            # The core keymap is fetched from the server each time (Xlib's cached XKB map
            # would go stale: this connection never processes MappingNotify). Its columns
            # are G1L1, G1L2, G2L1, G2L2, …; groups beyond the second fall back to the first.
            lo, table = self._d.keyboard_mapping()
            col = 2 * group if group in (0, 1) else 0
            scratch_codes = frozenset(kc for kc, _ in self._scratch.values()) | frozenset(self._scratch_free)
            levels: dict[int, tuple[int, int]] = {}
            for i, row in enumerate(table):
                keycode = lo + i
                if keycode < 9:
                    continue
                plain = row[col] if len(row) > col else 0
                shifted = row[col + 1] if len(row) > col + 1 else 0
                levels[keycode] = (plain, shifted)
            self._index = KeymapIndex(levels, exclude=scratch_codes)
            self._index_group = group
            self._index_built_at = now
        return self._index

    def plan_text(self, text: str) -> list[tuple[int, Optional[KeyStroke]]]:
        """(keysym, how to type it with the current layout or None → needs a scratch key)."""
        index = self._keymap_index()
        return [(sym, index.lookup(sym)) for sym in text_keysyms(text)]

    def type_text(self, text: str) -> None:
        plan = self.plan_text(text)
        if not plan:
            return
        _group, locked = self._d.xkb_state()
        caps = bool(locked & x11.LockMask)
        for keysym, stroke in plan:
            if stroke is None:
                keycode = self._bind_scratch(keysym)
                if keycode is None:
                    log.debug("no scratch keycode available for keysym 0x%x", keysym)
                    continue
                stroke = KeyStroke(keycode, False)
                want_shift = False
            else:
                want_shift = stroke.shift
                if caps and is_cased_letter(keysym):
                    want_shift = not want_shift
            self._tap(stroke.keycode, want_shift)
        self._d.flush()

    def _tap(self, keycode: int, want_shift: bool) -> None:
        held_shift = [kc for kc in self._shift_keycodes if kc in self._held_keys]
        if want_shift and not held_shift:
            shift = self._shift_keycodes[0]
            self._d.fake_key(shift, True)
            self._d.fake_key(keycode, True)
            self._d.fake_key(keycode, False)
            self._d.fake_key(shift, False)
        elif not want_shift and held_shift:
            for kc in held_shift:
                self._d.fake_key(kc, False)
            self._d.fake_key(keycode, True)
            self._d.fake_key(keycode, False)
            for kc in held_shift:
                self._d.fake_key(kc, True)
        else:
            self._d.fake_key(keycode, True)
            self._d.fake_key(keycode, False)

    # ------------------------------------------------------------------ scratch keycodes

    def _prepare_scratch(self) -> None:
        if self._scratch_ready:
            return
        self._scratch_ready = True
        lo, table = self._d.keyboard_mapping()
        unused = [lo + i for i, row in enumerate(table) if not any(row)]
        # Prefer the highest keycodes: nothing physical ever produces them.
        unused = [kc for kc in unused if kc > 8]
        self._scratch_free = sorted(unused, reverse=True)[:_SCRATCH_POOL_SIZE]
        self._scratch_free.reverse()  # pop() takes the highest first

    def _bind_scratch(self, keysym: int) -> Optional[int]:
        self._prepare_scratch()
        hit = self._scratch.get(keysym)
        if hit is not None:
            self._scratch[keysym] = (hit[0], time.monotonic())
            self._scratch.move_to_end(keysym)
            return hit[0]
        if self._scratch_free:
            keycode = self._scratch_free.pop()
        elif self._scratch:
            _old_sym, (keycode, used_at) = self._scratch.popitem(last=False)
            wait = _SCRATCH_MIN_AGE_SECONDS - (time.monotonic() - used_at)
            if wait > 0:
                self._d.flush()
                time.sleep(wait)
        else:
            return None
        self._d.change_keyboard_mapping(keycode, [keysym, keysym])
        self._d.sync()
        time.sleep(_SCRATCH_SETTLE_SECONDS)
        self._scratch[keysym] = (keycode, time.monotonic())
        return keycode

    def cleanup_idle(self, max_age: float = 2.0) -> None:
        """Give scratch keycodes back once typing has been quiet for a while."""
        if self._scratch and all(time.monotonic() - used_at > max_age for _kc, used_at in self._scratch.values()):
            self._restore_scratch()

    def _restore_scratch(self) -> None:
        if not self._scratch:
            return
        self._d.sync()
        time.sleep(_SCRATCH_SETTLE_SECONDS)
        for keycode, _used_at in self._scratch.values():
            self._d.change_keyboard_mapping(keycode, [x11.NoSymbol, x11.NoSymbol])
            self._scratch_free.append(keycode)
        self._scratch.clear()
        self._d.sync()

    @property
    def scratch_bindings(self) -> dict[int, int]:
        return {sym: kc for sym, (kc, _t) in self._scratch.items()}
