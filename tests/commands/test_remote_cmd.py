"""
Tier 1 Command Test: flair remote
Tests adding, listing, viewing, and removing remotes.
"""
import os
from pathlib import Path
from unittest.mock import patch
import pytest
from flair_cli.main import app

pytestmark = pytest.mark.command


def test_remote_add_view_remove(multi_actor_env, mock_backend, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    # 1. View remotes before repo initialization
    res_view_empty = cli_runner.invoke(app, ["remote", "view"])
    assert res_view_empty.exit_code == 0
    assert "no repository configured" in res_view_empty.output.lower()

    # 2. Add remote origin with mock_backend repo_hash
    with patch("flair_cli.cli.remote.api_client.clone_repository", return_value=mock_backend.clone_repository(mock_backend.repo_hash)):
        res_add = cli_runner.invoke(app, ["remote", "add", "origin", mock_backend.repo_hash])
        assert res_add.exit_code == 0
        assert "added successfully" in res_add.output.lower()

    # Verify .flair/repo.json and .flair/HEAD were created
    flair_dir = actor_a["cwd"] / ".flair"
    assert (flair_dir / "repo.json").exists()
    assert (flair_dir / "HEAD").exists()

    # 3. View remotes after addition
    res_view = cli_runner.invoke(app, ["remote", "view"])
    assert res_view.exit_code == 0
    assert "origin" in res_view.output
    assert "test-federated" in res_view.output

    # Also test 'list' alias
    res_list = cli_runner.invoke(app, ["remote", "list"])
    assert res_list.exit_code == 0
    assert "origin" in res_list.output

    # 4. Remove remote
    res_remove = cli_runner.invoke(app, ["remote", "remove", "origin"])
    assert res_remove.exit_code == 0
    assert "removed remote 'origin'" in res_remove.output.lower()
