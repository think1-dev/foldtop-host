import unittest

from . import helpers  # noqa: F401
from foldtop_host import nal

S3 = b"\x00\x00\x01"
S4 = b"\x00\x00\x00\x01"
AUD = b"\x09\xf0"
SPS = b"\x67\x64\x00\x1f\xac"
PPS = b"\x68\xee\x3c\x80"
SEI = b"\x06\x05\x01\xaa\x80"
IDR = b"\x65\x88\x84\x00\x10"
P_SLICE = b"\x41\x9a\x24\x6c"


def au(*nals, start=S4):
    return b"".join(start + n for n in nals)


class NalTests(unittest.TestCase):
    def test_iter_handles_both_start_codes(self):
        data = S4 + AUD + S3 + SPS + S4 + PPS + S3 + IDR
        spans = [data[s:e] for s, e in nal.iter_nals(data)]
        self.assertEqual(spans, [AUD, SPS, PPS, IDR])

    def test_no_start_code(self):
        self.assertEqual(list(nal.iter_nals(b"\x65\x88\x84")), [])
        self.assertEqual(list(nal.iter_nals(b"")), [])

    def test_inspect_keyframe(self):
        info = nal.inspect(nal.H264, au(AUD, SPS, PPS, SEI, IDR, IDR))
        self.assertTrue(info.keyframe)
        self.assertEqual(info.types, [9, 7, 8, 6, 5])           # stops at the first slice
        self.assertEqual(info.parameter_sets, [SPS, PPS])
        self.assertEqual(info.after_aud, len(S4) + len(AUD))

    def test_inspect_delta_frame(self):
        info = nal.inspect(nal.H264, au(AUD, P_SLICE, P_SLICE))
        self.assertFalse(info.keyframe)
        self.assertEqual(info.types, [9, 1])
        self.assertEqual(info.parameter_sets, [])

    def test_cache_passes_complete_keyframes_through(self):
        cache = nal.ParameterSetCache(nal.H264)
        data = au(AUD, SPS, PPS, IDR)
        out, keyframe, _ = cache.process(data)
        self.assertTrue(keyframe)
        self.assertEqual(out, data)
        self.assertTrue(cache.complete())

    def test_cache_reinserts_missing_parameter_sets_after_aud(self):
        cache = nal.ParameterSetCache(nal.H264)
        cache.process(au(AUD, SPS, PPS, IDR))
        out, keyframe, _ = cache.process(au(AUD, P_SLICE))
        self.assertFalse(keyframe)
        self.assertEqual(out, au(AUD, P_SLICE))
        bare = au(AUD, IDR)
        out, keyframe, _ = cache.process(bare)
        self.assertTrue(keyframe)
        self.assertEqual([out[s:e] for s, e in nal.iter_nals(out)], [AUD, SPS, PPS, IDR])
        # without an AUD the sets go first
        out, _, _ = cache.process(au(IDR, start=S3))
        self.assertEqual([out[s:e] for s, e in nal.iter_nals(out)], [SPS, PPS, IDR])

    def test_cache_only_adds_what_is_missing(self):
        cache = nal.ParameterSetCache(nal.H264)
        cache.process(au(SPS, PPS, IDR))
        out, _, _ = cache.process(au(SPS, IDR))
        self.assertEqual([out[s:e] for s, e in nal.iter_nals(out)], [PPS, SPS, IDR])

    def test_keyframe_before_any_parameter_sets_is_left_alone(self):
        cache = nal.ParameterSetCache(nal.H264)
        out, keyframe, _ = cache.process(au(IDR))
        self.assertTrue(keyframe)
        self.assertEqual(out, au(IDR))
        self.assertFalse(cache.complete())

    def test_hevc_types(self):
        vps, sps, pps = b"\x40\x01\x0c", b"\x42\x01\x01", b"\x44\x01\xc1"
        idr = b"\x26\x01\xaf"      # nal_unit_type 19 (IDR_W_RADL)
        trail = b"\x02\x01\xd0"    # nal_unit_type 1 (TRAIL_R)
        self.assertEqual(nal.nal_type(nal.HEVC, idr[0]), 19)
        info = nal.inspect(nal.HEVC, au(vps, sps, pps, idr))
        self.assertTrue(info.keyframe)
        self.assertEqual(info.types, [32, 33, 34, 19])
        self.assertFalse(nal.inspect(nal.HEVC, au(trail)).keyframe)
        cache = nal.ParameterSetCache(nal.HEVC)
        cache.process(au(vps, sps, pps, idr))
        out, keyframe, _ = cache.process(au(idr))
        self.assertTrue(keyframe)
        self.assertEqual([out[s:e] for s, e in nal.iter_nals(out)], [vps, sps, pps, idr])


if __name__ == "__main__":
    unittest.main()
