"""Two validation boundaries, isolated execution, and durable commit/abort decisions."""

from __future__ import annotations

import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .bridge import Prepared, QEMUBridge, QEMUConfig
from .filesystem import changed_paths, evaluate, validate_tree
from .image import Image
from .model import Request, TransactionError
from .policy import Policy
from .storage import StorageTarget
from .validators import AdmissionCheck, FinalCheck

TERMINAL = {"COMMITTED", "ABORTED", "INDETERMINATE"}


def event(state):
    return {"state": state, "at": datetime.now(timezone.utc).isoformat()}


class Coordinator:
    def __init__(self, target: StorageTarget, policy: Callable[[], Policy], config: QEMUConfig,
                 bridge_factory=QEMUBridge, *, admission_checks: tuple[AdmissionCheck, ...] = (),
                 final_checks: tuple[FinalCheck, ...] = ()):
        self.target, self.policy, self.config = target, policy, config
        self.bridge_factory = bridge_factory
        self.admission_checks, self.final_checks = admission_checks, final_checks

    def _admit(self, request, principal, policy):
        policy.admit(request, principal)
        for check in self.admission_checks:
            check(request, principal, policy)

    def _recover(self, record):
        state = record["state"]
        if state in TERMINAL:
            return record
        if state == "COMMITTING":
            current = self.target.revision()
            if current == record.get("after"):
                try:
                    self.target.confirm_publication(current)
                    state, reason = "COMMITTED", "recovered published image"
                except (OSError, TransactionError):
                    state, reason = "INDETERMINATE", "could not confirm publication durability"
            elif current == record.get("baseline"):
                state, reason = "ABORTED", "publication did not occur"
            else:
                state, reason = "INDETERMINATE", "target differs from both commit revisions; operator recovery required"
        else:
            state, reason = "ABORTED", "interrupted before publication"
        record.update(state=state, reason=reason)
        record.setdefault("events", []).append(event(state))
        record["result"] = {"request_id": record["request_id"], "state": state, "reason": reason,
                            "changed_paths": record.get("changed_paths", [])}
        self.target.record(record["request_id"], record)
        return record

    def recover(self):
        with self.target.lock():
            return self._recover_pending()

    def _recover_pending(self):
        records = []
        for path in sorted(self.target.journal.glob("*.json")):
            record = self.target.read_record(path.stem)
            records.append(self._recover(record))
        return records

    def run(self, raw, *, principal: str):
        request = Request.parse(raw)
        policy = self.policy()
        with self.target.lock():
            records = self._recover_pending()
            if any(r["state"] == "INDETERMINATE" for r in records):
                raise TransactionError("target has an indeterminate publication; operator recovery required")
            previous = self.target.read_record(request.request_id)
            if previous:
                if previous["request_digest"] != request.digest or previous["principal"] != principal:
                    raise TransactionError("request_id was already used by another request or principal")
                self._admit(request, principal, policy)
                return dict(previous["result"], replayed=True)
            record = {"version": 1, "request_id": request.request_id,
                      "request_digest": request.digest, "principal": principal,
                      "policy_digest": policy.fingerprint, "target": self.target.identity,
                      "state": "RECEIVED", "events": [event("RECEIVED")]}
            self.target.record(request.request_id, record)

            def transition(state, **values):
                next_record = dict(record, state=state, **values)
                next_record["events"] = record["events"] + [event(state)]
                self.target.record(request.request_id, next_record)
                record.clear()
                record.update(next_record)

            bridge = None
            try:
                self._admit(request, principal, policy)
                transition("ADMITTED")
                with tempfile.TemporaryDirectory(prefix="trial-", dir=self.target.journal) as directory:
                    shadow, baseline = self.target.snapshot(Path(directory))
                    before = Image(shadow)
                    original = before.tree(policy.guest_root)
                    expected, observations = evaluate(original, request, policy)
                    changes = changed_paths(original, expected)
                    transition("EXECUTING", baseline=baseline, changed_paths=changes)
                    bridge = self.bridge_factory(self.config, shadow, policy)
                    try:
                        bridge.start()
                        bridge.begin(len(request.operations))
                        for operation in request.operations:
                            bridge.execute(operation)
                        prepared = bridge.prepare()
                        bridge.close(require_clean=True)
                    finally:
                        bridge.close()
                    bridge = None
                    after = Image(shadow)
                    actual = after.tree(policy.guest_root)
                    validate_tree(actual, policy)
                    if actual != expected:
                        raise TransactionError("guest effects do not match the admitted operations")
                    if after.outside(policy.guest_root) != before.outside(policy.guest_root):
                        raise TransactionError("guest changed state outside its managed root")
                    metrics = Prepared(len(request.operations), len(actual), sum(len(n.data) for n in actual.values()))
                    if prepared != metrics:
                        raise TransactionError("guest preparation report does not match independent inspection")
                    current_policy = self.policy()
                    if current_policy.fingerprint != policy.fingerprint:
                        raise TransactionError("policy changed during execution; submit a new request")
                    self._admit(request, principal, current_policy)
                    for check in self.final_checks:
                        check(request, original, actual, current_policy)
                    if self.target.revision() != baseline:
                        raise TransactionError("target conflict: live image changed")
                    after_digest = self.target.prospective_revision(shadow)
                    transition("PREPARED", after=after_digest)
                    if request.mode == "preview":
                        result = {"request_id": request.request_id, "state": "ABORTED",
                                  "reason": "preview validated; shadow discarded", "validated": True,
                                  "changed_paths": changes}
                        transition("ABORTED", result=result)
                        return result
                    transition("COMMITTING")
                    self.target.publish(shadow, baseline, after_digest)
                    result = {"request_id": request.request_id, "state": "COMMITTED",
                              "changed_paths": changes, "revision": after_digest}
                    transition("COMMITTED", result=result)
                    # Release content after commit; do not store content in the audit record.
                    return dict(result, observations=observations)
            except Exception as exc:
                if record["state"] in {"COMMITTING", "COMMITTED"}:
                    return self._recover(record)["result"]
                reason = str(exc) if isinstance(exc, (TransactionError, OSError)) else "internal transaction error"
                result = {"request_id": request.request_id, "state": "ABORTED", "reason": reason,
                          "changed_paths": record.get("changed_paths", [])}
                transition("ABORTED", result=result)
                return result
            finally:
                if bridge is not None:
                    bridge.close()
