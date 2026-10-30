"""Read-only xv6-riscv image inspection, independent of the guest executor.

An image is never mounted on the host. Malformed geometry, an unclean log,
aliased blocks, bitmap mismatches, bad directory links, and orphan inodes fail closed.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

from .filesystem import Node
from .model import TransactionError

BSIZE = 1024
DINODE = struct.Struct("<hhhhI13I")
DIRENT = struct.Struct("<H14s")
MAGIC = 0x10203040


@dataclass(frozen=True)
class Inode:
    kind: int
    major: int
    minor: int
    links: int
    size: int
    data: bytes


class Image:
    def __init__(self, path: Path):
        if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise TransactionError("image is absent or exceeds the inspection limit")
        self.raw = path.read_bytes()
        if len(self.raw) < 2 * BSIZE:
            raise TransactionError("truncated xv6 image")
        magic, size, nblocks, ninodes, nlog, logstart, inodestart, bmapstart = struct.unpack_from("<8I", self.raw, BSIZE)
        data_start = size - nblocks
        if (magic != MAGIC or not 2 <= size <= 32768 or len(self.raw) != size * BSIZE
                or not 2 <= ninodes <= 4096 or nlog < 2 or logstart != 2
                or inodestart != logstart + nlog
                or bmapstart < inodestart + (ninodes + 15) // 16
                or data_start < bmapstart + (size + 8191) // 8192
                or data_start >= size):
            raise TransactionError("invalid xv6 superblock geometry")
        if struct.unpack_from("<I", self.raw, logstart * BSIZE)[0] != 0:
            raise TransactionError("xv6 log is not clean")
        self.size, self.data_start = size, data_start
        bitmap = self.raw[bmapstart * BSIZE:data_start * BSIZE]
        self.used = set(range(data_start))
        self.inodes: dict[int, Inode] = {}
        for number in range(1, ninodes):
            offset = inodestart * BSIZE + number * DINODE.size
            kind, major, minor, links, length, *addresses = DINODE.unpack_from(self.raw, offset)
            if kind == 0:
                continue
            if kind not in {1, 2, 3} or links < 1 or length > 268 * BSIZE:
                raise TransactionError("invalid live inode")
            count = (length + BSIZE - 1) // BSIZE
            direct = addresses[:12]
            indirect = addresses[12]
            pointers = list(direct)
            if count > 12:
                self._claim(indirect)
                pointers += list(struct.unpack_from("<256I", self.raw, indirect * BSIZE))
            elif indirect:
                raise TransactionError("unexpected indirect block")
            if any(pointers[count:]) or (kind == 3 and (length or any(addresses))):
                raise TransactionError("inode has blocks beyond its declared size")
            chunks = []
            for block in pointers[:count]:
                self._claim(block)
                chunks.append(self.raw[block * BSIZE:(block + 1) * BSIZE])
            self.inodes[number] = Inode(kind, major, minor, links, length, b"".join(chunks)[:length])
        allocated = {block for block in range(size) if bitmap[block // 8] & (1 << (block % 8))}
        if allocated != self.used:
            raise TransactionError("allocation bitmap does not match inode block ownership")
        self.paths: dict[str, int] = {}
        self._visited_dirs: set[int] = set()
        self._references: dict[int, int] = {}
        self._walk(1, "/", 1, 0)
        if set(self.inodes) != set(self.paths.values()):
            raise TransactionError("filesystem contains unreachable inodes")
        for path, number in self.paths.items():
            inode = self.inodes[number]
            if inode.kind == 1:
                children = sum(1 for p, n in self.paths.items() if p != "/"
                               and p.rpartition("/")[0] == path.rstrip("/")
                               and self.inodes[n].kind == 1)
                if inode.links != 1 + children:
                    raise TransactionError("directory link count mismatch")
            elif inode.links != self._references[number]:
                raise TransactionError("file link count mismatch")

    def _claim(self, block):
        if not self.data_start <= block < self.size or block in self.used:
            raise TransactionError("invalid or multiply owned data block")
        self.used.add(block)

    def _walk(self, number, path, parent, depth):
        if depth > 32 or number not in self.inodes:
            raise TransactionError("invalid directory reference or excessive depth")
        inode = self.inodes[number]
        self.paths[path] = number
        self._references[number] = self._references.get(number, 0) + 1
        if inode.kind != 1:
            return
        if number in self._visited_dirs or inode.size % DIRENT.size:
            raise TransactionError("directory cycle or malformed directory length")
        self._visited_dirs.add(number)
        entries = {}
        for offset in range(0, inode.size, DIRENT.size):
            child, encoded = DIRENT.unpack_from(inode.data, offset)
            if not child:
                continue
            name_bytes = encoded.split(b"\0", 1)[0]
            try:
                name = name_bytes.decode("ascii")
            except UnicodeError as exc:
                raise TransactionError("unsupported directory name") from exc
            if not name or name in entries or "/" in name or any(ord(c) < 32 for c in name):
                raise TransactionError("invalid directory entry")
            entries[name] = child
        if entries.get(".") != number or entries.get("..") != parent:
            raise TransactionError("invalid dot directory links")
        for name, child in entries.items():
            if name not in {".", ".."}:
                self._walk(child, path.rstrip("/") + "/" + name, number, depth + 1)

    def tree(self, root: str) -> dict[str, Node]:
        number = self.paths.get(root)
        if number is None or self.inodes[number].kind != 1:
            raise TransactionError("managed guest root is absent or is not a directory")
        result = {}
        for path, ino in self.paths.items():
            if path != root and not path.startswith(root + "/"):
                continue
            inode = self.inodes[ino]
            if inode.kind not in {1, 2} or (inode.kind == 2 and inode.links != 1):
                raise TransactionError("managed tree contains a device or hard link")
            key = "." if path == root else path[len(root) + 1:]
            result[key] = Node("directory" if inode.kind == 1 else "file",
                               inode.data if inode.kind == 2 else b"")
        return result

    def outside(self, root: str):
        return {p: (ino, self.inodes[ino]) for p, ino in self.paths.items()
                if p != root and not p.startswith(root + "/")}
