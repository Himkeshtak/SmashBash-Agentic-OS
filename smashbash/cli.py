"""Trusted entrypoint: an agent supplies JSON; an operator selects policy and target."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .bridge import QEMUBridge, QEMUConfig
from .coordinator import Coordinator
from .filesystem import validate_tree
from .image import Image
from .model import TransactionError, load_json
from .policy import Policy
from .storage import ImageTarget, sync_directory


def initialize(template: Path, image: Path, policy: Policy, config: QEMUConfig):
    if image.exists() or image.is_symlink():
        raise TransactionError("refusing to overwrite an existing target")
    image.parent.mkdir(parents=True, exist_ok=True)
    Image(template)
    with tempfile.TemporaryDirectory(prefix=".smashbash-init-", dir=image.parent) as directory:
        shadow = Path(directory) / "shadow.img"
        shutil.copyfile(template, shadow)
        shadow.chmod(0o600)
        bridge = QEMUBridge(config, shadow, policy)
        try:
            bridge.start()
            bridge.begin(0)
            report = bridge.prepare()
            bridge.close(require_clean=True)
            tree = Image(shadow).tree(policy.guest_root)
            validate_tree(tree, policy)
            if report.entries != len(tree) or report.bytes != sum(len(n.data) for n in tree.values()):
                raise TransactionError("initial guest report differs from the image")
            with shadow.open("r+b") as stream:
                os.fsync(stream.fileno())
            os.link(shadow, image)  # Exclusive publication; never overwrites a racing initializer.
            sync_directory(image.parent)
        finally:
            bridge.close()


def parser():
    result = argparse.ArgumentParser(description="Validated xv6 shadow transactions")
    sub = result.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build", help="compile the trusted xv6 template on Linux/WSL")
    build.add_argument("--xv6", type=Path, default=Path("xv6"))
    for command in ("init", "run", "inspect", "recover"):
        cmd = sub.add_parser(command)
        cmd.add_argument("--image", type=Path, required=True)
        cmd.add_argument("--policy", type=Path, required=True)
        cmd.add_argument("--state-dir", type=Path, default=Path(".smashbash"))
        cmd.add_argument("--kernel", type=Path, default=Path("xv6/kernel/kernel"))
        cmd.add_argument("--qemu", default="qemu-system-riscv64")
        if command == "init":
            cmd.add_argument("--template", type=Path, default=Path("xv6/fs.img"))
        if command == "run":
            cmd.add_argument("--request", type=Path, help="JSON file; defaults to stdin")
            cmd.add_argument("--principal", required=True, help="authenticated identity, never from the agent payload")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "build":
            subprocess.run(["make", "-C", str(args.xv6), "kernel/kernel", "fs.img"], check=True)
            return 0
        policy = Policy.load(args.policy)
        config = QEMUConfig(args.kernel, args.qemu)
        if args.command == "init":
            initialize(args.template, args.image, policy, config)
            output = {"state": "INITIALIZED", "image": str(args.image)}
        else:
            target = ImageTarget(args.image, args.state_dir)
            coordinator = Coordinator(target, lambda: Policy.load(args.policy), config)
            if args.command == "run":
                if args.request:
                    with args.request.open("rb") as stream:
                        data = stream.read(512 * 1024 + 1)
                else:
                    data = sys.stdin.buffer.read(512 * 1024 + 1)
                output = coordinator.run(load_json(data), principal=args.principal)
            elif args.command == "recover":
                output = {"transactions": coordinator.recover()}
            else:
                with target.lock():
                    tree = Image(target.path).tree(policy.guest_root)
                    validate_tree(tree, policy)
                    output = {"revision": target.revision(), "entries": {
                        path: {"kind": n.kind, "size": len(n.data), "sha256": n.digest()}
                        for path, n in sorted(tree.items())}}
        print(json.dumps(output, sort_keys=True))
        return 0 if output.get("validated") or output.get("state") not in {"ABORTED", "INDETERMINATE"} else 2
    except (TransactionError, OSError, subprocess.CalledProcessError) as exc:
        print(json.dumps({"state": "REJECTED", "reason": str(exc)}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
