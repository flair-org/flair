"""
Tier 1 Command Test: flair metrics
Tests set, show, reset, import, and verification of staged metrics.
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


def test_metrics_lifecycle(multi_actor_env, mock_backend, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])
    _setup_initialized_repo(actor_a["cwd"], cli_runner, mock_backend)

    # 1. Show metrics on empty repo
    res_show_empty = cli_runner.invoke(app, ["metrics", "show"])
    assert res_show_empty.exit_code == 0
    assert "no staged metrics found" in res_show_empty.output.lower()

    # 2. Set metrics with no args -> exit code 1
    res_set_empty = cli_runner.invoke(app, ["metrics", "set"])
    assert res_set_empty.exit_code == 1

    # 3. Set metrics
    res_set = cli_runner.invoke(app, [
        "metrics", "set",
        "--accuracy", "0.945",
        "--val-loss", "0.152",
        "--epoch", "10"
    ])
    assert res_set.exit_code == 0
    assert "metrics staged" in res_set.output.lower()

    # Verify .flair/metrics.json created
    metrics_file = actor_a["cwd"] / ".flair" / "metrics.json"
    assert metrics_file.exists()
    data = json.loads(metrics_file.read_text())
    assert data["accuracy"] == 0.945
    assert data["epoch"] == 10

    # 4. Show metrics
    res_show = cli_runner.invoke(app, ["metrics", "show"])
    assert res_show.exit_code == 0
    assert "0.945" in res_show.output
    assert "0.152" in res_show.output

    # 5. Verify unproven metrics
    res_verify = cli_runner.invoke(app, ["metrics", "verify"])
    assert res_verify.exit_code == 0
    assert "self-reported" in res_verify.output.lower() or "unverified" in res_verify.output.lower()

    # 6. Reset metrics
    res_reset = cli_runner.invoke(app, ["metrics", "reset"])
    assert res_reset.exit_code == 0
    assert "metrics reset" in res_reset.output.lower()
    assert not metrics_file.exists()

