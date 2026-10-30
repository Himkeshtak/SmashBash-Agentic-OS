"""Bounded SB1 serial transport. Only the private shadow image is attached to QEMU."""

from __future__ import annotations

import queue
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .model import OPERATIONS, Operation, TransactionError
from .policy import Policy


@dataclass(frozen=True)
class QEMUConfig:
    kernel: Path
    executable: str = "qemu-system-riscv64"
    memory_mb: int = 128
    cpus: int = 1


@dataclass(frozen=True)
class Prepared:
    operations: int
    entries: int
    bytes: int


class QEMUBridge:
    def __init__(self, config: QEMUConfig, image: Path, policy: Policy):
        self.config, self.image, self.policy = config, image.resolve(), policy
        self.process = None
        self.output = queue.Queue(maxsize=65536)
        self.overflow = threading.Event()
        self.deadline = time.monotonic() + policy.timeout_seconds
        self.received = 0

    def _reader(self):
        stream = self.process.stdout
        try:
            while True:
                chunk = stream.read(1)
                if not chunk:
                    break
                try:
                    self.output.put_nowait(chunk)
                except queue.Full:
                    self.overflow.set()
                    break
        finally:
            # None is not needed: process.poll() plus a bounded queue wait detects EOF.
            stream.close()

    def _byte(self):
        while True:
            if self.overflow.is_set():
                raise TransactionError("guest output buffer exceeded")
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TransactionError("guest transaction timed out")
            try:
                byte = self.output.get(timeout=min(remaining, 0.1))
                break
            except queue.Empty:
                if self.process.poll() is not None:
                    raise TransactionError("QEMU exited before completing the protocol")
        self.received += len(byte)
        if self.received > 2 * 1024 * 1024:
            raise TransactionError("guest total output budget exceeded")
        return byte

    def _until(self, marker: bytes):
        window = b""
        while not window.endswith(marker):
            window = (window + self._byte())[-max(1024, len(marker)):]

    def _response(self):
        line = bytearray()
        while True:
            byte = self._byte()
            if byte == b"\n":
                text = bytes(line).strip(b"\r")
                line.clear()
                if text.startswith(b"@SB1 "):
                    try:
                        response = text.decode("ascii")
                    except UnicodeError as exc:
                        raise TransactionError("non-ASCII guest protocol response") from exc
                    if response.startswith("@SB1 ABORT"):
                        raise TransactionError("xv6 validator rejected the transaction: " + response[11:])
                    return response
            else:
                line += byte
                if len(line) > 2048:
                    raise TransactionError("guest protocol line exceeded")

    def _send(self, line):
        if len(line) > 480 or any(ord(c) < 32 or ord(c) > 126 for c in line):
            raise TransactionError("invalid outbound protocol frame")
        try:
            self.process.stdin.write(line.encode("ascii") + b"\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise TransactionError("QEMU input failed") from exc

    def _ok(self, line):
        self._send(line)
        if self._response() != "@SB1 OK":
            raise TransactionError("unexpected guest acknowledgement")

    def start(self):
        executable = shutil.which(self.config.executable)
        if executable is None or not self.config.kernel.is_file():
            raise TransactionError("QEMU or the xv6 kernel is missing; run the documented build first")
        # QEMU -drive has its own comma syntax even without a shell.
        if "," in str(self.image):
            raise TransactionError("QEMU shadow path cannot contain commas")
        args = [executable, "-machine", "virt", "-bios", "none", "-kernel",
                str(self.config.kernel.resolve()), "-m", str(self.config.memory_mb),
                "-smp", str(self.config.cpus), "-nographic", "-nic", "none", "-no-reboot",
                "-global", "virtio-mmio.force-legacy=false",
                "-drive", f"file={self.image},if=none,format=raw,cache=directsync,id=shadow",
                "-device", "virtio-blk-device,drive=shadow,bus=virtio-mmio-bus.0"]
        try:
            self.process = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            stderr=subprocess.STDOUT, bufsize=0)
        except OSError as exc:
            raise TransactionError("could not launch QEMU") from exc
        self.reader = threading.Thread(target=self._reader, daemon=True)
        self.reader.start()
        self._until(b"$ ")
        self._send("shadowd")  # Fixed trusted binary; no agent string is executed as a command.
        if self._response() != "@SB1 READY 1":
            raise TransactionError("unsupported guest protocol")

    def begin(self, operations: int):
        p = self.policy
        self._ok(f"B {operations} {p.max_entries} {p.max_total_bytes} {p.max_file_bytes} {p.guest_root.encode().hex()}")

    def execute(self, operation: Operation):
        spec = OPERATIONS[operation.op]
        destination = operation.destination.encode().hex() if operation.destination else "-"
        self._ok(f"O {spec.wire} {operation.path.encode().hex()} {destination} {len(operation.content)}")
        for offset in range(0, len(operation.content), 48):
            self._ok("D " + operation.content[offset:offset + 48].hex())
        self._ok("E")

    def prepare(self) -> Prepared:
        self._send("F")
        parts = self._response().split()
        if len(parts) != 5 or parts[:2] != ["@SB1", "PREPARED"]:
            raise TransactionError("guest did not prepare the transaction")
        try:
            return Prepared(*(int(value) for value in parts[2:]))
        except ValueError as exc:
            raise TransactionError("malformed guest preparation report") from exc

    def close(self, *, require_clean=False):
        if self.process is None:
            return
        clean = False
        try:
            if self.process.poll() is None:
                self.process.stdin.write(b"\x01x")  # QEMU's exit command, not guest shell input.
                self.process.stdin.flush()
                try:
                    clean = self.process.wait(timeout=3) == 0
                except subprocess.TimeoutExpired:
                    self.process.terminate()
                    try:
                        self.process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self.process.kill()
                        self.process.wait(timeout=2)
            else:
                clean = self.process.returncode == 0
        except (OSError, BrokenPipeError):
            self.process.kill()
            self.process.wait(timeout=3)
        finally:
            self.process.stdin.close()
            self.reader.join(timeout=1)
            self.process = None
        if require_clean and not clean:
            raise TransactionError("QEMU did not shut down cleanly; shadow cannot be published")
