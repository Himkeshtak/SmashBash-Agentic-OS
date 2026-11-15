"""Run real guest regression tests and capture raw benchmark evidence (Linux/WSL)."""
import argparse
from dataclasses import replace
from pathlib import Path
import shutil
import tempfile
import time

from smashbash.bridge import QEMUConfig
from tests.helpers import ROOT, policy
from tests.test_kernel import TranscriptGuest


def run(command, marker, cpus, timeout):
    with tempfile.TemporaryDirectory() as directory:
        disk = Path(directory) / "disk.img"
        shutil.copyfile(ROOT / "xv6/fs.img", disk)
        guest = TranscriptGuest(QEMUConfig(ROOT / "xv6/kernel/kernel", cpus=cpus), disk,
                                replace(policy(), timeout_seconds=timeout))
        started = time.monotonic()
        try:
            guest.boot()
            guest._send(command)
            window = bytearray()
            line = bytearray()
            while not window.endswith(marker):
                byte = guest._byte()
                window = (window + byte)[-len(marker):]
                line += byte
                if byte == b"\n":
                    print(line.decode(errors="replace").rstrip(), flush=True)
                    if b"SOME TESTS FAILED" in line or b"panic:" in line:
                        raise RuntimeError("guest regression failed")
                    line.clear()
            guest._until(b"$ ")
            guest.close(require_clean=True)
            print(f"Completed {command!r}, cpus={cpus}, wall_seconds={time.monotonic()-started:.3f}", flush=True)
        finally:
            guest.close()
            evidence = ROOT / "data/kernel-evidence"
            evidence.mkdir(parents=True, exist_ok=True)
            (evidence / f"{command.split()[0]}-{cpus}cpu.txt").write_bytes(guest.transcript)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--regression", action="store_true")
    parser.add_argument("--bench", action="store_true")
    args = parser.parse_args()
    if args.regression:
        run("usertests -q", b"ALL TESTS PASSED", 3, 600)
    if args.bench:
        for cpus in (1, 3):
            run("specbench", b"BENCH COMPLETE", cpus, 90)


if __name__ == "__main__":
    main()
