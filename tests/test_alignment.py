"""Alignment evidence must not turn repeated hash collisions into certainty."""
from pathlib import Path
import tempfile
import unittest

from vjvision.alignment import aligned_candidates


class AlignmentTests(unittest.TestCase):
    def test_repeated_reference_occurrences_count_query_once(self):
        hashes = [("abc", 1), ("abc", 2), ("def", 30)]
        rows = [("ABC", 1, offset) for offset in range(1, 500)]
        result = aligned_candidates(hashes, rows, .1)[0]
        self.assertEqual(result["count"], 2)
        self.assertAlmostEqual(result["ratio"], 2 / 3)
        self.assertAlmostEqual(result["span"], .1)

    def test_adjacent_bins_combine_but_unrelated_positions_do_not(self):
        hashes = [("a", 0), ("b", 10), ("c", 20), ("d", 30)]
        rows = [("A", 7, 100), ("B", 7, 111), ("C", 7, 120), ("D", 7, 500)]
        result = aligned_candidates(hashes, rows, .1)[0]
        self.assertEqual(result["count"], 3)
        self.assertEqual(result["ratio"], .75)
        self.assertEqual(result["span"], 2)
        self.assertEqual(result["offset_seconds"], 10)

    def test_scores_separate_songs_and_normalize_hash_case(self):
        candidates = aligned_candidates([("a", 0), ("b", 10)],
            [("A", 2, 100), ("B", 2, 110), ("a", 1, 300)], .1)
        self.assertEqual([c["song_id"] for c in candidates], [2, 1])
        self.assertEqual([c["count"] for c in candidates], [2, 1])
        self.assertEqual(aligned_candidates([], [], .1), [])

    def test_sql_query_preserves_legacy_votes_and_reports_unique_support(self):
        from vjvision.fingerprint import FingerprintDB
        FingerprintDB._install_audioop_shim()
        from vjvision.dejavu_sqlite import SQLiteDatabase

        with tempfile.TemporaryDirectory() as folder:
            db = SQLiteDatabase(path=str(Path(folder) / "test.sqlite"))
            db.setup()
            sid = db.insert_song("track", "ABCD", 4)
            db.insert_hashes(sid, [("aa", 10), ("aa", 11), ("bb", 20), ("aa", 100)])
            hashes = [("aa", 0), ("aa", 1), ("bb", 10)]
            legacy_matches, legacy_counts = db.return_matches(hashes)
            matches, counts, evidence = db.return_matches_with_alignment(hashes, .1)
            self.assertCountEqual(matches, legacy_matches)
            self.assertEqual(counts, legacy_counts)
            self.assertEqual(evidence[0]["count"], 3)
            self.assertEqual(evidence[0]["ratio"], 1)
            self.assertEqual(db.return_matches_with_alignment([], .1), ([], {}, []))


if __name__ == "__main__":
    unittest.main()
