"""docs/vectors.json must always agree with the Python implementation — it is what
keeps the Swift client byte-compatible."""

import json
import unittest

from . import helpers
from . import gen_vectors
from foldtop_host import auth, nal
from foldtop_host import protocol as P
from foldtop_host.protocol import Msg


class VectorFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not helpers.VECTORS_PATH.exists():
            raise unittest.SkipTest("docs/vectors.json has not been generated (run tests/gen_vectors.py)")
        cls.doc = json.loads(helpers.VECTORS_PATH.read_text())

    def test_schema(self):
        self.assertEqual(list(self.doc), gen_vectors.SECTION_ORDER)
        for frame in self.doc["frames"]:
            self.assertEqual(set(frame), {"name", "type", "payloadHex", "frameHex"})
        for case in self.doc["input"]:
            self.assertEqual(set(case), {"name", "events", "hex"})
        self.assertEqual(set(self.doc["auth"]), {"clientNonceHex", "serverNonceHex", "certFPHex", "deviceId", "code",
                                                 "kHex", "proofHex", "serverProofHex", "deviceKeyHex", "macHex"})
        self.assertEqual(set(self.doc["videoMessage"]), {"epoch", "flags", "ptsMicros", "dataHex", "payloadHex"})
        self.assertEqual(set(self.doc["cursorShape"]),
                         {"serial", "width", "height", "hotX", "hotY", "rgbaHex", "payloadHex"})
        self.assertEqual(set(self.doc["cursorPos"]), {"x", "y", "visible", "payloadHex"})
        for unit in self.doc["video"]:
            self.assertEqual(set(unit), {"name", "codec", "keyframe", "width", "height", "auHex"})

    def test_file_matches_the_implementation(self):
        fresh = gen_vectors.deterministic_sections()
        for key, value in fresh.items():
            self.assertEqual(self.doc[key], value, f"section {key!r} is stale — run tests/gen_vectors.py")

    def test_every_message_type_has_a_frame(self):
        self.assertEqual({f["type"] for f in self.doc["frames"]}, {int(m) for m in Msg})
        names = [f["name"] for f in self.doc["frames"]]
        self.assertEqual(len(names), len(set(names)))

    def test_frames_decode(self):
        decoder = P.FrameDecoder()
        stream = b"".join(bytes.fromhex(f["frameHex"]) for f in self.doc["frames"])
        decoded = decoder.feed(stream)
        self.assertEqual(decoder.pending, 0)
        self.assertEqual(len(decoded), len(self.doc["frames"]))
        for frame, (msg_type, payload) in zip(self.doc["frames"], decoded):
            self.assertEqual(msg_type, frame["type"], frame["name"])
            self.assertEqual(payload.hex(), frame["payloadHex"], frame["name"])
            self.assertEqual(P.pack_frame(msg_type, payload).hex(), frame["frameHex"], frame["name"])

    def test_json_frames_parse(self):
        json_types = {Msg.HELLO, Msg.CHALLENGE, Msg.AUTH, Msg.AUTH_OK, Msg.AUTH_FAIL, Msg.START,
                      Msg.STREAM_CONFIG, Msg.TUNE, Msg.STATS, Msg.BYE, Msg.AUDIO_CONTROL, Msg.AUDIO_CONFIG}
        for frame in self.doc["frames"]:
            if frame["type"] in json_types:
                self.assertIsInstance(P.parse_json(bytes.fromhex(frame["payloadHex"])), dict, frame["name"])

    def test_audio_frames(self):
        frames = {f["name"]: f for f in self.doc["frames"]}
        self.assertEqual({frames[n]["type"] for n in ("audio-control", "audio-config", "sound", "mic")},
                         {0x40, 0x41, 0x42, 0x43})
        self.assertEqual(P.parse_json(bytes.fromhex(frames["audio-control"]["payloadHex"])),
                         {"sound": True, "microphone": False})
        config = P.parse_json(bytes.fromhex(frames["audio-config"]["payloadHex"]))
        self.assertEqual(config["sound"], {"codec": "pcm_s16le", "sampleRate": 48000, "channels": 2})
        self.assertEqual(config["microphone"], {"codec": "pcm_s16le", "sampleRate": 48000, "channels": 1})
        pts, pcm = P.unpack_sound(bytes.fromhex(frames["sound"]["payloadHex"]))
        self.assertEqual((pts, pcm), (gen_vectors.SOUND_PTS, gen_vectors.SOUND_PCM))
        self.assertTrue(P.pcm_is_whole_frames(pcm, 48000, 2))
        mic = bytes.fromhex(frames["mic"]["payloadHex"])
        self.assertEqual(mic, gen_vectors.MIC_PCM)
        self.assertTrue(P.pcm_is_whole_frames(mic, 48000, 1))
        ok = P.parse_json(bytes.fromhex(frames["auth-ok-audio"]["payloadHex"]))
        self.assertEqual(ok["audio"], {"sound": True, "microphone": True})
        self.assertEqual(ok["addresses"], ["192.168.0.120", "100.64.10.20"])

    def test_input_cases_roundtrip(self):
        ops = set()
        for case in self.doc["input"]:
            events = [gen_vectors.event_from_json(e) for e in case["events"]]
            self.assertEqual(P.encode_input(events).hex(), case["hex"], case["name"])
            self.assertEqual(P.decode_input(bytes.fromhex(case["hex"])), events, case["name"])
            ops |= {e["op"] for e in case["events"]}
        self.assertEqual(ops, {"moveAbs", "moveRel", "button", "scroll", "key", "text", "releaseAll"})

    def test_input_cases_cover_the_edges(self):
        events = [e for case in self.doc["input"] for e in case["events"]]
        self.assertTrue(any(e["op"] == "moveAbs" and e["x"] == 65535 and e["y"] == 65535 for e in events))
        self.assertTrue(any(e["op"] == "moveRel" and e["dx"] < 0 and e["dy"] < 0 for e in events))
        self.assertTrue(any(e["op"] == "moveRel" and e["dx"] == -32768 and e["dy"] == 32767 for e in events))
        self.assertTrue(any(e["op"] == "scroll" and e["dy"] < 0 for e in events))
        self.assertTrue(any(e["op"] == "text" and len(e["text"].encode()) > len(e["text"]) for e in events))
        self.assertTrue(any(e["op"] == "text" and any(ord(ch) > 0xFFFF for ch in e["text"]) for e in events))
        self.assertTrue(any(len(case["events"]) >= 5 for case in self.doc["input"]))

    def test_auth_vector(self):
        a = self.doc["auth"]
        cn, sn, fp = (bytes.fromhex(a[k]) for k in ("clientNonceHex", "serverNonceHex", "certFPHex"))
        k = auth.pair_key(a["code"], cn, sn)
        self.assertEqual(k.hex(), a["kHex"])
        self.assertEqual(auth.client_proof(k, fp, a["deviceId"]).hex(), a["proofHex"])
        self.assertEqual(auth.server_proof(k, fp, a["deviceId"]).hex(), a["serverProofHex"])
        device_key = auth.derive_device_key(k, fp, a["deviceId"])
        self.assertEqual(device_key.hex(), a["deviceKeyHex"])
        self.assertEqual(auth.device_mac(device_key, cn, sn, fp, a["deviceId"]).hex(), a["macHex"])
        self.assertEqual(a["code"], auth.normalize_code(a["code"]))

    def test_binary_message_vectors(self):
        v = self.doc["videoMessage"]
        self.assertEqual(P.unpack_video(bytes.fromhex(v["payloadHex"])),
                         (v["epoch"], v["flags"], v["ptsMicros"], bytes.fromhex(v["dataHex"])))
        c = self.doc["cursorShape"]
        self.assertEqual(P.unpack_cursor_shape(bytes.fromhex(c["payloadHex"])),
                         (c["serial"], c["width"], c["height"], c["hotX"], c["hotY"], bytes.fromhex(c["rgbaHex"])))
        p = self.doc["cursorPos"]
        self.assertEqual(P.unpack_cursor_pos(bytes.fromhex(p["payloadHex"])), (p["x"], p["y"], p["visible"]))

    def test_video_access_units_are_real_and_well_formed(self):
        units = {u["name"]: u for u in self.doc["video"]}
        self.assertEqual(set(units), {"idr", "p"})
        idr, p = bytes.fromhex(units["idr"]["auHex"]), bytes.fromhex(units["p"]["auHex"])
        self.assertTrue(units["idr"]["keyframe"])
        self.assertFalse(units["p"]["keyframe"])
        idr_types = [idr[s] & 0x1F for s, _e in nal.iter_nals(idr)]
        p_types = [p[s] & 0x1F for s, _e in nal.iter_nals(p)]
        self.assertTrue({7, 8, 5} <= set(idr_types), idr_types)
        self.assertNotIn(5, p_types)
        self.assertIn(1, p_types)
        self.assertTrue(nal.inspect("h264", idr).keyframe)
        self.assertFalse(nal.inspect("h264", p).keyframe)
        self.assertLess(len(idr) + len(p), 64 * 1024)

    @unittest.skipUnless(helpers.HAVE_GST, "needs GStreamer")
    def test_video_access_units_decode(self):
        units = self.doc["video"]
        sizes = helpers.decode_h264([bytes.fromhex(u["auHex"]) for u in units])
        self.assertEqual(sizes, [(u["width"], u["height"]) for u in units])


if __name__ == "__main__":
    unittest.main()
