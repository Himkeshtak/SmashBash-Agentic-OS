"""Copy verified raw runs into versioned evidence, with build and source hashes."""
from datetime import datetime, timezone
import hashlib
import json
import subprocess

from tests.helpers import ROOT


def main():
    source, target = ROOT / "data/kernel-evidence", ROOT / "docs/evidence"
    target.mkdir(parents=True, exist_ok=True)
    required = {
        "spectest-1cpu.log": b"SPECTEST ALL PASS",
        "spectest-3cpu.log": b"SPECTEST ALL PASS",
        "usertests-3cpu.txt": b"ALL TESTS PASSED",
        "specbench-1cpu.txt": b"BENCH COMPLETE",
        "specbench-3cpu.txt": b"BENCH COMPLETE",
        "crash-before-header.txt": b"integrity=PASS",
        "crash-after-header.txt": b"integrity=PASS",
        "crash-during-install.txt": b"integrity=PASS",
        "gdb-pipe-reader-before.txt": b"panic: acquire",
    }
    for name, marker in required.items():
        data = (source / name).read_bytes()
        if marker not in data: raise RuntimeError(f"missing evidence marker: {name}")
        (target / name.replace(".log", ".txt")).write_bytes(data)
    tests = (source / "python-qemu-tests.txt").read_bytes()
    if not tests.rstrip().endswith(b"OK"): raise RuntimeError("full test run did not pass")
    (target / "python-qemu-tests.txt").write_bytes(tests)
    paths = [ROOT / "xv6/Makefile", *sorted((ROOT / "xv6/kernel").glob("*.c")),
             *sorted((ROOT / "xv6/kernel").glob("*.h")), *sorted((ROOT / "xv6/user").glob("*.c")),
             *sorted((ROOT / "smashbash").glob("*.py"))]
    metadata = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "runtime": "Ubuntu WSL; QEMU virt; 128 MiB guest RAM; 1 and 3 virtual CPUs",
        "versions": {command: subprocess.check_output([command, "--version"], text=True).splitlines()[0]
                     for command in ("python3", "qemu-system-riscv64", "riscv64-linux-gnu-gcc", "gdb-multiarch")},
        "sha256": {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
                   for path in [ROOT / "xv6/kernel/kernel", ROOT / "xv6/fs.img", *paths]},
    }
    (target / "environment.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(f"Captured {len(required)+2} verified evidence files")


if __name__ == "__main__":
    main()
