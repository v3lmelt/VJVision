"""Build the isolated benchmark DLL from a locally cloned, pinned Olaf tree."""
import argparse
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--zig", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sources = ["pffft", "midl", "mdb", "hash-table", "queue", "olaf_deque",
               "olaf_max_filter_perceptual_van_herk", "olaf_fp_file_writer", "olaf_db",
               "olaf_db_id", "olaf_fp_db_writer", "olaf_fp_db_writer_cache", "olaf_ep_extractor",
               "olaf_fp_extractor", "olaf_runner", "olaf_stream_processor", "olaf_fp_matcher", "olaf_config"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    command = [str(args.zig.resolve()), "cc", "-shared", "-O2", "-std=gnu11", "-I", str(args.source.resolve() / "src"),
               str(Path(__file__).with_name("olaf_benchmark_bridge.c").resolve())]
    command += [str(args.source.resolve() / "src" / f"{name}.c") for name in sources]
    command += ["-o", str(args.output.resolve())]
    subprocess.run(command, check=True)


if __name__ == "__main__":
    main()
