"""Server logic that cannot be provoked reliably over a loopback socket:
network back-pressure handling and START validation."""

import asyncio
import unittest

from . import helpers  # noqa: F401
from foldtop_host import protocol as P
from foldtop_host import server as S
from foldtop_host.config import local_addresses, pairing_link
from foldtop_host.protocol import Msg


class _FakeTransport:
    def __init__(self):
        self.buffered = 0

    def get_write_buffer_size(self):
        return self.buffered


class _FakeWriter:
    def __init__(self):
        self.transport = _FakeTransport()
        self.frames = []

    def write(self, data):
        self.frames += P.FrameDecoder().feed(data)

    def get_extra_info(self, _name):
        return ("192.0.2.7", 5555)


class _FakeStream:
    encoder_name = "x264"

    def __init__(self):
        self.keyframe_requests = 0

    def request_keyframe(self):
        self.keyframe_requests += 1


def _session(bitrate=12000):
    async def make():
        return S.ClientSession(server=None, reader=None, writer=_FakeWriter())

    session = asyncio.run(make())
    session.stream = _FakeStream()
    session.epoch = 4
    session.params = S.StreamParams("primary", 1840, 1300, 60, bitrate, "h264", "local")
    return session


def _video(session):
    return [P.unpack_video(payload) for t, payload in session.writer.frames if t == Msg.VIDEO]


class BackPressureTests(unittest.TestCase):
    def test_normal_flow(self):
        s = _session()
        s._on_frame(4, b"IDR", True, 100)
        s._on_frame(4, b"P1", False, 200)
        self.assertEqual(_video(s), [(4, 1, 100, b"IDR"), (4, 0, 200, b"P1")])
        self.assertEqual(s.frames_dropped, 0)

    def test_stream_never_starts_on_a_delta_frame(self):
        s = _session()
        s._on_frame(4, b"P0", False, 1)
        s._on_frame(4, b"P1", False, 2)
        self.assertEqual(_video(s), [])
        self.assertEqual(s.stream.keyframe_requests, 1)     # asked once, not per frame
        s._on_frame(4, b"IDR", True, 3)
        s._on_frame(4, b"P2", False, 4)
        self.assertEqual([v[3] for v in _video(s)], [b"IDR", b"P2"])

    def test_frames_from_another_epoch_are_ignored(self):
        s = _session()
        s._on_frame(3, b"old", True, 1)
        self.assertEqual(_video(s), [])

    def test_congestion_drops_until_drained_then_resumes_on_idr(self):
        s = _session(bitrate=12000)
        high = max(512 * 1024, 12000 * 1000 // 8 // 2)
        s._on_frame(4, b"IDR", True, 1)
        s.writer.transport.buffered = high + 1            # socket backs up
        s._on_frame(4, b"P1", False, 2)                   # dropped → enters congestion
        s._on_frame(4, b"P2", False, 3)
        s._on_frame(4, b"I-mid", True, 4)                 # even a keyframe: still too full
        self.assertEqual([v[3] for v in _video(s)], [b"IDR"])
        self.assertEqual(s.stream.keyframe_requests, 0)   # pointless to ask while full

        s.writer.transport.buffered = 0                   # drained
        s._on_frame(4, b"P3", False, 5)                   # cannot decode without P1/P2
        s._on_frame(4, b"P4", False, 6)
        self.assertEqual(s.stream.keyframe_requests, 1)
        self.assertEqual([v[3] for v in _video(s)], [b"IDR"])
        s._on_frame(4, b"IDR2", True, 7)
        s._on_frame(4, b"P5", False, 8)
        self.assertEqual([v[3] for v in _video(s)], [b"IDR", b"IDR2", b"P5"])
        self.assertEqual(s.frames_dropped, 5)
        self.assertEqual(s.frames_sent, 3)

    def test_closed_session_sends_nothing(self):
        s = _session()
        s.closed = True
        s._on_frame(4, b"IDR", True, 1)
        self.assertEqual(s.writer.frames, [])


class StartParamTests(unittest.TestCase):
    def test_defaults_and_clamping(self):
        p = S.StreamParams.parse({"maxWidth": 1840, "maxHeight": 1300}, max_fps=120)
        self.assertEqual((p.monitor, p.fps, p.bitrate_kbps, p.codec, p.cursor), ("primary", 60, 12000, "h264", "video"))
        p = S.StreamParams.parse({"maxWidth": 10, "maxHeight": 99999, "fps": 1000, "bitrateKbps": 1,
                                  "codec": "av1", "cursor": "local", "monitor": "DP-2"}, max_fps=120)
        self.assertEqual((p.max_width, p.max_height, p.fps, p.bitrate_kbps), (64, 8192, 120, 500))
        self.assertEqual((p.codec, p.cursor, p.monitor), ("h264", "local", "DP-2"))

    def test_fractional_numbers_accepted(self):
        p = S.StreamParams.parse({"maxWidth": 1840.0, "maxHeight": 1300.7, "fps": 59.94}, max_fps=120)
        self.assertEqual((p.max_width, p.max_height, p.fps), (1840, 1300, 59))

    def test_missing_or_bad_size_is_a_protocol_error(self):
        for bad in ({}, {"maxWidth": 100}, {"maxWidth": "big", "maxHeight": 100}, {"maxWidth": True, "maxHeight": 1}):
            with self.assertRaises(P.ProtocolError, msg=repr(bad)):
                S.StreamParams.parse(bad, max_fps=120)


class PairingLinkTests(unittest.TestCase):
    def test_link_format(self):
        link = pairing_link(hosts=["192.168.0.120", "100.64.10.20"], port=47800, cert_fp_hex="ab" * 32,
                            code="ABCDEFGH", host_id="2d2a6cd9-91e9-4406-bb78-0506e8fec230",
                            host_name="work station/1")
        self.assertEqual(
            link,
            "foldtop://pair?host=192.168.0.120,100.64.10.20&port=47800&fp=" + "ab" * 32 +
            "&code=ABCDEFGH&id=2d2a6cd9-91e9-4406-bb78-0506e8fec230&name=work%20station%2F1")

    def test_local_addresses_skip_loopback_and_bridges(self):
        addrs = local_addresses()
        self.assertTrue(all(not a.startswith("127.") for a in addrs))
        self.assertEqual(len(addrs), len(set(addrs)))
        self.assertTrue(all(not a.startswith("172.17.") for a in addrs))  # docker0


if __name__ == "__main__":
    unittest.main()
