"""USB HID keyboard usages → Linux evdev key codes → X11 keycodes, plus the pure
(no X connection needed) logic for turning Unicode text into key presses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# Index = HID usage on the keyboard page (0x07), value = Linux evdev KEY_* code.
# This is the kernel's own table (drivers/hid/hid-input.c: hid_keyboard[]), so a key
# sent by the app lands on exactly the keycode a USB keyboard would produce.
_U = 0  # unmapped
HID_TO_EVDEV: tuple[int, ...] = (
    0, 0, 0, 0, 30, 48, 46, 32, 18, 33, 34, 35, 23, 36, 37, 38,                  # 0x00
    50, 49, 24, 25, 16, 19, 31, 20, 22, 47, 17, 45, 21, 44, 2, 3,                # 0x10
    4, 5, 6, 7, 8, 9, 10, 11, 28, 1, 14, 15, 57, 12, 13, 26,                     # 0x20
    27, 43, 43, 39, 40, 41, 51, 52, 53, 58, 59, 60, 61, 62, 63, 64,              # 0x30
    65, 66, 67, 68, 87, 88, 99, 70, 119, 110, 102, 104, 111, 107, 109, 106,      # 0x40
    105, 108, 103, 69, 98, 55, 74, 78, 96, 79, 80, 81, 75, 76, 77, 71,           # 0x50
    72, 73, 82, 83, 86, 127, 116, 117, 183, 184, 185, 186, 187, 188, 189, 190,   # 0x60
    191, 192, 193, 194, 134, 138, 130, 132, 128, 129, 131, 137, 133, 135, 136, 113,  # 0x70
    115, 114, _U, _U, _U, 121, _U, 89, 93, 124, 92, 94, 95, _U, _U, _U,          # 0x80
    122, 123, 90, 91, 85, _U, _U, _U, _U, _U, _U, _U, 111, _U, _U, _U,           # 0x90
    _U, _U, _U, _U, _U, _U, _U, _U, _U, _U, _U, _U, _U, _U, _U, _U,              # 0xA0
    _U, _U, _U, _U, _U, _U, 179, 180, _U, _U, _U, _U, _U, _U, _U, _U,            # 0xB0
    _U, _U, _U, _U, _U, _U, _U, _U, _U, _U, _U, _U, _U, _U, _U, _U,              # 0xC0
    _U, _U, _U, _U, _U, _U, _U, _U, 111, _U, _U, _U, _U, _U, _U, _U,             # 0xD0
    29, 42, 56, 125, 97, 54, 100, 126, 164, 166, 165, 163, 161, 115, 114, 113,   # 0xE0
    150, 158, 159, 128, 136, 177, 178, 176, 142, 152, 173, 140, _U, _U, _U, _U,  # 0xF0
)
assert len(HID_TO_EVDEV) == 256

EVDEV_TO_X11_OFFSET = 8

HID_LEFT_CTRL, HID_LEFT_SHIFT, HID_LEFT_ALT, HID_LEFT_GUI = 0xE0, 0xE1, 0xE2, 0xE3
HID_RIGHT_CTRL, HID_RIGHT_SHIFT, HID_RIGHT_ALT, HID_RIGHT_GUI = 0xE4, 0xE5, 0xE6, 0xE7


def hid_to_evdev(usage: int) -> int:
    """evdev key code for a HID keyboard usage, or 0 if there is none."""
    if 0 <= usage < 256:
        return HID_TO_EVDEV[usage]
    return 0


def hid_to_x11_keycode(usage: int) -> int:
    """X11 keycode (evdev code + 8) for a HID keyboard usage, or 0 if unmapped."""
    code = hid_to_evdev(usage)
    return code + EVDEV_TO_X11_OFFSET if code else 0


# --------------------------------------------------------------------------- text → keysyms

XK_BackSpace = 0xFF08
XK_Tab = 0xFF09
XK_Return = 0xFF0D
XK_Escape = 0xFF1B
XK_Delete = 0xFFFF
XK_Shift_L = 0xFFE1
XK_Shift_R = 0xFFE2

_CONTROL_KEYSYMS = {
    "\n": XK_Return,
    "\r": XK_Return,
    "\t": XK_Tab,
    "\b": XK_BackSpace,
    "\x1b": XK_Escape,
    "\x7f": XK_Delete,
}


def keysym_for_char(ch: str) -> Optional[int]:
    """The X keysym that produces `ch`, or None for characters that cannot be typed."""
    if ch in _CONTROL_KEYSYMS:
        return _CONTROL_KEYSYMS[ch]
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp  # Latin-1 keysyms equal their code points
    if cp < 0x20 or 0x7F <= cp < 0xA0:
        return None  # other control characters
    if 0xD800 <= cp <= 0xDFFF or cp > 0x10FFFF:
        return None
    return 0x01000000 | cp  # Unicode keysym


def text_keysyms(text: str) -> list[int]:
    """Keysyms for a string; CRLF collapses to one Return, untypable characters are dropped."""
    out: list[int] = []
    prev = ""
    for ch in text:
        if ch == "\n" and prev == "\r":
            prev = ch
            continue
        prev = ch
        sym = keysym_for_char(ch)
        if sym is not None:
            out.append(sym)
    return out


@dataclass(frozen=True)
class KeyStroke:
    keycode: int
    shift: bool


class KeymapIndex:
    """keysym → (keycode, needs Shift) for one layout group, built from the levels the
    server reports. Level 0 is preferred over level 1, lower keycodes over higher."""

    def __init__(self, levels: dict[int, tuple[int, int]], exclude: frozenset[int] = frozenset()) -> None:
        index: dict[int, KeyStroke] = {}
        for keycode in sorted(levels):
            if keycode in exclude:
                continue
            plain, shifted = levels[keycode]
            if plain and plain not in index:
                index[plain] = KeyStroke(keycode, False)
        for keycode in sorted(levels):
            if keycode in exclude:
                continue
            plain, shifted = levels[keycode]
            if shifted and shifted != plain and shifted not in index:
                index[shifted] = KeyStroke(keycode, True)
        self._index = index

    def lookup(self, keysym: int) -> Optional[KeyStroke]:
        return self._index.get(keysym)

    def __len__(self) -> int:
        return len(self._index)


def is_cased_letter(keysym: int) -> bool:
    """True if Caps Lock changes what this keysym's key produces."""
    if keysym >= 0x01000000:
        cp = keysym & 0x00FFFFFF
    elif 0x20 <= keysym <= 0xFF:
        cp = keysym
    else:
        return False
    try:
        ch = chr(cp)
    except ValueError:
        return False
    return ch.lower() != ch.upper()


class ScrollAccumulator:
    """Turns 1/120-detent scroll deltas into whole wheel clicks, carrying the remainder."""

    DETENT = 120

    def __init__(self) -> None:
        self._x = 0
        self._y = 0

    def add(self, dx: int, dy: int) -> tuple[int, int]:
        """Returns (horizontal clicks, vertical clicks); positive = right / down."""
        self._x += dx
        self._y += dy
        cx = int(self._x / self.DETENT)  # truncate toward zero
        cy = int(self._y / self.DETENT)
        self._x -= cx * self.DETENT
        self._y -= cy * self.DETENT
        return cx, cy

    def reset(self) -> None:
        self._x = 0
        self._y = 0
