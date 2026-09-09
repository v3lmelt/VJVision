"""Compare current recognition and Olaf through the same MatcherThread.run().

Music, native code and indexes remain local. Both engines use the same readable
reference songs. Olaf's score is translated to an explicit weak/accepted verdict,
not presented as a probability. A third arm applies that verdict rule to Dejavu
to expose gains attributable to the decision threshold rather than the engine.
"""
from __future__ import annotations

import argparse
import ctypes as ct
from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vjvision import fingerprint as fp, matcher
from vjvision.config import CaptureConfig
from tools import benchmark_dj_transitions as simulation
from tools.compare_recognition_engines import transformed
from tools.profile_recognition import backup, summary

SR = simulation.SR
CALLBACK = ct.CFUNCTYPE(None, ct.c_int, ct.c_float, ct.c_float, ct.c_char_p,
                       ct.c_uint32, ct.c_float, ct.c_float)


def group_hits(hits):
    by_song = {}
    for hit in hits:
        sid = hit["song_id"]
        if sid not in by_song or hit["count"] > by_song[sid]["count"]:
            by_song[sid] = hit
    return sorted(by_song.values(), key=lambda h: (-h["count"], h["song_id"]))


def make_hit(count, query_start, query_stop, path, reference_start, reference_stop):
    sid = re.fullmatch(r"track-(\d+)\.wav", path)
    if not sid:
        raise ValueError(f"Unexpected native reference alias: {path}")
    return dict(song_id=int(sid[1]), count=int(count), span=float(query_stop - query_start),
                offset_seconds=float(reference_start - query_start))


class Native:
    def __init__(self, dll, executable, folder):
        self.executable, self.folder = executable.resolve(), folder
        self.lib = ct.CDLL(str(dll.resolve()))
        self.lib.bench_open.argtypes = [ct.c_char_p]
        self.lib.bench_open.restype = ct.c_void_p
        self.lib.bench_query.argtypes = [ct.c_void_p, ct.POINTER(ct.c_float), ct.c_size_t, CALLBACK]
        self.lib.bench_query.restype = ct.c_size_t
        self.lib.bench_close.argtypes = [ct.c_void_p]
        self.lib.bench_close.restype = None
        self.runner = None

    def open(self):
        if self.runner is None:
            self.runner = self.lib.bench_open(os.fsencode(self.folder / "db"))
            if not self.runner:
                raise RuntimeError("Olaf runner initialization failed")

    def close(self):
        if self.runner:
            self.lib.bench_close(self.runner)
            self.runner = None

    def query_dll(self, mono):
        self.open()
        hits, errors = [], []

        def receive(count, q0, q1, path, identifier, r0, r1):
            try:
                if count == 0 and not path:
                    return
                hits.append(make_hit(count, q0, q1, path.decode("ascii"), r0, r1))
            except Exception as exc:
                errors.append(str(exc))

        callback = CALLBACK(receive)
        total = self.lib.bench_query(self.runner, mono.ctypes.data_as(ct.POINTER(ct.c_float)), len(mono), callback)
        if errors or total == ct.c_size_t(-1).value:
            raise RuntimeError(f"Native query failed: {errors}")
        return group_hits(hits), int(total)

    def query_cli(self, mono):
        raw = self.folder / "query.raw"
        mono.tofile(raw)
        proc = subprocess.run([str(self.executable), "query", str(raw), "query.wav"],
                              cwd=self.folder, capture_output=True, text=True, check=True, timeout=30)
        total = re.search(r"Matched (\d+) fp's", proc.stderr)
        if total is None:
            raise RuntimeError("Missing native fingerprint count")
        hits = []
        for line in proc.stdout.splitlines():
            fields = [f.strip() for f in line.split(",")]
            if len(fields) == 7 and fields[0].isdigit():
                if int(fields[0]) == 0 and not fields[3]:
                    continue
                hits.append(make_hit(int(fields[0]), float(fields[1]), float(fields[2]), fields[3],
                                     float(fields[5]), float(fields[6])))
        return group_hits(hits), int(total[1])

    def index(self, verb, songs):
        self.close()  # A long-lived LMDB read transaction must not hide index changes.
        command = [str(self.executable), verb]
        for sid, _, _ in songs:
            command.extend([str(self.folder / f"reference-{sid}.raw"), f"track-{sid}.wav"])
        subprocess.run(command, cwd=self.folder, capture_output=True, check=True, timeout=300)
        self.open()


def mono16(samples, input_sr):
    mono = samples.mean(axis=1) if samples.ndim == 2 else samples
    if input_sr != 16000:
        mono = resample_poly(mono, 16000, input_sr)
    return np.ascontiguousarray(mono, dtype=np.float32)


def verdict(hits, path_ids):
    """Frozen pre-existing prototype rule: 20 matches, 2 s, 2x runner-up.

    0.30 and 0.06 are compatibility verdict codes consumed by the unchanged
    application's thresholds. They are not estimated confidence probabilities.
    The app's separate 20% ratio gate still controls early long-window bypass.
    """
    if not hits:
        return fp.MatchResult(False, None, 0, 0, None, {"aligned_candidates": []})
    best = hits[0]
    runner = hits[1]["count"] if len(hits) > 1 else 0
    strong = best["count"] >= 20 and best["span"] >= 2 and best["count"] >= 2 * runner
    return fp.MatchResult(True, path_ids[best["song_id"]], .30 if strong else .06,
                          best["offset_seconds"], best["song_id"],
                          {"aligned_candidates": hits[:5], "accepted_verdict": strong})


class Engines:
    def __init__(self, db, native, paths):
        self.db, self.native, self.paths = db, native, paths

    def current(self, samples, input_sr):
        return self.db.match_from_array(samples, input_sr=input_sr)

    def common_dejavu(self, samples, input_sr):
        result = self.current(samples, input_sr)
        return verdict(result.raw.get("aligned_candidates", []), self.paths)

    def olaf(self, samples, input_sr, cli=False):
        if not samples.size or not np.any(samples):
            return verdict([], self.paths)
        mono = mono16(samples, input_sr)
        hits, total = (self.native.query_cli(mono) if cli else self.native.query_dll(mono))
        for hit in hits:
            # Olaf's native aligned-vote ratio is not Dejavu's unique-hash ratio.
            hit["ratio"] = hit["count"] / max(1, total)
        result = verdict(hits, self.paths)
        result.raw.update(native_fingerprints=total, evidence_kind="olaf_native_votes")
        return result


class MeasuredRecognizer(simulation.TimedRecognizer):
    def match_from_array(self, samples, input_sr):
        start, cpu = time.perf_counter(), time.process_time()
        at = self.clock.now - 1000
        result = self.method(self.db, samples, input_sr=input_sr)
        cpu = time.process_time() - cpu
        elapsed = time.perf_counter() - start
        self.clock.now += elapsed
        self.calls.append(dict(at=at, seconds=elapsed, cpu_s=cpu, window=len(samples) / input_sr,
                               song_id=result.song_id, confidence=result.confidence,
                               accepted_verdict=result.raw.get("accepted_verdict"),
                               aligned=result.raw.get("aligned_candidates", [])))
        return result


def assert_parity(cli, dll):
    a, na = cli
    b, nb = dll
    if na != nb or len(a) != len(b):
        raise AssertionError("DLL/CLI fingerprint count or candidate count differs")
    for x, y in zip(a, b):
        if x["song_id"] != y["song_id"] or x["count"] != y["count"]:
            raise AssertionError("DLL/CLI candidate differs")
        if abs(x["span"] - y["span"]) > .021 or abs(x["offset_seconds"] - y["offset_seconds"]) > .021:
            raise AssertionError("DLL/CLI time alignment differs beyond CLI rounding")


def main(args):
    if os.environ.get("HOME"):
        raise RuntimeError("The upstream CLI would use HOME/.olaf; refusing to touch that store")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="olaf-application-", dir=args.output.parent.resolve()))
    (folder / "db").mkdir()
    backup(fp.FINGERPRINTS_DB, folder / "dejavu.db")
    backup(fp.SONG_PATHS_DB, folder / "paths.sqlite")
    with patch.multiple(fp, FINGERPRINTS_DB=folder / "dejavu.db", SONG_PATHS_DB=folder / "paths.sqlite"):
        db = fp.FingerprintDB()
    mapping = {str(Path(r["file_path"]).resolve()): r["song_id"] for r in db._sqlite.execute("SELECT * FROM songs")}
    paths, songs, skipped = {}, [], []
    for path in sorted(args.tracks.iterdir()):
        try:
            audio, rate = sf.read(path, dtype="float32", always_2d=True)
        except Exception:
            skipped.append(path.suffix)
            continue
        sid = mapping.get(str(path.resolve()))
        if sid is None:
            continue
        if audio.shape[1] == 1:
            audio = np.repeat(audio, 2, axis=1)
        if rate != SR:
            audio = resample_poly(audio, SR, rate, axis=0)
        audio = audio[:, :2]
        mono16(audio, SR).tofile(folder / f"reference-{sid}.raw")
        paths[sid] = str(path.resolve())
        songs.append((sid, audio, hashlib.sha256(path.read_bytes()).hexdigest()))
    if len(songs) < 2:
        raise RuntimeError("Need at least two readable indexed songs")
    with sqlite3.connect(folder / "dejavu.db") as con:
        marks = ",".join("?" for _ in paths)
        for table in ("fingerprints", "songs"):
            con.execute(f"DELETE FROM {table} WHERE song_id NOT IN ({marks})", list(paths))
        con.commit()
        con.execute("VACUUM")
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    backup(folder / "dejavu.db", folder / "reference.db")
    native = Native(args.dll, args.olaf, folder)
    native.index("store", songs)
    engines = Engines(db, native, paths)
    cfg = CaptureConfig()
    variants = {"current_dejavu": lambda _, s, input_sr: engines.current(s, input_sr),
                "common_gate_dejavu": lambda _, s, input_sr: engines.common_dejavu(s, input_sr),
                "olaf_dll": lambda _, s, input_sr: engines.olaf(s, input_sr)}
    result = dict(head=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True, cwd=ROOT).strip(),
                  data_directory=str(folder), config=asdict(cfg), skipped=skipped,
                  song_sha256={sid: digest for sid, _, digest in songs}, reference_songs=len(songs),
                  source_sha256={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in [ROOT / "vjvision" / f"{name}.py" for name in
                                ["matcher", "fingerprint", "alignment", "dejavu_sqlite", "config"]]},
                  binary_sha256={"dll": hashlib.sha256(args.dll.read_bytes()).hexdigest(),
                                 "cli": hashlib.sha256(args.olaf.read_bytes()).hexdigest()},
                  index_bytes={"dejavu": (folder / "dejavu.db").stat().st_size,
                               "olaf": (folder / "db" / "data.mdb").stat().st_size},
                  micro=[], parity_checks=0, runs=[])
    rng = random.Random(509)

    def save():
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")

    # Identical 48 kHz windows for processing-cost comparisons. Warm both
    # engines first; initialization is excluded from these steady-state costs.
    warm = songs[0][1][30 * SR:36 * SR]
    engines.current(warm, SR)
    engines.olaf(warm, SR)
    for sid, audio, _ in songs:
        for offset in args.offsets:
            for seconds in [2, 4, 6, 12]:
                clip = audio[offset * SR:(offset + seconds) * SR]
                mono = mono16(clip, SR)
                assert_parity(native.query_cli(mono), native.query_dll(mono))
                result["parity_checks"] += 1
                order = ["current_dejavu", "olaf_dll", "olaf_cli"]
                rng.shuffle(order)
                for name in order:
                    start, cpu = time.perf_counter(), time.process_time()
                    answer = engines.current(clip, SR) if name == "current_dejavu" else engines.olaf(clip, SR, cli=name == "olaf_cli")
                    elapsed_cpu, elapsed = time.process_time() - cpu, time.perf_counter() - start
                    result["micro"].append(dict(variant=name, song_id=sid, offset=offset, seconds=seconds,
                        wall_s=elapsed, cpu_s=None if name == "olaf_cli" else elapsed_cpu,
                        accepted=answer.matched and answer.confidence >= .30, predicted=answer.song_id))
        print(f"micro and parity complete: song {sid}", flush=True)
    save()
    path_ids = {path: sid for sid, path in paths.items()}

    def compare(stream, old_id, target, kind, seed, offset, simulation_kind=None):
        order = list(variants)
        rng.shuffle(order)
        for name in order:
            with patch.object(simulation, "TimedRecognizer", MeasuredRecognizer):
                row = simulation.simulate(matcher, cfg, variants[name], None, stream, paths.get(old_id),
                                          target, simulation_kind or kind, seed, path_ids)
            row.update(variant=name, kind=kind, seed=seed, offset=offset,
                       recognition_cpu_s=sum(c["cpu_s"] for c in row["calls"]))
            result["runs"].append(row)
            print(f"{offset} {kind} {old_id}->{target} {name}: latency={row['latency']} "
                  f"correct={row['final_correct']} wrong={row['wrong_switches'] + row['unexpected_switches'] + row['premature_switches']}", flush=True)
        save()

    def clip(audio, offset):
        data = audio[offset * SR:int((offset + 12 + simulation.DURATION + 3) * SR)]
        if len(data) != int((12 + simulation.DURATION + 3) * SR):
            raise RuntimeError("Insufficient audio at a requested test offset")
        return data

    try:
        for offset_index, offset in enumerate(args.offsets):
            seed = [7, 29, 83][offset_index % 3]
            for i, (sid, audio, _) in enumerate(songs):
                target, next_audio, _ = songs[(i + 1) % len(songs)]
                a, b = clip(audio, offset), clip(next_audio, offset)
                for kind in ["hard_cut", "fade", "abort"]:
                    compare(simulation.transition(a, b, kind), sid, target, kind, seed, offset)
                initial = a.copy()
                initial[:12 * SR] = 0
                compare(initial, None, sid, "startup", seed, offset)
            zero = np.zeros_like(clip(songs[0][1], offset))
            compare(zero, None, None, "silence", seed, offset)
            noise = np.random.default_rng(seed).normal(0, .03, zero.shape).astype("float32")
            compare(noise, None, None, "noise", seed, offset)
        if args.stress:
            for i, (sid, audio, _) in enumerate(songs):
                target, next_audio, _ = songs[(i + 1) % len(songs)]
                for effect in ["speed_up", "tempo_up", "eq"]:
                    extended = next_audio[70 * SR:130 * SR]
                    b = transformed(extended, effect, SR)[:int((12 + simulation.DURATION + 3) * SR)]
                    compare(simulation.transition(clip(audio, 70), b, "hard_cut"), sid, target,
                            f"cut_{effect}", 41, 70, "hard_cut")
        # Leave EACH real song out, testing both cold unknown input and a
        # known-to-unknown cut. All index changes remain inside this folder.
        for i, song in enumerate(songs):
            sid, audio, _ = song
            native.index("delete", [song])
            with sqlite3.connect(folder / "dejavu.db") as con:
                con.execute("DELETE FROM fingerprints WHERE song_id=?", (sid,))
                con.execute("DELETE FROM songs WHERE song_id=?", (sid,))
            compare(clip(audio, 70), None, None, "unknown", 53, 70)
            old, old_audio, _ = songs[(i + 1) % len(songs)]
            compare(simulation.transition(clip(old_audio, 110), clip(audio, 110), "hard_cut"),
                    old, None, "unknown_playing", 53, 110, "abort")
            with sqlite3.connect(folder / "dejavu.db") as con:
                con.execute("ATTACH DATABASE ? AS original", (str(folder / "reference.db"),))
                for table in ("fingerprints", "songs"):
                    con.execute(f"INSERT INTO {table} SELECT * FROM original.{table} WHERE song_id=?", (sid,))
            native.index("store", [song])
    finally:
        native.close()
        db.close()
        save()
    result["summary"] = {}
    for name in variants:
        result["summary"][name] = {}
        for kind in sorted({r["kind"] for r in result["runs"]}):
            rows = [r for r in result["runs"] if r["variant"] == name and r["kind"] == kind]
            result["summary"][name][kind] = dict(cases=len(rows), correct=sum(r["final_correct"] for r in rows),
                wrong=sum(r["wrong_switches"] + r["unexpected_switches"] + r["premature_switches"] + r["reverts"] for r in rows),
                latency=summary([r["latency"] for r in rows if r["latency"] is not None]),
                wall=summary([r["recognition_wall_s"] for r in rows]),
                cpu=summary([r["recognition_cpu_s"] for r in rows]))
    result["complete"] = True
    save()
    print(json.dumps(result["summary"], indent=2), flush=True)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracks", type=Path, default=ROOT / "tracks")
    parser.add_argument("--olaf", type=Path, required=True)
    parser.add_argument("--dll", type=Path, required=True)
    parser.add_argument("--offsets", type=int, nargs="+", default=[30, 70, 110])
    parser.add_argument("--stress", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / "cache" / "olaf-application.json")
    main(parser.parse_args())
