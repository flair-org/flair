"""
Tier 1 Command Test: flair basemodel
Tests add, check, download, delete, immutability, and RBAC exit codes (0, 1, 2).
"""
import os
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest
from flair_cli.main import app

pytestmark = pytest.mark.command


def _setup_repo(cwd, cli_runner, mock_backend):
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


def test_basemodel_add_and_check(multi_actor_env, mock_backend, cli_runner, tmp_path):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])
    _setup_repo(actor_a["cwd"], cli_runner, mock_backend)

    # Initial check: no base model uploaded
    mock_backend.base_model_url = None
    res_check = cli_runner.invoke(app, ["basemodel", "check"])
    assert res_check.exit_code == 0
    assert "no base model" in res_check.output.lower()

    # Create dummy model file
    model_file = actor_a["cwd"] / "initial_model.pt"
    model_file.write_bytes(b"dummy_pt_weights")

    # Add base model
    res_add = cli_runner.invoke(app, ["basemodel", "add", str(model_file)])
    assert res_add.exit_code == 0
    assert "uploaded successfully" in res_add.output.lower()

    # Check again: exists
    res_check2 = cli_runner.invoke(app, ["basemodel", "check"])
    assert res_check2.exit_code == 0
    assert "base model exists" in res_check2.output.lower()


def test_basemodel_immutability_rejection(multi_actor_env, mock_backend, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])
    _setup_repo(actor_a["cwd"], cli_runner, mock_backend)

    mock_backend.base_model_url = "https://flair.mock/basemodel.pt"

    model_file = actor_a["cwd"] / "second_model.pt"
    model_file.write_bytes(b"another_model_weights")

    res = cli_runner.invoke(app, ["basemodel", "add", str(model_file)])
    assert res.exit_code == 1
    assert "immutable" in res.output.lower()


def test_basemodel_rbac_non_admin_forbidden(multi_actor_env, mock_backend, cli_runner):
    actor_b = multi_actor_env["actor_b"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_b["config"])
    os.chdir(actor_b["cwd"])
    _setup_repo(actor_b["cwd"], cli_runner, mock_backend)

    mock_backend.base_model_url = None
    model_file = actor_b["cwd"] / "model.pt"
    model_file.write_bytes(b"dummy_weights")

    # Actor B is contributor, not owner/admin -> exit code 2
    res = cli_runner.invoke(app, ["basemodel", "add", str(model_file)])
    assert res.exit_code == 2
    assert "permission denied" in res.output.lower()


def test_basemodel_download(multi_actor_env, mock_backend, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])
    _setup_repo(actor_a["cwd"], cli_runner, mock_backend)

    mock_backend.base_model_url = "https://flair.mock/basemodel.pt"

    with patch("flair_cli.cli.basemodel.download_base_model", return_value=True):
        res = cli_runner.invoke(app, ["basemodel", "download"])
        assert res.exit_code == 0


def test_basemodel_delete(multi_actor_env, mock_backend, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])
    _setup_repo(actor_a["cwd"], cli_runner, mock_backend)

    mock_backend.base_model_url = "https://flair.mock/basemodel.pt"
    mock_backend.base_model_cid = "QmExistingCid"

    res = cli_runner.invoke(app, ["basemodel", "delete", "-y"])
    assert res.exit_code == 0
    assert "deleted" in res.output.lower()

