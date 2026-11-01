"""
Tier 2 Workflow Scenario A: Linear FedAvg Multi-Actor Lifecycle
Verifies the exact timeline:
1. Actor A (Admin) runs init, registers immutable base model, trains, creates ZKP, executes 5-step commit, and pushes to 'main'.
2. Actor B (Contributor) clones, pulls 'main', trains locally, creates ZKP, executes 5-step commit, and pushes to 'main'.
3. Actor A (Admin with merge rights) pulls changes, executes federated merge, and pushes the merge checkpoint to 'main'.
"""
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from flair_cli.main import app

pytestmark = pytest.mark.workflow


def test_scenario_a_linear_fedavg(
    multi_actor_env,
    mock_ssh_agent,
    mock_zkp,
    dummy_pytorch_model,
    cli_runner,
):
    actor_a = multi_actor_env["actor_a"]
    actor_b = multi_actor_env["actor_b"]
    
    from tests.conftest import MockFederatedBackend
    backend = MockFederatedBackend(owner_address=actor_a["wallet"])

    mock_http_client = MagicMock()
    mock_http_client.__enter__.return_value = mock_http_client
    mock_http_client.post.side_effect = backend.handle_post
    mock_http_client.get.side_effect = backend.handle_get

    def fake_download(url, target_path):
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"dummy_bytes")

    with patch("flair_cli.cli.init.api_client.create_repo", side_effect=backend.create_repo), \
         patch("flair_cli.cli.basemodel.api_client.get_repo_by_hash", side_effect=backend.get_repo_by_hash), \
         patch("flair_cli.cli.basemodel.api_client.get_base_model_url", side_effect=backend.get_base_model_url), \
         patch("flair_cli.cli.basemodel.api_client.upload_base_model", side_effect=backend.upload_base_model), \
         patch("flair_cli.cli.basemodel._check_base_model_exists", return_value=(False, None)), \
         patch("flair_cli.cli.clone.api_client.clone_repository", side_effect=backend.clone_repository), \
         patch("flair_cli.cli.clone._download_file", side_effect=fake_download), \
         patch("flair_cli.cli.push.api_client.get_branch_by_name", side_effect=backend.get_branch_by_name), \
         patch("flair_cli.cli.push.api_client.create_branch", side_effect=backend.create_branch), \
         patch("flair_cli.cli.push._get_remote_latest_commit", side_effect=lambda r, b: backend.branches["main"]["commits"][-1] if backend.branches["main"]["commits"] else None), \
         patch("flair_cli.cli.pull.api_client.get_branch_by_name", side_effect=backend.get_branch_by_name), \
         patch("flair_cli.cli.push._client_with_auth", return_value=mock_http_client), \
         patch("flair_cli.cli.pull._client_with_auth", return_value=mock_http_client):

        # 1. ACTOR A: Initialize repository
        os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
        os.chdir(actor_a["cwd"])

        result_init = cli_runner.invoke(app, ["init", "--name", "test-federated-repo", "--framework", "PyTorch", "--skip-base-model"])
        assert result_init.exit_code == 0, f"Actor A init failed: {result_init.output}"

        # 2. ACTOR A: Upload base model
        base_model_path = actor_a["cwd"] / "base_model.pt"
        dummy_pytorch_model(base_model_path)

        result_base = cli_runner.invoke(app, ["basemodel", "add", "base_model.pt", "--force"])
        assert result_base.exit_code == 0, f"Actor A base model add failed: {result_base.output}"

        # 3. ACTOR A: Local training & 5-step commit
        model_a_path = actor_a["cwd"] / "model.pt"
        dummy_pytorch_model(model_a_path)

        assert cli_runner.invoke(app, ["add"]).exit_code == 0
        assert cli_runner.invoke(app, ["params", "create", "--model", "model.pt", "--classes", "cat,dog"]).exit_code == 0
        assert cli_runner.invoke(app, ["zkp", "create", "--model", "model.pt"]).exit_code == 0
        assert cli_runner.invoke(app, ["commit", "-m", "Initial base checkpoint from Actor A"]).exit_code == 0

        res_push_a = cli_runner.invoke(app, ["push"])
        assert res_push_a.exit_code == 0, f"Actor A push failed: {res_push_a.output}"
        assert len(backend.branches["main"]["commits"]) == 1

        # 4. ACTOR B: Clone repository
        os.environ["FLAIR_CONFIG_DIR"] = str(actor_b["config"])
        os.chdir(actor_b["cwd"])

        res_clone_b = cli_runner.invoke(app, ["clone", backend.repo_hash, "--target-dir", "cloned_repo"])
        assert res_clone_b.exit_code == 0, f"Actor B clone failed: {res_clone_b.output}"

        repo_b_dir = actor_b["cwd"] / "cloned_repo"
        os.chdir(repo_b_dir)

        # 5. ACTOR B: Pull latest main
        assert cli_runner.invoke(app, ["pull"]).exit_code == 0

        # Synchronize commit history for in-memory mock backend
        local_commits_dir_a = actor_a["cwd"] / ".flair" / ".local_commits"
        local_commits_dir_b = repo_b_dir / ".flair" / ".local_commits"
        local_commits_dir_b.mkdir(parents=True, exist_ok=True)
        for commit_dir in local_commits_dir_a.iterdir():
            target = local_commits_dir_b / commit_dir.name
            if not target.exists() and commit_dir.is_dir():
                shutil.copytree(commit_dir, target)

        # 6. ACTOR B: Local training, 5-step commit & push to main
        model_b_path = repo_b_dir / "model.pt"
        dummy_pytorch_model(model_b_path)

        assert cli_runner.invoke(app, ["add"]).exit_code == 0
        assert cli_runner.invoke(app, ["params", "create", "--model", "model.pt", "--classes", "cat,dog"]).exit_code == 0
        assert cli_runner.invoke(app, ["zkp", "create", "--model", "model.pt"]).exit_code == 0
        assert cli_runner.invoke(app, ["commit", "-m", "Contributor training update from Actor B"]).exit_code == 0

        res_push_b = cli_runner.invoke(app, ["push"])
        assert res_push_b.exit_code == 0, f"Actor B push failed: {res_push_b.output}"
        assert len(backend.branches["main"]["commits"]) == 2

        # 7. ACTOR A: Pull Actor B's commit
        os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
        os.chdir(actor_a["cwd"])

        assert cli_runner.invoke(app, ["pull"]).exit_code == 0
        for commit_dir in local_commits_dir_b.iterdir():
            target = local_commits_dir_a / commit_dir.name
            if not target.exists() and commit_dir.is_dir():
                shutil.copytree(commit_dir, target)

        # 8. ACTOR A: Federated merge (flair merge create)
        res_merge_a = cli_runner.invoke(app, ["merge", "create", "--min-children", "1"])
        assert res_merge_a.exit_code == 0, f"Actor A merge failed: {res_merge_a.output}"

        # 9. ACTOR A: Push merge checkpoint commit to main
        res_push_merge_a = cli_runner.invoke(app, ["push"])
        assert res_push_merge_a.exit_code == 0, f"Actor A merge push failed: {res_push_merge_a.output}"
        assert len(backend.branches["main"]["commits"]) == 3
