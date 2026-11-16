"""DNS-SD advertisement of `_foldtop._tcp` through avahi-daemon's D-Bus API.

Uses Gio from the system PyGObject (already needed for GStreamer) — no extra
dependency. Everything here fails soft: without Avahi the daemon still works, the
app just has to be given the address by QR code or by hand.
"""

from __future__ import annotations

import logging
from typing import Optional

from . import SERVICE_TYPE

log = logging.getLogger("foldtop.discovery")

_AVAHI = "org.freedesktop.Avahi"
_IF_SERVER = "org.freedesktop.Avahi.Server"
_IF_GROUP = "org.freedesktop.Avahi.EntryGroup"
_IF_UNSPEC = -1
_PROTO_UNSPEC = -1
_TIMEOUT_MS = 3000

# AvahiEntryGroupState
_GROUP_COLLISION = 3
_GROUP_FAILURE = 4


def _gio():
    import gi

    gi.require_version("Gio", "2.0")
    from gi.repository import Gio, GLib

    return Gio, GLib


def txt_records(host_id: str, host_name: str, cert_fp_hex: str) -> list[bytes]:
    return [
        b"v=1",
        f"id={host_id}".encode(),
        f"name={host_name}".encode(),
        f"fp={cert_fp_hex[:16]}".encode(),
    ]


class Advertiser:
    def __init__(self, *, name: str, port: int, txt: list[bytes]) -> None:
        self.requested_name = name
        self.name = name
        self.port = port
        self.txt = txt
        self._conn = None
        self._group_path: Optional[str] = None

    # ------------------------------------------------------------------ D-Bus plumbing

    def _call(self, path: str, interface: str, method: str, params=None, reply: Optional[str] = None):
        Gio, GLib = _gio()
        return self._conn.call_sync(
            _AVAHI, path, interface, method, params,
            GLib.VariantType.new(reply) if reply else None,
            Gio.DBusCallFlags.NONE, _TIMEOUT_MS, None,
        )

    # ------------------------------------------------------------------ public API

    def start(self) -> bool:
        """Publish the service. Returns False (and logs why) if Avahi is unavailable."""
        try:
            Gio, GLib = _gio()
            self._conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            self._publish()
            return True
        except Exception as exc:
            log.warning("mDNS advertisement unavailable (%s); pair by QR code or enter the address manually",
                        _short(exc))
            self._group_path = None
            return False

    def _publish(self) -> None:
        _Gio, GLib = _gio()
        (group_path,) = self._call("/", _IF_SERVER, "EntryGroupNew", None, "(o)").unpack()
        self._group_path = group_path
        name = self.requested_name
        for _attempt in range(8):
            params = GLib.Variant(
                "(iiussssqaay)",
                (_IF_UNSPEC, _PROTO_UNSPEC, 0, name, SERVICE_TYPE, "", "", self.port, self.txt),
            )
            try:
                self._call(group_path, _IF_GROUP, "AddService", params)
                self._call(group_path, _IF_GROUP, "Commit")
                self.name = name
                log.info("advertising %s as %r on port %d", SERVICE_TYPE, name, self.port)
                return
            except Exception as exc:
                if "Collision" not in str(exc):
                    raise
                (name,) = self._call("/", _IF_SERVER, "GetAlternativeServiceName",
                                     GLib.Variant("(s)", (name,)), "(s)").unpack()
                self._call(group_path, _IF_GROUP, "Reset")
        raise RuntimeError("could not find a free service name")

    def healthy(self) -> bool:
        if self._conn is None or self._group_path is None:
            return False
        try:
            (state,) = self._call(self._group_path, _IF_GROUP, "GetState", None, "(i)").unpack()
            return state not in (_GROUP_COLLISION, _GROUP_FAILURE)
        except Exception:
            return False

    def ensure(self) -> bool:
        """Re-publish if avahi-daemon restarted or the registration was lost."""
        if self.healthy():
            return True
        self._free_group()
        return self.start()

    def stop(self) -> None:
        self._free_group()
        self._conn = None

    def _free_group(self) -> None:
        if self._conn is not None and self._group_path is not None:
            try:
                self._call(self._group_path, _IF_GROUP, "Free")
            except Exception:
                pass
        self._group_path = None

    # ------------------------------------------------------------------ diagnostics

    def resolve_self(self) -> Optional[dict]:
        """Ask Avahi to resolve our own service (used by tests and `doctor`)."""
        return resolve(self.name)


def resolve(name: str, timeout_ms: int = _TIMEOUT_MS) -> Optional[dict]:
    """Resolve a `_foldtop._tcp` instance via Avahi; None if it cannot be found."""
    try:
        Gio, GLib = _gio()
        conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        reply = conn.call_sync(
            _AVAHI, "/", _IF_SERVER, "ResolveService",
            GLib.Variant("(iisssiu)", (_IF_UNSPEC, _PROTO_UNSPEC, name, SERVICE_TYPE, "local", _PROTO_UNSPEC, 0)),
            GLib.VariantType.new("(iissssisqaayu)"),
            Gio.DBusCallFlags.NONE, timeout_ms, None,
        ).unpack()
    except Exception as exc:
        log.debug("resolve failed: %s", _short(exc))
        return None
    txt = {}
    for record in reply[9]:
        raw = bytes(record)
        key, _, value = raw.partition(b"=")
        txt[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
    return {"name": reply[2], "type": reply[3], "host": reply[5], "address": reply[7], "port": reply[8], "txt": txt}


def avahi_available() -> tuple[bool, str]:
    try:
        Gio, GLib = _gio()
        conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        (version,) = conn.call_sync(_AVAHI, "/", _IF_SERVER, "GetVersionString", None,
                                    GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NONE, _TIMEOUT_MS, None).unpack()
        return True, version
    except Exception as exc:
        return False, _short(exc)


def _short(exc: Exception) -> str:
    text = str(exc).strip().splitlines()[0] if str(exc).strip() else exc.__class__.__name__
    return text[:160]
