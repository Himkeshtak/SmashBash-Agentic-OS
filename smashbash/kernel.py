"""Agent-neutral client of xv6 kernel transactions on the attached persistent disk.

The VM uses the target disk directly. Private effects, final validation, logging,
and abort are enforced by xv6; this module never snapshots or replaces the disk.
"""
from __future__ import annotations

from .bridge import Prepared, QEMUBridge
from .filesystem import changed_paths, evaluate, validate_tree
from .image import Image
from .model import Request, TransactionError, load_json
from .storage import durable_json


class KernelBridge(QEMUBridge):
    def start_mode(self, mode):
        if mode not in {"preview", "commit"}:
            raise TransactionError("invalid kernel mode")
        self.boot()
        self._send("shadowd kernel-" + mode)
        if self._response() != "@SB1 READY 2":
            raise TransactionError("guest lacks the kernel transaction protocol; rebuild and initialize an upgraded guest image")


class KernelCoordinator:
    def __init__(self, target, policy, config, *, admission_checks=(), final_checks=()):
        self.target, self.policy, self.config = target, policy, config
        self.admission_checks, self.final_checks = admission_checks, final_checks

    def run(self, raw, *, principal):
        request = Request.parse(raw)
        policy = self.policy()
        policy.admit(request, principal)
        for check in self.admission_checks:
            check(request, principal, policy)
        with self.target.lock():
            receipts = self.target.journal / "kernel-receipts"
            receipts.mkdir(exist_ok=True, mode=0o700)
            path = receipts / (request.request_id + ".json")
            previous = None
            for item in receipts.glob("*.json"):
                record = load_json(item.read_bytes())
                if record["state"] in {"EXECUTING", "INDETERMINATE"}:
                    raise TransactionError("unresolved kernel outcome: reboot/recover the disk and reconcile the receipt before retrying")
                if item == path: previous = record
            if previous:
                if previous["digest"] != request.digest or previous["principal"] != principal:
                    raise TransactionError("request_id already belongs to another request or principal")
                return dict(previous["result"], replayed=True)
            before = Image(self.target.path)
            original = before.tree(policy.guest_root)
            expected, observations = evaluate(original, request, policy)
            for check in self.final_checks:
                check(request, original, expected, policy)
            changes = changed_paths(original, expected)
            bridge = KernelBridge(self.config, self.target.path, policy)
            finalizing = False
            record = {"backend": "xv6-kernel", "digest": request.digest,
                      "principal": principal, "request_id": request.request_id, "state": "EXECUTING"}
            durable_json(path, record)
            try:
                bridge.start_mode(request.mode)
                bridge.begin(len(request.operations))
                for operation in request.operations: bridge.execute(operation)
                current = self.policy()
                if current.fingerprint != policy.fingerprint:
                    raise TransactionError("policy changed before kernel finalization")
                current.admit(request, principal)
                finalizing = True
                prepared = bridge.prepare()  # Guest validates and calls xcommit/xabort.
                bridge.close(require_clean=True)
                after = Image(self.target.path)
                actual = after.tree(policy.guest_root)
                validate_tree(actual, policy)
                wanted = original if request.mode == "preview" else expected
                if actual != wanted or after.outside(policy.guest_root) != before.outside(policy.guest_root):
                    raise TransactionError("post-decision inspection mismatch; manual reconciliation required")
                metrics = Prepared(len(request.operations), len(expected), sum(len(n.data) for n in expected.values()))
                if prepared != metrics:
                    raise TransactionError("kernel preparation metrics mismatch")
                result = {"request_id": request.request_id, "backend": "xv6-kernel",
                          "state": "ABORTED" if request.mode == "preview" else "COMMITTED",
                          "validated": True, "changed_paths": changes}
                if request.mode == "preview": result["reason"] = "validated and discarded by xabort"
                else: result["revision"] = self.target.revision()
            except (TransactionError, OSError) as exc:
                # A lost reply after F cannot establish whether the durable WAL
                # commit happened. Never retry or label that outcome ABORTED.
                rejected = str(exc).startswith("xv6 validator rejected the transaction:")
                result = {"request_id": request.request_id, "backend": "xv6-kernel",
                          "state": "INDETERMINATE" if finalizing and not rejected else "ABORTED",
                          "reason": str(exc), "changed_paths": changes}
            finally:
                bridge.close()
            record.update(state=result["state"], result=result)
            durable_json(path, record)
            if result["state"] == "COMMITTED":
                return dict(result, observations=observations)
            return result
