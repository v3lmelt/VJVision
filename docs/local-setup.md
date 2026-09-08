# VJVision

VJVision captures audio, draws a live spectrum, and identifies songs from a locally analyzed library to display track information and cover artwork.

## Windows setup

Use Python 3.11 for the environment verified with this project. In PowerShell, run these commands from the project directory. If the Python launcher does not recognize `-3.11`, substitute the full path to your Python 3.11 executable in the first command.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pip install --no-deps -r requirements-dejavu.txt
.\.venv\Scripts\python.exe main.py
```

Install both requirements files in the indicated order. The second file pins the upstream audio fingerprinting implementation; the PyPI package named `dejavu` is unrelated. Upstream specifies historical dependency versions, so `--no-deps` preserves the runtime installed by the first command.

After installation, double-click `start.bat`. It always uses the project environment, without requiring PowerShell activation.

## First session

1. Select **+ 添加文件** or **+ 添加文件夹**, then **分析队列**. Wait for analysis to finish.
2. Play an analyzed song with your usual player. Choose the matching WASAPI **[Loopback]** output in the audio device selector, or select a physical input for an external source.
3. Confirm that the input meter responds, then select **开始采集**.
4. Choose a spectrum style and rotation speed. Track information appears after recognition; cover artwork is extracted from the audio file.
5. Focus the visualizer and press **F** or **F11** to toggle fullscreen.

Development databases and logs live in `cache/`. Machine preferences live in `%APPDATA%/VJVision/prefs.json`. Recognition requires songs to have been analyzed locally.

## Visual themes

The default **杏桃频谱** theme uses an apricot background, square cover art, animated orbital markings, a pink spectrum, and 30 seconds of live input history. Select **经典流光** under **可视化显示 → 画面主题** to restore the original visualizer. Both themes support the spectrum-style selector and fullscreen shortcuts.

The square artwork stays upright while the surrounding markers rotate with the rotation and beat settings. Before recognition, the panel uses the configured standby image or its built-in sleeve graphic. Recognized tracks use their embedded artwork.

BPM and genre come from file tags. Duration, sample rate, bit depth, bitrate, and channel count come from file metadata; missing values appear as a dash. Input RMS and peak are measured in dBFS. The spectrum uses relative amplitude, and input history is a rolling signal view rather than a seekable song timeline.

## Validation and previews

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tools/preview_visualizer.py
```

The preview is saved to `artifacts/apricot-preview.png` using synthetic audio and example song details. Add `--cover PATH` to preview artwork, or `--standby` to render the idle screen. Set `--width` and `--height` to inspect other window sizes. These sample values are never loaded into the application.
