"""
Push command: Create and upload a commit to remote repository.
Follows the 5-step commit creation flow with ZKML proofs.
"""
from __future__ import annotations
import typer
from rich.console import Console
from pathlib import Path
import json
import httpx
import hashlib
import shutil
import os
from datetime import datetime, timezone

from ..api import client as api_client
from ..api.utils import _base_url, _client_with_auth
from ..core import session
from .utils.local_commits import _get_all_local_commits, _get_flair_dir, _get_head_info, _get_latest_local_commit
from .utils.commit_signing import (
    build_canonical_payload,
    extract_jti_from_jwt,
    find_agent_identity_by_fingerprint,
    load_ssh_agent_identities,
    sign_canonical_payload,
)

from .utils.local_commits import _is_commit_complete
from .utils.commit_reconciliation import apply_commit_hash_reconciliation, recover_pending_reconciliation_if_needed
from .utils.errors import handle_cli_exception

app = typer.Typer()
console = Console()
def _verify_local_commit_chain(commits_to_push: list[tuple], remote_head_hash: str | None = None) -> tuple[bool, str]:
    """
    Verify merkle tree chain integrity: each commit's previousCommitHash 
    must match the previous commit in the chain, remote head, or be genesis.
    
    Returns: (is_valid, error_message)
    """
    GENESIS_COMMIT_HASH = "_GENESIS_COMMIT_"
    
    if not commits_to_push:
        return True, ""
    
    for idx, (commit_data, commit_dir) in enumerate(commits_to_push):
        commit_hash = commit_data.get("commitHash") or ""
        previous_hash = commit_data.get("previousCommitHash")
        
        if idx == 0:
            # First commit can reference genesis, empty, or remote_head_hash if built on remote
            valid_parents = {GENESIS_COMMIT_HASH, None, ""}
            if remote_head_hash:
                valid_parents.add(remote_head_hash)
            if previous_hash and previous_hash not in valid_parents:
                prev_display = str(previous_hash)[:16]
                return False, f"First commit {commit_hash[:16]}... should reference Genesis or remote head ({str(remote_head_hash)[:16]}...) but references {prev_display}..."
        else:
            # Subsequent commits should reference the previous commit
            expected_parent = commits_to_push[idx - 1][0].get("commitHash") or ""
            if previous_hash != expected_parent:
                merged_parents = set(commit_data.get("mergedParentHashes") or [])
                target_parent = commit_data.get("metadata", {}).get("mergeParent")
                valid_merge_parents = set(merged_parents)
                if target_parent:
                    valid_merge_parents.add(target_parent)
                is_merge = bool(merged_parents) or commit_data.get("status") == "MERGER"
                if is_merge and (previous_hash in valid_merge_parents or expected_parent in valid_merge_parents):
                    pass
                else:
                    prev_display = str(previous_hash)[:16] if previous_hash else "None"
                    expected_display = str(expected_parent)[:16] if expected_parent else "None"
                    return False, f"Commit {commit_hash[:16]}... previousCommitHash ({prev_display}...) does not match parent ({expected_display}...)"
    
    return True, ""



def _get_remote_latest_commit(repo_hash: str, branch_hash: str) -> str | None:
    """Get the latest commit hash from remote branch."""
    try:
        with _client_with_auth() as client:
            response = client.get(
                f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/latest"
            )
            response.raise_for_status()
            data = response.json().get("data", {})
            return data.get("commitHash")
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            # Branch exists but has no commits yet
            return None
        raise
    except Exception:
        return None


def _garbage_collect_local_commits(retention_limit: int) -> int:
    """Delete local commits older than the retention limit.

    Returns: Number of commits deleted
    """
    if retention_limit <= 0:
        return 0

    flair_dir = _get_flair_dir()
    local_commits_dir = flair_dir / ".local_commits"

    if not local_commits_dir.exists():
        return 0

    commit_dirs = sorted(local_commits_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True)

    if len(commit_dirs) <= retention_limit:
        return 0

    to_delete = commit_dirs[retention_limit:]
    deleted_count = 0

    for commit_dir in to_delete:
        try:
            shutil.rmtree(commit_dir)
            deleted_count += 1
        except Exception:
            continue

    return deleted_count


def _load_repo_config() -> dict:
    """Load repository info from .flair/repo.json"""
    flair_dir = _get_flair_dir()
    config_file = flair_dir / "repo.json"
    
    if not config_file.exists():
        raise typer.BadParameter("Repository info not found. Run 'flair init' first.")
    
    with open(config_file, 'r') as f:
        return json.load(f)


def _can_publish_merge_commit(repo_config: dict, branch_name: str, wallet_address: str) -> tuple[bool, str]:
    """Check whether current wallet can publish MERGE commits on target branch."""
    metadata = repo_config.get("metadata", {}) if isinstance(repo_config, dict) else {}
    policy = metadata.get("mergePolicy") or repo_config.get("mergePolicy")

    if not isinstance(policy, dict):
        # No policy configured yet: preserve current behavior.
        return True, ""

    protected_branches = policy.get("protectedBranches") or []
    if protected_branches and branch_name not in protected_branches:
        return True, ""

    authorized = policy.get("authorizedMergers") or policy.get("authorizedWallets") or []
    if not authorized:
        return False, "MERGE commits are protected but no authorized mergers are configured."

    if wallet_address not in authorized:
        return False, "Permission denied. MERGE commits require maintainer access."

    return True, ""


def _load_repo_settings() -> dict:
    """Load repo settings from config.yaml in the repo root."""
    settings_file = Path.cwd() / "config.yaml"
    if not settings_file.exists():
        return {"commitRetentionLimit": 25}

    settings: dict[str, object] = {}
    try:
        with open(settings_file, "r") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if ":" not in stripped:
                    continue
                key, value = stripped.split(":", 1)
                key = key.strip()
                value = value.strip()
                if value.isdigit():
                    settings[key] = int(value)
                else:
                    settings[key] = value
    except Exception:
        return {"commitRetentionLimit": 25}

    if "commitRetentionLimit" not in settings:
        settings["commitRetentionLimit"] = 25

    return settings

def _check_remote_repo_exists(repo_hash: str) -> bool:
    """Check if the remote repository exists on Flair backend."""
    try:
        with _client_with_auth() as client:
            response = client.get(f"{_base_url()}/api/repo/hash/{repo_hash}")
            if response.status_code == 200:
                data = response.json()
                if data and (data.get("id") or data.get("data") or data.get("repoHash") or data.get("name")):
                    return True
                return True
            return False
    except Exception:
        return False


def _compute_param_hash(file_path: Path) -> str:
    """Compute SHA256 hash of params file."""
    sha256 = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            sha256.update(chunk)
    return sha256.hexdigest()


@app.command()
def push(
    branch_name: str = typer.Argument(None, help="Branch name to push to"),
    upstream: str = typer.Option(None, "-u", "--set-upstream", help="Set upstream (always 'origin')")
):
    """Push commits to remote repository.
    
    Pushes all completed local commits serially to the remote branch.
    Skips incomplete commits (missing params, ZKP, or not finalized).
    
    Prerequisites (for each commit):
    - Run 'flair add' to create a local commit
    - Run 'flair params create' to add model parameters
    - Run 'flair zkp create' to generate zero-knowledge proof
    - Run 'flair commit -m "message"' to finalize the commit
    
    Examples:
        flair push -u origin main
        flair push main
        flair push  # Push to current branch
    """
    try:
        recover_pending_reconciliation_if_needed()
        # Get all local commits sorted by creation time
        all_local_commits = _get_all_local_commits()
        if not all_local_commits:
            console.print("[red]No local commits found. Run 'flair add' first.[/red]")
            raise typer.Exit(1)
        
        # Load repo config
        repo_config = _load_repo_config()
        repo_hash = (
            repo_config.get("repoHash")
            or repo_config.get("hash")
            or repo_config.get("id")
            or repo_config.get("metadata", {}).get("repoHash")
        )
        
        if not repo_hash:
            console.print("[red]Repository hash not found in config.[/red]")
            raise typer.Exit(1)

        # Filter commits to push: only complete commits
        commits_to_push = []
        for commit_data, commit_dir in all_local_commits:
            if _is_commit_complete(commit_data, commit_dir):
                commits_to_push.append((commit_data, commit_dir))
            else:
                console.print(f"[dim]Skipping incomplete commit: {commit_data.get('commitHash', 'unknown')[:16]}...[/dim]")
                break
        
        if not commits_to_push:
            console.print("[yellow]No complete commits to push.[/yellow]")
            console.print("[dim]Ensure commits have params, ZKP, and are finalized with 'flair commit -m'.[/dim]")
            raise typer.Exit(0)
        
        # Verify local commit chain integrity (merkle tree)
        console.print("[dim]Verifying local commit chain integrity...[/dim]")
        chain_valid, chain_error = _verify_local_commit_chain(commits_to_push, remote_head_hash=None)
        if not chain_valid:
            console.print(f"[red]✗ Chain integrity check failed: {chain_error}[/red]")
            console.print(f"[yellow]Cannot push commits with broken chain. Fix and try again.[/yellow]")
            raise typer.Exit(code=1)
        console.print("[green]✓ Local chain verified[/green]")

        # Check if remote repository exists (User Decision 1: Do not auto-create on push, prompt user to add/link first)
        if not _check_remote_repo_exists(repo_hash):
            console.print(f"[red]Error: Remote repository '{repo_hash}' not found on remote.[/red]")
            console.print("[yellow]Please create or link the repository on FlairHub first using:[/yellow]")
            console.print(f"  flair remote add origin <remote_repo_hash>\n")
            raise typer.Exit(code=1)
        
        # Get HEAD info
        head_info = _get_head_info()
        
        # Determine target branch
        target_branch_name = branch_name
        if not target_branch_name:
            if head_info:
                target_branch_name = head_info.get("currentBranch")
            else:
                target_branch_name = "main"  # Default to main
        
        console.print(f"\n[cyan]Pushing to branch: {target_branch_name}[/cyan]")
        
        # Get or create branch
        branch_data = None
        try:
            branch_data = api_client.get_branch_by_name(repo_hash, target_branch_name)
        except Exception:
            pass
        
        # If branch doesn't exist, create it
        if not branch_data or isinstance(branch_data, list):
            console.print(f"[yellow]Branch '{target_branch_name}' not found. Creating...[/yellow]")
            
            # If this is the first branch, create from null parent
            # Otherwise, create from current branch
            parent_branch_hash = None
            if head_info:
                parent_branch_hash = head_info.get("branchHash")
            
            try:
                branch_data = api_client.create_branch(
                    repo_hash,
                    target_branch_name,
                    parent_branch_hash,
                    f"Created via push command"
                )
                console.print(f"[green]✓ Branch '{target_branch_name}' created[/green]")
            except Exception as e:
                console.print(f"[red]Failed to create branch: {e}[/red]")
                raise typer.Exit(code=1)
        
        branch_id = branch_data.get("id") or branch_data.get("branchId")
        branch_hash = branch_data.get("branchHash")
        
        if not branch_id or not branch_hash:
            console.print("[red]Invalid branch data received[/red]")
            raise typer.Exit(code=1)
        
        # Get remote latest commit
        remote_head_hash = _get_remote_latest_commit(repo_hash, branch_hash)
        
        if remote_head_hash:
            console.print(f"[dim]Remote HEAD: {remote_head_hash[:16]}...[/dim]")
        else:
            console.print(f"[dim]Remote HEAD: Genesis (no commits yet)[/dim]")
        
        # Find where to start pushing (after remote head)
        start_index = 0
        if remote_head_hash:
            for i, (commit_data, _) in enumerate(commits_to_push):
                if commit_data.get("commitHash") == remote_head_hash:
                    start_index = i + 1
                    break
        
        commits_to_push = commits_to_push[start_index:]
        
        if not commits_to_push:
            console.print("[green]✓ All commits already pushed. Branch is up to date.[/green]")
            raise typer.Exit(0)
        
        console.print(f"[cyan]Pushing {len(commits_to_push)} commit(s) serially...[/cyan]\n")

        current_session = session.load_session()
        user_principal = None
        if current_session:
            user_principal = current_session.principal or current_session.wallet_address

        if not current_session or not user_principal:
            console.print("[red]Authenticated principal not found. Run 'flair auth login'.[/red]")
            raise typer.Exit(code=2)

        wallet_address = user_principal

        with _client_with_auth() as client:
            response = client.get(f"{_base_url()}/api/user/keys")
            response.raise_for_status()
            registered_keys = response.json().get("data", [])

        registered_fingerprints = {
            key.get("fingerprint")
            for key in registered_keys
            if isinstance(key, dict) and key.get("fingerprint")
        }

        agent_identities = load_ssh_agent_identities()
        if not agent_identities:
            console.print("[red]No SSH keys are loaded in ssh-agent. Load the key with ssh-add and try again.[/red]")
            raise typer.Exit(code=1)

        ssh_identity = None
        for fingerprint in registered_fingerprints:
            ssh_identity = find_agent_identity_by_fingerprint(fingerprint, agent_identities)
            if ssh_identity:
                break

        if not ssh_identity:
            console.print("[red]No ssh-agent key matches a registered Flair SSH public key.[/red]")
            console.print("[yellow]Run flair auth ssh setup to register the public key, then ssh-add the key.[/yellow]")
            raise typer.Exit(code=1)
        
        # Determine initial parent commit hash
        parent_commit_hash = remote_head_hash if remote_head_hash else "_GENESIS_COMMIT_"

        # Phase 1: Push Manifest Handshake negotiation (User Decision 3)
        manifest_payload = {
            "clientVersion": "1.0.0",
            "repoHash": repo_hash,
            "targetBranch": {
                "name": target_branch_name,
                "branchHash": branch_hash,
                "parentBranchHash": head_info.get("branchHash") if head_info else None,
                "description": f"Push to {target_branch_name}"
            },
            "remoteHeadCommitHash": remote_head_hash,
            "commits": [
                {
                    "localCommitHash": c_data.get("commitHash"),
                    "previousCommitHash": c_data.get("previousCommitHash"),
                    "commitType": c_data.get("commitType", "CHECKPOINT"),
                    "mergedParentHashes": sorted(c_data.get("mergedParentHashes")) if c_data.get("mergedParentHashes") else None,
                    "message": c_data.get("message", ""),
                    "timestamp": c_data.get("timestamp"),
                    "params": c_data.get("params"),
                    "zkp": c_data.get("zkp"),
                    "metrics": c_data.get("metrics")
                }
                for c_data, c_dir in commits_to_push
            ]
        }

        manifest_negotiated = False
        negotiation_data = None
        try:
            negotiation_data = api_client.push_manifest(repo_hash, manifest_payload)
            if negotiation_data and isinstance(negotiation_data, dict):
                manifest_negotiated = True
                console.print("[green]✓ Push Manifest Handshake negotiated with backend[/green]\n")
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                manifest_negotiated = False
            else:
                raise
        except Exception:
            manifest_negotiated = False
        
        # Push each commit serially
        framework = repo_config.get("metadata", {}).get("framework") or repo_config.get("framework", "unknown")
        pushed_count = 0
        commit_hash_map: dict[str, str] = {}
        
        for idx, (commit_data, commit_dir) in enumerate(commits_to_push, 1):
            commit_hash = commit_data.get("commitHash")
            message = commit_data.get("message")
            commit_type = commit_data.get("commitType", "CHECKPOINT")
            commit_metrics = commit_data.get("metrics")
            merged_parent_hashes = commit_data.get("mergedParentHashes")
            is_merge_commit = bool(merged_parent_hashes) or commit_data.get("status") == "MERGER"

            if is_merge_commit:
                allowed, denial_reason = _can_publish_merge_commit(repo_config, target_branch_name, wallet_address)
                if not allowed:
                    console.print(f"[red]✗ Commit {idx}: {denial_reason}[/red]")
                    console.print(f"[yellow]Stopping push after {pushed_count} successful commit(s).[/yellow]")
                    raise typer.Exit(code=2)
            
            console.print(f"[bold cyan]═══ Commit {idx}/{len(commits_to_push)} ═══[/bold cyan]")
            console.print(f"[dim]Hash: {commit_hash[:16]}...[/dim]")
            console.print(f"[dim]Type: {commit_type}[/dim]")
            console.print(f"[dim]Message: {message}[/dim]")
            console.print(f"[dim]Parent: {parent_commit_hash[:16] if parent_commit_hash != '_GENESIS_COMMIT_' else 'Genesis'}...[/dim]\n")
            
            # Get params file for this commit
            params_info = commit_data.get("params")
            if commit_type == "DELTA":
                delta_params_info = commit_data.get("deltaParams")
                if not delta_params_info or not delta_params_info.get("file"):
                    console.print(f"[red]✗ Commit {idx}: Delta parameters missing[/red]")
                    console.print(f"[yellow]Stopping push after {pushed_count} successful commit(s).[/yellow]")
                    raise typer.Exit(code=1)
                params_file = commit_dir / ".delta_params" / delta_params_info["file"]
                if not params_file.exists():
                    params_file = commit_dir / delta_params_info["file"]
            else:
                if not params_info or not params_info.get("file"):
                    console.print(f"[red]✗ Commit {idx}: Parameters missing[/red]")
                    console.print(f"[yellow]Stopping push after {pushed_count} successful commit(s).[/yellow]")
                    raise typer.Exit(code=1)
                params_file = commit_dir / params_info["file"]
            
            if not params_file.exists():
                console.print(f"[red]✗ Commit {idx}: Parameters file not found[/red]")
                console.print(f"[yellow]Stopping push after {pushed_count} successful commit(s).[/yellow]")
                raise typer.Exit(code=1)
            
            # Get ZKP files for this commit
            zkp_info = commit_data.get("zkp")
            is_checkpoint = commit_type == "CHECKPOINT"
            
            zkp_files = {}
            if not is_checkpoint:
                if not zkp_info:
                    console.print(f"[red]✗ Commit {idx}: ZKP info missing[/red]")
                    console.print(f"[yellow]Stopping push after {pushed_count} successful commit(s).[/yellow]")
                    raise typer.Exit(code=1)
                
                proof_file = commit_dir / zkp_info.get("proof_file", "proof.zlib")
                vk_file = commit_dir / zkp_info.get("verification_key_file", "verification_key.zlib")
                settings_file = commit_dir / zkp_info.get("settings_file", "settings.zlib")
                
                if not all([proof_file.exists(), vk_file.exists(), settings_file.exists()]):
                    console.print(f"[red]✗ Commit {idx}: ZKP files missing[/red]")
                    console.print(f"[yellow]Stopping push after {pushed_count} successful commit(s).[/yellow]")
                    raise typer.Exit(code=1)
                
                zkp_files = {
                    "proof_file": proof_file,
                    "vk_file": vk_file,
                    "settings_file": settings_file,
                    "proof_cid": zkp_info.get("proof_cid"),
                    "vk_cid": zkp_info.get("verification_key_cid"),
                    "settings_cid": zkp_info.get("settings_cid"),
                    "base_commit_hash": zkp_info.get("base_commit_hash")
                }
            
            # Step 1: Initiate commit session
            console.print("[cyan]Step 1/5: Initiating commit session...[/cyan]")
            with _client_with_auth() as client:
                response = client.post(
                    f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/initiate",
                    json={"parentCommitHash": parent_commit_hash}
                )
                response.raise_for_status()
                init_data = response.json()
            
            session_id = init_data.get("sessionId")
            initiate_token = init_data.get("initiateToken")
            
            if not session_id or not initiate_token:
                console.print(f"[red]✗ Commit {idx}: Failed to initiate session[/red]")
                console.print(f"[yellow]Stopping push after {pushed_count} successful commit(s).[/yellow]")
                raise typer.Exit(code=1)
            
            console.print(f"[green]✓ Session initiated[/green]")
            
            # Step 2: Check ZKML proof uniqueness
            console.print("[cyan]Step 2/5: Checking ZKML proof uniqueness...[/cyan]")
            with _client_with_auth() as client:
                req_data = {
                    "sessionId": session_id,
                    "initiateToken": initiate_token,
                    "isCheckpoint": is_checkpoint
                }
                if not is_checkpoint:
                    req_data.update({
                        "proofCid": zkp_files["proof_cid"],
                        "settingsCid": zkp_files["settings_cid"],
                        "vkCid": zkp_files["vk_cid"]
                    })

                response = client.post(
                    f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/zkml-check",
                    json=req_data
                )
                response.raise_for_status()
                zkml_check_data = response.json()
            
            zkml_token = zkml_check_data.get("zkmlToken")
            if not zkml_token:
                console.print(f"[red]✗ Commit {idx}: Failed to verify ZKML proof uniqueness[/red]")
                console.print(f"[yellow]Stopping push after {pushed_count} successful commit(s).[/yellow]")
                raise typer.Exit(code=1)
            
            console.print(f"[green]✓ ZKML proof verified as unique[/green]")
            
            # Step 3: Upload ZKML proofs
            console.print("[cyan]Step 3/5: Uploading ZKML proofs...[/cyan]")
            with _client_with_auth() as client:
                data = {
                    "sessionId": session_id,
                    "initiateToken": initiate_token,
                    "zkmlToken": zkml_token
                }
                
                if is_checkpoint:
                    response = client.post(
                        f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/zkml-upload",
                        data=data
                    )
                else:
                    with open(zkp_files["proof_file"], "rb") as f_p, \
                         open(zkp_files["settings_file"], "rb") as f_s, \
                         open(zkp_files["vk_file"], "rb") as f_v:
                        files = {
                            "proof": ("proof.zlib", f_p.read(), "application/octet-stream"),
                            "settings": ("settings.zlib", f_s.read(), "application/octet-stream"),
                            "verification_key": ("verification_key.zlib", f_v.read(), "application/octet-stream")
                        }
                        response = client.post(
                            f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/zkml-upload",
                            files=files,
                            data=data
                        )
                response.raise_for_status()
                zkml_upload_data = response.json()
            
            zkml_receipt_token = zkml_upload_data.get("zkmlReceiptToken")
            if not zkml_receipt_token:
                console.print(f"[red]✗ Commit {idx}: Failed to upload ZKML proofs[/red]")
                console.print(f"[yellow]Stopping push after {pushed_count} successful commit(s).[/yellow]")
                raise typer.Exit(code=1)
            
            console.print(f"[green]✓ ZKML proofs uploaded[/green]")
            
            # Step 4: Upload parameters
            console.print("[cyan]Step 4/5: Uploading parameters...[/cyan]")
            param_hash = _compute_param_hash(params_file)
            
            with _client_with_auth() as client:
                with open(params_file, "rb") as pf:
                    files = {
                        "params": (params_file.name, pf.read(), "application/octet-stream")
                    }
                data = {
                    "sessionId": session_id,
                    "initiateToken": initiate_token,
                    "zkmlReceiptToken": zkml_receipt_token,
                    "paramHash": param_hash
                }
                
                response = client.post(
                    f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/params-upload",
                    files=files,
                    data=data
                )
                response.raise_for_status()
                params_upload_data = response.json()
            
            params_receipt_token = params_upload_data.get("paramsReceiptToken")
            if not params_receipt_token:
                console.print(f"[red]✗ Commit {idx}: Failed to upload parameters[/red]")
                console.print(f"[yellow]Stopping push after {pushed_count} successful commit(s).[/yellow]")
                raise typer.Exit(code=1)
            
            console.print(f"[green]✓ Parameters uploaded (hash: {param_hash[:16]}...)[/green]")
            
            # Step 5: Finalize commit with signature
            console.print("[cyan]Step 5/5: Finalizing commit...[/cyan]")
            session_jti = extract_jti_from_jwt(initiate_token)
            if not session_jti:
                console.print(f"[red]✗ Commit {idx}: Could not extract session nonce from initiate token[/red]")
                console.print(f"[yellow]Stopping push after {pushed_count} successful commit(s).[/yellow]")
                raise typer.Exit(code=1)
            signed_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')

            # Principal used for commit attribution (authenticated wallet or SSH principal).
            user_principal = wallet_address

            # Remap any local UUIDs in mergedParentHashes to authoritative SHA-256 hashes before signing
            raw_merged_parents = commit_data.get("mergedParentHashes")
            if raw_merged_parents:
                remapped_parents = [commit_hash_map.get(p, p) for p in raw_merged_parents]
                commit_data["mergedParentHashes"] = remapped_parents
                canonical_merged_parents = sorted(remapped_parents)
            else:
                canonical_merged_parents = None

            # Build canonical payload matching server structure (keep for SSH signing).
            canonical_payload = build_canonical_payload(
                session_jti=session_jti,
                signed_at=signed_at,
                params_ipfs_id=params_upload_data.get("paramsIpfsId", ""),
                param_hash=param_hash,
                previous_commit_hash=parent_commit_hash,
                architecture=framework,
                commit_type=commit_type,
                message=message,
                metrics=commit_metrics,
                merged_parent_hashes=canonical_merged_parents,
            )
            
            commit_signature = sign_canonical_payload(canonical_payload, ssh_identity)
            if not commit_signature:
                console.print(f"[red]✗ Commit {idx}: Failed to sign commit[/red]")
                console.print(f"[yellow]Stopping push after {pushed_count} successful commit(s).[/yellow]")
                raise typer.Exit(code=1)
            
            console.print(f"[dim]Commit signed (signature: {commit_signature[:16]}...)[/dim]")
            
            finalize_payload = {
                "sessionId": session_id,
                "initiateToken": initiate_token,
                "zkmlReceiptToken": zkml_receipt_token,
                "paramsReceiptToken": params_receipt_token,
                "signedAt": signed_at,
                "message": message,
                "paramHash": param_hash,
                "architecture": framework,
                "metrics": commit_metrics,
                "commitSignature": commit_signature,
                "sshKeyFingerprint": ssh_identity.fingerprint.replace("ssh:", "").strip(),
                "commitType": commit_type,
            }
            if canonical_merged_parents is not None:
                finalize_payload["mergedParentHashes"] = canonical_merged_parents

            with _client_with_auth() as client:
                response = client.post(
                    f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/finalize",
                    json=finalize_payload
                )
                response.raise_for_status()
                finalize_data = response.json()
            
            returned_commit_data = finalize_data.get("data", {})
            returned_commit_hash = returned_commit_data.get("commitHash")
            if not returned_commit_hash:
                console.print(f"[red]✗ Commit {idx}: Finalization failed[/red]")
                console.print(f"[yellow]Stopping push after {pushed_count} successful commit(s).[/yellow]")
                raise typer.Exit(code=1)
            
            console.print(f"[bold green]✓ Commit {idx} created successfully![/bold green]")
            console.print(f"  [dim]Hash: {returned_commit_hash[:16]}...[/dim]")
            console.print(f"  [dim]Type: {commit_type}[/dim]")
            
            # Update local commit.json with server-returned authoritative hash and status
            # This ensures the local hash matches the server's canonical commit hash
            commit_data["commitHash"] = returned_commit_hash
            commit_data["status"] = returned_commit_data.get("status")
            commit_file = commit_dir / "commit.json"
            try:
                with open(commit_file, 'w') as f:
                    json.dump(commit_data, f, indent=2)
                target_dir = commit_dir.parent / returned_commit_hash
                if target_dir != commit_dir and not target_dir.exists():
                    commit_dir.rename(target_dir)
                    commit_dir = target_dir
                console.print(f"  [dim]Local commit updated with server hash[/dim]\n")
            except Exception as e:
                console.print(f"[yellow]Warning: Failed to update local commit.json: {e}[/yellow]")
                console.print(f"[yellow]Server hash and local hash may differ. Consider re-pushing.[/yellow]\n")
            
            # Record commit mapping for reconciliation
            commit_hash_map[commit_hash] = returned_commit_hash

            # Update parent for next commit
            parent_commit_hash = returned_commit_hash
            pushed_count += 1
        
        # Phase 4: Finalize and Commit Hash Reconciliation (User Decision 4)
        if manifest_negotiated and negotiation_data:
            try:
                finalize_resp = api_client.push_finalize(repo_hash, {
                    "pushSessionId": negotiation_data.get("pushSessionId"),
                    "repoHash": repo_hash,
                    "branchHash": branch_hash,
                    "completedCommits": list(commit_hash_map.keys())
                })
                if isinstance(finalize_resp, dict) and finalize_resp.get("commitHashMap"):
                    commit_hash_map.update(finalize_resp["commitHashMap"])
            except Exception:
                pass

        if commit_hash_map:
            apply_commit_hash_reconciliation(commit_hash_map, parent_commit_hash, target_branch_name)

        # Summary
        console.print(f"[bold green]═══════════════════════════════════[/bold green]")
        console.print(f"[bold green]✓ Push complete![/bold green]")
        console.print(f"  [dim]Branch: {target_branch_name}[/dim]")
        console.print(f"  [dim]Commits pushed: {pushed_count}/{len(commits_to_push)}[/dim]")
        console.print(f"  [dim]Latest commit: {parent_commit_hash[:16]}...[/dim]")
        console.print(f"[bold green]═══════════════════════════════════[/bold green]")
        
        # Update .flair/HEAD with new commit hash
        if pushed_count > 0:
            flair_dir = _get_flair_dir()
            head_file = flair_dir / "HEAD"
            
            head_data = {
                "currentBranch": target_branch_name,
                "branchHash": branch_hash,
                "latestCommitHash": parent_commit_hash,
                "previousCommit": parent_commit_hash
            }
            
            with open(head_file, 'w') as f:
                json.dump(head_data, f, indent=2)
            
            console.print(f"\n[green]✓ HEAD updated[/green]")
            
            # Also track REMOTE_HEAD for reset command
            remote_head_file = flair_dir / "REMOTE_HEAD"
            with open(remote_head_file, 'w') as f:
                json.dump(head_data, f, indent=2)

            settings = _load_repo_settings()
            retention_limit = settings.get("commitRetentionLimit", 25)
            if isinstance(retention_limit, int) and retention_limit > 0:
                deleted_count = _garbage_collect_local_commits(retention_limit)
                if deleted_count > 0:
                    console.print(f"[dim]Garbage collected {deleted_count} old local commit(s)[/dim]")
        
    except typer.Exit:
        raise
    except (httpx.HTTPStatusError, httpx.RequestError, httpx.ConnectError, httpx.TimeoutException) as e:
        handle_cli_exception(e, context="Push", default_exit_code=3)
    except Exception as e:
        handle_cli_exception(e, context="Push", default_exit_code=1)
