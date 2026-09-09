"""Compare a persistent WSL/LMDB Panako worker with the current Windows matcher.

Uses the same six-song corpus and scenarios as benchmark_olaf_application.py.
The earlier report provides isolated reference indexes and corpus hashes only;
all timing and recognition results are measured afresh.
"""
from __future__ import annotations
import argparse
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
import random
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
from tools import benchmark_dj_transitions as simulation
from tools.benchmark_olaf_application import MeasuredRecognizer, verdict
from tools.compare_recognition_engines import transformed
from tools.panako_wsl_client import PanakoWorker, PanakoRecognizer, pcm16
from tools.profile_recognition import backup, summary
from vjvision import fingerprint as fp, matcher
from vjvision.config import CaptureConfig

SR = simulation.SR


class Measured(MeasuredRecognizer):
    def match_from_array(self, samples, input_sr):
        result = super().match_from_array(samples, input_sr)
        self.calls[-1].update(server_s=result.raw.get("server_s"),
                              roundtrip_s=result.raw.get("roundtrip_s"))
        return result


def main(args):
    previous = json.loads(args.reference_report.read_text())
    source = Path(previous["data_directory"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="panako-application-", dir=args.output.parent.resolve()))
    for original, name in [("reference.db", "dejavu.db"), ("paths.sqlite", "paths.sqlite")]:
        backup(source / original, folder / name)
    backup(folder / "dejavu.db", folder / "reference.db")
    with patch.multiple(fp, FINGERPRINTS_DB=folder / "dejavu.db", SONG_PATHS_DB=folder / "paths.sqlite"):
        db = fp.FingerprintDB()
    paths, songs = {}, []
    mapping = {str(Path(r["file_path"]).resolve()): r["song_id"] for r in db._sqlite.execute("SELECT * FROM songs")}
    for path in sorted(args.tracks.iterdir()):
        sid = mapping.get(str(path.resolve()))
        if str(sid) not in previous["song_sha256"] or not path.is_file():
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != previous["song_sha256"][str(sid)]:
            raise RuntimeError("Reference audio changed since the Olaf benchmark")
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
        if audio.shape[1] == 1:
            audio = np.repeat(audio, 2, axis=1)
        if rate != SR:
            audio = resample_poly(audio, SR, rate, axis=0)
        songs.append((sid, audio[:, :2]))
        paths[sid] = str(path.resolve())
    if len(songs) != previous["reference_songs"]:
        raise RuntimeError("The complete matched reference corpus is required")
    linux_folder = subprocess.check_output(
        ["wsl", "-d", args.distro, "--", "mktemp", "-d", "/tmp/vj-panako-XXXXXX"], text=True).strip()
    def worker():
        return PanakoWorker(args.distro, args.java, args.classes, linux_folder, folder / "worker.log")
    native = worker()
    cfg = CaptureConfig()
    result = dict(complete=False, head=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        folder=str(folder), linux_database=linux_folder + "/db", config=asdict(cfg), sample_rate=SR,
        reference_songs=len(songs), song_sha256=previous["song_sha256"],
        source_sha256={p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in previous["source_sha256"]},
        jar_sha256=hashlib.sha256((args.classes / "Panako-2.1-all.jar").read_bytes()).hexdigest(),
        startup_s=native.startup_s, micro=[], runs=[], index=[])
    def save():
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    save()
    try:
        for sid, audio in songs:
            result["index"].append(dict(song_id=sid, **native.request(1, sid, pcm16(audio, SR), timeout=300)))
            print(f"indexed {sid}", flush=True)
        probe = pcm16(songs[0][1][70*SR:82*SR], SR)
        before = native.request(2, pcm=probe)["hits"]
        if not before or before[0]["song_id"] != songs[0][0]:
            raise AssertionError("Stored reference did not match")
        native.close()
        native = worker()
        after = native.request(2, pcm=probe)["hits"]
        if before != after:
            raise AssertionError("Persistent index changed after worker restart")
        result["restart"] = dict(identical=True, startup_s=native.startup_s, song_id=songs[0][0])
        result["ping_wall_s"] = summary([native.request(0)["roundtrip_s"] for _ in range(30)])
        panako = PanakoRecognizer(native, paths)
        variants = {
            "current_dejavu": lambda _, s, input_sr: db.match_from_array(s, input_sr=input_sr),
            "common_gate_dejavu": lambda _, s, input_sr: verdict(db.match_from_array(s, input_sr=input_sr).raw.get("aligned_candidates", []), paths),
            "panako_wsl": lambda _, s, input_sr: panako.match(s, input_sr),
        }
        rng = random.Random(509)
        for method in variants.values():
            method(None, songs[0][1][30*SR:42*SR], SR)
        for sid, audio in songs:
            for offset in [30, 70, 110]:
                for seconds in [2, 4, 6, 12]:
                    clip = audio[offset*SR:(offset+seconds)*SR]
                    order = ["current_dejavu", "panako_wsl"]
                    rng.shuffle(order)
                    for name in order:
                        start = time.perf_counter()
                        answer = variants[name](None, clip, SR)
                        result["micro"].append(dict(variant=name, song_id=sid, offset=offset, seconds=seconds,
                            wall_s=time.perf_counter()-start, server_s=answer.raw.get("server_s"),
                            roundtrip_s=answer.raw.get("roundtrip_s"), predicted=answer.song_id,
                            accepted=answer.matched and answer.confidence >= .30))
            print(f"micro complete {sid}", flush=True)
            save()
        path_ids = {path: sid for sid, path in paths.items()}
        def compare(stream, old, target, kind, seed, offset, simulation_kind=None):
            order = list(variants)
            rng.shuffle(order)
            for name in order:
                with patch.object(simulation, "TimedRecognizer", Measured):
                    row = simulation.simulate(matcher, cfg, variants[name], None, stream, paths.get(old),
                        target, simulation_kind or kind, seed, path_ids)
                row.update(variant=name, kind=kind, offset=offset)
                result["runs"].append(row)
                print(f"{offset} {kind} {old}->{target} {name}: latency={row['latency']} correct={row['final_correct']}", flush=True)
            save()
        def clip(audio, offset):
            data = audio[offset*SR:int((offset+12+simulation.DURATION+3)*SR)]
            if len(data) != int((12+simulation.DURATION+3)*SR):
                raise RuntimeError("Insufficient audio")
            return data
        for offset, seed in zip([30, 70, 110], [7, 29, 83]):
            for i, (sid, audio) in enumerate(songs):
                target, next_audio = songs[(i+1) % len(songs)]
                a, b = clip(audio, offset), clip(next_audio, offset)
                for kind in ["hard_cut", "fade", "abort"]:
                    compare(simulation.transition(a, b, kind), sid, target, kind, seed, offset)
                initial = a.copy()
                initial[:12*SR] = 0
                compare(initial, None, sid, "startup", seed, offset)
            zero = np.zeros_like(clip(songs[0][1], offset))
            compare(zero, None, None, "silence", seed, offset)
            compare(np.random.default_rng(seed).normal(0, .03, zero.shape).astype("float32"),
                    None, None, "noise", seed, offset)
        for i, (sid, audio) in enumerate(songs):
            target, next_audio = songs[(i+1) % len(songs)]
            for effect in ["speed_up", "tempo_up", "eq"]:
                b = transformed(next_audio[70*SR:130*SR], effect, SR)[:int((12+simulation.DURATION+3)*SR)]
                compare(simulation.transition(clip(audio, 70), b, "hard_cut"), sid, target,
                        f"cut_{effect}", 41, 70, "hard_cut")
        native.close()
        run_negatives(args, result, songs, paths, folder)
        summarize_result(result)
    finally:
        native.close()
        db.close()
        save()


def summarize_result(result):
    result["summary"] = {}
    for name in sorted({r["variant"] for r in result["runs"]}):
        result["summary"][name] = {}
        for kind in sorted({r["kind"] for r in result["runs"]}):
            rows = [r for r in result["runs"] if r["variant"] == name and r["kind"] == kind]
            result["summary"][name][kind] = dict(cases=len(rows), correct=sum(r["final_correct"] for r in rows),
                clean=sum(r["final_correct"] and not any(r[k] for k in
                    ["wrong_switches", "unexpected_switches", "premature_switches", "reverts"]) for r in rows),
                errors={k: sum(r[k] for r in rows) for k in ["wrong_switches", "unexpected_switches", "premature_switches", "reverts"]},
                latency=summary([r["latency"] for r in rows if r["latency"] is not None]),
                wall=summary([r["recognition_wall_s"] for r in rows]))
    result["complete"] = True


def run_negatives(args, result, songs, paths, folder):
    """Build six fresh leave-one-out LMDBs; upstream deletion leaves stale hashes."""
    result["negative_index_method"] = "fresh_five_song_lmdb_per_heldout_song"
    result["negative_databases"] = []
    path_ids = {path: sid for sid, path in paths.items()}
    rng = random.Random(953)
    for i, (sid, audio) in enumerate(songs):
        sqlite_path = folder / f"heldout-{sid}.db"
        backup(folder / "reference.db", sqlite_path)
        with sqlite3.connect(sqlite_path) as con:
            for table in ["fingerprints", "songs"]:
                con.execute(f"DELETE FROM {table} WHERE song_id=?", (sid,))
        with patch.multiple(fp, FINGERPRINTS_DB=sqlite_path, SONG_PATHS_DB=folder / "paths.sqlite"):
            db = fp.FingerprintDB()
        linux_folder = subprocess.check_output(
            ["wsl", "-d", args.distro, "--", "mktemp", "-d", "/tmp/vj-panako-heldout-XXXXXX"], text=True).strip()
        native = PanakoWorker(args.distro, args.java, args.classes, linux_folder, folder / "negative-worker.log")
        result["negative_databases"].append(dict(heldout=sid, directory=linux_folder))
        try:
            for other, reference in songs:
                if other != sid:
                    native.request(1, other, pcm16(reference, SR), timeout=300)
            heldout = native.request(2, pcm=pcm16(audio[70*SR:82*SR], SR))
            if any(h["song_id"] == sid for h in heldout["hits"]):
                raise AssertionError("Held-out song leaked into the index")
            panako = PanakoRecognizer(native, paths)
            variants = {
                "current_dejavu": lambda _, s, input_sr: db.match_from_array(s, input_sr=input_sr),
                "common_gate_dejavu": lambda _, s, input_sr: verdict(db.match_from_array(s, input_sr=input_sr).raw.get("aligned_candidates", []), paths),
                "panako_wsl": lambda _, s, input_sr: panako.match(s, input_sr),
            }
            def clip(a, offset):
                return a[offset*SR:int((offset+12+simulation.DURATION+3)*SR)]
            old, old_audio = songs[(i+1) % len(songs)]
            for kind, offset, old_id, stream, simulation_kind in [
                ("unknown", 70, None, clip(audio, 70), "unknown"),
                ("unknown_playing", 110, old, simulation.transition(clip(old_audio, 110), clip(audio, 110), "hard_cut"), "abort")]:
                order = list(variants)
                rng.shuffle(order)
                for name in order:
                    with patch.object(simulation, "TimedRecognizer", Measured):
                        row = simulation.simulate(matcher, CaptureConfig(), variants[name], None, stream,
                            paths.get(old_id), None, simulation_kind, 53, path_ids)
                    row.update(variant=name, kind=kind, offset=offset, heldout_id=sid)
                    result["runs"].append(row)
                    print(f"heldout {sid} {kind} {name}: correct={row['final_correct']}", flush=True)
                args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        finally:
            native.close()
            db.close()


def resume_negatives(args):
    result = json.loads(args.output.read_text())
    if result["complete"] or len(result["runs"]) != 288 or any(r["kind"].startswith("unknown") for r in result["runs"]):
        raise ValueError("Resume requires exactly the completed 96 non-heldout scenarios per arm")
    if any(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != h for p, h in result["source_sha256"].items()):
        raise ValueError("Production recognition source changed")
    if hashlib.sha256((args.classes / "Panako-2.1-all.jar").read_bytes()).hexdigest() != result["jar_sha256"]:
        raise ValueError("Panako JAR changed")
    folder = Path(result["folder"])
    with sqlite3.connect(folder / "paths.sqlite") as con:
        mapping = {str(Path(p).resolve()): sid for sid, p in con.execute("SELECT song_id,file_path FROM songs")}
    songs, paths = [], {}
    for path in sorted(args.tracks.iterdir()):
        sid = mapping.get(str(path.resolve()))
        if str(sid) not in result["song_sha256"] or not path.is_file():
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != result["song_sha256"][str(sid)]:
            raise ValueError("Reference audio changed")
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
        if audio.shape[1] == 1:
            audio = np.repeat(audio, 2, axis=1)
        if rate != SR:
            audio = resample_poly(audio, SR, rate, axis=0)
        songs.append((sid, audio[:, :2]))
        paths[sid] = str(path.resolve())
    if len(songs) != result["reference_songs"]:
        raise ValueError("Reference corpus incomplete")
    result["storage_delete_failure"] = "Upstream processDeleteQueue reads storeQueue; stale fingerprints reference deleted metadata. Original 288 runs retained unchanged."
    try:
        run_negatives(args, result, songs, paths, folder)
        summarize_result(result)
    finally:
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-report", type=Path, default=ROOT / "cache/olaf-application.json")
    parser.add_argument("--tracks", type=Path, default=ROOT / "tracks")
    parser.add_argument("--java", type=Path, default=ROOT / "cache/linux-jre/bin/java")
    parser.add_argument("--classes", type=Path, default=ROOT / "cache/engine-panako")
    parser.add_argument("--distro", default="Ubuntu")
    parser.add_argument("--output", type=Path, default=ROOT / "cache/panako-application.json")
    parser.add_argument("--resume-negatives", action="store_true")
    args = parser.parse_args()
    resume_negatives(args) if args.resume_negatives else main(args)
