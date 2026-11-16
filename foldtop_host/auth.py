"""Host identity, paired devices and the pairing/auth math (docs/PROTOCOL.md §3)."""

from __future__ import annotations

import base64
import datetime as _dt
import hashlib
import hmac
import json
import os
import secrets
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 8
PAIRING_TTL_SECONDS = 300.0
PAIRING_MAX_FAILURES = 5
PBKDF2_ITERATIONS = 200_000
NONCE_LENGTH = 32


# --------------------------------------------------------------------------- crypto

def normalize_code(code: str) -> str:
    return "".join(ch for ch in code.upper() if ch not in "- \t\r\n")


def format_code(code: str) -> str:
    code = normalize_code(code)
    return f"{code[:4]}-{code[4:]}" if len(code) == CODE_LENGTH else code


def generate_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def pair_key(code: str, client_nonce: bytes, server_nonce: bytes) -> bytes:
    """K = PBKDF2-HMAC-SHA256(code, "foldtop/v1/pair" ‖ clientNonce ‖ serverNonce, 200k, 32)."""
    salt = b"foldtop/v1/pair" + client_nonce + server_nonce
    return hashlib.pbkdf2_hmac("sha256", normalize_code(code).encode("utf-8"), salt, PBKDF2_ITERATIONS, 32)


def _hmac(key: bytes, *parts: bytes) -> bytes:
    return hmac.new(key, b"".join(parts), hashlib.sha256).digest()


def client_proof(k: bytes, cert_fp: bytes, device_id: str) -> bytes:
    return _hmac(k, b"client", cert_fp, device_id.encode("utf-8"))


def server_proof(k: bytes, cert_fp: bytes, device_id: str) -> bytes:
    return _hmac(k, b"server", cert_fp, device_id.encode("utf-8"))


def derive_device_key(k: bytes, cert_fp: bytes, device_id: str) -> bytes:
    return _hmac(k, b"device-key", cert_fp, device_id.encode("utf-8"))


def device_mac(device_key: bytes, client_nonce: bytes, server_nonce: bytes, cert_fp: bytes, device_id: str) -> bytes:
    return _hmac(device_key, b"foldtop/v1/auth\x00", client_nonce, server_nonce, cert_fp, device_id.encode("utf-8"))


def constant_time_equal(a: bytes, b: bytes) -> bool:
    return hmac.compare_digest(a, b)


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: object, *, length: Optional[int] = None) -> bytes:
    """Strict base64 decode; raises ValueError on anything malformed."""
    if not isinstance(text, str):
        raise ValueError("expected a base64 string")
    try:
        data = base64.b64decode(text, validate=True)
    except Exception:
        raise ValueError("invalid base64") from None
    if length is not None and len(data) != length:
        raise ValueError(f"expected {length} bytes, got {len(data)}")
    return data


# --------------------------------------------------------------------------- state directory

def state_dir() -> Path:
    override = os.environ.get("FOLDTOP_STATE_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return Path(base) / "foldtop"


def ensure_state_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def _write_private(path: Path, data: bytes) -> None:
    """Atomically write a file that only the owner can read."""
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- identity

@dataclass
class HostIdentity:
    host_id: str
    host_name: str
    cert_path: Path
    key_path: Path
    cert_der: bytes
    cert_fp: bytes  # SHA-256 of cert_der

    @property
    def cert_fp_hex(self) -> str:
        return self.cert_fp.hex()

    @classmethod
    def load_or_create(cls, directory: Path, host_name: Optional[str] = None) -> "HostIdentity":
        ensure_state_dir(directory)
        meta_path = directory / "host.json"
        cert_path = directory / "cert.pem"
        key_path = directory / "key.pem"

        meta: dict = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text())
            except ValueError:
                meta = {}
        changed = False
        if not isinstance(meta.get("hostId"), str):
            meta["hostId"] = str(uuid.uuid4())
            changed = True
        if host_name:
            if meta.get("hostName") != host_name:
                meta["hostName"] = host_name
                changed = True
        elif not isinstance(meta.get("hostName"), str):
            meta["hostName"] = socket.gethostname() or "workstation"
            changed = True
        if changed:
            _write_private(meta_path, json.dumps(meta, indent=2).encode())

        if not (cert_path.exists() and key_path.exists()):
            cert_pem, key_pem = _generate_certificate(meta["hostId"])
            _write_private(key_path, key_pem)
            _write_private(cert_path, cert_pem)
        os.chmod(key_path, 0o600)

        cert_der = _pem_to_der(cert_path.read_bytes())
        return cls(
            host_id=meta["hostId"],
            host_name=meta["hostName"],
            cert_path=cert_path,
            key_path=key_path,
            cert_der=cert_der,
            cert_fp=hashlib.sha256(cert_der).digest(),
        )


def _pem_to_der(pem: bytes) -> bytes:
    import ssl

    return ssl.PEM_cert_to_DER_cert(pem.decode("ascii"))


def _generate_certificate(host_id: str) -> tuple[bytes, bytes]:
    """Self-signed ECDSA P-256 certificate. Trust comes from fingerprint pinning,
    so the subject and validity period are informational."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"foldtop-{host_id[:8]}")])
    now = _dt.datetime.now(_dt.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - _dt.timedelta(days=1))
        .not_valid_after(now + _dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("foldtop.local")]), critical=False)
        .sign(key, hashes.SHA256())
    )
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    return cert_pem, key_pem


# --------------------------------------------------------------------------- paired devices

@dataclass
class Device:
    device_id: str
    name: str
    key: bytes
    paired_at: float
    last_seen: float = 0.0

    def to_json(self) -> dict:
        return {
            "deviceId": self.device_id,
            "name": self.name,
            "key": b64e(self.key),
            "pairedAt": self.paired_at,
            "lastSeen": self.last_seen,
        }

    @classmethod
    def from_json(cls, obj: dict) -> "Device":
        return cls(
            device_id=str(obj["deviceId"]),
            name=str(obj.get("name", "")),
            key=b64d(obj["key"], length=32),
            paired_at=float(obj.get("pairedAt", 0.0)),
            last_seen=float(obj.get("lastSeen", 0.0)),
        )


class DeviceStore:
    """Paired devices, persisted as devices.json (0600) in the state directory."""

    def __init__(self, directory: Path) -> None:
        self._path = ensure_state_dir(directory) / "devices.json"
        self._devices: dict[str, Device] = {}
        self.reload()

    def reload(self) -> None:
        self._devices = {}
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text())
        except ValueError:
            return
        for item in raw.get("devices", []):
            try:
                dev = Device.from_json(item)
            except (KeyError, ValueError, TypeError):
                continue
            self._devices[dev.device_id] = dev

    def _save(self) -> None:
        payload = {"devices": [d.to_json() for d in self._devices.values()]}
        _write_private(self._path, json.dumps(payload, indent=2).encode())

    def get(self, device_id: str) -> Optional[Device]:
        return self._devices.get(device_id)

    def all(self) -> list[Device]:
        return sorted(self._devices.values(), key=lambda d: d.paired_at)

    def add(self, device_id: str, name: str, key: bytes) -> Device:
        dev = Device(device_id=device_id, name=name, key=key, paired_at=time.time(), last_seen=time.time())
        self._devices[device_id] = dev
        self._save()
        return dev

    def touch(self, device_id: str) -> None:
        dev = self._devices.get(device_id)
        if dev is not None:
            dev.last_seen = time.time()
            self._save()

    def remove(self, selector: str) -> list[Device]:
        """Remove by exact device id, id prefix (>= 4 chars) or exact name."""
        hits = [
            d for d in self._devices.values()
            if d.device_id == selector
            or d.name == selector
            or (len(selector) >= 4 and d.device_id.lower().startswith(selector.lower()))
        ]
        for d in hits:
            del self._devices[d.device_id]
        if hits:
            self._save()
        return hits


# --------------------------------------------------------------------------- pairing window

@dataclass
class PairingWindow:
    """A single-use pairing code. Lives only in the daemon's memory."""

    code: str = field(default_factory=generate_code)
    ttl: float = PAIRING_TTL_SECONDS
    max_failures: int = PAIRING_MAX_FAILURES
    opened_at: float = field(default_factory=time.monotonic)
    failures: int = 0
    used: bool = False

    @property
    def remaining(self) -> float:
        return max(0.0, self.ttl - (time.monotonic() - self.opened_at))

    @property
    def is_open(self) -> bool:
        return not self.used and self.failures < self.max_failures and self.remaining > 0.0

    def record_failure(self) -> None:
        self.failures += 1

    def consume(self) -> None:
        self.used = True


def new_nonce() -> bytes:
    return secrets.token_bytes(NONCE_LENGTH)
