"""
Tier 1 Command Test: flair branch & checkout
Asserts branch listing, creation, switching, and deletion.
"""
import os
from unittest.mock import patch
import pytest
from flair_cli.main import app

pytestmark = pytest.mark.command


def _setup_initialized_repo(cwd, cli_runner):
    repo_resp = {
        "id": "r-id",
        "hash": "r-hash",
        "repoHash": "r-hash",
        "name": "branch-test-repo",
        "metadata": {"framework": "PyTorch"},
        "defaultBranchHash": "b-main-hash"
    }
    with patch("flair_cli.cli.init.api_client.create_repo", return_value=repo_resp):
        res = cli_runner.invoke(app, ["init", "--name", "branch-test-repo", "--framework", "PyTorch", "--skip-base-model"])
        assert res.exit_code == 0


def test_branch_list_and_create(multi_actor_env, mock_backend, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    _setup_initialized_repo(actor_a["cwd"], cli_runner)

    # 1. List branches (initially main)
    with patch("flair_cli.cli.branch.api_client.get_branches", side_effect=mock_backend.get_branches):
        res_list = cli_runner.invoke(app, ["branch"])
        assert res_list.exit_code == 0
        assert "main" in res_list.output

    # 2. Create new branch 'feature-exp1'
    with patch("flair_cli.cli.branch.api_client.create_branch", side_effect=mock_backend.create_branch), \
         patch("flair_cli.cli.branch.api_client.get_branches", side_effect=mock_backend.get_branches):
        res_create = cli_runner.invoke(app, ["branch", "feature-exp1", "--description", "experiment branch"])
        assert res_create.exit_code == 0
        assert "created" in res_create.output.lower()
        assert "feature-exp1" in mock_backend.branches

    # 3. List branches again
    with patch("flair_cli.cli.branch.api_client.get_branches", side_effect=mock_backend.get_branches):
        res_list2 = cli_runner.invoke(app, ["branch"])
        assert res_list2.exit_code == 0
        assert "feature-exp1" in res_list2.output


def test_branch_delete(multi_actor_env, mock_backend, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    _setup_initialized_repo(actor_a["cwd"], cli_runner)

    with patch("flair_cli.cli.branch.api_client.create_branch", side_effect=mock_backend.create_branch), \
         patch("flair_cli.cli.branch.api_client.get_branches", side_effect=mock_backend.get_branches):
        res_create = cli_runner.invoke(app, ["branch", "temp-branch"])
        assert res_create.exit_code == 0

    def mock_del(repo_h, branch_h):
        mock_backend.branches.pop("temp-branch", None)

    with patch("flair_cli.cli.branch.api_client.get_branches", side_effect=mock_backend.get_branches), \
         patch("flair_cli.cli.branch.api_client.delete_branch", side_effect=mock_del):
        res_del = cli_runner.invoke(app, ["branch", "-d", "temp-branch"])
        assert res_del.exit_code == 0
        assert "deleted" in res_del.output.lower()


def test_checkout_switch_branch(multi_actor_env, mock_backend, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    _setup_initialized_repo(actor_a["cwd"], cli_runner)
    mock_backend.create_branch(mock_backend.repo_hash, "staging")

    with patch("flair_cli.cli.branch.api_client.get_branches", side_effect=mock_backend.get_branches):
        res_switch = cli_runner.invoke(app, ["checkout", "staging"])
        assert res_switch.exit_code == 0
        assert "switched" in res_switch.output.lower() or "staging" in res_switch.output.lower()
