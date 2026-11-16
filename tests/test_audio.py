"""Audio (docs/PROTOCOL.md "Audio"): sound to the phone and the phone's microphone.

The tests that touch the audio server make no audible sound: tones are played into a
private null sink and captured from its monitor, and the virtual microphone is recorded
back from its own source. Every device they create has a per-process `foldtop_t<pid>`
name and is unloaded again; nothing changes the default devices, volumes or mute state.
"""

import asyncio
import statistics
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from . import helpers
from foldtop_host import audio
from foldtop_host import protocol as P
from foldtop_host import server as S
from foldtop_host.protocol import Msg

NEEDS_AUDIO = unittest.skipUnless(helpers.HAVE_AUDIO and helpers.HAVE_GST, "needs an audio server and GStreamer")
NEEDS_AUDIO_AND_STREAM = unittest.skipUnless(
    helpers.HAVE_AUDIO and helpers.HAVE_GST and helpers.HAVE_DISPLAY, "needs an audio server, GStreamer and an X display")

SOUND_FORMAT = {"codec": "pcm_s16le", "sampleRate": 48000, "channels": 2}
MIC_FORMAT = {"codec": "pcm_s16le", "sampleRate": 48000, "channels": 1}


def tearDownModule():
    if helpers.HAVE_AUDIO:
        helpers.unload_audio_test_modules()


# --------------------------------------------------------------------------- pure

class AudioProtocolTests(unittest.TestCase):
    def test_message_types(self):
        self.assertEqual((Msg.AUDIO_CONTROL, Msg.AUDIO_CONFIG, Msg.SOUND, Msg.MIC), (0x40, 0x41, 0x42, 0x43))

    def test_sound_round_trip(self):
        pcm = bytes(range(16))
        payload = P.pack_sound(0x0102030405060708, pcm)
        self.assertEqual(payload[:8], bytes([1, 2, 3, 4, 5, 6, 7, 8]))      # big-endian timestamp
        self.assertEqual(P.unpack_sound(payload), (0x0102030405060708, pcm))
        self.assertEqual(P.unpack_sound(P.pack_sound(7, b"")), (7, b""))
        with self.assertRaises(P.ProtocolError):
            P.unpack_sound(b"\x00" * 7)

    def test_formats(self):
        self.assertEqual(P.audio_format(48000, 2), SOUND_FORMAT)
        self.assertEqual(audio.sound_format(), SOUND_FORMAT)
        self.assertEqual(audio.microphone_format(), MIC_FORMAT)
        self.assertEqual(P.pcm_frame_size(2), 4)

    def test_whole_frames_and_the_100_ms_limit(self):
        self.assertTrue(P.pcm_is_whole_frames(b"\x00" * 960, 48000, 1))        # 10 ms mono
        self.assertTrue(P.pcm_is_whole_frames(b"\x00" * 9600, 48000, 1))       # exactly 100 ms
        self.assertFalse(P.pcm_is_whole_frames(b"\x00" * 9602, 48000, 1))      # longer than 100 ms
        self.assertFalse(P.pcm_is_whole_frames(b"\x00" * 961, 48000, 1))       # half a sample
        self.assertFalse(P.pcm_is_whole_frames(b"\x00" * 1922, 48000, 2))      # half a stereo frame
        self.assertFalse(P.pcm_is_whole_frames(b"", 48000, 1))


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


def _session():
    async def make():
        return S.ClientSession(server=None, reader=None, writer=_FakeWriter())

    session = asyncio.run(make())
    session.sound = object()        # "sound is on"
    session._sound_gen = 3
    return session


def _sound(session):
    return [P.unpack_sound(payload) for t, payload in session.writer.frames if t == Msg.SOUND]


class SoundBackPressureTests(unittest.TestCase):
    """SOUND is dropped, never queued, when the client is not keeping up."""

    def test_normal_flow(self):
        s = _session()
        s._on_sound(3, 100, b"\x01\x00\x02\x00")
        s._on_sound(3, 200, b"\x03\x00\x04\x00")
        self.assertEqual(_sound(s), [(100, b"\x01\x00\x02\x00"), (200, b"\x03\x00\x04\x00")])
        self.assertEqual((s.sound_sent, s.sound_dropped), (2, 0))

    def test_dropped_while_the_write_buffer_is_backed_up(self):
        s = _session()
        limit = s._sound_backlog_limit()
        self.assertGreaterEqual(limit, S.MIN_SOUND_BACKLOG)
        s.writer.transport.buffered = limit + 1
        s._on_sound(3, 1, b"\x00" * 4)
        s._on_sound(3, 2, b"\x00" * 4)
        self.assertEqual(_sound(s), [])
        self.assertEqual((s.sound_sent, s.sound_dropped), (0, 2))
        s.writer.transport.buffered = limit                         # drained: sound resumes with the next chunk
        s._on_sound(3, 3, b"\x05\x00\x06\x00")
        self.assertEqual(_sound(s), [(3, b"\x05\x00\x06\x00")])

    def test_dropped_while_video_is_resynchronising(self):
        s = _session()
        s._congested = True
        s._on_sound(3, 1, b"\x00" * 4)
        self.assertEqual((_sound(s), s.sound_dropped), ([], 1))

    def test_limit_follows_the_video_bitrate(self):
        s = _session()
        self.assertEqual(s._sound_backlog_limit(), max(S.MIN_SOUND_BACKLOG, S.DEFAULT_BITRATE * 1000 // 8 * 150 // 1000))
        s.params = S.StreamParams("primary", 1840, 1300, 60, 40000, "h264", "local")
        self.assertEqual(s._sound_backlog_limit(), 40000 * 1000 // 8 * 150 // 1000)      # 150 ms of 40 Mb/s
        s.params = S.StreamParams("primary", 1840, 1300, 60, 500, "h264", "local")
        self.assertEqual(s._sound_backlog_limit(), S.MIN_SOUND_BACKLOG)

    def test_stale_or_unwanted_chunks_are_ignored(self):
        s = _session()
        s._on_sound(2, 1, b"\x00" * 4)                             # from a capture that was stopped
        s.sound = None
        s._on_sound(3, 2, b"\x00" * 4)                             # sound is off
        s.sound = object()
        s.closed = True
        s._on_sound(3, 3, b"\x00" * 4)                             # the connection is gone
        self.assertEqual((s.writer.frames, s.sound_sent, s.sound_dropped), ([], 0, 0))


class CapabilityTests(unittest.TestCase):
    def _manager(self, **kwargs):
        tmp = tempfile.TemporaryDirectory(prefix="foldtop-audio-")
        self.addCleanup(tmp.cleanup)
        return audio.AudioManager(state_dir=Path(tmp.name), **kwargs)

    def test_disabled(self):
        caps = self._manager(enabled=False).capabilities()
        self.assertEqual(caps.to_json(), {"sound": False, "microphone": False})
        self.assertIn("switched off", caps.sound_problem)

    def test_no_pactl(self):
        with mock.patch.object(audio.shutil, "which", return_value=None):
            caps = self._manager().capabilities()
        self.assertEqual(caps.to_json(), {"sound": False, "microphone": False})
        self.assertIn("pactl", caps.microphone_problem)

    def test_no_audio_server(self):
        with mock.patch.object(audio.shutil, "which", return_value="/usr/bin/pactl"), \
                mock.patch.object(audio, "pactl", return_value=(1, "", "Connection failure: Connection refused")):
            caps = self._manager().capabilities()
        self.assertEqual(caps.to_json(), {"sound": False, "microphone": False})
        self.assertIn("no audio server", caps.sound_problem)

    def test_missing_element_disables_only_that_direction(self):
        with mock.patch.object(audio.shutil, "which", return_value="/usr/bin/pactl"), \
                mock.patch.object(audio, "pactl", return_value=(0, "Server Name: test", "")), \
                mock.patch.object(audio, "element_exists", side_effect=lambda name: name != "pulsesink"):
            caps = self._manager().capabilities()
        self.assertEqual(caps.to_json(), {"sound": True, "microphone": False})
        self.assertIn("pulsesink", caps.microphone_problem)

    def test_defaults(self):
        manager = self._manager()
        self.assertEqual((manager.sound_source, manager.mic_name), ("@DEFAULT_MONITOR@", "foldtop_mic"))
        manager = self._manager(sound_source="some.monitor", mic_name="other_mic")
        self.assertEqual((manager.sound_source, manager.mic_name), ("some.monitor", "other_mic"))


class ModuleBookkeepingTests(unittest.TestCase):
    """The daemon only ever unloads modules it loaded itself."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="foldtop-audio-")
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.calls = []

    def _pactl(self, modules):
        def fake(*args, timeout=5.0):
            self.calls.append(args)
            if args[0] == "load-module":
                return 0, str(100 + sum(1 for c in self.calls if c[0] == "load-module")), ""
            if args[:3] == ("list", "short", "modules"):
                return 0, "\n".join(f"{i}\t{rest}" for i, rest in modules.items()), ""
            return 0, "", ""
        return fake

    def test_load_is_recorded_and_unload_checks_identity(self):
        modules = {"101": "module-null-sink\tsink_name=foldtop_mic_sink rate=48000"}
        with mock.patch.object(audio, "pactl", self._pactl(modules)):
            manager = audio.AudioManager(state_dir=self.dir)
            ident = manager.load_module("module-null-sink", "sink_name=foldtop_mic_sink",
                                        marker="sink_name=foldtop_mic_sink")
            self.assertEqual(ident, "101")
            self.assertTrue((self.dir / "audio-modules.json").exists())
            manager.unload_module(ident)
        self.assertIn(("unload-module", "101"), self.calls)
        self.assertFalse((self.dir / "audio-modules.json").exists())

    def test_a_reused_module_id_is_not_unloaded(self):
        # The audio server restarted and id 101 now belongs to something else.
        modules = {"101": "module-loopback\tsource=someone.elses"}
        with mock.patch.object(audio, "pactl", self._pactl(modules)):
            manager = audio.AudioManager(state_dir=self.dir)
            ident = manager.load_module("module-null-sink", "sink_name=foldtop_mic_sink",
                                        marker="sink_name=foldtop_mic_sink")
            manager.unload_module(ident)
            manager.unload_module("999")                                  # never loaded by this daemon
        self.assertNotIn("unload-module", [c[0] for c in self.calls])

    def test_failed_load_raises(self):
        with mock.patch.object(audio, "pactl", return_value=(1, "", "Failure: Module initialization failed")):
            with self.assertRaises(RuntimeError):
                audio.AudioManager(state_dir=self.dir).load_module("module-null-sink", marker="x")
        self.assertFalse((self.dir / "audio-modules.json").exists())

    def test_stale_cleanup_only_touches_recorded_modules_that_still_match(self):
        (self.dir / "audio-modules.json").write_text('{"7": "sink_name=foldtop_mic_sink", "8": "source_name=foldtop_mic"}')
        modules = {"7": "module-null-sink\tsink_name=foldtop_mic_sink", "8": "module-loopback\tunrelated",
                   "9": "module-null-sink\tsink_name=somebody_elses_sink"}
        with mock.patch.object(audio, "pactl", self._pactl(modules)):
            removed = audio.AudioManager(state_dir=self.dir).cleanup_stale()
        self.assertEqual(removed, 1)
        self.assertEqual([c for c in self.calls if c[0] == "unload-module"], [("unload-module", "7")])
        self.assertFalse((self.dir / "audio-modules.json").exists())

    def test_stale_cleanup_without_a_record_does_nothing(self):
        with mock.patch.object(audio, "pactl", self._pactl({})):
            self.assertEqual(audio.AudioManager(state_dir=self.dir).cleanup_stale(), 0)
        self.assertEqual(self.calls, [])


class _FakeAppSrc:
    def __init__(self):
        self.buffers = 0

    def emit(self, signal, _buffer):
        assert signal == "push-buffer"
        self.buffers += 1
        from foldtop_host import capture

        return capture.gst().FlowReturn.OK


@unittest.skipUnless(helpers.HAVE_GST, "needs GStreamer")
class MicrophonePacingTests(unittest.TestCase):
    """Microphone audio that arrives faster than real time is dropped, not queued, so
    speech never comes out late. Driven with explicit times; needs no audio server."""

    CHUNK = bytes(960)          # 10 ms of mono S16LE

    def _mic(self):
        tmp = tempfile.TemporaryDirectory(prefix="foldtop-audio-")
        self.addCleanup(tmp.cleanup)
        mic = audio.VirtualMicrophone(audio.AudioManager(state_dir=Path(tmp.name)), "unused")
        mic._src = _FakeAppSrc()
        return mic

    def test_real_time_pace_is_never_dropped(self):
        mic = self._mic()
        for i in range(500):
            self.assertTrue(mic.push(self.CHUNK, now=100.0 + i * 0.010))
        self.assertEqual((mic.pushed, mic.dropped, mic._src.buffers), (500, 0, 500))

    def test_jittery_but_real_time_delivery_is_not_dropped(self):
        mic = self._mic()
        now = 100.0
        for i in range(300):                                   # pairs of chunks every 20 ms
            now += 0.020 if i % 2 == 0 else 0.0
            self.assertTrue(mic.push(self.CHUNK, now=now), i)
        self.assertEqual(mic.dropped, 0)

    def test_a_burst_is_cut_to_the_allowance(self):
        mic = self._mic()
        results = [mic.push(self.CHUNK, now=50.0) for _ in range(300)]       # three seconds at once
        accepted = sum(results)
        self.assertTrue(12 <= accepted <= 14, accepted)                       # about 120 ms
        self.assertEqual(mic.dropped, 300 - accepted)
        self.assertTrue(all(results[:accepted]) and not any(results[accepted:]))

    def test_audio_after_a_burst_flows_again_at_real_time(self):
        mic = self._mic()
        for _ in range(300):
            mic.push(self.CHUNK, now=50.0)
        dropped = mic.dropped
        outcomes = [mic.push(self.CHUNK, now=50.0 + (i + 1) * 0.010) for i in range(200)]
        self.assertLessEqual(mic.dropped - dropped, 2)
        self.assertTrue(all(outcomes[5:]))

    def test_a_fast_clock_loses_only_the_excess(self):
        mic = self._mic()
        total = 3000                                            # 30 s of audio delivered in 25 s (20 % fast)
        for i in range(total):
            mic.push(self.CHUNK, now=10.0 + i * (25.0 / total))
        fraction = mic.dropped / total
        self.assertTrue(0.14 <= fraction <= 0.18, fraction)     # 1 - 25/30 ≈ 16.7 %

    def test_a_pause_resets_the_allowance_and_being_late_banks_no_credit(self):
        mic = self._mic()
        for _ in range(100):
            mic.push(self.CHUNK, now=10.0)                      # burst: mostly dropped
        self.assertTrue(mic.push(self.CHUNK, now=11.0))         # a second later: accepted straight away
        mic = self._mic()
        self.assertTrue(mic.push(self.CHUNK, now=10.0))
        accepted = sum(mic.push(self.CHUNK, now=10.2) for _ in range(100))   # 190 ms late, then a burst
        self.assertTrue(12 <= accepted <= 14, accepted)         # still only the allowance, not 190 ms more

    def test_malformed_audio_is_rejected_before_pacing(self):
        mic = self._mic()
        self.assertFalse(mic.push(b"\x00" * 961, now=1.0))
        self.assertFalse(mic.push(b"", now=1.0))
        self.assertFalse(mic.push(bytes(9602), now=1.0))
        self.assertEqual((mic.rejected, mic.dropped, mic.pushed), (3, 0, 0))

    def test_nothing_is_accepted_once_stopped(self):
        mic = self._mic()
        mic._src = None
        self.assertFalse(mic.push(self.CHUNK, now=1.0))
        self.assertEqual((mic.pushed, mic.dropped, mic.rejected), (0, 0, 0))


class DefaultInputTests(unittest.TestCase):
    """Whether, and how, the virtual microphone becomes the workstation's default input.

    `pactl` is replaced throughout, so the real default input is never touched."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.state = {"default": "hdmi.monitor", "fail_set": False}
        self.calls = []
        self.existing = {"hdmi.monitor", "usb.mic", "foldtop_mic"}

        def fake_pactl(*args, timeout=5.0):
            self.calls.append(args)
            if args[0] == "get-default-source":
                return 0, self.state["default"] + "\n", ""
            if args[0] == "set-default-source":
                if self.state["fail_set"]:
                    return 1, "", "No such entity"
                self.state["default"] = args[1]
                return 0, "", ""
            return 0, "", ""

        for target, replacement in (("pactl", fake_pactl), ("source_exists", lambda name: name in self.existing)):
            patcher = mock.patch.object(audio, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def _mic(self, **kwargs):
        manager = audio.AudioManager(state_dir=self.dir, **kwargs)
        return manager.new_microphone()

    def _sets(self):
        return [call[1] for call in self.calls if call[0] == "set-default-source"]

    def test_only_the_standard_device_becomes_the_default_unless_told_otherwise(self):
        self.assertTrue(audio.AudioManager(state_dir=self.dir).mic_default)
        self.assertFalse(audio.AudioManager(state_dir=self.dir, mic_name="other_mic").mic_default)
        self.assertTrue(audio.AudioManager(state_dir=self.dir, mic_name="other_mic", mic_default=True).mic_default)
        self.assertFalse(audio.AudioManager(state_dir=self.dir, mic_default=False).mic_default)
        self.assertTrue(self._mic().make_default)
        self.assertFalse(self._mic(mic_name="other_mic").make_default)

    def test_becomes_the_default_and_puts_the_old_one_back(self):
        mic = self._mic()
        mic._claim_default()
        self.assertEqual(self.state["default"], "foldtop_mic")
        mic._release_default()
        self.assertEqual(self.state["default"], "hdmi.monitor")
        self.assertEqual(self._sets(), ["foldtop_mic", "hdmi.monitor"])
        before = len(self.calls)
        mic._release_default()                                   # a second stop changes nothing
        self.assertEqual(len(self.calls), before)

    def test_a_default_chosen_meanwhile_is_left_alone(self):
        mic = self._mic()
        mic._claim_default()
        self.state["default"] = "usb.mic"                        # the user picked another input
        mic._release_default()
        self.assertEqual(self.state["default"], "usb.mic")
        self.assertEqual(self._sets(), ["foldtop_mic"])

    def test_nothing_is_restored_if_it_already_was_the_default(self):
        self.state["default"] = "foldtop_mic"
        mic = self._mic()
        mic._claim_default()
        mic._release_default()
        self.assertEqual(self._sets(), ["foldtop_mic"])

    def test_an_old_default_that_no_longer_exists_is_not_restored(self):
        mic = self._mic()
        mic._claim_default()
        self.existing.discard("hdmi.monitor")
        mic._release_default()
        self.assertEqual(self._sets(), ["foldtop_mic"])

    def test_failing_to_become_the_default_is_not_fatal(self):
        self.state["fail_set"] = True
        mic = self._mic()
        mic._claim_default()                                     # must not raise
        self.assertEqual(self.state["default"], "hdmi.monitor")
        mic._release_default()
        # One attempt, which failed; nothing was changed, so nothing is restored.
        self.assertEqual(self._sets(), ["foldtop_mic"])
        self.assertEqual(self.state["default"], "hdmi.monitor")

    def test_release_without_claim_does_nothing(self):
        mic = self._mic(mic_default=False)
        mic._release_default()
        self.assertEqual(self.calls, [])


# --------------------------------------------------------------------------- with the audio server

@NEEDS_AUDIO
class SoundTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sink = helpers.NullSink()
        self.sink.__enter__()
        self.addCleanup(self.sink.__exit__)

    async def test_sound_arrives_in_ten_millisecond_chunks_and_is_the_tone(self):
        with helpers.Tone(self.sink.name, freq=440, volume=0.2):
            async with helpers.ServerHarness(sound_source=self.sink.monitor, mic_name=helpers.audio_test_name("mic")) as h:
                c, _ = await h.paired_client()
                try:
                    self.assertEqual(c.auth_ok["audio"], {"sound": True, "microphone": True})
                    c.audio_control(sound=True)
                    self.assertEqual(await c.wait_audio_config(), {"sound": SOUND_FORMAT, "microphone": None})
                    chunks = await c.wait_sound(200)                          # about two seconds
                    sizes = [len(ch.pcm) for ch in chunks]
                    self.assertTrue(all(size > 0 and size % 4 == 0 for size in sizes), "not whole stereo frames")
                    self.assertLessEqual(max(sizes), 48000 * 4 // 10, "a SOUND message is longer than 100 ms")
                    self.assertEqual(statistics.median(sizes), 1920, "chunks are not 10 ms")
                    span = chunks[-1].received_at - chunks[0].received_at
                    per_second = (len(chunks) - 1) / span
                    self.assertTrue(80 <= per_second <= 125, f"{per_second:.0f} messages per second")
                    data_rate = sum(sizes[1:]) / span
                    self.assertTrue(0.8 * 192000 <= data_rate <= 1.25 * 192000, f"{data_rate:.0f} bytes per second")
                    # A 0.2-amplitude sine is 0.2 * 32767 / sqrt(2) ≈ 4634 RMS: this is the tone, not silence.
                    level = helpers.rms(helpers.samples_of(b"".join(ch.pcm for ch in chunks[50:])))
                    self.assertTrue(3500 <= level <= 5800, f"level {level:.0f}")
                    frequency = helpers.zero_crossing_frequency(helpers.samples_of(b"".join(ch.pcm for ch in chunks[50:]))[::2])
                    self.assertTrue(400 <= frequency <= 480, f"{frequency:.0f} Hz")
                    stamps = [ch.pts_micros for ch in chunks]
                    self.assertEqual(stamps, sorted(stamps))
                    now = time.monotonic_ns() // 1000
                    self.assertTrue(all(now - 5_000_000 <= stamp <= now for stamp in stamps), "timestamps are not on the monotonic clock")
                    self.assertGreater(h.server.active.sound_sent, 150)
                    self.assertEqual(h.server.active.sound_dropped, 0)
                finally:
                    await c.close()

    async def test_control_is_idempotent_and_can_switch_off(self):
        async with helpers.ServerHarness(sound_source=self.sink.monitor, mic_name=helpers.audio_test_name("mic")) as h:
            c, _ = await h.paired_client()
            try:
                c.audio_control(sound=True)
                first = await c.wait_audio_config()
                session = h.server.active
                capture = session.sound
                self.assertIsNotNone(capture)
                c.audio_control(sound=True)                                   # again: answered, nothing restarted
                self.assertEqual(await c.wait_audio_config(1), first)
                self.assertIs(session.sound, capture)
                await c.wait_sound(20)

                c.audio_control(sound=False)
                self.assertEqual(await c.wait_audio_config(2), {"sound": None, "microphone": None})
                self.assertIsNone(session.sound)
                await asyncio.sleep(0.3)
                seen = len(c.sound)
                await asyncio.sleep(0.5)
                self.assertEqual(len(c.sound), seen, "SOUND kept arriving after it was switched off")

                c.audio_control(sound=False)                                  # off again: still answered
                self.assertEqual(await c.wait_audio_config(3), {"sound": None, "microphone": None})
                c.audio_control()                                             # no fields at all means both off
                self.assertEqual(await c.wait_audio_config(4), {"sound": None, "microphone": None})

                c.audio_control(sound=True)                                   # and it can come back
                self.assertEqual((await c.wait_audio_config(5))["sound"], SOUND_FORMAT)
                await c.wait_sound(20)
                value = c.ping()
                await asyncio.sleep(0.2)
                self.assertIn(value, c.pongs)
            finally:
                await c.close()

    async def test_sound_is_independent_of_the_video_stream_messages(self):
        """STOP and KEYFRAME for a video stream that is not running do not disturb sound."""
        async with helpers.ServerHarness(sound_source=self.sink.monitor, mic_name=helpers.audio_test_name("mic")) as h:
            c, _ = await h.paired_client()
            try:
                c.audio_control(sound=True)
                await c.wait_audio_config()
                await c.wait_sound(20)
                c.stop()
                c.request_keyframe()
                await c.wait_sound(40)
                self.assertIsNotNone(h.server.active.sound)
            finally:
                await c.close()

    async def test_sound_stops_when_the_client_disconnects_or_is_replaced(self):
        async with helpers.ServerHarness(sound_source=self.sink.monitor, mic_name=helpers.audio_test_name("mic")) as h:
            first, creds = await h.paired_client()
            first.audio_control(sound=True)
            await first.wait_audio_config()
            await first.wait_sound(10)
            old_session = h.server.active
            capture = old_session.sound

            second = await h.device_client(creds)                             # the same device connects again
            try:
                await asyncio.wait_for(first.eof.wait(), 5.0)
                self.assertEqual(first.bye, {"reason": "replaced"})
                self.assertIsNone(old_session.sound)
                self.assertFalse(capture.running if hasattr(capture, "running") else capture._pipeline is not None)
                self.assertIsNone(h.server.active.sound)                      # audio is per connection: off until asked
                second.audio_control(sound=True)
                await second.wait_audio_config()
                await second.wait_sound(10)
                new_session = h.server.active
            finally:
                await first.close()
                await second.close()
            for _ in range(50):
                if h.server.active is None:
                    break
                await asyncio.sleep(0.1)
            self.assertIsNone(h.server.active)
            self.assertIsNone(new_session.sound)

    async def test_capture_error_is_reported_and_sound_can_be_asked_for_again(self):
        async with helpers.ServerHarness(sound_source=self.sink.monitor, mic_name=helpers.audio_test_name("mic")) as h:
            c, _ = await h.paired_client()
            try:
                c.audio_control(sound=True)
                await c.wait_audio_config()
                await c.wait_sound(10)
                session = h.server.active
                session._on_sound_error(session._sound_gen, "device went away")     # what the bus watcher would report
                config = await c.wait_audio_config(1)
                self.assertIsNone(config["sound"])
                self.assertIn("device went away", config["problem"])
                self.assertIsNone(session.sound)
                c.audio_control(sound=True)
                self.assertEqual((await c.wait_audio_config(2))["sound"], SOUND_FORMAT)
                await c.wait_sound(10)
            finally:
                await c.close()

    async def test_a_source_that_does_not_exist_is_a_problem_not_a_crash(self):
        async with helpers.ServerHarness(sound_source=helpers.audio_test_name("nosuch") + ".monitor",
                                         mic_name=helpers.audio_test_name("mic")) as h:
            c, _ = await h.paired_client()
            try:
                c.audio_control(sound=True)
                config = await c.wait_audio_config(timeout=15.0)
                self.assertIsNone(config["sound"])
                self.assertIn("does not exist", config["problem"])
                self.assertIsNone(h.server.active.sound)
                value = c.ping()
                await asyncio.sleep(0.2)
                self.assertIn(value, c.pongs)
            finally:
                await c.close()


@NEEDS_AUDIO
class SoundCaptureTests(unittest.TestCase):
    def test_source_exists(self):
        with helpers.NullSink() as sink:
            self.assertTrue(audio.source_exists(sink.monitor))
            self.assertFalse(audio.source_exists(sink.name + "_nope.monitor"))
        self.assertFalse(audio.source_exists(sink.monitor))                   # unloaded again
        self.assertTrue(audio.source_exists("@DEFAULT_MONITOR@"))             # resolved by the server

    def test_a_stream_that_delivers_nothing_is_reported(self):
        """An audio server may accept a capture stream that never produces data (its
        device vanished). That must surface as an error, not as silence forever."""
        errors = []
        with helpers.NullSink() as sink:
            capture = audio.SoundCapture(device=sink.monitor, on_chunk=lambda pts, pcm: None, on_error=errors.append)
            capture._data_timeout = 0.6
            with mock.patch.object(capture, "_has_data", return_value=False):   # as if nothing ever arrived
                capture.start()
                try:
                    deadline = time.monotonic() + 5.0
                    while not errors and time.monotonic() < deadline:
                        time.sleep(0.05)
                finally:
                    capture.stop()
        self.assertEqual(errors, ["no audio is arriving from the audio server"])

    def test_a_healthy_stream_reports_no_error(self):
        errors, got = [], []
        with helpers.NullSink() as sink:
            capture = audio.SoundCapture(device=sink.monitor, on_chunk=lambda pts, pcm: got.append(len(pcm)),
                                         on_error=errors.append)
            capture._data_timeout = 0.6
            capture.start()
            try:
                time.sleep(1.2)
            finally:
                capture.stop()
        self.assertEqual(errors, [])
        self.assertGreater(len(got), 50)                                      # an idle sink still delivers silence


@NEEDS_AUDIO
class DefaultMonitorTests(unittest.TestCase):
    def test_default_output_monitor_can_be_captured(self):
        """The real default: read-only, a fraction of a second, nothing is played."""
        got = []
        capture = audio.SoundCapture(device=audio.DEFAULT_SOUND_SOURCE, on_chunk=lambda pts, pcm: got.append(len(pcm)))
        capture.start()
        try:
            deadline = time.monotonic() + 5.0
            while len(got) < 20 and time.monotonic() < deadline:
                time.sleep(0.05)
        finally:
            capture.stop()
        self.assertGreaterEqual(len(got), 20, "no audio arrived from the default output's monitor")
        self.assertTrue(all(size % 4 == 0 and 0 < size <= 1920 for size in got))


@NEEDS_AUDIO
class MicrophoneTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.mic_name = helpers.audio_test_name("mic")
        self.sink = helpers.NullSink()
        self.sink.__enter__()
        self.addCleanup(self.sink.__exit__)

    def harness(self, **kwargs):
        return helpers.ServerHarness(sound_source=self.sink.monitor, mic_name=self.mic_name, **kwargs)

    def assertNoMicrophone(self):
        self.assertEqual(helpers.audio_modules(self.mic_name), [])
        self.assertIsNone(helpers.source_description(self.mic_name))

    async def _send_tone(self, client, seconds: float, freq: float = 1000.0) -> None:
        start = time.monotonic()
        for index in range(int(seconds * 100)):                                # 10 ms chunks at real-time pace
            client.send_mic(helpers.sine_pcm(freq, 480, start=index * 480))
            delay = start + (index + 1) * 0.01 - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        await client.drain()

    async def test_what_the_phone_sends_comes_out_of_the_foldtop_microphone(self):
        loop = asyncio.get_running_loop()
        async with self.harness() as h:
            c, _ = await h.paired_client()
            try:
                self.assertNoMicrophone()
                c.audio_control(microphone=True)
                self.assertEqual(await c.wait_audio_config(), {"sound": None, "microphone": MIC_FORMAT})
                self.assertEqual(helpers.source_description(self.mic_name), "Foldtop Microphone")
                self.assertEqual(len(helpers.audio_modules(self.mic_name)), 2)       # the sink and the source

                recording = loop.run_in_executor(None, helpers.record_source, self.mic_name, 2.4)
                await asyncio.sleep(0.5)
                await self._send_tone(c, 1.2)
                recorded = helpers.samples_of(await recording)
                loud = helpers.loud_span(recorded)
                self.assertGreater(len(loud), 0.9 * 48000, f"only {len(loud) / 48000:.2f} s of the 1.2 s came through")
                self.assertLess(len(loud), 1.5 * 48000)
                level = helpers.rms(loud)                                     # 8000 / sqrt(2) ≈ 5657
                self.assertTrue(4200 <= level <= 6500, f"level {level:.0f}")
                frequency = helpers.zero_crossing_frequency(loud)
                self.assertTrue(900 <= frequency <= 1100, f"{frequency:.0f} Hz")
                self.assertGreaterEqual(h.server.active.mic.pushed, 110)
            finally:
                await c.close()
        self.assertNoMicrophone()

    async def test_microphone_off_removes_the_device(self):
        async with self.harness() as h:
            c, _ = await h.paired_client()
            try:
                c.audio_control(microphone=True)
                await c.wait_audio_config()
                session = h.server.active
                mic = session.mic
                c.audio_control(microphone=True)                              # idempotent
                self.assertEqual(await c.wait_audio_config(1), {"sound": None, "microphone": MIC_FORMAT})
                self.assertIs(session.mic, mic)
                self.assertEqual(len(helpers.audio_modules(self.mic_name)), 2)

                c.audio_control(microphone=False)
                self.assertEqual(await c.wait_audio_config(2), {"sound": None, "microphone": None})
                self.assertIsNone(session.mic)
                self.assertNoMicrophone()
                self.assertFalse((h.dir / "audio-modules.json").exists())

                c.audio_control(microphone=True)                              # and back on
                self.assertEqual((await c.wait_audio_config(3))["microphone"], MIC_FORMAT)
                self.assertEqual(helpers.source_description(self.mic_name), "Foldtop Microphone")
            finally:
                await c.close()

    async def test_device_is_removed_when_the_client_vanishes(self):
        async with self.harness() as h:
            c, _ = await h.paired_client()
            c.audio_control(microphone=True)
            await c.wait_audio_config()
            self.assertEqual(len(helpers.audio_modules(self.mic_name)), 2)
            c.writer.transport.abort()                                        # no BYE: the phone just disappears
            for _ in range(60):
                if h.server.active is None and not helpers.audio_modules(self.mic_name):
                    break
                await asyncio.sleep(0.1)
            self.assertIsNone(h.server.active)
            self.assertNoMicrophone()
            await c.close()

    async def test_device_is_removed_when_the_client_is_replaced(self):
        async with self.harness() as h:
            first, creds = await h.paired_client()
            first.audio_control(microphone=True)
            await first.wait_audio_config()
            second = await h.device_client(creds)
            try:
                await asyncio.wait_for(first.eof.wait(), 5.0)
                self.assertNoMicrophone()                                     # gone before the new client is let in
                second.audio_control(microphone=True)                         # and the new client can have it
                self.assertEqual((await second.wait_audio_config())["microphone"], MIC_FORMAT)
            finally:
                await first.close()
                await second.close()

    async def test_device_is_removed_when_the_daemon_stops(self):
        async with self.harness() as h:
            c, _ = await h.paired_client()
            c.audio_control(sound=True, microphone=True)
            config = await c.wait_audio_config()
            self.assertEqual(config, {"sound": SOUND_FORMAT, "microphone": MIC_FORMAT})
            self.assertEqual(len(helpers.audio_modules(self.mic_name)), 2)
        # the harness has closed the server with the client still connected
        self.assertNoMicrophone()
        await c.close()

    async def test_mic_while_off_and_malformed_mic_are_ignored(self):
        async with self.harness() as h:
            c, _ = await h.paired_client()
            try:
                c.send_mic(helpers.sine_pcm(1000, 480))                       # the microphone is off: ignored
                value = c.ping()
                await asyncio.sleep(0.2)
                self.assertIn(value, c.pongs)
                self.assertNoMicrophone()

                c.audio_control(microphone=True)
                await c.wait_audio_config()
                mic = h.server.active.mic
                c.send_mic(b"\x00" * 961)                                     # half a sample
                c.send_mic(b"\x00" * 9602)                                    # more than 100 ms
                c.send_mic(b"")                                               # nothing
                c.send_mic(helpers.sine_pcm(1000, 480))                       # a good one
                value = c.ping()
                await asyncio.sleep(0.3)
                self.assertIn(value, c.pongs)
                self.assertEqual((mic.rejected, mic.pushed), (3, 1))
                self.assertIsNone(c.bye)
            finally:
                await c.close()

    async def test_a_killed_daemon_is_cleaned_up_by_the_next_one(self):
        loop = asyncio.get_running_loop()
        with tempfile.TemporaryDirectory(prefix="foldtop-audio-") as tmp:
            crashed = audio.AudioManager(state_dir=Path(tmp), mic_name=self.mic_name)
            mic = crashed.new_microphone()
            await loop.run_in_executor(None, mic.start)
            self.assertEqual(len(helpers.audio_modules(self.mic_name)), 2)
            await loop.run_in_executor(None, mic._halt)                       # the process dies: no unload happens
            self.assertEqual(len(helpers.audio_modules(self.mic_name)), 2)
            self.assertTrue((Path(tmp) / "audio-modules.json").exists())

            restarted = audio.AudioManager(state_dir=Path(tmp), mic_name=self.mic_name)
            removed = await loop.run_in_executor(None, restarted.cleanup_stale)
            self.assertEqual(removed, 2)
            self.assertNoMicrophone()
            self.assertFalse((Path(tmp) / "audio-modules.json").exists())

    async def test_starting_the_daemon_removes_what_a_killed_one_left(self):
        loop = asyncio.get_running_loop()
        h = self.harness()
        crashed = audio.AudioManager(state_dir=h.dir, mic_name=self.mic_name)
        mic = crashed.new_microphone()
        await loop.run_in_executor(None, mic.start)
        await loop.run_in_executor(None, mic._halt)
        self.assertEqual(len(helpers.audio_modules(self.mic_name)), 2)
        async with h:
            self.assertNoMicrophone()                                         # FoldtopServer.start() cleaned up
            c, _ = await h.paired_client()
            try:
                c.audio_control(microphone=True)
                self.assertEqual((await c.wait_audio_config())["microphone"], MIC_FORMAT)
            finally:
                await c.close()

    async def test_default_devices_are_left_alone(self):
        before = (helpers.run_pactl("get-default-sink"), helpers.run_pactl("get-default-source"))
        async with self.harness() as h:
            c, _ = await h.paired_client()
            try:
                c.audio_control(sound=True, microphone=True)
                await c.wait_audio_config()
                await asyncio.sleep(0.5)
                self.assertEqual((helpers.run_pactl("get-default-sink"), helpers.run_pactl("get-default-source")), before)
            finally:
                await c.close()
        self.assertEqual((helpers.run_pactl("get-default-sink"), helpers.run_pactl("get-default-source")), before)


class AudioDisabledTests(unittest.IsolatedAsyncioTestCase):
    """A host with audio switched off says so and ignores requests gracefully. Needs no audio server."""

    async def test_disabled_host(self):
        mic_name = helpers.audio_test_name("mic")
        async with helpers.ServerHarness(audio=False, mic_name=mic_name) as h:
            c, _ = await h.paired_client()
            try:
                self.assertEqual(c.auth_ok["audio"], {"sound": False, "microphone": False})
                c.audio_control(sound=True, microphone=True)
                config = await c.wait_audio_config()
                self.assertIsNone(config["sound"])
                self.assertIsNone(config["microphone"])
                self.assertIn("switched off", config["problem"])
                session = h.server.active
                self.assertIsNone(session.sound)
                self.assertIsNone(session.mic)
                c.send_mic(b"\x00" * 960)                                     # ignored
                c.audio_control(sound=False, microphone=False)                # nothing asked for: no problem reported
                self.assertEqual(await c.wait_audio_config(1), {"sound": None, "microphone": None})
                value = c.ping()
                await asyncio.sleep(0.2)
                self.assertIn(value, c.pongs)
                self.assertIsNone(c.bye)
            finally:
                await c.close()
        if helpers.HAVE_AUDIO:
            self.assertEqual(helpers.audio_modules(mic_name), [])

    async def test_host_without_an_audio_server(self):
        unavailable = audio.Capabilities(False, False, "no audio server (Connection refused)",
                                         "no audio server (Connection refused)")
        async with helpers.ServerHarness(mic_name=helpers.audio_test_name("mic")) as h:
            with mock.patch.object(h.server.audio, "capabilities", return_value=unavailable):
                c, _ = await h.paired_client()
                try:
                    self.assertEqual(c.auth_ok["audio"], {"sound": False, "microphone": False})
                    c.audio_control(sound=True)
                    config = await c.wait_audio_config()
                    self.assertEqual(config["sound"], None)
                    self.assertEqual(config["problem"], "no audio server (Connection refused)")
                    c.audio_control(microphone=True)
                    config = await c.wait_audio_config(1)
                    self.assertEqual((config["microphone"], config["problem"]),
                                     (None, "no audio server (Connection refused)"))
                finally:
                    await c.close()

    async def test_audio_control_must_be_a_json_object(self):
        async with helpers.ServerHarness(audio=False) as h:
            c, _ = await h.paired_client()
            try:
                c.send_raw(Msg.AUDIO_CONTROL, b"not json")
                await asyncio.wait_for(c.eof.wait(), 5.0)
                self.assertEqual(c.bye, {"reason": "protocol"})
            finally:
                await c.close()

    async def test_audio_messages_before_authentication_do_nothing(self):
        from foldtop_host.refclient import RefClient

        async with helpers.ServerHarness(audio=False) as h:
            c = RefClient("127.0.0.1", h.port)
            await c.connect(h.fp)
            try:
                c.send_raw(Msg.AUDIO_CONTROL, P.json_bytes({"sound": True, "microphone": True}))
                await c.drain()
                await asyncio.sleep(0.3)
                self.assertIsNone(h.server.active)
            finally:
                await c.close()


@NEEDS_AUDIO_AND_STREAM
class SoundWithVideoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sink = helpers.NullSink()
        self.sink.__enter__()
        self.addCleanup(self.sink.__exit__)

    async def test_sound_runs_across_video_start_reconfigure_and_stop(self):
        with helpers.Tone(self.sink.name):
            async with helpers.ServerHarness(test_source=helpers.SMPTE_SOURCE, sound_source=self.sink.monitor,
                                             mic_name=helpers.audio_test_name("mic")) as h:
                c, _ = await h.paired_client()
                try:
                    c.audio_control(sound=True)
                    await c.wait_audio_config()
                    await c.wait_sound(20)
                    capture = h.server.active.sound

                    c.start(max_width=640, max_height=360, fps=30, bitrate_kbps=2000, cursor="video")
                    await c.wait_config()
                    frames = await c.collect(15)
                    self.assertTrue(frames[0].keyframe)
                    await c.wait_sound(30)

                    c.start(max_width=320, max_height=180, fps=30, bitrate_kbps=1000, cursor="video")     # reconfigure
                    await c.wait_config(epoch_after=0)
                    await c.collect(10)
                    await c.wait_sound(30)

                    c.stop()
                    await asyncio.sleep(0.3)
                    await c.wait_sound(30)                                    # video stopped, sound did not
                    self.assertIs(h.server.active.sound, capture)             # the same capture all along
                    self.assertEqual(len(c.audio_configs), 1)
                finally:
                    await c.close()

    async def test_stalled_client_drops_sound_and_video_recovers(self):
        """The phone stops reading. Sound must be dropped rather than queued behind the
        video, and when the link recovers both carry on: video from a keyframe that
        decodes, sound from whatever is playing now."""
        with helpers.Tone(self.sink.name):
            async with helpers.ServerHarness(test_source=helpers.SNOW_SOURCE, sound_source=self.sink.monitor,
                                             mic_name=helpers.audio_test_name("mic")) as h:
                c, _ = await h.paired_client()
                try:
                    c.audio_control(sound=True)
                    await c.wait_audio_config()
                    c.start(max_width=640, max_height=360, fps=30, bitrate_kbps=20000, cursor="video")
                    await c.wait_config()
                    await c.collect(15)
                    await c.wait_sound(30)
                    session = h.server.active
                    self.assertEqual(session.sound_dropped, 0)

                    transport = c.writer.transport
                    transport.pause_reading()
                    stalled = False
                    for _ in range(200):                                      # up to 20 s
                        await asyncio.sleep(0.1)
                        if session.frames_dropped > 10 and session.sound_dropped > 10:
                            stalled = True
                            break
                    self.assertTrue(stalled, "the host never started dropping sound while the client was stalled")
                    backlog = session._buffered()
                    await asyncio.sleep(1.0)                                  # a second more of stall …
                    # … adds at most what was already in flight, not a second of audio (192 kB) plus video
                    self.assertLess(session._buffered(), backlog + 64 * 1024)
                    self.assertLess(session._buffered(), 2 * 20000 * 1000 // 8)
                    sent_while_stalled = session.sound_sent
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
                    self.assertIsNotNone(resumed_at, "no gap seen in the received video")
                    self.assertTrue(frames[resumed_at].keyframe, "video resumed on a delta frame")
                    tail = await helpers.adecode_h264([f.data for f in frames[resumed_at:]])
                    self.assertEqual(len(tail), len(frames) - resumed_at)

                    before = len(c.sound)
                    fresh = await c.wait_sound(50)                            # sound is flowing again
                    self.assertGreater(len(c.sound), before)
                    self.assertGreater(session.sound_sent, sent_while_stalled)
                    now = time.monotonic_ns() // 1000
                    self.assertLess(now - fresh[-1].pts_micros, 1_500_000, "sound resumed far behind real time")
                    self.assertIsNone(c.bye)
                    self.assertIsNotNone(session.sound)
                finally:
                    await c.close()


if __name__ == "__main__":
    unittest.main()
