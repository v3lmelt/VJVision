"""Keep machine preferences across the upstream package rename."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from vjvision import config


class PreferencesMigrationTests(unittest.TestCase):
    def test_legacy_preferences_are_loaded_without_overwriting_them(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "VJ-Visual" / "prefs.json"
            legacy.parent.mkdir()
            original = json.dumps({"audio_device": 42, "visual": {"theme": "classic"}})
            legacy.write_text(original, encoding="utf-8")
            current = root / "VJVision" / "prefs.json"
            settings = config.Settings()
            with patch.object(config, "PREFS_FILE", current), patch.object(config, "SETTINGS", settings):
                config.load_prefs()
            self.assertEqual(settings.audio_device, 42)
            self.assertEqual(settings.visual.theme, "classic")
            self.assertEqual(legacy.read_text(encoding="utf-8"), original)
            self.assertFalse(current.exists())

    def test_current_preferences_take_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, device in (("VJ-Visual", 42), ("VJVision", 7)):
                path = root / name / "prefs.json"
                path.parent.mkdir()
                path.write_text(json.dumps({"audio_device": device}), encoding="utf-8")
            settings = config.Settings()
            with patch.object(config, "PREFS_FILE", root / "VJVision" / "prefs.json"), patch.object(config, "SETTINGS", settings):
                config.load_prefs()
            self.assertEqual(settings.audio_device, 7)


if __name__ == "__main__":
    unittest.main()
