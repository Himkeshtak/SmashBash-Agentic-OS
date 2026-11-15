"""Ordinary xv6 user programs exercise kernel isolation inside a single VM."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest
import json
from unittest.mock import patch
from dataclasses import replace

from smashbash.bridge import QEMUBridge, QEMUConfig
from tests.helpers import ROOT, policy
from tests.helpers import request, write
from smashbash.cli import initialize
from smashbash.image import Image
from smashbash.kernel import KernelCoordinator
from smashbash.kernel import KernelBridge
from smashbash.agent import Agent
from smashbash.model import TransactionError
from smashbash.storage import ImageTarget


class TranscriptGuest(QEMUBridge):
    def __init__(self, *args):
        super().__init__(*args)
        self.transcript = bytearray()

    def _byte(self):
        byte = super()._byte()
        self.transcript += byte
        return byte


@unittest.skipUnless(os.environ.get("SMASHBASH_QEMU_TESTS") == "1", "requires built xv6/QEMU")
class KernelSpeculationTests(unittest.TestCase):
    def test_lost_reply_blocks_retry_after_real_kernel_commit(self):
        class LostReply(KernelBridge):
            def prepare(self):
                super().prepare()
                raise TransactionError("simulated lost final reply")

        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "kernel.img"
            config = QEMUConfig(ROOT / "xv6/kernel/kernel", cpus=3)
            initialize(ROOT / "xv6/fs.img", image, policy(), config)
            service = KernelCoordinator(ImageTarget(image, Path(directory) / "state"), policy, config)
            raw = request(write("durable", b"survives-reply-loss"))
            with patch("smashbash.kernel.KernelBridge", LostReply):
                result = service.run(raw, principal="local-user")
            self.assertEqual(result["state"], "INDETERMINATE", result)
            self.assertEqual(Image(image).tree("/data")["durable"].data, b"survives-reply-loss")
            with self.assertRaisesRegex(TransactionError, "unresolved kernel outcome"):
                service.run(raw, principal="local-user")

    def test_agent_neutral_kernel_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "kernel.img"
            config = QEMUConfig(ROOT / "xv6/kernel/kernel", cpus=3)
            initialize(ROOT / "xv6/fs.img", image, policy(), config)
            service = KernelCoordinator(ImageTarget(image, Path(directory) / "state"), policy, config)
            baseline = image.read_bytes()
            result = service.run(request(write("private", b"hidden"), mode="preview"), principal="local-user")
            self.assertEqual(result["state"], "ABORTED", result)
            self.assertTrue(result.get("validated"), result)
            self.assertEqual(image.read_bytes(), baseline)
            result = service.run(request(write("committed", b"kernel-published"), ident="commit"), principal="local-user")
            self.assertEqual(result["state"], "COMMITTED", result)
            self.assertEqual(Image(image).tree("/data")["committed"].data, b"kernel-published")
            class ProposedByLLM:
                def complete(self, messages, *, schema):
                    return json.dumps({"operations": [write("from-agent", b"agent-to-kernel")], "error": None})

            agent = Agent(ProposedByLLM(), policy, principal="local-user", coordinator=service)
            result = agent.run("Write agent-to-kernel to from-agent", mode="commit", request_id="agent")
            self.assertEqual(result["state"], "COMMITTED", result)
            self.assertEqual(Image(image).tree("/data")["from-agent"].data, b"agent-to-kernel")
            result = service.run(request({"op": "fs.read", "path": "committed"}, ident="read"), principal="local-user")
            self.assertEqual(result["state"], "COMMITTED", result)
            large_policy = replace(policy(), max_file_bytes=65536)
            large_service = KernelCoordinator(service.target, lambda: large_policy, config)
            result = large_service.run(request(write("too-large", b"z" * 65536), ident="overflow"), principal="local-user")
            self.assertEqual(result["state"], "ABORTED", result)
            self.assertNotIn("too-large", Image(image).tree("/data"))

    def test_ordinary_command_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "kernel.img"
            shutil.copyfile(ROOT / "xv6/fs.img", image)
            guest = TranscriptGuest(QEMUConfig(ROOT / "xv6/kernel/kernel", cpus=3), image,
                                    replace(policy(), timeout_seconds=30))
            try:
                guest.boot()
                for command in ("xrun mkdir live", "xrun -n mkdir hidden", "xrun echo LAUNCH_COMMIT",
                                "xrun -n echo LAUNCH_ABORT", "xrun rm absent"):
                    guest._send(command)
                    guest._until(b"$ ")
                guest.close(require_clean=True)
                fs = Image(image)
                self.assertIn("/live", fs.paths)
                self.assertNotIn("/hidden", fs.paths)
                # Console input is echoed; require a separate output line.
                normalized = bytes(guest.transcript).replace(b"\r\n", b"\n")
                self.assertIn(b"\nLAUNCH_COMMIT\n", normalized)
                self.assertNotIn(b"\nLAUNCH_ABORT\n", normalized)
            finally:
                guest.close()

    def test_guest_speculation(self):
        for cpus in (1, 3):
            with self.subTest(cpus=cpus):
                self.run_guest(cpus)

    def run_guest(self, cpus):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "kernel.img"
            shutil.copyfile(ROOT / "xv6/fs.img", image)
            guest = TranscriptGuest(QEMUConfig(ROOT / "xv6/kernel/kernel", cpus=cpus), image,
                                    replace(policy(), timeout_seconds=60))
            try:
                guest.boot()
                guest._send("spectest")
                guest._until(b"SPECTEST ALL PASS")
                guest._until(b"$ ")
                self.assertNotIn(b"SPECTEST FAIL", guest.transcript)
                self.assertNotIn(b"CONSOLE_MUST_NOT_ESCAPE", guest.transcript)
                self.assertEqual(guest.transcript.count(b"CONSOLE_COMMITTED_ONCE"), 1)
                guest.close(require_clean=True)
            except Exception as exc:
                self.fail(f"{exc}\n{guest.transcript.decode(errors='replace')[-5000:]}")
            finally:
                guest.close()
                evidence = ROOT / "data/kernel-evidence"
                evidence.mkdir(parents=True, exist_ok=True)
                (evidence / f"spectest-{cpus}cpu.log").write_bytes(guest.transcript)
