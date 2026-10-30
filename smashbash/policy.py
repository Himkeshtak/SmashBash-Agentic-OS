"""Trusted admission policy. Identity is supplied by the caller's authentication layer."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from .model import (MAX_ENTRIES, MAX_FILE_BYTES, MAX_OPERATIONS, MAX_TOTAL_BYTES,
                    OPERATIONS, Request, TransactionError, canonical_path, load_json)


def under(path: str, prefix: str) -> bool:
    return prefix == "." or path == prefix or path.startswith(prefix + "/")


@dataclass(frozen=True)
class Grant:
    operations: frozenset[str]
    prefixes: tuple[str, ...]


@dataclass(frozen=True)
class Policy:
    grants: dict[str, Grant]
    protected_paths: tuple[str, ...] = ()
    denied_intent_patterns: tuple[str, ...] = ()
    max_operations: int = 16
    max_entries: int = 100
    max_file_bytes: int = 32768
    max_total_bytes: int = 131072
    timeout_seconds: int = 30
    guest_root: str = "/data"
    fingerprint: str = ""

    @classmethod
    def load(cls, path: Path) -> Policy:
        return cls.parse(load_json(path.read_bytes()))

    @classmethod
    def parse(cls, raw) -> Policy:
        fields = {"version", "principals", "protected_paths", "denied_intent_patterns", "limits", "guest_root"}
        if not isinstance(raw, dict) or set(raw) != fields or type(raw["version"]) is not int or raw["version"] != 1:
            raise TransactionError("invalid policy schema")
        limits = raw["limits"]
        ceilings = {"max_operations": MAX_OPERATIONS, "max_entries": MAX_ENTRIES,
                    "max_file_bytes": MAX_FILE_BYTES, "max_total_bytes": MAX_TOTAL_BYTES,
                    "timeout_seconds": 300}
        if not isinstance(limits, dict) or set(limits) != set(ceilings):
            raise TransactionError("invalid policy limits")
        for name, ceiling in ceilings.items():
            if type(limits[name]) is not int or not 1 <= limits[name] <= ceiling:
                raise TransactionError(f"invalid policy limit: {name}")
        root = raw["guest_root"]
        if not isinstance(root, str) or not root.startswith("/") or len(root) > 24:
            raise TransactionError("guest_root must be a short absolute directory")
        canonical_path(root[1:])
        if root in {"/console"} or "/" in root[1:]:
            raise TransactionError("guest_root conflicts with a system device")
        principals = raw["principals"]
        if not isinstance(principals, dict) or not principals:
            raise TransactionError("policy must explicitly grant at least one principal")
        grants = {}
        for principal, grant in principals.items():
            if not isinstance(principal, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", principal):
                raise TransactionError("invalid principal in policy")
            if not isinstance(grant, dict) or set(grant) != {"operations", "path_prefixes"}:
                raise TransactionError("invalid permission grant")
            ops, prefixes = grant["operations"], grant["path_prefixes"]
            if not isinstance(ops, list) or not ops or any(not isinstance(x, str) or x not in OPERATIONS for x in ops):
                raise TransactionError("permission grant contains an unknown operation")
            if not isinstance(prefixes, list) or not prefixes:
                raise TransactionError("permission grant needs path prefixes")
            grants[principal] = Grant(frozenset(ops), tuple(canonical_path(p, root_allowed=True) for p in prefixes))
        protected, patterns = raw["protected_paths"], raw["denied_intent_patterns"]
        if not isinstance(protected, list) or not isinstance(patterns, list):
            raise TransactionError("invalid policy protection lists")
        protected = tuple(canonical_path(p, root_allowed=True) for p in protected)
        for pattern in patterns:
            if not isinstance(pattern, str) or len(pattern) > 256:
                raise TransactionError("invalid intent pattern")
            try:
                re.compile(pattern)
            except re.error as exc:
                raise TransactionError("invalid intent regular expression") from exc
        fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        return cls(grants, protected, tuple(patterns), guest_root=root, fingerprint=fingerprint, **limits)

    def admit(self, request: Request, principal: str) -> None:
        if not isinstance(principal, str):
            raise TransactionError("authenticated principal must be a string")
        grant = self.grants.get(principal)
        if grant is None:
            raise TransactionError("principal has no access grant")
        if len(request.operations) > self.max_operations:
            raise TransactionError("operation budget exceeded")
        if sum(len(op.content) for op in request.operations) > self.max_total_bytes:
            raise TransactionError("request byte budget exceeded")
        if any(re.search(p, request.intent, re.IGNORECASE) for p in self.denied_intent_patterns):
            raise TransactionError("intent rejected by a configured threat indicator")
        for op in request.operations:
            if op.op not in grant.operations:
                raise TransactionError(f"operation is not permitted: {op.op}")
            if len(op.content) > self.max_file_bytes:
                raise TransactionError("file payload exceeds policy")
            for path in (op.path, op.destination):
                if path is None:
                    continue
                if not any(under(path, prefix) for prefix in grant.prefixes):
                    raise TransactionError("path is outside the principal's permission scope")
                # Directory observations and removals can affect an entire subtree.
                if any(under(path, p) or under(p, path) for p in self.protected_paths):
                    raise TransactionError("operation overlaps a protected path")
