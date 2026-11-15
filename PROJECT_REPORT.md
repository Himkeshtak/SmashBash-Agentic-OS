# Group 22: Smash BASH
## Topic 5: Externalization Barriers for Speculative Processes
**Course:** CSL3030 — Operating Systems Lab, IIT Jodhpur  
**Target:** `xv6-riscv` Kernel

---

### Team Members
| Name | Roll Number | Subsystem Responsibility |
|---|---|---|
| **Bhargab Das** | B24CS1022 | Buffer Cache Shadow Layer & Memory Reclamation (`kernel/bio.c`) |
| **Akshaya Draksharapu** | B24CS1029 | Process Lifecycle & Session Control (`kernel/proc.c`, `sys_xbegin`) |
| **Himkesh Tak** | B24ES1022 | Output Muzzling & Deadlock-Free IPC (`kernel/console.c`, `kernel/pipe.c`) |
| **Shiv Shome** | B24ES1031 | Atomic Disk Commit (`kernel/log.c`, `sys_xcommit`) & Concurrency Control |

---

## 1. Objective & Architectural Decisions

Modern autonomous software agents and experimental workflows frequently issue shell commands and system calls that mutate system files before validating their decisions. Host-level virtualization and whole-VM snapshots incur multi-second startup latencies, duplicate disk images, and fail to support fine-grained concurrency where speculative and non-speculative processes execute concurrently inside the same operating system.

We engineered an **in-kernel hardware-transactional-memory (HTM) substrate** directly in `xv6-riscv`. A process and its subprocess tree enter speculative mode via `xbegin()`, during which all filesystem mutations and external outputs are held back. On `xcommit()`, all changes are applied atomically to disk in **one single log transaction**; on `xabort()`, they vanish instantaneously from RAM with zero trace on disk or console.

### Why Buffer Cache Shadowing over Inode Virtualization (`kernel/bio.c`)
In UNIX/xv6, *every filesystem structure is a disk block*:
- Inodes reside in disk blocks (`IBLOCK`).
- Directory entries are stored in directory data blocks.
- Indirect pointers are blocks.
- Free-space allocation bitmaps are blocks (`BBLOCK`).

Rather than building fragile inode clones and duplicating directory lookup logic, we shadowed disk blocks directly at the buffer cache layer (`kernel/bio.c`). Consequently, file creation, file writing, truncating, appending, and file deletion (`unlink`) all automatically inherit transactional isolation with zero modifications to on-disk filesystem structures.

---

## 2. End-to-End Speculative Execution Pipeline

```
+-----------------------------------------------------------------------------------------------------------------+
|  USERSPACE:  [Normal Process: p->spec=0]       |       [Speculative Parent: p->spec=1] ---> fork() ---> [Child Worker: p->spec=1]    |
+------------------------------------------------+----------------------------------------------------------------+
             | (Direct committed disk access)             | (Writes redirected, console & pipes muzzled)                  
             v                                            v                                                               
+-----------------------------------------------------------------------------------------------------------------+
|  IN-KERNEL BARRIERS (xv6 Kernel)                                                                                |
|   [Buffer Cache: bio.c]                              [Output Barriers: console.c & pipe.c]                              |
|   * shadows[NSHADOW=64] table in RAM                 * spec_console_table: Holds printf/write(1) output                 |
|   * bread(): returns private shadow if p->spec > 0   * Intra-spec pipe (cat|grep): direct delivery (no deadlock)        |
|   * bread_write(): Copy-on-Write slot allocation     * Cross-boundary pipe (spec -> outside): held until commit         |
|   * Capacity check: >64 blocks -> Safe -1 failure    * Conflict check: outside bwrite() flags spec_conflicted[spec]=1   |
+-----------------------------------------------------------------------------------------------------------------+
                        |                                                  |                                      
       +----------------+----------------+                +----------------+----------------+                     
       | on xabort()                     | on xcommit()   | on xabort()                     | on xcommit()         
       v                                 v                v                                 v                     
  [Zero Trace]               [Single Atomic Log Tx]  [Discard]                 [Flush to UART]                    
  Free RAM slots             Copy home -> end_op()   Erase buffer              Emit to screen                     
                                         |                                                                        
                                         v                                                                        
                             [Committed VirtIO Disk]                                                              
```

---

## 3. Technical Implementation Mechanisms

1. **Process Tracking & Process-Tree Inheritance (`kernel/proc.h`, `kernel/proc.c`):**
   Added `int spec` to `struct proc`. In `fork()`, child processes inherit `np->spec = p->spec`, allowing subprocess trees (such as `sh` executing `rm`, `cat`, compilers) to share the parent's speculative session. Context switching between speculative and non-speculative processes requires **zero cycles** because shadow slots reside in a global kernel array indexed by `(spec, blockno)`.
2. **Shadow Buffer Table (`kernel/bio.c`, `kernel/fs.c`):**
   Implemented `shadows[NSHADOW]` (64 blocks, 64 KB capacity). `bread()` returns the private shadow copy for speculative processes while normal processes read committed disk. `bread_write()` allocates a shadow slot on modification. If writes exceed 64 blocks, it safely fails with `-1`, strictly preventing leakage to real disk.
3. **Output Muzzling (`kernel/console.c`, `kernel/pipe.c`):**
   `consolewrite()` routes speculative characters to `spec_console_table`. Held characters are released to UART only after `end_op()` finishes during `xcommit()`, or erased silently on `xabort()`. Pipelines within the same speculation (`reader->spec == writer->spec`) write directly to pipe buffers to prevent deadlock.
4. **Single-Transaction Log Capacity (`kernel/param.h`, `kernel/log.c`):**
   Standard xv6 limits transactions to `MAXOPBLOCKS = 10`. We expanded `MAXOPBLOCKS = 70`, `LOGBLOCKS = 210`, and `NBUF = 210`. On `xcommit()`, all 64 shadow blocks commit in **one indivisible disk log transaction**, guaranteeing crash atomicity.

---

## 4. Conflict Resolution: First-Committer-Wins Policy

To guarantee strict serializability, concurrent modifications are governed by **First-Committer-Wins (Abort on Conflict)**:
1. When an outside process (`spec == 0`) modifies a disk block via `bwrite()`, `shadow_check_conflict()` inspects `shadows[]`.
2. If that block is currently held by an active speculation, the kernel sets `spec_conflicted[spec] = 1`.
3. The outside write commits normally.
4. When the speculative process invokes `xcommit()`, the kernel detects the conflict flag, discards all shadow buffers, and **returns `-1`**. The committed outside mutation remains intact on disk.

---

## 5. Team Member Contributions & Subsystem Ownership

- **Bhargab Das (B24CS1022) — Buffer Cache Shadow Layer & Memory Reclamation:**
  Engineered the in-kernel shadow buffer cache in `kernel/bio.c` (`shadows[64]`, `bread` interception, Copy-on-Write slot allocation in `bread_write`, buffer isolation from bcache LRU). Co-developed `xabort()` memory cleanup.
- **Akshaya Draksharapu (B24CS1029) — Process Lifecycle & Session Control:**
  Implemented process speculative state tracking in `kernel/proc.h` and `kernel/proc.c`, fork-tree inheritance, and state cleanup in `allocproc` and `freeproc`. Implemented **`sys_xbegin()`** session entry and nested call rejection (`-1`).
- **Himkesh Tak (B24ES1022) — Output Muzzling & Deadlock-Free IPC:**
  Developed external output barriers in `kernel/console.c` (`spec_console_table` buffer, UART flush on commit, abort discard). Engineered the deadlock-free pipe boundary in `kernel/pipe.c` distinguishing intra-speculation pipelines from cross-boundary pipes.
- **Shiv Shome (B24ES1031) — Atomic Disk Commit & Concurrency Control:**
  Implemented single-transaction log commit in `kernel/log.c` and tuned log capacity (`MAXOPBLOCKS = 70`, `LOGBLOCKS = 210`) in `kernel/param.h`. Implemented **`sys_xcommit()`**, conflict detection, and automated test suites (`spectest`, `conflicttest`, `hiddentest`, `benchspec`).

---

## 6. Experimental Verification & Benchmark Evaluation

All tests executed inside `xv6-riscv` on QEMU:

| Test Program | Verification Scenario | Observed Result | Evaluation Pass Criteria |
|---|---|---|---|
| **`spectest` (T1)** | Abort isolation: `rm a; echo hi > b; xabort()` | **PASSED** | Zero trace on disk (file `a` intact, `b` absent) and screen. |
| **`spectest` (T2)** | Commit atomicity: `rm a; echo hi > b; xcommit()` | **PASSED** | File `a` deleted, `b` created with data; held console released. |
| **`spectest` (T3)** | Process tree inheritance across `fork()` | **PASSED** | Child shares parent speculation domain; parent sees updates. |
| **`conflicttest`** | Concurrent outside write to same disk block | **PASSED** | Speculative commit rejected (`-1`); outside write preserved. |
| **`hiddentest`** | Nested `xbegin()` and >64 KB memory exhaustion | **PASSED** | Second `xbegin` returns `-1`; exhaustion fails with `-1`, disk clean. |

### Write Latency Benchmark (`benchspec`)
- **1 KB to 56 KB:** Speculative writes execute in **0–4 ticks** vs. **1–10 ticks** outside (**2x to 3.3x speedup**) because writes are held in RAM without synchronous disk log writes.
- **60 KB to 68 KB:** The 64-block shadow table is exhausted. Rather than leaking uncommitted data to disk, writes trigger the **CLIFF** and safely return `-1`.

---

## 7. Key Engineering Challenges Overcome

1. **Inter-Process Pipeline Deadlocks:** Standard command pipelines (e.g., `cat | grep`) share a unidirectional pipe. If all pipe writes are held until commit, the reader blocks indefinitely waiting for data while the writer blocks waiting for commit, causing a permanent deadlock. We solved this in `kernel/pipe.c` by allowing direct delivery when `writer->spec == reader->spec`, holding back only cross-boundary writes.
2. **Log Transaction Sizing:** Stock xv6 transactions are limited to 10 blocks. Committing 64 shadow blocks would trigger a kernel panic. Expanding `MAXOPBLOCKS = 70` and `LOGBLOCKS = 210` allowed the entire shadow cache to commit in a single atomic log operation without overflowing the 1024-byte block boundary (log header is 844 bytes).
