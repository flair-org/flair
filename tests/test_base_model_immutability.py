import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from typer.testing import CliRunner

from flair_cli.main import app


def test_admin_can_upload_base_model_first_time(
    multi_actor_env,
    dummy_pytorch_model,
    cli_runner,
):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    # Init repo
    repo_hash = "mock-repo-hash-111"
    with patch("flair_cli.cli.init.api_client.create_repo", return_value={"id": "repo-id", "hash": repo_hash}):
        res_init = cli_runner.invoke(app, ["init", "--name", "immutability-repo", "--framework", "PyTorch", "--skip-base-model"])
        assert res_init.exit_code == 0

    base_model_path = actor_a["cwd"] / "base_model.pt"
    dummy_pytorch_model(base_model_path)

    mock_repo = {
        "id": "repo-id",
        "hash": repo_hash,
        "ownerAddress": actor_a["wallet"],
        "adminIds": [],
    }

    with patch("flair_cli.cli.basemodel.api_client.get_repo_by_hash", return_value=mock_repo), \
         patch("flair_cli.cli.basemodel._check_base_model_exists", return_value=(False, None)), \
         patch("flair_cli.cli.basemodel.api_client.upload_base_model", return_value={"cid": "bafybase", "url": "https://flair/base.pt"}):
        res_add = cli_runner.invoke(app, ["basemodel", "add", "base_model.pt", "--force"])
        assert res_add.exit_code == 0
        assert "Base model uploaded successfully" in res_add.output


def test_base_model_is_immutable_cannot_be_overwritten(
    multi_actor_env,
    dummy_pytorch_model,
    cli_runner,
):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    repo_hash = "mock-repo-hash-111"
    flair_dir = actor_a["cwd"] / ".flair"
    flair_dir.mkdir(parents=True, exist_ok=True)
    repo_file = flair_dir / "repo.json"
    repo_file.write_text('{"hash": "mock-repo-hash-111", "repoHash": "mock-repo-hash-111"}')

    base_model_path = actor_a["cwd"] / "base_model_v2.pt"
    dummy_pytorch_model(base_model_path)

    mock_repo = {
        "id": "repo-id",
        "hash": repo_hash,
        "ownerAddress": actor_a["wallet"],
        "adminIds": [],
    }

    # Simulate base model already exists
    with patch("flair_cli.cli.basemodel.api_client.get_repo_by_hash", return_value=mock_repo), \
         patch("flair_cli.cli.basemodel._check_base_model_exists", return_value=(True, "https://flair/existing.pt")):
        res_add = cli_runner.invoke(app, ["basemodel", "add", "base_model_v2.pt", "--force"])
        assert res_add.exit_code != 0
        assert "immutable" in res_add.output.lower() or "already exists" in res_add.output.lower()


def test_non_admin_cannot_upload_base_model(
    multi_actor_env,
    dummy_pytorch_model,
    cli_runner,
):
    actor_b = multi_actor_env["actor_b"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_b["config"])
    os.chdir(actor_b["cwd"])

    # Create dummy repo config in Actor B's dir
    flair_dir = actor_b["cwd"] / ".flair"
    flair_dir.mkdir(parents=True, exist_ok=True)
    repo_file = flair_dir / "repo.json"
    repo_file.write_text('{"hash": "mock-repo-hash-111", "repoHash": "mock-repo-hash-111"}')

    base_model_path = actor_b["cwd"] / "malicious_base_model.pt"
    dummy_pytorch_model(base_model_path)

    # Repository belongs to Actor A, Actor B is not admin
    mock_repo = {
        "id": "repo-id",
        "hash": "mock-repo-hash-111",
        "ownerAddress": multi_actor_env["actor_a"]["wallet"],
        "adminIds": ["0xSomeOtherAdmin"],
    }

    with patch("flair_cli.cli.basemodel.api_client.get_repo_by_hash", return_value=mock_repo), \
         patch("flair_cli.cli.basemodel._check_base_model_exists", return_value=(False, None)):
        res_add = cli_runner.invoke(app, ["basemodel", "add", "malicious_base_model.pt", "--force"])
        assert res_add.exit_code == 2
        assert "Permission denied" in res_add.output


def test_non_admin_cannot_delete_base_model(
    multi_actor_env,
    cli_runner,
):
    actor_b = multi_actor_env["actor_b"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_b["config"])
    os.chdir(actor_b["cwd"])

    flair_dir = actor_b["cwd"] / ".flair"
    flair_dir.mkdir(parents=True, exist_ok=True)
    repo_file = flair_dir / "repo.json"
    repo_file.write_text('{"hash": "mock-repo-hash-111", "repoHash": "mock-repo-hash-111"}')

    mock_repo = {
        "id": "repo-id",
        "hash": "mock-repo-hash-111",
        "ownerAddress": multi_actor_env["actor_a"]["wallet"],
        "adminIds": [],
    }

    with patch("flair_cli.cli.basemodel.api_client.get_repo_by_hash", return_value=mock_repo), \
         patch("flair_cli.cli.basemodel._check_base_model_exists", return_value=(True, "https://flair/existing.pt")):
        res_del = cli_runner.invoke(app, ["basemodel", "delete", "--yes"])
        assert res_del.exit_code == 2
        assert "Permission denied" in res_del.output
