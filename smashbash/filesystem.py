"""Independent expected-state evaluator and final policy gate."""

from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass

from .model import Request, TransactionError, canonical_path
from .policy import Policy


@dataclass(frozen=True)
class Node:
    kind: str
    data: bytes = b""

    def digest(self):
        return hashlib.sha256(self.data).hexdigest()


def validate_tree(tree: dict[str, Node], policy: Policy) -> None:
    if tree.get(".") != Node("directory"):
        raise TransactionError("target root must be a directory")
    total = 0
    for path, node in tree.items():
        canonical_path(path, root_allowed=True)
        if node.kind not in {"file", "directory"}:
            raise TransactionError("special files and links are forbidden in the managed tree")
        if path != ".":
            parent = path.rpartition("/")[0] or "."
            if tree.get(parent) != Node("directory"):
                raise TransactionError("invalid directory hierarchy")
        if node.kind == "directory" and node.data:
            raise TransactionError("directory cannot contain a file payload")
        if len(node.data) > policy.max_file_bytes:
            raise TransactionError("final file size exceeds policy")
        total += len(node.data)
    if len(tree) > policy.max_entries or total > policy.max_total_bytes:
        raise TransactionError("final filesystem resource quota exceeded")


def evaluate(before: dict[str, Node], request: Request, policy: Policy):
    """Predict every allowed effect without executing agent-supplied code."""
    validate_tree(before, policy)
    tree, observations = dict(before), []
    for op in request.operations:
        node = tree.get(op.path)
        parent = op.path.rpartition("/")[0] or "."
        if op.path != "." and tree.get(parent) != Node("directory"):
            raise TransactionError("operation parent directory does not exist")
        if op.op in {"fs.write", "fs.append"}:
            if node is not None and node.kind != "file":
                raise TransactionError("write target is not a regular file")
            data = (node.data if node and op.op == "fs.append" else b"") + op.content
            tree[op.path] = Node("file", data)
            observation = {"op": op.op, "path": op.path, "size": len(data)}
        elif op.op == "fs.mkdir":
            if node is not None:
                raise TransactionError("mkdir target already exists")
            tree[op.path] = Node("directory")
            observation = {"op": op.op, "path": op.path}
        elif op.op == "fs.remove":
            if node is None:
                raise TransactionError("remove target does not exist")
            if any(p.startswith(op.path + "/") for p in tree):
                raise TransactionError("remove requires an empty directory")
            del tree[op.path]
            observation = {"op": op.op, "path": op.path}
        elif op.op == "fs.rename":
            if node is None or node.kind != "file" or op.destination in tree:
                raise TransactionError("rename needs a regular source and an absent destination")
            destination_parent = op.destination.rpartition("/")[0] or "."
            if tree.get(destination_parent) != Node("directory"):
                raise TransactionError("rename destination parent does not exist")
            tree[op.destination] = tree.pop(op.path)
            observation = {"op": op.op, "path": op.path, "destination": op.destination}
        elif op.op == "fs.read":
            if node is None or node.kind != "file":
                raise TransactionError("read target is not a regular file")
            observation = {"op": op.op, "path": op.path,
                           "content_b64": base64.b64encode(node.data).decode(), "sha256": node.digest()}
        elif op.op == "fs.list":
            if node != Node("directory"):
                raise TransactionError("list target is not a directory")
            prefix = "" if op.path == "." else op.path + "/"
            names = sorted(p[len(prefix):] for p in tree if p != "." and p.startswith(prefix)
                           and "/" not in p[len(prefix):])
            observation = {"op": op.op, "path": op.path, "entries": names}
        else:  # fs.stat; unknown operations were rejected during parsing.
            if node is None:
                raise TransactionError("stat target does not exist")
            observation = {"op": op.op, "path": op.path, "kind": node.kind, "size": len(node.data)}
        observations.append(observation)
        validate_tree(tree, policy)
    return tree, observations


def changed_paths(before, after):
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))
