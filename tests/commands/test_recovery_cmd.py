"""
Tier 1 Command Test: flair reset and flair revert
Tests rollback to previous commits, remote HEAD synchronization, and revert commits.
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


def test_reset_uninitialized_and_invalid(cli_runner, tmp_path):
    os.chdir(tmp_path)
    # 1. Uninitialized
    res = cli_runner.invoke(app, ["reset"])
    assert res.exit_code == 1

    # 2. Invalid syntax
    res_inv = cli_runner.invoke(app, ["reset", "INVALID_TARGET"])
    assert res_inv.exit_code == 1


def test_reset_head_back(multi_actor_env, mock_backend, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])
    _setup_initialized_repo(actor_a["cwd"], cli_runner, mock_backend)

    flair_dir = actor_a["cwd"] / ".flair"
    local_commits_dir = flair_dir / ".local_commits"

    # Commit 1 (Genesis parent)
    c1_hash = "c1111111111111111111111111111111"
    c1_dir = local_commits_dir / c1_hash
    c1_dir.mkdir(parents=True, exist_ok=True)
    (c1_dir / "commit.json").write_text(json.dumps({
        "commitHash": c1_hash,
        "commitType": "CHECKPOINT",
        "message": "Commit 1",
        "previousCommitHash": "_GENESIS_COMMIT_",
        "params": {"file": "params.pt"}
    }))
    (c1_dir / "params.pt").write_bytes(b"c1_weights")

    # Commit 2 (Parent is Commit 1)
    c2_hash = "c2222222222222222222222222222222"
    c2_dir = local_commits_dir / c2_hash
    c2_dir.mkdir(parents=True, exist_ok=True)
    (c2_dir / "commit.json").write_text(json.dumps({
        "commitHash": c2_hash,
        "commitType": "DELTA",
        "message": "Commit 2",
        "previousCommitHash": c1_hash,
        "params": {"file": "params.pt"}
    }))
    (c2_dir / "params.pt").write_bytes(b"c2_weights")

    # Set HEAD to Commit 2
    head_file = flair_dir / "HEAD"
    head_file.write_text(json.dumps({
        "currentBranch": "main",
        "branchHash": "b-main-hash",
        "latestCommitHash": c2_hash,
        "previousCommit": c2_hash
    }))

    # Reset HEAD~1
    with patch("flair_cli.cli.reset._get_target_full_params", return_value={"layer": 1}):
        res_reset = cli_runner.invoke(app, ["reset", "HEAD~1"])
        assert res_reset.exit_code == 0
        assert "reset successful" in res_reset.output.lower()

    # Verify HEAD now points to Commit 1
    new_head = json.loads(head_file.read_text())
    assert new_head.get("latestCommitHash") == c1_hash


def test_reset_to_remote(multi_actor_env, mock_backend, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])
    _setup_initialized_repo(actor_a["cwd"], cli_runner, mock_backend)

    flair_dir = actor_a["cwd"] / ".flair"
    local_commits_dir = flair_dir / ".local_commits"

    remote_hash = "cremote0000000000000000000000000"
    (flair_dir / "REMOTE_HEAD").write_text(json.dumps({
        "latestCommitHash": remote_hash
    }))

    # Set target commit dir
    c_dir = local_commits_dir / remote_hash
    c_dir.mkdir(parents=True, exist_ok=True)
    (c_dir / "commit.json").write_text(json.dumps({
        "commitHash": remote_hash,
        "commitType": "CHECKPOINT",
        "message": "Remote Commit",
        "previousCommitHash": "_GENESIS_COMMIT_",
        "params": {"file": "params.pt"}
    }))
    (c_dir / "params.pt").write_bytes(b"remote_weights")

    # Current head is unpushed commit
    unpushed_hash = "cunpushed99999999999999999999999"
    head_file = flair_dir / "HEAD"
    head_file.write_text(json.dumps({
        "currentBranch": "main",
        "branchHash": "b-main-hash",
        "latestCommitHash": unpushed_hash,
        "previousCommit": unpushed_hash
    }))

    with patch("flair_cli.cli.reset._get_target_full_params", return_value={"w": 1}):
        res_remote = cli_runner.invoke(app, ["reset", "--to-remote"])
        assert res_remote.exit_code == 0
        assert "reset successful" in res_remote.output.lower()

    new_head = json.loads(head_file.read_text())
    assert new_head.get("latestCommitHash") == remote_hash


def test_revert_parent_commit(multi_actor_env, mock_backend, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])
    _setup_initialized_repo(actor_a["cwd"], cli_runner, mock_backend)

    flair_dir = actor_a["cwd"] / ".flair"
    local_commits_dir = flair_dir / ".local_commits"

    # Parent commit
    p_hash = "parentcommit1111111111111111111"
    p_dir = local_commits_dir / p_hash
    p_dir.mkdir(parents=True, exist_ok=True)
    (p_dir / "commit.json").write_text(json.dumps({
        "commitHash": p_hash,
        "commitType": "CHECKPOINT",
        "message": "Parent Good Commit",
        "previousCommitHash": "_GENESIS_COMMIT_",
        "params": {"file": "params.pt"}
    }))
    (p_dir / "params.pt").write_bytes(b"good_params")

    # Current faulty head commit
    head_hash = "faultyhead2222222222222222222222"
    h_dir = local_commits_dir / head_hash
    h_dir.mkdir(parents=True, exist_ok=True)
    (h_dir / "commit.json").write_text(json.dumps({
        "commitHash": head_hash,
        "commitType": "DELTA",
        "message": "Faulty Commit",
        "previousCommitHash": p_hash,
        "params": {"file": "params.pt"}
    }))
    (h_dir / "params.pt").write_bytes(b"faulty_params")

    head_file = flair_dir / "HEAD"
    head_file.write_text(json.dumps({
        "currentBranch": "main",
        "branchHash": "b-main-hash",
        "latestCommitHash": head_hash,
        "previousCommit": head_hash
    }))

    def fake_save_params(params, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"reverted_weights")
        return True

    # Invoke revert
    with patch("flair_cli.cli.revert._get_parent_full_params", return_value={"w": 1}), \
         patch("flair_cli.cli.revert._save_pytorch_params", side_effect=fake_save_params), \
         patch("flair_cli.cli.revert.compute_architecture_hash", return_value="mock_arch_hash"):
        res_revert = cli_runner.invoke(app, ["revert", "-m", "Reverting faulty commit"])
        assert res_revert.exit_code == 0
        assert "revert successful" in res_revert.output.lower()
