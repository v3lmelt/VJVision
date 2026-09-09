"""Prepare comparable native-engine DJ queries and summarize the state changes.

Uses six-second windows every two seconds and two consecutive accepted hits.
The conservative prototype gate needs 20 matches spanning 2 s and a 2x lead.
This is an offline engine prototype, not the application's production filter.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.benchmark_dj_transitions import transition, SR, DURATION


def prepare(args):
    if os.environ.get("HOME"):
        raise RuntimeError("Upstream C core would use HOME/.olaf; refusing to touch that store.")
    folder = args.data.resolve()
    manifest = [f"S\t{p.name}" for p in sorted(folder.glob("reference-*.pcm"))]
    command = [str(args.olaf.resolve()), "store"]
    for path in sorted(folder.glob("reference-*.raw")):
        sid = int(path.stem.split("-")[1])
        command.extend([str(path), f"track-{sid}.wav"])
    subprocess.run(command, cwd=folder, capture_output=True, check=True)
    import sqlite3
    from vjvision.config import SONG_PATHS_DB
    with sqlite3.connect(SONG_PATHS_DB.resolve().as_uri() + "?mode=ro", uri=True) as db:
        paths = {str(Path(p).resolve()): sid for sid, p in db.execute("SELECT song_id,file_path FROM songs")}
    songs = []
    for path in sorted(args.tracks.glob("*.mp3")):
        audio, rate = sf.read(path, dtype="float32", always_2d=True)
        if rate != SR:
            audio = resample_poly(audio, SR, rate, axis=0)
        songs.append((paths[str(path.resolve())], audio))
    scenarios = []
    for offset in [30, 70]:
        for i, (old, a) in enumerate(songs):
            target, b = songs[(i + 1) % len(songs)]
            for kind in ["hard_cut", "fade", "abort"]:
                start, stop = offset * SR, int((offset + 12 + DURATION) * SR)
                stream = transition(a[start:stop], b[start:stop], kind)
                scenario = dict(old=old, target=target, offset=offset, kind=kind, queries=[])
                for at in range(0, int(DURATION), 2):
                    clip = stream[(at + 6) * SR:(at + 12) * SR]
                    mono = resample_poly(clip.mean(axis=1), 1, 3)
                    key = f"dj-{len(scenarios)}-{at}"
                    (np.clip(mono, -1, 1) * 32767).astype("<i2").tofile(folder / f"{key}.pcm")
                    manifest.append(f"Q\t{key}.pcm")
                    mono.astype("<f4").tofile(folder / "dj-query.raw")
                    output = subprocess.run([str(args.olaf.resolve()), "query", str(folder / "dj-query.raw"), "query.wav"],
                                            cwd=folder, capture_output=True, text=True, check=True, timeout=30).stdout
                    by_song = {}
                    for line in output.splitlines():
                        fields = [x.strip() for x in line.split(",")]
                        if len(fields) != 7 or not fields[0].isdigit() or not fields[3].startswith("track-"):
                            continue
                        sid = int(fields[3][6:-4])
                        hit = dict(song=sid, count=int(fields[0]), span=float(fields[2]) - float(fields[1]))
                        if sid not in by_song or hit["count"] > by_song[sid]["count"]:
                            by_song[sid] = hit
                    hits = sorted(by_song.values(), key=lambda h: -h["count"])
                    best = hits[0] if hits else dict(song=None, count=0, span=0)
                    best["runner"] = hits[1]["count"] if len(hits) > 1 else 0
                    scenario["queries"].append(dict(key=key, at=at, olaf=best))
                scenarios.append(scenario)
            print(f"prepared transitions {offset}/{old}", flush=True)
    (folder / "dj-manifest.tsv").write_text("\n".join(manifest), encoding="utf-8")
    args.output.write_text(json.dumps(dict(scenarios=scenarios), indent=2), encoding="utf-8")


def summarize(args):
    data = json.loads(args.output.read_text(encoding="utf-8"))
    panako = {}
    for line in args.panako_log.read_text(encoding="utf-8").splitlines():
        if not line.startswith("RESULT\t"):
            continue
        fields = line.split("\t")
        panako[fields[1][:-4]] = dict(song=None if fields[2] == "none" else int(fields[2][10:-4]),
            count=float(fields[4]), runner=float(fields[7]), span=float(fields[8]))
    results = []
    for scenario in data["scenarios"]:
        for engine in ["olaf", "panako"]:
            current, pending, votes = scenario["old"], None, 0
            events = []
            for query in scenario["queries"]:
                hit = query["olaf"] if engine == "olaf" else panako[query["key"]]
                valid = hit["count"] >= 20 and hit["span"] >= 2 and hit["count"] >= 2 * hit["runner"]
                candidate = hit["song"] if valid else None
                if candidate is None or candidate == current:
                    pending, votes = None, 0
                    continue
                votes = votes + 1 if candidate == pending else 1
                pending = candidate
                if votes >= 2:
                    current, pending, votes = candidate, None, 0
                    events.append(dict(at=query["at"], song=current))
            target, old, kind = scenario["target"], scenario["old"], scenario["kind"]
            reference = 9 if kind == "fade" else 5
            target_events = [e for e in events if e["song"] == target]
            results.append(dict(engine=engine, kind=kind, offset=scenario["offset"], old=old, target=target,
                final_correct=current == (old if kind == "abort" else target), events=events,
                latency=target_events[0]["at"] - reference if target_events else None,
                unexpected=sum(e["song"] != old for e in events) if kind == "abort" else
                    sum(e["song"] not in {old, target} or (e["song"] == target and e["at"] < reference) for e in events)))
    data["results"] = results
    args.output.write_text(json.dumps(data, indent=2), encoding="utf-8")
    for engine in ["olaf", "panako"]:
        for kind in ["hard_cut", "fade", "abort"]:
            rows = [r for r in results if r["engine"] == engine and r["kind"] == kind]
            latencies = [r["latency"] for r in rows if r["latency"] is not None]
            print(engine, kind, "correct", sum(r["final_correct"] for r in rows), "/", len(rows),
                  "unexpected", sum(r["unexpected"] for r in rows),
                  "median", float(np.median(latencies)) if latencies else None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tracks", type=Path, default=ROOT / "tracks")
    parser.add_argument("--data", type=Path, default=ROOT / "cache" / "engine-comparison-data")
    parser.add_argument("--olaf", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "cache" / "native-dj.json")
    parser.add_argument("--panako-log", type=Path)
    args = parser.parse_args()
    summarize(args) if args.panako_log else prepare(args)
