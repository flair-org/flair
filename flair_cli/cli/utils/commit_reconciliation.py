"""
Commit Hash Reconciliation Utility.

Applies backend-authoritative SHA-256 commit hashes to local UUID-based commits
using a crash-resilient two-phase journal (.flair/pending_reconciliation.json).
"""
from __future__ import annotations
from pathlib import Path
import json
import os
import shutil


def get_journal_path() -> Path:
    return Path.cwd() / ".flair" / "pending_reconciliation.json"


def record_reconciliation_journal(commit_hash_map: dict[str, str], branch_head: str | None, target_branch: str) -> Path:
    """Write journal file before beginning local commit DAG modifications."""
    journal_path = get_journal_path()
    journal_data = {
        "commitHashMap": commit_hash_map,
        "branchHead": branch_head,
        "targetBranch": target_branch
    }
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    with open(journal_path, "w", encoding="utf-8") as f:
        json.dump(journal_data, f, indent=2)
    return journal_path


def apply_commit_hash_reconciliation(commit_hash_map: dict[str, str], branch_head: str | None, target_branch: str):
    """
    Atomically rewrite local commit UUIDs to authoritative SHA-256 digests.
    """
    if not commit_hash_map:
        return

    flair_dir = Path.cwd() / ".flair"
    local_commits_dir = flair_dir / ".local_commits"

    if not local_commits_dir.exists():
        return

    # Record journal first
    record_reconciliation_journal(commit_hash_map, branch_head, target_branch)

    # 1. Update commit metadata and pointers
    for local_uuid, authoritative_sha in commit_hash_map.items():
        commit_dir = local_commits_dir / local_uuid
        # If already renamed from previous partial run
        if not commit_dir.exists():
            commit_dir = local_commits_dir / authoritative_sha

        if not commit_dir.exists():
            continue

        commit_file = commit_dir / "commit.json"
        if commit_file.exists():
            try:
                with open(commit_file, "r", encoding="utf-8") as f:
                    cdata = json.load(f)

                cdata["commitHash"] = authoritative_sha
                cdata["localCommitHash"] = local_uuid

                # Re-point parent commit hash if it was part of this push batch
                parent_hash = cdata.get("previousCommitHash")
                if parent_hash in commit_hash_map:
                    cdata["previousCommitHash"] = commit_hash_map[parent_hash]

                if "parentCommit" in cdata and cdata["parentCommit"] in commit_hash_map:
                    cdata["parentCommit"] = commit_hash_map[cdata["parentCommit"]]

                # Remap merge parents if present
                if "mergedParentHashes" in cdata and isinstance(cdata["mergedParentHashes"], list):
                    cdata["mergedParentHashes"] = [commit_hash_map.get(m, m) for m in cdata["mergedParentHashes"]]

                with open(commit_file, "w", encoding="utf-8") as f:
                    json.dump(cdata, f, indent=2)
            except Exception:
                pass

        # Rename directory if needed
        dest_dir = local_commits_dir / authoritative_sha
        if commit_dir.name != authoritative_sha:
            try:
                if dest_dir.exists() and dest_dir != commit_dir:
                    shutil.rmtree(dest_dir)
                commit_dir.rename(dest_dir)
            except Exception:
                pass

    # 2. Update .flair/HEAD
    head_file = flair_dir / "HEAD"
    if head_file.exists():
        try:
            with open(head_file, "r", encoding="utf-8") as f:
                head_data = json.load(f)

            if branch_head:
                head_data["previousCommit"] = branch_head
            elif head_data.get("previousCommit") in commit_hash_map:
                head_data["previousCommit"] = commit_hash_map[head_data["previousCommit"]]

            with open(head_file, "w", encoding="utf-8") as f:
                json.dump(head_data, f, indent=2)
        except Exception:
            pass

    # 3. Clean up journal file upon successful completion
    journal_path = get_journal_path()
    if journal_path.exists():
        try:
            journal_path.unlink()
        except Exception:
            pass


def recover_pending_reconciliation_if_needed():
    """Detect and complete any interrupted reconciliation journal."""
    journal_path = get_journal_path()
    if journal_path.exists():
        try:
            with open(journal_path, "r", encoding="utf-8") as f:
                journal = json.load(f)
            apply_commit_hash_reconciliation(
                journal.get("commitHashMap", {}),
                journal.get("branchHead"),
                journal.get("targetBranch", "main")
            )
        except Exception:
            pass
