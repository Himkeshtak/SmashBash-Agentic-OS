# Verified correction evidence

Captured on 2026-11-15 from the corrected working tree. [Environment and SHA-256 hashes](environment.json) identify the compiled kernel, fixture disk and source files. The GDB trace labelled `before` comes from the earlier failing intermediate build and is retained as debugging evidence, not as output of the final binary.

| Run | Result | Raw evidence |
|---|---|---|
| Python + real QEMU integration | 54 tests passed | [Full transcript](python-qemu-tests.txt) |
| Windows portable mode | 44 passed, 10 guest tests skipped | [Transcript](windows-tests.txt) |
| Kernel guest suite, one CPU | All nine groups passed | [Transcript](spectest-1cpu.txt) |
| Kernel guest suite, three CPUs | All nine groups passed | [Transcript](spectest-3cpu.txt) |
| Upstream xv6 quick suite | `ALL TESTS PASSED` | [Transcript](usertests-3cpu.txt) |
| Power loss before header | Both old files retained, integrity passed | [GDB + reboot](crash-before-header.txt) |
| Power loss after header | Both new files recovered, integrity passed | [GDB + reboot](crash-after-header.txt) |
| Power loss during install | Both new files recovered, integrity passed | [GDB + reboot](crash-during-install.txt) |

[One-CPU](specbench-1cpu.txt) and [three-CPU](specbench-3cpu.txt) measurements cover 1, 2, 4, 8, 16, 32 and 64 KiB, with three repetitions each. The first six sizes commit; 64 KiB safely aborts because data plus metadata exceed the default 64-dirty-block budget. Columns are guest clock ticks for begin, writes, and finalization. Zero is below timer resolution, not zero cost. These runs are functional measurements, not a hardware performance claim.

[The actual fork panic backtrace](gdb-pipe-reader-before.txt) located a pipe wakeup recursively acquiring the new child's proc lock. Endpoint accounting now runs after that lock is released. The corrected guest suites include fork/pipe/EOF and group lifecycle cases.

Agent integration uses a deterministic inference response and real QEMU execution. Provider HTTP behavior has separate tests. No external provider call or live API key was needed for these results. These are recorded local results, not a claim that remote CI or hidden assignment tests have passed.

Reproduce using the commands in [corrections](../corrections.md). `python3 -m tools.capture_evidence` copies completed verified raw runs into this directory; preserve the full test transcript at `data/kernel-evidence/python-qemu-tests.txt` first.
