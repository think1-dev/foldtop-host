"""Daemon configuration: defaults ← ~/.config/foldtop/config.json ← command line."""

from __future__ import annotations

import json
import os
import socket
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from . import DEFAULT_PORT
from .auth import state_dir


@dataclass
class Config:
    state_dir: Path = field(default_factory=state_dir)
    port: int = DEFAULT_PORT
    bind: Optional[list[str]] = None        # None = every interface (IPv4 and IPv6)
    display: Optional[str] = None           # None = $DISPLAY
    name: Optional[str] = None              # host name shown in the app; None = hostname
    advertise: bool = True                  # publish _foldtop._tcp via Avahi
    encoder: Optional[str] = None           # "x264" forces software encoding
    virtual_output: Optional[str] = None    # RandR output reserved as the phone's own display
    max_fps: int = 120
    audio: bool = True                      # False switches sound and the microphone off entirely
    sound_source: Optional[str] = None      # PulseAudio source to capture; None = the default output's monitor
    mic_name: Optional[str] = None          # device name of the virtual microphone; None = "foldtop_mic"
    mic_default: Optional[bool] = None      # make it the default input while on; None = yes for the standard device
    clipboard: bool = True                  # False keeps the clipboard to this machine
    paste_keys: Optional[str] = None        # what "paste" presses: shift+insert (default), ctrl+v or ctrl+shift+v
    test_source: Optional[str] = None       # tests only: a GStreamer source instead of the screen

    @classmethod
    def load(cls, directory: Optional[Path] = None, **overrides) -> "Config":
        cfg = cls(state_dir=directory or state_dir())
        path = cfg.state_dir / "config.json"
        if path.exists():
            try:
                raw = json.loads(path.read_text())
            except ValueError as exc:
                raise SystemExit(f"{path}: invalid JSON ({exc})") from None
            mapping = {
                "port": "port", "bind": "bind", "display": "display", "name": "name",
                "advertise": "advertise", "encoder": "encoder", "virtualOutput": "virtual_output",
                "virtual_output": "virtual_output", "maxFps": "max_fps",
                "audio": "audio", "soundSource": "sound_source", "micName": "mic_name",
                "micDefault": "mic_default",
                "clipboard": "clipboard", "pasteKeys": "paste_keys",
            }
            for key, attr in mapping.items():
                if key in raw and raw[key] is not None:
                    setattr(cfg, attr, raw[key])
        for key, value in overrides.items():
            if value is not None:
                setattr(cfg, key, value)
        if isinstance(cfg.bind, str):
            cfg.bind = [cfg.bind]
        cfg.port = int(cfg.port)
        return cfg

    @property
    def control_socket(self) -> Path:
        return self.state_dir / "control.sock"

    @property
    def effective_display(self) -> Optional[str]:
        return self.display or os.environ.get("DISPLAY")


_SKIP_INTERFACE_PREFIXES = ("lo", "docker", "br-", "veth", "virbr", "cni", "flannel", "podman", "lxc")


def local_addresses() -> list[str]:
    """IPv4 addresses a phone could reach this machine on: LAN first, then VPN
    (Tailscale/WireGuard) interfaces. Container bridges are left out."""
    lan: list[str] = []
    vpn: list[str] = []
    try:
        out = subprocess.run(["ip", "-j", "-4", "addr", "show", "up"], capture_output=True, text=True, timeout=3)
        for iface in json.loads(out.stdout or "[]"):
            name = iface.get("ifname", "")
            if name.startswith(_SKIP_INTERFACE_PREFIXES):
                continue
            for info in iface.get("addr_info", []):
                addr = info.get("local")
                if not addr or addr.startswith("127."):
                    continue
                (vpn if name.startswith(("tailscale", "wg", "tun", "utun", "zt")) else lan).append(addr)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    if not lan and not vpn:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))  # no packet is sent; just picks the default route
                lan.append(s.getsockname()[0])
        except OSError:
            pass
    seen: set[str] = set()
    return [a for a in lan + vpn if not (a in seen or seen.add(a))]


def pairing_link(*, hosts: list[str], port: int, cert_fp_hex: str, code: str, host_id: str, host_name: str) -> str:
    return (
        "foldtop://pair"
        f"?host={','.join(hosts)}"
        f"&port={port}"
        f"&fp={cert_fp_hex}"
        f"&code={code}"
        f"&id={host_id}"
        f"&name={quote(host_name, safe='')}"
    )
