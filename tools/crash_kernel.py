"""Kill QEMU at real WAL boundaries under GDB, then reboot and inspect the disk.

No kernel fault-injection syscall or production crash hook is used. Requires
gdb-multiarch on Linux/WSL. Each case owns a separate disposable fixture disk.
"""
from dataclasses import replace
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time

from smashbash.bridge import QEMUConfig
from smashbash.image import Image
from tests.helpers import ROOT, policy
from tests.test_kernel import TranscriptGuest


def boot(disk, command=None):
    guest = TranscriptGuest(QEMUConfig(ROOT / "xv6/kernel/kernel", cpus=3), disk,
                            replace(policy(), timeout_seconds=40))
    try:
        guest.boot()
        if command:
            guest._send(command)
            guest._until(b"CRASH BASELINE READY")
            guest._until(b"$ ")
        guest.close(require_clean=True)
        return bytes(guest.transcript)
    finally:
        guest.close()


def crash(disk, breakpoint):
    port = 12351
    proc = subprocess.Popen([
        "qemu-system-riscv64", "-machine", "virt", "-bios", "none", "-kernel", str(ROOT / "xv6/kernel/kernel"),
        "-m", "128M", "-smp", "3", "-nographic", "-nic", "none",
        "-global", "virtio-mmio.force-legacy=false",
        "-drive", f"file={disk},if=none,format=raw,cache=directsync,id=d",
        "-device", "virtio-blk-device,drive=d,bus=virtio-mmio-bus.0",
        "-gdb", f"tcp:127.0.0.1:{port}"], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0)
    console = bytearray()

    def console_reader():
        while chunk := proc.stdout.read(1):
            console.extend(chunk)

    thread = threading.Thread(target=console_reader, daemon=True)
    thread.start()
    debugger = None
    trace = bytearray()
    try:
        deadline = time.monotonic() + 30
        while b"$ " not in console:
            if time.monotonic() > deadline: raise RuntimeError("crash guest did not boot")
            time.sleep(.02)
        debugger = subprocess.Popen(["gdb-multiarch", "-nx", "-quiet", str(ROOT / "xv6/kernel/kernel")],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    bufsize=0)
        output = queue.Queue()

        def gdb_reader():
            while chunk := debugger.stdout.read(1): output.put(chunk)

        threading.Thread(target=gdb_reader, daemon=True).start()
        debugger.stdin.write((f"set pagination off\nset confirm off\ntarget remote 127.0.0.1:{port}\n"
                              f"break {breakpoint}\ncontinue\nbt\nprintf \"STOP_FOR_CRASH\\n\"\n").encode())
        debugger.stdin.flush()
        # The shell is waiting for input; the breakpoint is installed before the
        # transaction reaches its log boundary. Give GDB time to attach first.
        while b"Continuing." not in trace:
            trace += output.get(timeout=15)
        proc.stdin.write(b"specbench commit\n"); proc.stdin.flush()
        while not trace.endswith(b"STOP_FOR_CRASH\n"):
            trace += output.get(timeout=20)
        if b"Breakpoint 1," not in trace:
            raise RuntimeError("GDB did not reach the requested crash boundary")
        # Target is still stopped at the breakpoint. Kill it before detaching.
        proc.kill(); proc.wait(timeout=5)
        return bytes(trace) + b"\nConsole before power loss:\n" + bytes(console)
    finally:
        if debugger:
            debugger.kill(); debugger.wait(timeout=5)
        if proc.poll() is None:
            proc.kill(); proc.wait(timeout=5)
        thread.join(timeout=1)


def main():
    evidence = ROOT / "data/kernel-evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    cases = {
        "before-header": ("write_head if groups[0].state == 2", (b"old-a", b"old-b")),
        "after-header": ("install_trans if recovering == 0 && groups[0].state == 2", (b"new-a", b"new-b")),
        "during-install": ("bwrite if groups[0].state == 2 && b->blockno == log.lh.block[1]", (b"new-a", b"new-b")),
    }
    for name, (breakpoint, expected) in cases.items():
        with tempfile.TemporaryDirectory() as directory:
            disk = Path(directory) / "disk.img"
            shutil.copyfile(ROOT / "xv6/fs.img", disk)
            baseline = boot(disk, "specbench prepare")
            trace = crash(disk, breakpoint)
            recovery = boot(disk)
            image = Image(disk)  # Also checks directory graph, bitmap, links, clean log.
            actual = tuple(image.inodes[image.paths[path]].data for path in ("/crasha", "/crashb"))
            result = f"{name}: expected={expected!r}, actual={actual!r}, integrity=PASS\n"
            (evidence / f"crash-{name}.txt").write_bytes(baseline + trace + b"\nReboot:\n" + recovery + result.encode())
            if actual != expected: raise AssertionError(result)
            print(result.rstrip(), flush=True)


if __name__ == "__main__":
    main()
