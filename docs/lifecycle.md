# SmashBash System Architecture & Runtime Lifecycle

This document details the end-to-end architecture, trust boundaries, communication protocol, and verification mechanics of the SmashBash project.

---

## 1. System Overview & Trust Boundaries

The system is split into three distinct operational layers. Crucially, the **Host Orchestration Layer is the only trusted boundary**. Both the Agent and the Guest VM are treated as untrusted.

```
[ UNTRUSTED LAYER ]
  Agent Layer (smashbash/agent.py)
        │
        │ Structured JSON proposals (v1 schema)
        ▼
[ TRUSTED LAYER (Host User Space) ]
  Host Orchestration Layer (smashbash/)
    • Admission & Intent Policy Engine (policy.py, validators.py)
    • In-Memory Filesystem Simulation (filesystem.py)
    • Shadow Image Provisioning & Lock Manager (storage.py)
    • QEMU Supervisor & Bridge Transport (bridge.py)
    • Independent Raw Disk Inspector (image.py)
        │
        │ Bounded SB1 ASCII frames over serial pipe (stdio)
        ▼
[ UNTRUSTED LAYER (Isolated Hardware Sandbox) ]
  Guest Sandbox Layer (QEMU / xv6)
    • User Space : shadowd daemon (xv6/user/shadowd.c)
    • Kernel Space: xv6 Kernel (xv6/kernel/shadow.c) -> sys_shadowcheck()
    • Ephemeral Storage: device.img.shadow.tmp
```

---

## 2. Phase-by-Phase End-to-End Execution Flow

### Phase 0: Origin — `agent.py` (Constructing the Proposal)
* The user issues a natural language request (e.g., *"Read docs and write to note.txt"*).
* `agent.py` does not execute bash commands. It prompts the LLM to format the intent into a strictly defined JSON request matching `request.schema.json`.
* The structured payload includes:
  * `version`: `1`
  * `request_id`: A unique token (`req-XXXX`)
  * `intent`: Natural language explanation
  * `mode`: `"preview"` or `"commit"`
  * `operations`: List of actions (`fs.read`, `fs.write`, `fs.append`, `fs.mkdir`, `fs.remove`, `fs.rename`) with base64-encoded payloads.
* **Identity Injection:** The agent **cannot** pick or forge its identity. The calling infrastructure injects the authenticated `principal` (e.g., `"local-user"`) directly when invoking the Host Coordinator (`coordinator.run(request, principal)`).

---

### Phase 1: Host Admission & In-Memory Pre-Simulation (Host User Space)
Before touching any VM or disk resources, the host validates the request entirely in user space:

1. **Schema Check (`request.schema.json`):**
   * Rejects unrecognized keys (`"additionalProperties": false`).
   * Enforces path length limits ($\le 96$ characters) and bans traversal patterns (`.` and `..`).
2. **Policy Check (`policy.py`):**
   * `admit_intent()`: Scans the `intent` string using regular expressions for hostile indicators (e.g., *"bypass"*, *"disable validator"*).
   * `admit()`: Checks that the authenticated `principal` has permissions for the requested operations and that paths stay within `grant.prefixes`.
   * Blocks operations overlapping `policy.protected_paths` (e.g., secret directories).
3. **Validator Check (`validators.py`):**
   * Computes the SHA-256 digest of write/append payloads and matches them against `ContentHashDenylist`.
4. **In-Memory Tree Simulation (`filesystem.py`):**
   * Represents the filesystem as an in-memory dictionary: `dict[str, Node]` (`before_tree`).
   * Calls `evaluate()` to simulate every operation step-by-step:
     * Verifies that parent directories exist before creating files.
     * Ensures directories are empty before deletion (`fs.remove`).
     * Validates system quotas (`max_entries`, `max_file_bytes`, `max_total_bytes`) after **every single operation**.
   * Produces `expected_tree` (the mathematical blueprint of the expected disk state).

---

### Phase 2: Sandbox Isolation & The QEMU Bridge (`storage.py`, `bridge.py`)

* **Storage Isolation (`storage.py`):**
  * Acquires an OS-level exclusive advisory lock (`fcntl.flock`) on `device.img`.
  * The lock is held across the entire lifecycle: **cloning $\to$ QEMU run $\to$ verification $\to$ atomic commit**.
  * Creates an ephemeral clone: **the shadow copy** (`scratch_copy`).
  * Stored in the **same directory/filesystem** as `device.img` to allow an atomic inode swap upon commit.
  * Fail-closed: wrapped in a `finally` block so `shadow_path.unlink()` instantly rolls back changes if any error occurs.
* **Booting QEMU (`bridge.py`):**
  * QEMU is the virtual machine; the shadow copy is its hard drive.
  * Launched via `subprocess.Popen` with defensive flags:
    * `-nic none`: Physical network isolation (no internet, no LAN).
    * `-drive cache=directsync`: Bypasses host cache; all writes commit directly to the shadow file.
    * `-no-reboot`: Kernel crashes halt the VM immediately rather than entering an infinite reboot loop.
    * `-nographic`: Directs serial line I/O to Python `stdin`/`stdout`.
  * A background listener thread monitors output with a timeout clock and a 2 MB output quota.

---

### Phase 3: Shell Hijacking & The SB1 Protocol Exchange

* **Eliminating the Shell (`shadowd`):**
  * The host consumes kernel boot text until it sees the prompt (`$ `).
  * The host immediately sends `shadowd\n`.
  * `shadowd` takes over `stdin`/`stdout`, pausing the shell in the background. The raw shell becomes unreachable.
  * `shadowd` prints `@SB1 READY 1`. Any other output triggers a VM shutdown.
  * From this point on, raw shell commands (like `ls -la`) are impossible; `shadowd` only parses SB1 protocol frames.
* **The SB1 Frame Loop:**
  1. **`B` (Begin):** Host sends operation count, entry limits, byte quotas, and the `/data` root in hex. `shadowd` stores them in `session` memory and replies `@SB1 OK`.
  2. **`O` (Operation):** Host declares opcode (`W`, `A`, `M`, `U`, `N`), hex path, destination, and payload size. `shadowd` confirms the path stays within `/data` and replies `@SB1 OK`.
  3. **`D` (Data):** Host streams payload bytes in 24-byte hex chunks (48 hex chars per line). `shadowd` decodes them into its RAM buffer and replies `@SB1 OK`.
  4. **`E` (Execute):** `shadowd` checks that `received_len == expected_len` and executes native xv6 system calls:
     * `open()`: Creates/truncates file and links directory entry.
     * `write()`: Allocates blocks on the shadow disk and writes data.
     * `close()`: Releases the file descriptor and flushes metadata.
     * If any system call fails, `shadowd` sends `@SB1 ABORT` and exits.

---

### Phase 4: In-Kernel Integrity Audit (`sys_shadowcheck`)

When operations finish, the host sends `F\n` (Finalize). `shadowd` calls the kernel system call `sys_shadowcheck()` in `xv6/kernel/shadow.c`:

1. **`sys_sync()`:** Pushes all dirty buffer cache headers (`bcache`) out to the virtual disk.
2. **Superblock Boundary Check:** Verifies `sb.magic == FSMAGIC` and checks that log, inode, and bitmap block zones do not overlap.
3. **Anti-Aliasing Check:** Scans direct and indirect block pointers across all active inodes into a temporary `seen` bitmap. If two inodes reference the same block, the audit fails.
4. **Bitmap Cross-Examination:** Compares the `seen` allocation map against the on-disk free bitmap. Any space leak or phantom allocation causes immediate failure.
5. **Clean Log Check:** Verifies that the filesystem journal header is idle (`log.lh.n == 0`).

* If all checks pass, `sys_shadowcheck()` returns `0`.
* `shadowd` emits: `@SB1 PREPARED <ops> <entries> <bytes>`.
* The host reads this, sends `Ctrl-A x` to cleanly terminate QEMU, and the VM process halts.

---

### Phase 5: Post-Execution Verification & Atomic Commit (`image.py`, `storage.py`)

The host does not blindly trust the VM's `@SB1 PREPARED` handshake. It runs an independent forensic audit from the outside:

```
                  ┌────────────────────────────────────────┐
                  │ 1. Host In-Memory Simulation           │
                  │    evaluate() produced: expected_tree  │
                  └──────────────────┬─────────────────────┘
                                     │
                                     ▼
                    [ Do they match 100% identically? ]
                    [   actual_tree == expected_tree  ]
                                     ▲
                                     │
                  ┌──────────────────┴─────────────────────┐
                  │ 2. Independent Host Disk Inspection    │
                  │    image.py reads closed shadow.tmp    │
                  │    directly and builds: actual_tree    │
                  └────────────────────────────────────────┘
                                     │
                 ┌───────────────────┴───────────────────┐
                 │                                       │
            MATCHES                                 MISMATCHES
                 │                                       │
                 ▼                                       ▼
    ┌─────────────────────────┐             ┌─────────────────────────┐
    │ 3. ATOMIC COMMIT        │             │ 4. FAIL-CLOSED ABORT    │
    │  • os.replace(shadow,   │             │  • Delete shadow.tmp    │
    │               real_img) │             │  • Real disk untouched  │
    │  • fsync parent dir     │             │  • Raise TransactionErr │
    └─────────────────────────┘             └─────────────────────────┘
```

1. **Forensic Inspection (`image.py`):**
   * The host uses its own read-only xv6 parser (`Image`) to read the closed shadow file directly from the host.
   * It reconstructs the actual directory tree and file hashes (`actual_tree`).
2. **Structural Assertion:**
   * The host verifies:
     ```python
     if actual_tree != expected_tree:
         raise TransactionError("post-execution filesystem tree diverged from expected model")
     ```
   * If any unauthorized file was created or contents diverged, it aborts.
3. **Atomic Commit (`storage.py`):**
   * Writes `"state": "COMMITTING"` to the journal.
   * Calls `os.fsync()` on the shadow file.
   * Calls **`os.replace(shadow_path, target_path)`**, executing an atomic POSIX `rename()` pointer swap.
   * Calls `os.fsync()` on the parent directory.
   * Writes `"state": "COMMITTED"` to the recovery journal.
4. **Receipt Delivery:**
   * Releases the file lock.
   * Packages execution metadata into a `TransactionReceipt` and returns it to `agent.py`, which responds to the user.