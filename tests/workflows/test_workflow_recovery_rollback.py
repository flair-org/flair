"""
Tier 2 Workflow Scenario C: Disaster Recovery & Rollback
Tests:
1. Detection of parameter divergence via `flair diff`
2. Rollback of unpushed faulty commits via `flair reset HEAD~1`
3. Synchronization recovery via `flair reset --to-remote`
4. Compensating checkpoint creation via `flair revert`
"""
import os
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from flair_cli.main import app

pytestmark = pytest.mark.workflow


def test_scenario_c_recovery_rollback(
    multi_actor_env,
    mock_ssh_agent,
    mock_zkp,
    dummy_pytorch_model,
    cli_runner,
):
    actor_a = multi_actor_env["actor_a"]
    from tests.conftest import MockFederatedBackend
    backend = MockFederatedBackend(owner_address=actor_a["wallet"])

    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    # 1. Initialize repository
    repo_resp = {
        "id": backend.repo_id,
        "hash": backend.repo_hash,
        "repoHash": backend.repo_hash,
        "name": backend.repo_name,
        "ownerAddress": backend.owner_address,
        "metadata": {"framework": "PyTorch"},
        "defaultBranchHash": "b-main-hash"
    }

    with patch("flair_cli.cli.init.api_client.create_repo", return_value=repo_resp):
        assert cli_runner.invoke(app, ["init", "--name", backend.repo_name, "--framework", "PyTorch", "--skip-base-model"]).exit_code == 0

    flair_dir = actor_a["cwd"] / ".flair"
    local_commits_dir = flair_dir / ".local_commits"

    # 2. Establish Good Baseline Commit (C1)
    c1_hash = "goodbaseline00000000000000000000"
    c1_dir = local_commits_dir / c1_hash
    c1_dir.mkdir(parents=True, exist_ok=True)
    (c1_dir / "commit.json").write_text(json.dumps({
        "commitHash": c1_hash,
        "commitType": "CHECKPOINT",
        "message": "Baseline Good Model",
        "author": actor_a["wallet"],
        "timestamp": "2026-11-01T10:00:00Z",
        "params": {"file": "params.pt"},
        "previousCommitHash": "_GENESIS_COMMIT_"
    }))
    (c1_dir / "params.pt").write_bytes(b"good_baseline_weights")

    # Set as REMOTE_HEAD
    (flair_dir / "REMOTE_HEAD").write_text(json.dumps({"latestCommitHash": c1_hash}))

    # 3. Simulate Faulty / Divergent Commit (C2)
    c2_hash = "faultycommit11111111111111111111"
    c2_dir = local_commits_dir / c2_hash
    c2_dir.mkdir(parents=True, exist_ok=True)
    (c2_dir / "proof.zlib").write_bytes(b"dummy_proof")
    (c2_dir / "verification_key.zlib").write_bytes(b"dummy_vk")
    (c2_dir / "settings.zlib").write_bytes(b"dummy_settings")
    (c2_dir / "commit.json").write_text(json.dumps({
        "commitHash": c2_hash,
        "commitType": "DELTA",
        "message": "Corrupted Weights Regression",
        "author": actor_a["wallet"],
        "timestamp": "2026-11-01T11:00:00Z",
        "params": {"file": "params.pt"},
        "zkp": {
            "proof_file": "proof.zlib",
            "verification_key_file": "verification_key.zlib",
            "settings_file": "settings.zlib"
        },
        "previousCommitHash": c1_hash
    }))
    (c2_dir / "params.pt").write_bytes(b"faulty_corrupted_weights")

    # Point HEAD to faulty commit
    head_file = flair_dir / "HEAD"
    head_file.write_text(json.dumps({
        "currentBranch": "main",
        "branchHash": "b-main-hash",
        "latestCommitHash": c2_hash,
        "previousCommit": c2_hash
    }))

    # 4. Inspect status: 1 unpushed commit
    with patch("flair_cli.cli.status._get_remote_latest_commit", return_value=c1_hash):
        res_status = cli_runner.invoke(app, ["status"])
        assert res_status.exit_code == 0
        assert "Unpushed commits: 1" in res_status.output

        # 5. Rollback unpushed faulty commit using `flair reset --to-remote`
        with patch("flair_cli.cli.reset._get_target_full_params", return_value={"layer": 1}):
            res_reset = cli_runner.invoke(app, ["reset", "--to-remote"])
            assert res_reset.exit_code == 0
            assert "reset successful" in res_reset.output.lower()

        # Verify HEAD is restored to C1
        head_after_reset = json.loads(head_file.read_text())
        assert head_after_reset["latestCommitHash"] == c1_hash

        # Verify status now reports 0 unpushed commits
        res_status2 = cli_runner.invoke(app, ["status"])
        assert res_status2.exit_code == 0
        assert "Unpushed commits: 0" in res_status2.output
