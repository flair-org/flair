"""
Pytest fixtures for Federated Learning CLI tests.
Includes multi-actor environment isolation, mocked auth/session,
mocked SSH agent identities, mocked ZKP (EZKL), and mocked API backend.
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Generator
from unittest.mock import MagicMock, patch

import pytest
import torch
import numpy as np


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


import base64

def make_mock_jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode("utf-8").rstrip("=")
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8").rstrip("=")
    return f"{header}.{body}.mock_signature"


@pytest.fixture
def mock_ssh_agent():
    """
    Mocks SSH agent keys and signing so tests do not require an active ssh-agent process.
    Also handles 5-step commit creation HTTP endpoints.
    """
    mock_identity = MagicMock()
    mock_identity.fingerprint = "SHA256:mockfingerprint123456789"
    mock_identity.public_key_openssh = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMockKey"

    with patch("flair_cli.cli.push.load_ssh_agent_identities", return_value=[mock_identity]), \
         patch("flair_cli.cli.push.find_agent_identity_by_fingerprint", return_value=mock_identity), \
         patch("flair_cli.cli.push.sign_canonical_payload", return_value="mock-signature-base64"), \
         patch("flair_cli.cli.push._client_with_auth") as mock_client_auth, \
         patch("flair_cli.cli.pull._client_with_auth") as mock_pull_client_auth:
        
        mock_http_client = MagicMock()

        def fake_get(url, *args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "api/user/keys" in str(url):
                resp.json.return_value = {
                    "data": [{"fingerprint": "SHA256:mockfingerprint123456789"}]
                }
            elif "commit/status/sync" in str(url):
                resp.json.return_value = {"data": []}
            elif "commit/latest" in str(url):
                resp.json.return_value = {"data": {"commitHash": None}}
            else:
                resp.json.return_value = {"data": {}}
            return resp

        def fake_post(url, *args, **kwargs):
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
                resp.json.return_value = {
                    "data": {
                        "status": "APPROVED",
                        "commitHash": "mock-final-commit-hash-444"
                    }
                }

            else:
                resp.json.return_value = {"status": "SUCCESS"}
            return resp

        mock_http_client.__enter__.return_value = mock_http_client
        mock_http_client.get.side_effect = fake_get
        mock_http_client.post.side_effect = fake_post
        mock_client_auth.return_value = mock_http_client
        mock_pull_client_auth.return_value = mock_http_client

        yield {
            "identity": mock_identity,
            "fingerprint": "SHA256:mockfingerprint123456789",
            "signature": "mock-signature-base64"
        }



@pytest.fixture
def mock_zkp():
    """
    Mocks ZKP proof creation and verification so tests run quickly without requiring EZKL compilation.
    """
    async def fake_process_model_with_ezkl(onnx_file, dims, backend, commit_dir):
        commit_dir = Path(commit_dir)
        (commit_dir / "proof.zlib").write_bytes(b"dummy_proof_bytes")
        (commit_dir / "verification_key.zlib").write_bytes(b"dummy_vk_bytes")
        (commit_dir / "settings.zlib").write_bytes(b"dummy_settings_bytes")

    with patch("flair_cli.cli.zkp._process_model_with_ezkl", side_effect=fake_process_model_with_ezkl), \
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
