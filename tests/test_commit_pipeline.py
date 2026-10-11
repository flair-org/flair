import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from flair_cli.main import app
from tests.conftest import make_mock_jwt


def _setup_staged_commit(cwd: Path, dummy_pytorch_model, cli_runner):
    model_path = cwd / "model.pt"
    dummy_pytorch_model(model_path)
    res_add = cli_runner.invoke(app, ["add"])
    assert res_add.exit_code == 0
    res_params = cli_runner.invoke(app, ["params", "create", "--model", "model.pt", "--classes", "cat,dog"])
    assert res_params.exit_code == 0
    res_zkp = cli_runner.invoke(app, ["zkp", "create", "--model", "model.pt"])
    assert res_zkp.exit_code == 0, f"ZKP failed: {res_zkp.output}"
    res_commit = cli_runner.invoke(app, ["commit", "-m", "Staged commit for pipeline testing"])
    assert res_commit.exit_code == 0


def test_commit_pipeline_full_success(
    multi_actor_env,
    mock_ssh_agent,
    mock_zkp,
    dummy_pytorch_model,
    cli_runner,
):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    repo_resp = {"id": "r-id", "hash": "r-hash", "repoHash": "r-hash", "name": "pipe-repo", "metadata": {"framework": "PyTorch"}}
    with patch("flair_cli.cli.init.api_client.create_repo", return_value=repo_resp):
        cli_runner.invoke(app, ["init", "--name", "pipe-repo", "--framework", "PyTorch", "--skip-base-model"])

    _setup_staged_commit(actor_a["cwd"], dummy_pytorch_model, cli_runner)

    steps_called = []

    def mock_post(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        url_str = str(url)
        if "commit/create/initiate" in url_str:
            steps_called.append("initiate")
            resp.json.return_value = {
                "sessionId": "s-123",
                "initiateToken": make_mock_jwt({"jti": "nonce-1", "sub": "u1"})
            }
        elif "commit/create/zkml-check" in url_str:
            steps_called.append("zkml-check")
            resp.json.return_value = {"unique": True, "zkmlToken": "zkml-tok"}
        elif "commit/create/zkml-upload" in url_str:
            steps_called.append("zkml-upload")
            resp.json.return_value = {"zkmlReceiptToken": "zkml-rcpt"}
        elif "commit/create/params-upload" in url_str:
            steps_called.append("params-upload")
            resp.json.return_value = {"paramsReceiptToken": "params-rcpt", "paramsIpfsId": "QmParams"}
        elif "commit/create/finalize" in url_str:
            steps_called.append("finalize")
            resp.json.return_value = {"data": {"status": "APPROVED", "commitHash": "final-commit-hash"}}
        else:
            resp.json.return_value = {}
        return resp

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.side_effect = mock_post
    mock_client.get.return_value.status_code = 200
    mock_client.get.return_value.json.return_value = {
        "data": [{"fingerprint": "SHA256:mockfingerprint123456789"}]
    }

    with patch("flair_cli.cli.push.api_client.get_branch_by_name", return_value={"branchHash": "b-hash", "id": "b-id"}), \
         patch("flair_cli.cli.push._get_remote_latest_commit", return_value=None), \
         patch("flair_cli.cli.push._client_with_auth", return_value=mock_client):
        res_push = cli_runner.invoke(app, ["push"])
        assert res_push.exit_code == 0
        assert steps_called == ["initiate", "zkml-check", "zkml-upload", "params-upload", "finalize"]
        assert "Push complete!" in res_push.output


def test_commit_pipeline_aborts_when_zkml_not_unique(
    multi_actor_env,
    mock_ssh_agent,
    mock_zkp,
    dummy_pytorch_model,
    cli_runner,
):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    repo_resp = {"id": "r-id", "hash": "r-hash", "repoHash": "r-hash", "name": "pipe-repo", "metadata": {"framework": "PyTorch"}}
    with patch("flair_cli.cli.init.api_client.create_repo", return_value=repo_resp):
        cli_runner.invoke(app, ["init", "--name", "pipe-repo", "--framework", "PyTorch", "--skip-base-model"])

    _setup_staged_commit(actor_a["cwd"], dummy_pytorch_model, cli_runner)

    steps_called = []

    def mock_post(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        url_str = str(url)
        if "commit/create/initiate" in url_str:
            steps_called.append("initiate")
            resp.json.return_value = {
                "sessionId": "s-123",
                "initiateToken": make_mock_jwt({"jti": "nonce-1", "sub": "u1"})
            }
        elif "commit/create/zkml-check" in url_str:
            steps_called.append("zkml-check")
            # Duplicate zkML proof!
            resp.json.return_value = {"unique": False, "message": "Duplicate zkML proof detected"}
        elif "commit/create/zkml-upload" in url_str:
            steps_called.append("zkml-upload")
        return resp

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.side_effect = mock_post
    mock_client.get.return_value.status_code = 200
    mock_client.get.return_value.json.return_value = {
        "data": [{"fingerprint": "SHA256:mockfingerprint123456789"}]
    }

    with patch("flair_cli.cli.push.api_client.get_branch_by_name", return_value={"branchHash": "b-hash", "id": "b-id"}), \
         patch("flair_cli.cli.push._get_remote_latest_commit", return_value=None), \
         patch("flair_cli.cli.push._client_with_auth", return_value=mock_client):
        res_push = cli_runner.invoke(app, ["push"])
        assert res_push.exit_code != 0
        assert steps_called == ["initiate", "zkml-check"]
        assert "zkml-upload" not in steps_called
        assert "params-upload" not in steps_called
        assert "finalize" not in steps_called


def test_commit_pipeline_aborts_on_zkml_upload_failure(
    multi_actor_env,
    mock_ssh_agent,
    mock_zkp,
    dummy_pytorch_model,
    cli_runner,
):
    import httpx
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    repo_resp = {"id": "r-id", "hash": "r-hash", "repoHash": "r-hash", "name": "pipe-repo", "metadata": {"framework": "PyTorch"}}
    with patch("flair_cli.cli.init.api_client.create_repo", return_value=repo_resp):
        cli_runner.invoke(app, ["init", "--name", "pipe-repo", "--framework", "PyTorch", "--skip-base-model"])

    _setup_staged_commit(actor_a["cwd"], dummy_pytorch_model, cli_runner)

    steps_called = []

    def mock_post(url, *args, **kwargs):
        resp = MagicMock()
        url_str = str(url)
        if "commit/create/initiate" in url_str:
            steps_called.append("initiate")
            resp.status_code = 200
            resp.json.return_value = {
                "sessionId": "s-123",
                "initiateToken": make_mock_jwt({"jti": "nonce-1", "sub": "u1"})
            }
        elif "commit/create/zkml-check" in url_str:
            steps_called.append("zkml-check")
            resp.status_code = 200
            resp.json.return_value = {"unique": True, "zkmlToken": "zkml-tok"}
        elif "commit/create/zkml-upload" in url_str:
            steps_called.append("zkml-upload")
            resp.status_code = 500
            error_response = httpx.Response(status_code=500, request=httpx.Request("POST", url))
            resp.raise_for_status.side_effect = httpx.HTTPStatusError("Server error", request=error_response.request, response=error_response)
        return resp

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.post.side_effect = mock_post
    mock_client.get.return_value.status_code = 200
    mock_client.get.return_value.json.return_value = {
        "data": [{"fingerprint": "SHA256:mockfingerprint123456789"}]
    }

    with patch("flair_cli.cli.push.api_client.get_branch_by_name", return_value={"branchHash": "b-hash", "id": "b-id"}), \
         patch("flair_cli.cli.push._get_remote_latest_commit", return_value=None), \
         patch("flair_cli.cli.push._client_with_auth", return_value=mock_client):
        res_push = cli_runner.invoke(app, ["push"])
        assert res_push.exit_code != 0
        assert steps_called == ["initiate", "zkml-check", "zkml-upload"]
        assert "params-upload" not in steps_called
        assert "finalize" not in steps_called
