# Corrections and verification

The screenshot critique identified a real mismatch: the earlier implementation used host image transactions instead of kernel externalization barriers. These corrections implement the six mechanisms inside xv6.

| Requirement | Implementation | Verification |
|---|---|---|
| Syscalls and inheritance | `kernel/spec.c`, syscall/proc plumbing | Invalid/nested calls, repeated reuse, fork inheritance, child finalization denial, live-child commit rejection |
| Private kernel blocks | View-aware buffer cache and reserved backing | Inside sees private writes; another process in the same VM sees committed data |
| Console/pipe barriers | Buffered external output and tagged internal traffic | Abort marker absent, commit marker once, internal EOF, external commit/abort, mixed-reader denial |
| Create/unlink isolation | Private inode/namespace/bitmap views | Namespace commit/abort; outside open handles survive committed unlink |
| One logged commit | Exclusive redo-log publication, then output | Power loss at three WAL boundaries, reboot and independent integrity inspection |
| Conflicts and bounds | Outside-writer invalidation, fixed budgets, cancellation | Outside writer, four groups, overflow, owner death; one and three CPUs |

`xrun` executes ordinary programs through the kernel API. `shadowd` kernel mode and `smashbash/kernel.py` reconnect typed agent requests to the same mechanism. The LLM is optional and outside the kernel.

## Reproduce

```sh
make -C xv6 -j2 kernel/kernel fs.img
SMASHBASH_QEMU_TESTS=1 python3 -m unittest discover -s tests -v
python3 -m tools.verify_kernel --regression --bench
python3 -m tools.crash_kernel
```

Raw runs are written to ignored `data/kernel-evidence/`; selected verified output is copied into `docs/evidence/`. Test disks are temporary fixtures, not the isolation mechanism. These tests do not constitute a formal proof, hidden grader result or production security audit. Guest tick timing is coarse. The default 64-block dirty budget intentionally rejects 64 KiB writes once metadata is included.

Final local results: **54 host/QEMU tests passed**, all guest cases passed at one and three CPUs, the upstream xv6 quick suite passed, and all three crash-recovery cases passed. Windows portable mode passed 44 tests with 10 guest tests skipped. See [the raw evidence index](evidence/README.md).

## Correction journal — 2026-11-15

This is the actual correction session, not a reconstructed multi-day history.

- Implemented view-aware buffers, private inodes, process-group syscalls and logged publication; initial guest tests passed on three CPUs.
- Added output barriers. Real execution exposed `panic: acquire` in fork endpoint accounting. GDB traced a pipe wakeup acquiring the locked child proc. Moved accounting outside that lock and separated active/saved endpoints and pending output.
- Added conflicts, overflow, descriptor, group capacity, busy-finalization and owner-death tests, executed on one and three CPUs.
- Killed real QEMU guests at log boundaries. Before the durable header both files stayed old; after the header and during installation both new files recovered. Independent inspection verified integrity.
- Upstream `iref` exposed a configuration regression: raising `NINODE` also increased its stress depth beyond disk inode capacity. Restored the original cache limit and added a separate private inode pool, retaining the test unchanged.
- Reconnected typed agent requests to kernel transactions and made that backend the CLI default. Preview uses kernel abort; an ambiguous final reply blocks automatic retry.

The [kernel contract](architecture.md) documents mixed-reader rejection, lazy first reads, conservative conflicts, serialized filesystem syscalls, non-reversible inputs, output crash limits and manual ambiguous-receipt reconciliation.
