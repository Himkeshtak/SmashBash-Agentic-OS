# Externalization Barriers for Speculative Processes in xv6

**Course:** CSL3030 — Operating Systems Lab, IIT Jodhpur  
**Problem Statement:** Problem 6 (Hard) — Externalization barriers for speculative processes  
**Deliverables:** In-Kernel Speculative Transaction Engine, Test Suite, Benchmark Analysis, Project Report, and Submission Archive.

---

## 1. Overview

In modern autonomous computing and AI-assisted workflows, agents execute shell commands and system calls that mutate files and emit console output. An agent can choose wrong actions, corrupt configuration files, or emit destructive output before it can evaluate its plan.

This project implements an **in-kernel hardware-transactional-memory style substrate** in `xv6-riscv`:
1. **Speculative Process State:** A process and its entire child process tree execute in a private trial mode (`p->spec > 0`).
2. **Buffer Cache Shadowing (`kernel/bio.c`):** All file writes, creates, deletes (`unlink`), and appends are redirected to an in-kernel private shadow table (`shadows[NSHADOW]`, 64 blocks). Normal processes (`p->spec == 0`) continue reading committed disk state.
3. **Output Isolation Barriers:** Console output (`console.c`) and cross-boundary pipe writes (`pipe.c`) are buffered in memory and withheld from the outside world until commit.
4. **Crash-Resilient Atomic Commit (`sys_xcommit`):** On `xcommit()`, all shadow blocks are committed to disk in **one single journal log transaction** (`MAXOPBLOCKS = 70`, `LOGBLOCKS = 210`), followed by releasing buffered console output.
5. **Instantaneous Abort (`sys_xabort`):** On `xabort()`, all shadow slots and console buffers are reclaimed in memory with zero trace on disk or screen.
6. **First-Committer-Wins Conflict Detection:** If an outside process modifies a disk block currently held by an active speculation, the speculation is marked conflicted and rolls back on `xcommit()`, returning `-1`.

---

## 2. System Calls

Three system calls are exposed in xv6:

```c
int xbegin(void);   // Begins a speculative session. Inherited by child processes via fork().
                    // Returns 0 on success, or -1 if called while already speculative (nested).

int xcommit(void);  // Atomically writes all shadow blocks to disk in a single log transaction,
                    // releases held console output, and resets speculative state.
                    // Returns 0 on success, or -1 on conflict/exhaustion.

int xabort(void);   // Discards all shadow blocks and buffered console output with zero trace.
                    // Returns 0 on success.
```

---

## 3. Architecture & Key Invariants

```
+-------------------------------------------------------------------------------------------------+
|                                       USER SPACE PROCESSES                                      |
|   [Normal Process: p->spec=0]       [Speculative Parent: p->spec=7]    [Child Worker: p->spec=7]|
+-------------------------------------------------------------------------------------------------+
             | Direct FS calls                         | File writes, creates, deletes, console    
             v                                         v                                          
+-------------------------------------------------------------------------------------------------+
| IN-KERNEL EXTERNALIZATION BARRIER                                                              |
|                                                                                                 |
|   [Buffer Cache Shadow Layer: bio.c]                   [Output Muzzle: console.c & pipe.c]     |
|   +---------------------------------------+            +------------------------------------+   |
|   | shadows[NSHADOW=64] Table in RAM      |            | spec_console_table: Holds stdout   |   |
|   |   - bread(): private copy hit         |            | Pipe reader check:                 |   |
|   |   - bread_write(): shadow slot alloc  |            |   * reader->spec == writer->spec   |   |
|   |   - Exhaustion (>64KB) -> Return -1   |            |     -> direct pipeline delivery    |   |
|   +---------------------------------------+            |   * reader->spec == 0              |   |
|                       |                                |     -> hold until xcommit()        |   |
|                       |                                +------------------------------------+   |
+-----------------------|---------------------------------------------------|---------------------+
                        |                                                   |                      
       +----------------+----------------+                 +----------------+----------------+     
       | on xabort()                     | on xcommit()    | on xabort()                     | on xcommit()
       v                                 v                 v                                 v     
  [Zero Trace]               [Single Atomic Log Tx]   [Discard]                 [Flush to UART]   
  Reclaim RAM slots          log_write() -> end_op()  Erase buffer              Print to screen   
                                         |                                                        
                                         v                                                        
                             [Committed VirtIO Disk]                                              
```

- **Zero Context Switch Overhead:** Shadow blocks are keyed by `(spec_id, blockno)` in kernel RAM. Switching between processes requires no cache flushing or register reloading.
- **Deadlock-Free Intra-Speculation Pipelines:** Pipelines like `cat | grep` within the same speculation deliver data directly through the pipe buffer without deadlock.
- **Strict Non-Externalization Guarantee:** When shadow capacity is exceeded (>64 blocks / 64 KB), the kernel immediately fails the write with `-1`, never falling back to real disk.

---

## 4. Verification Suite

Four automated user test programs are implemented in `xv6/user/`:

| Program | Command in xv6 | What It Tests | Expected Result |
|---|---|---|---|
| **`spectest`** | `$ spectest` | Tests abort isolation, commit atomicity, and process tree inheritance across `fork()`. | `ALL 3 TESTS PASSED` |
| **`conflicttest`** | `$ conflicttest` | Tests First-Committer-Wins conflict resolution with concurrent outside writes. | `CONFLICT TEST PASSED` |
| **`hiddentest`** | `$ hiddentest` | Tests nested `xbegin()` rejection (`-1`) and shadow memory exhaustion non-externalization cliff. | `ALL HIDDEN TESTS PASSED` |
| **`benchspec`** | `$ benchspec` | Benchmarks write latency inside vs. outside speculation from 1 KB to 68 KB, illustrating the 64 KB cliff. | Table & cliff confirmation |

---

## 5. Building and Running

### Build the xv6 Kernel and Filesystem
```bash
# Using the installed riscv64 toolchain
export PATH=/home/bhargab-das/toolchain/bin:$PATH
make -C xv6 clean
make -C xv6 fs.img
```

### Launch in QEMU
```bash
export PATH=/home/bhargab-das/toolchain/bin:$PATH
make -C xv6 qemu
```
*(Press `Ctrl-A X` to exit QEMU).*

### Run Tests in xv6 Shell
Inside the running xv6 console:
```text
$ spectest
=== TEST 1: ABORT ISOLATION ===
TEST 1 PASSED: abort left zero trace on disk and screen.
=== TEST 2: COMMIT ATOMICITY ===
TEST 2 PASSED: commit applied both operations atomically.
=== TEST 3: PROCESS TREE INHERITANCE ===
TEST 3 PASSED: fork tree shared speculative domain.
ALL 3 TESTS PASSED!

$ conflicttest
=== TEST: FIRST-COMMITTER-WINS CONFLICT RESOLUTION ===
Speculative child started (spec=1)
Child modified block
Outside parent modifying same block while child is speculative...
Outside parent committed write successfully!
Child calling xcommit()...
Child xcommit() returned -1 as expected on conflict!
Outside parent verifying data was not corrupted by aborted speculation...
Parent read back: "PARENT_WINNER"
CONFLICT TEST PASSED!

$ hiddentest
=== HIDDEN TEST 1: NESTED xbegin() REJECTION ===
First xbegin() succeeded.
Nested xbegin() returned -1 as required!
HIDDEN TEST 1 PASSED!
=== HIDDEN TEST 2: SHADOW EXHAUSTION PROTECTION (>64 KB) ===
Starting write of 68 blocks (68 KB)...
Write safely returned -1 on shadow exhaustion!
HIDDEN TEST 2 PASSED!
ALL HIDDEN TESTS PASSED!

$ benchspec
===============================================================
  BENCHMARK: Write Cost Inside vs Outside Speculation
  Evaluating file sizes 1 KB to 68 KB (NSHADOW = 64)
===============================================================
Size (KB) | Outside Speculation (Ticks) | Inside Speculation (Ticks) | Status
----------+-----------------------------+----------------------------+-----------------------
   1 KB   |           0 ticks           |          0 ticks           | SUCCESS
   2 KB   |           1 ticks           |          0 ticks           | SUCCESS
   4 KB   |           1 ticks           |          1 ticks           | SUCCESS
   8 KB   |           2 ticks           |          1 ticks           | SUCCESS
  16 KB   |           3 ticks           |          2 ticks           | SUCCESS
  24 KB   |           5 ticks           |          3 ticks           | SUCCESS
  32 KB   |          14 ticks           |          3 ticks           | SUCCESS (4.6x faster)
  40 KB   |          18 ticks           |          4 ticks           | SUCCESS (4.5x faster)
  48 KB   |          21 ticks           |          5 ticks           | SUCCESS (4.2x faster)
  56 KB   |          25 ticks           |          5 ticks           | SUCCESS (5.0x faster)
  60 KB   |          28 ticks           |          CLIFF             | EXHAUSTION (Returned -1)
  64 KB   |          31 ticks           |          CLIFF             | EXHAUSTION (Returned -1)
  68 KB   |          34 ticks           |          CLIFF             | EXHAUSTION (Returned -1)
===============================================================
```

---

## 6. Deliverables & Submission Artifacts

- **Project Code Archive:** `submission_problem6_xv6.zip`
- **Project Report (PDF):** `PROJECT_REPORT.pdf` (3-page detailed report with architecture diagram, challenges, innovations, and evaluation results)
- **Project Report (Markdown):** `PROJECT_REPORT.md`
