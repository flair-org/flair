# Local-First Architecture Migration: From Immediate-RPC to Offline-First Workspace & Push Manifest Handshake

## Executive Summary

This document captures the complete architectural migration of the **Flair CLI** from its initial **Immediate-RPC** paradigm to an **Offline-First / Local-First** workspace paradigm. 

Under the previous model, developer commands like `flair init` and `flair branch` synchronously sent HTTP requests to the FlairHub backend, requiring constant internet connectivity, active credentials, and generating bursty server load. 

The new architecture decouples local development from remote synchronization:
1. **100% Offline Local Lifecycle**: Repository initialization (`init`), branch management (`branch`), and commit pipelines (`add`, `params`, `zkp`, `commit`) run completely locally without network or backend dependencies.
2. **Push Manifest Handshake**: All remote synchronization is deferred to `flair push`, which executes a Git-like manifest negotiation (`POST /push/manifest`), serially transfers only missing artifacts, and finalizes with atomic commit hash reconciliation (`POST /push/finalize`).
3. **Explicit Repository Linking**: Missing remote repositories are **not** auto-created; users are prompted to explicitly link an existing FlairHub repository via `flair remote add origin <repo_hash>`.
4. **Authoritative Hash Reconciliation**: Commits initially generated with client UUID v4 are reconciled with deterministic, backend-authoritative SHA-256 hashes using a crash-resilient, two-phase journaled rename engine.

---

## 1. Previous State vs. Migrated State Comparison

### High-Level Comparison Table

| Attribute | Previous State (Immediate-RPC) | Migrated State (Local-First Handshake) |
| :--- | :--- | :--- |
| **`flair init`** | Synchronously calls `POST /repo/create` on backend; prompts for immediate base model upload; fails if offline or unauthenticated. | Completely offline; generates local UUID v4 `repoHash` and initial branch UUID v4 `branchHash` (`main`); records `remote: null`; defers base model upload to push. |
| **`flair branch`** | Synchronously calls `POST .../branch/create` or `DELETE .../branch/hash/.../delete` against FlairHub. Fails offline. | Operates 100% offline on `.flair/branches.json`. Branches are pushed and registered on FlairHub during `flair push`. |
| **`flair push`** | Naively looped over local commits; executed 5 independent HTTP requests per commit (initiate, zkml-check, zkml-upload, params-upload, finalize); risked partial/orphaned state. | Verifies local Merkle integrity first; halts with prompt if remote repo is 404; submits a single `PushManifest`; receives missing artifact checklist; uploads serially; finalizes atomically. |
| **Identity Generation** | Remote backend assigned repository and branch hashes; client commits were assigned UUIDs that were overwritten during single-commit finalize without rollback protection. | Client generates offline UUID v4 for `repoHash`, `branchHash`, and local commits. Server generates authoritative SHA-256 commit hashes returned as a batch mapping (`commitHashMap`). |
| **Crash Resilience** | A failure during commit directory rename on Windows left the repository in a corrupted or desynchronized state. | Two-phase ACID journal (`.flair/pending_reconciliation.json`) with automated recovery on any subsequent CLI invocation. |
| **Server Load** | Bursty, chattiness on every branch creation and multiple roundtrips per commit. | Minimal. One negotiation handshake per push, followed by streaming only missing parameter/proof artifacts. |

---

## 2. The Previous State: Problems & Architectural Debt

Prior to this migration, the Flair CLI functioned as a thin RPC wrapper over the backend API:

```
[Previous State Flow]
flair init        ----(HTTP POST /repo/create)-------------------> Backend creates DB record
flair branch dev  ----(HTTP POST /repo/hash/.../branch/create)----> Backend creates DB record
flair add         ------------------------------------------------ Local UUID generated
flair commit      ------------------------------------------------ Local commit finalized
flair push:
  For each commit:
    1. POST .../commit/create/initiate
    2. POST .../commit/create/zkml-check
    3. POST .../commit/create/zkml-upload
    4. POST .../commit/create/params-upload
    5. POST .../commit/create/finalize
```

### Critical Issues in the Previous State
1. **Network Requirement for Local Work**: Developers could not start a project on a plane, train, or in an offline environment because `flair init` and `flair branch` crashed immediately without backend reachability.
2. **Server-Side Spam & Zombie Entities**: If a user ran `flair init` multiple times while testing or experimenting, multiple empty, orphaned repositories were created in the backend MongoDB database.
3. **No Batch Negotiation**: Pushing 10 commits generated 50+ HTTP requests. If request 37 failed, commits 1–7 were pushed while commits 8–10 remained local, causing state fragmentation and broken merge trees.
4. **Windows File Locking Concurrency Bugs**: During `flair push`, files inside `.flair/.local_commits/<uuid>/` were passed as open file objects to `httpx.post`. When the CLI subsequently attempted to rename the folder to the server's authoritative hash, Windows threw `[WinError 5] Access is denied` because the file handles were still open in the socket buffer.
5. **Auto-Creation Ambiguity**: Previous designs considered automatically creating remote repositories during `push`, which led to permission ambiguity, name collisions, and lack of alignment with standard VCS (Git/GitHub) workflows.

---

## 3. Changes Incorporated in the Migration

### 3.1 100% Offline `flair init` (`flair_cli/cli/init.py`)
- **Removed Synchronous API Call**: Eliminated the `api_client.create_repo` network call.
- **Client-Side UUID v4 Generation**: Generates a standard UUID v4 `repoHash` and a default `branchHash` for the `main` branch locally.
- **Local Metadata Initialization**:
  - Writes `.flair/repo.json`:
    ```json
    {
      "repoHash": "3c98d645-3dbd-4235-9799-1a7ee5df9d76",
      "name": "my-model",
      "remote": null,
      "metadata": {
        "framework": "pytorch",
        "description": "Local workspace"
      },
      "createdAt": "2026-11-28T12:00:00Z"
    }
    ```
  - Writes `.flair/HEAD` pointing to `main` with genesis commit hash `_GENESIS_COMMIT_`.
  - Writes `.flair/branches.json` tracking local branches.
- **Deferred Base Model Weights**: Replaced immediate upload prompt with an informational note indicating that weights will be uploaded on the first `flair push`.

### 3.2 100% Offline `flair branch` (`flair_cli/cli/branch.py`)
- **Removed Synchronous Branch RPCs**: Eliminated `api_client.create_branch` and `api_client.delete_branch`.
- **Local Branch Registration**:
  - `flair branch <name>` creates a new UUID v4 `branchHash` and appends it to `.flair/branches.json`.
  - `flair branch -d <name>` deletes the entry from `.flair/branches.json`.
  - `flair checkout <name>` checks `.flair/branches.json` first; only falls back to remote branch queries if a remote is linked.
- **Deferred Remote Branch Creation**: Branch records are communicated to the backend during `flair push` via the `targetBranch` payload in the manifest.

### 3.3 Two-Phase Journaled Commit Reconciliation (`flair_cli/cli/utils/commit_reconciliation.py`)
To solve the identity transition (from client UUID v4 to backend authoritative SHA-256) without risk of data loss or Windows file-locking corruption:
- **Journal File (`.flair/pending_reconciliation.json`)**: Before renaming directories, the CLI writes a write-ahead journal containing the complete `commitHashMap`, `targetBranch`, and `newHeadHash`.
- **Deep Metadata Rewriting**: Iterates through `.flair/.local_commits/` and updates `commit.json`:
  - `commitHash`: mapped to authoritative SHA-256.
  - `previousCommitHash`: remapped if parent was also in the push batch.
  - `parentCommit`, `mergedCommits`: remapped across merge commits.
- **Safe Directory Renaming**: Closes all file handles, performs folder rename from `<local_uuid>` to `<authoritative_sha256>`, and cleans up the journal upon completion.
- **Crash Recovery Hook**: `recover_pending_reconciliation_if_needed()` runs on every CLI invocation. If a push was killed mid-rename, the next CLI command detects the journal and completes the rename atomically.

### 3.4 Push Manifest API Client (`flair_cli/api/push.py` & `flair_cli/api/client.py`)
Introduced two dedicated endpoints in the API layer:
1. `push_manifest(repo_hash, manifest)` -> `POST /repo/hash/{repoHash}/push/manifest`
2. `push_finalize(repo_hash, payload)` -> `POST /repo/hash/{repoHash}/push/finalize`

### 3.5 Overhauled `flair push` Pipeline (`flair_cli/cli/push.py`)
The push pipeline was rewritten into a strict 4-phase protocol:

```
[New Local-First Push Architecture]
  Phase 0: Local Merkle Chain Validation
           (Verifies commit hash parentage locally before initiating any HTTP call)
               │
               ▼
  Phase 1: Pre-flight Remote Existence Check
           (GET /api/repo/hash/{repoHash} - halts with code 1 if 404 or unlinked)
               │
               ▼
  Phase 2: Push Manifest Handshake
           (POST /api/repo/hash/{repoHash}/push/manifest)
           - Submits local commit list + target branch
           - Receives missingCommits checklist + upload session tokens
               │
               ▼
  Phase 3: Serial Upload of Missing Artifacts
           - Reads binary payloads into memory buffers and closes file handles immediately
           - Uploads ZKP proofs (if missing) and parameter tensors (if missing)
               │
               ▼
  Phase 4: Batch Finalization & Commit Hash Reconciliation
           (POST /api/repo/hash/{repoHash}/push/finalize)
           - Receives commitHashMap: { [uuid]: sha256 }
           - Executes journaled directory rename & updates .flair/HEAD
```

#### Unlinked Repository Guard
In accordance with user decisions, if the repository does not exist on FlairHub or `repo.json` has `remote: null`:
```
Error: Remote repository '...' not found on remote.
Please create or link the repository on FlairHub first using:
  flair remote add origin <remote_repo_hash>
```

#### Windows File-Locking Fix
All file uploads were modified from:
```python
# Old: Open stream kept locked by httpx during subsequent rename
files = {"params": open(params_file, "rb")}
```
to:
```python
# New: Read bytes into memory and close file handle immediately
with open(params_file, "rb") as pf:
    payload_bytes = pf.read()
files = {"params": (params_file.name, payload_bytes, "application/octet-stream")}
```

---

## 4. Test Suite and Verification

The test harness was updated to reflect the local-first lifecycle:

1. **`tests/conftest.py`**:
   - Enhanced `MockFederatedBackend` to simulate the manifest handshake (`/push/manifest`), upload sessions, and finalize route (`/push/finalize`).
   - Fixed endpoint matching precedence so specific sub-endpoints (`basemodel/fetch_url`) take precedence over generic prefix routes (`repo/hash/`).
2. **`tests/commands/test_manifest_handshake_and_offline.py` (New Suite)**:
   - `test_init_and_branch_completely_offline`: Verifies `flair init` and `flair branch` succeed without network mocks.
   - `test_push_halts_when_remote_repo_not_found`: Asserts push aborts with exit code 1 and link instructions when remote is missing.
   - `test_push_manifest_handshake_and_reconciliation`: Validates manifest negotiation, serial upload, and directory renaming from UUID v4 to authoritative SHA-256.
   - `test_reconciliation_crash_recovery`: Validates that dangling `.flair/pending_reconciliation.json` files are automatically recovered by subsequent commands.
3. **Existing Workflow Tests Updated**:
   - `tests/commands/test_init_cmd.py`: Updated to assert offline UUID generation.
   - `tests/commands/test_branch_cmd.py`: Updated to assert local branch creation in `.flair/branches.json` instead of expecting pre-push remote backend mutations.
   - `tests/workflows/test_workflow_branching_collab.py`: Updated to reflect local-first branching.

**Test Result**: **117 passed, 16 skipped, 0 failed** in 14.73s.

---

## 5. Artifact & File Change Manifest

| File Path | Nature of Change | Description |
| :--- | :--- | :--- |
| `flair_cli/cli/init.py` | Modified | Made `init` 100% offline, generating client UUIDs for `repoHash` and `branchHash`. |
| `flair_cli/cli/branch.py` | Modified | Made `branch` 100% offline, storing branches in `.flair/branches.json`. |
| `flair_cli/cli/push.py` | Modified | Implemented manifest handshake, unlinked repo check, closed-handle file reads, and reconciliation invocation. |
| `flair_cli/cli/utils/commit_reconciliation.py` | **Created** | Two-phase journaled commit DAG renamer and crash recovery hook. |
| `flair_cli/api/push.py` | **Created** | Client functions for `push_manifest` and `push_finalize`. |
| `flair_cli/api/client.py` | Modified | Exported `push_manifest` and `push_finalize`. |
| `docs/adr/0003-push-manifest-handshake-and-local-first-lifecycle.md` | **Created** | Architectural Decision Record with full backend-facing schemas and specifications. |
| `GLOSSARY.md` | Modified | Added formal definitions for `Push Manifest`, `Push Handshake`, `Commit Hash Reconciliation`, and `Local-First Workspace`. |
| `tests/conftest.py` | Modified | Added mock support for manifest negotiation and SHA-256 commit generation. |
| `tests/commands/test_manifest_handshake_and_offline.py` | **Created** | Comprehensive unit test suite for offline init, branch, manifest handshake, and reconciliation. |
| `tests/commands/test_init_cmd.py` | Modified | Updated assertions for offline UUIDs. |
| `tests/commands/test_branch_cmd.py` | Modified | Updated assertions for local branch tracking. |
| `tests/workflows/test_workflow_branching_collab.py` | Modified | Updated assertions for local branch tracking. |

---

## 6. Backend Engineering Checklist for Handshake Support

For backend engineers implementing matching endpoints on FlairHub (`repo.controller.ts`, `push.controller.ts`):
1. **Model Addition**: Add `PushSession` model to Prisma with TTL expiration to manage handshake sessions.
2. **`POST /api/repo/hash/:repoHash/push/manifest`**:
   - Inspect target branch; auto-register in DB if missing.
   - Diff commits against DB and return `missingCommits` with upload session tokens.
3. **`POST /api/repo/hash/:repoHash/push/finalize`**:
   - Compute deterministic SHA-256 digests from canonical sorted JSON.
   - Atomically advance branch HEAD.
   - Return `commitHashMap: { [uuid]: sha256 }`.
4. **404 Guard**: Ensure `GET /api/repo/hash/:repoHash` returns strict 404 when repository does not exist.
5. **Decoupled Verification**: Set commit status to `PENDING_VERIFICATION` in push response and run heavy ZKML verification asynchronously in a worker queue.
