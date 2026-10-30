"""Opt-in real guest integration: SMASHBASH_QEMU_TESTS=1 python -m unittest ..."""

import base64
import os
import tempfile
import unittest
from pathlib import Path

from smashbash.bridge import QEMUBridge, QEMUConfig
from smashbash.cli import initialize
from smashbash.coordinator import Coordinator
from smashbash.filesystem import Node
from smashbash.image import Image
from smashbash.model import TransactionError
from smashbash.storage import ImageTarget
from tests.helpers import ROOT, policy, request, write


@unittest.skipUnless(os.environ.get("SMASHBASH_QEMU_TESTS") == "1", "requires built xv6 and QEMU")
class QEMUIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.config = QEMUConfig(ROOT / "xv6/kernel/kernel")
        cls.policy = policy()
        cls.template = cls.root / "initialized.img"
        initialize(ROOT / "xv6/fs.img", cls.template, cls.policy, cls.config)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        import shutil
        self.case = tempfile.TemporaryDirectory(dir=self.root)
        root = Path(self.case.name)
        image = root / "device.img"
        shutil.copyfile(self.template, image)
        self.target = ImageTarget(image, root / "state")
        self.coordinator = Coordinator(self.target, lambda: self.policy, self.config)

    def tearDown(self):
        self.case.cleanup()

    def run_request(self, raw):
        return self.coordinator.run(raw, principal="local-user")

    def test_commit_and_fresh_vm_read_observe_persistent_binary_data(self):
        payload = bytes(range(256)) * 64  # Exercises indirect blocks and binary-safe framing.
        result = self.run_request(request({"op": "fs.mkdir", "path": "notes"}, write("notes/bin", payload),
                                         write("notes/bin", b"tail", "fs.append"),
                                         {"op": "fs.rename", "path": "notes/bin", "destination": "notes/final"},
                                         {"op": "fs.list", "path": "notes"},
                                         {"op": "fs.stat", "path": "notes/final"},
                                         {"op": "fs.read", "path": "notes/final"}))
        self.assertEqual(result["state"], "COMMITTED", result)
        self.assertEqual(base64.b64decode(result["observations"][-1]["content_b64"]), payload + b"tail")
        # A second transaction creates a new QEMU process and reads the published disk.
        result = self.run_request(request({"op": "fs.read", "path": "notes/final"}, ident="fresh-boot"))
        self.assertEqual(result["state"], "COMMITTED", result)
        self.assertEqual(Image(self.target.path).tree("/data")["notes/final"].data, payload + b"tail")

    def test_validated_preview_is_discarded_and_delete_commits(self):
        before = self.target.path.read_bytes()
        result = self.run_request(request(mode="preview"))
        self.assertTrue(result.get("validated"), result)
        self.assertEqual(self.target.path.read_bytes(), before)
        result = self.run_request(request({"op": "fs.mkdir", "path": "empty"}, write("empty/a"),
                                         {"op": "fs.remove", "path": "empty/a"},
                                         {"op": "fs.remove", "path": "empty"}, ident="delete"))
        self.assertEqual(result["state"], "COMMITTED", result)
        self.assertEqual(Image(self.target.path).tree("/data"), {".": Node("directory")})

    def test_guest_rejects_invalid_frames_and_traversal_even_without_host_admission(self):
        import shutil
        for kind in ("traversal", "unknown", "count", "quota"):
            shadow = Path(self.case.name) / (kind + ".img")
            shutil.copyfile(self.template, shadow)
            bridge = QEMUBridge(self.config, shadow, self.policy)
            try:
                bridge.start()
                bridge.begin(1)
                with self.subTest(kind=kind), self.assertRaises(TransactionError):
                    if kind == "traversal":
                        bridge._ok("O W " + b"../README".hex() + " - 0")
                    elif kind == "unknown":
                        bridge._ok("O X " + b"note".hex() + " - 0")
                    elif kind == "quota":
                        bridge._ok("O W " + b"note".hex() + " - 65536")
                    else:
                        bridge.prepare()  # Declared operation was never executed.
            finally:
                bridge.close()

    def test_kernel_checker_rejects_allocation_bitmap_corruption_in_guest(self):
        import shutil
        import struct
        shadow = Path(self.case.name) / "corrupt.img"
        shutil.copyfile(self.template, shadow)
        raw = bytearray(shadow.read_bytes())
        bmapstart = struct.unpack_from("<I", raw, 1024 + 28)[0]
        raw[bmapstart * 1024 + 1900 // 8] |= 1 << (1900 % 8)
        shadow.write_bytes(raw)
        bridge = QEMUBridge(self.config, shadow, self.policy)
        try:
            bridge.start()
            bridge.begin(0)
            with self.assertRaisesRegex(TransactionError, "kernel-integrity"):
                bridge.prepare()
        finally:
            bridge.close()

    def test_guest_failure_after_a_real_shadow_write_never_changes_the_live_disk(self):
        class AbortSecondOperation(QEMUBridge):
            def execute(self, operation):
                if getattr(self, "executed", False):
                    self._ok("X")
                super().execute(operation)
                self.executed = True

        baseline = self.target.path.read_bytes()
        service = Coordinator(self.target, lambda: self.policy, self.config, AbortSecondOperation)
        result = service.run(request(write("first", b"trial"), write("second", b"trial")), principal="local-user")
        self.assertEqual(result["state"], "ABORTED", result)
        self.assertIn("xv6 validator rejected", result["reason"])
        self.assertEqual(self.target.path.read_bytes(), baseline)
