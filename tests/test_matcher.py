"""Exercise track transitions without an audio device or a fingerprint database."""
import queue
import unittest
from unittest.mock import Mock, patch

import numpy as np

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
        self.matcher._capture.snapshot.return_value = np.zeros((12 * 44100, 2), dtype=np.float32)
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

    def test_conflicting_weak_result_breaks_new_track_evidence(self):
        for song in ("A", "C"):
            with self.subTest(weak_song=song):
                self.matcher._current_track_path = "A"
                self.observe("A")
                self.observe("B")
                self.observe(song, 0.15)
                self.observe("B")
                self.assertEqual(self.matcher._current_track_path, "A")
                self.observe("B")
                self.assertEqual(self.matcher._current_track_path, "B")

    def test_same_candidate_weak_hit_does_not_vote_but_preserves_recent_evidence(self):
        self.observe("B")
        self.observe("B", .15)
        self.assertEqual(self.matcher._current_track_path, "A")
        self.assertEqual(self.matcher._pending_hits, 1)
        self.observe("B")
        self.assertEqual(self.matcher._current_track_path, "B")

    def test_weak_hits_cannot_extend_evidence_expiry(self):
        with patch("vjvision.matcher.time.monotonic", return_value=100):
            self.observe("B")
        with patch("vjvision.matcher.time.monotonic", return_value=103):
            self.observe("B", .15)
        with patch("vjvision.matcher.time.monotonic", return_value=105):
            self.observe("B")
        self.assertEqual(self.matcher._current_track_path, "A")
        self.assertEqual(self.matcher._pending_hits, 1)

    def test_noise_on_same_candidate_clears_evidence(self):
        self.observe("B")
        self.observe("B", .02)
        self.observe("B")
        self.assertEqual(self.matcher._current_track_path, "A")

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
        replacement = Mock(sr=44100)
        replacement.snapshot.return_value = np.zeros((12 * 44100, 2), dtype=np.float32)
        with patch("vjvision.matcher.AudioCapture", return_value=replacement):
            self.matcher._reconfigure_capture(7)
        self.observe("B")
        self.assertEqual(self.matcher._current_track_path, "A")
        self.observe("B")
        self.assertEqual(self.matcher._current_track_path, "B")


class RecognitionWindowTests(unittest.TestCase):
    def setUp(self):
        self.config = CaptureConfig()
        settings_patch = patch("vjvision.matcher.SETTINGS.capture", self.config)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.matcher = MatcherThread(queue.Queue(), queue.Queue(), queue.Queue())
        self.matcher._current_track_path = "A"
        self.matcher._fp = Mock()
        self.samples = np.ones((120, 2), dtype=np.float32)

    @staticmethod
    def result(path, confidence):
        return MatchResult(bool(path), path, confidence, 0.0, 1, {})

    def test_accepted_current_track_skips_long_window(self):
        self.matcher._fp.match_from_array.return_value = self.result("A", 0.30)
        result = self.matcher._match_snapshot(self.samples, 10)
        self.assertEqual(result.file_path, "A")
        self.assertEqual(self.matcher._fp.match_from_array.call_count, 1)
        self.assertEqual(len(self.matcher._fp.match_from_array.call_args.args[0]), 60)

    def aligned_result(self, **overrides):
        evidence = dict(song_id=1, count=40, ratio=.3, span=3, offset_seconds=50)
        evidence.update(overrides)
        return MatchResult(True, "B", .5, 50, 1, {"aligned_candidates": [evidence]})

    def test_unique_aligned_recent_evidence_skips_long_window(self):
        self.matcher._fp.match_from_array.return_value = self.aligned_result()
        result = self.matcher._match_snapshot(self.samples, 10)
        self.assertEqual(result.raw["recent_position"], 56)
        self.assertEqual(self.matcher._fp.match_from_array.call_count, 1)

    def test_weak_sparse_or_competing_alignment_still_needs_long_window(self):
        for change in [dict(count=19), dict(ratio=.19), dict(span=1.9), dict(song_id=2)]:
            with self.subTest(change=change):
                self.matcher._fp.match_from_array.reset_mock()
                self.matcher._fp.match_from_array.side_effect = [self.aligned_result(**change), self.result("A", .5)]
                result = self.matcher._match_snapshot(self.samples, 10)
                self.assertFalse(result.raw.get("window_agrees", True))
                self.assertEqual(self.matcher._fp.match_from_array.call_count, 2)
        competing = self.aligned_result()
        competing.raw["aligned_candidates"].append(dict(song_id=2, count=30))
        self.matcher._fp.match_from_array.side_effect = [competing, self.result("A", .5)]
        self.assertFalse(self.matcher._match_snapshot(self.samples, 10).raw["window_agrees"])

    def test_recent_confirmation_requires_continuous_playback_position(self):
        self.matcher._capture_running = True
        self.matcher._capture = Mock(sr=10)
        self.matcher._capture.snapshot.return_value = self.samples
        self.matcher._ensure_fp = Mock()
        with patch("vjvision.matcher.time.monotonic", return_value=100):
            self.matcher._fp.match_from_array.return_value = self.aligned_result()
            self.matcher._run_match()
        with patch("vjvision.matcher.time.monotonic", return_value=102):
            self.matcher._fp.match_from_array.return_value = self.aligned_result(offset_seconds=80)
            self.matcher._run_match()
        self.assertEqual(self.matcher._current_track_path, "A")
        self.assertEqual(self.matcher._pending_hits, 1)
        with patch("vjvision.matcher.time.monotonic", return_value=104), patch(
                "vjvision.matcher.extract_track", return_value=Track("B", "B", "", "", None)):
            self.matcher._fp.match_from_array.return_value = self.aligned_result(offset_seconds=82)
            self.matcher._run_match()
        self.assertEqual(self.matcher._current_track_path, "B")
        self.assertIsNone(self.matcher._pending_position)

    def test_strong_secondary_deck_needs_long_window_support(self):
        self.matcher._capture_running = True
        self.matcher._capture = Mock(sr=10)
        self.matcher._capture.snapshot.return_value = self.samples
        self.matcher._fp.match_from_array.side_effect = [self.result("B", .50), self.result("A", .60)] * 3
        with patch("vjvision.matcher.extract_track", return_value=Track("A", "A", "", "", None)):
            for _ in range(3):
                self.matcher._run_match()
        self.assertEqual(self.matcher._current_track_path, "A")
        self.assertEqual(self.matcher._pending_hits, 0)
        self.assertTrue(self.matcher._in_mix)

    def test_strong_short_hit_does_not_promote_weak_long_window(self):
        self.matcher._fp.match_from_array.side_effect = [self.result("B", .70), self.result("B", .20)]
        result = self.matcher._match_snapshot(self.samples, 10)
        self.assertEqual((result.file_path, result.confidence), ("B", .20))

    def test_long_window_supports_same_weak_candidate(self):
        self.matcher._fp.match_from_array.side_effect = [self.result("B", .15), self.result("B", .50)]
        result = self.matcher._match_snapshot(self.samples, 10)
        self.assertEqual(result.confidence, .50)
        self.assertEqual([len(c.args[0]) for c in self.matcher._fp.match_from_array.call_args_list], [60, 120])

    def test_conflicting_old_track_does_not_override_recent_candidate(self):
        self.matcher._fp.match_from_array.side_effect = [self.result("B", .15), self.result("A", .80)]
        result = self.matcher._match_snapshot(self.samples, 10)
        self.assertEqual((result.file_path, result.confidence), ("B", .15))

    def test_long_window_can_recover_missing_short_candidate(self):
        self.matcher._fp.match_from_array.side_effect = [self.result(None, 0), self.result("B", .40)]
        self.assertEqual(self.matcher._match_snapshot(self.samples, 10).file_path, "B")

    def test_silence_and_short_buffers_skip_duplicate_query(self):
        for samples in (np.zeros_like(self.samples), self.samples[-60:]):
            with self.subTest(frames=len(samples)):
                self.matcher._fp.match_from_array.reset_mock()
                self.matcher._fp.match_from_array.return_value = self.result(None, 0)
                self.matcher._match_snapshot(samples, 10)
                self.assertEqual(self.matcher._fp.match_from_array.call_count, 1)

    def test_fallback_is_only_one_confirmation_per_snapshot(self):
        self.matcher._capture_running = True
        self.matcher._capture = Mock(sr=10)
        self.matcher._capture.snapshot.return_value = self.samples
        self.matcher._fp.match_from_array.side_effect = [self.result("B", .15), self.result("B", .50)]
        with patch("vjvision.matcher.extract_track", return_value=Track("A", "A", "", "", None)):
            self.matcher._run_match()
        self.assertEqual(self.matcher._current_track_path, "A")
        self.assertEqual(self.matcher._pending_hits, 1)

    def test_initial_track_keeps_lower_acceptance_threshold(self):
        self.matcher._current_track_path = None
        self.matcher._fp.match_from_array.return_value = self.result("B", .25)
        self.matcher._match_snapshot(self.samples, 10)
        self.assertEqual(self.matcher._fp.match_from_array.call_count, 1)

    def test_new_mix_uses_fast_deadline_immediately(self):
        with patch("vjvision.matcher.time.monotonic", return_value=100), patch("vjvision.matcher.random.uniform", return_value=1):
            self.matcher._schedule_next_match(100)
            self.assertEqual(self.matcher._next_match_ts, 102)
            self.matcher._in_mix = True
            self.matcher._schedule_next_match(100)
            self.assertEqual(self.matcher._next_match_ts, 101.25)

    def test_slow_match_leaves_time_for_control_loop(self):
        with patch("vjvision.matcher.time.monotonic", return_value=105), patch("vjvision.matcher.random.uniform", return_value=1):
            self.matcher._schedule_next_match(100)
        self.assertAlmostEqual(self.matcher._next_match_ts, 105.05)

    def test_scheduling_enforces_new_audio_and_resets_on_stop(self):
        self.config.match_candidate_interval = .1
        self.matcher._in_mix = True
        with patch("vjvision.matcher.time.monotonic", return_value=100), patch("vjvision.matcher.random.uniform", return_value=.1):
            self.matcher._schedule_next_match(100)
            self.assertEqual(self.matcher._next_match_ts, 101)
        with patch("vjvision.matcher.extract_track", return_value=Track("A", "A", "", "", None)):
            self.matcher._stop_capture()
        self.assertEqual(self.matcher._next_match_ts, 0)


if __name__ == "__main__":
    unittest.main()
