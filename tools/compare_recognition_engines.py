"""Offline engine comparison using local audio and isolated indexes.

Pass a locally built Olaf C core with --olaf. Native subprocess startup and
query PCM serialization are included in its wall time. Audio transformations
are prepared outside timings. No music is uploaded or copied into Git.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from unittest.mock import patch

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly, butter, sosfilt

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from vjvision import fingerprint as fp
from tools.profile_recognition import backup, summary
from tools.benchmark_dj_transitions import source_module


def merge_panako(args):
    report = json.loads(args.output.read_text(encoding="utf-8"))
    reference = {r["key"]: r for r in report["rows"] if r["engine"] == "dejavu"}
    added = {}
    for line in args.panako_log.read_text(encoding="utf-8").splitlines():
        if not line.startswith("RESULT\t"):
            continue
        fields = line.split("\t")
        key = fields[1][:-4]
        if key in added or key not in reference:
            raise ValueError(f"Duplicate or unknown query key: {key}")
        predicted = None if fields[2] == "none" else int(fields[2][10:-4])
        added[key] = {**{k: reference[key][k] for k in ("key", "expected", "kind", "offset", "seconds")},
                      "engine": "panako", "predicted": predicted, "wall_s": float(fields[3]),
                      "score": float(fields[4]), "time_factor": float(fields[5]), "frequency_factor": float(fields[6])}
    if added.keys() != reference.keys():
        raise ValueError("Panako output is incomplete")
    report["rows"] = [r for r in report["rows"] if r["engine"] != "panako"] + list(added.values())
    report["summary"]["panako"] = {}
    for kind, seconds in sorted({(r["kind"], r["seconds"]) for r in added.values()}):
        rows = [r for r in added.values() if r["kind"] == kind and r["seconds"] == seconds]
        report["summary"]["panako"][f"{kind}-{seconds}s"] = dict(cases=len(rows),
            correct=sum(r["predicted"] == r["expected"] for r in rows),
            wrong=sum(r["predicted"] is not None and r["predicted"] != r["expected"] for r in rows),
            wall_s=summary([r["wall_s"] for r in rows]))
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))


def transformed(audio, kind, sr):
    if kind == "speed_up":
        return resample_poly(audio, 100, 106, axis=0)
    if kind == "speed_down":
        return resample_poly(audio, 100, 94, axis=0)
    if kind in {"tempo_up", "pitch_up"}:
        effect = "atempo=1.06" if kind == "tempo_up" else f"asetrate={sr}*1.059463,aresample={sr},atempo=0.943874"
        result = subprocess.run(["ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(sr),
                                 "-ac", "2", "-i", "pipe:0", "-af", effect,
                                 "-f", "f32le", "pipe:1"], input=audio.astype("<f4").tobytes(),
                                capture_output=True, check=True)
        return np.frombuffer(result.stdout, dtype="<f4").reshape(-1, 2).copy()
    if kind == "eq":
        return sosfilt(butter(2, [300, 5000], fs=sr, btype="bandpass", output="sos"), audio, axis=0).astype("float32")
    if kind == "noise":
        rng = np.random.default_rng(8103)
        return audio + rng.normal(0, np.sqrt(np.mean(audio ** 2)) * .1, audio.shape).astype("float32")
    return audio


def main(args):
    if os.environ.get("HOME"):
        raise RuntimeError("The upstream C core uses HOME/.olaf. Use a build configured for an isolated database; refusing to touch that store.")
    baseline = source_module(args.baseline_ref, "fingerprint").FingerprintDB.match_from_array
    folder = args.output.parent.resolve() / "engine-comparison-data"
    folder.mkdir(parents=True, exist_ok=True)
    native_db = folder / "db"
    native_db.mkdir(exist_ok=True)
    # These two exact files belong only to this benchmark's isolated LMDB.
    # Recreate the native index so a prior, larger test set cannot leak in.
    for name in ("data.mdb", "lock.mdb"):
        (native_db / name).unlink(missing_ok=True)
    for source, name in [(fp.FINGERPRINTS_DB, "fingerprints.db"), (fp.SONG_PATHS_DB, "paths.sqlite")]:
        backup(source, folder / name)
    with patch.object(fp, "FINGERPRINTS_DB", folder / "fingerprints.db"), patch.object(fp, "SONG_PATHS_DB", folder / "paths.sqlite"):
        db = fp.FingerprintDB()
    paths = {str(Path(r["file_path"]).resolve()): r["song_id"] for r in db._sqlite.execute("SELECT * FROM songs")}
    songs, skipped = [], []
    manifest = []
    for path in sorted(args.tracks.iterdir()):
        try:
            audio, sr = sf.read(path, dtype="float32", always_2d=True)
        except Exception:
            skipped.append(path.suffix)
            continue
        if str(path.resolve()) not in paths:
            continue
        if audio.shape[1] == 1:
            audio = np.repeat(audio, 2, axis=1)
        audio = resample_poly(audio[:, :2], 44100, sr, axis=0) if sr != 44100 else audio[:, :2]
        sid = paths[str(path.resolve())]
        raw = folder / f"reference-{sid}.raw"
        resample_poly(audio.mean(axis=1), 160, 441).astype("<f4").tofile(raw)
        pcm = raw.with_suffix(".pcm")
        (np.clip(np.fromfile(raw, dtype="<f4"), -1, 1) * 32767).astype("<i2").tofile(pcm)
        manifest.append(f"S\t{pcm.name}")
        songs.append((sid, audio, raw))
    if len(songs) < 2:
        raise RuntimeError("Need two or more readable indexed songs")
    # Equal reference libraries: all other songs are removed only from the backup.
    ids = [s[0] for s in songs]
    with sqlite3.connect(folder / "fingerprints.db") as con:
        placeholders = ",".join("?" for _ in ids)
        con.execute(f"DELETE FROM fingerprints WHERE song_id NOT IN ({placeholders})", ids)
        con.execute(f"DELETE FROM songs WHERE song_id NOT IN ({placeholders})", ids)
    command = [str(args.olaf.resolve()), "store"]
    for sid, _, raw in songs:
        command.extend([str(raw), f"track-{sid}.wav"])
    subprocess.run(command, cwd=folder, capture_output=True, check=True, timeout=300)
    original_align = db._djv.align_matches
    evidence = []

    def align(matches, dedup, total):
        counts = Counter(matches)
        best = {}
        for (sid, offset), count in counts.items():
            count += counts.get((sid, offset - 1), 0) + counts.get((sid, offset + 1), 0)
            if count > best.get(sid, (0, 0))[0]:
                best[sid] = (count, offset)
        evidence[:] = [{"song_id": int(sid), "count": int(count), "ratio": count / total, "offset": int(offset)}
                       for sid, (count, offset) in sorted(best.items(), key=lambda item: item[1][0], reverse=True)[:3]]
        return original_align(matches, dedup, total)

    db._djv.align_matches = align
    rows = []

    def query(audio, expected, kind, offset, seconds):
        audio = audio[:int(seconds * 44100)]
        key = f"query-{len(rows) // 2}"
        pcm = folder / f"{key}.pcm"
        (np.clip(resample_poly(audio.mean(axis=1), 160, 441), -1, 1) * 32767).astype("<i2").tofile(pcm)
        manifest.append(f"Q\t{pcm.name}")
        evidence.clear()
        start = time.perf_counter()
        result = baseline(db, audio)
        elapsed = time.perf_counter() - start
        rows.append(dict(engine="dejavu", key=key, expected=expected, kind=kind, offset=offset, seconds=seconds,
                         predicted=result.song_id if result.matched and result.confidence >= .3 else None,
                         confidence=result.confidence, wall_s=elapsed, aligned=list(evidence)))
        start = time.perf_counter()
        raw = folder / "query.raw"
        resample_poly(audio.mean(axis=1), 160, 441).astype("<f4").tofile(raw)
        proc = subprocess.run([str(args.olaf.resolve()), "query", str(raw), "query.wav"], cwd=folder,
                              capture_output=True, text=True, check=True, timeout=30)
        elapsed = time.perf_counter() - start
        hits = []
        for line in proc.stdout.splitlines():
            fields = [f.strip() for f in line.split(",")]
            if len(fields) == 7 and fields[0].isdigit() and fields[3].startswith("track-"):
                hits.append(dict(song_id=int(fields[3][6:-4]), count=int(fields[0]),
                                 span=float(fields[2]) - float(fields[1])))
        rows.append(dict(engine="olaf", key=key, expected=expected, kind=kind, offset=offset, seconds=seconds,
                         predicted=hits[0]["song_id"] if hits else None, wall_s=elapsed, hits=hits[:5]))

    for sid, audio, _ in songs:
        for offset in args.offsets:
            clip = audio[int(offset * 44100):int((offset + 16) * 44100)]
            if len(clip) < 16 * 44100:
                continue
            for seconds in [2, 4, 6, 12]:
                query(clip, sid, "clean", offset, seconds)
            for kind in ["speed_up", "speed_down", "tempo_up", "pitch_up", "eq", "noise"]:
                query(transformed(clip, kind, 44100), sid, kind, offset, 6)
        print(f"finished song {sid}", flush=True)
    query(np.zeros((6 * 44100, 2), dtype="float32"), None, "silence", 0, 6)
    query(np.random.default_rng(23).normal(0, .05, (6 * 44100, 2)).astype("float32"), None, "random_noise", 0, 6)
    # Leave one reference out of BOTH copies; test three genuine unknown clips.
    held, held_audio, held_raw = songs[-1]
    manifest.append(f"D\t{held_raw.with_suffix('.pcm').name}")
    subprocess.run([str(args.olaf.resolve()), "delete", str(held_raw), f"track-{held}.wav"], cwd=folder,
                   capture_output=True, check=True)
    with sqlite3.connect(folder / "fingerprints.db") as con:
        con.execute("DELETE FROM fingerprints WHERE song_id=?", (held,))
        con.execute("DELETE FROM songs WHERE song_id=?", (held,))
    for offset in args.offsets:
        query(held_audio[int(offset * 44100):], None, "unknown", offset, 6)
    db.close()
    (folder / "manifest.tsv").write_text("\n".join(manifest), encoding="utf-8")
    import hashlib
    report = dict(baseline_ref=args.baseline_ref, song_ids=ids, skipped_extensions=skipped,
                  olaf_binary_sha256=hashlib.sha256(args.olaf.read_bytes()).hexdigest(),
                  rows=rows, summary={})
    for engine in ["dejavu", "olaf"]:
        report["summary"][engine] = {}
        for kind, seconds in sorted({(r["kind"], r["seconds"]) for r in rows}):
            group = [r for r in rows if r["engine"] == engine and r["kind"] == kind and r["seconds"] == seconds]
            report["summary"][engine][f"{kind}-{seconds}s"] = dict(cases=len(group),
                correct=sum(r["predicted"] == r["expected"] for r in group),
                wrong=sum(r["predicted"] is not None and r["predicted"] != r["expected"] for r in group),
                wall_s=summary([r["wall_s"] for r in group]))
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.CRITICAL)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracks", type=Path, default=ROOT / "tracks")
    parser.add_argument("--olaf", type=Path)
    parser.add_argument("--baseline-ref", help="Trusted local Git baseline revision")
    parser.add_argument("--panako-log", type=Path, help="Merge bridge output into an existing comparison report")
    parser.add_argument("--offsets", type=float, nargs="+", default=[30, 70, 110])
    parser.add_argument("--output", type=Path, default=ROOT / "cache" / "engine-comparison.json")
    args = parser.parse_args()
    if args.panako_log:
        merge_panako(args)
    elif args.olaf and args.baseline_ref:
        main(args)
    else:
        parser.error("Provide --olaf and --baseline-ref, or --panako-log")
