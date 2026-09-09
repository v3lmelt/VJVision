"""Check native-result conversion without requiring Java or WSL."""
import unittest
import numpy as np
from tools.panako_wsl_client import aligned_hits, PanakoRecognizer, pcm16
from tools.benchmark_panako_application import summarize_result


class PanakoBridgeTests(unittest.TestCase):
    def hit(self, **kwargs):
        return dict(song_id=1, count=40, query_start=1, query_stop=5,
                    ref_start=71, ref_stop=75, time_factor=1,
                    matched_seconds_fraction=.8, **kwargs)

    def test_reference_position_accounts_for_speed(self):
        hit = self.hit()
        hit["time_factor"] = 1/1.06
        result = aligned_hits([hit], 6)[0]
        self.assertAlmostEqual(result["offset_seconds"] + 6, 71 + 5*1.06)
        self.assertEqual(result["span"], 4)
        self.assertEqual(result["ratio"], .8)

    def test_invalid_time_factor_cannot_reach_state_machine(self):
        for factor in [0, -1, float("nan"), float("inf")]:
            hit = self.hit()
            hit["time_factor"] = factor
            with self.assertRaises(ValueError):
                aligned_hits([hit], 6)

    def test_native_coverage_is_not_used_as_confidence_probability(self):
        class Worker:
            def request(self, *args, **kwargs):
                return dict(hits=[dict(song_id=1, count=19, query_start=1, query_stop=5,
                    ref_start=71, time_factor=1, matched_seconds_fraction=1)], server_s=.01, roundtrip_s=.02)
        answer = PanakoRecognizer(Worker(), {1: "A"}).match(np.ones((96000, 2), dtype="float32"), 16000)
        self.assertEqual(answer.confidence, .06)
        self.assertFalse(answer.raw["accepted_verdict"])

    def test_pcm_conversion_and_silence_skip(self):
        audio = np.array([[1, 1], [-1, -1], [0, 0]], dtype="float32")
        np.testing.assert_array_equal(np.frombuffer(pcm16(audio, 16000), dtype="<i2"), [32767,-32767,0])
        answer = PanakoRecognizer(None, {}).match(np.zeros((96000, 2), dtype="float32"), 16000)
        self.assertFalse(answer.matched)

    def test_restored_final_song_does_not_hide_an_intermediate_wrong_switch(self):
        result = {"runs": [dict(variant="test", kind="abort", final_correct=True,
            wrong_switches=0, unexpected_switches=1, premature_switches=0, reverts=1,
            latency=7, recognition_wall_s=.5)]}
        summarize_result(result)
        row = result["summary"]["test"]["abort"]
        self.assertEqual(row["correct"], 1)
        self.assertEqual(row["clean"], 0)


if __name__ == "__main__":
    unittest.main()
