import hashlib
import tempfile
import unittest
from pathlib import Path

from smashbash.bridge import Prepared, QEMUConfig
from smashbash.coordinator import Coordinator
from smashbash.filesystem import Node, evaluate
from smashbash.image import Image
from smashbash.model import Request, TransactionError
from smashbash.storage import ImageTarget
from smashbash.validators import ContentHashDenylist
from tests.helpers import make_image, policy, request, write


class ModelBridge:
    """Fault-injection double; actual guest behavior has separate QEMU tests."""

    def __init__(self, config, image, policy):
        self.image, self.policy = image, policy
        self.before = Image(image).tree(policy.guest_root)
        self.operations = []
        self.closed = False

    def start(self):
        pass

    def begin(self, count):
        self.count = count

    def execute(self, operation):
        self.operations.append(operation)

    def prepare(self):
        parsed = Request("model", "test", tuple(self.operations), "commit", "model")
        self.after, _ = evaluate(self.before, parsed, self.policy)
        make_image(self.image, self.after)
        return Prepared(self.count, len(self.after), sum(len(n.data) for n in self.after.values()))

    def close(self, **kwargs):
        self.closed = True


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        image = self.root / "device.img"
        make_image(image)
        self.target = ImageTarget(image, self.root / "state")
        self.policy = policy()
        self.config = QEMUConfig(Path("unused"))
        self.created = []

        def factory(*args):
            bridge = ModelBridge(*args)
            self.created.append(bridge)
            return bridge

        self.coordinator = Coordinator(self.target, lambda: self.policy, self.config, factory)

    def tearDown(self):
        self.temp.cleanup()

    def run_request(self, raw=None, principal="local-user"):
        return self.coordinator.run(raw or request(), principal=principal)

    def test_commit_persists_and_replay_does_not_repeat_execution(self):
        result = self.run_request(request(write("note", b"secret-value"), {"op": "fs.read", "path": "note"}))
        self.assertEqual(result["state"], "COMMITTED")
        self.assertEqual(Image(self.target.path).tree("/data")["note"].data, b"secret-value")
        replay = self.run_request(request(write("note", b"secret-value"), {"op": "fs.read", "path": "note"}))
        self.assertTrue(replay["replayed"])
        self.assertNotIn("observations", replay)
        self.assertEqual(len(self.created), 1)
        journal = next(self.target.journal.glob("*.json")).read_text()
        self.assertNotIn("secret-value", journal)
        self.assertNotIn(result["observations"][1]["content_b64"], journal)

    def test_preview_and_admission_denial_leave_target_byte_identical(self):
        baseline = self.target.path.read_bytes()
        result = self.run_request(request(mode="preview"))
        self.assertTrue(result["validated"])
        self.assertEqual(result["state"], "ABORTED")
        self.assertEqual(self.target.path.read_bytes(), baseline)
        result = self.run_request(request(ident="denied"), principal="reader")
        self.assertEqual(result["state"], "ABORTED")
        self.assertEqual(len(self.created), 1)  # Denial never created another bridge.
        self.assertEqual(self.target.path.read_bytes(), baseline)

    def test_permission_revocation_and_external_conflict_abort(self):
        original = self.coordinator.bridge_factory
        for kind in ("revoke", "conflict"):
            self.policy = policy()
            baseline = self.target.path.read_bytes()

            def factory(*args):
                bridge = original(*args)
                prepare = bridge.prepare

                def changed():
                    report = prepare()
                    if kind == "revoke":
                        from tests.helpers import policy_raw
                        from smashbash.policy import Policy
                        raw = policy_raw()
                        raw["principals"]["local-user"]["operations"] = ["fs.read"]
                        self.policy = Policy.parse(raw)
                    else:
                        make_image(self.target.path, {".": Node("directory"), "other": Node("file", b"external")})
                    return report

                bridge.prepare = changed
                return bridge

            self.coordinator.bridge_factory = factory
            result = self.run_request(request(ident=kind))
            self.assertEqual(result["state"], "ABORTED")
            if kind == "revoke":
                self.assertEqual(self.target.path.read_bytes(), baseline)
            else:
                self.assertIn("other", Image(self.target.path).tree("/data"))

    def test_guest_failure_unexpected_effects_and_forged_report_fail_closed(self):
        original = self.coordinator.bridge_factory
        baseline = self.target.path.read_bytes()
        for kind in ("failure", "unexpected", "protected", "report", "corrupt"):
            def factory(*args):
                bridge = original(*args)
                prepare = bridge.prepare

                def faulty():
                    if kind == "failure":
                        raise TransactionError("guest aborted")
                    report = prepare()
                    if kind == "unexpected":
                        make_image(bridge.image, {".": Node("directory"), "extra": Node("file", b"x")})
                    elif kind == "protected":
                        make_image(bridge.image, bridge.after, guard=b"tampered")
                    elif kind == "corrupt":
                        raw = bytearray(bridge.image.read_bytes())
                        raw[1024] = 0
                        bridge.image.write_bytes(raw)
                    else:
                        return Prepared(99, 99, 99)
                    return report

                bridge.prepare = faulty
                return bridge

            self.coordinator.bridge_factory = factory
            with self.subTest(kind=kind):
                result = self.run_request(request(ident=kind))
                self.assertEqual(result["state"], "ABORTED")
                self.assertEqual(self.target.path.read_bytes(), baseline)
                self.assertTrue(self.created[-1].closed)

    def test_request_id_collision_is_rejected(self):
        self.run_request()
        with self.assertRaises(TransactionError):
            self.run_request(request(write("different")))

    def test_commit_recovery_resolves_publication_after_process_interruption(self):
        record = {"request_id": "interrupted", "state": "COMMITTING",
                  "request_digest": "digest", "principal": "local-user",
                  "baseline": "old", "after": self.target.revision()}
        self.target.record("interrupted", record)
        recovered = self.coordinator.recover()[0]
        self.assertEqual(recovered["state"], "COMMITTED")

    def test_uncertain_recovery_blocks_further_commits_without_reverting_data(self):
        baseline = self.target.path.read_bytes()
        self.target.record("interrupted", {"request_id": "interrupted", "state": "COMMITTING",
                                           "baseline": "old", "after": "unrelated"})
        with self.assertRaises(TransactionError):
            self.run_request()
        self.assertEqual(self.target.path.read_bytes(), baseline)
        self.assertEqual(self.target.read_record("interrupted")["state"], "INDETERMINATE")

    def test_second_writer_cannot_enter_and_stale_lock_file_is_reusable(self):
        other = ImageTarget(self.target.path, self.target.state_dir)
        with self.target.lock():
            with self.assertRaises(TransactionError), other.lock():
                self.fail("second writer acquired the lock")
        with other.lock():
            pass

    def test_signature_scanner_and_final_acceptance_hook_can_veto(self):
        before = self.target.path.read_bytes()
        self.coordinator.admission_checks = (ContentHashDenylist(frozenset({hashlib.sha256(b"blocked").hexdigest()})),)
        result = self.run_request(request(write("note", b"blocked"), ident="signature"))
        self.assertEqual(result["state"], "ABORTED")
        self.assertEqual(self.created, [])

        def reject(request, before, after, policy):
            raise TransactionError("application invariant failed")

        self.coordinator.final_checks = (reject,)
        result = self.run_request(request(ident="final-veto"))
        self.assertEqual(result["state"], "ABORTED")
        self.assertEqual(self.target.path.read_bytes(), before)

    def test_failure_to_write_committed_receipt_never_reports_abort_after_publication(self):
        original_record = self.target.record
        attempts = []

        def fail_once(ident, record):
            if record["state"] == "COMMITTED" and not attempts:
                attempts.append(True)
                raise OSError("simulated receipt write failure")
            original_record(ident, record)

        self.target.record = fail_once
        result = self.run_request()
        self.assertEqual(result["state"], "COMMITTED", result)
        self.assertIn("note.txt", Image(self.target.path).tree("/data"))
        self.assertEqual(self.target.read_record("trial-1")["state"], "COMMITTED")

    def test_unconfirmed_durability_is_indeterminate_after_replacement(self):
        publish = self.target.publish

        def uncertain(*args):
            publish(*args)
            raise OSError("simulated directory flush failure")

        def cannot_confirm(revision):
            raise OSError("storage still unavailable")

        self.target.publish = uncertain
        self.target.confirm_publication = cannot_confirm
        result = self.run_request()
        self.assertEqual(result["state"], "INDETERMINATE", result)
        self.assertIn("note.txt", Image(self.target.path).tree("/data"))
        with self.assertRaises(TransactionError):
            self.run_request(request(ident="next"))
