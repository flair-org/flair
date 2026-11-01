"""
Tier 1 Command Test: flair config
Asserts config view, set, reset, and validation rules.
"""
import os
from pathlib import Path
import pytest
from flair_cli.main import app

pytestmark = pytest.mark.command


def test_config_view(multi_actor_env, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    res = cli_runner.invoke(app, ["config", "view"])
    assert res.exit_code == 0
    assert "api_base_url" in res.output
    assert "auth_url" in res.output
    assert "session_timeout_hours" in res.output


def test_config_set_and_view(multi_actor_env, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    # Set custom auth URL and session timeout
    res_set = cli_runner.invoke(app, [
        "config", "set",
        "--auth-url", "https://custom-auth.flair/signin",
        "--session-timeout-hours", "72"
    ])
    assert res_set.exit_code == 0

    # View updated config
    res_view = cli_runner.invoke(app, ["config", "view"])
    assert res_view.exit_code == 0
    assert "https://custom-auth.flair/signin" in res_view.output
    assert "72" in res_view.output


def test_config_reset(multi_actor_env, cli_runner):
    actor_a = multi_actor_env["actor_a"]
    os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
    os.chdir(actor_a["cwd"])

    res_reset = cli_runner.invoke(app, ["config", "reset"])
    assert res_reset.exit_code == 0
    assert "reset to defaults" in res_reset.output.lower()
