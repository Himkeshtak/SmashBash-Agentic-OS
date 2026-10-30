# Agent JSON and xv6 SB1 protocol

## Agent request v1

```json
{
  "version": 1,
  "request_id": "notes-001",
  "intent": "Create a note and read it back",
  "mode": "commit",
  "operations": [
    {"op": "fs.write", "path": "note.txt", "content_b64": "aGVsbG8="},
    {"op": "fs.read", "path": "note.txt"}
  ]
}
```

Every field above is required. Unknown fields, duplicate JSON keys, unsupported versions, nonfinite numbers, and unknown operations are rejected. `mode` is `commit` or `preview`. An agent cannot supply a target, kernel, policy, principal, executable, or bypass flag. The trusted caller supplies `principal` separately.

The independent schema is in `schema/request.schema.json`. Runtime parsing additionally enforces decoded byte sizes, request byte size, rename source/destination inequality, and policy limits. JSON Schema's `contentEncoding` is an annotation; runtime base64 validation remains mandatory.

| Operation | Required fields besides `op` | Semantics |
| --- | --- | --- |
| `fs.write` | `path`, `content_b64` | Create or replace a regular file; parent must exist |
| `fs.append` | `path`, `content_b64` | Append to a regular file, creating it if absent |
| `fs.mkdir` | `path` | Create a new directory; parent must exist |
| `fs.remove` | `path` | Remove a file or empty directory |
| `fs.rename` | `path`, `destination` | Move a regular file to an absent path; both parents must exist |
| `fs.read` | `path` | Read a regular file |
| `fs.list` | `path` | List a directory; `.` identifies the managed root |
| `fs.stat` | `path` | Inspect a file or directory; `.` identifies the managed root |

Paths are canonical ASCII relative names, composed of letters, numbers, `.`, `_`, and `-`, separated by `/`. No empty, `.` or `..` components, leading slash, backslash, or traversal is permitted. Each component is at most xv6's 14-byte `DIRSIZ`; total relative length is at most 96 bytes and depth at most eight. Binary content is base64 encoded. It is stored as data and never passed to an interpreter.

Protocol ceilings are 32 operations, 128 managed entries including the root, 64 KiB per file/payload, and 256 KiB total transferred bytes and managed file contents. Policies choose equal or lower limits. The configured guest root is one absolute directory name, for example `/data`, rather than a machine-specific host path.

An entire batch is admitted before execution. The host separately predicts each intermediate state, so invalid parents, conflicting types, quota overflows, nonempty directory removal, and missing read targets fail before entering QEMU. Reads observe preceding writes in the same batch. Returned observations are generated from the independent expected state after the actual guest effects and guest completion have been verified.

## Outcomes

- `COMMITTED`: the validated shadow was published; current-run read observations may be included.
- `ABORTED`: no publication happened, or `preview` intentionally discarded a validated trial. A preview includes `validated: true` and changed path names, without file contents.
- `INDETERMINATE`: an interrupted publication cannot be resolved from the recorded revisions. Further commits stop until an operator restores a known state.
- `REJECTED`: parsing, configuration, locking, or an unrecoverable journal error prevented the transaction API from accepting the request.

An identical request ID, principal, and digest returns the recorded receipt with `replayed: true`. A collision with different content or identity is rejected. Replay still checks current permissions. Journal replay does not return file contents. A preview is a terminal transaction: submit a new request ID to commit the plan afterward.

The CLI returns 0 for a commit, a validated preview, or a successful administration command; it returns 2 for denial, failed execution, or an indeterminate outcome. The Python API returns receipts for accepted IDs; structural and infrastructure rejections raise `TransactionError` or an I/O exception.

## SB1 guest transport

This is an internal versioned representation of the same typed operations, not a generic shell tunnel. The bridge launches the fixed trusted `shadowd` executable once. After that, only these ASCII frames are accepted. Paths and payloads are hex encoded to keep binary data, control characters, and shell metacharacters out of the transport. Response lines begin with `@SB1`; echoed request frames never contain that marker.

| Frame | Meaning |
| --- | --- |
| `B op_count entry_limit total_byte_limit file_byte_limit root_hex` | Begin one trial with trusted resource ceilings and root |
| `O opcode path_hex destination_hex_or_- payload_length` | Begin an operation |
| `D hex_bytes` | Transfer part of a write/append payload |
| `E` | Execute the complete operation and validate the managed tree |
| `F` | Require all operations complete; run final tree and kernel checks |

Opcodes are `R` read, `L` list, `S` stat, `W` write, `A` append, `M` mkdir, `U` remove, and `N` rename. No raw executable, shell, command, device, process-control, or network opcode exists.

The guest replies `@SB1 READY 1`, `@SB1 OK`, `@SB1 PREPARED operations entries bytes`, or `@SB1 ABORT reason`. Abort is terminal; after PREPARED no further operation is accepted. The bridge stops QEMU, then the host inspects the closed disk. QEMU receives only the private raw image, a trusted kernel, and its console; it has no network interface or host directory share.

Frame length, decoded payload length, operation count, nesting/state, response length, total output, and wall time are bounded. A timeout, unexpected response, guest exit, malformed frame, or non-clean QEMU shutdown prevents publication.
