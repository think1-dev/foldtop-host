"""The capture pipeline on its own, plus the failure and back-pressure paths that
need a real server."""

import asyncio
import threading
import time
import unittest
from unittest import mock

from . import helpers
from foldtop_host import nal
from foldtop_host import protocol as P

NEEDS_GST = unittest.skipUnless(helpers.HAVE_GST, "needs GStreamer")
NEEDS_STREAM = unittest.skipUnless(helpers.HAVE_DISPLAY and helpers.HAVE_GST, "needs an X display and GStreamer")

if helpers.HAVE_GST:
    from foldtop_host import capture


def _types(data: bytes) -> list[int]:
    return [data[s] & 0x1F for s, _e in nal.iter_nals(data)]


class _Collector:
    def __init__(self):
        self.frames = []
        self.errors = []
        self._cond = threading.Condition()

    def on_frame(self, data, keyframe, pts):
        with self._cond:
            self.frames.append((data, keyframe, pts))
            self._cond.notify_all()

    def on_error(self, message):
        with self._cond:
            self.errors.append(message)
            self._cond.notify_all()

    def wait(self, predicate, timeout=10.0):
        with self._cond:
            return self._cond.wait_for(predicate, timeout)


@NEEDS_GST
class PipelineDescriptionTests(unittest.TestCase):
    def test_screen_source_uses_inclusive_end_coordinates(self):
        desc = capture.build_description(region=(1920, 100, 2560, 1440), width=1280, height=720, fps=60,
                                         encoder_element="x264enc", codec="h264", show_pointer=False, display=":0")
        self.assertIn("startx=1920 starty=100 endx=4479 endy=1539", desc)
        self.assertIn("use-damage=false", desc)
        self.assertIn("show-pointer=false", desc)
        self.assertIn("display-name=:0", desc)
        self.assertIn("framerate=60/1", desc)
        self.assertIn("format=I420,width=1280,height=720,pixel-aspect-ratio=1/1", desc)
        self.assertIn("video/x-h264,stream-format=byte-stream,alignment=au", desc)
        self.assertEqual(desc.count("leaky=downstream"), 2)          # raw frames may be dropped …
        self.assertIn("appsink name=sink emit-signals=true sync=false max-buffers=60 drop=false", desc)  # … encoded never

    def test_pointer_and_codec_variants(self):
        desc = capture.build_description(region=(0, 0, 100, 100), width=100, height=100, fps=30,
                                         encoder_element="nvh265enc", codec="hevc", show_pointer=True)
        self.assertIn("show-pointer=true", desc)
        self.assertNotIn("display-name", desc)
        self.assertIn("nvh265enc name=enc ! video/x-h265,stream-format=byte-stream,alignment=au", desc)

    def test_x264_settings_are_zero_latency(self):
        settings = dict(capture._encoder_settings("x264enc", width=1840, height=1034, fps=60, bitrate_kbps=12000))
        self.assertEqual(settings["tune"], "zerolatency")
        self.assertEqual(settings["bframes"], "0")
        self.assertEqual(settings["rc-lookahead"], "0")
        self.assertEqual(settings["bitrate"], "12000")
        self.assertEqual(settings["byte-stream"], "true")
        self.assertEqual(settings["aud"], "true")
        self.assertIn("scenecut=0", settings["option-string"])
        self.assertIn("repeat-headers=1", settings["option-string"])
        self.assertTrue(2 <= int(settings["threads"]) <= 12)

    def test_encoder_detection_prefers_nvenc_when_installed(self):
        def exists(have):
            return lambda name: name in have

        with mock.patch.object(capture, "element_exists", exists({"x264enc"})):
            enc = capture.detect_encoder()
            self.assertEqual((enc.name, enc.h264, enc.hevc, enc.codecs), ("x264", "x264enc", None, ["h264"]))
            self.assertEqual(enc.element_for("hevc"), ("h264", "x264enc"))       # falls back
        with mock.patch.object(capture, "element_exists", exists({"x264enc", "nvh264enc", "nvh265enc"})):
            enc = capture.detect_encoder()
            self.assertEqual((enc.name, enc.h264, enc.hevc), ("nvenc", "nvh264enc", "nvh265enc"))
            self.assertEqual(enc.codecs, ["hevc", "h264"])                        # best first
            self.assertEqual(enc.element_for("hevc"), ("hevc", "nvh265enc"))
            self.assertEqual(capture.detect_encoder("x264").name, "x264")         # --encoder x264
        with mock.patch.object(capture, "element_exists", exists({"nvh264enc"})):
            self.assertEqual(capture.detect_encoder().codecs, ["h264"])
        with mock.patch.object(capture, "element_exists", exists(set())):
            enc = capture.detect_encoder()
            self.assertEqual(enc.codecs, [])
            with self.assertRaises(RuntimeError):
                enc.element_for("h264")

    def test_nvenc_settings_only_use_properties_that_exist(self):
        """The NVENC branch cannot run here; at least make sure applying its settings to
        an element that lacks most of them is harmless (that is how version skew is handled)."""
        Gst = capture.gst()
        element = Gst.ElementFactory.make("x264enc", None)
        applied = []
        for prop, value in capture._encoder_settings("nvh264enc", width=1840, height=1034, fps=60, bitrate_kbps=9000):
            if element.find_property(prop) is not None:
                Gst.util_set_object_arg(element, prop, value)
                applied.append(prop)
        self.assertIn("bitrate", applied)
        self.assertEqual(element.get_property("bitrate"), 9000)


@NEEDS_GST
class CaptureSessionTests(unittest.TestCase):
    def _session(self, collector, **kw):
        params = dict(region=(0, 0, 320, 180), width=320, height=180, fps=30, bitrate_kbps=800,
                      on_frame=collector.on_frame, on_error=collector.on_error,
                      source=capture.test_source(320, 180, 30, "ball"))
        params.update(kw)
        session = capture.CaptureSession(**params)
        self.addCleanup(session.stop)
        return session

    def test_frames_keyframe_request_and_stop(self):
        c = _Collector()
        s = self._session(c)
        s.start()
        self.assertTrue(c.wait(lambda: len(c.frames) >= 10))
        data, keyframe, pts = c.frames[0]
        self.assertTrue(keyframe)
        self.assertTrue({7, 8, 5} <= set(_types(data)))
        self.assertTrue(all(not kf for _d, kf, _p in c.frames[1:10]))
        self.assertLess(abs(time.monotonic_ns() // 1000 - c.frames[9][2]), 2_000_000)   # real capture times
        before = len(c.frames)
        s.request_keyframe()
        self.assertTrue(c.wait(lambda: any(kf for _d, kf, _p in c.frames[before:]), 5.0), "no IDR after request")
        index = next(i for i, f in enumerate(c.frames) if i >= before and f[1])
        self.assertLessEqual(index - before, 5)                     # within a few frames
        self.assertTrue({7, 8, 5} <= set(_types(c.frames[index][0])))
        self.assertTrue(s.running)
        s.stop()
        self.assertFalse(s.running)
        count = len(c.frames)
        time.sleep(0.3)
        self.assertEqual(len(c.frames), count)                      # nothing after stop()
        self.assertEqual(c.errors, [])
        s.stop()                                                    # idempotent
        s.request_keyframe()                                        # harmless when stopped
        s.set_bitrate(500)

    def test_scaling_to_requested_size(self):
        c = _Collector()
        s = self._session(c, region=(0, 0, 640, 360), width=426, height=240,
                          source=capture.test_source(640, 360, 30))
        s.start()
        self.assertTrue(c.wait(lambda: len(c.frames) >= 5))
        s.stop()
        sizes = helpers.decode_h264([f[0] for f in c.frames[:5]])
        self.assertEqual(set(sizes), {(426, 240)})

    def test_unexpected_end_is_reported(self):
        c = _Collector()
        source = ("videotestsrc is-live=true num-buffers=8 "
                  "! video/x-raw,format=BGRx,width=320,height=180,framerate=30/1")
        s = self._session(c, source=source)
        s.start()
        self.assertTrue(c.wait(lambda: c.errors, 10.0), "end of stream was not reported")
        self.assertIn("ended", c.errors[0])

    def test_bad_pipeline_fails_to_start(self):
        c = _Collector()
        s = self._session(c, source="filesrc location=/nonexistent/foldtop ! video/x-raw,format=BGRx,"
                                    "width=320,height=180,framerate=30/1")
        with self.assertRaises(Exception):
            s.start()
        self.assertFalse(s.running)


@NEEDS_STREAM
class LiveCaptureTests(unittest.TestCase):
    def test_sub_region_of_the_live_screen(self):
        """What streaming a second monitor at an offset amounts to."""
        from foldtop_host import x11

        with x11.Display() as d:
            mon = d.monitors()[0]
        region = (mon.x + 64, mon.y + 32, 801, 451)                 # odd size, non-zero origin
        width, height = P.fit_size(region[2], region[3], 640, 640)
        c = _Collector()
        s = capture.CaptureSession(region=region, width=width, height=height, fps=30, bitrate_kbps=1500,
                                   on_frame=c.on_frame, on_error=c.on_error)
        self.addCleanup(s.stop)
        s.start()
        self.assertTrue(c.wait(lambda: len(c.frames) >= 8))
        s.stop()
        self.assertEqual(c.errors, [])
        self.assertEqual((width, height), (640, 360))
        self.assertEqual(set(helpers.decode_h264([f[0] for f in c.frames[:8]])), {(640, 360)})

    def test_unreachable_display_fails_cleanly(self):
        c = _Collector()
        s = capture.CaptureSession(region=(0, 0, 320, 180), width=320, height=180, fps=30, bitrate_kbps=800,
                                   on_frame=c.on_frame, on_error=c.on_error, display=":4242")
        self.addCleanup(s.stop)
        try:
            s.start()
        except RuntimeError:
            return                                                   # refused synchronously: fine
        self.assertTrue(c.wait(lambda: c.errors, 10.0), "a dead display produced neither an exception nor an error")


@NEEDS_STREAM
class FailureAndBackPressureTests(unittest.IsolatedAsyncioTestCase):
    async def test_capture_that_keeps_dying_is_restarted_then_given_up(self):
        dying = ("videotestsrc is-live=true num-buffers=6 "
                 "! video/x-raw,format=BGRx,width=320,height=180,framerate={fps}/1")
        async with helpers.ServerHarness(test_source=dying) as h:
            c, _ = await h.paired_client()
            try:
                c.start(max_width=320, max_height=180, fps=30, bitrate_kbps=800, cursor="video")
                await asyncio.wait_for(c.eof.wait(), 30.0)
                self.assertEqual(c.bye, {"reason": "capture-failed"})
                epochs = [cfg["epoch"] for cfg in c.configs]
                self.assertEqual(epochs, [0, 1, 2, 3])               # initial start + 3 automatic restarts
                self.assertIsNone(h.server.active)
            finally:
                await c.close()

    async def test_unreachable_display_reports_capture_failed(self):
        async with helpers.ServerHarness(display=":4242") as h:
            c, _ = await h.paired_client()
            try:
                self.assertEqual(c.auth_ok["monitors"], [])           # nothing to enumerate …
                c.send_input(P.MoveRel(1, 1))                         # … and input is dropped, not fatal
                value = c.ping()
                await asyncio.sleep(0.2)
                self.assertIn(value, c.pongs)
                c.start(max_width=320, max_height=180, cursor="video")
                await asyncio.wait_for(c.eof.wait(), 10.0)
                self.assertEqual(c.bye, {"reason": "capture-failed"})
            finally:
                await c.close()

    async def test_stalled_client_gets_a_clean_resync(self):
        """The phone stops reading (bad Wi-Fi). The host must not queue video without
        bound; when the link recovers the stream resumes on a keyframe and decodes."""
        async with helpers.ServerHarness(test_source=helpers.SNOW_SOURCE) as h:
            c, _ = await h.paired_client()
            try:
                c.start(max_width=640, max_height=360, fps=30, bitrate_kbps=20000, cursor="video")
                await c.wait_config()
                await c.collect(15)
                session = h.server.active
                self.assertIsInstance(session._buffered(), int)       # the TLS transport reports its backlog
                transport = c.writer.transport
                transport.pause_reading()
                stalled = False
                for _ in range(200):                                  # up to 20 s
                    await asyncio.sleep(0.1)
                    if session.frames_dropped > 10:
                        stalled = True
                        break
                self.assertTrue(stalled, "host never started dropping while the client was stalled")
                bitrate_bytes = 20000 * 1000 // 8
                self.assertLess(session._buffered(), 2 * bitrate_bytes)   # bounded: under two seconds of video
                transport.resume_reading()

                frames = []
                deadline = time.monotonic() + 20
                resumed_at = None
                while time.monotonic() < deadline:
                    f = await c.next_frame(10.0)
                    frames.append(f)
                    if len(frames) > 1 and f.pts_micros - frames[-2].pts_micros > 150_000 and resumed_at is None:
                        resumed_at = len(frames) - 1
                    if resumed_at is not None and len(frames) - resumed_at >= 20:
                        break
                self.assertIsNotNone(resumed_at, "no gap seen in the received stream")
                self.assertTrue(frames[resumed_at].keyframe, "stream resumed on a delta frame")
                self.assertTrue({5, 7, 8} <= set(_types(frames[resumed_at].data)))
                for i in range(1, len(frames)):                       # every other gap-free frame is a delta
                    if i != resumed_at:
                        self.assertLess(frames[i].pts_micros - frames[i - 1].pts_micros, 150_000)
                tail = await helpers.adecode_h264([f.data for f in frames[resumed_at:]])
                self.assertEqual(len(tail), len(frames) - resumed_at)
                self.assertFalse(session._congested)
                self.assertIsNone(c.bye)
            finally:
                await c.close()


@NEEDS_GST
class VirtualOutputTests(unittest.TestCase):
    """`virtual_output` needs a spare connected output, which the development machine
    lacks — so only the decision logic and the exact xrandr invocation are tested."""

    def _server(self, virtual_output, monitors):
        import tempfile
        from pathlib import Path

        from foldtop_host.config import Config
        from foldtop_host.server import FoldtopServer

        tmp = tempfile.TemporaryDirectory(prefix="foldtop-test-")
        self.addCleanup(tmp.cleanup)
        server = FoldtopServer(Config.load(Path(tmp.name), port=0, advertise=False, display=":0",
                                           virtual_output=virtual_output, encoder="x264"))
        server.monitors = lambda: monitors
        self.addCleanup(server._inject_pool.shutdown)
        return server

    def setUp(self):
        from foldtop_host.x11 import Monitor

        self.primary = Monitor("DP-4", 0, 0, 4384, 2466, True)
        self.spare = Monitor("DP-2", 4384, 0, 1840, 1300, False)

    def test_never_runs_xrandr_unless_configured(self):
        server = self._server(None, [self.primary, self.spare])
        with mock.patch("foldtop_host.server.subprocess.run") as run:
            self.assertEqual(server.resolve_monitor("virtual", 1840, 1300), self.primary)
            self.assertEqual(server.resolve_monitor("primary", 1840, 1300), self.primary)
            self.assertEqual(server.resolve_monitor("DP-2", 1840, 1300), self.spare)
            self.assertEqual(server.resolve_monitor("nope", 1840, 1300), self.primary)
            run.assert_not_called()
        self.assertEqual([m["virtual"] for m in server.monitors_json()], [False, False])

    def test_reshapes_the_configured_output_to_the_viewport(self):
        server = self._server("DP-2", [self.primary, self.spare])
        with mock.patch("foldtop_host.server.subprocess.run") as run, mock.patch("foldtop_host.server.time.sleep"):
            run.return_value = mock.Mock(returncode=0, stderr="")
            self.assertEqual(server.resolve_monitor("virtual", 1840, 1300), self.spare)
            run.assert_called_once()
            argv = run.call_args.args[0]
            self.assertEqual(argv, ["xrandr", "--output", "DP-2", "--scale-from", "1840x1300"])
            self.assertEqual(run.call_args.kwargs["env"]["DISPLAY"], ":0")
            run.reset_mock()
            self.assertEqual(server.resolve_monitor("primary", 1840, 1300), self.primary)   # not asked for: untouched
            self.assertEqual(server.resolve_monitor("DP-2", 1840, 1300), self.spare)        # by name: no reshape
            run.assert_not_called()
        self.assertEqual([m["virtual"] for m in server.monitors_json()], [False, True])

    def test_falls_back_to_primary_when_the_output_is_absent_or_xrandr_fails(self):
        server = self._server("DP-2", [self.primary])                  # dummy plug not connected
        with mock.patch("foldtop_host.server.subprocess.run") as run, mock.patch("foldtop_host.server.time.sleep"):
            run.return_value = mock.Mock(returncode=1, stderr="xrandr: cannot find output")
            self.assertEqual(server.resolve_monitor("virtual", 1840, 1300), self.primary)
            run.side_effect = OSError("xrandr missing")
            self.assertEqual(server.resolve_monitor("virtual", 1840, 1300), self.primary)


if __name__ == "__main__":
    unittest.main()
