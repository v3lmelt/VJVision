"""Measure recognition on SQLite backups without changing the live library.

Run with the project Python: tools/profile_recognition.py --output cache/recognition-profile.json
Audio decoding and test-signal construction are excluded from match timings.
"""
from __future__ import annotations

import argparse
import cProfile
from contextlib import closing
from dataclasses import asdict
import io
import json
from pathlib import Path
import platform
import pstats
import re
import sqlite3
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from vjvision import fingerprint as fp_module
from vjvision.config import SETTINGS, LOG_FILE


def summary(values):
    values = np.asarray(values, dtype=float)
    if not values.size:
        return {}
    return {"n": len(values), "median": float(np.median(values)),
            "p95": float(np.percentile(values, 95)), "max": float(values.max())}


def backup(source, target):
    src = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def log_summary():
    text = LOG_FILE.read_text(encoding="utf-8", errors="replace") if LOG_FILE.exists() else ""
    rows = re.findall(r"confidence=([\d.]+).*?\[total=([\d.]+)s query=([\d.]+)s align=([\d.]+)s\]", text)
    return {key: summary([float(row[i]) for row in rows])
            for i, key in enumerate(["confidence", "total_s", "query_s", "align_s"])}


def measure(db, samples, sr, **labels):
    # Wrapping existing methods records time even for unsuccessful matches.
    stages = {"fingerprint_s": 0.0, "query_s": 0.0, "align_s": 0.0}
    originals = {}
    for method, key in [("generate_fingerprints", "fingerprint_s"),
                        ("find_matches", "query_s"), ("align_matches", "align_s")]:
        original = getattr(db._djv, method)
        originals[method] = original

        def timed(*args, _fn=original, _key=key, **kwargs):
            start = time.perf_counter()
            try:
                return _fn(*args, **kwargs)
            finally:
                stages[_key] += time.perf_counter() - start

        setattr(db._djv, method, timed)
    start = time.perf_counter()
    cpu_start = time.process_time()
    try:
        result = db.match_from_array(samples, input_sr=sr)
    finally:
        elapsed = time.perf_counter() - start
        cpu_elapsed = time.process_time() - cpu_start
        for method, original in originals.items():
            setattr(db._djv, method, original)
    return {**labels, **stages, "total_s": elapsed, "cpu_s": cpu_elapsed,
            "other_s": elapsed - sum(stages.values()),
            "song_id": result.song_id, "confidence": result.confidence,
            "matched": result.matched, "hashes": result.raw.get("fingerprints_total", 0),
            "candidates": [{"song_id": r["song_id"], "confidence": r["input_confidence"]}
                           for r in result.raw.get("results", [])]}


def auxiliary_measurements():
    """Isolate buffer-copy and exact-silence costs without opening audio devices."""
    from vjvision.audio_capture import AudioCapture
    from dejavu import Dejavu

    fp_module.FingerprintDB._optimize_dejavu_params()
    capture = AudioCapture.__new__(AudioCapture)
    capture._buf = np.zeros((12*48000, 2), dtype=np.float32)
    capture._lock = threading.Lock()
    capture._write_pos = 12345
    copies = []
    for _ in range(30):
        start = time.perf_counter()
        capture.snapshot()
        copies.append(time.perf_counter() - start)
    # Exact silence never queries the database, so no live store is needed.
    db = fp_module.FingerprintDB.__new__(fp_module.FingerprintDB)
    db._djv = Dejavu.__new__(Dejavu)
    silence = np.zeros((12*44100, 2), dtype=np.float32)
    repeats = [measure(db, silence, 44100) for _ in range(3)]
    checks = []
    for _ in range(30):
        start = time.perf_counter()
        assert float(np.max(np.abs(silence))) == 0.0
        checks.append(time.perf_counter() - start)
    return {"snapshot_48k_12s_s": summary(copies), "silence_12s_repeats": repeats,
            "exact_silence_check_s": summary(checks)}


def run(output):
    result = {"python": platform.python_version(), "numpy": np.__version__,
              "settings": asdict(SETTINGS.capture), "historical_log": log_summary(),
              "clips": [], "transitions": [], "controls": []}
    original_paths = fp_module.FINGERPRINTS_DB, fp_module.SONG_PATHS_DB
    with tempfile.TemporaryDirectory(prefix="vjvision-profile-") as folder:
        folder = Path(folder)
        for src, name in zip(original_paths, ["fingerprints.db", "song_paths.sqlite"]):
            backup(src, folder / name)
        fp_module.FINGERPRINTS_DB = folder / "fingerprints.db"
        fp_module.SONG_PATHS_DB = folder / "song_paths.sqlite"
        start = time.perf_counter()
        db = fp_module.FingerprintDB()
        result["initialization_s"] = time.perf_counter() - start
        try:
            with closing(sqlite3.connect(folder / "fingerprints.db")) as con:
                result["database"] = dict(zip(["songs", "hashes"], con.execute(
                    "SELECT COUNT(*), SUM(total_hashes) FROM songs WHERE fingerprinted=1").fetchone()))
                result["query_plan"] = con.execute(
                    "EXPLAIN QUERY PLAN SELECT hash,song_id,offset FROM fingerprints WHERE hash IN (UPPER(?))",
                    ("0" * 20,)).fetchall()
            files = db._sqlite.execute("SELECT song_id,file_path FROM songs ORDER BY song_id").fetchall()
            audio = []
            for sid, path in files:
                if not Path(path).is_file():
                    continue
                try:
                    data, sr = sf.read(path, dtype="float32", always_2d=True)
                except Exception as exc:
                    result.setdefault("decode_errors", []).append({"song_id": sid, "error_type": type(exc).__name__})
                    continue
                if len(data) / sr < 80:
                    continue
                audio.append((sid, sr, data))
            result["available_songs"] = [sid for sid, _, _ in audio]
            if not audio:
                raise RuntimeError("No indexed, readable audio files of at least 80 seconds")
            sid, sr, data = audio[0]
            result["first_match"] = measure(db, data[30*sr:42*sr], sr, expected=sid)
            for sid, sr, data in audio:
                print(f"Measuring song_id={sid}", flush=True)
                for end in [20, 40, 70]:
                    for length in [4, 6, 8, 12]:
                        clip = data[(end-length)*sr:end*sr]
                        for input_sr in [44100, 48000]:
                            capture = resample_poly(clip, input_sr, sr, axis=0) if sr != input_sr else clip
                            result["clips"].append(measure(db, capture, input_sr, expected=sid,
                                end=end, window=length, input_sr=input_sr, channel_mode="stereo"))
                        if length == 6:
                            for mode, capture in [("left", clip[:, :1]), ("mono", clip.mean(axis=1, keepdims=True))]:
                                result["clips"].append(measure(db, capture, sr, expected=sid,
                                    end=end, window=length, input_sr=sr, channel_mode=mode))
            # Hard cuts at t=0; match every second to expose the evidence curve.
            # This does not run the application's jittered scheduler.
            for index in range(min(3, len(audio)-1)):
                sid_a, sr_a, data_a = audio[index]
                sid_b, sr_b, data_b = audio[index+1]
                a = resample_poly(data_a[30*sr_a:60*sr_a], 44100, sr_a, axis=0)
                b = resample_poly(data_b[30*sr_b:60*sr_b], 44100, sr_b, axis=0)
                stream = np.concatenate([a[:12*44100], b[:20*44100]])
                print(f"Measuring hard cut {sid_a} -> {sid_b}", flush=True)
                for window in [6, 8, 12]:
                    for elapsed in range(1, 17):
                        end = (12+elapsed)*44100
                        result["transitions"].append(measure(db, stream[end-window*44100:end], 44100,
                            old=sid_a, expected=sid_b, elapsed=elapsed, window=window))
            rng = np.random.default_rng(20260908)
            for window in [4, 6, 8, 12]:
                for label, sample in [("silence", np.zeros((window*44100, 2), dtype=np.float32)),
                                      ("white_noise", rng.normal(0, .1, (window*44100, 2)).astype(np.float32))]:
                    result["controls"].append(measure(db, sample, 44100, control=label, window=window))
            profiler = cProfile.Profile()
            sid, sr, data = audio[0]
            profiler.enable()
            for _ in range(5):
                db.match_from_array(data[30*sr:42*sr], input_sr=sr)
            profiler.disable()
            stream = io.StringIO()
            pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative").print_stats(25)
            result["cprofile"] = stream.getvalue()
            result["auxiliary"] = auxiliary_measurements()
        finally:
            db.close()
            fp_module.FINGERPRINTS_DB, fp_module.SONG_PATHS_DB = original_paths
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved {output}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("cache/recognition-profile.json"))
    run(parser.parse_args().output.resolve())
