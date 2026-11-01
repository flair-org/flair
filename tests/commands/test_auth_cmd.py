"""
Tier 1 Command Test: flair auth
Asserts auth status, logout, login callback simulation, and ssh integration.
"""
import json
import os
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from flair_cli.main import app
from flair_cli.cli.auth import CallbackHandler
from tests.conftest import make_mock_jwt

pytestmark = pytest.mark.command


def test_auth_status_authenticated(multi_actor_env, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    res = cli_runner.invoke(app, ["auth", "status"])
    assert res.exit_code == 0
    assert "ActorAAdminWalletAddress" in res.output or "Authenticated" in res.output or "Principal" in res.output


def test_auth_logout(multi_actor_env, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    res_logout = cli_runner.invoke(app, ["auth", "logout"])
    assert res_logout.exit_code == 0
    assert "logged out" in res_logout.output.lower()

    # Session file removed or cleared
    res_status = cli_runner.invoke(app, ["auth", "status"])
    assert res_status.exit_code in (0, 1, 2)
    assert "not logged in" in res_status.output.lower() or "not authenticated" in res_status.output.lower() or "no active" in res_status.output.lower()


def test_auth_login_with_mock_callback(tmp_path, cli_runner):
    cfg_dir = tmp_path / "login_test_cfg"
    cfg_dir.mkdir()
    os.environ["FLAIR_CONFIG_DIR"] = str(cfg_dir)

    def trigger_callback():
        import time
        time.sleep(0.1)
        CallbackHandler.token = "mock-login-token-12345"
        CallbackHandler.principal = "MockLoginUserWallet123"
        CallbackHandler.wallet = "MockLoginUserWallet123"

    timer = threading.Thread(target=trigger_callback, daemon=True)
    timer.start()

    with patch("flair_cli.cli.auth.webbrowser.open"):
        res = cli_runner.invoke(app, ["auth", "login", "--no-browser", "--force"])
        assert res.exit_code == 0
        assert "login successful" in res.output.lower() or "principal" in res.output.lower()


def test_auth_ssh_env(multi_actor_env, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    # PowerShell output
    res_ps = cli_runner.invoke(app, ["auth", "ssh", "env", "--shell", "powershell"])
    assert res_ps.exit_code == 0
    assert "ssh-agent" in res_ps.output.lower() or "ssh-add" in res_ps.output.lower()

    # Bash output
    res_bash = cli_runner.invoke(app, ["auth", "ssh", "env", "--shell", "bash"])
    assert res_bash.exit_code == 0
    assert "ssh-agent" in res_bash.output.lower() or "ssh-add" in res_bash.output.lower()


def test_auth_ssh_status(multi_actor_env, mock_ssh_agent, tmp_path, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    mock_client = MagicMock()
    mock_client.__enter__.return_value = mock_client
    mock_client.get.return_value.status_code = 200
    mock_client.get.return_value.json.return_value = {
        "data": [{"fingerprint": mock_ssh_agent["fingerprint"]}]
    }

    dummy_key = tmp_path / "test_id_ed25519"
    dummy_key.write_text("dummy-private-key")

    with patch("flair_cli.cli.auth._client_with_auth", return_value=mock_client), \
         patch("flair_cli.cli.auth._resolve_ssh_key_path", return_value=dummy_key):
        res = cli_runner.invoke(app, ["auth", "ssh", "status"])
        assert res.exit_code == 0
        assert "ssh" in res.output.lower()
