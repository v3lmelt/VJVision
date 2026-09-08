"""Compare real recognition loops on offline DJ transitions and SQLite backups.

No audio device, metadata artwork, live database writes, or music uploads.
The virtual clock advances by actual recognition wall time plus loop sleeps.
Baseline source is loaded from a trusted local Git revision specified by the user.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
import queue
import random
import sqlite3
import subprocess
import sys
import tempfile
import time
import types
from unittest.mock import patch

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vjvision import fingerprint as fp_module, matcher as current_module
from vjvision.config import CaptureConfig, SETTINGS
from vjvision.metadata import Track
from tools.profile_recognition import backup, summary

SR = 48000
DURATION = 36.0
SWITCH_AT = 5.0
FADE_SECONDS = 8.0


def source_module(ref, name):
    source = subprocess.check_output(
        ["git", "show", f"{ref}:vjvision/{name}.py"], cwd=ROOT, encoding="utf-8")
    module = types.ModuleType(f"vjvision._benchmark_baseline_{name}")
    module.__file__ = str(ROOT / "vjvision" / f"{name}.py")
    sys.modules[module.__name__] = module
    exec(compile(source, f"{ref}:vjvision/{name}.py", "exec"), module.__dict__)
    return module


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.matcher = None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds
        if self.now >= 1000.0 + DURATION:
            self.matcher.stop()


class Capture:
    sr = SR

    def __init__(self, stream, clock):
        self.stream = stream
        self.clock = clock

    def snapshot(self):
        end = int((12 + self.clock.now - 1000.0) * SR)
        return self.stream[max(0, end - 12 * SR):end].copy()

    def current_level(self):
        return {"active": True, "peak": .5, "rms": .1}


class TimedRecognizer:
    def __init__(self, db, method, clock, path_ids):
        self.db, self.method, self.clock = db, method, clock
        self.path_ids = path_ids
        self.calls = []

    def match_from_array(self, samples, input_sr):
        start = time.perf_counter()
        at = self.clock.now - 1000
        result = self.method(self.db, samples, input_sr=input_sr)
        elapsed = time.perf_counter() - start
        self.clock.now += elapsed
        self.calls.append({"at": at, "seconds": elapsed, "window": len(samples) / input_sr,
                           "song_id": result.song_id, "confidence": result.confidence})
        return result

    def close(self):
        pass


def simulate(module, config, method, db, stream, old_path, target_id, kind, seed, path_ids):
    clock = Clock()
    events = []
    errors = []
    recognizer = TimedRecognizer(db, method, clock, path_ids)
    rng = random.Random(seed)
    with patch.object(module, "time", clock), patch.object(SETTINGS, "capture", config), \
            patch.object(random, "uniform", rng.uniform), \
            patch.object(module, "extract_track", side_effect=lambda p: Track(p, str(path_ids[p]), "", "", None)):
        matcher = module.MatcherThread(queue.Queue(), queue.Queue(), queue.Queue())
        clock.matcher = matcher
        matcher._capture = Capture(stream, clock)
        matcher._capture_running = True
        matcher._current_track_path = old_path
        matcher._fp = recognizer
        matcher._ensure_fp = lambda: None
        matcher._start_monitor = lambda: None
        matcher._close_capture = lambda: None
        matcher._send_viz = lambda message: None

        def ui(message):
            if message["type"] == "track":
                events.append({"at": clock.now - 1000, "song_id": int(message["title"]),
                               "confidence": message["confidence"]})

        matcher._send_ui = ui
        matcher._log = lambda text, level="info": errors.append(text) if level == "error" else None
        matcher.run()
        final_id = path_ids.get(matcher._current_track_path)
    if errors:
        raise RuntimeError(f"Matcher errors during {kind}: {errors}")
    old_id = path_ids.get(old_path)
    first_target = next((e["at"] for e in events if e["song_id"] == target_id), None)
    # For fades the reference is the equal-gain midpoint, not fade start.
    reference = SWITCH_AT + FADE_SECONDS / 2 if kind == "fade" else SWITCH_AT
    if kind in {"startup", "silence", "noise", "unknown"}:
        reference = 0.0
    expected_final = old_id if kind == "abort" else target_id
    wrong = [e for e in events if e["song_id"] not in {old_id, target_id}]
    premature = [e for e in events if e["song_id"] == target_id and e["at"] < reference]
    unexpected = events if kind in {"silence", "noise", "unknown"} else (
        [e for e in events if e["song_id"] != old_id] if kind == "abort" else [])
    reverts = [e for e in events if first_target is not None and e["at"] > first_target and e["song_id"] == old_id]
    return {"kind": kind, "seed": seed, "old_id": old_id, "target_id": target_id,
            "reference": reference, "first_target_at": first_target,
            "latency": first_target - reference if first_target is not None else None,
            "final_correct": final_id == expected_final, "final_id": final_id,
            "wrong_switches": len(wrong), "premature_switches": len(premature),
            "unexpected_switches": len(unexpected), "reverts": len(reverts),
            "calls": recognizer.calls, "events": events,
            "recognition_wall_s": sum(c["seconds"] for c in recognizer.calls)}


def transition(a, b, kind):
    t = np.arange(len(a), dtype=np.float32) / SR - 12
    if kind == "hard_cut":
        gain = (t >= SWITCH_AT).astype(np.float32)
    elif kind == "fade":
        gain = np.clip((t - SWITCH_AT) / FADE_SECONDS, 0, 1)
    elif kind == "abort":
        # Introduce a secondary deck, never let it dominate, then remove it.
        gain = .35 * np.clip(1 - np.abs(t - SWITCH_AT - 4) / 4, 0, 1)
    else:
        raise ValueError(kind)
    return a * (1 - gain[:, None]) + b * gain[:, None]


def benchmark(args):
    baseline_config = source_module(args.baseline_ref, "config").CaptureConfig()
    baseline_module = source_module(args.baseline_ref, "matcher")
    baseline_fp = source_module(args.baseline_ref, "fingerprint")
    variants = [("baseline", baseline_module, baseline_config, baseline_fp.FingerprintDB.match_from_array),
                ("optimized", current_module, CaptureConfig(), fp_module.FingerprintDB.match_from_array)]
    result = {"baseline_ref": args.baseline_ref, "sample_rate": SR, "duration": DURATION,
              "offset": args.offset, "settings": {name: asdict(cfg) for name, _, cfg, _ in variants},
              "optimized_source_sha256": {name: hashlib.sha256((ROOT / "vjvision" / f"{name}.py").read_bytes()).hexdigest()
                                          for name in ["matcher", "fingerprint", "config", "audio_capture"]},
              "runs": [], "skipped": []}
    originals = fp_module.FINGERPRINTS_DB, fp_module.SONG_PATHS_DB
    with tempfile.TemporaryDirectory(prefix="vjvision-dj-") as folder:
        folder = Path(folder)
        for src, name in zip(originals, ["fingerprints.db", "song_paths.sqlite"]):
            backup(src, folder / name)
        with patch.object(fp_module, "FINGERPRINTS_DB", folder / "fingerprints.db"), \
                patch.object(fp_module, "SONG_PATHS_DB", folder / "song_paths.sqlite"):
            db = fp_module.FingerprintDB()
        try:
            path_ids = {row["file_path"]: row["song_id"] for row in db._sqlite.execute("SELECT * FROM songs")}
            songs = []
            frames = int((12 + DURATION + 3) * SR)
            for path in sorted(args.tracks.iterdir()):
                if not path.is_file():
                    continue
                try:
                    info = sf.info(path)
                except Exception:
                    result["skipped"].append({"extension": path.suffix, "reason": "unsupported audio format"})
                    continue
                sid = path_ids.get(str(path.resolve()))
                if sid is None:
                    result["skipped"].append({"extension": path.suffix, "reason": "not indexed"})
                    continue
                native, sr = sf.read(path, start=int(args.offset * info.samplerate),
                                     frames=int((12 + DURATION + 3) * info.samplerate),
                                     dtype="float32", always_2d=True)
                data = resample_poly(native, SR, sr, axis=0) if sr != SR else native
                if len(data) < frames:
                    result["skipped"].append({"song_id": sid, "reason": "insufficient audio after offset"})
                    continue
                if data.shape[1] == 1:
                    data = np.repeat(data, 2, axis=1)
                songs.append((sid, str(path.resolve()), data[:frames, :2]))
            if len(songs) < 2:
                raise RuntimeError("Need at least two indexed, readable songs")
            result["song_ids"] = [sid for sid, _, _ in songs]
            result["database_songs"] = len(db._djv.db.get_songs())
            from dejavu.logic import fingerprint as fingerprint_logic
            result["kernel_checks"] = []
            for sid, _, data in songs:
                pcm = (resample_poly(data[:12 * SR], 44100, SR, axis=0) * 30000).astype(np.int16)
                hashes, timings = {}, {}
                for name, erosion in [("baseline", ndimage.binary_erosion),
                                      ("optimized", fp_module.FingerprintDB._erode_peak_background)]:
                    start = time.perf_counter()
                    with patch.object(fingerprint_logic, "binary_erosion", erosion):
                        hashes[name] = set().union(*(set(db._djv.generate_fingerprints(
                            pcm[:, channel], Fs=44100)[0]) for channel in range(pcm.shape[1])))
                    timings[name] = time.perf_counter() - start
                identical = hashes["baseline"] == hashes["optimized"]
                if not identical:
                    raise RuntimeError(f"Fingerprint compatibility failed for song_id={sid}")
                result["kernel_checks"].append({"song_id": sid, "identical": identical,
                                                "hashes": len(hashes["baseline"]), "seconds": timings})

            def compare(stream, old, target, kind, seed):
                for name, module, cfg, method in variants:
                    # Both variants import the same Dejavu module. Restore
                    # the original kernel for baseline calls to avoid
                    # accidentally benchmarking the optimized baseline.
                    erosion = (ndimage.binary_erosion if name == "baseline"
                               else fp_module.FingerprintDB._erode_peak_background)
                    with patch.object(fingerprint_logic, "binary_erosion", erosion):
                        row = simulate(module, cfg, method, db, stream, old, target, kind, seed, path_ids)
                    row["variant"] = name
                    result["runs"].append(row)
                    print(f"{kind} {row['old_id']}->{target} seed={seed} {name}: "
                          f"latency={row['latency']} final={row['final_correct']} "
                          f"unexpected={row['unexpected_switches']} wrong={row['wrong_switches']}", flush=True)
                # Preserve completed experiments even if a later case fails.
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")

            for seed in args.seeds:
                for i, (sid, path, a) in enumerate(songs):
                    next_sid, _, b = songs[(i + 1) % len(songs)]
                    for kind in ["hard_cut", "fade", "abort"]:
                        if kind in args.kinds and (args.old_song_id is None or sid == args.old_song_id):
                            compare(transition(a, b, kind), path, next_sid, kind, seed)
                for sid, _, data in songs:
                    if "startup" in args.kinds:
                        startup = data.copy()
                        startup[:12 * SR] = 0
                        compare(startup, None, sid, "startup", seed)
            zeros = np.zeros_like(songs[0][2])
            if "silence" in args.kinds:
                compare(zeros, None, None, "silence", args.seeds[0])
            rng = np.random.default_rng(20260909)
            if "noise" in args.kinds:
                compare(rng.normal(0, .03, zeros.shape).astype(np.float32), None, None, "noise", args.seeds[0])
            # A held-out real song is a stronger negative than synthetic noise.
            held_id, _, held_audio = songs[-1]
            with closing(sqlite3.connect(folder / "fingerprints.db")) as con:
                con.execute("DELETE FROM fingerprints WHERE song_id=?", (held_id,))
                con.execute("DELETE FROM songs WHERE song_id=?", (held_id,))
                con.commit()
            if "unknown" in args.kinds:
                compare(held_audio, None, None, "unknown", args.seeds[0])
            result["held_out_song_id"] = held_id
        finally:
            db.close()
    result["summary"] = {}
    for name, _, _, _ in variants:
        rows = [r for r in result["runs"] if r["variant"] == name]
        result["summary"][name] = {
            kind: {"cases": len(group), "final_correct": sum(r["final_correct"] for r in group),
                   "latency_s": summary([r["latency"] for r in group if r["latency"] is not None]),
                   "wrong_switches": sum(r["wrong_switches"] for r in group),
                   "premature_switches": sum(r["premature_switches"] for r in group),
                   "unexpected_switches": sum(r["unexpected_switches"] for r in group),
                   "reverts": sum(r["reverts"] for r in group),
                   "recognition_wall_s": summary([r["recognition_wall_s"] for r in group])}
            for kind in sorted({r["kind"] for r in rows})
            if (group := [r for r in rows if r["kind"] == kind])}
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result["summary"], indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline-ref", required=True)
    parser.add_argument("--tracks", type=Path, default=ROOT / "tracks")
    parser.add_argument("--offset", type=float, default=30.0)
    parser.add_argument("--seeds", type=int, nargs="+", default=[7])
    parser.add_argument("--kinds", nargs="+", default=["hard_cut", "fade", "abort", "startup", "silence", "noise", "unknown"],
                        choices=["hard_cut", "fade", "abort", "startup", "silence", "noise", "unknown"])
    parser.add_argument("--old-song-id", type=int, help="Limit transitions to this indexed outgoing song")
    parser.add_argument("--output", type=Path, default=ROOT / "cache" / "dj-benchmark.json")
    logging.basicConfig(level=logging.CRITICAL)
    benchmark(parser.parse_args())
