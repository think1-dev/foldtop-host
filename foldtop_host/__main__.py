"""Command line: python3 -m foldtop_host {run,pair,status,devices,unpair,doctor}."""

from __future__ import annotations

import argparse
import asyncio
import datetime as _dt
import json
import logging
import os
import signal
import socket
import sys
from pathlib import Path
from typing import Any, Optional

from . import DEFAULT_PORT, SERVICE_TYPE, __version__, auth
from .config import Config


# --------------------------------------------------------------------------- control client

def control_request(cfg: Config, request: dict[str, Any], timeout: float = 5.0) -> Optional[dict[str, Any]]:
    """Talk to the running daemon. None if it is not running."""
    path = cfg.control_socket
    if not path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall(json.dumps(request).encode() + b"\n")
            data = b""
            while not data.endswith(b"\n"):
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
        return json.loads(data.decode())
    except (OSError, ValueError):
        return None


def _print_pairing(info: dict[str, Any]) -> None:
    from . import qr

    minutes, seconds = divmod(int(info["expiresIn"]), 60)
    print()
    print(f"  Pairing code:  {info['formatted']}      (single use, valid {minutes}:{seconds:02d})")
    print()
    art = qr.render(info["link"])
    if art:
        print("  Scan this with the Foldtop app, or type the code above:")
        print()
        print(art)
    else:
        print("  In the Foldtop app choose this workstation and type the code above.")
    print()
    print(f"  {info['link']}")
    if info.get("hosts"):
        print(f"  Addresses: {', '.join(info['hosts'])}   port {info['port']}")
    print()


# --------------------------------------------------------------------------- commands

def cmd_run(args: argparse.Namespace) -> int:
    from .server import FoldtopServer

    cfg = _config(args, port=args.port, bind=args.bind, display=args.display, name=args.name,
                  encoder=args.encoder, virtual_output=args.virtual_output,
                  advertise=False if args.no_advertise else None,
                  audio=False if args.no_audio else None, sound_source=args.sound_source,
                  clipboard=False if args.no_clipboard else None)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S",
    )
    try:
        sys.stdout.reconfigure(line_buffering=True)  # pairing code must reach the journal promptly
    except (AttributeError, ValueError):
        pass
    if not cfg.effective_display:
        print("No X display: set DISPLAY (or pass --display :0). On a systemd user service, "
              "start it from graphical-session.target.", file=sys.stderr)
        return 2
    if control_request(cfg, {"cmd": "status"}) is not None:
        print("foldtop-host is already running for this state directory.", file=sys.stderr)
        return 1

    async def main() -> None:
        server = FoldtopServer(cfg)
        await server.start()
        if args.pair:
            server.open_pairing()
            _print_pairing(server.pairing_info())
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        serve = asyncio.create_task(server.serve_forever())
        await stop.wait()
        serve.cancel()
        await server.close()

    try:
        asyncio.run(main())
    except OSError as exc:
        print(f"cannot start: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_pair(args: argparse.Namespace) -> int:
    cfg = _config(args)
    info = control_request(cfg, {"cmd": "pair", "ttl": args.minutes * 60})
    if info is None:
        print("foldtop-host is not running. Start it with:  python3 -m foldtop_host run --pair", file=sys.stderr)
        return 1
    if not info.get("ok"):
        print(f"error: {info.get('error')}", file=sys.stderr)
        return 1
    _print_pairing(info)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _config(args)
    info = control_request(cfg, {"cmd": "status"})
    if info is None:
        print("foldtop-host is not running.")
        return 1
    if args.json:
        print(json.dumps(info, indent=2))
        return 0
    print(f"foldtop-host {info['version']} — {info['hostName']}, port {info['port']}, encoder {info['encoder']}")
    print(f"  fingerprint   {info['fingerprint']}")
    print(f"  discovery     {'advertising ' + repr(info['serviceName']) if info.get('advertised') else 'off'}")
    print(f"  paired        {info['devices']} device(s); pairing window {'OPEN' if info['pairingOpen'] else 'closed'}")
    client = info.get("client")
    if client:
        size = "x".join(str(v) for v in client["size"])
        state = f"streaming {client['monitor']} at {size}" if client["streaming"] else "connected, not streaming"
        audio_parts = [label for key, label in (("sound", "sound"), ("microphone", "microphone")) if client.get(key)]
        if audio_parts:
            state += " + " + " + ".join(audio_parts)
        if client.get("clipboard"):
            state += " + clipboard"
        print(f"  client        {client['name'] or client['deviceId']} from {client['peer']} — {state}")
    else:
        print("  client        none")
    return 0


def _fmt_time(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "never"


def cmd_devices(args: argparse.Namespace) -> int:
    cfg = _config(args)
    reply = control_request(cfg, {"cmd": "devices"})
    if reply is not None and reply.get("ok"):
        devices = reply["devices"]
    else:
        store = auth.DeviceStore(cfg.state_dir)
        devices = [{"deviceId": d.device_id, "name": d.name, "pairedAt": d.paired_at, "lastSeen": d.last_seen}
                   for d in store.all()]
    if args.json:
        print(json.dumps(devices, indent=2))
        return 0
    if not devices:
        print("No paired devices. Run:  python3 -m foldtop_host pair")
        return 0
    for d in devices:
        print(f"{d['deviceId']}  {d['name'] or '(unnamed)':28s} paired {_fmt_time(d['pairedAt'])}  "
              f"last seen {_fmt_time(d['lastSeen'])}")
    return 0


def cmd_unpair(args: argparse.Namespace) -> int:
    cfg = _config(args)
    reply = control_request(cfg, {"cmd": "unpair", "selector": args.device})
    if reply is not None and reply.get("ok"):
        removed = reply["removed"]
    else:
        store = auth.DeviceStore(cfg.state_dir)
        removed = [{"deviceId": d.device_id, "name": d.name} for d in store.remove(args.device)]
    if not removed:
        print(f"No paired device matches {args.device!r}.", file=sys.stderr)
        return 1
    for d in removed:
        print(f"Unpaired {d['name'] or d['deviceId']} ({d['deviceId']})")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = _config(args)
    problems = 0

    def check(ok: Optional[bool], label: str, detail: str = "") -> None:
        nonlocal problems
        mark = {True: " ok ", False: "FAIL", None: "warn"}[ok]
        if ok is False:
            problems += 1
        print(f"[{mark}] {label}{': ' + detail if detail else ''}")

    print(f"foldtop-host {__version__} — state directory {cfg.state_dir}")

    display = cfg.effective_display
    check(bool(display), "X display", display or "DISPLAY is not set")
    session = os.environ.get("XDG_SESSION_TYPE", "")
    if session and session != "x11":
        check(None, "session type", f"{session} — capture/injection need an X11 session (Xwayland apps only otherwise)")
    if display:
        try:
            from . import x11

            with x11.Display(display) as d:
                check(d.has_xtest(), "XTEST (input injection)")
                check(d.has_xfixes() or None, "XFIXES (cursor images)")
                mons = d.monitors()
                check(bool(mons), "monitors", ", ".join(
                    f"{m.name} {m.width}x{m.height}+{m.x}+{m.y}{' primary' if m.primary else ''}" for m in mons))
                if cfg.virtual_output:
                    check(any(m.name == cfg.virtual_output for m in mons) or None,
                          "virtual output", f"{cfg.virtual_output} "
                          f"{'is active' if any(m.name == cfg.virtual_output for m in mons) else 'is not connected'}")
        except Exception as exc:
            check(False, "X connection", str(exc))

    try:
        from . import capture

        for element in capture.required_elements():
            check(capture.element_exists(element), f"GStreamer element {element}")
        enc = capture.detect_encoder(cfg.encoder)
        check(enc.h264 is not None, "H.264 encoder",
              f"{enc.name} ({enc.h264})" if enc.h264 else "install gstreamer1.0-plugins-ugly (x264enc)")
        if enc.name == "x264":
            check(None, "hardware encoding", "NVENC not available — `sudo apt install gstreamer1.0-plugins-bad` "
                                             "adds nvh264enc; software x264 is used meanwhile")
    except Exception as exc:
        check(False, "GStreamer", f"{exc} — install python3-gi and gstreamer1.0-plugins-good/-ugly")

    if not cfg.audio:
        check(None, "audio", "switched off in the configuration")
    else:
        try:
            from . import audio

            caps = audio.AudioManager(state_dir=cfg.state_dir, enabled=True, sound_source=cfg.sound_source,
                                      mic_name=cfg.mic_name).capabilities()
            check(caps.sound or None, "sound to the phone",
                  f"captures {cfg.sound_source or 'the default output'}" if caps.sound
                  else f"unavailable ({caps.sound_problem}); video and input still work")
            check(caps.microphone or None, "phone microphone as an input device",
                  f"appears as \"{audio.MIC_DESCRIPTION}\" while a phone has its microphone on" if caps.microphone
                  else f"unavailable ({caps.microphone_problem}); video and input still work")
        except Exception as exc:
            check(None, "audio", f"unavailable ({exc})")

    if not cfg.clipboard:
        check(None, "shared clipboard", "switched off in the configuration")
    else:
        try:
            from . import clipboard

            probe = clipboard.Clipboard(cfg.effective_display, lambda _text: None)
            probe.start()
            probe.stop()
            check(True, "shared clipboard", "text copied on the phone or here can be pasted on the other")
        except Exception as exc:
            check(None, "shared clipboard", f"unavailable ({exc}); video and input still work")

    from . import discovery

    ok, detail = discovery.avahi_available()
    check(ok or None, "Avahi (mDNS discovery)", detail if ok else f"unavailable ({detail}); pairing by QR/address still works")

    status = control_request(cfg, {"cmd": "status"})
    if status is not None:
        check(True, "daemon", f"running on port {status['port']}, {status['devices']} paired device(s)")
        if ok and status.get("serviceName"):
            found = discovery.resolve(status["serviceName"])
            check(found is not None or None, f"{SERVICE_TYPE} resolvable",
                  f"{found['address']}:{found['port']}" if found else "not found via Avahi")
    else:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind(("0.0.0.0", cfg.port))
            check(True, f"port {cfg.port}", "free (daemon not running)")
        except OSError as exc:
            check(False, f"port {cfg.port}", f"in use by something else ({exc})")

    if cfg.state_dir.exists():
        mode = cfg.state_dir.stat().st_mode & 0o777
        check(True if mode == 0o700 else None, "state directory permissions",
              oct(mode) if mode == 0o700 else f"{oct(mode)} — the daemon tightens this to 0o700 when it starts")
        key = cfg.state_dir / "key.pem"
        if key.exists():
            key_mode = key.stat().st_mode & 0o777
            check(key_mode == 0o600, "TLS private key permissions", oct(key_mode))
    print("\nAll good." if problems == 0 else f"\n{problems} problem(s) found.")
    return 0 if problems == 0 else 1


# --------------------------------------------------------------------------- argparse

def _config(args: argparse.Namespace, **overrides) -> Config:
    directory = Path(args.state_dir) if getattr(args, "state_dir", None) else None
    return Config.load(directory, **overrides)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="foldtop-host", description="Foldtop workstation companion")
    ap.add_argument("--version", action="version", version=f"foldtop-host {__version__}")
    ap.add_argument("--state-dir", help="state directory (default ~/.config/foldtop, or $FOLDTOP_STATE_DIR)")
    sub = ap.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the daemon in the foreground")
    run.add_argument("--port", type=int, help=f"TCP port (default {DEFAULT_PORT})")
    run.add_argument("--bind", action="append", help="address to listen on (repeatable; default: all)")
    run.add_argument("--display", help="X display (default $DISPLAY)")
    run.add_argument("--name", help="name shown in the app (default: hostname)")
    run.add_argument("--encoder", choices=("auto", "x264"), help="force software encoding with x264")
    run.add_argument("--virtual-output", help="RandR output reserved as the phone's own display")
    run.add_argument("--no-advertise", action="store_true", help="do not publish the service over mDNS")
    run.add_argument("--no-audio", action="store_true", help="no sound to the phone and no virtual microphone")
    run.add_argument("--sound-source", metavar="DEVICE",
                     help="PulseAudio source to send as sound (default: the default output's monitor)")
    run.add_argument("--no-clipboard", action="store_true", help="do not share the clipboard with the phone")
    run.add_argument("--pair", action="store_true", help="open a pairing window at start-up")
    run.add_argument("--log-level", default="info", choices=("debug", "info", "warning", "error"))
    run.set_defaults(func=cmd_run)

    pair = sub.add_parser("pair", help="open a pairing window on the running daemon")
    pair.add_argument("--minutes", type=float, default=5.0, help="how long the code stays valid (default 5)")
    pair.set_defaults(func=cmd_pair)

    status = sub.add_parser("status", help="show what the running daemon is doing")
    status.add_argument("--json", action="store_true")
    status.set_defaults(func=cmd_status)

    devices = sub.add_parser("devices", help="list paired devices")
    devices.add_argument("--json", action="store_true")
    devices.set_defaults(func=cmd_devices)

    unpair = sub.add_parser("unpair", help="remove a paired device")
    unpair.add_argument("device", help="device id, id prefix or name")
    unpair.set_defaults(func=cmd_unpair)

    doctor = sub.add_parser("doctor", help="check that this machine can run the companion")
    doctor.set_defaults(func=cmd_doctor)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "encoder", None) == "auto":
        args.encoder = None
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
