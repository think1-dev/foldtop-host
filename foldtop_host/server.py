"""The Foldtop daemon: TLS server, handshake, streaming session, control socket.

One asyncio event loop owns every socket. Blocking work is pushed out:
  * PBKDF2 for pairing            → default executor
  * GStreamer start/stop          → default executor
  * X11 input injection           → a single dedicated thread (keeps events ordered)
  * cursor polling                → CursorTracker thread
  * encoded frames                → GStreamer streaming thread → call_soon_threadsafe
  * sound chunks                  → GStreamer streaming thread → call_soon_threadsafe
  * audio start/stop (pactl)      → default executor
  * clipboard (X selections)      → Clipboard thread → call_soon_threadsafe
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import ssl
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional

from . import PROTOCOL_VERSION, __version__, audio, auth, capture, clipboard, discovery, nal, x11
from . import protocol as P
from .config import Config, local_addresses, pairing_link
from .cursor import CursorTracker
from .inject import Injector
from .protocol import Msg

log = logging.getLogger("foldtop.server")

HANDSHAKE_TIMEOUT = 10.0
IDLE_TIMEOUT = 10.0
PING_INTERVAL = 3.0
STATS_INTERVAL = 1.0
MAX_CAPTURE_RESTARTS = 3

MIN_FPS, DEFAULT_FPS = 5, 60
MIN_BITRATE, MAX_BITRATE, DEFAULT_BITRATE = 500, 150_000, 12_000
MIN_VIEWPORT, MAX_VIEWPORT = 64, 8192
# SOUND is dropped, never queued, once this much is waiting to be written: about 150 ms
# of the video bitrate, and never less than 96 KiB (one large keyframe must not mute it).
MIN_SOUND_BACKLOG = 96 * 1024

# What "paste" presses on the workstation, as USB HID usages. Shift+Insert is the one
# shortcut that pastes in terminals as well as everywhere else (terminals take it from
# the PRIMARY selection, which a paste from the phone therefore sets too).
_SHIFT, _CTRL, _INSERT, _V = 0xE1, 0xE0, 0x49, 0x19
PASTE_KEYS = {
    "shift+insert": (_SHIFT, _INSERT),
    "ctrl+v": (_CTRL, _V),
    "ctrl+shift+v": (_CTRL, _SHIFT, _V),
}
DEFAULT_PASTE_KEYS = "shift+insert"


def paste_events(name: Optional[str]) -> list[P.InputEvent]:
    """The key events for one paste keystroke: every key down in order, then up in reverse."""
    usages = PASTE_KEYS.get((name or DEFAULT_PASTE_KEYS).replace(" ", "").lower(), PASTE_KEYS[DEFAULT_PASTE_KEYS])
    return [P.Key(u, True) for u in usages] + [P.Key(u, False) for u in reversed(usages)]


class _AuthFailure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class StreamParams:
    monitor: str
    max_width: int
    max_height: int
    fps: int
    bitrate_kbps: int
    codec: str
    cursor: str

    @classmethod
    def parse(cls, obj: dict[str, Any], max_fps: int) -> "StreamParams":
        def integer(key: str, default: int, lo: int, hi: int) -> int:
            value = obj.get(key, default)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise P.ProtocolError(f"START.{key} must be a number")
            return max(lo, min(hi, int(value)))

        if "maxWidth" not in obj or "maxHeight" not in obj:
            raise P.ProtocolError("START needs maxWidth and maxHeight")
        monitor = obj.get("monitor", "primary")
        codec = obj.get("codec", nal.H264)
        cursor = obj.get("cursor", "video")
        return cls(
            monitor=monitor if isinstance(monitor, str) else "primary",
            max_width=integer("maxWidth", 0, MIN_VIEWPORT, MAX_VIEWPORT),
            max_height=integer("maxHeight", 0, MIN_VIEWPORT, MAX_VIEWPORT),
            fps=integer("fps", DEFAULT_FPS, MIN_FPS, max_fps),
            bitrate_kbps=integer("bitrateKbps", DEFAULT_BITRATE, MIN_BITRATE, MAX_BITRATE),
            codec=codec if codec in (nal.H264, nal.HEVC) else nal.H264,
            cursor="local" if cursor == "local" else "video",
        )


class FoldtopServer:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.identity = auth.HostIdentity.load_or_create(config.state_dir, config.name)
        self.devices = auth.DeviceStore(config.state_dir)
        self.pairing: Optional[auth.PairingWindow] = None
        self.active: Optional["ClientSession"] = None
        self.sessions: set["ClientSession"] = set()
        self.encoder = capture.detect_encoder(config.encoder)
        self.audio = audio.AudioManager(state_dir=config.state_dir, enabled=config.audio,
                                        sound_source=config.sound_source, mic_name=config.mic_name,
                                        mic_default=config.mic_default)
        self.clipboard: Optional[clipboard.Clipboard] = None
        self._clipboard_lock = threading.Lock()
        self._clipboard_problem_logged = False
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self.started_at = time.time()
        self._server: Optional[asyncio.base_events.Server] = None
        self._control: Optional[asyncio.base_events.Server] = None
        self._advertiser: Optional[discovery.Advertiser] = None
        self._housekeeping: Optional[asyncio.Task] = None
        self._inject_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="foldtop-inject")
        self._injector: Optional[Injector] = None
        self._xinfo: Optional[x11.Display] = None
        self._xinfo_lock = threading.Lock()
        self._pair_gate = asyncio.Semaphore(2)
        self._closed = False
        self.port = config.port

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(str(self.identity.cert_path), str(self.identity.key_path))

        self._server = await asyncio.start_server(
            self._on_connection, host=self.config.bind, port=self.config.port, ssl=ctx,
            ssl_handshake_timeout=HANDSHAKE_TIMEOUT, reuse_address=True,
        )
        self.port = self._server.sockets[0].getsockname()[1]

        sock_path = self.config.control_socket
        with contextlib.suppress(FileNotFoundError):
            sock_path.unlink()
        old_umask = os.umask(0o177)
        try:
            self._control = await asyncio.start_unix_server(self._on_control, path=str(sock_path))
        finally:
            os.umask(old_umask)

        if self.config.advertise:
            loop = asyncio.get_running_loop()
            self._advertiser = discovery.Advertiser(
                name=self.identity.host_name, port=self.port,
                txt=discovery.txt_records(self.identity.host_id, self.identity.host_name, self.identity.cert_fp_hex),
            )
            await loop.run_in_executor(None, self._advertiser.start)

        if self.config.audio:
            # a previous run that was killed may have left its virtual microphone behind
            await asyncio.get_running_loop().run_in_executor(None, self._safe, self.audio.cleanup_stale)
        self._housekeeping = asyncio.create_task(self._housekeep())
        log.info("foldtop-host %s listening on port %d as %r (encoder: %s, fingerprint %s…)",
                 __version__, self.port, self.identity.host_name, self.encoder.name, self.identity.cert_fp_hex[:16])

    async def serve_forever(self) -> None:
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._housekeeping:
            self._housekeeping.cancel()
        for session in list(self.sessions):
            await session.shutdown("shutdown")
        if self._server:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        if self._control:
            self._control.close()
            with contextlib.suppress(Exception):
                await self._control.wait_closed()
            with contextlib.suppress(FileNotFoundError):
                self.config.control_socket.unlink()
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._safe, self.audio.close)
        if self.clipboard is not None:
            shared, self.clipboard = self.clipboard, None
            await loop.run_in_executor(None, self._safe, shared.stop)
        if self._advertiser:
            await loop.run_in_executor(None, self._advertiser.stop)
        if self._injector is not None:
            injector, self._injector = self._injector, None
            await loop.run_in_executor(self._inject_pool, injector.close)
        self._inject_pool.shutdown(wait=True)
        if self._xinfo is not None:
            self._xinfo.close()
            self._xinfo = None

    async def _housekeep(self) -> None:
        loop = asyncio.get_running_loop()
        tick = 0
        while True:
            await asyncio.sleep(1.0)
            tick += 1
            if self._injector is not None:
                with contextlib.suppress(RuntimeError):
                    self._inject_pool.submit(self._safe, self._injector.cleanup_idle)
            if self.pairing is not None and not self.pairing.is_open:
                self.pairing = None
            if self._advertiser is not None and tick % 30 == 0:
                await loop.run_in_executor(None, self._advertiser.ensure)

    @staticmethod
    def _safe(fn, *args) -> None:
        try:
            fn(*args)
        except Exception:
            log.exception("background task failed")

    # ------------------------------------------------------------------ X11 helpers

    def monitors(self) -> list[x11.Monitor]:
        with self._xinfo_lock:  # called from executor threads
            if self._xinfo is None:
                self._xinfo = x11.Display(self.config.effective_display)
            display = self._xinfo
        return display.monitors()

    def monitors_json(self) -> list[dict[str, Any]]:
        try:
            mons = self.monitors()
        except x11.X11Error as exc:
            log.error("cannot enumerate monitors: %s", exc)
            return []
        return [
            {"name": m.name, "x": m.x, "y": m.y, "width": m.width, "height": m.height,
             "primary": m.primary, "virtual": m.name == self.config.virtual_output}
            for m in mons
        ]

    def resolve_monitor(self, wanted: str, max_width: int, max_height: int) -> x11.Monitor:
        """Blocking. Picks the monitor for a START; reshapes the virtual output first
        when one is configured and requested."""
        virtual = self.config.virtual_output
        if wanted == "virtual" and virtual:
            self._reshape_virtual(virtual, max_width, max_height)
            wanted = virtual
        mons = self.monitors()
        for m in mons:
            if m.name == wanted:
                return m
        return next((m for m in mons if m.primary), mons[0])

    def _reshape_virtual(self, output: str, width: int, height: int) -> None:
        """Make the dedicated output exactly the phone's viewport, so the stream has no
        black bars. Only ever runs when `virtual_output` is configured. UNTESTED on the
        development machine (needs a spare connected output or dummy plug)."""
        env = dict(os.environ)
        if self.config.effective_display:
            env["DISPLAY"] = self.config.effective_display
        cmd = ["xrandr", "--output", output, "--scale-from", f"{width}x{height}"]
        try:
            result = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=8)
            if result.returncode != 0:
                log.warning("%s failed: %s", " ".join(cmd), result.stderr.strip())
                return
            time.sleep(0.4)  # let the compositor settle before the first grab
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("could not reshape %s: %s", output, exc)

    def injector(self) -> Injector:
        """Only call on the injection thread."""
        if self._injector is None:
            self._injector = Injector(self.config.effective_display)
        return self._injector

    def submit_input(self, fn_name: str, *args) -> None:
        def run() -> None:
            try:
                getattr(self.injector(), fn_name)(*args)
            except x11.X11Error as exc:
                log.error("input injection unavailable: %s", exc)
            except Exception:
                log.exception("input injection failed")

        try:
            self._inject_pool.submit(run)
        except RuntimeError:
            pass  # shutting down

    def was_recent_abs(self, x: int, y: int) -> bool:
        inj = self._injector
        return inj is not None and inj.was_recent_abs(x, y)

    # ------------------------------------------------------------------ clipboard

    def clipboard_ready(self) -> bool:
        """Blocking. True if the clipboard can be shared; starts sharing on first use."""
        if not self.config.clipboard:
            return False
        with self._clipboard_lock:
            if self.clipboard is not None and self.clipboard.running:
                return True
            shared = clipboard.Clipboard(self.config.effective_display, self._clipboard_copied)
            try:
                shared.start()
            except (clipboard.ClipboardError, x11.X11Error) as exc:
                if not self._clipboard_problem_logged:
                    self._clipboard_problem_logged = True
                    log.warning("the clipboard cannot be shared: %s", exc)
                return False
            self._clipboard_problem_logged = False
            self.clipboard = shared
            return True

    def clipboard_set(self, text: str, primary: bool) -> bool:
        """Blocking. Puts text from the phone on the workstation's clipboard."""
        if not self.clipboard_ready():
            return False
        shared = self.clipboard
        return shared is not None and shared.set_text(text, primary=primary)

    def _clipboard_copied(self, text: str) -> None:
        """Clipboard thread: something was copied on the workstation."""
        loop = self._loop
        if loop is not None and not self._closed:
            with contextlib.suppress(RuntimeError):     # the loop is shutting down
                loop.call_soon_threadsafe(self._send_clipboard, text)

    def _send_clipboard(self, text: str) -> None:
        session = self.active
        if session is None or session.closed or not session.clipboard_receive:
            return
        try:
            payload = P.pack_clipboard(0, text)
        except P.ProtocolError:
            return      # empty, or more than one message carries
        session.send(Msg.CLIPBOARD, payload)
        session.clipboard_sent += 1
        log.info("%s: clipboard to the phone (%d characters)", session.label, len(text))

    # ------------------------------------------------------------------ pairing

    def open_pairing(self, ttl: float = auth.PAIRING_TTL_SECONDS) -> auth.PairingWindow:
        self.pairing = auth.PairingWindow(ttl=ttl)
        log.info("pairing window opened for %.0f s", ttl)
        return self.pairing

    def pairing_is_open(self) -> bool:
        return self.pairing is not None and self.pairing.is_open

    def pairing_info(self) -> dict[str, Any]:
        window = self.pairing
        assert window is not None
        hosts = local_addresses()
        return {
            "code": window.code,
            "formatted": auth.format_code(window.code),
            "expiresIn": round(window.remaining),
            "hosts": hosts,
            "port": self.port,
            "link": pairing_link(hosts=hosts, port=self.port, cert_fp_hex=self.identity.cert_fp_hex,
                                 code=window.code, host_id=self.identity.host_id,
                                 host_name=self.identity.host_name),
        }

    # ------------------------------------------------------------------ connections

    async def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        session = ClientSession(self, reader, writer)
        self.sessions.add(session)
        try:
            await session.run()
        except Exception:
            log.exception("session crashed")
        finally:
            self.sessions.discard(session)
            await session.shutdown(None)

    async def activate(self, session: "ClientSession") -> None:
        previous = self.active
        if previous is not None and previous is not session:
            log.info("%s replaces %s", session.label, previous.label)
            await previous.shutdown("replaced")
        self.active = session

    def deactivate(self, session: "ClientSession") -> None:
        if self.active is session:
            self.active = None

    # ------------------------------------------------------------------ control socket

    async def _on_control(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 5.0)
            request = json.loads(line.decode() or "{}")
            response = await self._control_command(request)
        except Exception as exc:
            response = {"ok": False, "error": str(exc)}
        with contextlib.suppress(Exception):
            writer.write(json.dumps(response).encode() + b"\n")
            await writer.drain()
            writer.close()

    async def _control_command(self, request: dict[str, Any]) -> dict[str, Any]:
        cmd = request.get("cmd")
        if cmd == "pair":
            self.open_pairing(float(request.get("ttl", auth.PAIRING_TTL_SECONDS)))
            return {"ok": True, **self.pairing_info()}
        if cmd == "close-pairing":
            self.pairing = None
            return {"ok": True}
        if cmd == "status":
            active = self.active
            return {
                "ok": True,
                "version": __version__,
                "hostId": self.identity.host_id,
                "hostName": self.identity.host_name,
                "port": self.port,
                "fingerprint": self.identity.cert_fp_hex,
                "encoder": self.encoder.name,
                "codecs": self.encoder.codecs,
                "pairingOpen": self.pairing_is_open(),
                "devices": len(self.devices.all()),
                "advertised": bool(self._advertiser and self._advertiser.name) if self.config.advertise else False,
                "serviceName": self._advertiser.name if self._advertiser else None,
                "client": active.describe() if active else None,
                "uptime": round(time.time() - self.started_at),
            }
        if cmd == "devices":
            self.devices.reload()
            return {"ok": True, "devices": [_device_public(d) for d in self.devices.all()]}
        if cmd == "unpair":
            removed = self.devices.remove(str(request.get("selector", "")))
            for dev in removed:
                for session in list(self.sessions):
                    if session.device_id == dev.device_id:
                        await session.shutdown("shutdown")
            return {"ok": True, "removed": [_device_public(d) for d in removed]}
        return {"ok": False, "error": f"unknown command {cmd!r}"}


def _device_public(dev: auth.Device) -> dict[str, Any]:
    return {"deviceId": dev.device_id, "name": dev.name, "pairedAt": dev.paired_at, "lastSeen": dev.last_seen}


class ClientSession:
    def __init__(self, server: FoldtopServer, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.server = server
        self.reader = reader
        self.writer = writer
        self.loop = asyncio.get_running_loop()
        peer = writer.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if isinstance(peer, tuple) else str(peer)
        self.device_id: Optional[str] = None
        self.device_name = ""
        self.authenticated = False
        self.closed = False
        self.last_rx = time.monotonic()
        self.connected_at = time.time()
        # streaming state
        self.epoch = -1
        self.stream: Optional[capture.CaptureSession] = None
        self.params: Optional[StreamParams] = None
        self.monitor: Optional[x11.Monitor] = None
        self.stream_size = (0, 0)
        self.cursor_tracker: Optional[CursorTracker] = None
        self._reconfig = asyncio.Lock()
        self._need_keyframe = True
        self._congested = False
        self._keyframe_requested = False
        self._restarts: list[float] = []
        self._cursor_epoch = -1
        self._ticker: Optional[asyncio.Task] = None
        # audio (independent of the video stream)
        self.sound: Optional[audio.SoundCapture] = None
        self.mic: Optional[audio.VirtualMicrophone] = None
        self._audio_lock = asyncio.Lock()
        self._sound_gen = 0
        self.sound_sent = 0
        self.sound_dropped = 0
        # clipboard
        self.clipboard_receive = False
        self.clipboard_sent = 0
        self.clipboard_received = 0
        # stats
        self._stat_frames = 0
        self._stat_bytes = 0
        self._stat_since = time.monotonic()
        self.frames_sent = 0
        self.frames_dropped = 0

    @property
    def label(self) -> str:
        return f"{self.device_name or self.device_id or 'client'} ({self.peer})"

    def describe(self) -> dict[str, Any]:
        return {
            "deviceId": self.device_id, "name": self.device_name, "peer": self.peer,
            "streaming": self.stream is not None, "size": list(self.stream_size),
            "monitor": self.monitor.name if self.monitor else None,
            "framesSent": self.frames_sent, "framesDropped": self.frames_dropped,
            "sound": self.sound is not None, "microphone": self.mic is not None,
            "soundSent": self.sound_sent, "soundDropped": self.sound_dropped,
            "clipboard": self.clipboard_receive,
            "clipboardSent": self.clipboard_sent, "clipboardReceived": self.clipboard_received,
        }

    # ------------------------------------------------------------------ sending

    def send(self, msg_type: int, payload: bytes = b"") -> None:
        if self.closed:
            return
        try:
            self.writer.write(P.pack_frame(msg_type, payload))
        except Exception:
            self.closed = True

    def send_json(self, msg_type: int, obj: dict[str, Any]) -> None:
        self.send(msg_type, P.json_bytes(obj))

    def _buffered(self) -> int:
        transport = self.writer.transport
        try:
            return transport.get_write_buffer_size()
        except Exception:
            return 0

    # ------------------------------------------------------------------ main loop

    async def run(self) -> None:
        try:
            await asyncio.wait_for(self._handshake(), HANDSHAKE_TIMEOUT)
        except _AuthFailure as failure:
            log.warning("auth failed for %s: %s", self.peer, failure.reason)
            self.send_json(Msg.AUTH_FAIL, {"reason": failure.reason})
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.writer.drain(), 2.0)
            return
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, OSError):
            return
        except P.ProtocolError as exc:
            log.warning("protocol error during handshake from %s: %s", self.peer, exc)
            self.send_json(Msg.AUTH_FAIL, {"reason": "protocol"})
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.writer.drain(), 2.0)
            return

        self._ticker = asyncio.create_task(self._tick())
        try:
            while not self.closed:
                msg_type, payload = await P.read_frame(self.reader)
                self.last_rx = time.monotonic()
                await self._dispatch(msg_type, payload)
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, OSError):
            pass
        except P.ProtocolError as exc:
            log.warning("protocol error from %s: %s", self.label, exc)
            await self.shutdown("protocol")

    async def _handshake(self) -> None:
        server = self.server
        identity = server.identity

        msg_type, payload = await P.read_frame(self.reader)
        if msg_type != Msg.HELLO:
            raise _AuthFailure("protocol")
        hello = P.parse_json(payload)
        if hello.get("v") != PROTOCOL_VERSION:
            raise _AuthFailure("version")
        device_id = hello.get("deviceId")
        if not isinstance(device_id, str) or not 1 <= len(device_id) <= 128:
            raise _AuthFailure("protocol")
        try:
            client_nonce = auth.b64d(hello.get("clientNonce"), length=auth.NONCE_LENGTH)
        except ValueError:
            raise _AuthFailure("protocol") from None
        name = hello.get("deviceName")
        self.device_id = device_id
        self.device_name = name[:128] if isinstance(name, str) else ""

        server_nonce = auth.new_nonce()
        known = server.devices.get(device_id)
        self.send_json(Msg.CHALLENGE, {
            "v": PROTOCOL_VERSION,
            "hostId": identity.host_id,
            "hostName": identity.host_name,
            "serverNonce": auth.b64e(server_nonce),
            "known": known is not None,
            "pairing": server.pairing_is_open(),
        })

        msg_type, payload = await P.read_frame(self.reader)
        if msg_type != Msg.AUTH:
            raise _AuthFailure("protocol")
        request = P.parse_json(payload)
        mode = request.get("mode")
        ok: dict[str, Any] = {"hostId": identity.host_id, "hostName": identity.host_name}

        if mode == "device":
            if known is None:
                raise _AuthFailure("not-paired")
            try:
                mac = auth.b64d(request.get("mac"), length=32)
            except ValueError:
                raise _AuthFailure("protocol") from None
            expected = auth.device_mac(known.key, client_nonce, server_nonce, identity.cert_fp, device_id)
            if not auth.constant_time_equal(mac, expected):
                raise _AuthFailure("bad-proof")
            server.devices.touch(device_id)
        elif mode == "pair":
            try:
                proof = auth.b64d(request.get("proof"), length=32)
            except ValueError:
                raise _AuthFailure("protocol") from None
            window = server.pairing
            if window is None or not window.is_open:
                raise _AuthFailure("pairing-closed")
            async with server._pair_gate:
                k = await self.loop.run_in_executor(None, auth.pair_key, window.code, client_nonce, server_nonce)
            if not window.is_open:  # consumed or exhausted while we were deriving
                raise _AuthFailure("pairing-closed")
            if not auth.constant_time_equal(proof, auth.client_proof(k, identity.cert_fp, device_id)):
                window.record_failure()
                if not window.is_open:
                    log.warning("pairing window closed after %d failed attempts", window.failures)
                raise _AuthFailure("bad-proof")
            window.consume()
            server.pairing = None
            server.devices.add(device_id, self.device_name, auth.derive_device_key(k, identity.cert_fp, device_id))
            ok["serverProof"] = auth.b64e(auth.server_proof(k, identity.cert_fp, device_id))
            log.info("paired new device %s", self.label)
        else:
            raise _AuthFailure("protocol")

        ok["encoder"] = server.encoder.name
        ok["codecs"] = server.encoder.codecs
        ok["monitors"] = await self.loop.run_in_executor(None, server.monitors_json)
        # Every address a phone could use (LAN first, then VPN such as Tailscale), so a
        # device paired over Wi-Fi can find this machine again from somewhere else.
        ok["addresses"] = await self.loop.run_in_executor(None, local_addresses)
        capabilities = await self.loop.run_in_executor(None, server.audio.capabilities)
        ok["audio"] = capabilities.to_json()
        ok["clipboard"] = await self.loop.run_in_executor(None, server.clipboard_ready)
        self.authenticated = True
        await server.activate(self)
        self.send_json(Msg.AUTH_OK, ok)
        primary = next((m for m in ok["monitors"] if m.get("primary")), None)
        if primary:
            server.submit_input("set_region", primary["x"], primary["y"], primary["width"], primary["height"])
        log.info("%s connected", self.label)

    async def _dispatch(self, msg_type: int, payload: bytes) -> None:
        if msg_type == Msg.INPUT:
            events = P.decode_input(payload)
            if events:
                self.server.submit_input("apply", events)
        elif msg_type == Msg.PING:
            self.send(Msg.PONG, payload[:8])
        elif msg_type == Msg.PONG:
            pass
        elif msg_type == Msg.START:
            await self._start_stream(StreamParams.parse(P.parse_json(payload), self.server.config.max_fps))
        elif msg_type == Msg.STOP:
            async with self._reconfig:
                await self._stop_stream()
        elif msg_type == Msg.KEYFRAME:
            if self.stream is not None:
                self.stream.request_keyframe()
        elif msg_type == Msg.TUNE:
            obj = P.parse_json(payload)
            value = obj.get("bitrateKbps")
            if isinstance(value, (int, float)) and not isinstance(value, bool) and self.stream is not None:
                kbps = max(MIN_BITRATE, min(MAX_BITRATE, int(value)))
                self.stream.set_bitrate(kbps)
                if self.params:
                    self.params.bitrate_kbps = kbps
        elif msg_type == Msg.MIC:
            mic = self.mic
            if mic is not None:      # MIC while the microphone is off is ignored
                mic.push(payload)
        elif msg_type == Msg.AUDIO_CONTROL:
            obj = P.parse_json(payload)
            await self._audio_control(obj.get("sound") is True, obj.get("microphone") is True)
        elif msg_type == Msg.CLIPBOARD:
            flags, text = P.unpack_clipboard(payload)
            await self._clipboard_from_client(text, bool(flags & P.CLIPBOARD_FLAG_PASTE))
        elif msg_type == Msg.CLIPBOARD_CONTROL:
            wanted = P.parse_json(payload).get("receive") is True
            self.clipboard_receive = wanted and await self.loop.run_in_executor(None, self.server.clipboard_ready)
        elif msg_type == Msg.BYE:
            self.closed = True
        # anything else: ignored for forward compatibility

    async def _tick(self) -> None:
        """~1 Hz: STATS while streaming, PING keep-alive, idle timeout."""
        last_ping = time.monotonic()
        try:
            while not self.closed:
                await asyncio.sleep(STATS_INTERVAL)
                now = time.monotonic()
                if now - self.last_rx > IDLE_TIMEOUT:
                    log.info("%s timed out", self.label)
                    await self.shutdown(None)
                    return
                if now - last_ping >= PING_INTERVAL:
                    last_ping = now
                    self.send(Msg.PING, P.pack_ping(time.monotonic_ns() // 1000))
                if self.stream is not None and self.monitor is not None:
                    span = max(now - self._stat_since, 1e-3)
                    self.send_json(Msg.STATS, {
                        "fps": round(self._stat_frames / span, 1),
                        "kbps": round(self._stat_bytes * 8 / 1000 / span, 1),
                        "encoder": self.stream.encoder_name,
                        "captureWidth": self.monitor.width,
                        "captureHeight": self.monitor.height,
                    })
                self._stat_frames = 0
                self._stat_bytes = 0
                self._stat_since = now
        except asyncio.CancelledError:
            pass

    # ------------------------------------------------------------------ streaming

    async def _start_stream(self, params: StreamParams, *, restart: bool = False) -> None:
        async with self._reconfig:
            if self.closed:
                return
            await self._stop_stream()
            server = self.server
            try:
                monitor = await self.loop.run_in_executor(
                    None, server.resolve_monitor, params.monitor, params.max_width, params.max_height)
            except Exception as exc:
                log.error("cannot start stream: %s", exc)
                await self._capture_failed()
                return
            width, height = P.fit_size(monitor.width, monitor.height, params.max_width, params.max_height)
            codec, _element = server.encoder.element_for(params.codec) if server.encoder.h264 else (params.codec, "")
            self.epoch = (self.epoch + 1) % 256
            epoch = self.epoch
            self.params, self.monitor, self.stream_size = params, monitor, (width, height)
            self._need_keyframe, self._congested, self._keyframe_requested = True, False, False
            if not restart:
                self._restarts.clear()

            self.send_json(Msg.STREAM_CONFIG, {
                "epoch": epoch, "codec": codec, "width": width, "height": height, "fps": params.fps,
                "cursor": params.cursor,
                "monitor": {"name": monitor.name, "x": monitor.x, "y": monitor.y,
                            "width": monitor.width, "height": monitor.height},
            })
            server.submit_input("set_region", monitor.x, monitor.y, monitor.width, monitor.height)

            source = server.config.test_source
            try:
                session = capture.CaptureSession(
                    region=(monitor.x, monitor.y, monitor.width, monitor.height),
                    width=width, height=height, fps=params.fps, bitrate_kbps=params.bitrate_kbps,
                    codec=params.codec, show_pointer=(params.cursor != "local"),
                    display=server.config.effective_display, encoder=server.encoder,
                    source=source.format(width=monitor.width, height=monitor.height, fps=params.fps) if source else None,
                    on_frame=lambda data, keyframe, pts, e=epoch: self.loop.call_soon_threadsafe(
                        self._on_frame, e, data, keyframe, pts),
                    on_error=lambda message, e=epoch: self.loop.call_soon_threadsafe(
                        self._on_capture_error, e, message),
                )
                await self.loop.run_in_executor(None, session.start)
            except Exception as exc:
                log.error("capture failed to start: %s", exc)
                await self._capture_failed()
                return
            self.stream = session
            self._stat_frames, self._stat_bytes, self._stat_since = 0, 0, time.monotonic()
            log.info("%s streaming %s %dx%d → %dx%d @%d fps, %d kbps, %s/%s, cursor %s (epoch %d)",
                     self.label, monitor.name, monitor.width, monitor.height, width, height, params.fps,
                     params.bitrate_kbps, session.encoder_name, session.codec, params.cursor, epoch)

            if params.cursor == "local":
                tracker = CursorTracker(
                    server.config.effective_display,
                    on_shape=lambda image, e=epoch: self.loop.call_soon_threadsafe(self._on_cursor_shape, e, image),
                    on_position=lambda x, y, e=epoch: self.loop.call_soon_threadsafe(self._on_cursor_pos, e, x, y),
                )
                tracker.start()
                self.cursor_tracker = tracker

    async def _stop_stream(self) -> None:
        """Caller holds self._reconfig."""
        tracker, self.cursor_tracker = self.cursor_tracker, None
        if tracker is not None:
            tracker.stop()
        session, self.stream = self.stream, None
        if session is not None:
            await self.loop.run_in_executor(None, session.stop)

    def _on_frame(self, epoch: int, data: bytes, keyframe: bool, pts_micros: int) -> None:
        if self.closed or epoch != self.epoch or self.stream is None:
            return
        buffered = self._buffered()
        bitrate = self.params.bitrate_kbps if self.params else DEFAULT_BITRATE
        high = max(512 * 1024, bitrate * 1000 // 8 // 2)  # ≈ half a second of video
        low = high // 8
        if self._congested:
            if buffered > low:
                self.frames_dropped += 1
                return
            if not keyframe:
                if not self._keyframe_requested:
                    self._keyframe_requested = True
                    self.stream.request_keyframe()
                self.frames_dropped += 1
                return
            self._congested = False
        elif buffered > high:
            # The network cannot keep up. Dropping a P-frame breaks every frame after
            # it, so drop everything until the socket drains, then resume on an IDR.
            self._congested, self._keyframe_requested = True, False
            self.frames_dropped += 1
            return
        if self._need_keyframe:
            if not keyframe:
                if not self._keyframe_requested:
                    self._keyframe_requested = True
                    self.stream.request_keyframe()
                self.frames_dropped += 1
                return
            self._need_keyframe = False
        self._keyframe_requested = False
        flags = P.VIDEO_FLAG_KEYFRAME if keyframe else 0
        self.send(Msg.VIDEO, P.pack_video(epoch, flags, pts_micros, data))
        self.frames_sent += 1
        self._stat_frames += 1
        self._stat_bytes += len(data)

    def _on_capture_error(self, epoch: int, message: str) -> None:
        if self.closed or epoch != self.epoch:
            return
        now = time.monotonic()
        self._restarts = [t for t in self._restarts if now - t < 30.0] + [now]
        if len(self._restarts) > MAX_CAPTURE_RESTARTS or self.params is None:
            asyncio.ensure_future(self._capture_failed())
        else:
            log.warning("restarting capture after error: %s", message)
            asyncio.ensure_future(self._restart_stream(self.params))

    async def _restart_stream(self, params: StreamParams) -> None:
        await asyncio.sleep(0.5)
        await self._start_stream(params, restart=True)

    async def _capture_failed(self) -> None:
        asyncio.ensure_future(self.shutdown("capture-failed"))

    def _on_cursor_shape(self, epoch: int, image: x11.CursorImage) -> None:
        if self.closed or epoch != self.epoch or self.stream is None:
            return
        self.send(Msg.CURSOR_SHAPE, P.pack_cursor_shape(
            image.serial, image.width, image.height, image.hot_x, image.hot_y, image.rgba))

    def _on_cursor_pos(self, epoch: int, x: int, y: int) -> None:
        monitor = self.monitor
        if self.closed or epoch != self.epoch or self.stream is None or monitor is None:
            return
        first = self._cursor_epoch != epoch
        self._cursor_epoch = epoch
        if not first and self.server.was_recent_abs(x, y):
            return  # the pointer is where this client put it
        self.send(Msg.CURSOR_POS, P.pack_cursor_pos(
            P.pixel_to_norm(x, monitor.x, monitor.width),
            P.pixel_to_norm(y, monitor.y, monitor.height),
            monitor.contains(x, y)))

    # ------------------------------------------------------------------ audio

    async def _audio_control(self, want_sound: bool, want_microphone: bool) -> None:
        """Bring both directions to what the client asked for, then say what is running."""
        async with self._audio_lock:
            if self.closed:
                return
            manager = self.server.audio
            problems: list[str] = []
            capabilities: Optional[audio.Capabilities] = None
            if (want_sound and self.sound is None) or (want_microphone and self.mic is None):
                capabilities = await self.loop.run_in_executor(None, manager.capabilities)

            if want_sound and self.sound is None:
                if not capabilities.sound:
                    problems.append(capabilities.sound_problem)
                else:
                    self._sound_gen += 1
                    gen = self._sound_gen
                    sound = manager.new_sound(
                        on_chunk=lambda pts, pcm, g=gen: self.loop.call_soon_threadsafe(self._on_sound, g, pts, pcm),
                        on_error=lambda message, g=gen: self.loop.call_soon_threadsafe(
                            self._on_sound_error, g, message))
                    try:
                        await self.loop.run_in_executor(None, sound.start)
                        self.sound = sound
                        log.info("%s: sound on", self.label)
                    except Exception as exc:
                        problems.append(f"sound could not be started ({exc})")
            elif not want_sound and self.sound is not None:
                await self._stop_sound()

            if want_microphone and self.mic is None:
                if not capabilities.microphone:
                    problems.append(capabilities.microphone_problem)
                else:
                    mic = manager.new_microphone()
                    try:
                        await self.loop.run_in_executor(None, mic.start)
                        self.mic = mic
                        log.info("%s: microphone on (input device %r)", self.label, mic.source_name)
                    except Exception as exc:
                        problems.append(f"the microphone could not be set up ({exc})")
            elif not want_microphone and self.mic is not None:
                await self._stop_microphone()

            if self.closed:          # the client went away while this was starting
                await self._stop_sound()
                await self._stop_microphone()
                return
            self._send_audio_config(problems)

    def _send_audio_config(self, problems: list[str]) -> None:
        config: dict[str, Any] = {
            "sound": self.sound.format if self.sound is not None else None,
            "microphone": self.mic.format if self.mic is not None else None,
        }
        if problems:
            config["problem"] = "; ".join(p for p in problems if p)
        self.send_json(Msg.AUDIO_CONFIG, config)

    async def _stop_sound(self) -> None:
        sound, self.sound = self.sound, None
        if sound is not None:
            self._sound_gen += 1     # chunks already queued on the loop are now stale
            await self.loop.run_in_executor(None, sound.stop)
            log.info("%s: sound off", self.label)

    async def _stop_microphone(self) -> None:
        mic, self.mic = self.mic, None
        if mic is not None:
            await self.loop.run_in_executor(None, mic.stop)
            log.info("%s: microphone off", self.label)

    def _sound_backlog_limit(self) -> int:
        bitrate = self.params.bitrate_kbps if self.params else DEFAULT_BITRATE
        return max(MIN_SOUND_BACKLOG, bitrate * 1000 // 8 * 150 // 1000)

    def _on_sound(self, gen: int, pts_micros: int, pcm: bytes) -> None:
        if self.closed or gen != self._sound_gen or self.sound is None:
            return
        # Never queue sound behind video: if the client is not keeping up, this chunk is
        # simply not sent, so what is heard stays in step with what is seen.
        if self._congested or self._buffered() > self._sound_backlog_limit():
            self.sound_dropped += 1
            return
        self.send(Msg.SOUND, P.pack_sound(pts_micros, pcm))
        self.sound_sent += 1

    def _on_sound_error(self, gen: int, message: str) -> None:
        if self.closed or gen != self._sound_gen or self.sound is None:
            return
        asyncio.ensure_future(self._sound_failed(gen, message))

    async def _sound_failed(self, gen: int, message: str) -> None:
        async with self._audio_lock:
            if self.closed or gen != self._sound_gen or self.sound is None:
                return
            await self._stop_sound()
            self._send_audio_config([f"sound stopped ({message})"])

    # ------------------------------------------------------------------ clipboard

    async def _clipboard_from_client(self, text: str, paste: bool) -> None:
        """Text copied on the phone: put it on the workstation's clipboard, and paste it
        if asked. The content is never logged."""
        if self.server.active is not self:
            return
        size = len(text.encode("utf-8"))
        if size == 0 or size > P.MAX_CLIPBOARD_BYTES:
            log.info("%s: ignored a clipboard message of %d bytes", self.label, size)
            return
        # A paste also sets PRIMARY, which is what Shift+Insert pastes in a terminal.
        done = await self.loop.run_in_executor(None, self.server.clipboard_set, text, paste)
        if not done:
            log.warning("%s: could not put the phone's text on the clipboard", self.label)
            return
        self.clipboard_received += 1
        if paste and not self.closed:
            # The clipboard is ours before this is queued, so the keystroke finds it there.
            self.server.submit_input("apply", paste_events(self.server.config.paste_keys))
        log.info("%s: clipboard from the phone (%d characters)%s", self.label, len(text),
                 ", pasted" if paste else "")

    # ------------------------------------------------------------------ teardown

    async def shutdown(self, reason: Optional[str]) -> None:
        """Idempotent. `reason` (if any) is sent to the peer as BYE."""
        if getattr(self, "_shut", False):
            return
        self._shut = True
        was_authenticated = self.authenticated
        if reason is not None and was_authenticated and not self.closed:
            self.send_json(Msg.BYE, {"reason": reason})
        self.closed = True
        if self._ticker is not None:
            self._ticker.cancel()
        with contextlib.suppress(Exception):
            async with self._reconfig:
                await self._stop_stream()
        with contextlib.suppress(Exception):
            async with self._audio_lock:
                await self._stop_sound()
                await self._stop_microphone()
        if was_authenticated:
            if self.server.active is self:
                # nobody is driving any more: let go of everything this client held
                self.server.submit_input("release_all")
            self.server.deactivate(self)
            log.info("%s disconnected%s", self.label, f" ({reason})" if reason else "")
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.writer.drain(), 1.0)
        with contextlib.suppress(Exception):
            self.writer.close()
            await asyncio.wait_for(self.writer.wait_closed(), 2.0)
