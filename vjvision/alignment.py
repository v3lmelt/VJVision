"""Time-consistent fingerprint evidence without repeated-reference inflation."""
from collections import defaultdict


def aligned_candidates(hashes, rows, seconds_per_frame):
    """Count each query fingerprint once within a three-frame offset band.

    A hash appearing at many reference positions must not manufacture extra
    evidence. Adjacent offset bins tolerate the query's FFT grid phase.
    """
    queries = defaultdict(set)
    for value, position in hashes:
        queries[value.upper()].add(int(position))
    total = sum(len(positions) for positions in queries.values())
    if not total:
        return []
    bands = defaultdict(lambda: defaultdict(set))
    for value, song_id, reference in rows:
        for position in queries.get(value.upper(), ()):
            bands[int(song_id)][int(reference) - position].add((value.upper(), position))
    candidates = []
    for song_id, offsets in bands.items():
        strongest, center = set(), 0
        for offset in offsets:
            support = offsets[offset] | offsets.get(offset - 1, set()) | offsets.get(offset + 1, set())
            if len(support) > len(strongest):
                strongest, center = support, offset
        times = [position for _, position in strongest]
        candidates.append({"song_id": song_id, "count": len(strongest),
                           "ratio": len(strongest) / total,
                           "span": (max(times) - min(times)) * seconds_per_frame,
                           "offset_seconds": center * seconds_per_frame})
    return sorted(candidates, key=lambda c: (-c["count"], c["song_id"]))
