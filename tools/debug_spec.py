"""Capture a real GDB backtrace from a failing xv6 guest test (Linux/WSL)."""
import argparse
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "data/kernel-evidence/gdb-backtrace.txt")
    parser.add_argument("--port", type=int, default=12349)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as directory:
        disk = Path(directory) / "disk.img"
        shutil.copyfile(ROOT / "xv6/fs.img", disk)
        process = subprocess.Popen([
            "qemu-system-riscv64", "-machine", "virt", "-bios", "none", "-kernel", str(ROOT / "xv6/kernel/kernel"),
            "-m", "128M", "-smp", "3", "-nographic", "-nic", "none", "-global", "virtio-mmio.force-legacy=false",
            "-drive", f"file={disk},if=none,format=raw,id=d", "-device", "virtio-blk-device,drive=d,bus=virtio-mmio-bus.0",
            "-gdb", f"tcp:127.0.0.1:{args.port}", "-S"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, bufsize=0)
        transcript = bytearray()

        def reader():
            while chunk := process.stdout.read(1):
                transcript.extend(chunk)

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        debugger = subprocess.Popen([
            "gdb-multiarch", "-nx", "-batch", str(ROOT / "xv6/kernel/kernel"),
            "-ex", f"target remote 127.0.0.1:{args.port}", "-ex", "break panic", "-ex", "continue",
            "-ex", "bt", "-ex", "info threads"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 30
            while b"$ " not in transcript:
                if time.monotonic() > deadline:
                    raise RuntimeError("guest failed to boot under GDB")
                time.sleep(0.02)
            process.stdin.write(b"spectest\n")
            process.stdin.flush()
            output, _ = debugger.communicate(timeout=30)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(output + b"\nGuest console:\n" + transcript)
            print(output.decode(errors="replace"))
        finally:
            if debugger.poll() is None:
                debugger.kill()
                debugger.wait()
            process.kill()
            process.wait()
            thread.join(timeout=1)


if __name__ == "__main__":
    main()
