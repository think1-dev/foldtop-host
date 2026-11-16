"""End-to-end: a real FoldtopServer on 127.0.0.1 driven by the reference client over TLS.

Stream tests use a synthetic GStreamer source unless they are about the screen
itself; `LiveScreenTests` grabs the real monitor (read-only) and reports cadence.
"""

import asyncio
import json
import os
import statistics
import time
import unittest

from . import helpers
from foldtop_host import auth, nal
from foldtop_host import protocol as P
from foldtop_host.protocol import Msg
from foldtop_host.refclient import AuthFailed, Credentials, PinMismatch, RefClient, ServerProofMismatch

NEEDS_STREAM = unittest.skipUnless(helpers.HAVE_DISPLAY and helpers.HAVE_GST, "needs an X display and GStreamer")


def _types(data: bytes) -> list[int]:
    return [data[s] & 0x1F for s, _e in nal.iter_nals(data)]


async def _eventually(predicate, timeout=3.0, interval=0.01):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


class HandshakeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.h = helpers.ServerHarness()
        await self.h.__aenter__()
        self.addAsyncCleanup(self.h.__aexit__)
        self.clients = []
        self.addAsyncCleanup(self._close_clients)

    async def _close_clients(self):
        for c in self.clients:
            await c.close()

    def client(self, **kw) -> RefClient:
        c = RefClient("127.0.0.1", self.h.port, **kw)
        self.clients.append(c)
        return c

    # -- pairing ------------------------------------------------------------

    async def test_pair_with_good_code(self):
        code = self.h.open_pairing()
        c = self.client(device_name="Jordan's iPhone Duo")
        challenge = await c.connect(self.h.fp)
        self.assertEqual(challenge["v"], 1)
        self.assertFalse(challenge["known"])
        self.assertTrue(challenge["pairing"])
        self.assertEqual(challenge["hostId"], self.h.server.identity.host_id)

        creds = await c.pair(f"{code[:4]}-{code[4:]}".lower())     # dashes / case are normalised
        self.assertIn("serverProof", c.auth_ok)                    # and it verified (pair() checks it)
        self.assertEqual(c.auth_ok["codecs"], self.h.server.encoder.codecs)
        self.assertIsInstance(c.auth_ok["monitors"], list)
        self.assertIsInstance(c.auth_ok["addresses"], list)        # where else this host can be reached
        self.assertTrue(all(isinstance(a, str) and a for a in c.auth_ok["addresses"]))

        stored = self.h.server.devices.get(c.device_id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.name, "Jordan's iPhone Duo")
        self.assertEqual(stored.key, creds.device_key)             # both sides derived the same key
        on_disk = json.loads((self.h.dir / "devices.json").read_text())
        self.assertEqual(on_disk["devices"][0]["deviceId"], c.device_id)
        self.assertNotIn(code, (self.h.dir / "devices.json").read_text())
        self.assertFalse(self.h.server.pairing_is_open())          # the code is single-use

    async def test_code_cannot_be_reused(self):
        code = self.h.open_pairing()
        first = self.client()
        await first.connect(self.h.fp)
        await first.pair(code)
        second = self.client()
        challenge = await second.connect(self.h.fp)
        self.assertFalse(challenge["pairing"])
        with self.assertRaises(AuthFailed) as cm:
            await second.pair(code)
        self.assertEqual(cm.exception.reason, "pairing-closed")

    async def test_bad_code_is_rejected_and_window_closes_after_five(self):
        code = self.h.open_pairing()
        wrong = "ZZZZZZZZ" if code != "ZZZZZZZZ" else "YYYYYYYY"
        for attempt in range(5):
            c = self.client()
            await c.connect(self.h.fp)
            with self.assertRaises(AuthFailed) as cm:
                await c.pair(wrong)
            self.assertEqual(cm.exception.reason, "bad-proof", attempt)
        self.assertFalse(self.h.server.pairing_is_open())
        c = self.client()
        await c.connect(self.h.fp)
        with self.assertRaises(AuthFailed) as cm:
            await c.pair(code)                                      # even the right code is now useless
        self.assertEqual(cm.exception.reason, "pairing-closed")
        self.assertEqual(self.h.server.devices.all(), [])

    async def test_pairing_requires_an_open_window(self):
        c = self.client()
        challenge = await c.connect(self.h.fp)
        self.assertFalse(challenge["pairing"])
        with self.assertRaises(AuthFailed) as cm:
            await c.pair("ABCDEFGH")
        self.assertEqual(cm.exception.reason, "pairing-closed")

    async def test_expired_window(self):
        window = self.h.server.open_pairing(ttl=0.05)
        await asyncio.sleep(0.1)
        c = self.client()
        await c.connect(self.h.fp)
        with self.assertRaises(AuthFailed) as cm:
            await c.pair(window.code)
        self.assertEqual(cm.exception.reason, "pairing-closed")

    # -- man in the middle ---------------------------------------------------

    async def test_pinned_client_refuses_a_different_certificate(self):
        other = bytes(32)
        c = self.client()
        with self.assertRaises(PinMismatch):
            await c.connect(other)

    async def test_proof_bound_to_another_certificate_is_rejected(self):
        """A proof computed over a fingerprint other than the server's own — what a
        relaying attacker would forward — must not pair."""
        code = self.h.open_pairing()
        c = self.client()
        await c.connect(self.h.fp)
        with self.assertRaises(AuthFailed) as cm:
            await c.pair(code, observed_fp=bytes(range(32)))
        self.assertEqual(cm.exception.reason, "bad-proof")
        self.assertEqual(self.h.server.devices.all(), [])

    async def test_real_tls_mitm_cannot_pair_even_with_the_right_code(self):
        code = self.h.open_pairing()
        async with helpers.MitmProxy(self.h.port) as proxy:
            self.assertNotEqual(proxy.identity.cert_fp, self.h.fp)
            # QR pairing: the phone knows the real fingerprint and refuses the proxy outright.
            pinned = RefClient("127.0.0.1", proxy.port)
            self.clients.append(pinned)
            with self.assertRaises(PinMismatch):
                await pinned.connect(self.h.fp)
            # Manual pairing: no pin yet, the user typed the correct code — still no pairing,
            # because the proof covers the certificate the phone actually saw.
            victim = RefClient("127.0.0.1", proxy.port)
            self.clients.append(victim)
            challenge = await victim.connect(None)
            self.assertTrue(challenge["pairing"])
            self.assertEqual(victim.cert_fp, proxy.identity.cert_fp)
            with self.assertRaises(AuthFailed) as cm:
                await victim.pair(code)
            self.assertEqual(cm.exception.reason, "bad-proof")
            self.assertGreater(proxy.bytes_relayed, 0)
        self.assertEqual(self.h.server.devices.all(), [])
        self.assertEqual(self.h.server.pairing.failures, 1)

    async def test_client_detects_a_server_that_does_not_know_the_code(self):
        """An impostor that accepts any proof cannot produce serverProof."""
        ident = self.h.server.identity

        async def impostor(reader, writer):
            await P.read_frame(reader)
            writer.write(P.pack_json(Msg.CHALLENGE, {"v": 1, "hostId": "x", "hostName": "evil",
                                                    "serverNonce": auth.b64e(auth.new_nonce()),
                                                    "known": False, "pairing": True}))
            await P.read_frame(reader)
            writer.write(P.pack_json(Msg.AUTH_OK, {"hostId": "x", "hostName": "evil",
                                                  "serverProof": auth.b64e(bytes(32)),
                                                  "codecs": ["h264"], "monitors": []}))
            await writer.drain()

        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(ident.cert_path), str(ident.key_path))
        fake = await asyncio.start_server(impostor, "127.0.0.1", 0, ssl=ctx)
        self.addCleanup(fake.close)
        c = RefClient("127.0.0.1", fake.sockets[0].getsockname()[1])
        self.clients.append(c)
        await c.connect(None)
        with self.assertRaises(ServerProofMismatch):
            await c.pair("ABCDEFGH")

    # -- device auth ---------------------------------------------------------

    async def test_reconnect_in_device_mode(self):
        client, creds = await self.h.paired_client()
        self.clients.append(client)
        await client.close()

        again = self.client(device_id=creds.device_id)
        challenge = await again.connect(creds.cert_fp)
        self.assertTrue(challenge["known"])
        ok = await again.authenticate(creds.device_key)
        self.assertNotIn("serverProof", ok)
        self.assertEqual(ok["hostId"], self.h.server.identity.host_id)
        self.assertTrue(await _eventually(lambda: self.h.server.active is not None
                                          and self.h.server.active.device_id == creds.device_id))

    async def test_device_auth_survives_a_daemon_restart(self):
        client, creds = await self.h.paired_client()
        await client.close()
        from foldtop_host.server import FoldtopServer

        await self.h.server.close()
        self.h.server = FoldtopServer(self.h.config)           # same state directory
        await self.h.server.start()
        self.assertEqual(self.h.server.identity.cert_fp, creds.cert_fp)
        again = self.client(device_id=creds.device_id)
        await again.connect(creds.cert_fp)
        await again.authenticate(creds.device_key)

    async def test_wrong_device_key(self):
        client, creds = await self.h.paired_client()
        await client.close()
        c = self.client(device_id=creds.device_id)
        await c.connect(creds.cert_fp)
        with self.assertRaises(AuthFailed) as cm:
            await c.authenticate(bytes(32))
        self.assertEqual(cm.exception.reason, "bad-proof")

    async def test_unknown_device(self):
        c = self.client()
        challenge = await c.connect(self.h.fp)
        self.assertFalse(challenge["known"])
        with self.assertRaises(AuthFailed) as cm:
            await c.authenticate(bytes(32))
        self.assertEqual(cm.exception.reason, "not-paired")

    async def test_unpaired_device_is_refused_and_disconnected(self):
        client, creds = await self.h.paired_client()
        self.clients.append(client)
        reply = await self.h.server._control_command({"cmd": "unpair", "selector": creds.device_id})
        self.assertEqual(len(reply["removed"]), 1)
        self.assertTrue(await _eventually(client.eof.is_set))
        c = self.client(device_id=creds.device_id)
        await c.connect(creds.cert_fp)
        with self.assertRaises(AuthFailed) as cm:
            await c.authenticate(creds.device_key)
        self.assertEqual(cm.exception.reason, "not-paired")

    async def test_replayed_mac_is_useless(self):
        """The MAC covers both nonces, so one captured on an earlier connection fails."""
        client, creds = await self.h.paired_client()
        await client.close()
        first = self.client(device_id=creds.device_id)
        await first.connect(creds.cert_fp)
        captured = auth.device_mac(creds.device_key, first.client_nonce, first.server_nonce,
                                   first.cert_fp, creds.device_id)
        second = self.client(device_id=creds.device_id)
        await second.connect(creds.cert_fp)
        second.send_raw(Msg.AUTH, P.json_bytes({"mode": "device", "mac": auth.b64e(captured)}))
        with self.assertRaises(AuthFailed) as cm:
            await second._read_auth_result()
        self.assertEqual(cm.exception.reason, "bad-proof")

    # -- malformed handshakes -------------------------------------------------

    async def _raw(self):
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection("127.0.0.1", self.h.port, ssl=ctx)
        self.addCleanup(writer.close)
        return reader, writer

    async def _expect_fail(self, frames: list[bytes], reason: str):
        reader, writer = await self._raw()
        for frame in frames:
            writer.write(frame)
        msg_type, payload = None, b""
        while msg_type != Msg.AUTH_FAIL:
            msg_type, payload = await asyncio.wait_for(P.read_frame(reader), 5.0)
        self.assertEqual(P.parse_json(payload)["reason"], reason)
        self.assertEqual(await asyncio.wait_for(reader.read(), 5.0), b"")   # then the server closes

    def _hello(self, **overrides) -> bytes:
        hello = {"v": 1, "deviceId": "dev-1", "deviceName": "x", "clientNonce": auth.b64e(bytes(32))}
        hello.update(overrides)
        return P.pack_json(Msg.HELLO, hello)

    async def test_wrong_protocol_version(self):
        await self._expect_fail([self._hello(v=2)], "version")

    async def test_malformed_hello(self):
        await self._expect_fail([self._hello(clientNonce="short")], "protocol")
        await self._expect_fail([self._hello(deviceId="")], "protocol")
        await self._expect_fail([P.pack_frame(Msg.HELLO, b"not json")], "protocol")
        await self._expect_fail([P.pack_frame(Msg.START, b"{}")], "protocol")          # anything before HELLO

    async def test_unknown_auth_mode_and_bad_fields(self):
        await self._expect_fail([self._hello(), P.pack_json(Msg.AUTH, {"mode": "magic"})], "protocol")
        self.h.open_pairing()
        await self._expect_fail([self._hello(), P.pack_json(Msg.AUTH, {"mode": "pair", "proof": "!!"})], "protocol")

    async def test_session_messages_before_auth_do_nothing(self):
        reader, writer = await self._raw()
        writer.write(self._hello())
        msg_type, _ = await asyncio.wait_for(P.read_frame(reader), 5.0)
        self.assertEqual(msg_type, Msg.CHALLENGE)
        writer.write(P.pack_frame(Msg.INPUT, P.encode_input([P.Key(0xE1, True)])))
        msg_type, payload = await asyncio.wait_for(P.read_frame(reader), 5.0)
        self.assertEqual(msg_type, Msg.AUTH_FAIL)
        self.assertIsNone(self.h.server._injector)       # the injector was never even created

    # -- session basics --------------------------------------------------------

    async def test_ping_pong_and_unknown_types(self):
        client, _ = await self.h.paired_client()
        self.clients.append(client)
        client.send_raw(0x66, b"from the future")         # unknown type: ignored
        value = client.ping(0x1122334455667788)
        self.assertTrue(await _eventually(lambda: client.pongs == [value]))
        self.assertIsNone(client.bye)

    async def test_bad_input_is_a_protocol_error(self):
        client, _ = await self.h.paired_client()
        self.clients.append(client)
        client.send_raw(Msg.INPUT, b"\xee")
        self.assertTrue(await _eventually(client.eof.is_set))
        self.assertEqual(client.bye, {"reason": "protocol"})

    async def test_oversized_frame_closes_the_connection(self):
        client, _ = await self.h.paired_client()
        self.clients.append(client)
        client.writer.write(b"\xff\xff\xff\xff")
        self.assertTrue(await _eventually(client.eof.is_set))

    async def test_new_client_replaces_the_previous_one(self):
        first, creds_a = await self.h.paired_client("phone-a")
        self.clients.append(first)
        self.assertEqual(self.h.server.active.device_name, "phone-a")
        second, _ = await self.h.paired_client("phone-b")
        self.clients.append(second)
        self.assertTrue(await _eventually(first.eof.is_set))
        self.assertEqual(first.bye, {"reason": "replaced"})
        self.assertEqual(self.h.server.active.device_name, "phone-b")
        value = second.ping()
        self.assertTrue(await _eventually(lambda: second.pongs == [value]))
        self.assertIsNone(second.bye)
        self.assertEqual(len(self.h.server.devices.all()), 2)     # both stay paired

    async def test_idle_client_is_dropped(self):
        from foldtop_host import server as S

        saved = (S.IDLE_TIMEOUT, S.PING_INTERVAL, S.STATS_INTERVAL)
        S.IDLE_TIMEOUT, S.PING_INTERVAL, S.STATS_INTERVAL = 0.6, 0.2, 0.1
        try:
            # a client that answers PINGs stays connected …
            alive, _ = await self.h.paired_client("alive")
            self.clients.append(alive)
            await asyncio.sleep(1.2)
            self.assertFalse(alive.eof.is_set())
            self.assertGreaterEqual(alive.pings_seen, 3)
            # … one that goes silent is dropped.
            code = self.h.open_pairing()
            silent = self.client(device_name="silent")
            await silent.connect(self.h.fp)
            k = auth.pair_key(code, silent.client_nonce, silent.server_nonce)
            silent.send_raw(Msg.AUTH, P.json_bytes(
                {"mode": "pair", "proof": auth.b64e(auth.client_proof(k, silent.cert_fp, silent.device_id))}))
            msg_type, _ = await P.read_frame(silent.reader)
            self.assertEqual(msg_type, Msg.AUTH_OK)
            started = time.monotonic()
            data = await asyncio.wait_for(silent.reader.read(-1), 5.0)   # never replies to PING
            self.assertLess(time.monotonic() - started, 3.0)
            self.assertIn(b"\x30", data[:8])                              # it was being pinged
        finally:
            S.IDLE_TIMEOUT, S.PING_INTERVAL, S.STATS_INTERVAL = saved

    # -- control socket ----------------------------------------------------------

    async def test_control_socket_commands(self):
        from foldtop_host.__main__ import control_request

        loop = asyncio.get_running_loop()

        async def call(req):
            return await loop.run_in_executor(None, control_request, self.h.config, req)

        status = await call({"cmd": "status"})
        self.assertTrue(status["ok"])
        self.assertEqual(status["port"], self.h.port)
        self.assertEqual(status["fingerprint"], self.h.fp.hex())
        self.assertFalse(status["pairingOpen"])
        self.assertIsNone(status["client"])
        self.assertEqual(os.stat(self.h.config.control_socket).st_mode & 0o777, 0o600)

        info = await call({"cmd": "pair"})
        self.assertTrue(info["ok"])
        self.assertEqual(len(info["code"]), 8)
        self.assertEqual(info["formatted"], f"{info['code'][:4]}-{info['code'][4:]}")
        self.assertTrue(info["link"].startswith("foldtop://pair?host="))
        self.assertIn(f"&port={self.h.port}&fp={self.h.fp.hex()}&code={info['code']}&id=", info["link"])
        self.assertTrue(290 <= info["expiresIn"] <= 300)

        c = self.client(device_name="via-control")
        await c.connect(self.h.fp)
        await c.pair(info["code"])
        devices = await call({"cmd": "devices"})
        self.assertEqual([d["name"] for d in devices["devices"]], ["via-control"])
        self.assertNotIn("key", devices["devices"][0])            # secrets never leave the store
        status = await call({"cmd": "status"})
        self.assertEqual(status["client"]["name"], "via-control")

        removed = await call({"cmd": "unpair", "selector": "via-control"})
        self.assertEqual(len(removed["removed"]), 1)
        self.assertEqual((await call({"cmd": "devices"}))["devices"], [])
        self.assertFalse((await call({"cmd": "bogus"}))["ok"])


@NEEDS_STREAM
class StreamTests(unittest.IsolatedAsyncioTestCase):
    """Streaming behaviour with a synthetic 640x360 source (deterministic, cheap)."""

    SOURCE = helpers.SMPTE_SOURCE

    async def asyncSetUp(self):
        self.h = helpers.ServerHarness(test_source=self.SOURCE)
        await self.h.__aenter__()
        self.addAsyncCleanup(self.h.__aexit__)
        self.client, self.creds = await self.h.paired_client()
        self.addAsyncCleanup(self.client.close)

    async def test_first_frame_is_a_complete_keyframe_and_everything_decodes(self):
        c = self.client
        c.start(max_width=640, max_height=360, fps=30, bitrate_kbps=3000, cursor="video")
        cfg = await c.wait_config()
        mon = c.auth_ok["monitors"][0]
        expected = P.fit_size(mon["width"], mon["height"], 640, 360)
        self.assertEqual((cfg["width"], cfg["height"]), expected)
        self.assertEqual((cfg["epoch"], cfg["codec"], cfg["fps"], cfg["cursor"]), (0, "h264", 30, "video"))
        self.assertEqual(cfg["monitor"], {k: mon[k] for k in ("name", "x", "y", "width", "height")})

        frames = await c.collect(45)
        self.assertEqual(c.stale_frames, 0)                       # STREAM_CONFIG came before any VIDEO
        first = frames[0]
        self.assertTrue(first.keyframe)
        types = _types(first.data)
        self.assertIn(7, types)                                   # SPS
        self.assertIn(8, types)                                   # PPS
        self.assertIn(5, types)                                   # IDR slice
        self.assertLess(types.index(7), types.index(5))
        self.assertLess(types.index(8), types.index(5))
        self.assertTrue(first.data.startswith(b"\x00\x00\x00\x01") or first.data.startswith(b"\x00\x00\x01"))
        for f in frames[1:]:
            self.assertFalse(f.keyframe)                          # no periodic or scene-cut IDRs
            self.assertNotIn(5, _types(f.data))
        pts = [f.pts_micros for f in frames]
        self.assertEqual(pts, sorted(pts))
        self.assertEqual(len(set(pts)), len(pts))
        self.assertLess(abs(time.monotonic_ns() // 1000 - pts[-1]), 5_000_000)   # monotonic capture clock

        decoded = await helpers.adecode_h264([f.data for f in frames])
        self.assertEqual(len(decoded), len(frames))               # no B-frames: one picture per access unit
        self.assertEqual(set(decoded), {expected})

    async def test_keyframe_on_request(self):
        c = self.client
        c.start(max_width=320, max_height=180, fps=30, bitrate_kbps=2000, cursor="video")
        await c.wait_config()
        await c.collect(10)
        c.request_keyframe()
        got = None
        for _ in range(30):
            f = await c.next_frame()
            if f.keyframe:
                got = f
                break
        self.assertIsNotNone(got, "no IDR within 30 frames of KEYFRAME")
        types = _types(got.data)
        self.assertTrue({5, 7, 8} <= set(types))
        # a decoder that joins here can decode from this frame on
        tail = [got.data] + [f.data for f in await c.collect(10)]
        self.assertEqual(len(await helpers.adecode_h264(tail)), len(tail))

    async def test_reconfigure_bumps_epoch_and_restarts_on_a_keyframe(self):
        c = self.client
        c.start(max_width=320, max_height=180, fps=30, bitrate_kbps=2000, cursor="video")
        cfg0 = await c.wait_config()
        await c.collect(5)
        c.start(max_width=480, max_height=480, fps=30, bitrate_kbps=2000, cursor="video")   # posture change
        cfg1 = await c.wait_config(epoch_after=cfg0["epoch"])
        self.assertEqual(cfg1["epoch"], cfg0["epoch"] + 1)
        mon = c.auth_ok["monitors"][0]
        expected = P.fit_size(mon["width"], mon["height"], 480, 480)
        self.assertEqual((cfg1["width"], cfg1["height"]), expected)
        frames = await c.collect(12)
        self.assertTrue(all(f.epoch == cfg1["epoch"] for f in frames))
        self.assertTrue(frames[0].keyframe)
        self.assertTrue({5, 7, 8} <= set(_types(frames[0].data)))
        self.assertEqual(set(await helpers.adecode_h264([f.data for f in frames])), {expected})
        self.assertEqual([cfg["epoch"] for cfg in c.configs], [0, 1])

    async def test_stop_then_start_again(self):
        c = self.client
        c.start(max_width=320, max_height=180, fps=30, bitrate_kbps=2000, cursor="video")
        await c.wait_config()
        await c.collect(5)
        c.stop()
        await asyncio.sleep(0.5)
        while not c.frames.empty():
            c.frames.get_nowait()
        await asyncio.sleep(0.4)
        self.assertTrue(c.frames.empty(), "frames kept coming after STOP")
        value = c.ping()                                           # the connection is still usable
        self.assertTrue(await _eventually(lambda: value in c.pongs))
        self.assertFalse(self.h.server.active.describe()["streaming"])
        c.start(max_width=320, max_height=180, fps=30, bitrate_kbps=2000, cursor="video")
        cfg = await c.wait_config(epoch_after=0)
        self.assertEqual(cfg["epoch"], 1)
        self.assertTrue((await c.next_frame()).keyframe)

    async def test_stats_while_streaming(self):
        c = self.client
        c.start(max_width=320, max_height=180, fps=30, bitrate_kbps=2000, cursor="video")
        await c.wait_config()
        self.assertTrue(await _eventually(lambda: len(c.stats) >= 2, timeout=5.0))
        stat = c.stats[-1]
        self.assertEqual(set(stat), {"fps", "kbps", "encoder", "captureWidth", "captureHeight"})
        self.assertEqual(stat["encoder"], self.h.server.encoder.name)
        self.assertTrue(24 <= stat["fps"] <= 36, stat)
        mon = c.auth_ok["monitors"][0]
        self.assertEqual((stat["captureWidth"], stat["captureHeight"]), (mon["width"], mon["height"]))

    async def test_stream_stops_when_client_is_replaced(self):
        c = self.client
        c.start(max_width=320, max_height=180, fps=30, bitrate_kbps=2000, cursor="video")
        await c.wait_config()
        await c.collect(5)
        other, _ = await self.h.paired_client("second")
        self.addAsyncCleanup(other.close)
        self.assertTrue(await _eventually(c.eof.is_set))
        self.assertEqual(c.bye, {"reason": "replaced"})
        other.start(max_width=320, max_height=180, fps=30, bitrate_kbps=2000, cursor="video")
        cfg = await other.wait_config()
        self.assertEqual(cfg["epoch"], 0)                          # epochs are per connection
        frames = await other.collect(5)
        self.assertTrue(frames[0].keyframe)

    async def test_abrupt_disconnect_stops_the_pipeline(self):
        c = self.client
        c.start(max_width=320, max_height=180, fps=30, bitrate_kbps=2000, cursor="video")
        await c.wait_config()
        await c.collect(3)
        session = self.h.server.active
        c.writer.transport.abort()                                 # the phone vanishes mid-stream
        self.assertTrue(await _eventually(lambda: self.h.server.active is None, timeout=5.0))
        self.assertIsNone(session.stream)


@NEEDS_STREAM
class BitrateTests(unittest.IsolatedAsyncioTestCase):
    """Noise is incompressible, so the output rate tracks the target closely."""

    async def test_tune_changes_bitrate_without_restart(self):
        async with helpers.ServerHarness(test_source=helpers.SNOW_SOURCE) as h:
            c, _ = await h.paired_client()
            try:
                c.start(max_width=640, max_height=360, fps=30, bitrate_kbps=6000, cursor="video")
                cfg = await c.wait_config()

                async def measure(seconds: float) -> float:
                    frames = await c.collect(int(seconds * 30))
                    span = frames[-1].received_at - frames[0].received_at
                    return sum(len(f.data) for f in frames[1:]) * 8 / 1000 / span, frames

                await c.collect(30)                                 # let rate control settle
                high, _ = await measure(2.0)
                c.tune(1500)
                await c.collect(45)
                low, frames = await measure(2.0)
                self.assertTrue(4000 <= high <= 8000, f"at 6000 kbps target measured {high:.0f}")
                self.assertTrue(900 <= low <= 2300, f"at 1500 kbps target measured {low:.0f}")
                self.assertEqual(c.stream_config["epoch"], cfg["epoch"])   # same stream, no restart
                self.assertFalse(any(f.keyframe for f in frames))
                print(f"\n    [tune] 6000 kbps target → {high:.0f} kbps, 1500 kbps target → {low:.0f} kbps",
                      end="", flush=True)
            finally:
                await c.close()


@NEEDS_STREAM
class LiveScreenTests(unittest.IsolatedAsyncioTestCase):
    """The real thing: ximagesrc on the live monitor. Reads the screen; injects only
    pointer motion (restored) and a Left Shift tap."""

    async def asyncSetUp(self):
        from foldtop_host import x11

        self.h = helpers.ServerHarness()
        await self.h.__aenter__()
        self.addAsyncCleanup(self.h.__aexit__)
        self.client, self.creds = await self.h.paired_client()
        self.addAsyncCleanup(self.client.close)
        self.x = x11.Display()
        self.addCleanup(self.x.close)
        self.pointer_start = self.x.query_pointer()[:2]
        self.addCleanup(self._restore_pointer)

    def _restore_pointer(self):
        self.x.fake_motion(*self.pointer_start)
        self.x.sync()

    async def _cadence(self, max_w: int, max_h: int, fps: int, kbps: int, seconds: float) -> dict:
        c = self.client
        epoch_before = c.stream_config["epoch"] if c.stream_config else -1
        c.start(max_width=max_w, max_height=max_h, fps=fps, bitrate_kbps=kbps, cursor="local")
        cfg = await c.wait_config(epoch_after=epoch_before)
        frames = await c.collect(int(seconds * fps), timeout=seconds * 3 + 10)
        steady = frames[fps // 2:]                                  # skip half a second of start-up
        gaps = [(b.received_at - a.received_at) * 1000 for a, b in zip(steady, steady[1:])]
        latency = [(f.received_at * 1e6 - f.pts_micros) / 1000 for f in steady]
        span = steady[-1].received_at - steady[0].received_at
        return {
            "cfg": cfg, "frames": frames,
            "fps": (len(steady) - 1) / span,
            "gap_p50": statistics.median(gaps), "gap_p99": sorted(gaps)[int(len(gaps) * 0.99)],
            "gap_max": max(gaps),
            "lat_p50": statistics.median(latency), "lat_p95": sorted(latency)[int(len(latency) * 0.95)],
            "kbps": sum(len(f.data) for f in steady) * 8 / 1000 / span,
        }

    async def test_live_monitor_stream_cadence_and_decode(self):
        mon = self.client.auth_ok["monitors"][0]
        for max_w, max_h, kbps in ((1840, 1300, 12000), (2700, 1900, 20000)):
            r = await self._cadence(max_w, max_h, 60, kbps, seconds=4.0)
            expected = P.fit_size(mon["width"], mon["height"], max_w, max_h)
            cfg, frames = r["cfg"], r["frames"]
            self.assertEqual((cfg["width"], cfg["height"]), expected)
            self.assertEqual(cfg["monitor"]["name"], mon["name"])
            self.assertTrue(frames[0].keyframe)
            self.assertTrue({5, 7, 8} <= set(_types(frames[0].data)))
            self.assertEqual(sum(f.keyframe for f in frames), 1)
            decoded = await helpers.adecode_h264([f.data for f in frames[:90]])
            self.assertEqual(len(decoded), 90)
            self.assertEqual(set(decoded), {expected})
            print(f"\n    [live {mon['width']}x{mon['height']} → {expected[0]}x{expected[1]} @60] "
                  f"{r['fps']:.1f} fps, frame gap p50 {r['gap_p50']:.1f} / p99 {r['gap_p99']:.1f} / "
                  f"max {r['gap_max']:.1f} ms, capture→client p50 {r['lat_p50']:.1f} / p95 {r['lat_p95']:.1f} ms, "
                  f"{r['kbps']:.0f} kbps", end="", flush=True)
            self.assertGreater(r["fps"], 55.0, "could not hold ~60 fps end to end")
            self.assertLess(r["lat_p95"], 100.0)
        self.assertEqual(self.client.stale_frames, 0)

    async def test_local_cursor_messages(self):
        c = self.client
        c.start(max_width=640, max_height=400, fps=30, bitrate_kbps=2000, cursor="local")
        cfg = await c.wait_config()
        self.assertEqual(cfg["cursor"], "local")
        self.assertTrue(await _eventually(lambda: c.cursor_shapes and c.cursor_positions, timeout=3.0))
        serial, width, height, hot_x, hot_y, rgba = c.cursor_shapes[0]
        self.assertEqual(len(rgba), width * height * 4)
        mon = cfg["monitor"]
        x, y, visible = c.cursor_positions[0]
        self.assertEqual((x, y), (P.pixel_to_norm(self.pointer_start[0], mon["x"], mon["width"]),
                                  P.pixel_to_norm(self.pointer_start[1], mon["y"], mon["height"])))
        self.assertTrue(visible)

        # 1. Where this client puts the pointer itself is not echoed back …
        await asyncio.sleep(0.1)
        before = len(c.cursor_positions)
        target = (20000, 30000)
        c.send_input(P.MoveAbs(*target))
        want = (P.norm_to_pixel(target[0], mon["x"], mon["width"]), P.norm_to_pixel(target[1], mon["y"], mon["height"]))
        self.assertTrue(await _eventually(lambda: self.x.query_pointer()[:2] == want), self.x.query_pointer())
        await asyncio.sleep(0.15)
        self.assertEqual(len(c.cursor_positions), before, c.cursor_positions[before:])

        # 2. … but motion the client could not predict is reported (relative motion here;
        #    the same path covers someone moving the physical mouse).
        c.send_input(P.MoveRel(37, -21))
        moved = (want[0] + 37, want[1] - 21)
        self.assertTrue(await _eventually(lambda: self.x.query_pointer()[:2] == moved))
        expect = (P.pixel_to_norm(moved[0], mon["x"], mon["width"]),
                  P.pixel_to_norm(moved[1], mon["y"], mon["height"]), True)
        self.assertTrue(await _eventually(lambda: c.cursor_positions[-1] == expect, timeout=2.0),
                        c.cursor_positions[-3:])
        # and the reported position maps back to the exact pixel
        self.assertEqual((P.norm_to_pixel(expect[0], mon["x"], mon["width"]),
                          P.norm_to_pixel(expect[1], mon["y"], mon["height"])), moved)

    async def test_video_cursor_mode_sends_no_cursor_messages(self):
        c = self.client
        c.start(max_width=640, max_height=400, fps=30, bitrate_kbps=2000, cursor="video")
        await c.wait_config()
        await c.collect(15)
        self.assertEqual(c.cursor_shapes, [])
        self.assertEqual(c.cursor_positions, [])

    async def test_held_key_is_released_when_the_client_vanishes(self):
        from foldtop_host.keymap import hid_to_x11_keycode

        shift = hid_to_x11_keycode(0xE1)
        if shift in self.x.pressed_keycodes():
            self.skipTest("Left Shift is physically held")
        c = self.client
        try:
            c.send_input(P.Key(0xE1, True))
            self.assertTrue(await _eventually(lambda: shift in self.x.pressed_keycodes()))
            c.writer.transport.abort()                              # no BYE, no key-up
            self.assertTrue(await _eventually(lambda: shift not in self.x.pressed_keycodes(), timeout=5.0),
                            "Left Shift stayed down after the client disappeared")
        finally:
            if shift in self.x.pressed_keycodes():
                self.x.fake_key(shift, False)
                self.x.sync()

    async def test_held_key_is_released_when_replaced(self):
        from foldtop_host.keymap import hid_to_x11_keycode

        shift = hid_to_x11_keycode(0xE1)
        if shift in self.x.pressed_keycodes():
            self.skipTest("Left Shift is physically held")
        try:
            self.client.send_input(P.Key(0xE1, True))
            self.assertTrue(await _eventually(lambda: shift in self.x.pressed_keycodes()))
            other, _ = await self.h.paired_client("second")
            self.addAsyncCleanup(other.close)
            self.assertTrue(await _eventually(lambda: shift not in self.x.pressed_keycodes(), timeout=5.0))
            self.assertEqual(self.client.bye, {"reason": "replaced"})
        finally:
            if shift in self.x.pressed_keycodes():
                self.x.fake_key(shift, False)
                self.x.sync()


@unittest.skipUnless(helpers.HAVE_DISPLAY, "needs the system D-Bus / Avahi")
class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_is_published_and_resolvable(self):
        from foldtop_host import discovery

        available, detail = discovery.avahi_available()
        if not available:
            self.skipTest(f"Avahi unavailable: {detail}")
        async with helpers.ServerHarness(advertise=True, name="foldtop-selftest") as h:
            status = await h.server._control_command({"cmd": "status"})
            self.assertTrue(status["advertised"])
            loop = asyncio.get_running_loop()
            found = None
            for _ in range(20):                                    # registration takes a moment to probe
                found = await loop.run_in_executor(None, discovery.resolve, status["serviceName"])
                if found:
                    break
                await asyncio.sleep(0.25)
            self.assertIsNotNone(found, "Avahi could not resolve the advertised service")
            self.assertEqual(found["port"], h.port)
            self.assertEqual(found["type"], "_foldtop._tcp")
            self.assertEqual(found["txt"]["v"], "1")
            self.assertEqual(found["txt"]["id"], h.server.identity.host_id)
            self.assertEqual(found["txt"]["name"], "foldtop-selftest")
            self.assertEqual(found["txt"]["fp"], h.fp.hex()[:16])
        # after shutdown the registration is withdrawn
        loop = asyncio.get_running_loop()
        gone = False
        for _ in range(20):
            if await loop.run_in_executor(None, discovery.resolve, status["serviceName"], 700) is None:
                gone = True
                break
            await asyncio.sleep(0.25)
        self.assertTrue(gone, "service still resolvable after the daemon stopped")


if __name__ == "__main__":
    unittest.main()
