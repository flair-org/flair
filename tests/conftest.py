"""
Pytest fixtures for Federated Learning CLI tests.
Includes multi-actor environment isolation, mocked auth/session,
mocked SSH agent identities, mocked ZKP (EZKL), and shared MockFederatedBackend.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Generator
from unittest.mock import MagicMock, patch

import pytest
import torch
import numpy as np
import httpx

# Ensure FLAIR_CONFIG_DIR is set immediately so imports never attempt to write to ~/.flair
if "FLAIR_CONFIG_DIR" not in os.environ:
    _default_test_cfg = Path(tempfile.gettempdir()) / "flair_pytest_default_config"
    _default_test_cfg.mkdir(parents=True, exist_ok=True)
    os.environ["FLAIR_CONFIG_DIR"] = str(_default_test_cfg)


def make_mock_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode("utf-8").rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
    return f"{header}.{body}.mock_signature"


class FakeLoop(asyncio.AbstractEventLoop):
    """
    Lightweight EventLoop stub that avoids Windows ProactorEventLoop / socketpair hangs
    during sandboxed pytest execution.
    """
    def __init__(self):
        self._is_closed = False

    def run_until_complete(self, coro):
        if hasattr(coro, "close"):
            try:
                coro.close()
            except Exception:
                pass

    def is_closed(self):
        return self._is_closed

    def close(self):
        self._is_closed = True


@pytest.fixture
def multi_actor_env(tmp_path: Path) -> Generator[Dict[str, Any], None, None]:
    """
    Creates an isolated multi-actor environment for Actor A (admin) and Actor B (contributor).
    Provides separate working directories and separate config directories.
    """
    actor_a_dir = tmp_path / "workspace_actor_a"
    actor_a_config = tmp_path / "config_actor_a"
    actor_b_dir = tmp_path / "workspace_actor_b"
    actor_b_config = tmp_path / "config_actor_b"

    actor_a_dir.mkdir(parents=True, exist_ok=True)
    actor_a_config.mkdir(parents=True, exist_ok=True)
    actor_b_dir.mkdir(parents=True, exist_ok=True)
    actor_b_config.mkdir(parents=True, exist_ok=True)

    # Actor A Session (Admin)
    session_a = {
        "token": "mock-jwt-admin-token-actor-a",
        "principal": "ActorAAdminWalletAddress11111111111111111111",
        "wallet_address": "ActorAAdminWalletAddress11111111111111111111",
        "expires_at": "2099-01-01T00:00:00Z"
    }
    (actor_a_config / "session.json").write_text(json.dumps(session_a), encoding="utf-8")

    # Actor B Session (Contributor)
    session_b = {
        "token": "mock-jwt-contributor-token-actor-b",
        "principal": "ActorBContributorWalletAddress22222222222222",
        "wallet_address": "ActorBContributorWalletAddress22222222222222",
        "expires_at": "2099-01-01T00:00:00Z"
    }
    (actor_b_config / "session.json").write_text(json.dumps(session_b), encoding="utf-8")

    original_env = os.environ.get("FLAIR_CONFIG_DIR")
    original_cwd = os.getcwd()

    env_data = {
        "actor_a": {
            "cwd": actor_a_dir,
            "config": actor_a_config,
            "wallet": session_a["wallet_address"],
            "token": session_a["token"],
            "role": "admin"
        },
        "actor_b": {
            "cwd": actor_b_dir,
            "config": actor_b_config,
            "wallet": session_b["wallet_address"],
            "token": session_b["token"],
            "role": "contributor"
        },
    }

    try:
        yield env_data
    finally:
        os.chdir(original_cwd)
        if original_env is not None:
            os.environ["FLAIR_CONFIG_DIR"] = original_env
        else:
            os.environ.pop("FLAIR_CONFIG_DIR", None)


@pytest.fixture
def mock_ssh_agent():
    """
    Mocks SSH agent keys and signing so tests do not require an active ssh-agent process.
    """
    mock_identity = MagicMock()
    mock_identity.fingerprint = "SHA256:mockfingerprint123456789"
    mock_identity.public_key_openssh = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey"

    with patch("flair_cli.cli.push.load_ssh_agent_identities", return_value=[mock_identity]), \
         patch("flair_cli.cli.push.find_agent_identity_by_fingerprint", return_value=mock_identity), \
         patch("flair_cli.cli.push.sign_canonical_payload", return_value="mock-signature-base64"), \
         patch("flair_cli.cli.auth.load_ssh_agent_identities", return_value=[mock_identity]):
        yield {
            "identity": mock_identity,
            "fingerprint": "SHA256:mockfingerprint123456789",
            "signature": "mock-signature-base64"
        }


@pytest.fixture
def mock_zkp():
    """
    Mocks ZKP proof creation and verification so tests run quickly without requiring EZKL compilation.
    Bypasses asyncio socketpair hangs on Windows.
    """
    fake_loop = FakeLoop()

    def fake_run(coro):
        if hasattr(coro, "close"):
            try:
                coro.close()
            except Exception:
                pass
        try:
            from flair_cli.cli.utils.local_commits import _get_latest_local_commit
            res = _get_latest_local_commit()
            if res:
                _, cdir = res
                (cdir / "proof.zlib").write_bytes(b"dummy_proof_bytes")
                (cdir / "verification_key.zlib").write_bytes(b"dummy_vk_bytes")
                (cdir / "settings.zlib").write_bytes(b"dummy_settings_bytes")
        except Exception:
            pass

    fake_loop.run_until_complete = fake_run

    with patch("flair_cli.cli.zkp.asyncio.new_event_loop", return_value=fake_loop), \
         patch("flair_cli.cli.zkp.asyncio.set_event_loop"), \
         patch("flair_cli.cli.zkp._verify_proof_with_ezkl", return_value=True), \
         patch("flair_cli.cli.zkp._compute_cid_v1_raw", return_value="bafybeidummymockcid"), \
         patch("flair_cli.cli.zkp._convert_to_onnx", side_effect=lambda p, f: p.with_suffix(".onnx")):
        yield


@pytest.fixture
def dummy_pytorch_model():
    """
    Creates a simple PyTorch model file for testing.
    """
    class SimpleNet(torch.nn.Module):
        def __init__(self):
            super().__init__()
            self.linear = torch.nn.Linear(4, 2)

        def forward(self, x):
            return self.linear(x)

    def _creator(path: Path):
        model = SimpleNet()
        torch.save(model.state_dict(), path)
        return path

    return _creator


@pytest.fixture
def cli_runner():
    from typer.testing import CliRunner
    return CliRunner()


class MockFederatedBackend:
    """Stateful in-memory mock backend capturing multi-actor repository state."""

    def __init__(self, owner_address: str = "ActorAAdminWalletAddress11111111111111111111"):
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
        self.commits: dict[str, dict] = {}
        self.keys = [{"fingerprint": "SHA256:mockfingerprint123456789", "publicKey": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey"}]

    def create_repo(self, payload):
        self.repo_name = payload.get("name", self.repo_name)
        return {
            "id": self.repo_id,
            "hash": self.repo_hash,
            "repoHash": self.repo_hash,
            "name": self.repo_name,
            "ownerAddress": self.owner_address,
            "adminIds": self.admin_ids,
            "metadata": payload.get("metadata", {"framework": "PyTorch"})
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

    def delete_base_model(self, repo_hash):
        self.base_model_cid = None
        self.base_model_url = None
        return True

    def get_branch_by_name(self, repo_hash, branch_name):
        return self.branches.get(branch_name, {
            "id": f"branch-{branch_name}-id",
            "branchId": f"branch-{branch_name}-id",
            "branchHash": f"branch-{branch_name}-hash",
            "name": branch_name,
            "commits": []
        })

    def create_branch(self, repo_hash, name, parent_hash=None, description=""):
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
                "baseModel": {"uri": self.base_model_url, "cid": self.base_model_cid, "extension": ".pt"} if self.base_model_url else None
            },
            "branches": [
                {
                    "name": bname,
                    "hash": b["branchHash"],
                    "branchHash": b["branchHash"],
                    "isDefault": (bname == "main"),
                    "latestCommit": latest_commit_dict if bname == "main" else None
                }
                for bname, b in self.branches.items()
            ]
        }

    def handle_delete(self, url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        url_str = str(url)
        if "basemodel/delete" in url_str:
            self.base_model_cid = None
            self.base_model_url = None
            resp.json.return_value = {"data": "deleted-cid"}
        elif "branch/hash" in url_str:
            resp.json.return_value = {"data": "deleted"}
        else:
            resp.json.return_value = {"status": "SUCCESS"}
        return resp

    def handle_post(self, url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        url_str = str(url)

        if "commit/create/initiate" in url_str:
            resp.json.return_value = {
                "sessionId": "mock-session-123",
                "initiateToken": make_mock_jwt({"jti": "mock-jti-nonce-987", "sub": "test-user"})
            }
        elif "commit/create/zkml-check" in url_str:
            resp.json.return_value = {"unique": True, "zkmlToken": "mock-zkml-token-555"}
        elif "commit/create/zkml-upload" in url_str:
            resp.json.return_value = {"zkmlReceiptToken": "mock-zkml-receipt-token-111"}
        elif "commit/create/params-upload" in url_str:
            resp.json.return_value = {
                "paramsReceiptToken": "mock-params-receipt-token-222",
                "paramsIpfsId": "QmMockParamsIpfsCid333"
            }
        elif "commit/create/finalize" in url_str:
            data = kwargs.get("json", {})
            commit_hash = f"commit-hash-{len(self.commits) + 1}"
            self.commits[commit_hash] = data
            self.branches["main"]["commits"].append(commit_hash)
            resp.json.return_value = {
                "data": {
                    "status": "APPROVED",
                    "commitHash": commit_hash
                }
            }
        elif "basemodel/upload" in url_str:
            self.base_model_cid = "QmNewBaseModelCid"
            self.base_model_url = "https://flair.mock/basemodel.pt"
            resp.json.return_value = {
                "data": {
                    "cid": self.base_model_cid,
                    "fileExtension": ".pt",
                    "url": self.base_model_url
                }
            }
        elif "branch/create" in url_str:
            json_body = kwargs.get("json", {})
            bname = json_body.get("name", "new-branch")
            branch_data = self.create_branch(self.repo_hash, bname)
            resp.json.return_value = {"data": branch_data}
        elif "api/user/keys" in url_str:
            resp.json.return_value = {"status": "SUCCESS"}
        else:
            resp.json.return_value = {"status": "SUCCESS"}
        return resp

    def handle_get(self, url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        url_str = str(url)

        if "api/user/keys" in url_str:
            resp.json.return_value = {"data": self.keys}
        elif "commit/status/sync" in url_str:
            resp.json.return_value = {
                "data": [{"commitHash": h, "status": "APPROVED"} for h in self.branches["main"]["commits"]]
            }
        elif "commit/latest" in url_str:
            latest = self.branches["main"]["commits"][-1] if self.branches["main"]["commits"] else None
            resp.json.return_value = {"data": {"commitHash": latest}}
        elif "basemodel/fetch_url" in url_str:
            if not self.base_model_url:
                err_resp = MagicMock()
                err_resp.status_code = 400
                err_resp.raise_for_status.side_effect = httpx.HTTPStatusError("No base model", request=MagicMock(), response=err_resp)
                return err_resp
            resp.json.return_value = {"data": self.base_model_url, "fileExtension": ".pt"}
        elif "clone" in url_str:
            resp.json.return_value = {"data": self.clone_repository(self.repo_hash)}
        elif "/branch/name/" in url_str:
            bname = url_str.split("/branch/name/")[-1]
            resp.json.return_value = {"data": self.get_branch_by_name(self.repo_hash, bname)}
        elif "/branch" in url_str:
            resp.json.return_value = {"data": self.get_branches(self.repo_hash)}
        elif "/repo/hash/" in url_str:
            resp.json.return_value = {
                "data": {
                    "hash": self.repo_hash,
                    "id": self.repo_id,
                    "name": self.repo_name,
                    "ownerAddress": self.owner_address,
                    "adminIds": [self.owner_address]
                }
            }
        else:
            resp.json.return_value = {"data": {}}
        return resp


@pytest.fixture
def mock_backend():
    """Provides a configured MockFederatedBackend instance with client patches."""
    backend = MockFederatedBackend()
    mock_http_client = MagicMock()
    mock_http_client.__enter__.return_value = mock_http_client
    mock_http_client.post.side_effect = backend.handle_post
    mock_http_client.get.side_effect = backend.handle_get
    mock_http_client.delete.side_effect = backend.handle_delete

    def fake_download(url, target_path):
        target = Path(target_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"dummy_bytes")

    with patch("flair_cli.cli.push._client_with_auth", return_value=mock_http_client), \
         patch("flair_cli.cli.pull._client_with_auth", return_value=mock_http_client), \
         patch("flair_cli.api.utils._client_with_auth", return_value=mock_http_client), \
         patch("flair_cli.api.basemodel._client_with_auth", return_value=mock_http_client), \
         patch("flair_cli.api.repo._client_with_auth", return_value=mock_http_client), \
         patch("flair_cli.api.branch._client_with_auth", return_value=mock_http_client), \
         patch("flair_cli.api.commit._client_with_auth", return_value=mock_http_client), \
         patch("flair_cli.cli.clone._download_file", side_effect=fake_download), \
         patch("flair_cli.cli.branch._download_file", side_effect=fake_download):
        yield backend
