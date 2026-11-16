#!/usr/bin/env python3
"""Generate docs/vectors.json — golden byte vectors shared with the Swift tests.

    cd host && python3 tests/gen_vectors.py            # rewrite docs/vectors.json
    cd host && python3 tests/gen_vectors.py --check    # fail if the file is stale

Everything except the `video` section is fully deterministic. `video` holds real
output of the capture pipeline (synthetic source → videoconvertscale → encoder) and
is kept from the existing file unless --video is passed or the file has none.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import threading
from pathlib import Path

HOST_DIR = Path(__file__).resolve().parent.parent
if str(HOST_DIR) not in sys.path:
    sys.path.insert(0, str(HOST_DIR))

from foldtop_host import auth  # noqa: E402
from foldtop_host import protocol as P  # noqa: E402
from foldtop_host.protocol import Msg  # noqa: E402

# In the standalone companion repository `docs/` sits beside the package; in the
# full project it is one level up, shared with the app's tests.
VECTORS_PATH = (HOST_DIR if (HOST_DIR / "docs").is_dir() else HOST_DIR.parent) / "docs" / "vectors.json"

CLIENT_NONCE = bytes(range(32))
SERVER_NONCE = bytes(range(32, 64))
CERT_FP = hashlib.sha256(b"foldtop test certificate").digest()
DEVICE_ID = "6F1D2C3B-4A59-4E87-9B6A-0C1D2E3F4A5B"
HOST_ID = "2d2a6cd9-91e9-4406-bb78-0506e8fec230"
CODE = "K7M2QX9D"

VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_FPS = 320, 180, 30

# Audio: four stereo frames for SOUND and six mono samples for MIC, covering zero,
# both extremes and mixed signs so a byte-order or sign mistake cannot go unnoticed.
SOUND_PTS = 0x0001_0203_0405_0607
SOUND_PCM = struct.pack("<8h", 0, 0, 1000, -1000, 32767, -32768, -1, 1)
MIC_PCM = struct.pack("<6h", 0, 256, -256, 32767, -32768, 12345)


# Clipboard: text with multi-byte characters, a newline and a tab, in both flag states.
CLIPBOARD_CASES: list[tuple[str, int, str]] = [
    ("set", 0, "copied on the phone"),
    ("paste", P.CLIPBOARD_FLAG_PASTE, "naïve café → 日本語 🌍\n\tline 2"),
]


def _event_json(ev: P.InputEvent) -> dict:
    if isinstance(ev, P.MoveAbs):
        return {"op": "moveAbs", "x": ev.x, "y": ev.y}
    if isinstance(ev, P.MoveRel):
        return {"op": "moveRel", "dx": ev.dx, "dy": ev.dy}
    if isinstance(ev, P.Button):
        return {"op": "button", "button": ev.button, "down": ev.down}
    if isinstance(ev, P.Scroll):
        return {"op": "scroll", "dx": ev.dx, "dy": ev.dy}
    if isinstance(ev, P.Key):
        return {"op": "key", "usage": ev.usage, "down": ev.down}
    if isinstance(ev, P.Text):
        return {"op": "text", "text": ev.text}
    if isinstance(ev, P.ReleaseAll):
        return {"op": "releaseAll"}
    raise TypeError(ev)


def event_from_json(obj: dict) -> P.InputEvent:
    op = obj["op"]
    if op == "moveAbs":
        return P.MoveAbs(obj["x"], obj["y"])
    if op == "moveRel":
        return P.MoveRel(obj["dx"], obj["dy"])
    if op == "button":
        return P.Button(obj["button"], obj["down"])
    if op == "scroll":
        return P.Scroll(obj["dx"], obj["dy"])
    if op == "key":
        return P.Key(obj["usage"], obj["down"])
    if op == "text":
        return P.Text(obj["text"])
    if op == "releaseAll":
        return P.ReleaseAll()
    raise ValueError(op)


INPUT_CASES: list[tuple[str, list[P.InputEvent]]] = [
    ("move-abs-origin", [P.MoveAbs(0, 0)]),
    ("move-abs-far-corner", [P.MoveAbs(65535, 65535)]),
    ("move-abs-mixed", [P.MoveAbs(32768, 1), P.MoveAbs(4660, 43981)]),
    ("move-rel-positive", [P.MoveRel(12, 7)]),
    ("move-rel-negative", [P.MoveRel(-3, -250)]),
    ("move-rel-extremes", [P.MoveRel(-32768, 32767)]),
    ("button-left-click", [P.Button(1, True), P.Button(1, False)]),
    ("button-right-and-middle", [P.Button(3, True), P.Button(3, False), P.Button(2, True), P.Button(2, False)]),
    ("scroll-down-one-detent", [P.Scroll(0, 120)]),
    ("scroll-up-fraction", [P.Scroll(0, -15)]),
    ("scroll-horizontal", [P.Scroll(-120, 0), P.Scroll(240, 0)]),
    ("scroll-extremes", [P.Scroll(32767, -32768)]),
    ("key-a-tap", [P.Key(0x04, True), P.Key(0x04, False)]),
    ("key-shift-chord", [P.Key(0xE1, True), P.Key(0x1E, True), P.Key(0x1E, False), P.Key(0xE1, False)]),
    ("key-ctrl-alt-delete", [P.Key(0xE0, True), P.Key(0xE2, True), P.Key(0x4C, True),
                             P.Key(0x4C, False), P.Key(0xE2, False), P.Key(0xE0, False)]),
    ("key-usage-16-bit", [P.Key(0x0123, True), P.Key(0xFFFF, False)]),
    ("text-ascii", [P.Text("Hello, Foldtop!")]),
    ("text-empty", [P.Text("")]),
    ("text-multibyte", [P.Text("naïve café → 日本語 🌍")]),
    ("text-newline-tab", [P.Text("line 1\n\tline 2")]),
    ("release-all", [P.ReleaseAll()]),
    ("mixed-batch", [P.MoveAbs(1000, 64000), P.Button(1, True), P.MoveRel(-5, 9), P.Button(1, False),
                     P.Scroll(-7, 33), P.Key(0x28, True), P.Text("é"), P.Key(0x28, False), P.ReleaseAll()]),
]


def auth_section() -> dict:
    k = auth.pair_key(CODE, CLIENT_NONCE, SERVER_NONCE)
    device_key = auth.derive_device_key(k, CERT_FP, DEVICE_ID)
    return {
        "clientNonceHex": CLIENT_NONCE.hex(),
        "serverNonceHex": SERVER_NONCE.hex(),
        "certFPHex": CERT_FP.hex(),
        "deviceId": DEVICE_ID,
        "code": CODE,
        "kHex": k.hex(),
        "proofHex": auth.client_proof(k, CERT_FP, DEVICE_ID).hex(),
        "serverProofHex": auth.server_proof(k, CERT_FP, DEVICE_ID).hex(),
        "deviceKeyHex": device_key.hex(),
        "macHex": auth.device_mac(device_key, CLIENT_NONCE, SERVER_NONCE, CERT_FP, DEVICE_ID).hex(),
    }


def video_message_section() -> dict:
    data = bytes.fromhex("00000001 09f0 00000001 6742c01e 00000001 68ce3c80 00000001 6588840a".replace(" ", ""))
    epoch, flags, pts = 7, P.VIDEO_FLAG_KEYFRAME, 0x0001_0203_0405_0607
    return {"epoch": epoch, "flags": flags, "ptsMicros": pts, "dataHex": data.hex(),
            "payloadHex": P.pack_video(epoch, flags, pts, data).hex()}


def cursor_shape_section() -> dict:
    # 3x2 premultiplied RGBA: opaque white, opaque black, half-transparent red, clear, …
    rgba = bytes([255, 255, 255, 255, 0, 0, 0, 255, 128, 0, 0, 128,
                  0, 0, 0, 0, 0, 64, 0, 64, 10, 20, 30, 200])
    serial, width, height, hot_x, hot_y = 0x89ABCDEF, 3, 2, 1, 0
    return {"serial": serial, "width": width, "height": height, "hotX": hot_x, "hotY": hot_y,
            "rgbaHex": rgba.hex(), "payloadHex": P.pack_cursor_shape(serial, width, height, hot_x, hot_y, rgba).hex()}


def cursor_pos_section() -> dict:
    x, y, visible = 65535, 258, True
    return {"x": x, "y": y, "visible": visible, "payloadHex": P.pack_cursor_pos(x, y, visible).hex()}


def frames_section(sections: dict) -> list[dict]:
    a = sections["auth"]
    json_frames: list[tuple[str, int, dict]] = [
        ("hello", Msg.HELLO, {"v": 1, "deviceId": DEVICE_ID, "deviceName": "Jordan’s iPhone Duo",
                              "clientNonce": auth.b64e(CLIENT_NONCE), "app": "foldtop-ios/1.0"}),
        ("challenge", Msg.CHALLENGE, {"v": 1, "hostId": HOST_ID, "hostName": "workstation-1",
                                      "serverNonce": auth.b64e(SERVER_NONCE), "known": True, "pairing": False}),
        ("auth-device", Msg.AUTH, {"mode": "device", "mac": auth.b64e(bytes.fromhex(a["macHex"]))}),
        ("auth-pair", Msg.AUTH, {"mode": "pair", "proof": auth.b64e(bytes.fromhex(a["proofHex"]))}),
        ("auth-ok-pair", Msg.AUTH_OK, {
            "hostId": HOST_ID, "hostName": "workstation-1",
            "serverProof": auth.b64e(bytes.fromhex(a["serverProofHex"])),
            "encoder": "x264", "codecs": ["h264"],
            "monitors": [{"name": "DP-4", "x": 0, "y": 0, "width": 4384, "height": 2466,
                          "primary": True, "virtual": False},
                         {"name": "DP-2", "x": 4384, "y": 0, "width": 1840, "height": 1300,
                          "primary": False, "virtual": True}]}),
        ("auth-ok-device", Msg.AUTH_OK, {
            "hostId": HOST_ID, "hostName": "workstation-1", "encoder": "nvenc", "codecs": ["hevc", "h264"],
            "monitors": [{"name": "DP-4", "x": 0, "y": 0, "width": 4384, "height": 2466,
                          "primary": True, "virtual": False}]}),
        ("auth-ok-audio", Msg.AUTH_OK, {
            "hostId": HOST_ID, "hostName": "workstation-1", "encoder": "x264", "codecs": ["h264"],
            "monitors": [{"name": "DP-4", "x": 0, "y": 0, "width": 4384, "height": 2466,
                          "primary": True, "virtual": False}],
            "addresses": ["192.168.0.120", "100.64.10.20"],
            "audio": {"sound": True, "microphone": True}}),
        ("auth-ok-clipboard", Msg.AUTH_OK, {
            "hostId": HOST_ID, "hostName": "workstation-1", "encoder": "x264", "codecs": ["h264"],
            "monitors": [{"name": "DP-4", "x": 0, "y": 0, "width": 4384, "height": 2466,
                          "primary": True, "virtual": False}],
            "addresses": ["192.168.0.120", "100.64.10.20"],
            "audio": {"sound": True, "microphone": True}, "clipboard": True}),
        ("auth-fail", Msg.AUTH_FAIL, {"reason": "bad-proof"}),
        ("start", Msg.START, {"monitor": "primary", "maxWidth": 1840, "maxHeight": 1300, "fps": 60,
                              "bitrateKbps": 12000, "codec": "h264", "cursor": "local"}),
        ("stream-config", Msg.STREAM_CONFIG, {
            "epoch": 3, "codec": "h264", "width": 1840, "height": 1034, "fps": 60, "cursor": "local",
            "monitor": {"name": "DP-4", "x": 0, "y": 0, "width": 4384, "height": 2466}}),
        ("tune", Msg.TUNE, {"bitrateKbps": 8000}),
        ("stats", Msg.STATS, {"fps": 59.8, "kbps": 7421.5, "encoder": "x264",
                              "captureWidth": 4384, "captureHeight": 2466}),
        ("audio-control", Msg.AUDIO_CONTROL, {"sound": True, "microphone": False}),
        ("audio-config", Msg.AUDIO_CONFIG, {"sound": P.audio_format(48000, 2),
                                            "microphone": P.audio_format(48000, 1)}),
        ("clipboard-control", Msg.CLIPBOARD_CONTROL, {"receive": True}),
        ("bye", Msg.BYE, {"reason": "replaced"}),
    ]
    binary_frames: list[tuple[str, int, bytes]] = [
        ("video", Msg.VIDEO, bytes.fromhex(sections["videoMessage"]["payloadHex"])),
        ("stop", Msg.STOP, b""),
        ("keyframe", Msg.KEYFRAME, b""),
        ("input-mixed-batch", Msg.INPUT, P.encode_input(dict(INPUT_CASES)["mixed-batch"])),
        ("cursor-shape", Msg.CURSOR_SHAPE, bytes.fromhex(sections["cursorShape"]["payloadHex"])),
        ("cursor-pos", Msg.CURSOR_POS, bytes.fromhex(sections["cursorPos"]["payloadHex"])),
        ("ping", Msg.PING, P.pack_ping(0x0123456789ABCDEF)),
        ("pong", Msg.PONG, P.pack_ping(0x0123456789ABCDEF)),
        ("sound", Msg.SOUND, P.pack_sound(SOUND_PTS, SOUND_PCM)),
        ("mic", Msg.MIC, MIC_PCM),
    ] + [(f"clipboard-{name}", Msg.CLIPBOARD, P.pack_clipboard(flags, text)) for name, flags, text in CLIPBOARD_CASES]
    out = []
    for name, msg_type, obj in json_frames:
        payload = P.json_bytes(obj)
        out.append({"name": name, "type": int(msg_type), "payloadHex": payload.hex(),
                    "frameHex": P.pack_frame(msg_type, payload).hex()})
    for name, msg_type, payload in binary_frames:
        out.append({"name": name, "type": int(msg_type), "payloadHex": payload.hex(),
                    "frameHex": P.pack_frame(msg_type, payload).hex()})
    out.sort(key=lambda f: (f["type"], f["name"]))
    return out


def deterministic_sections() -> dict:
    sections: dict = {
        "input": [{"name": name, "events": [_event_json(e) for e in events], "hex": P.encode_input(events).hex()}
                  for name, events in INPUT_CASES],
        "auth": auth_section(),
        "videoMessage": video_message_section(),
        "cursorShape": cursor_shape_section(),
        "cursorPos": cursor_pos_section(),
        "clipboard": [{"name": name, "flags": flags, "text": text, "payloadHex": P.pack_clipboard(flags, text).hex()}
                      for name, flags, text in CLIPBOARD_CASES],
    }
    sections["frames"] = frames_section(sections)
    return sections


def capture_video() -> list[dict]:
    """One IDR access unit and the P-frame after it, straight from the capture pipeline."""
    from foldtop_host import capture, nal

    frames: list[tuple[bytes, bool]] = []
    done = threading.Event()

    def on_frame(data: bytes, keyframe: bool, _pts: int) -> None:
        if len(frames) < 2:
            frames.append((data, keyframe))
        if len(frames) >= 2:
            done.set()

    source = (f"videotestsrc is-live=true pattern=smpte horizontal-speed=4 "
              f"! video/x-raw,format=BGRx,width={VIDEO_WIDTH},height={VIDEO_HEIGHT},framerate={VIDEO_FPS}/1")
    session = capture.CaptureSession(
        region=(0, 0, VIDEO_WIDTH, VIDEO_HEIGHT), width=VIDEO_WIDTH, height=VIDEO_HEIGHT, fps=VIDEO_FPS,
        bitrate_kbps=600, on_frame=on_frame, source=source, encoder=capture.detect_encoder("x264"))
    session.start()
    try:
        if not done.wait(10):
            raise RuntimeError("the encoder produced no frames")
    finally:
        session.stop()
    (idr, idr_key), (p, p_key) = frames
    if not idr_key or p_key:
        raise RuntimeError("expected an IDR followed by a P-frame")
    types = [idr[s] & 0x1F for s, _e in nal.iter_nals(idr)]
    if not {5, 7, 8} <= set(types):
        raise RuntimeError(f"IDR access unit is missing parameter sets: {types}")
    return [
        {"name": "idr", "codec": "h264", "keyframe": True, "width": VIDEO_WIDTH, "height": VIDEO_HEIGHT,
         "auHex": idr.hex()},
        {"name": "p", "codec": "h264", "keyframe": False, "width": VIDEO_WIDTH, "height": VIDEO_HEIGHT,
         "auHex": p.hex()},
    ]


SECTION_ORDER = ["frames", "input", "auth", "videoMessage", "cursorShape", "cursorPos", "clipboard", "video"]


def build(existing: dict | None, refresh_video: bool) -> dict:
    sections = deterministic_sections()
    if refresh_video or not (existing and existing.get("video")):
        sections["video"] = capture_video()
    else:
        sections["video"] = existing["video"]
    return {key: sections[key] for key in SECTION_ORDER}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="verify the file instead of writing it")
    ap.add_argument("--video", action="store_true", help="re-capture the encoder output too")
    args = ap.parse_args()
    existing = json.loads(VECTORS_PATH.read_text()) if VECTORS_PATH.exists() else None
    if args.check:
        if existing is None:
            print(f"{VECTORS_PATH} is missing", file=sys.stderr)
            return 1
        fresh = deterministic_sections()
        stale = [k for k in fresh if existing.get(k) != fresh[k]]
        if stale:
            print(f"{VECTORS_PATH} is stale in: {', '.join(stale)}", file=sys.stderr)
            return 1
        print("vectors.json is up to date")
        return 0
    doc = build(existing, args.video)
    VECTORS_PATH.parent.mkdir(parents=True, exist_ok=True)
    VECTORS_PATH.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {VECTORS_PATH}: {len(doc['frames'])} frames, {len(doc['input'])} input cases, "
          f"{len(doc['video'])} access units ({sum(len(v['auHex']) // 2 for v in doc['video'])} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
