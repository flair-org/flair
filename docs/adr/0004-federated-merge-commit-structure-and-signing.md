# ADR 0004: Federated Merge Commit Structure, Strict Commit Types, and Provenance Signing

## Status
Accepted

## Context
In Flair's federated learning lifecycle, repository Admins aggregate model updates submitted by multiple Contributors into a unified model state using `flair merge create`.

Previously, the schema and identity of merge commits suffered from architectural ambiguity:
1. **Commit Type Fragmentation**: Vestigial code paths in `flair pull` and `flair push` checked for a hypothetical `"MERGE"` commit type, whereas FlairHub's backend database strictly models commit types as an enum of either `CHECKPOINT` or `DELTA`.
2. **Inconsistent Parent Hashes**: `flair merge` stored parent references under `"mergedCommits"` at the root of `commit.json` and duplicated them under `"metadata.mergeParents"`, while the backend API contract specifies `mergedParentHashes`.
3. **Missing Provenance in Commit Signatures**: `build_canonical_payload()` omitted merge parents, meaning developer SSH signatures did not cryptographically commit to the parent DAG lineage that produced the aggregated model weights.
4. **Local-First Reconciliation Risk**: In offline workspaces, local contributor commits use client-generated UUID v4 identifiers. If a local merge commit signed those UUIDs, FlairHub's backend (which assigns authoritative SHA-256 hashes upon upload) would reject the signature verification.

## Decisions

### 1. Strict Two-Value Commit Type Enum (`CHECKPOINT | DELTA`)
- The backend commit type enum is strictly `CHECKPOINT` or `DELTA`. There is no `MERGE` or `MERGED` commit type.
- A federated merge commit is structurally a `CHECKPOINT` commit containing full aggregated parameters (`params.pt` or `params.npz`).
- All vestigial references to `commitType == "MERGE"` are eliminated across the CLI (`pull.py`, `push.py`).

### 2. Standardized `mergedParentHashes` Schema
- The root of `commit.json` stores parent references in a single canonical list:
  ```json
  {
    "commitHash": "uuid-or-sha256",
    "commitType": "CHECKPOINT",
    "mergedParentHashes": [
      "parent-hash-1",
      "parent-hash-2"
    ],
    "message": "MERGE of 2 commits",
    "params": { ... }
  }
  ```
- `mergedCommits` and `metadata.mergeParents` are completely deprecated and removed.
- Non-merge commits store `"mergedParentHashes": null`.
- The presence of a non-empty `mergedParentHashes` list serves as the permanent, immutable discriminator identifying a federated merge commit across `status`, `log`, `reconstruction`, and `pull`.

### 3. Canonical Commit Signing with Provenance
- `mergedParentHashes` is incorporated into `build_canonical_payload()` in `flair_cli/cli/utils/commit_signing.py`:
  ```python
  {
      "sessionJti": session_jti,
      "signedAt": signed_at,
      "paramsIpfsId": params_ipfs_id,
      "paramHash": param_hash,
      "previousCommitHash": previous_commit_hash,
      "architecture": architecture,
      "architectureHash": None,
      "commitType": commit_type,
      "mergedParentHashes": sorted(merged_parent_hashes) if merged_parent_hashes else None,
      "message": message,
      "metrics": normalize_json_value(metrics or {}),
  }
  ```
- For standard commits, `"mergedParentHashes": null` is serialized.
- For merge commits, `mergedParentHashes` is sorted lexicographically (`sorted(...)`) before JSON canonicalization to ensure deterministic signatures regardless of local aggregation array order.

### 4. In-Flight UUID Remapping Before Signing
- When `flair push` uploads a batch containing both contributor commits and a merge commit, contributor commits are assigned authoritative SHA-256 hashes by the backend.
- Before constructing `canonical_payload` and requesting the SSH signature, `flair push` remaps any temporary UUIDs in `mergedParentHashes` to their authoritative SHA-256 hashes using the in-flight `commit_hash_map`.
- The two-phase journaled reconciliation engine (`commit_reconciliation.py`) also updates `mergedParentHashes` during crash-resilient disk renames.

### 5. Push Manifest & Finalize Payloads
- `mergedParentHashes` is transmitted in `manifest_payload["commits"]` during `POST /push/manifest`.
- `mergedParentHashes` is transmitted in the commit finalization payload during `POST .../commit/create/finalize`.

## Consequences
- **Positive**: Clean alignment between Flair CLI and FlairHub Prisma schemas (`CHECKPOINT` and `DELTA` only).
- **Positive**: Cryptographic provenance guarantees: SSH signatures mathematically bind the aggregated checkpoint weights to their constituent parent commits.
- **Positive**: Elimination of type-branch fragmentation and redundant metadata keys (`mergedCommits`, `mergeParents`).
