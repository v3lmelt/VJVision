"""Run the same Panako PCM worker on the host JVM; no WSL or shell process."""
import os
import sys
from pathlib import Path
from tools.panako_wsl_client import PanakoWorker


def library_name():
    return "jgaborator.dll" if sys.platform == "win32" else ("libjgaborator.dylib" if sys.platform == "darwin" else "libjgaborator.so")


class NativePanakoWorker(PanakoWorker):
    def __init__(self, java, classes, libraries, database, log, *, map_bytes=256 * 1024 * 1024):
        if not isinstance(map_bytes, int) or map_bytes <= 0:
            raise ValueError("map_bytes must be a positive integer")
        database = Path(database).resolve()
        database.mkdir(parents=True, exist_ok=True)
        command = [str(Path(java).resolve()), "-Xmx2g", "-Dfile.encoding=UTF-8",
            "-Dvjvision.panako.map.bytes=" + str(map_bytes),
            "-Djava.library.path=" + str(Path(libraries).resolve()),
            "-cp", os.pathsep.join([str(Path(classes).resolve()), str(Path(classes).resolve() / "Panako-2.1-all.jar")]),
            "PanakoStreamServer", str(database / "db")]
        super().__init__(None, java, classes, database, log, command=command, cwd=database)
