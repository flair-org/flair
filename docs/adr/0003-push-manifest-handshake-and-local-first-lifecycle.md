# ADR 0003: Push Manifest Handshake and Local-First Workspace Lifecycle

## Status
Accepted

## Context
Previously, the Flair CLI operated as an immediate RPC client: `flair init` sent an HTTP POST request to create the repository on the backend, and `flair branch` immediately contacted the backend to register branches. This introduced tight network coupling, prevented offline work, and created server connection spikes. Furthermore, pushing multiple commits required naive serial execution of 5+ HTTP round-trips per commit, risking orphaned remote state if an upload dropped mid-way.

We decided to decouple all local creation operations into a 100% offline **Local-First Workspace** and consolidate all remote synchronization into an atomic, negotiated **Push Manifest Handshake** executed during `flair push`.

---

## Decisions

### 1. Offline Local-First Identity Generation
- `flair init` runs completely offline, generating a local UUID v4 `repoHash` in `.flair/repo.json` with `remote: null`.
- `flair branch <name>` runs completely offline, generating a local UUID v4 `branchHash` recorded in `.flair/branches.json` and `.flair/HEAD`.
- Local commits created via `flair add` continue to generate temporary UUID v4 identifiers.
- No network requests or backend authentication checks are required for `init`, `branch`, `add`, `params create`, `zkp create`, or `commit`.

### 2. Unlinked Repository Verification at Push Time
- In alignment with Git's model, `flair push` will NOT automatically create a missing remote repository on the backend.
- `flair push` validates that a remote is bound and reachable via `GET /repo/hash/{repoHash}`.
- If the repository is unlinked (`remote: null`) or returns HTTP 404, `flair push` halts immediately with an explicit prompt guiding the user to create the repository on FlairHub and link it using `flair remote add origin <remote_repo_hash>`.

### 3. Four-Phase Push Manifest Handshake Protocol
All remote synchronization is negotiated through a 4-phase protocol:

```
    CLI                                                      Backend
     |                                                          |
     | ----- 1. POST /repo/hash/{repoHash}/push/manifest -----> |
     |                                                          | (Inspects DB, diffs commit DAG,
     |                                                          |  registers branch if missing,
     |                                                          |  allocates upload sessions)
     | <---- 2. 200 OK (Missing Commits & Upload Tokens) ------ |
     |                                                          |
     | [Loop through missing commits serially]                 |
     | ----- 3a. POST .../commit/create/zkml-upload ----------> |
     | ----- 3b. POST .../commit/create/params-upload --------> |
     |                                                          |
     | ----- 4. POST /repo/hash/{repoHash}/push/finalize -----> |
     |                                                          | (Computes authoritative SHA-256
     |                                                          |  hashes across new commit DAG)
     | <---- 200 OK (Commit Hash Reconciliation Map) ---------- |
     |                                                          |
  [Apply journaled rename & update local HEAD]
```

#### Phase 1: Push Manifest Submission
The CLI builds a `PushManifest` representing the current local state and POSTs it to:
`POST /api/repo/hash/{repoHash}/push/manifest`

**Request Schema:**
```json
{
  "clientVersion": "1.0.0",
  "repoHash": "uuid-v4-or-remote-hash",
  "targetBranch": {
    "name": "feature-weights",
    "branchHash": "uuid-v4-branch-hash",
    "parentBranchHash": "uuid-v4-parent-or-null",
    "description": "Optional branch description"
  },
  "remoteHeadCommitHash": "sha256-of-remote-head-or-null",
  "commits": [
    {
      "localCommitHash": "uuid-v4-commit-1",
      "previousCommitHash": "_GENESIS_COMMIT_",
      "commitType": "CHECKPOINT",
      "message": "Initial local weights",
      "timestamp": "2026-11-28T12:00:00Z",
      "author": "wallet-address-or-principal",
      "paramHash": "sha256-digest-of-weights",
      "params": {
        "file": "params.pt",
        "size": 10485760
      },
      "zkp": {
        "isCheckpoint": true,
        "proof_cid": "cid-v1",
        "settings_cid": "cid-v1",
        "verification_key_cid": "cid-v1"
      },
      "metrics": {}
    }
  ]
}
```

#### Phase 2: Missing Artifact Negotiation Response
The backend validates parentage, registers `targetBranch` if it is a new branch, and identifies which commits and artifact files must be uploaded:

**Response Schema:**
```json
{
  "data": {
    "pushSessionId": "uuid-v4-push-session",
    "status": "NEGOTIATED",
    "branchHash": "uuid-v4-branch-hash",
    "upToDate": false,
    "missingCommits": [
      {
        "localCommitHash": "uuid-v4-commit-1",
        "needsParams": true,
        "needsZkp": false,
        "sessionId": "uuid-v4-commit-session",
        "initiateToken": "jwt-token",
        "zkmlReceiptToken": "jwt-token-or-null"
      }
    ]
  }
}
```

#### Phase 3: Serial Artifact Upload
For each commit listed in `missingCommits`, the CLI uploads the missing parameter tensors and ZKP files serially using pre-authorized session tokens. This ensures backward compatibility with existing `/zkml-upload` and `/params-upload` endpoints without modifying weight upload storage handlers.

#### Phase 4: Finalization and Commit Hash Reconciliation
Once all missing payloads have been uploaded, the CLI calls:
`POST /api/repo/hash/{repoHash}/push/finalize`

**Request Schema:**
```json
{
  "pushSessionId": "uuid-v4-push-session",
  "repoHash": "remote-repo-hash",
  "branchHash": "branch-hash",
  "completedCommits": ["uuid-v4-commit-1"]
}
```

**Response Schema:**
```json
{
  "data": {
    "success": true,
    "branchHead": "4a7d...authoritative-sha256",
    "commitHashMap": {
      "uuid-v4-commit-1": "4a7d...authoritative-sha256"
    },
    "verificationStatus": "PENDING_VERIFICATION"
  }
}
```

### 4. Group Commit Hash Reconciliation in CLI
Upon receiving `commitHashMap`:
1. The CLI writes `.flair/pending_reconciliation.json` containing the mapping journal.
2. For each local commit in `.flair/.local_commits/<local_uuid>`:
   - Updates `commit.json`: sets `commitHash` to the authoritative SHA-256 and updates `previousCommitHash` references.
   - Renames folder from `<local_uuid>` to `<authoritative_sha256>`.
3. Updates `.flair/HEAD` with the authoritative `previousCommit`.
4. Deletes `.flair/pending_reconciliation.json`.
If a crash occurs before completion, subsequent CLI commands detect the journal and complete reconciliation before executing.

### 5. Passive Status Synchronization
The push response assigns commits an initial verification status (`PENDING_VERIFICATION`). The CLI does not block or poll synchronously. Instead, `flair status`, `flair push`, and `flair pull` passively query the backend for updated statuses on subsequent runs.

---

## Consequences
- **Positive**: `flair init` and `flair branch` work completely offline without network latency or credential requirements.
- **Positive**: Network overhead during push is dramatically reduced by negotiating missing objects in a single manifest request.
- **Positive**: Interrupted pushes can be safely resumed without duplicating records or corrupting the DAG.
- **Backend Reference**: Backend engineers have a concrete contract to implement the `/push/manifest` and `/push/finalize` endpoints.
- **Trade-off**: Commit hashes on the client are mutable between local creation (UUID v4) and remote push (authoritative SHA-256). The journaled reconciliation step ensures local integrity across this transition.
