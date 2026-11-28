"""
Tier 1 Command Test: flair init and offline repository initialization.
Asserts timeout resolution, init unauthenticated check, and offline UUID v4 generation.
"""
import os
import json
import uuid
from pathlib import Path
from unittest.mock import patch
import pytest
import httpx
from flair_cli.main import app
from flair_cli.api.utils import _get_timeout
from flair_cli.core.session import Session, save_session

pytestmark = pytest.mark.command


def test_timeout_resolution():
    """Verify FLAIR_REQUEST_TIMEOUT and FLAIR_TIMEOUT env vars."""
    orig_req = os.environ.get("FLAIR_REQUEST_TIMEOUT")
    orig_fallback = os.environ.get("FLAIR_TIMEOUT")

    try:
        os.environ.pop("FLAIR_REQUEST_TIMEOUT", None)
        os.environ.pop("FLAIR_TIMEOUT", None)
        assert _get_timeout() == 30.0

        os.environ["FLAIR_REQUEST_TIMEOUT"] = "120"
        assert _get_timeout() == 120.0

        os.environ.pop("FLAIR_REQUEST_TIMEOUT", None)
        os.environ["FLAIR_TIMEOUT"] = "60"
        assert _get_timeout() == 60.0

        # Invalid numeric fallback to 30.0
        os.environ["FLAIR_TIMEOUT"] = "invalid"
        assert _get_timeout() == 30.0
    finally:
        if orig_req is not None:
            os.environ["FLAIR_REQUEST_TIMEOUT"] = orig_req
        else:
            os.environ.pop("FLAIR_REQUEST_TIMEOUT", None)
        if orig_fallback is not None:
            os.environ["FLAIR_TIMEOUT"] = orig_fallback
        else:
            os.environ.pop("FLAIR_TIMEOUT", None)


def test_init_unauthenticated(tmp_path, cli_runner):
    """flair init should exit with code 1 and prompt to log in when unauthenticated."""
    os.environ["FLAIR_CONFIG_DIR"] = str(tmp_path)
    os.chdir(tmp_path)

    res = cli_runner.invoke(app, ["init", "--name", "test-repo"])
    assert res.exit_code == 1
    assert "Not authenticated. Run 'flair auth login' first." in res.output


def test_init_offline_uuid_generation(tmp_path, cli_runner):
    """flair init should initialize locally without contacting backend and generate UUID v4 hashes."""
    os.environ["FLAIR_CONFIG_DIR"] = str(tmp_path)
    os.chdir(tmp_path)

    # Create dummy session
    session = Session(token="mock_token", principal="test_user", wallet_address="test_wallet", expires_at=None)
    save_session(session)

    with patch("flair_cli.api.client.create_repo") as mock_create_repo:
        res = cli_runner.invoke(app, ["init", "--name", "offline-repo"])
        assert res.exit_code == 0
        assert "Repository initialized successfully" in res.output
        mock_create_repo.assert_not_called()

    flair_dir = tmp_path / ".flair"
    assert (flair_dir / "repo.json").exists()
    assert (flair_dir / "HEAD").exists()
    assert (flair_dir / "branches.json").exists()

    with open(flair_dir / "repo.json") as f:
        repo_data = json.load(f)
    assert repo_data["name"] == "offline-repo"
    # Ensure repoHash is a valid UUID
    uuid.UUID(repo_data["repoHash"])
    assert repo_data["remote"] is None

    with open(flair_dir / "HEAD") as f:
        head_data = json.load(f)
    assert head_data["currentBranch"] == "main"
    uuid.UUID(head_data["branchHash"])

    with open(flair_dir / "branches.json") as f:
        branches_data = json.load(f)
    assert len(branches_data) == 1
    assert branches_data[0]["name"] == "main"
    assert branches_data[0]["branchHash"] == head_data["branchHash"]
