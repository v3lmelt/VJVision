"""Validate the benchmark's verdict conversion and parity checks without native dependencies."""
import unittest
from tools.benchmark_olaf_application import assert_parity, group_hits, make_hit, verdict


class OlafBenchmarkTests(unittest.TestCase):
    def hit(self, sid=1, count=20, span=2):
        return dict(song_id=sid, count=count, span=span, ratio=.3, offset_seconds=30)

    def test_native_repeated_offsets_do_not_manufacture_a_runner_up(self):
        hits = group_hits([self.hit(count=40), self.hit(count=30), self.hit(sid=2, count=10)])
        self.assertEqual([h["count"] for h in hits], [40, 10])
        self.assertTrue(verdict(hits, {1: "A", 2: "B"}).raw["accepted_verdict"])

    def test_verdict_is_categorical_and_requires_all_three_conditions(self):
        paths = {1: "A", 2: "B"}
        self.assertEqual(verdict([self.hit()], paths).confidence, .30)
        cases = [[self.hit(count=19)], [self.hit(span=1.99)],
                 [self.hit(count=20), self.hit(sid=2, count=11)]]
        for hits in cases:
            with self.subTest(hits=hits):
                result = verdict(hits, paths)
                self.assertTrue(result.matched)
                self.assertEqual(result.confidence, .06)
                self.assertFalse(result.raw["accepted_verdict"])
        self.assertFalse(verdict([], paths).matched)

    def test_cli_rounding_is_tolerated_but_wrong_counts_are_rejected(self):
        a = self.hit()
        b = {**a, "span": 2.009, "offset_seconds": 30.01}
        assert_parity(([a], 30), ([b], 30))
        for candidate, total in [({**b, "count": 21}, 30), (b, 31), ({**b, "song_id": 2}, 30)]:
            with self.assertRaises(AssertionError):
                assert_parity(([a], 30), ([candidate], total))

    def test_reference_alias_and_offset_conversion_are_explicit(self):
        hit = make_hit(20, 1, 4, "track-12.wav", 71, 74)
        self.assertEqual((hit["song_id"], hit["offset_seconds"], hit["span"]), (12, 70, 3))
        with self.assertRaises(ValueError):
            make_hit(20, 1, 4, "unexpected.wav", 71, 74)


if __name__ == "__main__":
    unittest.main()
