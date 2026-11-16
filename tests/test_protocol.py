import asyncio
import struct
import unittest

from . import helpers  # noqa: F401  (sets sys.path)
from foldtop_host import protocol as P
from foldtop_host.protocol import Msg


class FramingTests(unittest.TestCase):
    def test_pack_frame_layout(self):
        frame = P.pack_frame(Msg.PING, b"\x01\x02")
        self.assertEqual(frame, b"\x00\x00\x00\x03\x30\x01\x02")

    def test_empty_payload(self):
        self.assertEqual(P.pack_frame(Msg.STOP), b"\x00\x00\x00\x01\x13")

    def test_decoder_handles_any_chunking(self):
        frames = [P.pack_frame(Msg.PING, bytes(range(8))), P.pack_frame(Msg.STOP),
                  P.pack_json(Msg.TUNE, {"bitrateKbps": 8000}), P.pack_frame(Msg.VIDEO, b"x" * 70_000)]
        stream = b"".join(frames)
        for chunk in (1, 2, 3, 5, 7, 4096, len(stream)):
            dec = P.FrameDecoder()
            got = []
            for i in range(0, len(stream), chunk):
                got += dec.feed(stream[i:i + chunk])
            self.assertEqual([t for t, _ in got], [Msg.PING, Msg.STOP, Msg.TUNE, Msg.VIDEO], chunk)
            self.assertEqual(got[0][1], bytes(range(8)))
            self.assertEqual(got[1][1], b"")
            self.assertEqual(P.parse_json(got[2][1]), {"bitrateKbps": 8000})
            self.assertEqual(len(got[3][1]), 70_000)
            self.assertEqual(dec.pending, 0)

    def test_length_limits(self):
        with self.assertRaises(P.ProtocolError):
            P.FrameDecoder().feed(b"\x00\x00\x00\x00")           # length 0
        with self.assertRaises(P.ProtocolError):
            P.FrameDecoder().feed(struct.pack(">I", P.MAX_FRAME + 1))
        # exactly the maximum is accepted
        dec = P.FrameDecoder()
        self.assertEqual(dec.feed(struct.pack(">I", P.MAX_FRAME)), [])
        with self.assertRaises(P.ProtocolError):
            P.pack_frame(Msg.VIDEO, b"\x00" * P.MAX_FRAME)

    def test_read_frame_async(self):
        async def go():
            reader = asyncio.StreamReader()
            reader.feed_data(P.pack_frame(Msg.KEYFRAME) + P.pack_frame(Msg.PING, b"12345678"))
            reader.feed_eof()
            self.assertEqual(await P.read_frame(reader), (Msg.KEYFRAME, b""))
            self.assertEqual(await P.read_frame(reader), (Msg.PING, b"12345678"))
            with self.assertRaises(asyncio.IncompleteReadError):
                await P.read_frame(reader)

        asyncio.run(go())

    def test_json_rejects_non_objects(self):
        for bad in (b"[1,2]", b"3", b"\xff\xfe", b"{"):
            with self.assertRaises(P.ProtocolError):
                P.parse_json(bad)

    def test_json_is_utf8_not_escaped(self):
        self.assertEqual(P.json_bytes({"n": "Zoë’s iPhone"}), '{"n":"Zoë’s iPhone"}'.encode("utf-8"))


class InputTests(unittest.TestCase):
    EVENTS = [
        P.MoveAbs(0, 0), P.MoveAbs(65535, 65535), P.MoveAbs(32768, 1),
        P.MoveRel(-32768, 32767), P.MoveRel(-1, 1),
        P.Button(1, True), P.Button(3, False), P.Button(2, True),
        P.Scroll(-120, 120), P.Scroll(15, -7),
        P.Key(0x04, True), P.Key(0xE1, False), P.Key(0xFFFF, True),
        P.Text(""), P.Text("héllo → 🌍"), P.ReleaseAll(),
    ]

    def test_roundtrip(self):
        blob = P.encode_input(self.EVENTS)
        self.assertEqual(P.decode_input(blob), self.EVENTS)

    def test_exact_bytes(self):
        self.assertEqual(P.encode_input([P.MoveAbs(0x1234, 0xABCD)]), bytes.fromhex("011234abcd"))
        self.assertEqual(P.encode_input([P.MoveRel(-2, 3)]), bytes.fromhex("02fffe0003"))
        self.assertEqual(P.encode_input([P.Button(3, True)]), bytes.fromhex("030301"))
        self.assertEqual(P.encode_input([P.Scroll(0, -120)]), bytes.fromhex("040000ff88"))
        self.assertEqual(P.encode_input([P.Key(0xE1, True)]), bytes.fromhex("0500e101"))
        self.assertEqual(P.encode_input([P.Text("é")]), bytes.fromhex("060002c3a9"))
        self.assertEqual(P.encode_input([P.ReleaseAll()]), b"\x07")

    def test_truncated_and_unknown(self):
        for bad in ("01", "0112", "011234ab", "02ff", "0301", "0500e1", "0600", "060005c3a9", "08", "ff"):
            with self.assertRaises(P.ProtocolError, msg=bad):
                P.decode_input(bytes.fromhex(bad))

    def test_invalid_utf8_text(self):
        with self.assertRaises(P.ProtocolError):
            P.decode_input(bytes.fromhex("060002c328"))

    def test_range_checks_on_encode(self):
        for ev in (P.MoveAbs(-1, 0), P.MoveAbs(0, 65536), P.MoveRel(40000, 0), P.Scroll(0, -40000),
                   P.Key(70000, True), P.Button(300, True)):
            with self.assertRaises(P.ProtocolError, msg=repr(ev)):
                P.encode_input([ev])

    def test_empty_message(self):
        self.assertEqual(P.decode_input(b""), [])


class BinaryMessageTests(unittest.TestCase):
    def test_video(self):
        payload = P.pack_video(3, P.VIDEO_FLAG_KEYFRAME, 0x0102030405060708, b"\x00\x00\x00\x01\x65")
        self.assertEqual(payload[:10], bytes.fromhex("03010102030405060708"))
        self.assertEqual(P.unpack_video(payload), (3, 1, 0x0102030405060708, b"\x00\x00\x00\x01\x65"))
        with self.assertRaises(P.ProtocolError):
            P.unpack_video(b"\x00" * 9)

    def test_video_epoch_wraps(self):
        self.assertEqual(P.unpack_video(P.pack_video(256 + 7, 0, 1, b""))[0], 7)

    def test_cursor_shape(self):
        rgba = bytes(range(16))
        payload = P.pack_cursor_shape(0xDEADBEEF, 2, 2, 1, 0, rgba)
        self.assertEqual(payload[:12], bytes.fromhex("deadbeef0002000200010000"))
        self.assertEqual(P.unpack_cursor_shape(payload), (0xDEADBEEF, 2, 2, 1, 0, rgba))
        with self.assertRaises(P.ProtocolError):
            P.pack_cursor_shape(1, 2, 2, 0, 0, b"\x00" * 15)
        with self.assertRaises(P.ProtocolError):
            P.unpack_cursor_shape(payload[:-1])

    def test_cursor_pos(self):
        self.assertEqual(P.pack_cursor_pos(65535, 0, True), bytes.fromhex("ffff000001"))
        self.assertEqual(P.unpack_cursor_pos(bytes.fromhex("0102030400")), (0x0102, 0x0304, False))

    def test_ping(self):
        self.assertEqual(P.pack_ping(1), b"\x00" * 7 + b"\x01")
        self.assertEqual(P.unpack_ping(P.pack_ping(2**64 - 1)), 2**64 - 1)


class GeometryTests(unittest.TestCase):
    def test_norm_to_pixel_endpoints(self):
        self.assertEqual(P.norm_to_pixel(0, 0, 4384), 0)
        self.assertEqual(P.norm_to_pixel(65535, 0, 4384), 4383)
        self.assertEqual(P.norm_to_pixel(65535, 1920, 2560), 1920 + 2559)
        self.assertEqual(P.norm_to_pixel(32768, 0, 3), 1)
        self.assertEqual(P.norm_to_pixel(40000, 7, 1), 7)

    def test_pixel_norm_roundtrip_is_exact(self):
        for size in (2, 3, 640, 1080, 2466, 4384):
            for pixel in {0, 1, size // 3, size // 2, size - 2, size - 1}:
                norm = P.pixel_to_norm(100 + pixel, 100, size)
                self.assertTrue(0 <= norm <= 65535)
                self.assertEqual(P.norm_to_pixel(norm, 100, size), 100 + pixel, (size, pixel))

    def test_pixel_to_norm_clamps(self):
        self.assertEqual(P.pixel_to_norm(-50, 0, 1000), 0)
        self.assertEqual(P.pixel_to_norm(99, 100, 1000), 0)
        self.assertEqual(P.pixel_to_norm(5000, 0, 1000), 65535)

    def test_fit_size(self):
        self.assertEqual(P.fit_size(4384, 2466, 1840, 1300), (1840, 1034))
        self.assertEqual(P.fit_size(4384, 2466, 2700, 1900), (2700, 1518))
        self.assertEqual(P.fit_size(1920, 1080, 4000, 4000), (1920, 1080))      # never upscale
        self.assertEqual(P.fit_size(1921, 1081, 4000, 4000), (1920, 1080))      # even
        self.assertEqual(P.fit_size(1080, 1920, 1000, 1000), (562, 1000))       # portrait
        w, h = P.fit_size(3, 3, 64, 64)
        self.assertEqual((w, h), (2, 2))


if __name__ == "__main__":
    unittest.main()
