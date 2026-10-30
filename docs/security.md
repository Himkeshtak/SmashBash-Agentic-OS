# Safety and persistence contract

## Trust boundaries

Untrusted input is the agent's JSON: intent, operation names, paths, and file content. The trusted computing base is the host service and its account, authentication integration, policy source, registered validators, journal directory, kernel, guest service, QEMU, and publication adapter. Keep agents from modifying or directly invoking these resources under the service's credentials.

The CLI is a trusted operator tool, not an authentication mechanism. An agent allowed to select `--principal`, `--policy`, or `--image` already controls the authority. A remote integration must bind those values after authenticating the agent and must keep raw bridge access private.

## Enforced controls

- Strict versioned schemas and bounded data; no executable command field.
- Explicit operation grants and relative path prefixes; protected subtrees and overlapping directory operations are denied.
- A fresh disk copy per trial, with no live device, host filesystem share, network interface, or arbitrary program execution.
- Guest-side path and file-type checks, independent final tree checks, and a kernel allocation checker.
- Independent host inspection of filesystem structure and the complete logical effect set before publication.
- Trusted admission hooks for signature scanning or organization policy; final hooks for application invariants. Hook failure aborts.
- An exclusive cooperative writer lock, baseline conflict checks, private temporary files, and atomic replacement.
- A durable publication intent, recoverable outcomes, request replay protection, and content-free audit records.

Policy grants express authorization, not universal harmlessness. A permitted deletion can still delete valuable permitted data. Restrict scopes and mutations according to the caller's role. Intent regular expressions and hash denylists are indicators; neither is a complete malware detector or a semantic proof about an LLM's purpose. Add the detectors and domain invariants your deployment needs through trusted hooks.

Stored data can become dangerous if another application later interprets it as code or configuration. This service never executes it, but cannot prove every future consumer will handle it safely.

## Commit guarantees

The included target is an exclusively service-owned, offline xv6 image on a local filesystem with atomic file replacement. Every service writer must use the same state directory and locking protocol. A separately running VM holding the old image open will continue writing its old file, so it must not share the target. Do not treat an arbitrary active host drive as an xv6 image.

The adapter flushes the temporary image, verifies its digest, rechecks the live baseline, replaces the image on the same filesystem, and flushes its parent directory on POSIX. `fsync` ultimately relies on the filesystem, storage device, and QEMU honoring their durability contracts. Windows can run host tests and inspect images; the validated QEMU runtime and directory durability path are Linux/WSL.

Snapshot capture and atomic publication assume exclusive ownership. A checksum check cannot eliminate races from an uncooperative external writer between the check and rename. Conflict rejection preserves observed external changes; it never blindly writes the old snapshot back.

The VM's filesystem log may persist individual trial operations to its private disk. That disk is not the published device. Live publication happens only after all required checks and a clean guest shutdown. This gives batch atomicity for the image, even when a trial internally used several xv6 log transactions.

## Failure and recovery

Journal transitions are written through a flushed temporary file and atomic rename. `COMMITTING` is durable before target publication. If the process dies in that window, compare the actual revision with the recorded `after` and `baseline`:

| Actual revision | Outcome |
| --- | --- |
| Proposed revision | COMMITTED |
| Original baseline | ABORTED |
| Neither | INDETERMINATE; block subsequent publication |

Interruption before the publication window aborts. Recovery reflushes a matching proposed revision before confirming publication; inability to confirm durability becomes INDETERMINATE. A failed receipt write after publication cannot turn a committed operation into a reported abort. Journal corruption fails closed. Back up the journal and image together. If a result is indeterminate, investigate and restore a known image/journal pair under exclusive service ownership; do not manually mark a transaction committed without establishing the actual state.

The journal contains principals, path names, hashes, state timestamps, and reasons. It omits raw intent and file payloads, but metadata can still be sensitive. Keep it private and apply retention/storage limits in a deployed service. Temporary private shadow files may remain after a hard process crash. They are never reused or published by recovery. A production supervisor should terminate orphaned QEMU workers and reap their private scratch storage; the coordinator's time budget applies while it is alive.

## Explicit exclusions

The implementation does not provide a rollback for the host OS, SQL database, network service, payment, email, arbitrary program, or device. It does not implement process-tree speculation, live disk snapshots, multi-target atomic commits, secure erasure of freed disk blocks, remote authentication, or protection against a compromised host/QEMU/kernel. The kernel allocation check is supplemented by host directory-graph validation; it is not a full general-purpose filesystem repair tool.

A new persistence adapter must use native transactions or an equivalent atomic publication mechanism. Never propagate raw xv6 disk blocks into a host filesystem or copy a live database file to simulate commit. A new operation must define its allowed effects and corresponding validation at each boundary before being enabled.
