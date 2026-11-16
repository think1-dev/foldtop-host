"""Shared test plumbing."""

from __future__ import annotations

import asyncio
import contextlib
import os
import ssl
import sys
import tempfile
from pathlib import Path
from typing import Optional

HOST_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = HOST_DIR.parent
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

from foldtop_host import auth  # noqa: E402
from foldtop_host.config import Config  # noqa: E402

# In the standalone companion repository `docs/` sits beside the package; in the
# full project it is one level up, shared with the app's tests.
VECTORS_PATH = (HOST_DIR if (HOST_DIR / "docs").is_dir() else REPO_DIR) / "docs" / "vectors.json"

# Expected failures (bad codes, protocol errors) are logged by the daemon; keep the
# test output readable unless FOLDTOP_TEST_LOG=1.
import logging  # noqa: E402

if os.environ.get("FOLDTOP_TEST_LOG"):
    logging.basicConfig(level=logging.DEBUG, format="%(levelname)s %(name)s: %(message)s")
else:
    _log = logging.getLogger("foldtop")
    _log.addHandler(logging.NullHandler())
    _log.propagate = False

# A synthetic source in ximagesrc's raw format. `{fps}` is filled in by the server.
SNOW_SOURCE = ("videotestsrc is-live=true pattern=snow "
               "! video/x-raw,format=BGRx,width=640,height=360,framerate={fps}/1")
SMPTE_SOURCE = ("videotestsrc is-live=true pattern=smpte horizontal-speed=3 "
                "! video/x-raw,format=BGRx,width=640,height=360,framerate={fps}/1")


def have_display() -> bool:
    if not os.environ.get("DISPLAY"):
        return False
    try:
        from foldtop_host import x11

        x11.Display().close()
        return True
    except Exception:
        return False


def have_gstreamer() -> bool:
    try:
        from foldtop_host import capture

        return all(capture.element_exists(e) for e in capture.required_elements()) and \
            capture.detect_encoder().h264 is not None
    except Exception:
        return False


HAVE_DISPLAY = have_display()
HAVE_GST = have_gstreamer()


class ServerHarness:
    """A FoldtopServer on 127.0.0.1 with a throw-away state directory."""

    def __init__(self, *, test_source: Optional[str] = None, advertise: bool = False, **overrides) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="foldtop-test-")
        self.dir = Path(self._tmp.name)
        self.config = Config.load(self.dir, port=0, bind=["127.0.0.1"], advertise=advertise,
                                  test_source=test_source, **overrides)
        self.server = None

    async def __aenter__(self) -> "ServerHarness":
        from foldtop_host.server import FoldtopServer

        self.server = FoldtopServer(self.config)
        await self.server.start()
        return self

    async def __aexit__(self, *_exc) -> None:
        if self.server is not None:
            await self.server.close()
        self._tmp.cleanup()

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def fp(self) -> bytes:
        return self.server.identity.cert_fp

    def open_pairing(self) -> str:
        return self.server.open_pairing().code

    async def paired_client(self, name: str = "test-device", **kwargs):
        """Returns (client, credentials) for a freshly paired, connected client."""
        from foldtop_host.refclient import RefClient

        client = RefClient("127.0.0.1", self.port, device_name=name, **kwargs)
        await client.connect(self.fp)
        creds = await client.pair(self.open_pairing())
        return client, creds

    async def device_client(self, creds, name: str = "test-device"):
        from foldtop_host.refclient import RefClient

        client = RefClient("127.0.0.1", self.port, device_id=creds.device_id, device_name=name)
        await client.connect(creds.cert_fp)
        await client.authenticate(creds.device_key)
        return client


class MitmProxy:
    """A TLS-terminating relay with its own certificate — what an active attacker on
    the LAN would run. It forwards every decrypted byte in both directions."""

    def __init__(self, upstream_port: int) -> None:
        self._upstream_port = upstream_port
        self._tmp = tempfile.TemporaryDirectory(prefix="foldtop-mitm-")
        self.identity = auth.HostIdentity.load_or_create(Path(self._tmp.name), "mitm")
        self._server = None
        self.port = 0
        self.bytes_relayed = 0

    async def __aenter__(self) -> "MitmProxy":
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(self.identity.cert_path), str(self.identity.key_path))
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0, ssl=ctx)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc) -> None:
        self._server.close()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self._server.wait_closed(), 2.0)
        self._tmp.cleanup()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", self._upstream_port, ssl=ctx)
        except OSError:
            writer.close()
            return

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    self.bytes_relayed += len(data)
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, ssl.SSLError, OSError):
                pass
            finally:
                with contextlib.suppress(Exception):
                    dst.close()

        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))


def decode_h264(access_units: list[bytes], timeout: float = 10.0) -> list[tuple[int, int]]:
    """Decode Annex-B access units with avdec_h264; returns (width, height) per decoded
    frame. Raises on a decoder error."""
    from foldtop_host import capture

    Gst = capture.gst()
    pipeline = Gst.parse_launch(
        "appsrc name=src is-live=false format=time "
        "caps=video/x-h264,stream-format=byte-stream,alignment=au "
        "! avdec_h264 ! appsink name=sink sync=false emit-signals=false max-buffers=0 drop=false"
    )
    src = pipeline.get_by_name("src")
    sink = pipeline.get_by_name("sink")
    pipeline.set_state(Gst.State.PLAYING)
    try:
        for index, au in enumerate(access_units):
            buf = Gst.Buffer.new_wrapped(au)
            buf.pts = index * Gst.SECOND // 60
            buf.duration = Gst.SECOND // 60
            result = src.emit("push-buffer", buf)
            if result != Gst.FlowReturn.OK:
                raise RuntimeError(f"push-buffer returned {result}")
        src.emit("end-of-stream")
        sizes: list[tuple[int, int]] = []
        bus = pipeline.get_bus()
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sample = sink.emit("try-pull-sample", 100 * Gst.MSECOND)
            if sample is not None:
                s = sample.get_caps().get_structure(0)
                sizes.append((s.get_value("width"), s.get_value("height")))
                continue
            msg = bus.pop_filtered(Gst.MessageType.ERROR | Gst.MessageType.EOS)
            if msg is not None:
                if msg.type == Gst.MessageType.ERROR:
                    err, debug = msg.parse_error()
                    raise RuntimeError(f"decoder error: {err.message} {debug or ''}")
                break
        # drain anything decoded just before EOS was observed
        while True:
            sample = sink.emit("try-pull-sample", 50 * Gst.MSECOND)
            if sample is None:
                break
            s = sample.get_caps().get_structure(0)
            sizes.append((s.get_value("width"), s.get_value("height")))
        return sizes
    finally:
        pipeline.set_state(Gst.State.NULL)


async def adecode_h264(access_units: list[bytes]) -> list[tuple[int, int]]:
    """decode_h264 off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(None, decode_h264, access_units)


# --------------------------------------------------------------------------- audio

import itertools  # noqa: E402
import math  # noqa: E402
import shutil  # noqa: E402
import struct  # noqa: E402
import subprocess  # noqa: E402
import time as _time  # noqa: E402

# Every device these tests create carries this prefix, so leftovers are easy to spot
# (`pactl list short modules | grep foldtop`) and never collide with a running daemon.
AUDIO_TEST_PREFIX = f"foldtop_t{os.getpid()}"
_audio_names = itertools.count(1)


def audio_test_name(kind: str = "dev") -> str:
    return f"{AUDIO_TEST_PREFIX}_{kind}{next(_audio_names)}"


def run_pactl(*args: str) -> str:
    result = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=10,
                            env=dict(os.environ, LC_ALL="C"))
    if result.returncode != 0:
        raise RuntimeError(f"pactl {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout.strip()


def have_audio() -> bool:
    """A PulseAudio-protocol server (PipeWire or PulseAudio) and the GStreamer elements."""
    if shutil.which("pactl") is None:
        return False
    try:
        run_pactl("info")
        from foldtop_host import audio, capture

        return all(capture.element_exists(e) for e in (*audio.SOUND_ELEMENTS, *audio.MIC_ELEMENTS, "audiotestsrc"))
    except Exception:
        return False


HAVE_AUDIO = have_audio()


def audio_modules(containing: str) -> list[str]:
    """Lines of `pactl list short modules` that mention `containing`."""
    return [line for line in run_pactl("list", "short", "modules").splitlines() if containing in line]


def unload_audio_test_modules() -> int:
    """Last-resort cleanup: unload every module these tests created."""
    removed = 0
    for line in reversed(audio_modules(AUDIO_TEST_PREFIX)):
        ident = line.split("\t", 1)[0].strip()
        if ident.isdigit():
            subprocess.run(["pactl", "unload-module", ident], capture_output=True, timeout=10)
            removed += 1
    return removed


def source_description(name: str) -> Optional[str]:
    """The human-readable description of a source, or None if it does not exist."""
    current = None
    for line in run_pactl("list", "sources").splitlines():
        line = line.strip()
        if line.startswith("Name: "):
            current = line[6:]
        elif line.startswith("Description: ") and current == name:
            return line[13:]
    return None


class NullSink:
    """A private sink that goes nowhere: audio played into it is never audible, and
    its monitor can be captured. Unloaded on exit."""

    def __init__(self, name: Optional[str] = None) -> None:
        self.name = name or audio_test_name("sink")
        self.monitor = f"{self.name}.monitor"
        self._module: Optional[str] = None

    def __enter__(self) -> "NullSink":
        self._module = run_pactl("load-module", "module-null-sink", f"sink_name={self.name}",
                                 "rate=48000", "channels=2",
                                 f"sink_properties=\"device.description='Foldtop test sink (silent)'\"")
        return self

    def __exit__(self, *_exc) -> None:
        if self._module:
            subprocess.run(["pactl", "unload-module", self._module], capture_output=True, timeout=10)
            self._module = None


class Tone:
    """Plays a sine into a sink for as long as the context is open."""

    def __init__(self, sink: str, *, freq: int = 440, volume: float = 0.2) -> None:
        self._description = (f"audiotestsrc is-live=true freq={freq} volume={volume} "
                             f"! audio/x-raw,format=S16LE,rate=48000,channels=2 ! pulsesink device={sink}")
        self._pipeline = None

    def __enter__(self) -> "Tone":
        from foldtop_host import capture

        Gst = capture.gst()
        self._pipeline = Gst.parse_launch(self._description)
        self._pipeline.set_state(Gst.State.PLAYING)
        return self

    def __exit__(self, *_exc) -> None:
        if self._pipeline is not None:
            from foldtop_host import capture

            self._pipeline.set_state(capture.gst().State.NULL)
            self._pipeline = None


def record_source(source: str, seconds: float) -> bytes:
    """Blocking: record S16LE 48 kHz mono from a source for `seconds`."""
    from foldtop_host import capture

    Gst = capture.gst()
    pipeline = Gst.parse_launch(
        f"pulsesrc device={source} buffer-time=40000 latency-time=10000 provide-clock=false "
        "! audio/x-raw,format=S16LE,rate=48000,channels=1,layout=interleaved "
        "! appsink name=sink emit-signals=true sync=false max-buffers=0 drop=false")
    recorded = bytearray()

    def on_sample(sink):
        sample = sink.emit("pull-sample")
        if sample is not None:
            buf = sample.get_buffer()
            recorded.extend(buf.extract_dup(0, buf.get_size()))
        return Gst.FlowReturn.OK

    pipeline.get_by_name("sink").connect("new-sample", on_sample)
    pipeline.set_state(Gst.State.PLAYING)
    try:
        _time.sleep(seconds)
    finally:
        pipeline.set_state(Gst.State.NULL)
    return bytes(recorded)


def sine_pcm(freq: float, frames: int, *, amplitude: int = 8000, start: int = 0) -> bytes:
    """`frames` mono S16LE samples of a sine, continuing from sample index `start`."""
    return struct.pack(f"<{frames}h", *(int(amplitude * math.sin(2 * math.pi * freq * (start + i) / 48000))
                                        for i in range(frames)))


def samples_of(pcm: bytes) -> tuple:
    count = len(pcm) // 2
    return struct.unpack(f"<{count}h", pcm[:count * 2])


def loud_span(samples: tuple, threshold: int = 2000) -> tuple:
    """The part of a recording between the first and last sample above `threshold`."""
    first = next((i for i, v in enumerate(samples) if abs(v) > threshold), None)
    if first is None:
        return ()
    last = len(samples) - next(i for i, v in enumerate(reversed(samples)) if abs(v) > threshold)
    return samples[first:last]


def rms(samples: tuple) -> float:
    return math.sqrt(sum(v * v for v in samples) / len(samples)) if samples else 0.0


def zero_crossing_frequency(samples: tuple, rate: int = 48000) -> float:
    """A rough dominant frequency: sign changes per second, halved."""
    if len(samples) < 2:
        return 0.0
    crossings = sum(1 for a, b in zip(samples, samples[1:]) if (a < 0) != (b < 0))
    return crossings / 2 / (len(samples) / rate)
