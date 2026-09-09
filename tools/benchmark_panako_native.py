"""Validate native Panako against the previously recorded WSL scenario manifest.

No WSL process is launched. Recognition is measured anew on the host JVM;
historical timings are not a simultaneous speed comparison.
"""
import argparse
from dataclasses import asdict
import hashlib
import json
import logging
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.panako_native_client import NativePanakoWorker, library_name
from tools.panako_wsl_client import PanakoRecognizer, pcm16
from tools.benchmark_panako_application import Measured, summarize_result
from tools import benchmark_dj_transitions as sim
from tools.compare_recognition_engines import transformed
from vjvision import matcher
from vjvision.config import CaptureConfig, SONG_PATHS_DB
import sqlite3

SR = sim.SR


def main(args):
    previous = json.loads(args.reference_report.read_text())
    if not previous["complete"] or previous["config"] != asdict(CaptureConfig()):
        raise ValueError("Completed reference run with identical configuration required")
    if any(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() != h for p, h in previous["source_sha256"].items()):
        raise ValueError("Production source changed since reference run")
    with sqlite3.connect(SONG_PATHS_DB.resolve().as_uri() + "?mode=ro", uri=True) as con:
        mapping = {str(Path(p).resolve()): sid for sid, p in con.execute("SELECT song_id,file_path FROM songs")}
    songs, paths, pcm = {}, {}, {}
    for path in sorted(args.tracks.iterdir()):
        sid = mapping.get(str(path.resolve()))
        if str(sid) not in previous["song_sha256"] or not path.is_file():
            continue
        if hashlib.sha256(path.read_bytes()).hexdigest() != previous["song_sha256"][str(sid)]:
            raise ValueError("Audio differs from reference")
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
        if audio.shape[1] == 1:
            audio = np.repeat(audio, 2, axis=1)
        if rate != SR:
            audio = resample_poly(audio, SR, rate, axis=0)
        songs[sid], paths[sid] = audio[:, :2], str(path.resolve())
        pcm[sid] = pcm16(songs[sid], SR)
    if len(songs) != previous["reference_songs"]:
        raise ValueError("Incomplete reference corpus")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix="panako-native-", dir=args.output.parent.resolve()))
    def worker():
        return NativePanakoWorker(args.java, args.classes, args.libraries, folder / "worker", folder / "worker.log")
    native = worker()
    result = dict(complete=False, backend="host_jvm_no_wsl", folder=str(folder), config=previous["config"],
        source_sha256=previous["source_sha256"], song_sha256=previous["song_sha256"], reference_songs=len(songs),
        dll_sha256=hashlib.sha256((args.libraries / library_name()).read_bytes()).hexdigest(),
        storage_sha256=hashlib.sha256((args.classes / "be/panako/strategy/panako/storage/PanakoStorageKV.class").read_bytes()).hexdigest(),
        jar_sha256=hashlib.sha256((args.classes / "Panako-2.1-all.jar").read_bytes()).hexdigest(),
        startup_s=native.startup_s, micro=[], runs=[], deletion_checks=[])
    def save():
        args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    save()
    try:
        for sid in songs:
            native.request(1, sid, pcm[sid], timeout=300)
            print(f"indexed {sid}", flush=True)
        probe_sid = next(iter(songs))
        probe = pcm16(songs[probe_sid][70*SR:82*SR], SR)
        before = native.request(2, pcm=probe)["hits"]
        if not before or before[0]["song_id"] != probe_sid:
            raise AssertionError("Stored reference failed to match")
        native.close()
        native = worker()
        after = native.request(2, pcm=probe)["hits"]
        if before != after:
            raise AssertionError("Restart changed results")
        result["restart_identical"] = True
        recognizer = PanakoRecognizer(native, paths)
        for old in previous["micro"]:
            if old["variant"] != "panako_wsl":
                continue
            sid, offset, seconds = old["song_id"], old["offset"], old["seconds"]
            start = time.perf_counter()
            answer = recognizer.match(songs[sid][offset*SR:(offset+seconds)*SR], SR)
            result["micro"].append(dict(song_id=sid, offset=offset, seconds=seconds,
                wall_s=time.perf_counter()-start, predicted=answer.song_id,
                accepted=answer.matched and answer.confidence >= .30,
                reference_predicted=old["predicted"], reference_accepted=old["accepted"],
                hits=answer.raw.get("aligned_candidates", [])))
        save()
        path_ids = {p: sid for sid, p in paths.items()}
        def clip(sid, offset):
            return songs[sid][offset*SR:int((offset+12+sim.DURATION+3)*SR)]
        zero = np.zeros_like(clip(probe_sid, 30))
        for old in previous["runs"]:
            if old["variant"] != "panako_wsl":
                continue
            kind, offset, seed = old["kind"], old["offset"], old["seed"]
            a, b = old["old_id"], old["target_id"]
            simulation_kind = kind
            if kind in ["hard_cut", "fade", "abort"]:
                stream = sim.transition(clip(a, offset), clip(b, offset), kind)
            elif kind == "startup":
                stream = clip(b, offset).copy()
                stream[:12*SR] = 0
            elif kind == "silence":
                stream = zero
            elif kind == "noise":
                stream = np.random.default_rng(seed).normal(0, .03, zero.shape).astype("float32")
            elif kind.startswith("cut_"):
                changed = transformed(songs[b][70*SR:130*SR], kind[4:], SR)[:len(zero)]
                stream = sim.transition(clip(a, offset), changed, "hard_cut")
                simulation_kind = "hard_cut"
            elif kind == "unknown":
                heldout = old["heldout_id"]
                native.request(3, heldout, pcm[heldout], timeout=300)
                # Verify the deletion survives process restart and does not just
                # hide results in memory or a query exclusion list.
                native.close()
                native = worker()
                recognizer.worker = native
                query = native.request(2, pcm=pcm16(songs[heldout][70*SR:82*SR], SR))
                if any(h["song_id"] == heldout for h in query["hits"]):
                    raise AssertionError("Deleted reference still matches")
                result["deletion_checks"].append(heldout)
                stream = clip(heldout, offset)
            elif kind == "unknown_playing":
                stream = sim.transition(clip(a, offset), clip(old["heldout_id"], offset), "hard_cut")
                simulation_kind = "abort"
            else:
                raise ValueError(kind)
            with patch.object(sim, "TimedRecognizer", Measured):
                row = sim.simulate(matcher, CaptureConfig(), lambda _, s, input_sr: recognizer.match(s, input_sr),
                    None, stream, paths.get(a), b, simulation_kind, seed, path_ids)
            row.update(variant="panako_native", kind=kind, offset=offset, heldout_id=old.get("heldout_id"))
            result["runs"].append(row)
            if kind == "unknown_playing":
                heldout = old["heldout_id"]
                native.request(1, heldout, pcm[heldout], timeout=300)
                query = native.request(2, pcm=pcm16(songs[heldout][70*SR:82*SR], SR))
                if not query["hits"] or query["hits"][0]["song_id"] != heldout:
                    raise AssertionError("Restored reference did not match")
            print(f"{len(result['runs'])}/108 {kind}: correct={row['final_correct']} latency={row['latency']}", flush=True)
            save()
        summarize_result(result)
    finally:
        native.close()
        save()


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reference-report", type=Path, default=ROOT / "cache/panako-application.json")
    parser.add_argument("--tracks", type=Path, default=ROOT / "tracks")
    parser.add_argument("--java", type=Path, required=True)
    parser.add_argument("--classes", type=Path, required=True)
    parser.add_argument("--libraries", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "cache/panako-native.json")
    main(parser.parse_args())
