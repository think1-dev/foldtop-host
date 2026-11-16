import hashlib
import hmac
import json
import os
import ssl
import stat
import tempfile
import time
import unittest
from pathlib import Path

from . import helpers  # noqa: F401
from foldtop_host import auth


class CryptoTests(unittest.TestCase):
    CN = bytes(range(32))
    SN = bytes(range(32, 64))
    FP = hashlib.sha256(b"certificate").digest()
    DEVICE = "0B4E2C1A-5D7F-4A3B-9C8D-112233445566"

    def test_pair_key_matches_spec(self):
        k = auth.pair_key("ABCD-EFGH", self.CN, self.SN)
        expected = hashlib.pbkdf2_hmac("sha256", b"ABCDEFGH", b"foldtop/v1/pair" + self.CN + self.SN, 200_000, 32)
        self.assertEqual(k, expected)

    def test_code_normalisation(self):
        self.assertEqual(auth.normalize_code(" abcd-efgh\n"), "ABCDEFGH")
        self.assertEqual(auth.pair_key("abcd efgh", self.CN, self.SN), auth.pair_key("ABCDEFGH", self.CN, self.SN))
        self.assertEqual(auth.format_code("abcdefgh"), "ABCD-EFGH")

    def test_proofs_match_spec_and_differ(self):
        k = b"\x11" * 32
        dev = self.DEVICE.encode()
        self.assertEqual(auth.client_proof(k, self.FP, self.DEVICE),
                         hmac.new(k, b"client" + self.FP + dev, hashlib.sha256).digest())
        self.assertEqual(auth.server_proof(k, self.FP, self.DEVICE),
                         hmac.new(k, b"server" + self.FP + dev, hashlib.sha256).digest())
        self.assertEqual(auth.derive_device_key(k, self.FP, self.DEVICE),
                         hmac.new(k, b"device-key" + self.FP + dev, hashlib.sha256).digest())
        values = {auth.client_proof(k, self.FP, self.DEVICE), auth.server_proof(k, self.FP, self.DEVICE),
                  auth.derive_device_key(k, self.FP, self.DEVICE)}
        self.assertEqual(len(values), 3)

    def test_device_mac_matches_spec(self):
        key = b"\x22" * 32
        expected = hmac.new(key, b"foldtop/v1/auth\x00" + self.CN + self.SN + self.FP + self.DEVICE.encode(),
                            hashlib.sha256).digest()
        self.assertEqual(auth.device_mac(key, self.CN, self.SN, self.FP, self.DEVICE), expected)

    def test_proof_is_bound_to_certificate(self):
        k = auth.pair_key("ABCDEFGH", self.CN, self.SN)
        other = hashlib.sha256(b"attacker certificate").digest()
        self.assertNotEqual(auth.client_proof(k, self.FP, self.DEVICE), auth.client_proof(k, other, self.DEVICE))

    def test_generated_codes(self):
        codes = {auth.generate_code() for _ in range(200)}
        self.assertGreater(len(codes), 190)
        for code in codes:
            self.assertEqual(len(code), 8)
            self.assertTrue(set(code) <= set(auth.CODE_ALPHABET))
        self.assertEqual(len(auth.CODE_ALPHABET), 32)
        self.assertFalse(set("IO01") & set(auth.CODE_ALPHABET))

    def test_strict_base64(self):
        self.assertEqual(auth.b64d(auth.b64e(b"\x00" * 32), length=32), b"\x00" * 32)
        for bad in (None, 5, "!!!", "AAAA", "A" * 43):
            with self.assertRaises(ValueError, msg=repr(bad)):
                auth.b64d(bad, length=32)


class IdentityTests(unittest.TestCase):
    def test_create_then_reload_is_stable(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "state"
            first = auth.HostIdentity.load_or_create(directory, "ws-1")
            second = auth.HostIdentity.load_or_create(directory)
            self.assertEqual(first.host_id, second.host_id)
            self.assertEqual(first.cert_fp, second.cert_fp)
            self.assertEqual(second.host_name, "ws-1")
            self.assertEqual(first.cert_fp, hashlib.sha256(first.cert_der).digest())
            self.assertEqual(len(first.cert_fp), 32)

    def test_permissions(self):
        with tempfile.TemporaryDirectory() as td:
            directory = Path(td) / "state"
            ident = auth.HostIdentity.load_or_create(directory)
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(ident.key_path.stat().st_mode), 0o600)

    def test_certificate_is_usable_ecdsa_p256(self):
        from cryptography import x509
        from cryptography.hazmat.primitives.asymmetric import ec

        with tempfile.TemporaryDirectory() as td:
            ident = auth.HostIdentity.load_or_create(Path(td))
            cert = x509.load_der_x509_certificate(ident.cert_der)
            self.assertIsInstance(cert.public_key(), ec.EllipticCurvePublicKey)
            self.assertEqual(cert.public_key().curve.name, "secp256r1")
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(str(ident.cert_path), str(ident.key_path))  # raises if mismatched

    def test_state_dir_env_override(self):
        old = os.environ.get("FOLDTOP_STATE_DIR")
        os.environ["FOLDTOP_STATE_DIR"] = "/tmp/some-foldtop-dir"
        try:
            self.assertEqual(auth.state_dir(), Path("/tmp/some-foldtop-dir"))
        finally:
            if old is None:
                del os.environ["FOLDTOP_STATE_DIR"]
            else:
                os.environ["FOLDTOP_STATE_DIR"] = old


class DeviceStoreTests(unittest.TestCase):
    def test_persist_and_remove(self):
        with tempfile.TemporaryDirectory() as td:
            store = auth.DeviceStore(Path(td))
            store.add("aaaa1111-0000", "Phone A", b"\x01" * 32)
            store.add("bbbb2222-0000", "Phone B", b"\x02" * 32)
            path = Path(td) / "devices.json"
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

            again = auth.DeviceStore(Path(td))
            self.assertEqual([d.name for d in again.all()], ["Phone A", "Phone B"])
            self.assertEqual(again.get("aaaa1111-0000").key, b"\x01" * 32)

            self.assertEqual([d.name for d in again.remove("bbbb")], ["Phone B"])   # id prefix
            self.assertEqual(again.remove("bb"), [])                                # too short to be a prefix
            self.assertEqual([d.name for d in again.remove("Phone A")], ["Phone A"])  # by name
            self.assertEqual(auth.DeviceStore(Path(td)).all(), [])

    def test_corrupt_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "devices.json").write_text("{not json")
            self.assertEqual(auth.DeviceStore(Path(td)).all(), [])
            (Path(td) / "devices.json").write_text(json.dumps({"devices": [{"deviceId": "x", "key": "short"}]}))
            self.assertEqual(auth.DeviceStore(Path(td)).all(), [])


class PairingWindowTests(unittest.TestCase):
    def test_expiry(self):
        window = auth.PairingWindow(ttl=0.05)
        self.assertTrue(window.is_open)
        time.sleep(0.08)
        self.assertFalse(window.is_open)
        self.assertEqual(window.remaining, 0.0)

    def test_closes_after_five_failures(self):
        window = auth.PairingWindow()
        for _ in range(4):
            window.record_failure()
            self.assertTrue(window.is_open)
        window.record_failure()
        self.assertFalse(window.is_open)

    def test_single_use(self):
        window = auth.PairingWindow()
        window.consume()
        self.assertFalse(window.is_open)

    def test_defaults_match_spec(self):
        self.assertEqual(auth.PAIRING_TTL_SECONDS, 300)
        self.assertEqual(auth.PAIRING_MAX_FAILURES, 5)
        self.assertEqual(auth.PBKDF2_ITERATIONS, 200_000)


if __name__ == "__main__":
    unittest.main()
