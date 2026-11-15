# Kernel transaction architecture

The complete Mermaid diagram is in [README](../README.md#complete-architecture). Ordinary speculative and outside processes run within one xv6 kernel. The whole-image prototype is an optional legacy backend.

## Private state

`struct proc` carries group membership, an internal I/O-view override, saved descriptors and saved cwd. `xbegin` reserves backing RAM, detaches inode-backed file objects and offsets from outside users, and changes cwd to its private inode view. Duplicated descriptors retain their alias relationship. `fork` inherits membership; `exec` retains it.

The buffer cache keys entries by `(device, block, speculation)`. First access copies a committed block into private backing RAM. `log_write` records private bytes and dirty blocks; `bwrite` rejects private buffers. File data, directories, bitmap and indirect blocks use the same path. A separate private inode cache prevents sizes, links and block pointers leaking to outsiders.

RAM is reserved before admission so a failing multi-block syscall can unwind within private state. Dirty/output exhaustion fails the group and never falls back to real writes. Private dirty accounting may briefly exceed the logical budget while a syscall unwinds, but that group cannot publish.

## Synchronization and conflicts

A sleeping filesystem gate serializes filesystem syscalls, exit cleanup and final publication. Blocking console/pipe I/O and wait do not hold it. This supports concurrent processes and groups interleaved between syscalls, while prioritizing correctness over filesystem throughput.

Pipe endpoint registration during fork occurs after releasing the child's proc lock. Otherwise pipe wakeup can recursively acquire that lock. The captured GDB evidence records this actual bug and the resulting correction.

Ordinary `log_write` invalidates active groups that read its block. The outside writer proceeds. A committing group similarly invalidates affected peers. Conflicts include metadata blocks, so conservative false conflicts are expected. Unread blocks are copied at their current committed value; begin does not create a globally frozen snapshot.

## Final validator and commit

The owner requests `xcommit` after joining children. Under the filesystem gate, the kernel checks:

1. Valid ownership and no remaining live child members.
2. No conflict, failed mutation, unsupported effect or exhausted output/dirty budget.
3. Valid filesystem geometry, supported inode types and sizes, unique block ownership, consistent allocation bitmap and a clean real redo log. Open unlinked inodes are allowed while referenced.
4. The complete dirty set fits the attached disk's actual log and the log can be reserved exclusively.

All dirty blocks enter one redo-log transaction. Synchronous `end_op` writes payload blocks, the durable header, home blocks, and clears the header. The header is the filesystem commit point. Recovery installs a transaction with a durable header and ignores one without it.

Committed inode views are refreshed, owner handles are rebased and saved references dropped. Orphan reclamation can issue subsequent cleanup transactions, without changing whether the namespace operation committed. The gate is released before blocking external output flushes.

Application safety is separate: the structured executor checks paths, tree quotas, types, link restrictions and operation completion. Host admission checks authenticated grants and configured threat indicators. The generic kernel API cannot determine whether every allowed edit is beneficial or malicious.

## Output and lifecycle

Console writes become bounded records. External pipe writes retain their destination and queue bytes. Pending-output accounting prevents premature EOF. Active and saved pipe endpoints are counted by group. Internal pipes accept tagged bytes immediately; outsiders cannot consume those bytes. Abort purges remaining private bytes, and commit makes them ordinary. Mixed inside/outside readers are rejected.

Only the begin owner finalizes. Commit with a live child returns `-1` without dooming the group, permitting join and retry. Child finalization and nested begin are rejected. Owner abort/death cancels descendants, retaining their private identity until exit. The last member frees backing and private cache state.

`xstate(pid)` is a diagnostic extension returning a group's dirty-block count, or `-1` for no group. The guest tests use it for deterministic outside-observer synchronization; it exposes status metadata, never private file bytes. Conflict invalidation wakes blocked group members so their next wait-condition check can fail and unwind.

`mknod` and killing outside processes are rejected during speculation. Abort does not reverse process memory, consumed input or internal pipe reads. Kernel diagnostic prints remain visible. Filesystem commit and output delivery are not one crash-atomic event.

## Agent transport

`KernelCoordinator` shares the typed schema and admission policy. `KernelBridge` attaches the persistent disk directly and starts a fixed `shadowd kernel-commit` or `shadowd kernel-preview` program. It ingests a bounded complete batch before `xbegin`. Input ACKs acknowledge ingestion only. The guest replays privately, suppresses intermediate ACKs, validates, and calls `xcommit/xabort` before reporting. No privileged output bypass is added.

Trusted host application checks run against predicted effects before execution. Post-decision inspection verifies actual state but cannot undo a committed transaction. A mismatch or lost final reply is indeterminate and blocks automatic retry. The redo log is not an exactly-once request ledger. Host filesystems, SQL databases and remote APIs require separate native adapters.
