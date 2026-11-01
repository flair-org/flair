"""
Tier 2 Workflow Scenario B: Branching & Collaboration
Tests:
1. Feature branch creation from main (flair branch <feature>)
2. Branch switching (flair checkout <feature>)
3. Intelligent branch caching and restoration (.flair/.cache/<branch>)
4. Commits on isolated feature branches without polluting main
"""
import os
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
from flair_cli.main import app

pytestmark = pytest.mark.workflow


def test_scenario_b_branching_collab(
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
         patch("flair_cli.cli.clone.api_client.clone_repository", side_effect=backend.clone_repository), \
         patch("flair_cli.cli.clone._download_file", side_effect=fake_download), \
         patch("flair_cli.cli.branch.api_client.get_branches", side_effect=backend.get_branches), \
         patch("flair_cli.cli.branch.api_client.create_branch", side_effect=backend.create_branch), \
         patch("flair_cli.cli.branch.api_client.get_branch_by_name", side_effect=backend.get_branch_by_name), \
         patch("flair_cli.cli.branch._download_file", side_effect=fake_download), \
         patch("flair_cli.cli.push.api_client.get_branch_by_name", side_effect=backend.get_branch_by_name), \
         patch("flair_cli.cli.push.api_client.create_branch", side_effect=backend.create_branch), \
         patch("flair_cli.cli.push._get_remote_latest_commit", side_effect=lambda r, b: backend.branches.get(b, {}).get("commits", [None])[-1] if backend.branches.get(b, {}).get("commits") else None), \
         patch("flair_cli.cli.push._client_with_auth", return_value=mock_http_client):

        # -----------------------------------------------------------------
        # 1. Admin initializes repo
        # -----------------------------------------------------------------
        os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
        os.chdir(actor_a["cwd"])

        res_init = cli_runner.invoke(app, ["init", "--name", "branch-collab-repo", "--framework", "PyTorch", "--skip-base-model"])
        assert res_init.exit_code == 0

        # Baseline commit on main
        model_a = actor_a["cwd"] / "model.pt"
        dummy_pytorch_model(model_a)
        assert cli_runner.invoke(app, ["add"]).exit_code == 0
        assert cli_runner.invoke(app, ["params", "create", "--model", "model.pt", "--classes", "cat,dog"]).exit_code == 0
        assert cli_runner.invoke(app, ["zkp", "create", "--model", "model.pt"]).exit_code == 0
        assert cli_runner.invoke(app, ["commit", "-m", "Baseline commit on main"]).exit_code == 0
        assert cli_runner.invoke(app, ["push"]).exit_code == 0

        # -----------------------------------------------------------------
        # 2. Contributor clones repo
        # -----------------------------------------------------------------
        os.environ["FLAIR_CONFIG_DIR"] = str(actor_b["config"])
        os.chdir(actor_b["cwd"])

        res_clone = cli_runner.invoke(app, ["clone", backend.repo_hash, "--target-dir", "cloned_repo"])
        assert res_clone.exit_code == 0
        repo_b = actor_b["cwd"] / "cloned_repo"
        os.chdir(repo_b)

        # -----------------------------------------------------------------
        # 3. Contributor creates & checks out feature branch
        # -----------------------------------------------------------------
        res_br = cli_runner.invoke(app, ["branch", "experiment-lora"])
        assert res_br.exit_code == 0
        assert "experiment-lora" in backend.branches

        res_co = cli_runner.invoke(app, ["checkout", "experiment-lora"])
        assert res_co.exit_code == 0

        # Verify HEAD points to experiment-lora
        head_data = json.loads((repo_b / ".flair" / "HEAD").read_text())
        assert head_data["currentBranch"] == "experiment-lora"

        # -----------------------------------------------------------------
        # 4. Contributor stages and commits on feature branch
        # -----------------------------------------------------------------
        model_b = repo_b / "model.pt"
        dummy_pytorch_model(model_b)
        assert cli_runner.invoke(app, ["add"]).exit_code == 0
        assert cli_runner.invoke(app, ["params", "create", "--model", "model.pt", "--classes", "cat,dog"]).exit_code == 0
        assert cli_runner.invoke(app, ["zkp", "create", "--model", "model.pt"]).exit_code == 0
        assert cli_runner.invoke(app, ["commit", "-m", "LoRA experiment checkpoint"]).exit_code == 0

        # Verify commit exists in .flair/.local_commits
        local_commits_dir = repo_b / ".flair" / ".local_commits"
        assert any(local_commits_dir.iterdir())

        # Now stage uncommitted work on experiment-lora to test branch cache preservation
        staged_params_file = repo_b / ".flair" / ".params" / "params_in_progress.pt"
        staged_params_file.write_bytes(b"wip_lora_weights")

        # -----------------------------------------------------------------
        # 5. Switch back to main -> cache is saved
        # -----------------------------------------------------------------
        res_co_main = cli_runner.invoke(app, ["checkout", "main"])
        assert res_co_main.exit_code == 0

        cache_dir = repo_b / ".flair" / ".cache" / "experiment-lora"
        assert cache_dir.exists()
        assert (cache_dir / "params_in_progress.pt").exists()

        # -----------------------------------------------------------------
        # 6. Switch back to experiment-lora -> cache is restored
        # -----------------------------------------------------------------
        res_co_lora = cli_runner.invoke(app, ["checkout", "experiment-lora"])
        assert res_co_lora.exit_code == 0
        assert "restored" in res_co_lora.output.lower()
        assert (repo_b / ".flair" / ".params" / "params_in_progress.pt").exists()
