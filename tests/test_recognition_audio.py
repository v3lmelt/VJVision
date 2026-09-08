"""Audio edge cases without device access or changes to a fingerprint store."""
import unittest
from unittest.mock import Mock

import numpy as np

from vjvision.audio_capture import AudioCapture
from vjvision.fingerprint import FingerprintDB


class RecognitionAudioTests(unittest.TestCase):
    def test_fast_erosion_matches_scipy_at_borders_and_on_sparse_masks(self):
        from scipy import ndimage

        rng = np.random.default_rng(9)
        arrays = [np.zeros((31, 17), dtype=bool), np.ones((31, 17), dtype=bool),
                  rng.random((31, 17)) > .03, rng.random((31, 17)) > .8]
        footprints = [np.ones((41, 41), dtype=bool), np.ones((3, 5), dtype=bool),
                      ndimage.generate_binary_structure(2, 1)]
        for data in arrays:
            for footprint in footprints:
                for border in (0, 1):
                    with self.subTest(shape=footprint.shape, border=border):
                        expected = ndimage.binary_erosion(data, structure=footprint, border_value=border)
                        actual = FingerprintDB._erode_peak_background(data, footprint, border)
                        np.testing.assert_array_equal(actual, expected)

    def test_fast_erosion_keeps_generated_fingerprints_identical(self):
        from scipy import ndimage
        from unittest.mock import patch

        FingerprintDB._install_audioop_shim()
        FingerprintDB._optimize_dejavu_params()
        from dejavu.logic import fingerprint

        rng = np.random.default_rng(90)
        samples = (rng.normal(0, 2000, 3 * 44100)).astype(np.int16)
        with patch.object(fingerprint, "binary_erosion", ndimage.binary_erosion):
            before = fingerprint.fingerprint(samples)
        with patch.object(fingerprint, "binary_erosion", FingerprintDB._erode_peak_background):
            after = fingerprint.fingerprint(samples)
        self.assertTrue(before)
        self.assertEqual(before, after)

    def test_silence_and_empty_input_never_fingerprint_or_query(self):
        db = FingerprintDB.__new__(FingerprintDB)
        db._djv = Mock()
        for samples in (np.zeros((12 * 48000, 2), dtype=np.float32), np.empty((0, 2))):
            result = db.match_from_array(samples, input_sr=48000)
            self.assertFalse(result.matched)
        db._djv.generate_fingerprints.assert_not_called()
        db._djv.find_matches.assert_not_called()

    def test_quiet_nonzero_input_still_reaches_fingerprinting(self):
        db = FingerprintDB.__new__(FingerprintDB)
        db._djv = Mock()
        db._djv.generate_fingerprints.return_value = ([], 0)
        db.match_from_array(np.full((44100, 1), 1e-5, dtype=np.float32))
        db._djv.generate_fingerprints.assert_called_once()

    def test_snapshot_uses_position_from_same_locked_buffer_copy(self):
        capture = AudioCapture.__new__(AudioCapture)
        capture._buf = np.arange(12, dtype=np.float32).reshape(6, 2)
        capture._write_pos = 2

        class AdvancingLock:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                # Model a callback advancing the write pointer as soon as
                # the snapshot releases the lock.
                capture._write_pos = 4

        capture._lock = AdvancingLock()
        snapshot = capture.snapshot()
        np.testing.assert_array_equal(snapshot, np.roll(capture._buf, -2, axis=0))
        snapshot[:] = -1
        self.assertTrue(np.all(capture._buf >= 0))


if __name__ == "__main__":
    unittest.main()
