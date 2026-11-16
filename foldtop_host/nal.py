"""Annex-B helpers: just enough NAL parsing to flag keyframes and guarantee that every
IDR access unit carries its parameter sets (PROTOCOL.md 0x12 VIDEO)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional

_START3 = b"\x00\x00\x01"
_START4 = b"\x00\x00\x00\x01"

H264 = "h264"
HEVC = "hevc"

# H.264 nal_unit_type
H264_NON_IDR, H264_IDR, H264_SEI, H264_SPS, H264_PPS, H264_AUD = 1, 5, 6, 7, 8, 9
# H.265 nal_unit_type
HEVC_VPS, HEVC_SPS, HEVC_PPS, HEVC_AUD = 32, 33, 34, 35


def iter_nals(data: bytes) -> Iterator[tuple[int, int]]:
    """Yield (start, end) byte offsets of each NAL unit's payload (start code excluded)."""
    n = len(data)
    pos = data.find(_START3)
    while pos != -1:
        start = pos + 3
        nxt = data.find(_START3, start)
        if nxt == -1:
            end = n
        else:
            end = nxt
            # a 4-byte start code's leading zero belongs to the start code
            if end > start and data[end - 1] == 0:
                end -= 1
        if end > start:
            yield start, end
        pos = nxt


def nal_type(codec: str, first_byte: int) -> int:
    if codec == HEVC:
        return (first_byte >> 1) & 0x3F
    return first_byte & 0x1F


def _is_vcl(codec: str, t: int) -> bool:
    return t <= 31 if codec == HEVC else 1 <= t <= 5


def _is_keyframe_type(codec: str, t: int) -> bool:
    return 16 <= t <= 21 if codec == HEVC else t == H264_IDR


def _is_parameter_set(codec: str, t: int) -> bool:
    return t in (HEVC_VPS, HEVC_SPS, HEVC_PPS) if codec == HEVC else t in (H264_SPS, H264_PPS)


def _is_aud(codec: str, t: int) -> bool:
    return t == (HEVC_AUD if codec == HEVC else H264_AUD)


@dataclass
class AccessUnitInfo:
    keyframe: bool
    types: list[int]            # NAL types up to and including the first VCL NAL
    parameter_sets: list[bytes]  # raw parameter-set NALs found (no start codes)
    after_aud: int               # byte offset just past a leading AUD (0 if none)


def inspect(codec: str, data: bytes) -> AccessUnitInfo:
    """Look at the NALs that precede (and include) the first slice of an access unit."""
    types: list[int] = []
    psets: list[bytes] = []
    keyframe = False
    after_aud = 0
    for index, (start, end) in enumerate(iter_nals(data)):
        t = nal_type(codec, data[start])
        types.append(t)
        if index == 0 and _is_aud(codec, t):
            after_aud = end
        if _is_parameter_set(codec, t):
            psets.append(bytes(data[start:end]))
        if _is_vcl(codec, t):
            keyframe = _is_keyframe_type(codec, t)
            break
    return AccessUnitInfo(keyframe=keyframe, types=types, parameter_sets=psets, after_aud=after_aud)


class ParameterSetCache:
    """Remembers the stream's parameter sets and re-inserts them into keyframes that
    arrive without them (some hardware encoders only emit them once)."""

    def __init__(self, codec: str) -> None:
        self.codec = codec
        self._sets: dict[int, bytes] = {}

    def _required(self) -> tuple[int, ...]:
        return (HEVC_VPS, HEVC_SPS, HEVC_PPS) if self.codec == HEVC else (H264_SPS, H264_PPS)

    def process(self, data: bytes) -> tuple[bytes, bool, Optional[AccessUnitInfo]]:
        """Returns (access unit, is keyframe, info)."""
        info = inspect(self.codec, data)
        for raw in info.parameter_sets:
            self._sets[nal_type(self.codec, raw[0])] = raw
        if not info.keyframe:
            return data, False, info
        present = {nal_type(self.codec, raw[0]) for raw in info.parameter_sets}
        missing = [t for t in self._required() if t not in present and t in self._sets]
        if missing:
            prefix = b"".join(_START4 + self._sets[t] for t in missing)
            data = data[:info.after_aud] + prefix + data[info.after_aud:]
        return data, True, info

    def complete(self) -> bool:
        return all(t in self._sets for t in self._required())
