"""
Test suite for centralized error translation and diagnostics in flair_cli.utils.errors.
Verifies all OpenAPI 3.0 mappings, exit codes, and 3-part layout formatting.
"""

import httpx
import pytest
import typer
from rich.console import Console

from flair_cli.cli.utils.errors import (
    _match_endpoint_category,
    format_api_error,
    handle_cli_exception,
)


def test_match_endpoint_category():
    assert _match_endpoint_category("/api/repo/create") == "repo_create"
    assert _match_endpoint_category("http://localhost:2112/repo/hash/abc/clone") == "repo_clone"
    assert _match_endpoint_category("/api/repo/hash/abc/basemodel/upload") == "basemodel_upload"
    assert _match_endpoint_category("/api/repo/hash/abc/basemodel/fetch_url") == "basemodel_fetch_url"
    assert _match_endpoint_category("/api/repo/hash/abc/basemodel/delete") == "basemodel_delete"
    assert _match_endpoint_category("/api/repo/hash/abc/branch/create") == "branch_create"
    assert _match_endpoint_category("/api/repo/hash/abc/branch/hash/def/delete") == "branch_delete"
    assert _match_endpoint_category("/api/repo/hash/abc/branch/name/main") == "branch_get"
    assert _match_endpoint_category("/api/repo/hash/abc/branch") == "branch_list"
    assert _match_endpoint_category("/api/repo/hash/abc/branch/hash/def/commit/create/initiate") == "commit_initiate"
    assert _match_endpoint_category("/api/repo/hash/abc/branch/hash/def/commit/create/zkml-check") == "commit_zkml_check"
    assert _match_endpoint_category("/api/repo/hash/abc/branch/hash/def/commit/create/zkml-upload") == "commit_zkml_upload"
    assert _match_endpoint_category("/api/repo/hash/abc/branch/hash/def/commit/create/params-upload") == "commit_params_upload"
    assert _match_endpoint_category("/api/repo/hash/abc/branch/hash/def/commit/create/finalize") == "commit_finalize"
    assert _match_endpoint_category("/api/repo/hash/abc/branch/hash/def/commit/status/sync") == "commit_status_sync"
    assert _match_endpoint_category("/api/repo/hash/abc/branch/hash/def/commit/hash/ghi/pull") == "commit_pull"
    assert _match_endpoint_category("/api/user/keys") == "user_keys"


def test_format_api_error_repo_create():
    summary, cause, fix, exit_code = format_api_error(400, "/repo/create")
    assert "400" in summary
    assert "Invalid repository name" in cause
    assert exit_code == 1

    summary, cause, fix, exit_code = format_api_error(401, "/repo/create")
    assert "Authentication required" in cause
    assert exit_code == 2

    summary, cause, fix, exit_code = format_api_error(500, "/repo/create")
    assert "Internal server error" in cause
    assert exit_code == 3


def test_format_api_error_clone():
    summary, cause, fix, exit_code = format_api_error(404, "/repo/hash/r123/clone")
    assert "Repository hash does not exist" in cause
    assert exit_code == 1

    summary, cause, fix, exit_code = format_api_error(500, "/repo/hash/r123/clone")
    assert "Server failed to package" in cause
    assert exit_code == 3


def test_format_api_error_basemodel():
    # 403 Forbidden
    summary, cause, fix, exit_code = format_api_error(403, "/basemodel/upload")
    assert "Permission denied" in cause
    assert exit_code == 2

    # 400 No base model exists
    summary, cause, fix, exit_code = format_api_error(400, "/basemodel/fetch_url")
    assert "No base model exists" in cause
    assert exit_code == 1


def test_format_api_error_commit_conflicts():
    # 409 ZKML proof duplicate
    summary, cause, fix, exit_code = format_api_error(409, "/commit/create/zkml-check")
    assert "Conflict" in cause
    assert "identical ZKML proof" in cause
    assert exit_code == 1

    # 409 Commit collision on finalize
    summary, cause, fix, exit_code = format_api_error(409, "/commit/create/finalize")
    assert "concurrent push collision" in cause
    assert "flair pull" in fix


def test_format_api_error_ssh_keys():
    # 409 SSH key already registered
    summary, cause, fix, exit_code = format_api_error(409, "/api/user/keys")
    assert "already registered" in cause
    assert "flair auth ssh status" in fix


def test_format_api_error_with_backend_detail():
    resp_data = {"error": {"message": "Custom DB timeout at postgres://..."}}
    summary, cause, fix, exit_code = format_api_error(500, "/repo/create", response_data=resp_data)
    assert "Custom DB timeout" in cause


def test_handle_cli_exception_http_error(capsys):
    req = httpx.Request("POST", "http://localhost:2112/api/repo/hash/abc/commit/create/finalize")
    resp = httpx.Response(status_code=403, request=req)
    exc = httpx.HTTPStatusError("Forbidden", request=req, response=resp)

    with pytest.raises(typer.Exit) as exc_info:
        handle_cli_exception(exc, context="Commit finalize")

    assert exc_info.value.exit_code == 2
    captured = capsys.readouterr()
    assert "Commit finalize failed" in captured.out
    assert "Cause:" in captured.out
    assert "Fix:" in captured.out
    assert "SSH key" in captured.out or "verification failed" in captured.out


def test_handle_cli_exception_timeout(capsys):
    exc = httpx.ReadTimeout("Timeout")

    with pytest.raises(typer.Exit) as exc_info:
        handle_cli_exception(exc, context="Repository init", default_exit_code=1)

    assert exc_info.value.exit_code == 1
    captured = capsys.readouterr()
    assert "Request timed out" in captured.out
    assert "FLAIR_REQUEST_TIMEOUT" in captured.out


def test_handle_cli_exception_connection_error(capsys):
    exc = httpx.ConnectError("Connection refused")

    with pytest.raises(typer.Exit) as exc_info:
        handle_cli_exception(exc, context="Sync pull")

    assert exc_info.value.exit_code == 3
    captured = capsys.readouterr()
    assert "Network Error" in captured.out
    assert "FLAIR_API_BASE" in captured.out
