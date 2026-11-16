"""Reference client: the smallest correct implementation of the client side of
docs/PROTOCOL.md. Used by the test-suite, and handy for checking a host without a
phone:

    python3 -m foldtop_host.refclient --host 127.0.0.1 --pair ABCD-EFGH --creds /tmp/c.json
    python3 -m foldtop_host.refclient --host 127.0.0.1 --creds /tmp/c.json --frames 120 --dump out.h264
    python3 -m foldtop_host.refclient --host 127.0.0.1 --creds /tmp/c.json --sound 3
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import ssl
import struct
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from . import DEFAULT_PORT, PROTOCOL_VERSION, auth
from . import protocol as P
from .protocol import Msg


class AuthFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PinMismatch(Exception):
    """The server's certificate is not the pinned one."""


class ServerProofMismatch(Exception):
    """The server could not prove it knows the pairing code (possible MITM)."""


@dataclass
class Credentials:
    device_id: str
    device_key: bytes
    cert_fp: bytes
    host_id: str = ""
    host_name: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"deviceId": self.device_id, "deviceKey": auth.b64e(self.device_key),
                "certFP": self.cert_fp.hex(), "hostId": self.host_id, "hostName": self.host_name}

    @classmethod
    def from_json(cls, obj: dict[str, Any]) -> "Credentials":
        return cls(device_id=obj["deviceId"], device_key=auth.b64d(obj["deviceKey"], length=32),
                   cert_fp=bytes.fromhex(obj["certFP"]), host_id=obj.get("hostId", ""),
                   host_name=obj.get("hostName", ""))


@dataclass
class VideoFrame:
    epoch: int
    keyframe: bool
    pts_micros: int
    data: bytes
    received_at: float = field(default_factory=time.monotonic)


@dataclass
class SoundChunk:
    pts_micros: int
    pcm: bytes
    received_at: float = field(default_factory=time.monotonic)


def pcm_rms(pcm: bytes) -> float:
    """Root-mean-square level of S16LE samples (0 for silence, ~23170 for a full-scale sine)."""
    count = len(pcm) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", pcm[:count * 2])
    return math.sqrt(sum(v * v for v in samples) / count)


class RefClient:
    def __init__(self, host: str, port: int = DEFAULT_PORT, *, device_id: Optional[str] = None,
                 device_name: str = "refclient") -> None:
        self.host = host
        self.port = port
        self.device_id = device_id or str(uuid.uuid4())
        self.device_name = device_name
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.cert_fp = b""
        self.client_nonce = b""
        self.server_nonce = b""
        self.challenge: dict[str, Any] = {}
        self.auth_ok: dict[str, Any] = {}
        self.stream_config: Optional[dict[str, Any]] = None
        self.configs: list[dict[str, Any]] = []
        self.stats: list[dict[str, Any]] = []
        self.cursor_shapes: list[tuple] = []
        self.cursor_positions: list[tuple] = []
        self.pongs: list[int] = []
        self.pings_seen = 0
        self.bye: Optional[dict[str, Any]] = None
        self.frames: asyncio.Queue[VideoFrame] = asyncio.Queue()
        self.stale_frames = 0
        self.audio_config: Optional[dict[str, Any]] = None
        self.audio_configs: list[dict[str, Any]] = []
        self.sound: list[SoundChunk] = []
        self.clipboard: list[tuple[int, str]] = []      # (flags, text) of every CLIPBOARD received
        self._pump: Optional[asyncio.Task] = None
        self.eof = asyncio.Event()

    # ------------------------------------------------------------------ connection

    async def connect(self, pinned_fp: Optional[bytes] = None) -> dict[str, Any]:
        """TLS connect, check the pin (if any), exchange HELLO/CHALLENGE."""
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # trust is the fingerprint pin below, not a CA
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port, ssl=ctx)
        der = self.writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        self.cert_fp = hashlib.sha256(der).digest()
        if pinned_fp is not None and not auth.constant_time_equal(self.cert_fp, pinned_fp):
            await self.close()
            raise PinMismatch(f"server presented {self.cert_fp.hex()[:16]}…, pinned {pinned_fp.hex()[:16]}…")
        self.client_nonce = auth.new_nonce()
        self._send_json(Msg.HELLO, {
            "v": PROTOCOL_VERSION, "deviceId": self.device_id, "deviceName": self.device_name,
            "clientNonce": auth.b64e(self.client_nonce), "app": "foldtop-refclient/1.0",
        })
        msg_type, payload = await P.read_frame(self.reader)
        if msg_type == Msg.AUTH_FAIL:
            raise AuthFailed(P.parse_json(payload).get("reason", "?"))
        if msg_type != Msg.CHALLENGE:
            raise P.ProtocolError(f"expected CHALLENGE, got 0x{msg_type:02x}")
        self.challenge = P.parse_json(payload)
        self.server_nonce = auth.b64d(self.challenge["serverNonce"], length=auth.NONCE_LENGTH)
        return self.challenge

    async def pair(self, code: str, *, observed_fp: Optional[bytes] = None) -> Credentials:
        """Pair with a code. `observed_fp` overrides the fingerprint used in the proof
        (tests use it to simulate a relayed proof)."""
        fp = observed_fp if observed_fp is not None else self.cert_fp
        k = await asyncio.get_running_loop().run_in_executor(
            None, auth.pair_key, code, self.client_nonce, self.server_nonce)
        self._send_json(Msg.AUTH, {"mode": "pair", "proof": auth.b64e(auth.client_proof(k, fp, self.device_id))})
        ok = await self._read_auth_result()
        try:
            proof = auth.b64d(ok.get("serverProof"), length=32)
        except ValueError:
            proof = b""
        if not auth.constant_time_equal(proof, auth.server_proof(k, fp, self.device_id)):
            await self.close()
            raise ServerProofMismatch("server did not prove knowledge of the pairing code")
        self._begin()
        return Credentials(device_id=self.device_id, device_key=auth.derive_device_key(k, fp, self.device_id),
                           cert_fp=fp, host_id=ok.get("hostId", ""), host_name=ok.get("hostName", ""))

    async def authenticate(self, device_key: bytes) -> dict[str, Any]:
        mac = auth.device_mac(device_key, self.client_nonce, self.server_nonce, self.cert_fp, self.device_id)
        self._send_json(Msg.AUTH, {"mode": "device", "mac": auth.b64e(mac)})
        ok = await self._read_auth_result()
        self._begin()
        return ok

    async def _read_auth_result(self) -> dict[str, Any]:
        assert self.reader is not None
        msg_type, payload = await P.read_frame(self.reader)
        if msg_type == Msg.AUTH_FAIL:
            reason = P.parse_json(payload).get("reason", "?")
            await self.close()
            raise AuthFailed(reason)
        if msg_type != Msg.AUTH_OK:
            raise P.ProtocolError(f"expected AUTH_OK, got 0x{msg_type:02x}")
        self.auth_ok = P.parse_json(payload)
        return self.auth_ok

    def _begin(self) -> None:
        self._pump = asyncio.create_task(self._receive())

    async def close(self) -> None:
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None
        if self.writer is not None:
            try:
                self.writer.close()
                await asyncio.wait_for(self.writer.wait_closed(), 2.0)
            except Exception:
                pass
            self.writer = None

    # ------------------------------------------------------------------ session

    def _send(self, msg_type: int, payload: bytes = b"") -> None:
        assert self.writer is not None
        self.writer.write(P.pack_frame(msg_type, payload))

    def _send_json(self, msg_type: int, obj: dict[str, Any]) -> None:
        self._send(msg_type, P.json_bytes(obj))

    def start(self, *, max_width: int, max_height: int, fps: int = 60, bitrate_kbps: int = 12000,
              monitor: str = "primary", codec: str = "h264", cursor: str = "local") -> None:
        self._send_json(Msg.START, {"monitor": monitor, "maxWidth": max_width, "maxHeight": max_height,
                                    "fps": fps, "bitrateKbps": bitrate_kbps, "codec": codec, "cursor": cursor})

    def stop(self) -> None:
        self._send(Msg.STOP)

    def request_keyframe(self) -> None:
        self._send(Msg.KEYFRAME)

    def tune(self, bitrate_kbps: int) -> None:
        self._send_json(Msg.TUNE, {"bitrateKbps": bitrate_kbps})

    def send_input(self, *events: P.InputEvent) -> None:
        self._send(Msg.INPUT, P.encode_input(events))

    def ping(self, value: Optional[int] = None) -> int:
        value = time.monotonic_ns() // 1000 if value is None else value
        self._send(Msg.PING, P.pack_ping(value))
        return value

    def audio_control(self, *, sound: bool = False, microphone: bool = False) -> None:
        """Ask for sound from the host and/or offer this client's microphone."""
        self._send_json(Msg.AUDIO_CONTROL, {"sound": sound, "microphone": microphone})

    def send_mic(self, pcm: bytes) -> None:
        """One MIC message: S16LE PCM in the format AUDIO_CONFIG.microphone gave."""
        self._send(Msg.MIC, pcm)

    def clipboard_control(self, *, receive: bool) -> None:
        """Ask the host to send (or stop sending) what is copied on the workstation."""
        self._send_json(Msg.CLIPBOARD_CONTROL, {"receive": receive})

    def send_clipboard(self, text: str, *, paste: bool = False) -> None:
        """Put text on the workstation's clipboard; with `paste`, also paste it there."""
        self._send(Msg.CLIPBOARD, P.pack_clipboard(P.CLIPBOARD_FLAG_PASTE if paste else 0, text))

    def send_bye(self, reason: str = "client-quit") -> None:
        self._send_json(Msg.BYE, {"reason": reason})

    def send_raw(self, msg_type: int, payload: bytes = b"") -> None:
        self._send(msg_type, payload)

    async def drain(self) -> None:
        assert self.writer is not None
        await self.writer.drain()

    async def _receive(self) -> None:
        assert self.reader is not None
        try:
            while True:
                msg_type, payload = await P.read_frame(self.reader)
                if msg_type == Msg.VIDEO:
                    epoch, flags, pts, data = P.unpack_video(payload)
                    if self.stream_config is None or epoch != self.stream_config["epoch"]:
                        self.stale_frames += 1
                        continue
                    self.frames.put_nowait(VideoFrame(epoch, bool(flags & P.VIDEO_FLAG_KEYFRAME), pts, data))
                elif msg_type == Msg.STREAM_CONFIG:
                    self.stream_config = P.parse_json(payload)
                    self.configs.append(self.stream_config)
                    while not self.frames.empty():  # frames of the previous epoch are void
                        self.frames.get_nowait()
                elif msg_type == Msg.PING:
                    self.pings_seen += 1
                    self._send(Msg.PONG, payload[:8])
                elif msg_type == Msg.PONG:
                    self.pongs.append(P.unpack_ping(payload))
                elif msg_type == Msg.STATS:
                    self.stats.append(P.parse_json(payload))
                elif msg_type == Msg.CURSOR_SHAPE:
                    self.cursor_shapes.append(P.unpack_cursor_shape(payload))
                elif msg_type == Msg.CURSOR_POS:
                    self.cursor_positions.append(P.unpack_cursor_pos(payload))
                elif msg_type == Msg.AUDIO_CONFIG:
                    self.audio_config = P.parse_json(payload)
                    self.audio_configs.append(self.audio_config)
                elif msg_type == Msg.SOUND:
                    pts, pcm = P.unpack_sound(payload)
                    self.sound.append(SoundChunk(pts, pcm))
                elif msg_type == Msg.CLIPBOARD:
                    self.clipboard.append(P.unpack_clipboard(payload))
                elif msg_type == Msg.BYE:
                    self.bye = P.parse_json(payload)
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError, OSError):
            pass
        except asyncio.CancelledError:
            raise
        finally:
            self.eof.set()

    async def next_frame(self, timeout: float = 5.0) -> VideoFrame:
        return await asyncio.wait_for(self.frames.get(), timeout)

    async def collect(self, count: int, timeout: float = 15.0) -> list[VideoFrame]:
        deadline = time.monotonic() + timeout
        out: list[VideoFrame] = []
        while len(out) < count:
            out.append(await self.next_frame(max(deadline - time.monotonic(), 0.01)))
        return out

    async def wait_audio_config(self, after: int = 0, timeout: float = 8.0) -> dict[str, Any]:
        """The next AUDIO_CONFIG once more than `after` have arrived."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.audio_configs) > after:
                return self.audio_configs[after]
            await asyncio.sleep(0.01)
        raise asyncio.TimeoutError("no AUDIO_CONFIG")

    async def wait_sound(self, count: int, timeout: float = 8.0) -> list[SoundChunk]:
        """Wait until `count` more SOUND messages have arrived; returns them."""
        start = len(self.sound)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.sound) - start >= count:
                return self.sound[start:start + count]
            await asyncio.sleep(0.01)
        raise asyncio.TimeoutError(f"only {len(self.sound) - start} of {count} SOUND messages arrived")

    async def wait_config(self, epoch_after: int = -1, timeout: float = 5.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.stream_config is not None and self.stream_config["epoch"] != epoch_after:
                return self.stream_config
            await asyncio.sleep(0.01)
        raise asyncio.TimeoutError("no STREAM_CONFIG")


# --------------------------------------------------------------------------- CLI

async def _main(args: argparse.Namespace) -> int:
    creds_path = Path(args.creds) if args.creds else None
    creds: Optional[Credentials] = None
    if creds_path and creds_path.exists():
        creds = Credentials.from_json(json.loads(creds_path.read_text()))

    client = RefClient(args.host, args.port, device_id=creds.device_id if creds else None, device_name=args.name)
    pin = bytes.fromhex(args.fp) if args.fp else (creds.cert_fp if creds else None)
    challenge = await client.connect(pin)
    print(f"connected to {challenge.get('hostName')} ({client.cert_fp.hex()[:16]}…), "
          f"known={challenge.get('known')} pairing={challenge.get('pairing')}")

    if args.pair:
        creds = await client.pair(args.pair)
        print("paired; server proof verified")
        if creds_path:
            creds_path.write_text(json.dumps(creds.to_json(), indent=2))
            creds_path.chmod(0o600)
    elif creds:
        await client.authenticate(creds.device_key)
        print("authenticated")
    else:
        print("need --pair CODE or --creds FILE with saved credentials", file=sys.stderr)
        return 2
    print("monitors:", client.auth_ok.get("monitors"), "encoder:", client.auth_ok.get("encoder"),
          "audio:", client.auth_ok.get("audio"))

    if args.sound > 0:
        client.audio_control(sound=True)
        config = await client.wait_audio_config()
        print("audio:", config)
        if config.get("sound"):
            await asyncio.sleep(args.sound)
            chunks = list(client.sound)
            total = sum(len(c.pcm) for c in chunks)
            level = pcm_rms(b"".join(c.pcm for c in chunks))
            print(f"{len(chunks)} sound messages, {total} bytes in {args.sound:.1f}s "
                  f"({total / args.sound / 1000:.0f} kB/s), level {level:.0f} of 32767")
        client.audio_control(sound=False)

    if args.frames > 0:
        client.start(max_width=args.width, max_height=args.height, fps=args.fps,
                     bitrate_kbps=args.bitrate, cursor=args.cursor, monitor=args.monitor)
        cfg = await client.wait_config()
        print("stream:", cfg)
        frames = await client.collect(args.frames, timeout=max(15.0, args.frames / max(args.fps, 1) * 3))
        span = frames[-1].received_at - frames[0].received_at
        total = sum(len(f.data) for f in frames)
        print(f"{len(frames)} frames in {span:.2f}s = {(len(frames) - 1) / span if span else 0:.1f} fps, "
              f"{total * 8 / 1000 / span if span else 0:.0f} kbps, first keyframe={frames[0].keyframe}")
        if args.dump:
            with open(args.dump, "wb") as fh:
                for f in frames:
                    fh.write(f.data)
            print("wrote", args.dump)
    client.send_bye()
    await client.drain()
    await client.close()
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="foldtop-refclient", description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--fp", help="expected certificate fingerprint (64 hex chars)")
    ap.add_argument("--pair", metavar="CODE", help="pair with this code")
    ap.add_argument("--creds", help="JSON file to save/load device credentials")
    ap.add_argument("--name", default="refclient")
    ap.add_argument("--frames", type=int, default=0, help="start a stream and receive this many frames")
    ap.add_argument("--width", type=int, default=1840)
    ap.add_argument("--height", type=int, default=1300)
    ap.add_argument("--fps", type=int, default=60)
    ap.add_argument("--bitrate", type=int, default=12000)
    ap.add_argument("--cursor", choices=("local", "video"), default="local")
    ap.add_argument("--monitor", default="primary")
    ap.add_argument("--dump", help="write the received Annex-B stream to this file")
    ap.add_argument("--sound", type=float, default=0.0, metavar="SECONDS",
                    help="ask for the host's sound for this long and report what arrived")
    args = ap.parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except (AuthFailed, PinMismatch, ServerProofMismatch) as exc:
        print(f"failed: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
