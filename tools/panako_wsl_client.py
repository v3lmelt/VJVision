"""Persistent, local-only WSL Panako worker for offline benchmarks."""
from __future__ import annotations
import json
from pathlib import Path
import queue
import struct
import subprocess
import threading
import time

import numpy as np
from tools.benchmark_olaf_application import mono16, group_hits, verdict


def wsl_path(path):
    path = Path(path).resolve()
    if not path.drive or path.drive.startswith("\\"):
        raise ValueError("A local Windows drive path is required")
    return "/mnt/" + path.drive[0].lower() + path.as_posix()[2:]


class PanakoWorker:
    def __init__(self, distro, java, classes, database, log, *, command=None, cwd=None):
        self.log = open(log, "ab")
        self.responses = queue.Queue()
        start = time.perf_counter()
        self.process = subprocess.Popen(
            command or ["wsl", "-d", distro, "--cd", database, "--exec", wsl_path(java),
             "-Xmx2g", "-cp", wsl_path(classes / "Panako-2.1-all.jar") + ":" + wsl_path(classes),
             "PanakoStreamServer", database + "/db"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
            cwd=cwd, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        def reader():
            try:
                for line in self.process.stdout:
                    self.responses.put(json.loads(line))
            except Exception as exc:
                self.responses.put(exc)
            finally:
                self.responses.put(RuntimeError("Panako worker closed its output; inspect the worker log"))
        threading.Thread(target=reader, daemon=True).start()
        try:
            if not self.receive(90).get("ready"):
                raise RuntimeError("Invalid worker handshake")
        except Exception:
            self.close()
            raise
        self.startup_s = time.perf_counter() - start

    def receive(self, timeout):
        try:
            answer = self.responses.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError("Panako response timed out") from None
        if isinstance(answer, Exception):
            raise answer
        return answer

    def request(self, operation, sid=0, pcm=b"", timeout=60):
        start = time.perf_counter()
        self.process.stdin.write(struct.pack(">iii", operation, sid, len(pcm)))
        self.process.stdin.write(pcm)
        self.process.stdin.flush()
        answer = self.receive(timeout)
        answer["roundtrip_s"] = time.perf_counter() - start
        return answer

    def close(self):
        if self.process.poll() is None:
            try:
                self.process.stdin.write(struct.pack(">i", 4))
                self.process.stdin.close()
                self.process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                self.process.kill()
                self.process.wait(timeout=10)
        self.log.close()


def pcm16(samples, sr):
    return (np.clip(mono16(samples, sr), -1, 1) * 32767).astype("<i2").tobytes()


def aligned_hits(hits, seconds):
    results = []
    for h in hits:
        factor = h["time_factor"]
        if not np.isfinite(factor) or factor <= 0:
            raise ValueError("Invalid Panako time factor")
        results.append({**h, "span": h["query_stop"] - h["query_start"],
            # Panako's factor is query duration / reference duration. Present
            # the reference position at the window end to the existing app.
            "offset_seconds": h["ref_start"] + (seconds - h["query_start"]) / factor - seconds,
            # Native temporal coverage, NOT Dejavu's unique fingerprint ratio.
            "ratio": h["matched_seconds_fraction"]})
    return group_hits(results)


class PanakoRecognizer:
    def __init__(self, worker, paths):
        self.worker, self.paths = worker, paths
        self.last = {}

    def match(self, samples, input_sr):
        if not samples.size or not np.any(samples):
            self.last = {}
            return verdict([], self.paths)
        answer = self.worker.request(2, pcm=pcm16(samples, input_sr))
        self.last = answer
        hits = aligned_hits(answer["hits"], len(samples) / input_sr)
        result = verdict(hits, self.paths)
        result.raw.update(evidence_kind="panako_native_votes_and_temporal_coverage",
                          server_s=answer["server_s"], roundtrip_s=answer["roundtrip_s"])
        return result
