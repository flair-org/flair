import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from typer.testing import CliRunner

from flair_cli.main import app
from tests.conftest import make_mock_jwt


def test_exit_code_0_on_successful_init(multi_actor_env, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    repo_resp = {"id": "r-id", "hash": "r-hash", "repoHash": "r-hash", "name": "exit-code-repo", "metadata": {"framework": "PyTorch"}}
    with patch("flair_cli.cli.init.api_client.create_repo", return_value=repo_resp):
        res = cli_runner.invoke(app, ["init", "--name", "exit-code-repo", "--framework", "PyTorch", "--skip-base-model"])
        assert res.exit_code == 0


def test_exit_code_1_on_not_in_flair_repo(tmp_path, cli_runner):
    # Empty directory without .flair
    empty_dir = tmp_path / "empty_dir"
    empty_dir.mkdir()
    os.chdir(empty_dir)

    res = cli_runner.invoke(app, ["push"])
    assert res.exit_code == 1
    assert "not in a flair repository" in res.output.lower() or "no local commits" in res.output.lower() or "not found" in res.output.lower()


def test_exit_code_1_on_broken_commit_chain(
    multi_actor_env,
    cli_runner,
):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    flair_dir = actor_a["cwd"] / ".flair"
    flair_dir.mkdir(parents=True, exist_ok=True)
    (flair_dir / "repo.json").write_text('{"hash": "r-hash", "repoHash": "r-hash", "metadata": {"framework": "PyTorch"}}')
    (flair_dir / "HEAD").write_text('{"currentBranch": "main", "branchHash": "b-hash", "latestCommitHash": "c1", "previousCommit": "c1"}')

    local_commits = flair_dir / ".local_commits"
    c1_dir = local_commits / "c1"
    c1_dir.mkdir(parents=True, exist_ok=True)
    c1_data = {
        "commitHash": "c1",
        "message": "c1",
        "commitType": "CHECKPOINT",
        "previousCommitHash": None,
        "params": {"file": "params.pt"},
    }
    (c1_dir / "commit.json").write_text(json.dumps(c1_data))
    (c1_dir / "params.pt").write_bytes(b"dummy")

    # c2 has corrupted previousCommitHash that does NOT link to c1
    c2_dir = local_commits / "c2"
    c2_dir.mkdir(parents=True, exist_ok=True)
    c2_data = {
        "commitHash": "c2",
        "message": "c2",
        "commitType": "CHECKPOINT",
        "previousCommitHash": "non-existent-parent",
        "params": {"file": "params.pt"},
    }
    (c2_dir / "commit.json").write_text(json.dumps(c2_data))
    (c2_dir / "params.pt").write_bytes(b"dummy")

    with patch("flair_cli.cli.push.api_client.get_branch_by_name", return_value={"branchHash": "b-hash", "id": "b-id"}), \
         patch("flair_cli.cli.push._get_remote_latest_commit", return_value=None):
        res = cli_runner.invoke(app, ["push"])
        assert res.exit_code == 1
        assert "Chain integrity check failed" in res.output


def test_exit_code_2_on_unauthenticated_session(tmp_path, cli_runner):
    # Config dir with NO session.json
    empty_config = tmp_path / "no_auth_config"
    empty_config.mkdir()
    os.environ["FLAIR_CONFIG_DIR"] = str(empty_config)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    os.chdir(workspace)

    flair_dir = workspace / ".flair"
    flair_dir.mkdir(parents=True, exist_ok=True)
    (flair_dir / "repo.json").write_text('{"hash": "r-hash", "repoHash": "r-hash"}')

    res_pull = cli_runner.invoke(app, ["pull"])
    assert res_pull.exit_code == 2
    assert "Not authenticated" in res_pull.output


def test_exit_code_2_on_unauthorized_basemodel_upload(
    multi_actor_env,
    dummy_pytorch_model,
    cli_runner,
):
    actor_b = multi_actor_env["actor_b"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_b["config"])
    os.chdir(actor_b["cwd"])

    flair_dir = actor_b["cwd"] / ".flair"
    flair_dir.mkdir(parents=True, exist_ok=True)
    (flair_dir / "repo.json").write_text('{"hash": "r-hash", "repoHash": "r-hash"}')

    base_model_path = actor_b["cwd"] / "base_model.pt"
    dummy_pytorch_model(base_model_path)

    # Actor A owns repo, Actor B is not admin
    mock_repo = {
        "id": "r-id",
        "hash": "r-hash",
        "ownerAddress": multi_actor_env["actor_a"]["wallet"],
        "adminIds": [],
    }

    with patch("flair_cli.cli.basemodel.api_client.get_repo_by_hash", return_value=mock_repo), \
         patch("flair_cli.cli.basemodel._check_base_model_exists", return_value=(False, None)):
        res = cli_runner.invoke(app, ["basemodel", "add", "base_model.pt", "--force"])
        assert res.exit_code == 2
        assert "Permission denied" in res.output


def test_exit_code_2_on_unauthorized_merge_commit_push(
    multi_actor_env,
    mock_ssh_agent,
    cli_runner,
):
    actor_b = multi_actor_env["actor_b"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_b["config"])
    os.chdir(actor_b["cwd"])

    flair_dir = actor_b["cwd"] / ".flair"
    flair_dir.mkdir(parents=True, exist_ok=True)
    # Protected branches with only Actor A authorized
    repo_config = {
        "hash": "r-hash",
        "repoHash": "r-hash",
        "metadata": {
            "framework": "PyTorch",
            "mergePolicy": {
                "protectedBranches": ["main"],
                "authorizedWallets": [multi_actor_env["actor_a"]["wallet"]]
            }
        }
    }
    (flair_dir / "repo.json").write_text(json.dumps(repo_config))
    (flair_dir / "HEAD").write_text('{"currentBranch": "main", "branchHash": "b-hash", "latestCommitHash": None, "previousCommit": None}')

    # Create a MERGER commit
    local_commits = flair_dir / ".local_commits"
    c_dir = local_commits / "merge-commit-1"
    c_dir.mkdir(parents=True, exist_ok=True)
    c_data = {
        "commitHash": "merge-commit-1",
        "message": "MERGE commit",
        "commitType": "CHECKPOINT",
        "status": "MERGER",
        "previousCommitHash": None,
        "params": {"file": "params.pt"},
    }
    (c_dir / "commit.json").write_text(json.dumps(c_data))
    (c_dir / "params.pt").write_bytes(b"dummy")

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value.status_code = 200
    mock_client.get.return_value.json.return_value = {
        "data": [{"fingerprint": "SHA256:mockfingerprint123456789"}]
    }

    with patch("flair_cli.cli.push.api_client.get_branch_by_name", return_value={"branchHash": "b-hash", "id": "b-id"}), \
         patch("flair_cli.cli.push._get_remote_latest_commit", return_value=None), \
         patch("flair_cli.cli.push._client_with_auth", return_value=mock_client):
        res = cli_runner.invoke(app, ["push"])
        assert res.exit_code == 2
        assert "Permission denied" in res.output or "maintainer access" in res.output


def test_exit_code_3_on_remote_http_error(
    multi_actor_env,
    cli_runner,
):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    flair_dir = actor_a["cwd"] / ".flair"
    flair_dir.mkdir(parents=True, exist_ok=True)
    (flair_dir / "repo.json").write_text('{"hash": "r-hash", "repoHash": "r-hash"}')
    (flair_dir / "HEAD").write_text('{"currentBranch": "main", "branchHash": "b-hash"}')

    # Commit exists locally
    local_commits = flair_dir / ".local_commits"
    c_dir = local_commits / "c1"
    c_dir.mkdir(parents=True, exist_ok=True)
    (c_dir / "commit.json").write_text('{"commitHash": "c1", "message": "msg", "commitType": "CHECKPOINT", "params": {"file": "p.pt"}}')
    (c_dir / "p.pt").write_bytes(b"dummy")

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    err_resp = httpx.Response(status_code=500, request=httpx.Request("GET", "https://api.flair/sync"))
    mock_client.get.return_value.raise_for_status.side_effect = httpx.HTTPStatusError("Server error", request=err_resp.request, response=err_resp)

    with patch("flair_cli.cli.pull.api_client.get_branch_by_name", return_value={"branchHash": "b-hash", "id": "b-id"}), \
         patch("flair_cli.cli.pull._client_with_auth", return_value=mock_client):
        res_pull = cli_runner.invoke(app, ["pull"])
        assert res_pull.exit_code == 3
        assert "HTTP 500" in res_pull.output or "Error communicating with server" in res_pull.output


def test_exit_code_3_on_remote_network_connection_error(
    multi_actor_env,
    cli_runner,
):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    flair_dir = actor_a["cwd"] / ".flair"
    flair_dir.mkdir(parents=True, exist_ok=True)
    (flair_dir / "repo.json").write_text('{"hash": "r-hash", "repoHash": "r-hash"}')
    (flair_dir / "HEAD").write_text('{"currentBranch": "main", "branchHash": "b-hash"}')

    local_commits = flair_dir / ".local_commits"
    c_dir = local_commits / "c1"
    c_dir.mkdir(parents=True, exist_ok=True)
    (c_dir / "commit.json").write_text('{"commitHash": "c1", "message": "msg", "commitType": "CHECKPOINT", "params": {"file": "p.pt"}}')
    (c_dir / "p.pt").write_bytes(b"dummy")

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.side_effect = httpx.ConnectError("Failed to connect to host")

    with patch("flair_cli.cli.pull.api_client.get_branch_by_name", return_value={"branchHash": "b-hash", "id": "b-id"}), \
         patch("flair_cli.cli.pull._client_with_auth", return_value=mock_client):
        res_pull = cli_runner.invoke(app, ["pull"])
        assert res_pull.exit_code == 3
        assert "Network Error" in res_pull.output
