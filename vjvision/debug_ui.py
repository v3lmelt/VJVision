"""First-screen debug UI built with CustomTkinter.

Controls:
    - Audio device picker (+ refresh)
    - Music directory picker (+ "Index library" button)
    - Spectrum style selector (Bar / Radial / Wave)
    - Rotation speed slider
    - Beat-reactive toggle
    - Start / Stop capture buttons
    - Current track info (title / artist / album / confidence)
    - Live log

All control changes are pushed onto ``out_queue`` as ``{'type': 'settings', ...}``
messages. The matcher process polls ``in_queue`` for status updates which we
display via ``root.after`` (Tkinter is not thread-safe).
"""
from __future__ import annotations

import logging
import queue as _queue
import tkinter as tk
from pathlib import Path
from tkinter import filedialog
from typing import Callable, Optional

import customtkinter as ctk

from . import __version__

log = logging.getLogger(__name__)

STYLE_OPTIONS = [("柱状", "bar"), ("波形", "wave"), ("镜像", "mirror")]


class DebugUI:
    """CustomTkinter main window."""

    def __init__(
        self,
        out_queue: "_queue.Queue",
        in_queue: "_queue.Queue",
        viz_mgr=None,
    ) -> None:
        self.out = out_queue
        self.input = in_queue
        self.viz_mgr = viz_mgr

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        self.root = ctk.CTk()
        self.root.title(f"VJVision 控制台  v{__version__}")
        # Clicking the window X runs the same shutdown path as the quit
        # button: notify the matcher, then destroy the window so main.py's
        # finally block runs (stops the visualizer child process, etc.).
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        # Auto-fit window height to the screen so everything fits
        # comfortably on first open; the scrollbar kicks in only if the
        # user shrinks the window manually.
        screen_h = self.root.winfo_screenheight()
        win_h = min(screen_h - 60, 1180)
        self.root.geometry(f"780x{win_h}+40+20")
        self.root.minsize(720, 600)

        # Scrollable container with auto-hide scrollbar.
        #
        # - Visually flat: transparent fg, no border, no label.
        # - When content fits inside the window the scrollbar is hidden
        #   (pack_forget); as soon as the user shrinks the window enough
        #   that content overflows, the scrollbar reappears and lets them
        #   scroll.  We recompute visibility on every root <Configure>
        #   event and once after the first layout pass.
        self.scroll = ctk.CTkScrollableFrame(
            self.root,
            fg_color="transparent",
            border_width=0,
            label_text="",
        )
        self.scroll.pack(fill="both", expand=True)

        self._build_widgets()
        self._refresh_devices()

        # Push initial visual settings to the visualizer so it doesn't
        # keep using its built-in defaults (e.g. rotation_speed=0.6).
        self._on_rotation_change(self.rotation_var.get())
        self._on_style_change()
        self._on_beat_change()

        # Begin polling incoming status messages.
        self.root.after(100, self._poll_in)

        # Auto-hide / show scrollbar: once content is laid out, and
        # again whenever the window is resized.
        self._configure_after_id: Optional[str] = None   # debounce handle
        self.root.after(50, self._update_scrollbar_visibility)
        self.root.bind("<Configure>", self._on_root_configure)

    # -- helpers ----------------------------------------------------------
    def _send(self, msg: dict) -> None:
        try:
            self.out.put_nowait(msg)
        except _queue.Full:
            log.warning("out_queue full - dropping %s", msg.get("type"))

    def _on_root_configure(self, _evt: object) -> None:
        """Debounced re-check of scrollbar visibility on resize."""
        # Guard against runs before `self._configure_after_id` exists
        # (not expected but keeps <Configure> safe during construction).
        if getattr(self, "_configure_after_id", None):
            self.root.after_cancel(self._configure_after_id)
        self._configure_after_id = self.root.after(
            60, self._update_scrollbar_visibility
        )

    def _update_scrollbar_visibility(self) -> None:
        """Hide the scrollbar when content fits; show it when overflowing.

        CTkScrollableFrame 6.0 places the scrollbar via **grid** (not pack,
        not place) at column=1, row=1 sticky="nesw". So we must use
        ``grid_remove()`` to hide and ``grid()`` to re-show — ``pack_forget``
        had zero effect because the scrollbar was never packed in the first
        place.  We compare the canvas's ``scrollregion`` height against the
        container height because that's the authoritative "content needs"
        measure after layout settles.
        """
        try:
            sb = self.scroll._scrollbar
            canvas = self.scroll._parent_canvas
            self.scroll.update_idletasks()
            region = canvas.cget("scrollregion")   # "x1 y1 x2 y2" or ""
            view_h = self.scroll.winfo_height()
            if not region:
                return
            parts = [int(x) for x in region.split()]
            content_h = parts[3] - parts[1] if len(parts) == 4 else 0
        except Exception:
            return
        if content_h <= view_h + 2:   # content fits — hide
            try:
                sb.grid_remove()
            except Exception:
                pass
        else:                          # content overflows — show
            try:
                sb.grid(
                    column=1, row=1, sticky="nesw",
                    padx=(0, 1), pady=6, rowspan=1, columnspan=1,
                )
            except Exception:
                pass

    def _build_widgets(self) -> None:
        pad = {"padx": 12, "pady": 6}

        # === Audio device ==============================================
        self.dev_frame = ctk.CTkFrame(self.scroll)
        self.dev_frame.pack(fill="x", **pad)
        ctk.CTkLabel(self.dev_frame, text="音频设备").pack(anchor="w", padx=8, pady=(8, 0))

        # Two-stage picker — all three controls on a single row to save
        # vertical space:
        #   1. Host API dropdown (MME / WASAPI / DirectSound / ...)
        #   2. Device dropdown — filtered to only show devices of the
        #      selected host API.
        #   3. Refresh Devices button.
        api_row = ctk.CTkFrame(self.dev_frame, fg_color="transparent")
        api_row.pack(fill="x", padx=8, pady=(4, 4))
        self.api_var = ctk.StringVar(value="")
        self.api_menu = ctk.CTkOptionMenu(
            api_row, variable=self.api_var, values=["（无）"],
            command=self._on_api_change, width=120,
        )
        self.api_menu.pack(side="left", padx=(0, 8))
        self.device_var = ctk.StringVar(value="")
        self.device_menu = ctk.CTkOptionMenu(
            api_row, variable=self.device_var, values=["（无设备）"],
            command=self._on_device_change, width=180,
        )
        self.device_menu.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.refresh_btn = ctk.CTkButton(
            api_row, text="刷新", width=80,
            command=self._refresh_devices,
        )
        self.refresh_btn.pack(side="left")

        # Input level meter - shows whether the selected soundcard is
        # actually receiving signal. Useful for distinguishing "wrong
        # device selected" from "device muted at the OS mixer".
        meter_row = ctk.CTkFrame(self.dev_frame, fg_color="transparent")
        meter_row.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(meter_row, text="输入电平：").pack(side="left")
        self.level_bar = ctk.CTkProgressBar(meter_row, width=300)
        self.level_bar.set(0.0)
        self.level_bar.pack(side="left", padx=(8, 8), fill="x", expand=True)
        # Live status: no device / no signal / monitoring / LIVE / clip,
        # plus a dBFS readout so the operator can quantify the input
        # level BEFORE starting capture (the meter already runs in
        # monitor mode, right after the device is selected).
        self.level_status = ctk.CTkLabel(meter_row, text="无输入设备",
                                         text_color="gray", width=150,
                                         anchor="w")
        self.level_status.pack(side="left", padx=(0, 8))
        # Peak indicator: turns red when the input is clipping.
        self.clip_label = ctk.CTkLabel(meter_row, text="峰值", text_color="gray",
                                       width=40)
        self.clip_label.pack(side="left")

        # === Library preparation (pre-performance) ======================
        self._queue: list[str] = []
        self.prep_frame = ctk.CTkFrame(self.scroll)
        self.prep_frame.pack(fill="x", **pad)
        ctk.CTkLabel(self.prep_frame, text="曲库准备").pack(anchor="w", padx=8, pady=(8, 0))

        # Add row: "Add Folder" + "Add Files" + "Clear" + queue count.
        # Folders are expanded to individual audio files when added.
        add_row = ctk.CTkFrame(self.prep_frame, fg_color="transparent")
        add_row.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkButton(add_row, text="+ 添加文件夹", width=120,
                      command=self._add_folder).pack(side="left")
        ctk.CTkButton(add_row, text="+ 添加文件", width=110,
                      command=self._pick_audio_files).pack(side="left", padx=4)
        ctk.CTkButton(add_row, text="清空", width=60,
                      command=self._clear_queue).pack(side="left")
        self.queue_label = ctk.CTkLabel(add_row, text="队列：0 个文件")
        self.queue_label.pack(side="left", padx=12)

        # Queued files scrollable list.
        self.queue_list = ctk.CTkTextbox(self.prep_frame, height=80, state="disabled")
        self.queue_list.pack(fill="x", padx=8, pady=2)

        # Action buttons.
        action_row = ctk.CTkFrame(self.prep_frame, fg_color="transparent")
        action_row.pack(fill="x", padx=8, pady=(0, 4))
        self.analyze_btn = ctk.CTkButton(action_row, text="分析队列", fg_color="green",
                                          command=self._on_analyze_queue)
        self.analyze_btn.pack(side="left")
        self.cancel_btn = ctk.CTkButton(action_row, text="✖ 取消", width=80,
                                        fg_color="gray", hover_color="#555555",
                                        state="disabled",
                                        command=self._on_cancel_analyze)
        self.cancel_btn.pack(side="left", padx=4)
        ctk.CTkButton(action_row, text="⚠ 强制重建索引", width=150,
                      fg_color="#B22222", hover_color="#8B0000",
                      command=self._on_force_reindex).pack(side="left", padx=4)
        ctk.CTkButton(action_row, text="刷新状态", width=120,
                      command=self._on_refresh_status).pack(side="left")

        # Progress bar + status label — lights up during indexing so the
        # user can see per-song progress, ETA, and current file name at
        # a glance without scrolling through the log.
        self.index_progress = ctk.CTkProgressBar(self.prep_frame, height=18)
        self.index_progress.set(0.0)
        self.index_progress.pack(fill="x", padx=8, pady=(8, 0))
        self.prep_status = ctk.CTkLabel(self.prep_frame, text="曲库：未知")
        self.prep_status.pack(anchor="w", padx=8, pady=(2, 8))

        # === Spectrum style ============================================
        self.style_frame = ctk.CTkFrame(self.scroll)
        self.style_frame.pack(fill="x", **pad)
        ctk.CTkLabel(self.style_frame, text="频谱样式").pack(anchor="w", padx=8, pady=(8, 0))
        self.style_var = ctk.StringVar(value="bar")
        for label, value in STYLE_OPTIONS:
            ctk.CTkRadioButton(
                self.style_frame, text=label, variable=self.style_var, value=value,
                command=self._on_style_change,
            ).pack(side="left", padx=12, pady=8)

        # === Rotation + beat ============================================
        self.anim_frame = ctk.CTkFrame(self.scroll)
        self.anim_frame.pack(fill="x", **pad)
        # Row: label + entry + range hint. Using an entry instead of a
        # slider so the user can type an exact value (the slider's long
        # drag bar wasted horizontal space and was easy to nudge by
        # accident with the mouse wheel).
        rot_row = ctk.CTkFrame(self.anim_frame, fg_color="transparent")
        rot_row.pack(fill="x", padx=8, pady=(8, 2))
        ctk.CTkLabel(rot_row, text="旋转速度（圈/秒）：").pack(side="left")
        self.rotation_var = ctk.DoubleVar(value=0.25)
        self.rotation_entry = ctk.CTkEntry(
            rot_row, textvariable=self.rotation_var, width=70,
            justify="center",
        )
        self.rotation_entry.pack(side="left", padx=(6, 8))
        ctk.CTkLabel(rot_row, text="（范围：0.05–2.0）",
                     text_color="gray").pack(side="left")
        # Commit the value on Enter / FocusOut so typing isn't interrupted
        # by sending half-typed numbers to the visualizer mid-edit.
        self.rotation_entry.bind("<Return>", lambda e: self._on_rotation_change(self.rotation_var.get()))
        self.rotation_entry.bind("<FocusOut>", lambda e: self._on_rotation_change(self.rotation_var.get()))
        self.beat_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(
            self.anim_frame, text="随节拍旋转",
            variable=self.beat_var, command=self._on_beat_change,
        ).pack(anchor="w", padx=8, pady=(0, 8))

        # === Display mode (window vs fullscreen) ====================
        self.display_frame = ctk.CTkFrame(self.scroll)
        self.display_frame.pack(fill="x", **pad)
        ctk.CTkLabel(self.display_frame, text="可视化显示").pack(
            anchor="w", padx=8, pady=(8, 0))
        from .config import SETTINGS as _S

        theme_row = ctk.CTkFrame(self.display_frame, fg_color="transparent")
        theme_row.pack(fill="x", padx=8, pady=8)
        ctk.CTkLabel(theme_row, text="画面主题：").pack(side="left", padx=(0, 6))
        self.theme_var = ctk.StringVar(
            value="杏桃频谱" if _S.visual.theme == "pastel" else "经典流光")
        ctk.CTkOptionMenu(
            theme_row, variable=self.theme_var,
            values=["杏桃频谱", "经典流光"], width=160,
            command=self._on_theme_change,
        ).pack(side="left")

        # Standby / LOGO image: an optional logo shown CENTERED (≤40% of
        # the screen, alpha channel supported) before the first track is
        # recognised. Its dominant colours drive the flowing background
        # until a cover takes over; it fades out once a song is
        # identified. Clear = background only in standby.
        standby_row = ctk.CTkFrame(self.display_frame, fg_color="transparent")
        standby_row.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(standby_row, text="待机画面/LOGO图片：").pack(
            side="left", padx=(0, 6))
        self.standby_label = ctk.CTkLabel(
            standby_row, text="（无）", text_color="gray", anchor="w",
        )
        self.standby_label.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            standby_row, text="清除", width=60,
            fg_color="#555555", hover_color="#777777",
            command=self._on_clear_standby,
        ).pack(side="right", padx=(4, 0))
        ctk.CTkButton(
            standby_row, text="选择…", width=90,
            command=self._on_choose_standby,
        ).pack(side="right")
        _si = _S.visual.standby_image or ""
        if _si:
            self.standby_label.configure(text=Path(_si).name)

        # Background mode selector: flowing colour blobs vs blurred cover.
        bg_row = ctk.CTkFrame(self.display_frame, fg_color="transparent")
        bg_row.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(bg_row, text="经典主题背景：").pack(side="left", padx=(0, 6))
        self.bg_mode_var = ctk.StringVar(
            value="流光 (flow)" if _S.visual.bg_mode == "flow" else "封面模糊 (blur)")
        ctk.CTkOptionMenu(
            bg_row, variable=self.bg_mode_var,
            values=["流光 (flow)", "封面模糊 (blur)"],
            command=self._on_bg_mode_change, width=140,
        ).pack(side="left")

        # Font picker: lets the user choose a font for the visualizer's
        # track-info text. Fonts are annotated with [CJK] when they carry
        # Chinese / Japanese / Korean glyphs, so the user can pick a
        # large-charset font deliberately.
        font_row = ctk.CTkFrame(self.display_frame, fg_color="transparent")
        font_row.pack(fill="x", padx=8, pady=(0, 8))
        ctk.CTkLabel(font_row, text="字体：").pack(side="left", padx=(0, 6))
        self._font_options = self._build_font_options()
        display_default = _S.visual.font_name or "auto"
        # Find the matching display string (match on the raw font VALUE,
        # e.g. "auto", then show its localised display label).
        matching = [d for d, v in self._font_options
                    if v.lower() == display_default.lower()]
        self.font_var = ctk.StringVar(
            value=matching[0] if matching else self._font_options[0][0],
        )
        self.font_menu = ctk.CTkOptionMenu(
            font_row, variable=self.font_var,
            values=[d for d, _ in self._font_options],
            command=self._on_font_change, width=220,
        )
        self.font_menu.pack(side="left")
        # Live preview: shows what a sample string looks like in the
        # currently-selected font. Useful for spotting missing glyphs.
        self.font_preview = ctk.CTkLabel(
            self.display_frame,
            text="示例：中文 / 日本語 / test  123 ABC",
            anchor="w", justify="left",
        )
        self.font_preview.pack(fill="x", padx=8, pady=(0, 4))

        # --- Visualizer window management ---
        # Reopen: fully kill + respawn the pygame child process. Use when
        # the visualizer was closed (ESC), crashed, or stuck on a broken
        # SDL surface. Restarts from scratch — fast (~1s).
        # Reset: soft clear — sends a 'viz_reset' message that wipes
        # cover/title/colors without killing the process. Much faster
        # than a full restart, useful for fixing a wrong track display.
        viz_btn_row = ctk.CTkFrame(self.display_frame, fg_color="transparent")
        viz_btn_row.pack(fill="x", padx=8, pady=(0, 0))
        ctk.CTkButton(
            viz_btn_row, text="🔄 重启可视化窗口",
            width=180, fg_color="#c05621", hover_color="#dd6b20",
            command=self._on_viz_restart,
        ).pack(side="left")
        ctk.CTkButton(
            viz_btn_row, text="↺ 重置画面",
            width=140, fg_color="#2b6cb0", hover_color="#3182ce",
            command=self._on_viz_reset,
        ).pack(side="left", padx=4)
        # Shows whether the viz process is currently alive.
        self.viz_status_label = ctk.CTkLabel(
            viz_btn_row, text="", text_color="gray",
        )
        self.viz_status_label.pack(side="right")
        self._update_viz_status_label()

        # Fullscreen hint — collapsed to the last line so it doesn't eat
        # vertical real estate but is still findable when the user needs it.
        ctk.CTkLabel(
            self.display_frame,
            text="点击窗口后按 F 或 F11 全屏，Esc 退出。",
            text_color="gray", font=("", 12),
        ).pack(anchor="w", padx=8, pady=(8, 8))

        # Kick off periodic viz status polling from the main thread.
        self.root.after(3000, self._poll_viz_alive)

        # === Capture controls ==========================================
        self.cap_frame = ctk.CTkFrame(self.scroll)
        self.cap_frame.pack(fill="x", **pad)
        ctk.CTkButton(self.cap_frame, text="开始采集", fg_color="green",
                      command=self._on_start).pack(side="left", padx=8, pady=8)
        ctk.CTkButton(self.cap_frame, text="停止采集", fg_color="darkred",
                      command=self._on_stop).pack(side="left", padx=8, pady=8)
        self.capture_status = ctk.CTkLabel(self.cap_frame, text="采集：已停止")
        self.capture_status.pack(side="left", padx=12)

        # === Current track ============================================
        self.track_frame = ctk.CTkFrame(self.scroll)
        self.track_frame.pack(fill="x", **pad)
        ctk.CTkLabel(self.track_frame, text="当前曲目").pack(anchor="w", padx=8, pady=(8, 0))
        self.track_title = ctk.CTkLabel(self.track_frame, text="（无）", font=("Arial", 16, "bold"))
        self.track_title.pack(anchor="w", padx=8)
        self.track_artist = ctk.CTkLabel(self.track_frame, text="")
        self.track_artist.pack(anchor="w", padx=8)
        self.track_conf = ctk.CTkLabel(self.track_frame, text="")
        self.track_conf.pack(anchor="w", padx=8)
        self.track_state = ctk.CTkLabel(
            self.track_frame, text="监听中", text_color="#63b3ed",
        )
        self.track_state.pack(anchor="w", padx=8, pady=(0, 8))

        # === Log ======================================================
        self.log_frame = ctk.CTkFrame(self.scroll)
        # Fixed height so the log doesn't shrink to zero inside a scroll.
        self.log_frame.pack(fill="x", **pad)
        ctk.CTkLabel(self.log_frame, text="日志").pack(anchor="w", padx=8, pady=(8, 0))
        self.log_text = ctk.CTkTextbox(self.log_frame, height=110, state="disabled")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    # -- device picker ----------------------------------------------------
    def _refresh_devices(self) -> None:
        from .audio_capture import AudioCapture
        from .config import SETTINGS, save_prefs
        devices = AudioCapture.list_input_devices()
        self._devices = devices

        # Group devices by host_api.
        from collections import OrderedDict
        groups: dict[str, list[dict]] = OrderedDict()
        for d in devices:
            ha = d["host_api"] or "Unknown"
            groups.setdefault(ha, []).append(d)
        self._api_groups = groups

        # --- Populate the host-API dropdown ---
        api_labels = list(groups.keys()) if groups else ["（无设备）"]
        self.api_menu.configure(values=api_labels)

        # --- Pick the initial host API + device ---
        saved_index: int | None = None
        try:
            raw = SETTINGS.audio_device
            if isinstance(raw, int):
                saved_index = raw
        except Exception:
            pass

        saved_match = None
        usb_match = None
        first_match = None
        for d in devices:
            if saved_index is not None and d["index"] == saved_index:
                saved_match = d
            if "usb" in d["name"].lower() and usb_match is None:
                usb_match = d
            if first_match is None:
                first_match = d

        chosen = saved_match or usb_match or first_match
        if chosen is not None:
            # Set the host API to the chosen device's API.
            chosen_api = chosen["host_api"] or "Unknown"
            self.api_var.set(chosen_api)
            # Populate the device dropdown with only this API's devices.
            self._populate_devices_for_api(chosen_api)
            # Select the chosen device.
            label = f"[{chosen['index']}] {chosen['name']}"
            self.device_var.set(label)
            # Persist so next run opens on the same device.
            if SETTINGS.audio_device != chosen["index"]:
                SETTINGS.audio_device = chosen["index"]
                save_prefs()
        else:
            self.api_var.set(api_labels[0])
            self._populate_devices_for_api(api_labels[0])
        self._on_device_change(self.device_var.get())

    def _populate_devices_for_api(self, api_name: str) -> None:
        """Fill the device dropdown with only devices of the given host API."""
        devs = self._api_groups.get(api_name, [])
        labels = [f"[{d['index']}] {d['name']}" for d in devs]
        if not labels:
            labels = ["（无设备）"]
        self.device_menu.configure(values=labels)

    def _on_api_change(self, api_name: str) -> None:
        """User picked a different host API — refresh the device list."""
        self._populate_devices_for_api(api_name)
        # Auto-select the first device of this API.
        labels = self.device_menu.cget("values")
        if labels:
            self.device_var.set(labels[0])
            self._on_device_change(labels[0])

    def _on_device_change(self, label: str) -> None:
        """Parse a device label like ``[3] Some Device`` → index=3.

        Also persist the choice to prefs.json.
        """
        from .config import SETTINGS, save_prefs

        if label in ("（无设备）", "（无）", ""):
            return

        try:
            import re
            m = re.search(r"\[(\d+)\]", label)
            idx = int(m.group(1)) if m else None
        except (ValueError, IndexError):
            idx = None

        # Persist the new choice.
        if idx is not None:
            if SETTINGS.audio_device != idx:
                SETTINGS.audio_device = idx
                save_prefs()
        self._send({"type": "device", "index": idx})

    # -- folder / file queue --------------------------------------------
    def _add_folder(self) -> None:
        """Pick a folder, expand it to audio files, and add them to the queue.

        Also updates SETTINGS.music_dir to this folder so that
        Force Re-index and library-status queries use the same root.
        """
        from pathlib import Path
        from .config import SETTINGS
        initial = str(SETTINGS.music_dir)
        chosen = filedialog.askdirectory(initialdir=initial)
        if not chosen:
            return
        # Update the library root so Force Re-index uses this folder.
        SETTINGS.music_dir = Path(chosen)
        self._send({"type": "music_dir", "path": str(SETTINGS.music_dir)})

        # Walk the folder for audio files (soundfile 0.14 decodes all
        # of these natively — see fingerprint._walk_audio_files).
        root = Path(chosen)
        exts = {".flac", ".wav", ".mp3", ".aiff", ".aif", ".ogg"}
        found = sorted(
            str(p) for p in root.rglob("*")
            if p.is_file() and p.suffix.lower() in exts
        )
        if not found:
            self._append_log(f"[该文件夹中没有音频文件：{chosen}]")
            return
        added = 0
        for p in found:
            if p not in self._queue:
                self._queue.append(p)
                added += 1
        self._refresh_queue_display()
        self._append_log(f"+ 文件夹：{chosen} → 已加入队列 {added} 个文件")

    def _on_force_reindex(self) -> None:
        """⚠ Clear MySQL + SQLite and re-index from SETTINGS.music_dir.

        Uses the folder last set via "+ Add Folder" (or the default
        D:\\DJMusLib).  No folder picker — the user already told us
        where the library is.
        """
        from .config import SETTINGS
        folder = str(SETTINGS.music_dir)

        # Confirm with user — this wipes everything.
        import tkinter.messagebox as _mb
        if not _mb.askyesno(
            "强制重建索引",
            "这将删除所有已存指纹（MySQL + SQLite），\n"
            f"并重新索引以下文件夹中的每一首歌：\n  {folder}\n\n"
            "当识别置信度偏低、或歌曲曾用错误参数索引时，\n"
            "才使用此功能。\n\n"
            "确定继续？",
        ):
            self._append_log("已取消强制重建索引。")
            return
        self._send({"type": "force_reindex"})
        self.cancel_btn.configure(state="normal")
        self._append_log(
            f"⚠ 强制重建索引：正在清空全部数据并重新索引 {folder}…"
        )

    # -- file picker / queue --------------------------------------------
    def _pick_audio_files(self) -> None:
        from .config import SETTINGS
        files = filedialog.askopenfilenames(
            initialdir=str(SETTINGS.music_dir),
            title="选择要分析的音频文件",
            filetypes=[
                ("音频文件", "*.flac *.wav *.mp3 *.aiff *.aif *.ogg"),
                ("所有文件", "*.*"),
            ],
        )
        if files:
            # Dedupe against already-queued paths.
            for p in files:
                if p not in self._queue:
                    self._queue.append(p)
            self._refresh_queue_display()

    def _clear_queue(self) -> None:
        self._queue.clear()
        self._refresh_queue_display()

    def _refresh_queue_display(self) -> None:
        self.queue_label.configure(text=f"队列：{len(self._queue)} 个文件")
        self.queue_list.configure(state="normal")
        self.queue_list.delete("1.0", "end")
        for p in self._queue:
            self.queue_list.insert("end", p + "\n")
        self.queue_list.configure(state="disabled")

    def _on_analyze_queue(self) -> None:
        if not self._queue:
            self._append_log("[队列为空] 请先选择音频文件。")
            return
        # Disable the button immediately so a double-click can't spawn
        # a second indexing pool.  Re-enabled on prep_done.
        self.analyze_btn.configure(state="disabled")
        # Send paths in ≤500-file batches — multiprocessing.Queue's pipe
        # buffer is ~64KB on Windows and pickling >~600 paths in one go
        # exceeds that, causing silent truncation or deadlocks.  The
        # matcher side accumulates batches until it sees a finalising
        # ``prepare_files_done`` message, then fires the real indexing pass.
        all_paths = list(self._queue)
        BATCH = 500
        for i in range(0, len(all_paths), BATCH):
            self._send({
                "type": "prepare_files_batch",
                "paths": all_paths[i : i + BATCH],
            })
        self._send({
            "type": "prepare_files_done",
            "total": len(all_paths),
        })
        self.cancel_btn.configure(state="normal")

    def _on_cancel_analyze(self) -> None:
        """Cancel ongoing indexing — sends a cancel flag to the matcher."""
        self._send({"type": "cancel_indexing"})
        self.cancel_btn.configure(state="disabled")
        self._append_log("[取消] 已向索引器发送取消信号…")

    def _on_refresh_status(self) -> None:
        self._send({"type": "library_status"})

    # -- visual settings -------------------------------------------------
    def _on_style_change(self) -> None:
        self._send({"type": "settings", "style": self.style_var.get()})

    def _on_rotation_change(self, value) -> None:
        # Clamp to the documented range [0.05, 2.0]; ignore non-numeric
        # input (e.g. empty entry while user is mid-edit) instead of
        # crashing the Tk main loop.
        try:
            v = round(float(value), 2)
        except (TypeError, ValueError):
            return
        if v < 0.05:
            v = 0.05
        elif v > 2.0:
            v = 2.0
        self._send({"type": "settings", "rotation_speed": v})

    def _on_beat_change(self) -> None:
        self._send({"type": "settings", "beat_reactive": bool(self.beat_var.get())})

    def _on_theme_change(self, choice: str) -> None:
        self._send({"type": "settings",
                    "theme": "pastel" if choice == "杏桃频谱" else "classic"})

    def _on_bg_mode_change(self, choice: str) -> None:
        """Switch between flowing-colour and blurred-cover backgrounds."""
        # The option menu shows localised labels ("流光 (flow)" /
        # "封面模糊 (blur)"); map back to the raw value the engine knows.
        raw = "blur" if "blur" in choice else "flow"
        self._send({"type": "settings", "bg_mode": raw})

    def _build_font_options(self) -> list[tuple[str, str]]:
        """Return (display_name, font_name) pairs for the font picker.

        Display names are suffixed with "[CJK]" when the font supports
        Chinese / Japanese / Korean glyphs. The list starts with an
        "auto" entry that picks the best CJK font automatically, then
        all CJK-capable fonts (alphabetical), then the rest.
        """
        # Deferred import so the UI can still be built if pygame is
        # not yet initialised (e.g. tests stubbing it out).
        try:
            import pygame
            if not pygame.font.get_init():
                pygame.font.init()
            from .visualizer import _list_system_fonts
            raw = _list_system_fonts()
        except Exception:
            # No pygame / not initialised - offer only the auto option.
            return [("自动（优选中文字体）", "auto")]

        out: list[tuple[str, str]] = [("自动（优选中文字体）", "auto")]
        for name, supports in raw:
            display = f"{name}  [中文字体]" if supports else name
            out.append((display, name))
        return out

    def _on_font_change(self, choice: str) -> None:
        """Apply the user's font choice to the visualizer.

        ``choice`` is the display string; we look up the raw font name
        in ``_font_options``. Falls back to "auto" if not found.
        """
        # Look up the raw font name corresponding to the display string.
        raw = "auto"
        for display, name in self._font_options:
            if display == choice:
                raw = name
                break
        # Update the live preview label with the new font. We re-render
        # the sample using the actual pygame font so the user sees the
        # real glyph coverage.
        try:
            import pygame
            if not pygame.font.get_init():
                pygame.font.init()
            from .visualizer import _pick_font
            resolved = _pick_font(raw)
            f = pygame.font.SysFont(resolved, 24)
            sample = "中文 / 日本語 / test  123 ABC"
            surf = f.render(sample, True, (255, 255, 255))
            import numpy as np
            arr = pygame.surfarray.array_alpha(surf)
            non_zero = int((arr > 0).sum())
            total = arr.size
            ratio = non_zero / total if total > 0 else 0.0
            missing = ratio < 0.03
            preview_text = (sample + ("  [有缺字！]" if missing else ""))
            self.font_preview.configure(text=preview_text)
        except Exception:
            # Pygame not available in this process - just keep the
            # default sample text.
            pass
        self._send({"type": "settings", "font_name": raw})

    # -- standby image -----------------------------------------------------
    def _on_choose_standby(self) -> None:
        from tkinter import filedialog
        path = filedialog.askopenfilename(
            title="选择待机图片（推荐带透明通道的 PNG）",
            filetypes=[
                ("图片", "*.png *.jpg *.jpeg *.bmp *.webp"),
                ("所有文件", "*.*"),
            ],
        )
        if not path:
            return
        from .config import SETTINGS, save_prefs
        SETTINGS.visual.standby_image = path
        save_prefs()
        self.standby_label.configure(text=Path(path).name)
        self._send({"type": "settings", "standby_image": path})

    def _on_clear_standby(self) -> None:
        from .config import SETTINGS, save_prefs
        SETTINGS.visual.standby_image = ""
        save_prefs()
        self.standby_label.configure(text="（无）")
        self._send({"type": "settings", "standby_image": ""})

    # -- capture ----------------------------------------------------------
    def _on_start(self) -> None:
        self._send({"type": "start"})
        self.capture_status.configure(text="采集：运行中")
        self.track_state.configure(text="采集中", text_color="#48bb78")

    def _on_stop(self) -> None:
        self._send({"type": "stop"})
        # Stopping returns to monitor mode: the stream stays open and the
        # level meter keeps working, only recognition stops.
        self.capture_status.configure(
            text="采集：已停止（监听输入中）")
        self.track_state.configure(text="监听中", text_color="#63b3ed")

    # -- visualizer management --------------------------------------------
    def _on_viz_restart(self) -> None:
        """Hard restart: kill and respawn the visualizer child process."""
        if self.viz_mgr is None:
            self._append_log("[可视化] 可视化管理器不可用")
            return
        # The restart involves a brief sleep(0.3) + process spawn — run
        # it in a background thread so the UI stays responsive.
        threading = __import__("threading")
        def worker():
            try:
                self.viz_mgr.restart()
            except Exception as exc:
                log.error("viz_restart failed: %s", exc)
        threading.Thread(target=worker, daemon=True, name="UIVizRestart").start()
        self._append_log("🔄 正在重启可视化窗口…")
        self.root.after(300, self._update_viz_status_label)

    def _on_viz_reset(self) -> None:
        """Soft reset: tell the visualizer to clear state."""
        if self.viz_mgr is None:
            self._append_log("[可视化] 可视化管理器不可用")
            return
        self.viz_mgr.reset()
        self._append_log("↺ 可视化画面已重置")

    def _update_viz_status_label(self) -> None:
        if self.viz_mgr is None:
            self.viz_status_label.configure(text="可视化：未知", text_color="gray")
            return
        if self.viz_mgr.alive:
            self.viz_status_label.configure(
                text="● 运行中", text_color="green",
            )
        else:
            self.viz_status_label.configure(
                text="● 已停止", text_color="red",
            )

    def _poll_viz_alive(self) -> None:
        """Periodic liveness check from the UI thread."""
        self._update_viz_status_label()
        self.root.after(3000, self._poll_viz_alive)

    # -- status poller ---------------------------------------------------
    def _poll_in(self) -> None:
        try:
            while True:
                msg = self.input.get_nowait()
                self._handle_in(msg)
        except _queue.Empty:
            pass
        self.root.after(100, self._poll_in)

    def _handle_in(self, msg: dict) -> None:
        mtype = msg.get("type")
        if mtype == "log":
            self._append_log(msg.get("text", ""))
        elif mtype == "track":
            self.track_title.configure(text=msg.get("title", "（无）"))
            self.track_artist.configure(text=f"{msg.get('artist', '')} — {msg.get('album', '')}")
            conf = msg.get("confidence")
            self.track_conf.configure(text=f"置信度：{conf:.2f}" if conf is not None else "")
        elif mtype == "index_progress":
            done, total, info = msg.get("done", 0), msg.get("total", 0), msg.get("info", "")
            if total > 0:
                self.index_progress.set(min(1.0, done / total))
            else:
                self.index_progress.set(0.0)
            self.prep_status.configure(text=f"分析中：{done}/{total} — {info}")
        elif mtype == "index_done":
            songs = msg.get("songs", 0)
            self.index_progress.set(1.0)
            self.prep_status.configure(text=f"曲库：已准备 {songs} 首")
        elif mtype == "library_status":
            prepared = msg.get("prepared", 0)
            pending = msg.get("pending", 0)
            total = msg.get("total", 0)
            line = f"曲库：已分析 {prepared} 首"
            if pending:
                line += f"，待处理 {pending} 首（共 {total} 首）"
            self.prep_status.configure(text=line)
        elif mtype == "prep_done":
            new = msg.get("new", 0)
            total = msg.get("total", 0)
            self.cancel_btn.configure(state="disabled")
            self.analyze_btn.configure(state="normal")  # re-enable for next run
            if "error" in msg:
                self._append_log(f"准备失败：{msg['error']}")
            else:
                # Clear the queue on success - everything's now in the index.
                self._queue.clear()
                self._refresh_queue_display()
            self.prep_status.configure(text=f"已准备 {new}/{total} 个文件")
        elif mtype == "capture_status":
            text = msg.get("text", "")
            self.capture_status.configure(text=text)
            if "运行中" in text:
                self.track_state.configure(text="采集中", text_color="#48bb78")
            elif "监听" in text:
                self.track_state.configure(text="监听中", text_color="#63b3ed")
            elif "已停止" in text:
                self.track_state.configure(text="待机", text_color="gray")
        elif mtype == "viz_status":
            # Matcher is reporting the visualizer process state.
            self._append_log(msg.get("text", ""))
            self._update_viz_status_label()
        elif mtype == "level":
            # Live input level from the soundcard.  These messages flow
            # in BOTH modes: monitor (stream open, capture not started)
            # and capture — so the operator can confirm signal before
            # going live.
            peak = msg.get("peak", 0.0)
            rms = msg.get("rms", 0.0)
            peak_hold = msg.get("peak_hold", 0.0)
            signal = msg.get("signal", False)
            clips = msg.get("clips", 0)
            active = msg.get("active", False)
            capturing = msg.get("capturing", False)
            # dBFS readout from the block RMS; floor at -60 dB so the
            # label never shows -inf on near-silence.
            import math as _math
            dbfs = 20.0 * _math.log10(max(rms, 1e-3)) if rms > 0 else -60.0
            if not active:
                self.level_bar.set(0.0)
                self.clip_label.configure(text_color="gray")
                self.level_status.configure(
                    text="无输入设备", text_color="#e56b6b")
            elif not signal:
                self.level_bar.set(0.0)
                self.clip_label.configure(text_color="gray")
                self.level_status.configure(
                    text="无信号 — 请检查设备/调音台",
                    text_color="#f6ad55")
            else:
                # Use peak_hold for the bar so the meter is readable.
                self.level_bar.set(peak_hold)
                # Red peak indicator if recent clipping.
                if clips > 0:
                    self.clip_label.configure(text_color="red")
                    self.level_status.configure(
                        text=f"削波！{dbfs:.0f} dBFS", text_color="red")
                elif capturing:
                    self.clip_label.configure(text_color="gray")
                    self.level_status.configure(
                        text=f"● 采集中  {dbfs:.0f} dBFS", text_color="#48bb78")
                else:
                    self.clip_label.configure(text_color="gray")
                    self.level_status.configure(
                        text=f"信号正常  {dbfs:.0f} dBFS（监听）",
                        text_color="#63b3ed")

    def _append_log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        # Keep last 500 lines.
        if int(self.log_text.index("end-1c").split(".")[0]) > 500:
            self.log_text.delete("1.0", "2.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # -- lifecycle --------------------------------------------------------
    def run(self) -> None:
        self.root.mainloop()

    def quit(self) -> None:
        self._send({"type": "quit"})
        self.root.destroy()
