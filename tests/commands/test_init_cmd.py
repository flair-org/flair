"""
Tier 1 Command Test: flair init and request timeout handling
Asserts timeout resolution, init unauthenticated check, and timeout error handling.
"""
import os
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


def test_init_timeout_handling(tmp_path, cli_runner):
    """flair init should display helpful advice on httpx.TimeoutException."""
    os.environ["FLAIR_CONFIG_DIR"] = str(tmp_path)
    os.chdir(tmp_path)

    # Create dummy session
    session = Session(token="mock_token", principal="test_user", wallet_address="test_wallet", expires_at=None)
    save_session(session)

    with patch("flair_cli.api.client.create_repo", side_effect=httpx.ReadTimeout("The read operation timed out")):
        res = cli_runner.invoke(app, ["init", "--name", "timeout-repo"])
        assert res.exit_code == 1
        assert "Request timed out" in res.output
        assert "FLAIR_REQUEST_TIMEOUT" in res.output
