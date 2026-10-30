"""Strict public JSON contract, shared by CLI and Python callers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any


class TransactionError(Exception):
    """A request cannot safely proceed."""


# These are protocol ceilings, not grants. Policy can only reduce them.
MAX_REQUEST_BYTES = 512 * 1024
MAX_OPERATIONS = 32
MAX_FILE_BYTES = 64 * 1024
MAX_TOTAL_BYTES = 256 * 1024
MAX_ENTRIES = 128
MAX_PATH = 96
MAX_COMPONENT = 14  # xv6 DIRSIZ; longer components silently alias in xv6.
MAX_DEPTH = 8


@dataclass(frozen=True)
class OperationSpec:
    wire: str
    fields: frozenset[str]
    mutation: bool = False


OPERATIONS = {
    "fs.read": OperationSpec("R", frozenset({"op", "path"})),
    "fs.list": OperationSpec("L", frozenset({"op", "path"})),
    "fs.stat": OperationSpec("S", frozenset({"op", "path"})),
    "fs.write": OperationSpec("W", frozenset({"op", "path", "content_b64"}), True),
    "fs.append": OperationSpec("A", frozenset({"op", "path", "content_b64"}), True),
    "fs.mkdir": OperationSpec("M", frozenset({"op", "path"}), True),
    "fs.remove": OperationSpec("U", frozenset({"op", "path"}), True),
    "fs.rename": OperationSpec("N", frozenset({"op", "path", "destination"}), True),
}


def canonical_path(value: Any, *, root_allowed: bool = False) -> str:
    if root_allowed and value == ".":
        return value
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_PATH:
        raise TransactionError("path must be a nonempty relative path within protocol limits")
    parts = value.split("/")
    if len(parts) > MAX_DEPTH or any(
        part in {"", ".", ".."}
        or len(part) > MAX_COMPONENT
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
        for part in parts
    ):
        raise TransactionError("path must use canonical ASCII components of at most 14 characters")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TransactionError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise TransactionError("nonfinite JSON numbers are forbidden")


def load_json(data: bytes | str) -> Any:
    if isinstance(data, str):
        data = data.encode("utf-8")
    if len(data) > MAX_REQUEST_BYTES:
        raise TransactionError("JSON document exceeds protocol byte limit")
    try:
        return json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    except (ValueError, UnicodeError, RecursionError) as exc:
        raise TransactionError("invalid JSON document") from exc


@dataclass(frozen=True)
class Operation:
    op: str
    path: str
    content: bytes = b""
    destination: str | None = None

    @classmethod
    def parse(cls, raw: Any) -> Operation:
        if not isinstance(raw, dict) or not isinstance(raw.get("op"), str):
            raise TransactionError("each operation must be a typed object")
        spec = OPERATIONS.get(raw["op"])
        if spec is None or set(raw) != spec.fields:
            raise TransactionError("unknown operation or unexpected operation fields")
        path = canonical_path(raw["path"], root_allowed=raw["op"] in {"fs.list", "fs.stat"})
        destination = None
        if "destination" in raw:
            destination = canonical_path(raw["destination"])
            if destination == path:
                raise TransactionError("rename source and destination must differ")
        content = b""
        if "content_b64" in raw:
            encoded = raw["content_b64"]
            if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
                raise TransactionError("content exceeds protocol file limit")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise TransactionError("content_b64 must be valid base64") from exc
            if len(content) > MAX_FILE_BYTES:
                raise TransactionError("content exceeds protocol file limit")
        return cls(raw["op"], path, content, destination)


@dataclass(frozen=True)
class Request:
    request_id: str
    intent: str
    operations: tuple[Operation, ...]
    mode: str
    digest: str

    @classmethod
    def parse(cls, raw: Any) -> Request:
        if not isinstance(raw, dict) or set(raw) != {"version", "request_id", "intent", "mode", "operations"}:
            raise TransactionError("request fields must be version, request_id, intent, mode, operations")
        if type(raw["version"]) is not int or raw["version"] != 1:
            raise TransactionError("unsupported protocol version")
        ident = raw["request_id"]
        if not isinstance(ident, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", ident):
            raise TransactionError("invalid request_id")
        intent = raw["intent"]
        if not isinstance(intent, str) or not 1 <= len(intent) <= 4096 or any(ord(c) < 32 for c in intent):
            raise TransactionError("intent must be printable text of 1 to 4096 characters")
        if not isinstance(raw["mode"], str) or raw["mode"] not in {"commit", "preview"}:
            raise TransactionError("mode must be commit or preview")
        ops = raw["operations"]
        if not isinstance(ops, list) or not 1 <= len(ops) <= MAX_OPERATIONS:
            raise TransactionError("operation count exceeds protocol limits")
        operations = tuple(Operation.parse(op) for op in ops)
        if sum(len(op.content) for op in operations) > MAX_TOTAL_BYTES:
            raise TransactionError("request payload exceeds protocol limit")
        digest = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"),
                                          ensure_ascii=True).encode()).hexdigest()
        return cls(ident, intent, operations, raw["mode"], digest)
