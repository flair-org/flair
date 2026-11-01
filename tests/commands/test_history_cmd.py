"""
Tier 1 Command Test: flair status, log, diff
Tests repository state, commit history traversal, and weight diffing.
"""
import os
import json
from pathlib import Path
from unittest.mock import patch
import pytest
from flair_cli.main import app

pytestmark = pytest.mark.command


def _setup_initialized_repo(cwd, cli_runner, mock_backend):
    repo_resp = {
        "id": mock_backend.repo_id,
        "hash": mock_backend.repo_hash,
        "repoHash": mock_backend.repo_hash,
        "name": mock_backend.repo_name,
        "ownerAddress": mock_backend.owner_address,
        "metadata": {"framework": "PyTorch"},
        "defaultBranchHash": "b-main-hash"
    }
    with patch("flair_cli.cli.init.api_client.create_repo", return_value=repo_resp):
        res = cli_runner.invoke(app, ["init", "--name", mock_backend.repo_name, "--framework", "PyTorch", "--skip-base-model"])
        assert res.exit_code == 0


def test_status_uninitialized(cli_runner, tmp_path):
    os.chdir(tmp_path)
    res = cli_runner.invoke(app, ["status"])
    assert res.exit_code == 1
    assert "not a flair repository" in res.output.lower() or "run 'flair init'" in res.output.lower()


def test_status_and_log_initialized(multi_actor_env, mock_backend, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])
    _setup_initialized_repo(actor_a["cwd"], cli_runner, mock_backend)

    # 1. Status on empty repo
    res_status = cli_runner.invoke(app, ["status"])
    assert res_status.exit_code == 0
    assert "Branch: main" in res_status.output
    assert "Unpushed commits: 0" in res_status.output

    # 2. Log on empty repo
    res_log = cli_runner.invoke(app, ["log"])
    assert res_log.exit_code == 0
    assert "no commits found" in res_log.output.lower()

    # 3. Simulate a commit in .flair/.local_commits/
    flair_dir = actor_a["cwd"] / ".flair"
    commit_hash = "mockcommithash1234567890abcdef"
    commit_dir = flair_dir / ".local_commits" / commit_hash
    commit_dir.mkdir(parents=True, exist_ok=True)
    
    commit_data = {
        "commitHash": commit_hash,
        "commitType": "CHECKPOINT",
        "message": "First test commit",
        "author": actor_a["wallet"],
        "timestamp": "2026-11-01T12:00:00Z",
        "params": {"file": "params.pt", "ipfsObject": {"cid": "mock-cid"}},
        "previousCommitHash": "_GENESIS_COMMIT_"
    }
    (commit_dir / "commit.json").write_text(json.dumps(commit_data))
    (commit_dir / "params.pt").write_bytes(b"dummy_weights")

    # Update HEAD
    head_file = flair_dir / "HEAD"
    head_data = {
        "currentBranch": "main",
        "branchHash": "b-main-hash",
        "latestCommitHash": commit_hash,
        "previousCommit": commit_hash
    }
    head_file.write_text(json.dumps(head_data))

    # Test status again
    res_status2 = cli_runner.invoke(app, ["status"])
    assert res_status2.exit_code == 0
    assert "Branch: main" in res_status2.output
    assert "Params: \u2713" in res_status2.output

    # Test log with commit
    res_log2 = cli_runner.invoke(app, ["log"])
    assert res_log2.exit_code == 0
    assert "First test commit" in res_log2.output
    assert commit_hash[:8] in res_log2.output


def test_diff_command(multi_actor_env, mock_backend, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])
    _setup_initialized_repo(actor_a["cwd"], cli_runner, mock_backend)

    # Missing required arguments -> exit code 2
    res_missing = cli_runner.invoke(app, ["diff"])
    assert res_missing.exit_code == 2

    # Nonexistent commits -> exit code 1
    res_nonexistent = cli_runner.invoke(app, ["diff", "deadbeef1", "deadbeef2"])
    assert res_nonexistent.exit_code == 1
    assert "error: commit not found" in res_nonexistent.output.lower()
