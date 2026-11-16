"""Wire protocol v1 — framing and message codecs (docs/PROTOCOL.md).

Pure stdlib, no I/O beyond the asyncio stream helper, so it is trivially testable
and is the source of the golden vectors the Swift implementation is checked against.
"""

from __future__ import annotations

import asyncio
import json
import struct
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Iterable, Union

MAX_FRAME = 16_777_216


class ProtocolError(Exception):
    """The peer sent something that violates the protocol."""


class Msg(IntEnum):
    HELLO = 0x01
    CHALLENGE = 0x02
    AUTH = 0x03
    AUTH_OK = 0x04
    AUTH_FAIL = 0x05
    START = 0x10
    STREAM_CONFIG = 0x11
    VIDEO = 0x12
    STOP = 0x13
    KEYFRAME = 0x14
    TUNE = 0x15
    INPUT = 0x20
    CURSOR_SHAPE = 0x21
    CURSOR_POS = 0x22
    PING = 0x30
    PONG = 0x31
    STATS = 0x32
    AUDIO_CONTROL = 0x40
    AUDIO_CONFIG = 0x41
    SOUND = 0x42
    MIC = 0x43
    CLIPBOARD_CONTROL = 0x50
    CLIPBOARD = 0x51
    BYE = 0x7F


# --------------------------------------------------------------------------- framing

def pack_frame(msg_type: int, payload: bytes = b"") -> bytes:
    length = 1 + len(payload)
    if length > MAX_FRAME:
        raise ProtocolError(f"frame too large: {length}")
    return struct.pack(">IB", length, int(msg_type) & 0xFF) + payload


def pack_json(msg_type: int, obj: dict[str, Any]) -> bytes:
    return pack_frame(msg_type, json_bytes(obj))


def json_bytes(obj: dict[str, Any]) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def parse_json(payload: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError(f"bad JSON payload: {exc}") from None
    if not isinstance(obj, dict):
        raise ProtocolError("JSON payload is not an object")
    return obj


class FrameDecoder:
    """Incremental decoder: feed bytes, get back complete (type, payload) frames."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf += data
        out: list[tuple[int, bytes]] = []
        while True:
            if len(self._buf) < 4:
                return out
            (length,) = struct.unpack_from(">I", self._buf, 0)
            if length < 1 or length > MAX_FRAME:
                raise ProtocolError(f"bad frame length {length}")
            if len(self._buf) < 4 + length:
                return out
            msg_type = self._buf[4]
            payload = bytes(self._buf[5:4 + length])
            del self._buf[:4 + length]
            out.append((msg_type, payload))

    @property
    def pending(self) -> int:
        return len(self._buf)


async def read_frame(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    """Read one frame. Raises asyncio.IncompleteReadError on EOF."""
    header = await reader.readexactly(4)
    (length,) = struct.unpack(">I", header)
    if length < 1 or length > MAX_FRAME:
        raise ProtocolError(f"bad frame length {length}")
    body = await reader.readexactly(length)
    return body[0], body[1:]


# --------------------------------------------------------------------------- input

OP_MOVE_ABS = 0x01
OP_MOVE_REL = 0x02
OP_BUTTON = 0x03
OP_SCROLL = 0x04
OP_KEY = 0x05
OP_TEXT = 0x06
OP_RELEASE_ALL = 0x07


@dataclass(frozen=True)
class MoveAbs:
    x: int  # 0…65535
    y: int


@dataclass(frozen=True)
class MoveRel:
    dx: int  # desktop pixels
    dy: int


@dataclass(frozen=True)
class Button:
    button: int  # 1 left, 2 middle, 3 right
    down: bool


@dataclass(frozen=True)
class Scroll:
    dx: int  # 1/120 detent; >0 scrolls right
    dy: int  # 1/120 detent; >0 scrolls down


@dataclass(frozen=True)
class Key:
    usage: int  # USB HID keyboard usage, page 0x07
    down: bool


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class ReleaseAll:
    pass


InputEvent = Union[MoveAbs, MoveRel, Button, Scroll, Key, Text, ReleaseAll]


def _u16(v: int, what: str) -> int:
    if not 0 <= v <= 0xFFFF:
        raise ProtocolError(f"{what} out of range: {v}")
    return v


def _i16(v: int, what: str) -> int:
    if not -0x8000 <= v <= 0x7FFF:
        raise ProtocolError(f"{what} out of range: {v}")
    return v


def encode_input(events: Iterable[InputEvent]) -> bytes:
    out = bytearray()
    for ev in events:
        if isinstance(ev, MoveAbs):
            out += struct.pack(">BHH", OP_MOVE_ABS, _u16(ev.x, "x"), _u16(ev.y, "y"))
        elif isinstance(ev, MoveRel):
            out += struct.pack(">Bhh", OP_MOVE_REL, _i16(ev.dx, "dx"), _i16(ev.dy, "dy"))
        elif isinstance(ev, Button):
            if not 0 <= ev.button <= 0xFF:
                raise ProtocolError(f"button out of range: {ev.button}")
            out += struct.pack(">BBB", OP_BUTTON, ev.button, 1 if ev.down else 0)
        elif isinstance(ev, Scroll):
            out += struct.pack(">Bhh", OP_SCROLL, _i16(ev.dx, "dx"), _i16(ev.dy, "dy"))
        elif isinstance(ev, Key):
            out += struct.pack(">BHB", OP_KEY, _u16(ev.usage, "usage"), 1 if ev.down else 0)
        elif isinstance(ev, Text):
            raw = ev.text.encode("utf-8")
            if len(raw) > 0xFFFF:
                raise ProtocolError("text event longer than 65535 bytes")
            out += struct.pack(">BH", OP_TEXT, len(raw)) + raw
        elif isinstance(ev, ReleaseAll):
            out.append(OP_RELEASE_ALL)
        else:  # pragma: no cover - programming error
            raise TypeError(f"not an input event: {ev!r}")
    return bytes(out)


def decode_input(payload: bytes) -> list[InputEvent]:
    events: list[InputEvent] = []
    i, n = 0, len(payload)

    def need(k: int) -> None:
        if i + k > n:
            raise ProtocolError("truncated input event")

    while i < n:
        op = payload[i]
        i += 1
        if op == OP_MOVE_ABS:
            need(4)
            x, y = struct.unpack_from(">HH", payload, i)
            i += 4
            events.append(MoveAbs(x, y))
        elif op == OP_MOVE_REL:
            need(4)
            dx, dy = struct.unpack_from(">hh", payload, i)
            i += 4
            events.append(MoveRel(dx, dy))
        elif op == OP_BUTTON:
            need(2)
            events.append(Button(payload[i], payload[i + 1] != 0))
            i += 2
        elif op == OP_SCROLL:
            need(4)
            dx, dy = struct.unpack_from(">hh", payload, i)
            i += 4
            events.append(Scroll(dx, dy))
        elif op == OP_KEY:
            need(3)
            (usage,) = struct.unpack_from(">H", payload, i)
            events.append(Key(usage, payload[i + 2] != 0))
            i += 3
        elif op == OP_TEXT:
            need(2)
            (length,) = struct.unpack_from(">H", payload, i)
            i += 2
            need(length)
            try:
                text = payload[i:i + length].decode("utf-8")
            except UnicodeDecodeError:
                raise ProtocolError("text event is not valid UTF-8") from None
            i += length
            events.append(Text(text))
        elif op == OP_RELEASE_ALL:
            events.append(ReleaseAll())
        else:
            # Event sizes are not self-describing, so an unknown op cannot be skipped.
            raise ProtocolError(f"unknown input op 0x{op:02x}")
    return events


# --------------------------------------------------------------------------- video / cursor / ping

VIDEO_FLAG_KEYFRAME = 0x01
_VIDEO_HEADER = struct.Struct(">BBQ")
_CURSOR_SHAPE_HEADER = struct.Struct(">IHHHH")
_CURSOR_POS = struct.Struct(">HHB")


def pack_video(epoch: int, flags: int, pts_micros: int, data: bytes) -> bytes:
    return _VIDEO_HEADER.pack(epoch & 0xFF, flags & 0xFF, pts_micros & 0xFFFFFFFFFFFFFFFF) + data


def unpack_video(payload: bytes) -> tuple[int, int, int, bytes]:
    if len(payload) < _VIDEO_HEADER.size:
        raise ProtocolError("truncated VIDEO")
    epoch, flags, pts = _VIDEO_HEADER.unpack_from(payload, 0)
    return epoch, flags, pts, payload[_VIDEO_HEADER.size:]


def pack_cursor_shape(serial: int, width: int, height: int, hot_x: int, hot_y: int, rgba: bytes) -> bytes:
    if len(rgba) != width * height * 4:
        raise ProtocolError("cursor pixel data does not match its dimensions")
    return _CURSOR_SHAPE_HEADER.pack(serial & 0xFFFFFFFF, _u16(width, "width"), _u16(height, "height"),
                                     _u16(hot_x, "hotX"), _u16(hot_y, "hotY")) + rgba


def unpack_cursor_shape(payload: bytes) -> tuple[int, int, int, int, int, bytes]:
    if len(payload) < _CURSOR_SHAPE_HEADER.size:
        raise ProtocolError("truncated CURSOR_SHAPE")
    serial, width, height, hot_x, hot_y = _CURSOR_SHAPE_HEADER.unpack_from(payload, 0)
    rgba = payload[_CURSOR_SHAPE_HEADER.size:]
    if len(rgba) != width * height * 4:
        raise ProtocolError("CURSOR_SHAPE pixel data does not match its dimensions")
    return serial, width, height, hot_x, hot_y, rgba


def pack_cursor_pos(x: int, y: int, visible: bool) -> bytes:
    return _CURSOR_POS.pack(_u16(x, "x"), _u16(y, "y"), 1 if visible else 0)


def unpack_cursor_pos(payload: bytes) -> tuple[int, int, bool]:
    if len(payload) < _CURSOR_POS.size:
        raise ProtocolError("truncated CURSOR_POS")
    x, y, visible = _CURSOR_POS.unpack_from(payload, 0)
    return x, y, visible != 0


def pack_ping(timestamp: int) -> bytes:
    return struct.pack(">Q", timestamp & 0xFFFFFFFFFFFFFFFF)


def unpack_ping(payload: bytes) -> int:
    if len(payload) < 8:
        raise ProtocolError("truncated PING/PONG")
    return struct.unpack_from(">Q", payload, 0)[0]


# --------------------------------------------------------------------------- audio

PCM_S16LE = "pcm_s16le"          # the only codec in v1: signed 16-bit little-endian, interleaved
MAX_AUDIO_MESSAGE_MS = 100       # a SOUND or MIC message never carries more than this
_SOUND_HEADER = struct.Struct(">Q")


def audio_format(sample_rate: int, channels: int) -> dict[str, Any]:
    """The format object used in AUDIO_CONFIG."""
    return {"codec": PCM_S16LE, "sampleRate": int(sample_rate), "channels": int(channels)}


def pcm_frame_size(channels: int) -> int:
    """Bytes per PCM frame (one sample for every channel)."""
    return 2 * channels


def pcm_is_whole_frames(pcm: bytes, sample_rate: int, channels: int) -> bool:
    """True if `pcm` is a non-empty run of whole frames no longer than the per-message limit."""
    frame = pcm_frame_size(channels)
    limit = sample_rate * MAX_AUDIO_MESSAGE_MS // 1000 * frame
    return 0 < len(pcm) <= limit and len(pcm) % frame == 0


def pack_sound(pts_micros: int, pcm: bytes) -> bytes:
    return _SOUND_HEADER.pack(pts_micros & 0xFFFFFFFFFFFFFFFF) + pcm


def unpack_sound(payload: bytes) -> tuple[int, bytes]:
    if len(payload) < _SOUND_HEADER.size:
        raise ProtocolError("truncated SOUND")
    (pts,) = _SOUND_HEADER.unpack_from(payload, 0)
    return pts, payload[_SOUND_HEADER.size:]


# --------------------------------------------------------------------------- clipboard

MAX_CLIPBOARD_BYTES = 1_048_576  # the most text one CLIPBOARD message carries
CLIPBOARD_FLAG_PASTE = 0x01      # C→S: also paste it where the keyboard focus is


def pack_clipboard(flags: int, text: str) -> bytes:
    raw = text.encode("utf-8")
    if not raw or len(raw) > MAX_CLIPBOARD_BYTES:
        raise ProtocolError(f"clipboard text must be 1…{MAX_CLIPBOARD_BYTES} bytes, not {len(raw)}")
    return bytes([flags & 0xFF]) + raw


def unpack_clipboard(payload: bytes) -> tuple[int, str]:
    """(flags, text). The text may be empty or over the limit; the receiver ignores those."""
    if len(payload) < 1:
        raise ProtocolError("truncated CLIPBOARD")
    try:
        return payload[0], payload[1:].decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("CLIPBOARD text is not valid UTF-8") from None


# --------------------------------------------------------------------------- geometry helpers

def norm_to_pixel(v: int, origin: int, size: int) -> int:
    """Map 0…65535 linearly onto the first…last pixel of a span."""
    if size <= 1:
        return origin
    return origin + (v * (size - 1) + 32767) // 65535


def pixel_to_norm(p: int, origin: int, size: int) -> int:
    if size <= 1:
        return 0
    rel = min(max(p - origin, 0), size - 1)
    return (rel * 65535 + (size - 1) // 2) // (size - 1)


def fit_size(src_w: int, src_h: int, max_w: int, max_h: int) -> tuple[int, int]:
    """Fit src into max preserving aspect ratio, never upscaling, even dimensions."""
    scale = min(max_w / src_w, max_h / src_h, 1.0)
    w = int(src_w * scale + 1e-6)
    h = int(src_h * scale + 1e-6)
    w -= w % 2
    h -= h % 2
    return max(w, 2), max(h, 2)
