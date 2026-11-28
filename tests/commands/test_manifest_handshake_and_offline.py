"""
Tests for Push Manifest Handshake and Local-First Offline Workspace Architecture.

Validates:
1. Offline 'flair init' with local UUID v4 identities and zero network requests.
2. Offline 'flair branch' with local UUID v4 identities.
3. 'flair push' unlinked/non-existent remote repository error prompt.
4. 'flair push' manifest handshake negotiation and journaled commit hash reconciliation.
"""
import json
import os
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from flair_cli.main import app
from flair_cli.core.session import Session, save_session
from flair_cli.cli.utils.commit_reconciliation import (
    apply_commit_hash_reconciliation,
    get_journal_path,
    recover_pending_reconciliation_if_needed,
)
from tests.conftest import make_mock_jwt

pytestmark = pytest.mark.command


def _create_staged_commit(cwd: Path, dummy_pytorch_model, cli_runner):
    model_path = cwd / "model.pt"
    dummy_pytorch_model(model_path)
    res_add = cli_runner.invoke(app, ["add"])
    assert res_add.exit_code == 0
    res_params = cli_runner.invoke(app, ["params", "create", "--model", "model.pt", "--classes", "cat,dog"])
    assert res_params.exit_code == 0
    res_zkp = cli_runner.invoke(app, ["zkp", "create", "--model", "model.pt"])
    assert res_zkp.exit_code == 0
    res_commit = cli_runner.invoke(app, ["commit", "-m", "Local-first test commit"])
    assert res_commit.exit_code == 0


def test_init_and_branch_completely_offline(tmp_path, cli_runner):
    """flair init and flair branch must operate 100% offline with zero HTTP calls."""
    os.environ["FLAIR_CONFIG_DIR"] = str(tmp_path)
    os.chdir(tmp_path)

    session = Session(token="mock_tok", principal="test_actor", wallet_address="test_actor", expires_at=None)
    save_session(session)

    with patch("flair_cli.api.client.create_repo") as mock_create_repo, \
         patch("flair_cli.api.client.create_branch") as mock_create_branch:
        # 1. Initialize offline
        res_init = cli_runner.invoke(app, ["init", "--name", "offline-workspace", "--skip-base-model"])
        assert res_init.exit_code == 0
        assert "Repository initialized successfully" in res_init.output
        mock_create_repo.assert_not_called()

        flair_dir = tmp_path / ".flair"
        with open(flair_dir / "repo.json") as f:
            repo_data = json.load(f)
        repo_uuid = repo_data["repoHash"]
        uuid.UUID(repo_uuid)  # Validate UUID v4 format
        assert repo_data["remote"] is None

        # 2. Branch offline
        res_branch = cli_runner.invoke(app, ["branch", "experiment-offline"])
        assert res_branch.exit_code == 0
        assert "created" in res_branch.output.lower()
        mock_create_branch.assert_not_called()

        with open(flair_dir / "branches.json") as f:
            branches = json.load(f)
        exp_branch = next((b for b in branches if b["name"] == "experiment-offline"), None)
        assert exp_branch is not None
        uuid.UUID(exp_branch["branchHash"])  # Validate UUID v4 format


def test_push_halts_when_remote_repo_not_found(tmp_path, mock_zkp, dummy_pytorch_model, cli_runner):
    """flair push must prompt the user to link/add the repository if it does not exist on remote."""
    os.environ["FLAIR_CONFIG_DIR"] = str(tmp_path)
    os.chdir(tmp_path)

    session = Session(token="mock_tok", principal="test_actor", wallet_address="test_actor", expires_at=None)
    save_session(session)

    # Initialize locally and create staged commit
    cli_runner.invoke(app, ["init", "--name", "unlinked-repo", "--skip-base-model"])
    _create_staged_commit(tmp_path, dummy_pytorch_model, cli_runner)

    # Attempt to push to non-existent remote
    with patch("flair_cli.cli.push._check_remote_repo_exists", return_value=False):
        res_push = cli_runner.invoke(app, ["push"])
        assert res_push.exit_code == 1
        assert "not found" in res_push.output.lower()
        assert "flair remote add origin" in res_push.output


def test_push_manifest_handshake_and_reconciliation(
    multi_actor_env,
    mock_ssh_agent,
    mock_zkp,
    dummy_pytorch_model,
    cli_runner,
):
    """Validate push manifest handshake and atomic commit hash reconciliation."""
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    cli_runner.invoke(app, ["init", "--name", "manifest-test-repo", "--skip-base-model"])
    _create_staged_commit(actor_a["cwd"], dummy_pytorch_model, cli_runner)

    flair_dir = actor_a["cwd"] / ".flair"
    from flair_cli.cli.utils.local_commits import _get_latest_local_commit
    latest = _get_latest_local_commit()
    assert latest is not None
    commit_data_before, commit_dir_before = latest
    local_commit_uuid = commit_data_before.get("commitHash")
    # Verify local commit hash is a UUID v4
    uuid.UUID(local_commit_uuid)

    manifest_captured = []
    finalize_captured = []

    authoritative_sha = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def mock_push_manifest(repo_hash, manifest):
        manifest_captured.append(manifest)
        return {
            "pushSessionId": "sess-uuid-777",
            "status": "NEGOTIATED",
            "missingCommits": [
                {
                    "localCommitHash": local_commit_uuid,
                    "needsParams": True,
                    "needsZkp": False
                }
            ]
        }

    def mock_push_finalize(repo_hash, payload):
        finalize_captured.append(payload)
        return {
            "success": True,
            "branchHead": authoritative_sha,
            "commitHashMap": {
                local_commit_uuid: authoritative_sha
            },
            "verificationStatus": "PENDING_VERIFICATION"
        }

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value.status_code = 200
    mock_client.get.return_value.json.return_value = {
        "data": [{"fingerprint": "SHA256:mockfingerprint123456789"}]
    }

    def mock_post(url, *args, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        url_str = str(url)
        if "commit/create/initiate" in url_str:
            resp.json.return_value = {"sessionId": "s-1", "initiateToken": make_mock_jwt({"jti": "j-1"})}
        elif "commit/create/zkml-check" in url_str:
            resp.json.return_value = {"unique": True, "zkmlToken": "z-1"}
        elif "commit/create/zkml-upload" in url_str:
            resp.json.return_value = {"zkmlReceiptToken": "zr-1"}
        elif "commit/create/params-upload" in url_str:
            resp.json.return_value = {"paramsReceiptToken": "pr-1", "paramsIpfsId": "Qm1"}
        elif "commit/create/finalize" in url_str:
            resp.json.return_value = {"data": {"status": "APPROVED", "commitHash": authoritative_sha}}
        else:
            resp.json.return_value = {}
        return resp

    mock_client.post.side_effect = mock_post

    with patch("flair_cli.cli.push.api_client.get_repo_by_hash", return_value={"id": "r-id", "name": "manifest-test-repo"}), \
         patch("flair_cli.cli.push.api_client.get_branch_by_name", return_value={"branchHash": "b-hash", "id": "b-id"}), \
         patch("flair_cli.cli.push._get_remote_latest_commit", return_value=None), \
         patch("flair_cli.cli.push.api_client.push_manifest", side_effect=mock_push_manifest), \
         patch("flair_cli.cli.push.api_client.push_finalize", side_effect=mock_push_finalize), \
         patch("flair_cli.cli.push._client_with_auth", return_value=mock_client):

        res_push = cli_runner.invoke(app, ["push"])
        assert res_push.exit_code == 0, f"Push failed: {res_push.output}"
        assert "Push complete!" in res_push.output

    # 1. Assert Push Manifest was built and sent
    assert len(manifest_captured) == 1
    manifest = manifest_captured[0]
    assert manifest["targetBranch"]["name"] == "main"
    assert len(manifest["commits"]) == 1
    assert manifest["commits"][0]["localCommitHash"] == local_commit_uuid

    # 2. Assert Commit Hash Reconciliation was applied
    with open(flair_dir / "HEAD") as f:
        head_after = json.load(f)
    assert head_after["previousCommit"] == authoritative_sha

    # Folder must be renamed to authoritative SHA-256
    local_commits_dir = flair_dir / ".local_commits"
    assert not (local_commits_dir / local_commit_uuid).exists()
    assert (local_commits_dir / authoritative_sha).exists()

    with open(local_commits_dir / authoritative_sha / "commit.json") as f:
        reconciled_commit = json.load(f)
    assert reconciled_commit["commitHash"] == authoritative_sha
    assert reconciled_commit["localCommitHash"] == local_commit_uuid

    # Journal must be cleaned up
    assert not get_journal_path().exists()
