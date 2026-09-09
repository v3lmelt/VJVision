"""Build the portable worker and narrowly patched LMDB adapter from pinned sources.

Keeps upstream code/licensing in the external cache; the published JAR is not edited.
The patch reduces the default map reservation and fixes the wrong deletion queue.
"""
import argparse
from pathlib import Path
import shutil
import subprocess


def patch_storage(source):
    original = '.setMapSize(1024l * 1024l * 1024l * 1024l)'
    if source.count(original) != 1:
        raise ValueError("Unexpected upstream map-size expression")
    source = source.replace(original, '.setMapSize(Long.getLong("vjvision.panako.map.bytes", 256L * 1024L * 1024L))')
    start = source.index("public void processDeleteQueue()")
    end = source.index("public void addToQueryQueue", start)
    deletion = source[start:end]
    if deletion.count("storeQueue") != 3:
        raise ValueError("Unexpected upstream deletion implementation")
    return source[:start] + deletion.replace("storeQueue", "deleteQueue") + source[end:]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--jar", type=Path, required=True)
    parser.add_argument("--javac", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source = args.source / "src/main/java/be/panako/strategy/panako/storage/PanakoStorageKV.java"
    patched = args.output / "patched-source/be/panako/strategy/panako/storage/PanakoStorageKV.java"
    patched.parent.mkdir(parents=True, exist_ok=True)
    patched.write_text(patch_storage(source.read_text(encoding="utf-8")), encoding="utf-8")
    jar = args.output / "Panako-2.1-all.jar"
    if args.jar.resolve() != jar.resolve():
        shutil.copy2(args.jar, jar)
    subprocess.run([str(args.javac.resolve()), "--release", "11", "-cp", str(jar.resolve()), "-d", str(args.output.resolve()),
        str(patched.resolve()), str(Path(__file__).with_name("PanakoStreamServer.java").resolve())], check=True)


if __name__ == "__main__":
    main()
