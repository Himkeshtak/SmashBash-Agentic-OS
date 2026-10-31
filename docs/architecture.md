# Transaction architecture

The OS consumes a versioned operation protocol and knows nothing about an agent's vendor, framework, model, or prompting strategy. Trusted deployment code chooses the identity and capabilities. The agent proposes operations; neither its text nor its self-reported identity is an authority.

```mermaid
flowchart TD
    U["User task"] --> IV["Principal + intent precheck"]
    I["Authenticated principal<br/>supplied by trusted integration"] --> IV
    P["Trusted policy + optional scanners"] --> IV
    CFG["Trusted runtime configuration<br/>local .env or environment"] --> LA
    CFG --> TX
    IV -->|reject| AB["ABORT / reject<br/>No live effects"]
    IV --> LA["Agent.py / Agent class<br/>OpenRouter or local Ollama inference"]
    LA -->|inference or proposal failure| AB
    LA --> J["JSON request v1<br/>intent + typed operations"]
    A["Any other agent / client"] --> J
    I --> V1
    P --> V1
    J --> V1["Admission validator<br/>schema, permissions, scope,<br/>threat indicators, budgets"]
    V1 -->|reject| AB
    V1 -->|admit| TX["Coordinator<br/>lock target + baseline revision"]
    D[("Dedicated persistent xv6 disk")] -->|copy only| SH[("Private shadow image")]
    TX --> SH
    SH --> EXPECT["Independent expected-state evaluation<br/>check batch semantics and quotas"]
    EXPECT -->|invalid| AB
    EXPECT --> B["QEMU bridge<br/>SB1 frames; no agent shell commands"]
    B --> E["xv6 shadowd<br/>confined filesystem operations"]
    E -->|operation failure or timeout| AB
    E --> G["OS final validator<br/>tree quotas, types, links,<br/>operation completion"]
    G --> K["Kernel shadowcheck syscall<br/>block ownership, bitmap,<br/>inode bounds, clean log"]
    G -->|invalid| AB
    K -->|invalid| AB
    K -->|PREPARED| F["Stop VM + independent host inspection<br/>directory graph, exact effects,<br/>unchanged system namespace"]
    F -->|invalid or unclean shutdown| AB
    F --> C["Commit gate<br/>recheck policy + target revision<br/>optional application invariants"]
    C -->|invalid / conflict / preview| AB
    C -->|accepted| INT["Durable COMMITTING intent"]
    INT --> PUB["ImageTarget<br/>fsync temporary image<br/>atomic replacement + directory fsync"]
    PUB --> D
    PUB --> OK["COMMITTED receipt<br/>release read observations"]
    TX --> AUD[("Private transaction journal<br/>accepted request IDs and outcomes")]
    AB --> AUD
    INT --> AUD
    OK --> AUD
    AUD -. interrupted transaction .-> REC["Recovery<br/>resolve pending journal state"]
    REC -->|interrupted before publication| RAB["Recovered ABORTED<br/>discard trial"]
    REC -->|COMMITTING| REV{"Current target revision?"}
    REV -->|proposed revision and durability confirmed| ROK["Recovered COMMITTED receipt"]
    REV -->|original baseline| RAB
    REV -->|unrelated revision or durability unconfirmed| UNC["INDETERMINATE<br/>block further publication"]
    ROK --> AUD
    RAB --> AUD
    UNC --> AUD
```

## Validation criteria

| Boundary | Criteria | Rejection behavior |
| --- | --- | --- |
| Admission | Version and exact fields; known operations; authenticated grant; path scope; protected subtrees; canonical paths; operation and payload limits; configured intent/signature scanners | No bridge is created |
| xv6 operation service | Strict frame sequencing; one active transaction; bounded payload; no shell execution; root confinement; successful syscalls; no devices or linked regular files; per-operation quota validation | Abort the entire private VM trial |
| OS final tree validator | All declared operations completed; no unfinished payload; managed namespace contains only regular files/directories; no repeated inode; bounded depth, entry count, file sizes, and total bytes; valid directory size/link counts | Never emits PREPARED |
| Kernel final validator | Valid superblock geometry; inode types/sizes/link viability; no data block out of range or shared between owners; exact allocation bitmap; empty log after sync | Never emits PREPARED |
| Host independent final validator | Full directory graph including dot links and reachability; file and directory reference counts; logical state equals the separately evaluated request; outside namespace unchanged; guest metrics match; optional trusted application invariants | Discard the shadow |
| Publication | Policy still current; permissions still valid; live baseline still identical; shadow digest verified while preparing replacement; durable commit intent exists | Conflict/abort, or explicit recovery if publication may already have happened |

## State machine

```mermaid
stateDiagram-v2
    [*] --> RECEIVED
    RECEIVED --> ADMITTED: permissions and policy pass
    ADMITTED --> EXECUTING: stable private snapshot
    EXECUTING --> PREPARED: both final validation boundaries pass
    PREPARED --> COMMITTING: commit mode and durable intent
    COMMITTING --> COMMITTED: atomic publication
    COMMITTING --> COMMITTED: recovery confirms proposed revision and durability
    RECEIVED --> ABORTED: denied
    ADMITTED --> ABORTED: snapshot / semantic failure
    EXECUTING --> ABORTED: guest / validation failure
    PREPARED --> ABORTED: preview / conflict
    COMMITTING --> ABORTED: recovery finds original revision
    COMMITTING --> INDETERMINATE: unrelated revision or durability unconfirmed
    COMMITTED --> [*]
    ABORTED --> [*]
    INDETERMINATE --> [*]
```

The filesystem log commits operations to the private disk during a trial. That is not publication to the live target. The external transaction commit is the host's atomic image replacement, after the VM has stopped. Abort discards the private disk rather than undoing mutations to a live filesystem.

## Agent and persistence independence

The Python `Coordinator.run` method is the common entrypoint. The included LLM-backed `Agent` invokes OpenRouter or local Ollama to propose a batch and submits it to that same method. Other CLI clients, LLM tools, queues, or a future authenticated server can also call it. There is deliberately no unauthenticated network listener or provider dependency in the OS execution path. See [the real agent setup](agent.md).

`StorageTarget` separates trial execution from native persistence. Its snapshot must yield a bootable private xv6 view. Its prospective revision must identify the proposed native result. Its publication method must enforce the baseline and atomically persist that result. Only `ImageTarget` ships here. Directory and database adapters must provide equivalent isolation, mapping, atomicity, and crash recovery before they can be enabled.

The earlier process-level speculation document is historical context. Whole-VM image isolation was chosen here to give xv6 trials a concrete externalization boundary without pretending that disk-block shadowing alone also isolates inode caches, unrelated processes, pipes, devices, or arbitrary host effects.
