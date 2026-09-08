"""Signal semantics and offscreen layout checks; no audio hardware required."""
import math
from pathlib import Path
import tempfile
import unittest
import wave

import numpy as np
import pygame

from vjvisual.metadata import extract_track
from vjvisual.pastel_visualizer import PastelRenderer, _db
from vjvisual.visualizer import VisualState


class PastelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        pygame.font.init()

    def test_stale_input_decays_and_history_expires(self):
        renderer = PastelRenderer("segoeui")
        screen = pygame.Surface((800, 450))
        state = VisualState()
        renderer.observe(dict(bins=[.6]*48, rms=.1, input_peak=.5,
                              sample_rate=48000, channels=2), now=0)
        renderer.draw(screen, state, now=.1)
        self.assertGreater(renderer.levels.max(), 0)
        self.assertAlmostEqual(float(_db(.1)), -20)
        for i in range(1, 60):
            renderer.draw(screen, state, now=1+i/30)
        self.assertLess(renderer.levels.max(), .001)
        self.assertEqual(renderer.rms, 0)
        renderer.draw(screen, state, now=31)
        self.assertEqual(len(renderer.history), 0)

    def test_empty_spectrum_and_multiple_layouts(self):
        renderer = PastelRenderer("segoeui")
        renderer.observe(dict(bins=[], rms=0), now=0)
        state = VisualState(title="A long title "*40, artist="Artist "*30)
        for size in ((1600, 900), (800, 600), (720, 1280), (2560, 1080)):
            for style in ("bar", "wave", "mirror"):
                state.style = style
                surface = pygame.Surface(size)
                renderer.draw(surface, state, now=.2)
                self.assertEqual(surface.get_size(), size)
        self.assertTrue(np.isfinite(renderer.levels).all())

    def test_wave_file_metadata_uses_actual_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"example.wav"
            with wave.open(str(path), "wb") as audio:
                audio.setnchannels(2)
                audio.setsampwidth(2)
                audio.setframerate(48000)
                audio.writeframes(bytes(48000*4))
            track = extract_track(str(path))
            self.assertEqual(track.details["sample_rate"], 48000)
            self.assertEqual(track.details["bit_depth"], 16)
            self.assertEqual(track.details["channels"], 2)
            self.assertTrue(math.isclose(track.details["duration"], 1))
            self.assertEqual(track.details["bpm"], "")
            self.assertEqual(track.details["format"], "WAV")


if __name__ == "__main__":
    unittest.main()
