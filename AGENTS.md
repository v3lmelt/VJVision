# Repository Guidelines

## Project Structure & Module Organization

`main.py` coordinates the CustomTkinter control UI, matcher thread, and separate pygame visualizer process. Application modules live in `vjvision/`:

- `audio_capture.py` handles audio input; `matcher.py` coordinates recognition.
- `fingerprint.py` manages indexing; `dejavu_sqlite.py` supplies SQLite storage.
- `metadata.py` extracts track details and cover artwork.
- `debug_ui.py`, `visualizer.py`, and `pastel_visualizer.py` implement controls and display themes.
- `config.py` defines settings and runtime paths.

`cache/` contains development databases and generated assets. Artwork comes from music metadata or a user-selected standby image. `build.bat` and `VJVision.spec` support Windows packaging. `tests/` contains automated checks; `tools/` contains preview utilities.

## Build, Test, and Development Commands

Run these commands from the repository root in PowerShell:

- `py -3.11 -m venv .venv` creates an isolated environment.
- `.\.venv\Scripts\Activate.ps1` activates it.
- `python -m pip install -r requirements.txt` installs application dependencies.
- `python -m pip install --no-deps -r requirements-dejavu.txt` installs the pinned audio recognition library after its runtime dependencies.
- `python main.py` launches the control UI and visualizer.
- `.\start.bat` launches using the project environment without activation.
- `python -m compileall -q main.py vjvision` checks Python syntax without launching the application.
- `.\build.bat` packages `dist-onefile/VJVision.exe`, installing PyInstaller if needed and replacing previous build output. Close running packaged instances first.

## Coding Style & Naming Conventions

Use four-space indentation, `snake_case` functions and variables, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Follow existing type annotations, dataclasses, module docstrings, and module-level logging. Centralize defaults and paths in `config.py`. Preserve queue-based communication and keep Tkinter updates on the main thread. No formatter or linter is configured.

## Testing Guidelines

Run `python -m unittest discover -s tests -v` for signal, layout, and metadata checks. No coverage threshold is configured. Manually verify audio-device selection, indexing, recognition, theme switching, and shutdown when relevant. Check packaged startup after packaging changes. Record the audio and display setup used. Name tests `tests/test_<module>.py` and isolate database tests with temporary files.

## Commit & Pull Request Guidelines

Follow the existing `feat:`, `fix:`, `docs:`, and `chore:` commit prefixes with concise imperative subjects. Keep changes focused. Pull requests should describe behavior changes, link relevant issues, document validation, and include screenshots for UI changes.

## Configuration & Runtime Data

Development data lives in `cache/`; packaged data lives beside the executable in `data/`. Machine preferences live in `%APPDATA%/VJVision/prefs.json`. Preserve this separation. Avoid committing personal music paths, generated databases, logs, or packaged output.
