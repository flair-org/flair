"""
Centralized Error Translation & Diagnostics Utility for Flair CLI.

Maps backend OpenAPI endpoints, HTTP status codes, and network exceptions
to clear, diagnostic console explanations:
  1. Summary (What failed)
  2. Root Cause (Why it failed, citing exact OpenAPI backend status & cause)
  3. Actionable Fix (Concrete user instructions to resolve)
"""

from __future__ import annotations

import re
from typing import Any, Optional
import httpx
from rich.console import Console
import typer

from ...api.utils import _base_url

console = Console()

# Mapping of endpoint patterns and HTTP status codes to (cause, fix)
# Derived strictly from FlairHub OpenAPI 3.0.0 specification
_OPENAPI_ERROR_MAP: dict[str, dict[int, tuple[str, str]]] = {
    "repo_create": {
        400: (
            "Invalid repository name or metadata payload.",
            "Verify repository name contains only valid characters and valid metadata fields."
        ),
        401: (
            "Authentication required to create a repository.",
            "Run 'flair auth login' to authenticate with your wallet."
        ),
        500: (
            "Internal server error occurred while creating the repository.",
            "The backend encountered a server error. Please wait a moment and try again."
        ),
    },
    "repo_clone": {
        404: (
            "Repository hash does not exist on the remote server.",
            "Verify the repository hash with 'flair status' or check your repository on FlairHub."
        ),
        500: (
            "Server failed to package repository clone archive.",
            "The remote server encountered an error bundling repository data. Try again later."
        ),
    },
    "repo_get": {
        404: (
            "Repository hash was not found on the remote server.",
            "Verify the repository hash in .flair/repo.json or check FlairHub."
        ),
        500: (
            "Server failed to fetch repository details.",
            "The backend encountered an error. Please try again later."
        ),
    },
    "basemodel_upload": {
        400: (
            "Invalid base model file format, unsupported extension, or corrupted weights.",
            "Ensure the model file has an allowed extension (.pt, .pth, .onnx, .h5, .keras) and valid weights."
        ),
        401: (
            "Authentication required to upload a base model.",
            "Run 'flair auth login' to sign in before uploading."
        ),
        403: (
            "Permission denied: only repository owners or admins can upload base models.",
            "Base models are immutable and require owner/admin privileges. Check repo ownership."
        ),
        404: (
            "Repository hash was not found on the remote server.",
            "Verify the repository exists on FlairHub."
        ),
        500: (
            "Server failed to store or pin base model to IPFS storage.",
            "The remote storage service failed. Wait a moment and re-attempt upload."
        ),
    },
    "basemodel_fetch_url": {
        400: (
            "No base model exists for this repository.",
            "Upload a base model first using 'flair basemodel add <model_file>'."
        ),
        404: (
            "Repository hash was not found on the remote server.",
            "Verify the repository exists on FlairHub."
        ),
        500: (
            "Server failed to generate base model download URL.",
            "Please try again later or check server status."
        ),
    },
    "basemodel_delete": {
        400: (
            "No base model registered for this repository, or delete request is invalid.",
            "Check if a base model exists first with 'flair basemodel check'."
        ),
        401: (
            "Authentication required to delete a base model.",
            "Run 'flair auth login' to authenticate with your wallet."
        ),
        403: (
            "Permission denied: only repository owners or admins can delete base models.",
            "You must be the repository owner or admin to delete base models."
        ),
        404: (
            "Base model or repository not found on the remote server.",
            "Verify the repository hash and model status."
        ),
        500: (
            "Server failed to unpin or remove the base model.",
            "Server storage deletion error. Please try again later."
        ),
    },
    "branch_list": {
        404: (
            "Repository was not found on the remote server.",
            "Check that your local repository is linked to a valid remote repository hash."
        ),
        500: (
            "Server database error while fetching repository branches.",
            "Please try again later."
        ),
    },
    "branch_create": {
        400: (
            "Invalid branch name or parent branch hash.",
            "Choose a valid branch name without spaces or special characters."
        ),
        401: (
            "Authentication required to create a branch.",
            "Run 'flair auth login' to authenticate before creating branches."
        ),
        404: (
            "Repository or parent branch hash was not found on the remote server.",
            "Run 'flair branch' to view available branches and verify your active branch."
        ),
        500: (
            "Server failed to create branch.",
            "The backend encountered an internal error. Please try again later."
        ),
    },
    "branch_delete": {
        400: (
            "Cannot delete default branch or active root branch.",
            "The default branch is protected. Switch to another branch before deleting non-default branches."
        ),
        401: (
            "Authentication required to delete a branch.",
            "Run 'flair auth login' to authenticate before deleting branches."
        ),
        404: (
            "Branch or repository was not found on the remote server.",
            "Run 'flair branch' to list existing branches and verify the branch name."
        ),
        500: (
            "Server failed to delete branch.",
            "The backend encountered an internal error. Please try again later."
        ),
    },
    "branch_get": {
        404: (
            "Branch was not found in the remote repository.",
            "Run 'flair branch' to list valid branches on the remote."
        ),
        500: (
            "Server failed to retrieve branch details.",
            "Please try again later."
        ),
    },
    "commit_initiate": {
        400: (
            "Invalid parent commit hash or commit parameters.",
            "Check your commit history with 'flair log' or run 'flair pull' to sync."
        ),
        401: (
            "Authentication required to initiate commit session.",
            "Run 'flair auth login' to authenticate your wallet session."
        ),
        403: (
            "Permission denied: writer or contributor role required to push to this branch.",
            "You do not have writer access to this repository. Request collaborator access from the owner."
        ),
        404: (
            "Repository or branch hash was not found on remote.",
            "Ensure the branch exists on remote and repo hash in .flair/repo.json is correct."
        ),
        500: (
            "Server failed to initialize commit session.",
            "Backend session generation failure. Please try pushing again."
        ),
    },
    "commit_zkml_check": {
        400: (
            "Malformed ZKML proof payload or invalid proof parameters.",
            "Regenerate proof using 'flair zkp create --model <model>'."
        ),
        401: (
            "Authentication required to check ZKML proof.",
            "Run 'flair auth login' to renew your session."
        ),
        403: (
            "Commit session token expired or unauthorized.",
            "Re-run 'flair push' to initiate a fresh commit session."
        ),
        404: (
            "Commit session or target branch was not found.",
            "Session timed out. Re-run 'flair push' to initiate a fresh session."
        ),
        409: (
            "Conflict: an identical ZKML proof already exists in this repository.",
            "Model parameters produced a duplicate proof. Train further or update weights before committing."
        ),
        500: (
            "Server proof verification engine failure.",
            "Backend proof verifier error. Please try again later."
        ),
    },
    "commit_zkml_upload": {
        400: (
            "Missing required ZKML proof files (proof, settings, or verification key).",
            "Verify all proof files exist in .flair/.zkp and recreate if needed."
        ),
        401: (
            "Authentication required to upload ZKML proof.",
            "Run 'flair auth login' to sign in."
        ),
        403: (
            "Invalid or expired commit session token.",
            "Re-run 'flair push' to establish a fresh session."
        ),
        404: (
            "Commit session was not found on server.",
            "Session expired. Re-run 'flair push'."
        ),
        409: (
            "ZKML proof upload conflict.",
            "Concurrent upload detected. Re-run 'flair push'."
        ),
        500: (
            "Server failed to store ZKML artifacts to IPFS storage.",
            "Storage pinning error on server. Please try again later."
        ),
    },
    "commit_params_upload": {
        400: (
            "Parameter file missing or parameter hash does not match computed digest.",
            "Recreate local parameter file with 'flair params create' and re-push."
        ),
        401: (
            "Authentication required to upload model parameters.",
            "Run 'flair auth login' to sign in."
        ),
        403: (
            "Invalid or expired commit session token.",
            "Re-run 'flair push' to establish a fresh session."
        ),
        404: (
            "Commit session was not found on server.",
            "Session expired. Re-run 'flair push'."
        ),
        409: (
            "Parameter upload conflict.",
            "Concurrent upload detected. Re-run 'flair push'."
        ),
        500: (
            "Server failed to store model parameters to IPFS storage.",
            "Storage pinning error on server. Please try again later."
        ),
    },
    "commit_finalize": {
        400: (
            "Malformed commit payload or signature format.",
            "Ensure your SSH key and commit payload are properly formatted."
        ),
        401: (
            "Authentication required to finalize commit.",
            "Run 'flair auth login' to sign in."
        ),
        403: (
            "Commit signature verification failed or SSH key fingerprint is not registered to your account.",
            "Run 'flair auth ssh status' to check your key and 'flair auth ssh setup' to register it."
        ),
        404: (
            "Commit session or branch was not found on server.",
            "Session expired. Re-run 'flair push'."
        ),
        409: (
            "Conflict: remote branch HEAD has moved since session initiation (concurrent push collision).",
            "Run 'flair pull' to synchronize remote commits before pushing again."
        ),
        500: (
            "Atomic commit finalization failed on server.",
            "Database transaction error on backend. Please re-run 'flair push'."
        ),
    },
    "commit_status_sync": {
        404: (
            "Repository or target branch was not found on remote server.",
            "Verify repository configuration with 'flair status' and branch with 'flair branch'."
        ),
        500: (
            "Server failed to query commit statuses.",
            "The backend encountered an error. Please try again later."
        ),
    },
    "commit_pull": {
        404: (
            "Commit hash was not found on remote branch.",
            "Verify the commit hash or run 'flair pull' to update branch references."
        ),
        500: (
            "Server failed to package commit artifacts for pull.",
            "Backend packaging error. Please try again later."
        ),
    },
    "user_keys": {
        400: (
            "Invalid SSH public key format (must be OpenSSH format, e.g. ssh-ed25519).",
            "Generate a new valid SSH key with 'flair auth ssh setup'."
        ),
        401: (
            "Authentication required to register or access SSH keys.",
            "Run 'flair auth login' to authenticate your wallet before setting up SSH."
        ),
        404: (
            "User account was not found on the remote server.",
            "Ensure you have completed login with 'flair auth login'."
        ),
        409: (
            "This SSH public key is already registered to an account.",
            "Run 'flair auth ssh status' to check keys, or generate a fresh key with 'flair auth ssh setup --force'."
        ),
        500: (
            "Server failed to store SSH key.",
            "Database error on backend. Please try again later."
        ),
    },
}


def _match_endpoint_category(endpoint_or_url: str) -> str:
    """Classify request URL / endpoint into OpenAPI category."""
    e = endpoint_or_url.lower()
    e = e.replace("/commit/seqpush/create/", "/commit/create/")

    if "/repo/create" in e or e == "create_repo":
        return "repo_create"
    if "/clone" in e:
        return "repo_clone"
    if "/basemodel/upload" in e:
        return "basemodel_upload"
    if "/basemodel/fetch_url" in e:
        return "basemodel_fetch_url"
    if "/basemodel/delete" in e:
        return "basemodel_delete"
    if "/branch/create" in e:
        return "branch_create"
    if "/branch/hash/" in e and "/delete" in e:
        return "branch_delete"
    if "/branch/name/" in e:
        return "branch_get"
    if "/branch" in e and "/commit" not in e:
        return "branch_list"
    if "/commit/create/initiate" in e:
        return "commit_initiate"
    if "/commit/create/zkml-check" in e:
        return "commit_zkml_check"
    if "/commit/create/zkml-upload" in e:
        return "commit_zkml_upload"
    if "/commit/create/params-upload" in e:
        return "commit_params_upload"
    if "/commit/create/finalize" in e:
        return "commit_finalize"
    if "/commit/status/sync" in e:
        return "commit_status_sync"
    if "/commit/hash/" in e and "/pull" in e:
        return "commit_pull"
    if "/user/keys" in e:
        return "user_keys"
    if "/repo/hash/" in e:
        return "repo_get"

    return ""


def format_api_error(
    status_code: int,
    endpoint_or_url: str = "",
    response_data: Optional[dict[str, Any]] = None,
    context: str = "Command",
) -> tuple[str, str, str, int]:
    """Format an API error using OpenAPI specs.

    Returns:
        (summary, root_cause, actionable_fix, exit_code)
    """
    category = _match_endpoint_category(endpoint_or_url)
    mapping = _OPENAPI_ERROR_MAP.get(category, {}).get(status_code)

    # Extract any server-provided error detail
    server_detail = None
    if isinstance(response_data, dict):
        server_detail = (
            response_data.get("error", {}).get("message")
            if isinstance(response_data.get("error"), dict)
            else response_data.get("error")
        ) or response_data.get("message") or response_data.get("detail")

    if mapping:
        cause, fix = mapping
        if server_detail and str(server_detail).strip():
            cause = f"{cause} (Backend: {server_detail})"
    else:
        # Generic HTTP status code fallback based on HTTP/OpenAPI conventions
        if status_code == 400:
            cause = f"Bad Request: The server could not understand or validate the request."
            fix = "Check command parameters and repository configuration."
        elif status_code == 401:
            cause = "Unauthorized: Session is invalid or has expired."
            fix = "Run 'flair auth login' to authenticate with your wallet."
        elif status_code == 403:
            cause = "Forbidden: You do not have permission to perform this action."
            fix = "Verify your account permissions or collaborator role on this repository."
        elif status_code == 404:
            cause = "Not Found: The requested resource does not exist on the remote server."
            fix = "Verify the repository hash, branch name, or commit hash."
        elif status_code == 409:
            cause = "Conflict: Remote state has diverged or conflicting data exists."
            fix = "Synchronize local state with 'flair pull' before retrying."
        elif status_code >= 500:
            cause = f"Internal Server Error (HTTP {status_code}) occurred on the FlairHub backend."
            fix = "The remote server encountered an error. Please wait a moment and try again."
        else:
            cause = f"HTTP request failed with status code {status_code}."
            fix = "Check server status and command syntax."

        if server_detail and str(server_detail).strip():
            cause = f"{cause} (Backend: {server_detail})"

    # Exit code determination:
    # 2: Authentication / Authorization (401, 403)
    # 3: Server / Network error (500-599)
    # 1: Client / operational validation (400, 404, 409)
    if status_code in (401, 403):
        exit_code = 2
    elif status_code >= 500:
        exit_code = 3
    else:
        exit_code = 1

    summary = f"Error communicating with server (HTTP {status_code})"
    return summary, cause, fix, exit_code


def print_cli_error(context: str, summary: str, root_cause: str, actionable_fix: str) -> None:
    """Print standard 3-part diagnostic error block to console."""
    console.print(f"[bold red]✗ {context} failed: {summary}[/bold red]")
    console.print(f"[dim]Cause: {root_cause}[/dim]")
    console.print(f"[yellow]Fix: {actionable_fix}[/yellow]")


def handle_cli_exception(
    exc: Exception,
    context: str = "Command",
    endpoint: str = "",
    default_exit_code: Optional[int] = None,
) -> None:
    """Catch and format CLI exceptions with full diagnostic explanation.

    Always re-raises typer.Exit directly.
    Exits with appropriate exit code contract (0, 1, 2, 3).
    """
    if isinstance(exc, typer.Exit):
        raise exc

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        response_data = None
        try:
            if exc.response.content:
                response_data = exc.response.json()
        except Exception:
            pass

        req_url = str(exc.request.url) if exc.request else endpoint
        summary, cause, fix, computed_exit_code = format_api_error(
            status_code=status_code,
            endpoint_or_url=req_url or endpoint,
            response_data=response_data,
            context=context,
        )

        exit_code = default_exit_code if default_exit_code is not None else computed_exit_code
        # Preserve exit code 2 for auth/permissions regardless of default_exit_code
        if status_code in (401, 403):
            exit_code = 2

        print_cli_error(context, summary, cause, fix)
        raise typer.Exit(code=exit_code)

    if isinstance(exc, httpx.TimeoutException):
        summary = "Request timed out"
        cause = (
            "The remote server did not respond within the timeout limit. "
            "This commonly happens when the FlairHub backend is waking up from idle/sleep "
            "(e.g. on Render) or during high network latency."
        )
        fix = (
            "Please wait a moment and try again, or increase the timeout using:\n"
            "  set FLAIR_REQUEST_TIMEOUT=120 (PowerShell: $env:FLAIR_REQUEST_TIMEOUT=\"120\")"
        )
        exit_code = default_exit_code if default_exit_code is not None else 3
        print_cli_error(context, summary, cause, fix)
        raise typer.Exit(code=exit_code)

    if isinstance(exc, (httpx.ConnectError, httpx.RequestError)):
        summary = f"Network Error - Failed to connect to host: {exc}"
        cause = (
            f"Could not establish a network connection to FlairHub at {_base_url()}. "
            "The remote host may be offline, port blocked by firewall, or DNS resolution failed."
        )
        fix = (
            "Check your internet connection, verify the FlairHub backend URL (FLAIR_API_BASE), "
            "or ensure the local/remote backend server is running."
        )
        exit_code = default_exit_code if default_exit_code is not None else 3
        print_cli_error(context, summary, cause, fix)
        raise typer.Exit(code=exit_code)

    # General unexpected exception
    summary = str(exc)
    cause = "An operational or file validation error occurred."
    fix = "Verify local repository state with 'flair status' or check permissions and command syntax."
    exit_code = default_exit_code if default_exit_code is not None else 1
    print_cli_error(context, summary, cause, fix)
    raise typer.Exit(code=exit_code)
