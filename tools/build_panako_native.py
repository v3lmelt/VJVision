"""Build JGaborator JNI for the host without WSL; upstream sources stay external."""
import argparse
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--zig", type=Path, required=True)
    parser.add_argument("--jdk", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    src = args.source.resolve() / "gaborator"
    cores = [p for p in src.glob("gaborator-*") if p.is_dir()]
    if len(cores) != 1:
        raise ValueError("Expected one pinned Gaborator core directory")
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    windows = sys.platform == "win32"
    target = ["-target", "x86_64-windows-gnu"] if windows else []
    compiler = str(args.zig.resolve())
    objects = []
    for name in ["pffft", "fftpack"]:
        obj = out / f"{name}.o"
        command = [compiler, "cc", *target, "-c", "-O3", "-ffast-math"]
        if not windows:
            command.append("-fPIC")
        if name == "fftpack":
            command.append("-DFFTPACK_DOUBLE_PRECISION")
        subprocess.run([*command, str(src / "pffft" / f"{name}.c"), "-o", str(obj)], check=True)
        objects.append(str(obj))
    platform_include = "win32" if windows else ("darwin" if sys.platform == "darwin" else "linux")
    name = "jgaborator.dll" if windows else ("libjgaborator.dylib" if sys.platform == "darwin" else "libjgaborator.so")
    command = [compiler, "c++", *target, "-std=gnu++11", "-D_USE_MATH_DEFINES", "-O3", "-ffast-math", "-shared",
               "-DGABORATOR_USE_PFFFT", "-I", str(src / "pffft"), "-I", str(cores[0]),
               "-I", str(args.jdk.resolve() / "include"), "-I", str(args.jdk.resolve() / "include" / platform_include)]
    if not windows:
        command.append("-fPIC")
    subprocess.run([*command, str(src / "jgaborator.cc"), *objects, "-o", str(out / name)], check=True)
    print(out / name)


if __name__ == "__main__":
    main()
