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

    called_urls = []

    def mock_push_manifest():
        return {
            "pushSessionId": "sess-uuid-777",
            "status": "NEGOTIATED",
            "branchHash": "b-hash",
            "upToDate": False,
            "existingCommitHashMap": {},
            "missingCommits": [
                {
                    "localCommitHash": local_commit_uuid,
                    "needsParams": True,
                    "needsZkp": False,
                    "sessionId": "sess-uuid-777-commit",
                    "initiateToken": make_mock_jwt({"jti": "nonce-jti-123", "sub": "test_actor"}),
                    "zkmlReceiptToken": "mock-zkml-receipt-receipt"
                }
            ]
        }

    def mock_push_finalize():
        return {
            "success": True,
            "branchHead": authoritative_sha,
            "commitHashMap": {
                local_commit_uuid: authoritative_sha
            },
            "verificationStatus": "PENDING"
        }

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value.status_code = 200
    mock_client.get.return_value.json.return_value = {
        "data": [{"fingerprint": "SHA256:mockfingerprint123456789"}]
    }

    def mock_post(url, *args, **kwargs):
        called_urls.append(str(url))
        resp = MagicMock()
        resp.status_code = 200
        url_str = str(url)
        if "push/manifest" in url_str:
            manifest_captured.append(kwargs["json"])
            resp.json.return_value = {"data": mock_push_manifest()}
        elif "push/finalize" in url_str:
            finalize_captured.append(kwargs["json"])
            resp.json.return_value = {"data": mock_push_finalize()}
        elif "commit/create/params-upload" in url_str:
            resp.json.return_value = {"paramsReceiptToken": "pr-1", "paramsIpfsId": "Qm1"}
        elif "commit/create/zkml-upload" in url_str:
            resp.json.return_value = {"zkmlReceiptToken": "zr-1"}
        else:
            resp.json.return_value = {}
        return resp

    mock_client.post.side_effect = mock_post

    with patch("flair_cli.cli.push._client_with_auth", return_value=mock_client):

        res_push = cli_runner.invoke(app, ["push"])
        assert res_push.exit_code == 0, f"Push failed: {res_push.output}"
        assert "Push complete!" in res_push.output

    # 1. Assert Push Manifest was built and sent
    assert len(manifest_captured) == 1
    manifest = manifest_captured[0]
    assert manifest["targetBranch"]["name"] == "main"
    assert len(manifest["commits"]) == 1
    assert manifest["commits"][0]["localCommitHash"] == local_commit_uuid

    # 2. Assert zero legacy per-commit calls: NO initiate, NO zkml-check, NO commit/create/finalize
    assert not any("commit/create/initiate" in u for u in called_urls)
    assert not any("commit/create/zkml-check" in u for u in called_urls)
    assert not any("commit/create/finalize" in u for u in called_urls)

    # 3. Assert Batch Finalize payload contains signed envelopes
    assert len(finalize_captured) == 1
    finalize_payload = finalize_captured[0]
    assert finalize_payload["pushSessionId"] == "sess-uuid-777"
    assert "commits" in finalize_payload
    assert len(finalize_payload["commits"]) == 1
    envelope = finalize_payload["commits"][0]
    assert envelope["localCommitHash"] == local_commit_uuid
    assert envelope["commitSignature"]
    assert envelope["sshKeyFingerprint"]
    assert envelope["paramsReceiptToken"] == "pr-1"

    # 4. Assert Commit Hash Reconciliation was applied
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


def test_push_terminal_handshake_when_up_to_date(
    multi_actor_env,
    mock_ssh_agent,
    mock_zkp,
    dummy_pytorch_model,
    cli_runner,
):
    """When upToDate is true, push reconciles from existingCommitHashMap and exits without calling finalize."""
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    cli_runner.invoke(app, ["init", "--name", "uptodate-test-repo", "--skip-base-model"])
    _create_staged_commit(actor_a["cwd"], dummy_pytorch_model, cli_runner)

    flair_dir = actor_a["cwd"] / ".flair"
    from flair_cli.cli.utils.local_commits import _get_latest_local_commit
    commit_data, _ = _get_latest_local_commit()
    local_uuid = commit_data["commitHash"]

    authoritative_sha = "f2d4e6a8b1c3d5e7f9a1b3c5d7e9f1a3b5c7d9e1f3a5b7c9d1e3f5a7b9c1d3e5"
    finalize_called = []

    def mock_push_manifest():
        return {
            "pushSessionId": "sess-up-to-date",
            "status": "NEGOTIATED",
            "branchHash": "branch-main-hash",
            "upToDate": True,
            "existingCommitHashMap": {
                local_uuid: authoritative_sha
            },
            "missingCommits": []
        }

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value.status_code = 200
    mock_client.get.return_value.json.return_value = {
        "data": [{"fingerprint": "SHA256:mockfingerprint123456789"}]
    }

    def mock_post(url, *args, **kwargs):
        response = MagicMock()
        response.status_code = 200
        if "push/manifest" in str(url):
            response.json.return_value = {"data": mock_push_manifest()}
        else:
            finalize_called.append(kwargs.get("json"))
            response.json.return_value = {"data": {"success": True}}
        return response

    mock_client.post.side_effect = mock_post

    with patch("flair_cli.cli.push._client_with_auth", return_value=mock_client):

        res_push = cli_runner.invoke(app, ["push"])
        assert res_push.exit_code == 0
        assert "up to date" in res_push.output.lower()

    # Finalize must NEVER be called when upToDate is True
    assert len(finalize_called) == 0

    # Local commit must be reconciled from existingCommitHashMap
    local_commits_dir = flair_dir / ".local_commits"
    assert (local_commits_dir / authoritative_sha).exists()


def test_push_fork_lineage_resolution(
    multi_actor_env,
    mock_ssh_agent,
    mock_zkp,
    dummy_pytorch_model,
    cli_runner,
):
    """When pushing a fork branch, parentBranchHash is read from branches.json rather than HEAD."""
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    cli_runner.invoke(app, ["init", "--name", "fork-test-repo", "--skip-base-model"])
    
    # Create feature branch diverged from main
    res_br = cli_runner.invoke(app, ["branch", "feature-x"])
    assert res_br.exit_code == 0
    res_co = cli_runner.invoke(app, ["checkout", "feature-x"])
    assert res_co.exit_code == 0

    flair_dir = actor_a["cwd"] / ".flair"
    with open(flair_dir / "branches.json") as f:
        branches = json.load(f)
    main_rec = next(b for b in branches if b["name"] == "main")
    feat_rec = next(b for b in branches if b["name"] == "feature-x")
    assert feat_rec["parentBranchHash"] == main_rec["branchHash"]

    # Stage commit on feature-x
    _create_staged_commit(actor_a["cwd"], dummy_pytorch_model, cli_runner)

    manifest_captured = []
    def mock_push_manifest():
        return {
            "pushSessionId": "sess-fork",
            "status": "NEGOTIATED",
            "branchHash": feat_rec["branchHash"],
            "upToDate": True,
            "existingCommitHashMap": {},
            "missingCommits": []
        }

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value.status_code = 200
    mock_client.get.return_value.json.return_value = {
        "data": [{"fingerprint": "SHA256:mockfingerprint123456789"}]
    }

    def mock_post(url, *args, **kwargs):
        response = MagicMock()
        response.status_code = 200
        if "push/manifest" in str(url):
            manifest_captured.append(kwargs["json"])
            response.json.return_value = {"data": mock_push_manifest()}
        else:
            response.json.return_value = {"data": {}}
        return response

    mock_client.post.side_effect = mock_post

    with patch("flair_cli.cli.push._client_with_auth", return_value=mock_client):

        res_push = cli_runner.invoke(app, ["push", "feature-x"])
        assert res_push.exit_code == 0, f"Push failed: {res_push.output}"

    assert len(manifest_captured) == 1
    target_br = manifest_captured[0]["targetBranch"]
    assert target_br["name"] == "feature-x"
    assert target_br["branchHash"] == feat_rec["branchHash"]
    # Must send main's branchHash as parentBranchHash, NOT self-referencing
    assert target_br["parentBranchHash"] == main_rec["branchHash"]
    assert target_br["parentBranchHash"] != target_br["branchHash"]


def test_seqpush_legacy_flow(
    multi_actor_env,
    mock_ssh_agent,
    mock_zkp,
    dummy_pytorch_model,
    cli_runner,
):
    """flair seqpush maintains backward compatibility using legacy 5-step per-commit calls."""
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    cli_runner.invoke(app, ["init", "--name", "seqpush-test-repo", "--skip-base-model"])
    _create_staged_commit(actor_a["cwd"], dummy_pytorch_model, cli_runner)

    flair_dir = actor_a["cwd"] / ".flair"
    from flair_cli.cli.utils.local_commits import _get_latest_local_commit
    commit_data, _ = _get_latest_local_commit()
    local_uuid = commit_data["commitHash"]

    seq_authoritative_sha = "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2"
    called_endpoints = []

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value.status_code = 200
    mock_client.get.return_value.json.return_value = {
        "data": [{"fingerprint": "SHA256:mockfingerprint123456789"}]
    }

    def mock_post(url, *args, **kwargs):
        called_endpoints.append(str(url))
        resp = MagicMock()
        resp.status_code = 200
        url_str = str(url)
        legacy_url_str = url_str.replace("/commit/seqpush/create/", "/commit/create/")
        if "commit/create/initiate" in legacy_url_str:
            resp.json.return_value = {"sessionId": "s-seq-1", "initiateToken": make_mock_jwt({"jti": "jti-seq-1"})}
        elif "commit/create/zkml-check" in legacy_url_str:
            resp.json.return_value = {"unique": True, "zkmlToken": "z-seq-1"}
        elif "commit/create/zkml-upload" in legacy_url_str:
            resp.json.return_value = {"zkmlReceiptToken": "zr-seq-1"}
        elif "commit/create/params-upload" in legacy_url_str:
            resp.json.return_value = {"paramsReceiptToken": "pr-seq-1", "paramsIpfsId": "QmSeq1"}
        elif "commit/create/finalize" in legacy_url_str:
            resp.json.return_value = {"data": {"status": "APPROVED", "commitHash": seq_authoritative_sha}}
        else:
            resp.json.return_value = {}
        return resp

    mock_client.post.side_effect = mock_post

    with patch("flair_cli.cli.push._check_remote_repo_exists", return_value=True), \
         patch("flair_cli.cli.push.api_client.get_branch_by_name", return_value={"branchHash": "b-seq-hash", "id": "b-seq-id"}), \
         patch("flair_cli.cli.push._get_remote_latest_commit", return_value=None), \
         patch("flair_cli.cli.push._client_with_auth", return_value=mock_client):

        res_seq = cli_runner.invoke(app, ["seqpush"])
        assert res_seq.exit_code == 0, f"seqpush failed: {res_seq.output}"
        assert "Sequential push complete!" in res_seq.output

    # Assert seqpush uses the legacy 5-step commit creation route family.
    seqpush_endpoints = (
        "commit/create/initiate",
        "commit/create/zkml-check",
        "commit/create/zkml-upload",
        "commit/create/params-upload",
        "commit/create/finalize",
    )
    for endpoint in seqpush_endpoints:
        assert any(endpoint in url for url in called_endpoints)
