"""Screen capture → scale → H.264 using GStreamer.

    ximagesrc (monitor rectangle) → videoconvertscale → encoder → appsink

Every appsink buffer is one access unit in Annex-B form. Raw frames may be dropped
before the encoder when it cannot keep up (always safe); encoded frames are never
dropped here — the server decides what to do under network back-pressure.

Only the core `Gst` typelib is required (appsink is driven through its signals, the
force-key-unit event is built by hand), so no gir1.2-gst-plugins-base is needed.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from . import nal

log = logging.getLogger("foldtop.capture")

_gst = None
_gst_lock = threading.Lock()
_ENCODER_PTS_OFFSET_NS = 1000 * 3600 * 1_000_000_000


def gst():
    """Import and initialise GStreamer lazily (keeps `doctor`/`devices` fast and
    lets the pure-protocol parts run on machines without GStreamer)."""
    global _gst
    with _gst_lock:
        if _gst is None:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst

            Gst.init(None)
            _gst = Gst
    return _gst


def element_exists(name: str) -> bool:
    return gst().ElementFactory.find(name) is not None


@dataclass(frozen=True)
class EncoderInfo:
    name: str                   # "nvenc" | "x264" — reported in AUTH_OK / STATS
    h264: Optional[str]         # element name
    hevc: Optional[str] = None

    @property
    def codecs(self) -> list[str]:
        out = []
        if self.hevc:
            out.append(nal.HEVC)
        if self.h264:
            out.append(nal.H264)
        return out

    def element_for(self, codec: str) -> tuple[str, str]:
        """(codec actually used, element) — falls back to H.264 when HEVC is unavailable."""
        if codec == nal.HEVC and self.hevc:
            return nal.HEVC, self.hevc
        if self.h264:
            return nal.H264, self.h264
        raise RuntimeError("no H.264 encoder element is installed")


def detect_encoder(prefer: Optional[str] = None) -> EncoderInfo:
    """NVENC when gst-plugins-bad's nvcodec is installed, x264 otherwise.
    `prefer="x264"` forces software encoding."""
    has_x264 = element_exists("x264enc")
    if prefer != "x264" and element_exists("nvh264enc"):
        return EncoderInfo("nvenc", "nvh264enc", "nvh265enc" if element_exists("nvh265enc") else None)
    if has_x264:
        return EncoderInfo("x264", "x264enc")
    return EncoderInfo("none", None)


def required_elements() -> list[str]:
    return ["ximagesrc", "videoconvertscale", "queue", "appsink"]


def _threads_for(pixels: int, cap: int) -> int:
    return max(2, min(cap, -(-pixels // 260_000)))


def build_description(
    *,
    region: tuple[int, int, int, int],
    width: int,
    height: int,
    fps: int,
    encoder_element: str,
    codec: str,
    show_pointer: bool,
    display: Optional[str] = None,
    source: Optional[str] = None,
) -> str:
    """The gst-launch description for one capture session. Encoder properties are
    applied afterwards (they differ per element and plugin version)."""
    x, y, w, h = region
    if source is None:
        src = (
            f"ximagesrc name=src use-damage=false show-pointer={'true' if show_pointer else 'false'} "
            f"startx={x} starty={y} endx={x + w - 1} endy={y + h - 1}"
        )
        if display:
            src += f" display-name={display}"
        src += f" ! video/x-raw,framerate={fps}/1"
    else:
        src = source
    scale_threads = _threads_for(max(w * h, width * height), 16)
    raw_caps = f"video/x-raw,format=I420,width={width},height={height},pixel-aspect-ratio=1/1"
    out_caps = ("video/x-h265,stream-format=byte-stream,alignment=au" if codec == nal.HEVC
                else "video/x-h264,stream-format=byte-stream,alignment=au")
    leaky = "queue max-size-buffers=2 max-size-bytes=0 max-size-time=0 leaky=downstream"
    return (
        f"{src} ! {leaky} "
        f"! videoconvertscale n-threads={scale_threads} add-borders=false ! {raw_caps} ! {leaky} "
        f"! {encoder_element} name=enc ! {out_caps} "
        f"! appsink name=sink emit-signals=true sync=false max-buffers=60 drop=false"
    )


def _encoder_settings(element: str, *, width: int, height: int, fps: int, bitrate_kbps: int) -> list[tuple[str, str]]:
    if element == "x264enc":
        threads = _threads_for(width * height, 12)
        return [
            ("tune", "zerolatency"),
            # superfast keeps CABAC and deblocking (sharper text per bit than ultrafast)
            # and still encodes a phone-sized viewport at 60 fps on a few cores.
            ("speed-preset", "superfast"),
            ("bitrate", str(bitrate_kbps)),
            ("vbv-buf-capacity", "300"),
            ("key-int-max", "2147483647"),   # IDR only at start and on request
            ("bframes", "0"),
            ("b-adapt", "false"),
            ("rc-lookahead", "0"),
            ("sync-lookahead", "0"),
            ("mb-tree", "false"),
            ("sliced-threads", "true"),
            ("threads", str(threads)),
            ("byte-stream", "true"),
            ("aud", "true"),
            ("option-string", "scenecut=0:repeat-headers=1"),
        ]
    # NVENC (gst-plugins-bad "nvcodec"). Property names differ between plugin versions;
    # anything the installed element does not have is skipped. UNTESTED on this branch's
    # development machine, where the plugin is not installed.
    return [
        ("bitrate", str(bitrate_kbps)),
        ("max-bitrate", str(bitrate_kbps)),
        ("rc-mode", "cbr"),
        ("preset", "low-latency-hq"),
        ("tune", "ultra-low-latency"),
        ("zerolatency", "true"),
        ("zero-reorder-delay", "true"),
        ("bframes", "0"),
        ("b-frames", "0"),
        ("rc-lookahead", "0"),
        ("gop-size", "-1"),
        ("aud", "true"),
        ("repeat-sequence-header", "true"),
        ("vbv-buffer-size", str(max(bitrate_kbps * 300 // 1000, 1))),
    ]


FrameCallback = Callable[[bytes, bool, int], None]


class CaptureSession:
    """One running capture/encode pipeline.

    on_frame(access_unit, keyframe, pts_micros) is called on a GStreamer streaming
    thread; on_error(message) on a watcher thread.
    """

    def __init__(
        self,
        *,
        region: tuple[int, int, int, int],
        width: int,
        height: int,
        fps: int,
        bitrate_kbps: int,
        on_frame: FrameCallback,
        on_error: Optional[Callable[[str], None]] = None,
        codec: str = nal.H264,
        show_pointer: bool = False,
        display: Optional[str] = None,
        encoder: Optional[EncoderInfo] = None,
        source: Optional[str] = None,
    ) -> None:
        self.region = region
        self.width = width
        self.height = height
        self.fps = fps
        self.bitrate_kbps = bitrate_kbps
        self._on_frame = on_frame
        self._on_error = on_error
        self._encoder = encoder or detect_encoder()
        self.codec, self._element = self._encoder.element_for(codec)
        self.encoder_name = self._encoder.name
        self._show_pointer = show_pointer
        self._display = display
        self._source = source
        self._psets = nal.ParameterSetCache(self.codec)
        self._pipeline = None
        self._sink = None
        self._enc = None
        self._bus_thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._clock_origin_ns: Optional[int] = None
        self.frames = 0
        self.bytes = 0
        self.first_frame = threading.Event()

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        Gst = gst()
        desc = build_description(
            region=self.region, width=self.width, height=self.height, fps=self.fps,
            encoder_element=self._element, codec=self.codec, show_pointer=self._show_pointer,
            display=self._display, source=self._source,
        )
        log.debug("pipeline: %s", desc)
        pipeline = Gst.parse_launch(desc)
        enc = pipeline.get_by_name("enc")
        for prop, value in _encoder_settings(self._element, width=self.width, height=self.height,
                                             fps=self.fps, bitrate_kbps=self.bitrate_kbps):
            if enc.find_property(prop) is not None:
                try:
                    Gst.util_set_object_arg(enc, prop, value)
                except Exception as exc:  # pragma: no cover - plugin-version dependent
                    log.debug("encoder property %s=%s rejected: %s", prop, value, exc)
        sink = pipeline.get_by_name("sink")
        sink.connect("new-sample", self._on_new_sample)
        self._pipeline, self._enc, self._sink = pipeline, enc, sink
        self._clock_origin_ns = None
        self._stopping.clear()
        self._bus_thread = threading.Thread(target=self._watch_bus, name="foldtop-gst-bus", daemon=True)
        self._bus_thread.start()
        result = pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            self.stop()
            raise RuntimeError("the capture pipeline failed to start (is the X display reachable?)")

    def stop(self) -> None:
        self._stopping.set()
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is not None:
            pipeline.set_state(gst().State.NULL)
        thread, self._bus_thread = self._bus_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._sink = None
        self._enc = None

    @property
    def running(self) -> bool:
        return self._pipeline is not None

    # ------------------------------------------------------------------ controls

    def request_keyframe(self) -> None:
        sink = self._sink
        if sink is None:
            return
        Gst = gst()
        structure = Gst.Structure.new_from_string("GstForceKeyUnit, all-headers=(boolean)true")
        sink.send_event(Gst.Event.new_custom(Gst.EventType.CUSTOM_UPSTREAM, structure))

    def set_bitrate(self, bitrate_kbps: int) -> None:
        enc = self._enc
        if enc is None:
            return
        self.bitrate_kbps = bitrate_kbps
        Gst = gst()
        for prop in ("bitrate", "max-bitrate"):
            if enc.find_property(prop) is not None:
                Gst.util_set_object_arg(enc, prop, str(bitrate_kbps))

    # ------------------------------------------------------------------ internals

    def _on_new_sample(self, sink):
        Gst = gst()
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        size = buf.get_size()
        if size <= 0:
            return Gst.FlowReturn.OK
        data = buf.extract_dup(0, size)
        pts_us = self._capture_time_us(buf.pts)
        data, keyframe, _info = self._psets.process(data)
        self.frames += 1
        self.bytes += len(data)
        self.first_frame.set()
        if not self._stopping.is_set():
            try:
                self._on_frame(data, keyframe, pts_us)
            except Exception:  # pragma: no cover - never break the streaming thread
                log.exception("frame callback failed")
        return Gst.FlowReturn.OK

    def _capture_time_us(self, pts: int) -> int:
        """Buffer timestamp → CLOCK_MONOTONIC microseconds at which the frame was grabbed.

        ximagesrc stamps frames with the pipeline running time; base time + running time
        is the pipeline clock, which is the monotonic system clock. Video encoders that
        may reorder (x264enc) shift timestamps by 1000 hours to keep DTS positive.
        """
        Gst = gst()
        now_us = time.monotonic_ns() // 1000
        pipeline = self._pipeline
        if pts == Gst.CLOCK_TIME_NONE or pipeline is None:
            return now_us
        if self._clock_origin_ns is None:
            clock = pipeline.get_clock()
            base = pipeline.get_base_time()
            if clock is None or base == Gst.CLOCK_TIME_NONE:
                return now_us
            # pipeline-clock → time.monotonic_ns() (zero when both are CLOCK_MONOTONIC)
            self._clock_origin_ns = base - (clock.get_time() - time.monotonic_ns())
        if pts >= _ENCODER_PTS_OFFSET_NS:
            pts -= _ENCODER_PTS_OFFSET_NS
        stamp = (self._clock_origin_ns + pts) // 1000
        # never report a capture time in the future or absurdly far in the past
        return stamp if now_us - 5_000_000 <= stamp <= now_us else now_us

    def _watch_bus(self) -> None:
        Gst = gst()
        pipeline = self._pipeline
        if pipeline is None:
            return
        bus = pipeline.get_bus()
        wanted = Gst.MessageType.ERROR | Gst.MessageType.EOS
        while not self._stopping.is_set():
            msg = bus.timed_pop_filtered(200 * Gst.MSECOND, wanted)
            if msg is None:
                continue
            if msg.type == Gst.MessageType.ERROR:
                err, debug = msg.parse_error()
                text = f"{err.message} ({debug})" if debug else err.message
            else:
                text = "capture pipeline ended unexpectedly"
            if not self._stopping.is_set():
                log.error("capture error: %s", text)
                if self._on_error is not None:
                    self._on_error(text)
            return


def test_source(width: int, height: int, fps: int, pattern: str = "smpte") -> str:
    """A synthetic source with the same raw format ximagesrc produces — used for the
    golden vectors and for benchmarking the encoder without touching the screen."""
    return (f"videotestsrc is-live=true pattern={pattern} "
            f"! video/x-raw,format=BGRx,width={width},height={height},framerate={fps}/1")
