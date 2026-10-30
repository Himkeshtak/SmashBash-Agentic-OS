"""Trusted integration hooks; agents cannot register executable validators."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Protocol

from .filesystem import Node
from .model import Request, TransactionError
from .policy import Policy


class AdmissionCheck(Protocol):
    def __call__(self, request: Request, principal: str, policy: Policy) -> None: ...


class FinalCheck(Protocol):
    def __call__(self, request: Request, before: dict[str, Node], after: dict[str, Node],
                 policy: Policy) -> None: ...


@dataclass(frozen=True)
class ContentHashDenylist:
    """An example malware-signature adapter, backed by operator-provided SHA-256 values."""

    hashes: frozenset[str]

    def __call__(self, request: Request, principal: str, policy: Policy) -> None:
        for operation in request.operations:
            if operation.content and hashlib.sha256(operation.content).hexdigest() in self.hashes:
                raise TransactionError("payload rejected by a configured content signature")
