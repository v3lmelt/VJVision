"""Matcher thread - the orchestration hub.

Responsibilities:
* Owns an :class:`AudioCapture` instance (USB soundcard input).
* Polls a ``cmd_queue`` for control messages from the debug UI.
* Periodically snapshots the captured audio and runs dejavu on it.
* When a match is found, reads tags + cover art via :mod:`mutagen` and pushes
  a ``track`` message to the visualizer process (and the UI).
* Forwards every spectrum callback (~30 Hz) to the visualizer process.
"""
from __future__ import annotations

import logging
import queue as _queue
import random
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from .audio_capture import AudioCapture
from .config import SETTINGS
from .fingerprint import FingerprintDB, MatchResult
from .metadata import Track, extract_track

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    import numpy as np


class MatcherThread(threading.Thread):
    """Glues audio capture, dejavu, metadata and the visualizer together."""

    def __init__(
        self,
        cmd_queue: "_queue.Queue",
        viz_queue: "_queue.Queue",
        ui_queue: "_queue.Queue",
        viz_mgr=None,
    ) -> None:
        super().__init__(daemon=True, name="Matcher")
        self.cmd = cmd_queue
        self.viz = viz_queue
        self.ui = ui_queue
        self.viz_mgr = viz_mgr
        self._stop_event = threading.Event()
        self._capture: Optional[AudioCapture] = None
        self._fp: Optional[FingerprintDB] = None
        # Boot-time monitor opens on the saved device (AudioCapture
        # falls back to the same SETTINGS value when given None).
        self._device_index: Optional[int] = SETTINGS.audio_device
        self._capture_running = False
        self._current_track_path: Optional[str] = None
        # Track-change confirmation filter: require N strong hits on one
        # consistent candidate within a bounded time before switching.
        # Kills false positives from brief noise or DJ transitions.
        self._pending_path: Optional[str] = None
        self._pending_hits: int = 0
        self._pending_since: float = 0.0
        self._pending_position: Optional[tuple[float, float]] = None
        # Hold the current artwork while a different candidate is being
        # confirmed. Recovery of the current track uses the same evidence rule.
        self._in_mix: bool = False
        # Periodic verification scheduling: we add random jitter to the
        # match interval so checks aren't rhythmically predictable.
        self._next_match_ts: float = 0.0
        # Cumulator for batched prepare_files messages (UI sends in 500-file
        # chunks to stay under the multiprocessing pipe's 64KB buffer limit).
        self._prepare_pending: list[str] = []

    # -- helpers ----------------------------------------------------------
    def _log(self, text: str, level: str = "info") -> None:
        getattr(log, level, log.info)(text)
        self._send_ui({"type": "log", "text": text})

    def _viz_restart_and_resync(self) -> None:
        """Restart the visualizer, then push the current track back to it.

        A freshly-spawned visualizer process has no memory of what was
        playing — it starts at the standby screen.  After the restart
        completes we re-extract the track metadata and send a ``track``
        message so the new instance jumps straight to the current song
        instead of lingering on standby until the next recognition hit.
        """
        self.viz_mgr.restart()
        if self._current_track_path is not None:
            try:
                from .metadata import extract_track
                t = extract_track(self._current_track_path)
                self._send_viz({
                    "type": "track",
                    "title": t.title,
                    "artist": t.artist,
                    "album": t.album,
                    "cover": t.cover_path,
                    "details": t.details,
                    "_resync": True,   # hint for the visualizer
                    "tentative": self._in_mix,
                })
                log.info("Re-synced current track to restarted viz: %s", t.title)
            except Exception as exc:
                log.warning("viz resync failed: %s", exc)

    def _send_viz(self, msg: dict) -> None:
        """Forward a message to the visualizer process.

        Drops silently if the visualizer process is dead — we don't want
        to fill a dead queue with spectrum messages and eventually block
        the matcher thread (which would freeze recognition).
        """
        # Hard queue-full is still a drop-and-warn, same as before.
        # But if the process itself has died we skip putting entirely.
        if self.viz_mgr is not None and not self.viz_mgr.alive:
            return
        try:
            self.viz.put_nowait(msg)
        except _queue.Full:
            # Never block here — the matcher thread must keep running
            # even if the visualizer is slow or dead.
            pass

    def _send_ui(self, msg: dict) -> None:
        try:
            self.ui.put_nowait(msg)
        except _queue.Full:
            log.warning("ui_queue full - dropping %s", msg.get("type"))

    # -- spectrum forwarder (called from the audio thread) ---------------
    def _on_spectrum(self, bins, peak) -> None:
        # Convert numpy array to plain list so multiprocessing pickles cheaply.
        capture = self._capture
        level = capture.current_level() if capture is not None else {}
        self._send_viz({
            "type": "spectrum",
            "bins": bins.tolist(),
            "peak": float(peak),
            "rms": float(level.get("rms", peak)),
            "input_peak": float(level.get("peak", peak)),
            "sample_rate": capture.sr if capture is not None else 0,
            "channels": capture.channels if capture is not None else 0,
        })

    # -- fingerprint DB lifecycle ---------------------------------------
    def _ensure_fp(self) -> None:
        if self._fp is not None:
            return
        self._repair_thread: Optional[threading.Thread] = None
        try:
            self._fp = FingerprintDB()
            stats = self._fp.stats()
            missing = getattr(self._fp, "_missing_hashes", [])
            self._log(
                f"dejavu connected. {stats['songs']} songs indexed. "
                f"DB sync found {len(missing)} missing-hash files."
            )
            self._send_ui({"type": "index_done", "songs": stats["songs"]})

            # Auto-repair orphaned songs: SQLite had path entries but MySQL
            # had no fingerprints.  Run them through index_files silently
            # in a background thread so recognition isn't blocked.
            if missing:
                self._log(
                    f"Auto-repairing {len(missing)} orphaned tracks "
                    f"(including songs like 'Stars Align' that exist on disk "
                    f"but weren't in the DB)...", "warning"
                )
                def _repair_worker():
                    try:
                        from .config import save_prefs
                        repaired = self._fp.index_files(
                            missing, progress=None,
                        )
                        self._log(
                            f"Auto-repair done: {repaired}/{len(missing)} "
                            f"orphaned tracks now fully indexed."
                        )
                        save_prefs()  # ensure post-repair state is captured
                        self._send_ui({"type": "library_status",
                                       **self._fp.library_status()})
                    except Exception as exc:
                        self._log(f"Auto-repair failed: {exc}", "error")
                self._repair_thread = threading.Thread(
                    target=_repair_worker, daemon=True, name="DBRepair",
                )
                self._repair_thread.start()
        except Exception as exc:
            self._log(f"dejavu init failed: {exc}", "error")
            self._fp = None

    # -- audio capture lifecycle ----------------------------------------
    # Two distinct modes share one InputStream:
    #   * MONITOR  - stream open, level meter live, spectrum + recognition
    #                OFF.  Starts automatically at boot (and on every
    #                device change) so the operator can verify signal
    #                levels BEFORE going live.
    #   * CAPTURE  - monitor + spectrum to the visualizer + dejavu
    #                recognition loop.  Toggled by the Start/Stop buttons;
    #                stopping capture returns to monitor mode (the stream
    #                stays open, no audio-device re-open click).
    def _reconfigure_capture(self, device_index) -> None:
        """(Re)build the AudioCapture object and reopen the stream.

        The stream always comes up in MONITOR mode; callers that want
        recognition (``_capture_running``) re-enable it via
        :meth:`_start_capture`.
        """
        self._reset_transition()
        if self._capture is not None:
            self._capture.stop()
        self._capture = AudioCapture(
            device=device_index, on_spectrum=self._on_spectrum,
        )
        # New object defaults to monitor (spectrum gated off).
        self._capture.spectrum_enabled = self._capture_running
        self._open_stream_monitor()

    def _open_stream_monitor(self) -> bool:
        """Open the InputStream for level metering. Errors are non-fatal:
        a missing/busy device at boot just leaves the meter at zero and
        Start Capture will retry."""
        if self._capture is None:
            return False
        try:
            self._capture.start()
            return True
        except Exception as exc:
            self._log(
                f"Input monitor unavailable (device {self._device_index}): "
                f"{exc}. Pick another device or press Start to retry.",
                "error",
            )
            return False

    def _start_monitor(self) -> None:
        """Boot-time monitor: build the capture for the saved device and
        open the stream without starting recognition."""
        if self._capture is None:
            self._reconfigure_capture(self._device_index)
        else:
            self._open_stream_monitor()

    def _start_capture(self) -> None:
        if self._capture is None:
            self._reconfigure_capture(self._device_index)
        if self._capture is None:
            self._log("No audio capture available.", "error")
            return
        try:
            # Stream may have failed to open at boot (device was busy or
            # missing) — retry now.
            if self._capture.current_level().get("active") is False:
                self._capture.start()
            self._capture.spectrum_enabled = True
            if not self._capture_running:
                self._next_match_ts = 0.0
            self._capture_running = True
            self._send_ui({"type": "capture_status", "text": "采集：运行中"})
            self._send_viz({"type": "status", "text": "Capture started"})
            self._log("Capture started.")
        except Exception as exc:
            self._log(f"Capture start failed: {exc}", "error")

    def _stop_capture(self) -> None:
        # Return to MONITOR mode: recognition + spectrum off, but the
        # stream stays open so the level meter keeps working.
        self._capture_running = False
        self._reset_transition()
        if self._capture is not None:
            try:
                self._capture.spectrum_enabled = False
            except Exception:
                pass
        self._send_ui({"type": "capture_status",
                       "text": "采集：已停止（监听输入中）"})

    def _close_capture(self) -> None:
        """Fully close the InputStream (shutdown only)."""
        if self._capture is not None:
            try:
                self._capture.stop()
            except Exception:
                pass
            self._capture = None
        self._capture_running = False

    # -- indexing --------------------------------------------------------
    def _index_library(self) -> None:
        self._ensure_fp()
        if self._fp is None:
            self._log("Cannot index: dejavu not connected.", "error")
            return

        def progress(done, total, info):
            self._send_ui({
                "type": "index_progress",
                "done": done, "total": total, "info": info,
            })
            if done % 50 == 0:
                self._log(f"[{done}/{total}] {info}")

        def worker():
            self._log("Indexing library - this may take a while...")
            try:
                new = self._fp.index_library(progress)
                stats = self._fp.stats()
                self._send_ui({"type": "index_done", "songs": stats["songs"]})
                self._send_ui({"type": "library_status",
                               **self._fp.library_status()})
                self._log(f"Indexing done: {new} new, total {stats['songs']}.")
            except Exception as exc:
                self._log(f"Indexing failed: {exc}", "error")

        threading.Thread(target=worker, daemon=True, name="Indexer").start()

    def _force_reindex(self) -> None:
        """Clear MySQL + SQLite, then re-index everything from scratch."""
        self._ensure_fp()
        if self._fp is None:
            self._log("Cannot re-index: dejavu not connected.", "error")
            return

        # If the startup auto-repair thread is still running, wait for it
        # to finish before we wipe the DB — otherwise the two threads
        # race (repair inserts fingerprints while we TRUNCATE, and the
        # drained connection pool can invalidate repair's cursors).
        repair = getattr(self, "_repair_thread", None)
        if repair is not None and repair.is_alive():
            self._log("Waiting for in-progress auto-repair to finish...")
            repair.join(timeout=120)

        def progress(done, total, info):
            self._send_ui({
                "type": "index_progress",
                "done": done, "total": total, "info": info,
            })
            if done % 50 == 0 or "Cleared" in info or "Re-indexing" in info:
                self._log(f"[{done}/{total}] {info}")

        def worker():
            self._log("⚠ Force re-index: clearing ALL fingerprints...", "warning")
            try:
                new = self._fp.force_reindex(progress)
                stats = self._fp.stats()
                self._send_ui({"type": "index_done", "songs": stats["songs"]})
                self._send_ui({"type": "library_status",
                               **self._fp.library_status()})
                self._log(f"✅ Re-index done: {new} new, total {stats['songs']}.")
            except Exception as exc:
                self._log(f"Re-index failed: {exc}", "error")

        threading.Thread(target=worker, daemon=True, name="ForceReindexer").start()

    def _prepare_files(self, paths: list[str]) -> None:
        """Semi-automatic preparation: fingerprint just the picked files."""
        if not paths:
            self._log("Nothing to prepare - queue is empty.")
            return
        # Guard against double-clicking "分析队列" which would spawn two
        # concurrent indexing pools (and two sets of black console windows).
        if getattr(self, "_preparing", False):
            self._log("已有索引任务在运行中，忽略重复的分析请求。", "warning")
            return
        self._preparing = True
        self._ensure_fp()
        if self._fp is None:
            self._log("Cannot prepare: dejavu not connected.", "error")
            self._preparing = False
            return

        def progress(done, total, info):
            self._send_ui({
                "type": "index_progress",
                "done": done, "total": total, "info": info,
            })
            self._log(f"[{done}/{total}] {info}")

        def worker():
            self._log(f"Preparing {len(paths)} file(s)...")
            try:
                new = self._fp.index_files(paths, progress)
                stats = self._fp.stats()
                self._send_ui({
                    "type": "prep_done",
                    "new": new, "total": len(paths),
                })
                self._send_ui({"type": "index_done", "songs": stats["songs"]})
                self._send_ui({"type": "library_status",
                               **self._fp.library_status()})
                self._log(f"Prepared {new} new of {len(paths)}.")
            except Exception as exc:
                self._log(f"Preparation failed: {exc}", "error")
                self._send_ui({"type": "prep_done", "new": 0,
                               "total": len(paths), "error": str(exc)})
            finally:
                self._preparing = False

        threading.Thread(target=worker, daemon=True, name="Preparer").start()

    def _query_library_status(self) -> None:
        """Push the prepared/pending counts to the UI without re-indexing."""
        self._ensure_fp()
        if self._fp is None:
            return
        try:
            status = self._fp.library_status()
            self._send_ui({"type": "library_status", **status})
        except Exception as exc:
            self._log(f"Status query failed: {exc}", "error")

    # -- matching --------------------------------------------------------
    def _clear_pending_match(self) -> None:
        """Discard expired, conflicting or interrupted recognition evidence."""
        self._pending_path = None
        self._pending_hits = 0
        self._pending_since = 0.0
        self._pending_position = None

    def _set_mix_state(self, active: bool) -> None:
        """Keep the visualizer's pulse in sync without changing the track."""
        if self._in_mix == active:
            return
        self._in_mix = active
        if self._current_track_path is None:
            return
        self._log("Mix detected — holding current track." if active
                  else "Mix ended — current track display restored.")
        self._send_viz({"type": "status", "text": "Mixing…" if active else "Matched"})
        try:
            track = extract_track(self._current_track_path)
        except Exception:
            track = Track(self._current_track_path, "", "", "", None)
        self._send_viz({
            "type": "track",
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "cover": track.cover_path,
            "details": track.details,
            "tentative": active,
        })

    def _reset_transition(self) -> None:
        """Preserve artwork, but never carry evidence across capture sessions."""
        self._clear_pending_match()
        self._set_mix_state(False)
        self._next_match_ts = 0.0

    def _match_snapshot(self, snapshot: "np.ndarray", sample_rate: int) -> MatchResult:
        """Prefer aligned recent evidence; verify uncertain switches over 12 s.

        Both queries observe the same snapshot and produce a single result
        for the confirmation filter. Conflicting older evidence must not
        replace a plausible candidate in the recent audio.
        """
        cfg = SETTINGS.capture
        frames = max(1, int(cfg.fast_match_seconds * sample_rate))
        recent = snapshot[-frames:]
        short = self._fp.match_from_array(recent, input_sr=sample_rate)
        threshold = (cfg.first_track_confidence if self._current_track_path is None
                     else cfg.match_confidence)
        accepted = short.matched and short.file_path and short.confidence >= threshold
        switching = (self._current_track_path is not None
                     and short.file_path != self._current_track_path)
        if ((accepted and not switching) or len(snapshot) <= frames):
            return short
        aligned = short.raw.get("aligned_candidates", [])
        if accepted and switching and aligned and aligned[0]["song_id"] == short.song_id:
            best = aligned[0]
            runner = aligned[1]["count"] if len(aligned) > 1 else 0
            if (best["count"] >= cfg.match_aligned_min_hashes
                    and best["ratio"] >= cfg.match_aligned_min_ratio
                    and best["span"] >= cfg.match_aligned_min_span
                    and best["count"] >= cfg.match_aligned_margin * runner):
                # Track the estimated position at the end of the snapshot,
                # so confirmation can reject jumps between repeated sections.
                return replace(short, raw={**short.raw,
                    "recent_position": best["offset_seconds"] + len(recent) / sample_rate})
        # A silent full buffer needs no second query. The fingerprint path
        # also guards silence, including callers outside this matcher.
        if not snapshot.any():
            return short
        fallback = self._fp.match_from_array(snapshot, input_sr=sample_rate)
        plausible_short = (short.matched and short.file_path
                           and short.confidence >= cfg.mix_candidate_confidence)
        if plausible_short and fallback.file_path != short.file_path:
            return replace(short, raw={**short.raw, "window_agrees": False})
        if accepted and switching:
            # Short clips can identify a briefly introduced secondary deck.
            # A switch also needs accepted support from the longer window;
            # return its actual score rather than inflating a short hit.
            if fallback.matched:
                return fallback
            return replace(short, raw={**short.raw, "window_agrees": False})
        if fallback.matched and fallback.file_path and fallback.confidence > short.confidence:
            return fallback
        return short

    def _schedule_next_match(self, started_at: float) -> None:
        """Schedule from the updated state, allowing at least 1 s of new audio."""
        cfg = SETTINGS.capture
        candidate = (self._current_track_path is None or self._in_mix
                     or self._pending_path is not None)
        interval = cfg.match_candidate_interval if candidate else cfg.match_interval
        jitter = max(0.0, min(0.3, cfg.match_jitter_ratio))
        delay = max(1.0, interval * random.uniform(1.0 - jitter, 1.0 + jitter))
        # Slow recognition must still yield to commands and level updates.
        self._next_match_ts = max(started_at + delay, time.monotonic() + 0.05)

    def _run_match(self) -> None:
        if not self._capture_running or self._capture is None:
            return
        self._ensure_fp()
        if self._fp is None:
            self._clear_pending_match()
            return
        # Match in memory; the audio capture's separate spectrum path keeps
        # driving the visualizer regardless of the recognition result.
        try:
            snapshot = self._capture.snapshot()
        except Exception as exc:
            self._clear_pending_match()
            self._log(f"Snapshot failed: {exc}", "error")
            return
        self._send_viz({"type": "status", "text": "Matching..."})
        try:
            result = self._match_snapshot(snapshot, self._capture.sr)
        except Exception as exc:
            self._clear_pending_match()
            self._log(f"Match raised: {exc}", "error")
            return

        if not result.matched or not result.file_path:
            self._clear_pending_match()
            # Retain existing artwork through silence or an unknown track.
            self._send_viz({"type": "status", "text": "No match"})
            return

        cfg = SETTINGS.capture
        now = time.monotonic()
        # Large configured confirmation counts also need enough time for
        # their minimum number of fresh snapshots.
        evidence_seconds = max(
            cfg.match_confirmation_seconds,
            (max(1, cfg.match_confirmations) - 1) * cfg.match_candidate_interval
            * (1 + max(0.0, min(0.3, cfg.match_jitter_ratio))) + 0.5,
        )
        if self._pending_path is not None and now - self._pending_since > evidence_seconds:
            self._clear_pending_match()
        accept_threshold = (
            cfg.first_track_confidence if self._current_track_path is None
            else cfg.match_confidence
        )
        window_agrees = result.raw.get("window_agrees", True)
        if result.confidence < accept_threshold or not window_agrees:
            # A weak hit adds no vote. Only the same plausible candidate
            # may preserve unexpired evidence; conflicts/noise reset it.
            if (not window_agrees or result.file_path != self._pending_path
                    or result.confidence < cfg.mix_candidate_confidence):
                self._clear_pending_match()
            if (result.confidence >= cfg.mix_candidate_confidence
                    and self._current_track_path is not None
                    and result.file_path != self._current_track_path):
                self._set_mix_state(True)
            # A weak hit on the outgoing song is not evidence that a mix
            # has ended. Wait for enough recent strong hits on either song.
            self._send_viz({
                "type": "status",
                "text": "Mixing…" if self._in_mix else "Listening…",
            })
            return

        is_current = result.file_path == self._current_track_path
        if is_current and not self._in_mix:
            self._clear_pending_match()
            return
        if self._current_track_path is not None and not is_current:
            self._set_mix_state(True)

        # Count each result exactly once, including during a cross-fade.
        # The initial track keeps the existing fast startup behavior.
        confirm_needed = (
            1 if self._current_track_path is None
            else max(1, cfg.match_confirmations)
        )
        position = result.raw.get("recent_position")
        if (result.file_path == self._pending_path and position is not None
                and self._pending_position is not None):
            previous_position, previous_at = self._pending_position
            if abs((position - previous_position) - (now - previous_at)) > cfg.match_position_tolerance:
                self._clear_pending_match()
        if result.file_path == self._pending_path:
            self._pending_hits += 1
        else:
            self._pending_path = result.file_path
            self._pending_hits = 1
            self._pending_since = now
        self._pending_position = (position, now) if position is not None else None
        if self._pending_hits < confirm_needed:
            log.info(
                "Pending match (%d/%d): %s (conf=%.2f)",
                self._pending_hits, confirm_needed,
                Path(result.file_path).name, result.confidence,
            )
            self._send_viz({
                "type": "status",
                "text": f"Pending ({self._pending_hits}/{confirm_needed})",
            })
            return

        self._clear_pending_match()
        if is_current:
            self._set_mix_state(False)
            return

        try:
            track = extract_track(result.file_path)
        except Exception as exc:
            self._log(f"Metadata read failed: {exc}", "error")
            track = Track(result.file_path, "", "", "", None)
        self._current_track_path = result.file_path
        self._in_mix = False
        self._log(f"Match: {track.title} (conf={result.confidence:.2f})")
        self._send_viz({
            "type": "track",
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "cover": track.cover_path,
            "details": track.details,
        })
        self._send_ui({
            "type": "track",
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "confidence": result.confidence,
        })

    # -- command dispatch ------------------------------------------------
    def _handle_cmd(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "quit":
            self._stop_event.set()
        elif mtype == "viz_restart":
            if self.viz_mgr is not None:
                # Run in a background thread — restart() has a sleep(0.3)
                # and we don't want to block the matcher's main loop.
                threading.Thread(
                    target=self._viz_restart_and_resync,
                    daemon=True, name="VizRestart",
                ).start()
                self._log("Restarting visualizer...")
        elif mtype == "viz_reset":
            if self.viz_mgr is not None:
                self.viz_mgr.reset()
                self._log("Resetting visualizer state...")
        elif mtype == "device":
            idx = msg.get("index")
            # The UI re-announces the current device on every device-list
            # refresh (including at boot, right after our monitor stream
            # already opened).  Skip the close/reopen cycle when the
            # selection is unchanged and the stream is already live.
            already_live = (
                self._capture is not None
                and self._capture.current_level().get("active")
            )
            if idx == self._device_index and already_live:
                pass
            else:
                self._device_index = idx
                self._reconfigure_capture(idx)
                if self._capture_running:
                    self._start_capture()
        elif mtype == "start":
            self._start_capture()
        elif mtype == "stop":
            self._stop_capture()
        elif mtype == "music_dir":
            SETTINGS.music_dir = Path(msg.get("path", SETTINGS.music_dir))
        elif mtype == "index_library":
            self._index_library()
        elif mtype == "force_reindex":
            self._force_reindex()
        elif mtype == "prepare_files":
            # Legacy single-shot path — kept for backwards compatibility
            # with anything else that might call us.  Goes straight to
            # indexing, no batching.
            self._prepare_files(msg.get("paths", []))
        elif mtype == "prepare_files_batch":
            # Accumulate 500-file chunks from the UI until the finalising
            # ``prepare_files_done`` message arrives below.
            chunk = msg.get("paths", [])
            if chunk:
                self._prepare_pending.extend(chunk)
        elif mtype == "prepare_files_done":
            # All batches received — de-dup, then fire the real index pass.
            if self._prepare_pending:
                unique = list(dict.fromkeys(self._prepare_pending))
                self._prepare_pending = []
                self._prepare_files(unique)
            else:
                self._log("No files received in any batch — nothing to prepare.")
        elif mtype == "cancel_indexing":
            self._prepare_pending = []  # drop any partially-accumulated batches
            if self._fp is not None:
                self._fp.cancel_indexing()
            self._log("[cancel] Indexing cancelled by user.")
        elif mtype == "library_status":
            self._query_library_status()
        elif mtype == "settings":
            # Update our shared SETTINGS so the changes persist on restart.
            if "style" in msg:
                SETTINGS.visual.spectrum_style = msg["style"]
            if "rotation_speed" in msg:
                SETTINGS.visual.rotation_speed = msg["rotation_speed"]
            if "beat_reactive" in msg:
                SETTINGS.visual.beat_reactive = msg["beat_reactive"]
            if "fullscreen" in msg:
                SETTINGS.visual.fullscreen = bool(msg["fullscreen"])
            if "bg_mode" in msg:
                SETTINGS.visual.bg_mode = msg["bg_mode"]
            if msg.get("theme") in {"pastel", "classic"}:
                SETTINGS.visual.theme = msg["theme"]
            if "font_name" in msg:
                SETTINGS.visual.font_name = str(msg["font_name"])
            if "standby_image" in msg:
                SETTINGS.visual.standby_image = str(msg["standby_image"])
            self._send_viz(msg)   # forward to the visualizer process.

    # -- main loop -------------------------------------------------------
    def run(self) -> None:
        self._log("Matcher thread started.")
        last_level_ts = 0.0
        last_viz_check_ts = 0.0
        level_period = 0.1        # 10 fps level updates to the UI
        viz_check_period = 5.0    # check viz process liveness every 5s

        # Open the input stream in MONITOR mode right away: the operator
        # must be able to verify signal levels on the selected soundcard
        # BEFORE pressing Start Capture.  Failures are logged but never
        # block the matcher thread.
        try:
            self._start_monitor()
        except Exception as exc:
            self._log(f"Input monitor failed to start: {exc}", "error")

        while not self._stop_event.is_set():
            # Drain the command queue.
            try:
                while True:
                    self._handle_cmd(self.cmd.get_nowait())
            except _queue.Empty:
                pass

            now = time.monotonic()
            # --- Viz liveness check ---
            # If the visualizer died (ESC pressed, SDL crash, etc.), we
            # notify the UI so the operator can restart it via the button.
            if (self.viz_mgr is not None and
                    now - last_viz_check_ts >= viz_check_period):
                last_viz_check_ts = now
                if not self.viz_mgr.alive:
                    self._send_ui({
                        "type": "viz_status",
                        "text": "⚠ 可视化进程已退出 — 请点击『🔄 重启可视化窗口』按钮",
                    })

            # Recompute the deadline AFTER recognition so a newly detected
            # candidate gets the faster interval immediately.
            if self._capture_running and now >= self._next_match_ts:
                try:
                    self._run_match()
                except Exception as exc:
                    self._clear_pending_match()
                    self._log(f"Match loop error: {exc}", "error")
                finally:
                    self._schedule_next_match(now)

            # Push live input level to the UI ~10 fps so the user can
            # see whether their soundcard is receiving signal.  Works in
            # BOTH modes: monitor (before capture) and capture.
            if self._capture is not None and now - last_level_ts >= level_period:
                last_level_ts = now
                try:
                    self._send_ui({"type": "level",
                                   "capturing": self._capture_running,
                                   **self._capture.current_level()})
                except Exception:
                    pass

            time.sleep(0.05)

        # Cleanup.
        self._close_capture()
        if self._fp is not None:
            try:
                self._fp.close()
            except Exception:
                pass
        self._log("Matcher thread stopped.")

    def stop(self) -> None:
        self._stop_event.set()
