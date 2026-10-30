"""Small known filesystem fixtures; no QEMU dependency for host safety tests."""

import base64
import struct
from pathlib import Path

from smashbash.filesystem import Node
from smashbash.image import BSIZE, DINODE, DIRENT, MAGIC
from smashbash.policy import Policy

ROOT = Path(__file__).resolve().parents[1]


def policy_raw():
    import json
    return json.loads((ROOT / "config/policy.example.json").read_text())


def policy():
    return Policy.parse(policy_raw())


def request(*operations, ident="trial-1", mode="commit", intent="Update a project file"):
    return {"version": 1, "request_id": ident, "intent": intent, "mode": mode,
            "operations": list(operations) or [write("note.txt", b"hello")]}


def write(path, data=b"hello", op="fs.write"):
    return {"op": op, "path": path, "content_b64": base64.b64encode(data).decode()}


def make_image(path, tree=None, *, guard=b"trusted"):
    tree = tree or {".": Node("directory")}
    image = bytearray(128 * BSIZE)
    struct.pack_into("<8I", image, BSIZE, MAGIC, 128, 121, 32, 2, 2, 4, 6)
    nodes = {"/": Node("directory"), "/data": tree["."], "/README": Node("file", guard)}
    nodes.update({"/data/" + p: n for p, n in sorted(tree.items()) if p != "."})
    inums = {name: index + 1 for index, name in enumerate(nodes)}
    next_block = 7
    for name, node in nodes.items():
        ino = inums[name]
        links = 1
        data = node.data
        if node.kind == "directory":
            parent = name.rpartition("/")[0] or "/"
            entries = [(ino, "."), (inums[parent], "..")]
            children = [p for p in nodes if p != "/" and (p.rpartition("/")[0] or "/") == name]
            entries.extend((inums[p], p.rpartition("/")[2]) for p in children)
            links += sum(nodes[p].kind == "directory" for p in children)
            data = b"".join(DIRENT.pack(number, n.encode()) for number, n in entries)
        count = (len(data) + BSIZE - 1) // BSIZE
        if count > 12:
            raise ValueError("fixture supports only direct blocks")
        pointers = list(range(next_block, next_block + count)) + [0] * (13 - count)
        for i in range(count):
            chunk = data[i * BSIZE:(i + 1) * BSIZE]
            image[(next_block + i) * BSIZE:(next_block + i) * BSIZE + len(chunk)] = chunk
        next_block += count
        DINODE.pack_into(image, 4 * BSIZE + ino * DINODE.size,
                         1 if node.kind == "directory" else 2, 0, 0, links, len(data), *pointers)
    for block in range(next_block):
        image[6 * BSIZE + block // 8] |= 1 << (block % 8)
    Path(path).write_bytes(image)
    return inums
