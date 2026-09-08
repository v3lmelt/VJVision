"""Exercise track transitions without an audio device or a fingerprint database."""
import queue
import unittest
from unittest.mock import Mock, patch

from vjvision.config import CaptureConfig
from vjvision.fingerprint import MatchResult
from vjvision.matcher import MatcherThread
from vjvision.metadata import Track


class MatcherTransitionTests(unittest.TestCase):
    def setUp(self):
        self.config = CaptureConfig()
        settings_patch = patch("vjvision.matcher.SETTINGS.capture", self.config)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        metadata_patch = patch(
            "vjvision.matcher.extract_track",
            side_effect=lambda path: Track(path, path, "artist", "album", None),
        )
        metadata_patch.start()
        self.addCleanup(metadata_patch.stop)
        self.matcher = MatcherThread(queue.Queue(), queue.Queue(), queue.Queue())
        self.matcher._capture = Mock(sr=44100)
        self.matcher._capture_running = True
        self.matcher._fp = Mock()
        self.matcher._ensure_fp = Mock()
        self.observe("A")

    def observe(self, song, confidence=0.6):
        result = MatchResult(
            song is not None, song, confidence if song else 0.0, 0.0,
            ord(song) if song else None, {},
        )
        self.matcher._fp.match_from_array.return_value = result
        self.matcher._run_match()

    def tracks(self):
        messages = []
        while not self.matcher.viz.empty():
            message = self.matcher.viz.get_nowait()
            if message["type"] == "track":
                messages.append(message)
        return messages

    def test_startup_keeps_existing_fast_confirmation(self):
        self.assertEqual(self.matcher._current_track_path, "A")
        self.assertEqual(self.tracks()[-1]["title"], "A")

    def test_stable_new_track_switches_once_after_confirmation(self):
        self.tracks()
        self.observe("B")
        self.assertEqual(self.matcher._current_track_path, "A")
        self.observe("B")
        self.assertEqual(self.matcher._current_track_path, "B")
        self.observe("B")
        self.assertEqual([m["title"] for m in self.tracks()].count("B"), 1)

    def test_alternating_tracks_never_replace_current_track(self):
        self.tracks()
        for song in ("B", "A", "B", "A", "C", "B", "A"):
            self.observe(song)
            self.assertEqual(self.matcher._current_track_path, "A")
        self.assertTrue(all(m["title"] == "A" for m in self.tracks()))

    def test_weak_result_breaks_consecutive_new_track_hits(self):
        for song in ("A", "B", "C"):
            with self.subTest(weak_song=song):
                self.matcher._current_track_path = "A"
                self.observe("A")
                self.observe("B")
                self.observe(song, 0.15)
                self.observe("B")
                self.assertEqual(self.matcher._current_track_path, "A")
                self.observe("B")
                self.assertEqual(self.matcher._current_track_path, "B")

    def test_unmatched_or_noise_result_breaks_consecutive_hits(self):
        for song in (None, "C"):
            with self.subTest(interruption=song):
                self.matcher._current_track_path = "A"
                self.observe("A")
                self.observe("B")
                self.observe(song, 0.02)
                self.observe("B")
                self.assertEqual(self.matcher._current_track_path, "A")

    def test_match_failure_breaks_consecutive_hits(self):
        self.observe("B")
        self.matcher._fp.match_from_array.side_effect = RuntimeError("test failure")
        self.matcher._run_match()
        self.matcher._fp.match_from_array.side_effect = None
        self.observe("B")
        self.assertEqual(self.matcher._current_track_path, "A")

    def test_snapshot_failure_breaks_consecutive_hits(self):
        self.observe("B")
        self.matcher._capture.snapshot.side_effect = RuntimeError("test failure")
        self.matcher._run_match()
        self.matcher._capture.snapshot.side_effect = None
        self.observe("B")
        self.assertEqual(self.matcher._current_track_path, "A")

    def test_mixing_respects_configured_confirmation_count(self):
        self.config.match_confirmations = 3
        self.observe("B")
        self.observe("B")
        self.assertEqual(self.matcher._current_track_path, "A")
        self.observe("B")
        self.assertEqual(self.matcher._current_track_path, "B")

    def test_weak_current_track_does_not_end_mix(self):
        self.observe("B")
        self.observe("A", 0.15)
        self.assertTrue(self.matcher._in_mix)
        self.assertTrue(self.tracks()[-1].get("tentative", False))

    def test_current_track_needs_stable_recovery_and_restores_display(self):
        self.observe("B")
        self.observe("A")
        self.assertTrue(self.matcher._in_mix)
        self.observe("A")
        self.assertFalse(self.matcher._in_mix)
        displayed = self.tracks()[-1]
        self.assertEqual(displayed["title"], "A")
        self.assertFalse(displayed.get("tentative", False))

    def test_weak_new_song_never_overwrites_display(self):
        self.tracks()
        for _ in range(5):
            self.observe("B", 0.15)
        self.assertEqual(self.matcher._current_track_path, "A")
        self.assertTrue(all(m["title"] == "A" for m in self.tracks()))

    def test_capture_restart_discards_previous_candidate(self):
        self.observe("B")
        self.matcher._stop_capture()
        self.matcher._start_capture()
        self.observe("B")
        self.assertEqual(self.matcher._current_track_path, "A")

    def test_device_change_discards_previous_candidate(self):
        self.observe("B")
        with patch("vjvision.matcher.AudioCapture", return_value=Mock(sr=44100)):
            self.matcher._reconfigure_capture(7)
        self.observe("B")
        self.assertEqual(self.matcher._current_track_path, "A")


if __name__ == "__main__":
    unittest.main()
