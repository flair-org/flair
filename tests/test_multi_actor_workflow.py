"""
Multi-actor End-to-End Federated Learning workflow test.
Verifies the exact timeline:
1. Actor A (Admin) runs init, registers immutable base model, trains, creates ZKP, executes 5-step commit, and pushes to 'main'.
2. Actor B (Contributor) clones, pulls 'main', trains locally, creates ZKP, executes 5-step commit, and pushes to 'main'.
3. Actor A (Admin with merge rights) pulls changes, executes federated merge, and pushes the merge checkpoint to 'main'.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner
import torch

from flair_cli.main import app
from tests.conftest import make_mock_jwt


@pytest.fixture
def cli_runner():
    return CliRunner()


class MockFederatedBackend:
    """In-memory mock backend capturing state across Actor A and Actor B."""

    def __init__(self, owner_address: str):
        self.repo_hash = "mock-repo-hash-abc123456789"
        self.repo_id = "mock-repo-id-abc123"
        self.repo_name = "test-federated-repo"
        self.owner_address = owner_address
        self.admin_ids = [owner_address]
        self.base_model_cid = None
        self.base_model_url = None
        self.branches = {
            "main": {
                "branchId": "branch-main-id",
                "branchHash": "branch-main-hash-111",
                "name": "main",
                "commits": []
            }
        }
        self.commits = {}

    def create_repo(self, payload):
        return {
            "id": self.repo_id,
            "hash": self.repo_hash,
            "repoHash": self.repo_hash,
            "name": payload.get("name", self.repo_name),
            "ownerAddress": self.owner_address,
            "adminIds": self.admin_ids,
            "metadata": payload.get("metadata", {})
        }

    def get_repo_by_hash(self, repo_hash):
        return {
            "id": self.repo_id,
            "hash": self.repo_hash,
            "repoHash": self.repo_hash,
            "name": self.repo_name,
            "ownerAddress": self.owner_address,
            "adminIds": self.admin_ids,
            "metadata": {"framework": "PyTorch"}
        }

    def upload_base_model(self, repo_hash, file_path):
        self.base_model_cid = "QmMockBaseModelCID123"
        self.base_model_url = f"https://mock-storage.flair/base/{Path(file_path).name}"
        return {
            "cid": self.base_model_cid,
            "url": self.base_model_url,
            "fileExtension": Path(file_path).suffix
        }

    def get_base_model_url(self, repo_hash):
        if not self.base_model_url:
            raise Exception("No base model exists")
        return {
            "data": self.base_model_url,
            "fileExtension": ".pt"
        }

    def get_branch_by_name(self, repo_hash, branch_name):
        return self.branches.get(branch_name, {
            "id": f"branch-{branch_name}-id",
            "branchId": f"branch-{branch_name}-id",
            "branchHash": f"branch-{branch_name}-hash",
            "name": branch_name
        })

    def create_branch(self, repo_hash, name, parent_hash, description):
        branch = {
            "id": f"branch-{name}-id",
            "branchId": f"branch-{name}-id",
            "branchHash": f"branch-{name}-hash",
            "name": name,
            "commits": []
        }
        self.branches[name] = branch
        return branch

    def get_branches(self, repo_hash):
        return list(self.branches.values())

    def clone_repository(self, repo_hash):
        latest_hash = self.branches["main"]["commits"][-1] if self.branches["main"]["commits"] else None
        latest_commit_dict = {"commitHash": latest_hash, "params": {}} if latest_hash else None
        return {
            "repo": {
                "name": self.repo_name,
                "hash": self.repo_hash,
                "id": self.repo_id,
                "owner": self.owner_address,
                "metadata": {"framework": "PyTorch"},
                "baseModel": {"uri": self.base_model_url, "cid": self.base_model_cid, "extension": ".pt"}
            },
            "branches": [
                {
                    "name": "main",
                    "hash": self.branches["main"]["branchHash"],
                    "branchHash": self.branches["main"]["branchHash"],
                    "isDefault": True,
                    "latestCommit": latest_commit_dict
                }
            ]
        }


    def handle_post(self, url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "commit/create/initiate" in str(url):
            resp.json.return_value = {
                "sessionId": "mock-session-123",
                "initiateToken": make_mock_jwt({"jti": "mock-jti-nonce-987", "sub": "test-user"})
            }
        elif "commit/create/zkml-check" in str(url):
            resp.json.return_value = {"unique": True, "zkmlToken": "mock-zkml-token-555"}
        elif "commit/create/zkml-upload" in str(url):
            resp.json.return_value = {"zkmlReceiptToken": "mock-zkml-receipt-token-111"}
        elif "commit/create/params-upload" in str(url):
            resp.json.return_value = {
                "paramsReceiptToken": "mock-params-receipt-token-222",
                "paramsIpfsId": "QmMockParamsIpfsCid333"
            }
        elif "commit/create/finalize" in str(url):
            data = kwargs.get("json", {})
            commit_hash = f"commit-hash-author-{len(self.commits) + 1}"
            self.commits[commit_hash] = data
            self.branches["main"]["commits"].append(commit_hash)
            resp.json.return_value = {
                "data": {
                    "status": "APPROVED",
                    "commitHash": commit_hash
                }
            }
        else:
            resp.json.return_value = {"status": "SUCCESS"}
        return resp

    def handle_get(self, url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        if "api/user/keys" in str(url):
            resp.json.return_value = {
                "data": [{"fingerprint": "SHA256:mockfingerprint123456789"}]
            }
        elif "commit/status/sync" in str(url):
            resp.json.return_value = {
                "data": [{"commitHash": h, "status": "APPROVED"} for h in self.branches["main"]["commits"]]
            }
        elif "commit/latest" in str(url):
            latest = self.branches["main"]["commits"][-1] if self.branches["main"]["commits"] else None
            resp.json.return_value = {"data": {"commitHash": latest}}
        else:
            resp.json.return_value = {"data": {}}
        return resp


def test_full_multi_actor_federated_lifecycle(
    multi_actor_env,
    mock_ssh_agent,
    mock_zkp,
    dummy_pytorch_model,
    cli_runner,
):
    actor_a = multi_actor_env["actor_a"]
    actor_b = multi_actor_env["actor_b"]
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

        # =====================================================================
        # 1. ACTOR A: Initialize repository
        # =====================================================================
        os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
        os.chdir(actor_a["cwd"])

        result_init = cli_runner.invoke(app, ["init", "--name", "test-federated-repo", "--framework", "PyTorch", "--skip-base-model"])
        assert result_init.exit_code == 0, f"Actor A init failed: {result_init.output}"
        assert (actor_a["cwd"] / ".flair").exists()

        # =====================================================================
        # 2. ACTOR A: Create and upload immutable base model
        # =====================================================================
        base_model_path = actor_a["cwd"] / "base_model.pt"
        dummy_pytorch_model(base_model_path)

        result_base = cli_runner.invoke(app, ["basemodel", "add", "base_model.pt", "--force"])
        assert result_base.exit_code == 0, f"Actor A base model add failed: {result_base.output}"
        assert backend.base_model_cid is not None

        # =====================================================================
        # 3. ACTOR A: Local training & 5-step commit creation
        # =====================================================================
        model_a_path = actor_a["cwd"] / "model.pt"
        dummy_pytorch_model(model_a_path)

        # Step 1: flair add
        res_add_a = cli_runner.invoke(app, ["add"])
        assert res_add_a.exit_code == 0, f"Actor A flair add failed: {res_add_a.output}"

        # Step 2: flair params create
        res_params_a = cli_runner.invoke(app, ["params", "create", "--model", "model.pt", "--classes", "cat,dog"])
        assert res_params_a.exit_code == 0, f"Actor A params create failed: {res_params_a.output}"

        # Step 3: flair zkp create
        res_zkp_a = cli_runner.invoke(app, ["zkp", "create", "--model", "model.pt"])
        assert res_zkp_a.exit_code == 0, f"Actor A zkp create failed: {res_zkp_a.output}"

        # Step 4: flair commit
        res_commit_a = cli_runner.invoke(app, ["commit", "-m", "Initial base checkpoint from Actor A"])
        assert res_commit_a.exit_code == 0, f"Actor A commit failed: {res_commit_a.output}"

        # Step 5: flair push to main
        res_push_a = cli_runner.invoke(app, ["push"])
        assert res_push_a.exit_code == 0, f"Actor A push failed: {res_push_a.output}"
        assert len(backend.branches["main"]["commits"]) == 1

        # =====================================================================
        # 4. ACTOR B: Clone repository
        # =====================================================================
        os.environ["FLAIR_CONFIG_DIR"] = str(actor_b["config"])
        os.chdir(actor_b["cwd"])

        res_clone_b = cli_runner.invoke(app, ["clone", backend.repo_hash, "--target-dir", "cloned_repo"])
        assert res_clone_b.exit_code == 0, f"Actor B clone failed: {res_clone_b.output}"

        repo_b_dir = actor_b["cwd"] / "cloned_repo"
        os.chdir(repo_b_dir)
        assert (repo_b_dir / ".flair").exists()

        # =====================================================================
        # 5. ACTOR B: Pull latest main branch & sync base commit
        # =====================================================================
        res_pull_b = cli_runner.invoke(app, ["pull"])
        assert res_pull_b.exit_code == 0, f"Actor B pull failed: {res_pull_b.output}"

        # In federated environment, pull/sync ensures local repo has base commit
        local_commits_dir_a = actor_a["cwd"] / ".flair" / ".local_commits"
        local_commits_dir_b = repo_b_dir / ".flair" / ".local_commits"
        local_commits_dir_b.mkdir(parents=True, exist_ok=True)
        for commit_dir in local_commits_dir_a.iterdir():
            target = local_commits_dir_b / commit_dir.name
            if not target.exists() and commit_dir.is_dir():
                shutil.copytree(commit_dir, target)

        # =====================================================================
        # 6. ACTOR B: Train locally, 5-step commit & push to main
        # =====================================================================
        model_b_path = repo_b_dir / "model.pt"
        dummy_pytorch_model(model_b_path)

        # Step 1: add
        res_add_b = cli_runner.invoke(app, ["add"])
        assert res_add_b.exit_code == 0, f"Actor B flair add failed: {res_add_b.output}"

        # Step 2: params create
        res_params_b = cli_runner.invoke(app, ["params", "create", "--model", "model.pt", "--classes", "cat,dog"])
        assert res_params_b.exit_code == 0, f"Actor B params create failed: {res_params_b.output}"

        # Step 3: zkp create
        res_zkp_b = cli_runner.invoke(app, ["zkp", "create", "--model", "model.pt"])
        assert res_zkp_b.exit_code == 0, f"Actor B zkp create failed: {res_zkp_b.output}"

        # Step 4: commit
        res_commit_b = cli_runner.invoke(app, ["commit", "-m", "Contributor training update from Actor B"])
        assert res_commit_b.exit_code == 0, f"Actor B commit failed: {res_commit_b.output}"

        # Step 5: push
        res_push_b = cli_runner.invoke(app, ["push"])
        assert res_push_b.exit_code == 0, f"Actor B push failed: {res_push_b.output}"
        assert len(backend.branches["main"]["commits"]) == 2

        # =====================================================================
        # 7. ACTOR A: Pull Actor B's commit & sync
        # =====================================================================
        os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
        os.chdir(actor_a["cwd"])

        res_pull_a = cli_runner.invoke(app, ["pull"])
        assert res_pull_a.exit_code == 0, f"Actor A pull failed: {res_pull_a.output}"

        # Synchronize Actor B's commit to Actor A's local commits
        for commit_dir in local_commits_dir_b.iterdir():
            target = local_commits_dir_a / commit_dir.name
            if not target.exists() and commit_dir.is_dir():
                shutil.copytree(commit_dir, target)

        import time
        time.sleep(0.05)

        # =====================================================================
        # 8. ACTOR A: Execute federated merge (flair merge create)
        # =====================================================================
        res_merge_a = cli_runner.invoke(app, ["merge", "create", "--min-children", "1"])
        assert res_merge_a.exit_code == 0, f"Actor A merge failed: {res_merge_a.output}"

        # =====================================================================
        # 9. ACTOR A: Push merge checkpoint commit to main
        # =====================================================================
        res_push_merge_a = cli_runner.invoke(app, ["push"])
        assert res_push_merge_a.exit_code == 0, f"Actor A merge push failed: {res_push_merge_a.output}"
        assert len(backend.branches["main"]["commits"]) == 3


