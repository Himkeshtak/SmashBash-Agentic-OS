"""Durable image publication and a journal, with a single-writer ownership contract."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import ContextManager, Protocol

from .model import TransactionError


class StorageTarget(Protocol):
    """Adapter contract. Native backends must supply atomic publication and recovery revisions.

    snapshot returns a private, bootable xv6 image plus the native baseline revision.
    A database adapter must use its own DB transaction; copying database files is invalid.
    """

    identity: str
    journal: Path

    def lock(self) -> ContextManager: ...
    def revision(self) -> str: ...
    def snapshot(self, directory: Path) -> tuple[Path, str]: ...
    def prospective_revision(self, shadow: Path) -> str: ...
    def publish(self, shadow: Path, baseline: str, expected: str) -> None: ...
    def confirm_publication(self, revision: str) -> None: ...
    def read_record(self, ident: str) -> dict | None: ...
    def record(self, ident: str, value: dict) -> None: ...


def digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def sync_directory(path: Path):
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def durable_json(path: Path, value):
    fd, temporary = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class ImageTarget:
    """Only a dedicated regular xv6 image; never a raw device or active guest disk."""

    def __init__(self, path: Path, state_dir: Path):
        self.path = path.absolute()
        if self.path.is_symlink() or not self.path.exists() or not stat.S_ISREG(self.path.stat().st_mode):
            raise TransactionError("target must be a dedicated regular image file")
        if self.path.stat().st_nlink != 1 or not 2048 <= self.path.stat().st_size <= 32 * 1024 * 1024:
            raise TransactionError("target must be an xv6 image with one hard link and a supported size")
        self.path = self.path.resolve()
        self.state_dir = state_dir.resolve()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # State is trusted service storage, never an agent-writable directory.
        self.identity = hashlib.sha256(str(self.path).encode()).hexdigest()
        self.journal = self.state_dir / self.identity
        self.journal.mkdir(mode=0o700, exist_ok=True)

    @contextmanager
    def lock(self):
        """OS lock is released on process death; the lock file is never unlinked."""
        with (self.journal / "target.lock").open("a+b") as stream:
            try:
                if os.name == "nt":
                    import msvcrt
                    stream.seek(0)
                    if not stream.read(1):
                        stream.write(b"\0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise TransactionError("target is busy") from exc
            try:
                yield
            finally:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def revision(self):
        if (self.path.is_symlink() or not self.path.is_file() or self.path.stat().st_nlink != 1
                or not 2048 <= self.path.stat().st_size <= 32 * 1024 * 1024):
            raise TransactionError("target type changed")
        return digest_file(self.path)

    def snapshot(self, directory: Path):
        before = self.revision()
        shadow = directory / "shadow.img"
        shutil.copyfile(self.path, shadow)
        shadow.chmod(0o600)
        if digest_file(shadow) != before or self.revision() != before:
            raise TransactionError("target changed while taking the snapshot")
        return shadow, before

    def publish(self, shadow: Path, baseline: str, expected: str):
        """The temporary replacement is on the target's filesystem for atomic rename."""
        fd, temporary = tempfile.mkstemp(prefix=".smashbash-", suffix=".img", dir=self.path.parent)
        try:
            with os.fdopen(fd, "wb") as destination, shadow.open("rb") as source:
                shutil.copyfileobj(source, destination)
                destination.flush()
                os.fsync(destination.fileno())
            if digest_file(Path(temporary)) != expected:
                raise TransactionError("shadow changed before publication")
            if self.revision() != baseline:
                raise TransactionError("target conflict: live image changed")
            os.chmod(temporary, stat.S_IMODE(self.path.stat().st_mode))
            os.replace(temporary, self.path)
            sync_directory(self.path.parent)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def prospective_revision(self, shadow: Path):
        return digest_file(shadow)

    def confirm_publication(self, revision: str):
        with self.path.open("r+b") as stream:
            os.fsync(stream.fileno())
        sync_directory(self.path.parent)
        if self.revision() != revision:
            raise TransactionError("published revision changed during recovery")

    def read_record(self, ident: str):
        path = self.journal / (ident + ".json")
        if not path.exists():
            return None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise TransactionError("transaction journal is unreadable; recovery required") from exc
        states = {"RECEIVED", "ADMITTED", "EXECUTING", "PREPARED", "COMMITTING",
                  "COMMITTED", "ABORTED", "INDETERMINATE"}
        if (not isinstance(record, dict) or record.get("request_id") != ident
                or not isinstance(record.get("state"), str) or record["state"] not in states):
            raise TransactionError("transaction journal is invalid; recovery required")
        if record["state"] in {"COMMITTED", "ABORTED", "INDETERMINATE"} and (
                not isinstance(record.get("result"), dict) or record["result"].get("state") != record["state"]):
            raise TransactionError("terminal transaction receipt is invalid; recovery required")
        return record

    def record(self, ident: str, value):
        durable_json(self.journal / (ident + ".json"), value)
