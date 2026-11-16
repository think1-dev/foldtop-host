"""Audio: the workstation's sound to the phone, and the phone's microphone to the
workstation (docs/PROTOCOL.md, "Audio").

    sound        pulsesrc (monitor of the default output) → S16LE 48 kHz stereo → appsink
    microphone   appsrc (S16LE 48 kHz mono from the phone) → pulsesink → a null sink whose
                 monitor is re-published as an input device called "Foldtop Microphone"

Both go through the PulseAudio protocol, so they work the same on PipeWire (pipewire-pulse)
and on PulseAudio. The virtual microphone is two server modules loaded with `pactl`;
they are unloaded when the microphone is switched off, and their ids are written to the
state directory so a daemon that died without unloading them cleans up at its next start.

Everything here fails soft: no audio server, no `pactl` or a missing GStreamer element
makes that direction unavailable and nothing else.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from . import protocol as P
from .capture import element_exists, gst

log = logging.getLogger("foldtop.audio")

SOUND_RATE, SOUND_CHANNELS = 48000, 2
MIC_RATE, MIC_CHANNELS = 48000, 1
CHUNK_FRAMES = 480                       # 10 ms at 48 kHz: the size of a SOUND message
DEFAULT_SOUND_SOURCE = "@DEFAULT_MONITOR@"
DEFAULT_MIC_NAME = "foldtop_mic"
MIC_DESCRIPTION = "Foldtop Microphone"
MIC_BACKLOG_MS = 120                     # microphone audio waiting to be played is capped at this

SOUND_ELEMENTS = ("pulsesrc", "appsink")
MIC_ELEMENTS = ("appsrc", "queue", "audioconvert", "audioresample", "pulsesink")

_MODULES_FILE = "audio-modules.json"


def sound_format() -> dict[str, Any]:
    return P.audio_format(SOUND_RATE, SOUND_CHANNELS)


def microphone_format() -> dict[str, Any]:
    return P.audio_format(MIC_RATE, MIC_CHANNELS)


def pactl(*args: str, timeout: float = 5.0) -> tuple[int, str, str]:
    """Run `pactl`; (exit code, stdout, stderr). Exit code 127 when it cannot be run."""
    try:
        result = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=timeout,
                                env=dict(os.environ, LC_ALL="C"))
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def source_exists(name: str) -> bool:
    """Whether the audio server has a source with this name. Names such as
    @DEFAULT_MONITOR@ are resolved by the server and always count as existing."""
    if name.startswith("@"):
        return True
    code, out, _err = pactl("list", "short", "sources")
    if code != 0:
        return True     # cannot tell: let the stream itself decide
    return any(line.split("\t")[1:2] == [name] for line in out.splitlines())


def loaded_modules() -> dict[str, str]:
    """{module id: "name<TAB>arguments"} of every module in the audio server."""
    code, out, _err = pactl("list", "short", "modules")
    modules: dict[str, str] = {}
    if code == 0:
        for line in out.splitlines():
            ident, _, rest = line.partition("\t")
            if ident.strip().isdigit():
                modules[ident.strip()] = rest
    return modules


@dataclass(frozen=True)
class Capabilities:
    sound: bool
    microphone: bool
    sound_problem: str = ""
    microphone_problem: str = ""

    def to_json(self) -> dict[str, bool]:
        return {"sound": self.sound, "microphone": self.microphone}


class AudioManager:
    """What this host can do with audio, and the bookkeeping for the server modules
    behind the virtual microphone. One per daemon."""

    def __init__(self, *, state_dir: Path, enabled: bool = True, sound_source: Optional[str] = None,
                 mic_name: Optional[str] = None, mic_default: Optional[bool] = None) -> None:
        self.state_dir = Path(state_dir)
        self.enabled = enabled
        self.sound_source = sound_source or DEFAULT_SOUND_SOURCE
        self.mic_name = mic_name or DEFAULT_MIC_NAME
        # Make the virtual microphone the workstation's default input while it is on,
        # so applications that record from "the default microphone" hear the phone
        # without anyone choosing a device. Unless set explicitly this applies to the
        # standard device only: a custom-named one (tests, special setups) leaves the
        # default alone.
        self.mic_default = (mic_name is None) if mic_default is None else bool(mic_default)
        self._lock = threading.Lock()
        self._owned: dict[str, str] = {}     # module id → a string its arguments must contain

    # ------------------------------------------------------------------ capabilities

    def capabilities(self) -> Capabilities:
        """Blocking (runs `pactl info`); cheap enough to call for every connection, which
        also means an audio server that starts after the daemon is picked up."""
        if not self.enabled:
            reason = "audio is switched off on this workstation"
            return Capabilities(False, False, reason, reason)
        reason = ""
        if shutil.which("pactl") is None:
            reason = "pactl is not installed (package pulseaudio-utils)"
        else:
            code, _out, err = pactl("info", timeout=3.0)
            if code != 0:
                reason = f"no audio server ({err.splitlines()[-1] if err else 'pactl info failed'})"
        if reason:
            return Capabilities(False, False, reason, reason)
        try:
            missing_sound = [e for e in SOUND_ELEMENTS if not element_exists(e)]
            missing_mic = [e for e in MIC_ELEMENTS if not element_exists(e)]
        except Exception as exc:  # GStreamer itself is missing
            reason = f"GStreamer is not available ({exc})"
            return Capabilities(False, False, reason, reason)
        sound_problem = f"GStreamer element {missing_sound[0]} is missing" if missing_sound else ""
        mic_problem = f"GStreamer element {missing_mic[0]} is missing" if missing_mic else ""
        return Capabilities(not sound_problem, not mic_problem, sound_problem, mic_problem)

    # ------------------------------------------------------------------ factories

    def new_sound(self, *, on_chunk: Callable[[int, bytes], None],
                  on_error: Optional[Callable[[str], None]] = None) -> "SoundCapture":
        return SoundCapture(device=self.sound_source, on_chunk=on_chunk, on_error=on_error)

    def new_microphone(self) -> "VirtualMicrophone":
        return VirtualMicrophone(self, self.mic_name, make_default=self.mic_default)

    # ------------------------------------------------------------------ server modules

    def load_module(self, name: str, *args: str, marker: str) -> str:
        """Load a server module and remember it. `marker` is a substring of its
        arguments (the device name) used to recognise it again before unloading."""
        code, out, err = pactl("load-module", name, *args)
        if code != 0 or not out.isdigit():
            raise RuntimeError(f"could not load {name}: {err or out or 'pactl failed'}")
        with self._lock:
            self._owned[out] = marker
            self._save()
        return out

    def unload_module(self, ident: str) -> None:
        with self._lock:
            marker = self._owned.pop(ident, None)
            self._save()
        if marker is None:
            return
        # Module ids can be reused after the audio server restarts: only unload it if
        # it is still the module this daemon loaded.
        if marker in loaded_modules().get(ident, ""):
            code, _out, err = pactl("unload-module", ident)
            if code != 0:
                log.warning("could not unload audio module %s: %s", ident, err)

    def cleanup_stale(self) -> int:
        """Unload modules a previous daemon with this state directory left behind
        (it was killed before it could). Returns how many were removed."""
        path = self.state_dir / _MODULES_FILE
        try:
            recorded = json.loads(path.read_text())
        except (OSError, ValueError):
            return 0
        removed = 0
        if isinstance(recorded, dict) and recorded:
            present = loaded_modules()
            for ident, marker in recorded.items():
                if isinstance(marker, str) and marker and marker in present.get(str(ident), ""):
                    code, _out, _err = pactl("unload-module", str(ident))
                    removed += code == 0
        with self._lock:
            self._save()
        if removed:
            log.info("removed %d audio module(s) left by a previous run", removed)
        return removed

    def close(self) -> None:
        """Unload anything still loaded (normally nothing: sessions stop their own)."""
        with self._lock:
            owned = list(self._owned)
        for ident in owned:
            self.unload_module(ident)

    def _save(self) -> None:
        """Caller holds the lock."""
        path = self.state_dir / _MODULES_FILE
        try:
            if self._owned:
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._owned))
                os.replace(tmp, path)
            else:
                path.unlink(missing_ok=True)
        except OSError as exc:
            log.debug("could not record audio modules: %s", exc)


def _monotonic_us(pipeline, origin: list, pts: int) -> int:
    """Buffer timestamp → CLOCK_MONOTONIC microseconds (the clock VIDEO uses).
    `origin` is a one-element cache for the pipeline-clock → monotonic offset."""
    Gst = gst()
    now_us = time.monotonic_ns() // 1000
    if pts == Gst.CLOCK_TIME_NONE or pipeline is None:
        return now_us
    if origin[0] is None:
        clock = pipeline.get_clock()
        base = pipeline.get_base_time()
        if clock is None or base == Gst.CLOCK_TIME_NONE:
            return now_us
        origin[0] = base - (clock.get_time() - time.monotonic_ns())
    stamp = (origin[0] + pts) // 1000
    return stamp if now_us - 2_000_000 <= stamp <= now_us else now_us


class _Pipeline:
    """Shared start/stop/bus-watching for the two small pipelines below."""

    # A subclass that expects data to flow sets this to a number of seconds: if
    # nothing has arrived by then, that is reported as an error (an audio server
    # can accept a stream for a device that never produces anything).
    _data_timeout: Optional[float] = None

    def __init__(self, on_error: Optional[Callable[[str], None]]) -> None:
        self._on_error = on_error
        self._pipeline = None
        self._bus_thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()
        self._started_at = 0.0

    def _has_data(self) -> bool:
        return True

    def _launch(self, description: str):
        Gst = gst()
        log.debug("pipeline: %s", description)
        self._pipeline = Gst.parse_launch(description)
        self._stopping.clear()
        return self._pipeline

    def _play(self, what: str) -> None:
        Gst = gst()
        pipeline = self._pipeline
        self._started_at = time.monotonic()
        self._bus_thread = threading.Thread(target=self._watch_bus, name="foldtop-audio-bus", daemon=True)
        self._bus_thread.start()
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            self._halt()
            raise RuntimeError(f"{what} could not be started")

    def _halt(self) -> None:
        self._stopping.set()
        pipeline, self._pipeline = self._pipeline, None
        if pipeline is not None:
            pipeline.set_state(gst().State.NULL)
        thread, self._bus_thread = self._bus_thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)

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
                timeout = self._data_timeout
                if timeout is None or self._has_data() or time.monotonic() - self._started_at < timeout:
                    continue
                text = "no audio is arriving from the audio server"
            elif msg.type == Gst.MessageType.ERROR:
                err, _debug = msg.parse_error()
                text = err.message
            else:
                text = "the audio stream ended unexpectedly"
            if not self._stopping.is_set():
                log.warning("audio error: %s", text)
                if self._on_error is not None:
                    self._on_error(text)
            return


class SoundCapture(_Pipeline):
    """Captures what the workstation is playing.

    on_chunk(pts_micros, pcm) is called on a GStreamer streaming thread with whole
    frames of S16LE 48 kHz stereo, at most CHUNK_FRAMES (10 ms) at a time.
    """

    # Even a silent output delivers (silent) audio; nothing at all means the stream
    # is not connected to anything.
    _data_timeout = 3.0

    def __init__(self, *, device: str, on_chunk: Callable[[int, bytes], None],
                 on_error: Optional[Callable[[str], None]] = None) -> None:
        super().__init__(on_error)
        self.device = device
        self._on_chunk = on_chunk
        self._origin: list = [None]
        self.chunks = 0
        self.bytes = 0

    @property
    def format(self) -> dict[str, Any]:
        return sound_format()

    def _has_data(self) -> bool:
        return self.chunks > 0

    def start(self) -> None:
        if not source_exists(self.device):
            raise RuntimeError(f"the audio source {self.device!r} does not exist")
        self.chunks = 0
        self.bytes = 0
        # ~10 ms fragments from the server; provide-clock=false keeps the pipeline on
        # the monotonic system clock so timestamps line up with the video's.
        pipeline = self._launch(
            f"pulsesrc name=src device={self.device} buffer-time=20000 latency-time=10000 "
            f"provide-clock=false client-name=foldtop-host "
            f"! audio/x-raw,format=S16LE,rate={SOUND_RATE},channels={SOUND_CHANNELS},layout=interleaved "
            f"! appsink name=sink emit-signals=true sync=false max-buffers=50 drop=true"
        )
        pipeline.get_by_name("sink").connect("new-sample", self._on_new_sample)
        self._origin = [None]
        self._play("sound capture")

    def stop(self) -> None:
        self._halt()

    def _on_new_sample(self, sink):
        Gst = gst()
        sample = sink.emit("pull-sample")
        if sample is None:
            return Gst.FlowReturn.OK
        buf = sample.get_buffer()
        frame = P.pcm_frame_size(SOUND_CHANNELS)
        size = buf.get_size() - buf.get_size() % frame      # whole frames only
        if size <= 0 or self._stopping.is_set():
            return Gst.FlowReturn.OK
        data = buf.extract_dup(0, size)
        pts_us = _monotonic_us(self._pipeline, self._origin, buf.pts)
        step = CHUNK_FRAMES * frame
        for offset in range(0, size, step):
            chunk = data[offset:offset + step]
            self.chunks += 1
            self.bytes += len(chunk)
            try:
                self._on_chunk(pts_us + offset // frame * 1_000_000 // SOUND_RATE, chunk)
            except Exception:  # pragma: no cover - never break the streaming thread
                log.exception("sound callback failed")
        return Gst.FlowReturn.OK


class VirtualMicrophone(_Pipeline):
    """An input device on the workstation that plays what the phone's microphone hears.

    start() loads a null sink and re-publishes its monitor as a source described as
    "Foldtop Microphone"; push() feeds it S16LE 48 kHz mono; stop() removes both.
    """

    def __init__(self, manager: AudioManager, name: str,
                 on_error: Optional[Callable[[str], None]] = None, *, make_default: bool = False) -> None:
        super().__init__(on_error)
        self._manager = manager
        self.source_name = name
        self.sink_name = f"{name}_sink"
        self.make_default = make_default
        self._previous_default: Optional[str] = None    # the default input to put back on stop
        self._claimed_default = False
        self._modules: list[str] = []
        self._src = None
        self._push_lock = threading.Lock()
        # Pacing: how much audio has been accepted since `_pace_start`, to keep the
        # feed from running ahead of real time (see push()).
        self._pace_start: Optional[float] = None
        self._pace_accepted = 0.0
        self._last_push = 0.0
        self.pushed = 0
        self.rejected = 0       # malformed MIC messages
        self.dropped = 0        # well-formed audio discarded because it arrived faster than real time

    @property
    def format(self) -> dict[str, Any]:
        return microphone_format()

    def start(self) -> None:
        try:
            self._modules.append(self._manager.load_module(
                "module-null-sink", f"sink_name={self.sink_name}", f"rate={MIC_RATE}",
                f"channels={MIC_CHANNELS}", "channel_map=mono",
                f"sink_properties=\"device.description='{MIC_DESCRIPTION} (sink)'\"",
                marker=f"sink_name={self.sink_name}"))
            self._modules.append(self._manager.load_module(
                "module-remap-source", f"source_name={self.source_name}",
                f"master={self.sink_name}.monitor", f"channels={MIC_CHANNELS}",
                "channel_map=mono", "master_channel_map=mono", "remix=no",
                f"source_properties=\"device.description='{MIC_DESCRIPTION}'\"",
                marker=f"source_name={self.source_name}"))
            # A short leaky queue: if the phone's clock runs fast or the network delivers
            # a burst, old audio is dropped instead of the delay growing. The limit is in
            # bytes because buffers stamped on arrival all carry (nearly) the same time,
            # so a time limit would never see a burst as "full".
            backlog = MIC_RATE * MIC_BACKLOG_MS // 1000 * P.pcm_frame_size(MIC_CHANNELS)
            pipeline = self._launch(
                f"appsrc name=src is-live=true format=time do-timestamp=true "
                f"caps=audio/x-raw,format=S16LE,rate={MIC_RATE},channels={MIC_CHANNELS},layout=interleaved "
                f"! queue max-size-bytes={backlog} max-size-buffers=0 max-size-time=0 leaky=downstream "
                f"! audioconvert ! audioresample "
                f"! pulsesink device={self.sink_name} buffer-time=60000 latency-time=10000 sync=false "
                f"client-name=foldtop-host"
            )
            self._src = pipeline.get_by_name("src")
            self._play("the virtual microphone")
            if self.make_default:
                self._claim_default()
        except Exception:
            self.stop()
            raise

    def _claim_default(self) -> None:
        """Make this device the default input, remembering what was the default."""
        code, out, _err = pactl("get-default-source", timeout=3.0)
        previous = out.strip() if code == 0 else ""
        code, _out, err = pactl("set-default-source", self.source_name, timeout=3.0)
        if code != 0:
            log.warning("could not make %s the default input (%s)", self.source_name, err.strip() or "pactl failed")
            return
        self._claimed_default = True
        # Nothing to restore if it already was the default (a previous session left it so).
        self._previous_default = previous if previous and previous != self.source_name else None
        log.info("%s is now the default input (was %s)", self.source_name, previous or "unknown")

    def _release_default(self) -> None:
        """Put the earlier default input back, if this device is still the default."""
        if not self._claimed_default:
            return
        self._claimed_default = False
        previous, self._previous_default = self._previous_default, None
        if not previous:
            return      # the audio server picks another default once the device is gone
        code, out, _err = pactl("get-default-source", timeout=3.0)
        if code != 0 or out.strip() != self.source_name:
            return      # somebody chose a different default meanwhile: leave it
        if not source_exists(previous):
            return
        code, _out, err = pactl("set-default-source", previous, timeout=3.0)
        if code == 0:
            log.info("default input restored to %s", previous)
        else:
            log.warning("could not restore the default input to %s (%s)", previous, err.strip() or "pactl failed")

    def stop(self) -> None:
        with self._push_lock:
            self._src = None
        # Before the device disappears, so applications move back in one step.
        self._release_default()
        self._halt()
        modules, self._modules = self._modules, []
        for ident in reversed(modules):     # the source first: it depends on the sink
            self._manager.unload_module(ident)

    def push(self, pcm: bytes, now: Optional[float] = None) -> bool:
        """Feed one MIC message. Returns False (and ignores it) if it is not whole
        frames, is longer than the per-message limit, the microphone is stopped, or
        audio is arriving faster than it can be played.

        The last case is what keeps the microphone live: audio that arrives in a
        burst (the network stalled, then delivered a second at once) or steadily too
        fast (the phone's clock runs fast) would otherwise pile up in the audio server
        and everything said afterwards would come out late. At most MIC_BACKLOG_MS
        more than real time is accepted; the excess is dropped.
        """
        if not P.pcm_is_whole_frames(pcm, MIC_RATE, MIC_CHANNELS):
            self.rejected += 1
            return False
        now = time.monotonic() if now is None else now
        duration = len(pcm) / (MIC_RATE * P.pcm_frame_size(MIC_CHANNELS))
        with self._push_lock:
            src = self._src
            if src is None:
                return False
            if self._pace_start is None or now - self._last_push > 0.25:
                self._pace_start, self._pace_accepted = now, 0.0     # first audio, or after a pause
            ahead = self._pace_accepted - (now - self._pace_start)
            if ahead < 0:                                            # fell behind: no credit is saved up
                self._pace_start, self._pace_accepted, ahead = now, 0.0, 0.0
            self._last_push = now
            if ahead > MIC_BACKLOG_MS / 1000:
                self.dropped += 1
                return False
            self._pace_accepted += duration
            Gst = gst()
            result = src.emit("push-buffer", Gst.Buffer.new_wrapped(bytes(pcm)))
        if result != Gst.FlowReturn.OK:
            return False
        self.pushed += 1
        return True
