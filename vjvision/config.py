"""Central configuration for VJVision.

All paths, device hints and tunables live here so the rest of the codebase
stays free of magic numbers.

User-editable prefs (selected device, music dir, display choices) are
persisted to ``cache/prefs.json`` between runs.  Code-level defaults stay
in the :class:`Settings` dataclass — JSON overrides only the keys that
exist in the file, so new settings introduced later don't crash an old
prefs file.
"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path


def _compute_base_dirs() -> tuple[Path, Path, Path, Path]:
    """Return (bundle_dir, app_dir, data_dir, prefs_dir).

    * ``bundle_dir`` — read-only Python resources.  Dev mode = project
      root; PyInstaller-frozen = the temporary ``_MEIPASS`` extraction.
    * ``app_dir`` — the folder the user sees / runs from.  Dev mode =
      project root; frozen = the folder containing the .exe (USB root).
    * ``data_dir`` — portable, user-visible state that MUST travel with
      the exe: fingerprint DB, cover cache, song-path index.  Frozen it
      sits next to the exe so analysing on PC A and copying the folder to
      PC B carries every analysed artifact along.
    * ``prefs_dir`` — **NOT** portable.  Per-user, per-machine storage for
      things that depend on local hardware (audio device index, monitor
      choice).  Lives in ``%APPDATA%/VJVision/`` so the same exe on a
      USB drive auto-restores its last-used device on the same PC, but
      doesn't pollute PC B with PC A's PortAudio indexes.
    """
    if getattr(sys, "frozen", False):  # PyInstaller
        app_dir = Path(sys.executable).resolve().parent
        bundle_dir = Path(getattr(sys, "_MEIPASS", app_dir))
        data_dir = app_dir / "data"
    else:
        app_dir = Path(__file__).resolve().parent.parent
        bundle_dir = app_dir
        data_dir = app_dir / "cache"
    # Per-machine prefs — always %APPDATA%\VJVision\, regardless of
    # whether we're frozen or running as source.
    prefs_dir = Path(os.environ.get("APPDATA", str(Path.home() / ".config"))) / "VJVision"
    return bundle_dir, app_dir, data_dir, prefs_dir


# ``__file__`` is ``vjvision/config.py`` so PROJECT_ROOT is two levels up.
BUNDLE_DIR, APP_DIR, CACHE_DIR, PREFS_DIR = _compute_base_dirs()
PROJECT_ROOT = APP_DIR
# dejavu fingerprint store (SQLite backend in portable builds) and the
# song_id -> file_path index.  Both are plain files, safe to copy.
FINGERPRINTS_DB = CACHE_DIR / "fingerprints.db"
SONG_PATHS_DB = CACHE_DIR / "song_paths.sqlite"
COVER_CACHE = CACHE_DIR / "covers"
TEMP_AUDIO = CACHE_DIR / "tmp_capture.wav"
LOG_FILE = CACHE_DIR / "vjvision.log"
# prefs.json lives in %APPDATA%\VJVision\ — NOT on the U盘.  This lets
# the same exe auto-restore its last-used audio device on the same PC,
# but doesn't carry PC A's PortAudio indexes over to PC B.
PREFS_FILE = PREFS_DIR / "prefs.json"

# Make sure working dirs exist at import time.
for _p in (CACHE_DIR, COVER_CACHE, PREFS_DIR):
    _p.mkdir(parents=True, exist_ok=True)


@dataclass
class MySQLConfig:
    host: str = "127.0.0.1"
    port: int = 3306
    user: str = "root"
    password: str = ""
    database: str = "dejavu"


@dataclass
class CaptureConfig:
    sample_rate: int = 44100
    channels: int = 2
    block_size: int = 1024          # frames per callback
    match_seconds: int = 12          # length of clip passed to dejavu.
                                     # 8s = too short for low-hash-density songs
                                     # (confidence 0.05-0.12); 12s stabilises
                                     # real matches at 0.20-0.85.
    match_interval: int = 4          # seconds between recognition attempts.
                                     # 12s was too slow when DJ switches tracks;
                                     # 6s gave ~2 opportunities per change but
                                     # long cross-fades still took ~28s to
                                     # confirm; 4s catches the confidence climb
                                     # faster during extended mixes.
    match_confirmations: int = 2     # require N consecutive hits before
                                     # switching the displayed track.  This
                                     # filters out the occasional low-confidence
                                     # false positive on a mid-track noise burst.
    match_confidence: float = 0.30   # accepted hit for switching or mix recovery
    first_track_confidence: float = 0.25  # preserve fast initial acquisition
    mix_candidate_confidence: float = 0.06  # pulse current artwork, never switch
    spectrum_fps: int = 30         # how often FFT bins are pushed to the visualizer
    spectrum_bins: int = 48        # number of frequency bins rendered (denser bar grid)


@dataclass
class VisualConfig:
    theme: str = "pastel"        # pastel instrument panel | classic flowing cover
    spectrum_style: str = "bar"     # one of: bar | wave | mirror
    rotation_speed: float = 0.3    # revolutions per second (slower for "chill" feel)
    beat_reactive: bool = False    # if True, rotation pulses with bass energy
    fullscreen: bool = False       # if True the visualizer opens fullscreen
    window_width: int = 1280      # windowed-mode width
    window_height: int = 720       # windowed-mode height
    bg_blur_px: int = 8        # smaller = stronger blur (for 'blur' mode)
    bg_brightness: float = 0.35
    bg_mode: str = "flow"     # "flow" = key-color animated blobs; "blur" = blurred cover
    cover_size_ratio: float = 0.35  # fraction of screen height for the rotating cover
    # "auto" = pick the best CJK-capable system font; otherwise an
    # explicit font name (must be a registered system font).
    font_name: str = "auto"
    # Optional image (PNG/JPG; alpha channel supported) shown centered
    # on screen BEFORE the first track is recognised. It occupies at
    # most 40% of the screen and its dominant colours drive the flowing
    # background. It fades out once a track is identified. "" = none.
    standby_image: str = ""


@dataclass
class Settings:
    # Music library root - override in the UI or here.
    music_dir: Path = Path(r"D:\DJMusLib")
    mysql: MySQLConfig = field(default_factory=MySQLConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    visual: VisualConfig = field(default_factory=VisualConfig)
    # If non-None, sounddevice will open this device index/name. None = pick at runtime.
    audio_device: str | int | None = None
    # 0-based monitor index for the visualizer window (right display).
    # Set to 0 if you only have one display and want it on the primary screen.
    visualizer_display: int = 1


SETTINGS = Settings()


# --------------------------------------------------------------------------- #
# Persistence helpers                                                         #
# --------------------------------------------------------------------------- #
def save_prefs() -> None:
    """Dump the current :data:`SETTINGS` to :data:`PREFS_FILE`.

    Only "user preference" fields are persisted — not transient runtime
    state.  We keep it deliberately lightweight so the JSON file stays
    human-editable and forward-compatible with new Settings fields.
    """
    prefs = {
        # audio_device IS persisted — but prefs.json itself lives in
        # %APPDATA%\VJVision\ (see PREFS_FILE), NOT on the USB drive.
        # So the same exe auto-restores its last-used device on the same
        # PC, while PC B gets a clean "pick your device" experience since
        # its %APPDATA%\VJVision\prefs.json doesn't exist yet.
        "audio_device": SETTINGS.audio_device,
        "music_dir": str(SETTINGS.music_dir),
        "visualizer_display": SETTINGS.visualizer_display,
        "visual": asdict(SETTINGS.visual),
    }
    try:
        with open(PREFS_FILE, "w", encoding="utf-8") as f:
            json.dump(prefs, f, indent=2, ensure_ascii=False)
    except OSError:
        pass  # non-critical — prefs are nice-to-have


def load_prefs() -> None:
    """Load JSON prefs into :data:`SETTINGS` if the file exists.

    Missing keys are silently ignored — code-level defaults in the
    :class:`Settings` dataclass always win when no saved value is present.
    """
    # Preserve machine preferences when upgrading the old VJ-Visual checkout.
    # Once saved under VJVision, that file takes precedence over the legacy one.
    source = PREFS_FILE
    if not source.is_file():
        source = PREFS_FILE.parent.parent / "VJ-Visual" / "prefs.json"
    if not source.is_file():
        return
    try:
        with open(source, "r", encoding="utf-8") as f:
            prefs = json.load(f)
    except (OSError, json.JSONDecodeError):
        return

    if "audio_device" in prefs:
        SETTINGS.audio_device = prefs["audio_device"]
    if "music_dir" in prefs:
        SETTINGS.music_dir = Path(prefs["music_dir"])
    if "visualizer_display" in prefs:
        SETTINGS.visualizer_display = int(prefs["visualizer_display"])

    v = prefs.get("visual")
    if isinstance(v, dict):
        for k, val in v.items():
            if hasattr(SETTINGS.visual, k):
                setattr(SETTINGS.visual, k, val)
