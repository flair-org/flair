"""
Push command: 4-Phase Push Manifest Handshake & Batch Reconciliation.
Executes atomic, offline-first remote synchronization:
1. Negotiates manifest (POST /push/manifest).
2. Terminal handshake if up-to-date.
3. Serially uploads only missing artifacts using pre-authorized tokens.
4. Batch finalizes signed envelopes atomically (POST /push/finalize).
5. Applies journaled commit hash reconciliation to local commits and DAG.

Also provides seqpush for backward compatibility and fallback diagnostic use.
"""
from __future__ import annotations
import typer
from rich.console import Console
from pathlib import Path
import json
import httpx
import hashlib
import shutil
import sys
from datetime import datetime, timezone

from ..api import client as api_client
from ..api.utils import _base_url, _client_with_auth
from ..core import session
from .utils.local_commits import _get_all_local_commits, _get_flair_dir, _get_head_info
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
    """Verify merkle tree chain integrity."""
    GENESIS_COMMIT_HASH = "_GENESIS_COMMIT_"
    if not commits_to_push:
        return True, ""
    
    for idx, (commit_data, commit_dir) in enumerate(commits_to_push):
        commit_hash = commit_data.get("commitHash") or ""
        previous_hash = commit_data.get("previousCommitHash")
        
        if idx == 0:
            valid_parents = {GENESIS_COMMIT_HASH, None, ""}
            if remote_head_hash:
                valid_parents.add(remote_head_hash)
            if previous_hash and previous_hash not in valid_parents:
                prev_display = str(previous_hash)[:16]
                return False, f"First commit {commit_hash[:16]}... should reference Genesis or remote head ({str(remote_head_hash)[:16]}...) but references {prev_display}..."
        else:
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
            return None
        raise
    except Exception:
        return None


def _garbage_collect_local_commits(retention_limit: int) -> int:
    """Delete local commits older than the retention limit."""
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


def _load_branches() -> list[dict]:
    """Load local branches from .flair/branches.json"""
    flair_dir = _get_flair_dir()
    branches_file = flair_dir / "branches.json"
    if not branches_file.exists():
        return []
    try:
        with open(branches_file, "r") as f:
            return json.load(f)
    except Exception:
        return []


def _can_publish_merge_commit(repo_config: dict, branch_name: str, wallet_address: str) -> tuple[bool, str]:
    """Check whether current wallet can publish merge checkpoints on target branch."""
    metadata = repo_config.get("metadata", {}) if isinstance(repo_config, dict) else {}
    policy = metadata.get("mergePolicy") or repo_config.get("mergePolicy")
    if not isinstance(policy, dict):
        return True, ""
    protected_branches = policy.get("protectedBranches") or []
    if protected_branches and branch_name not in protected_branches:
        return True, ""
    authorized = policy.get("authorizedMergers") or policy.get("authorizedWallets") or []
    if not authorized:
        return False, "Merge checkpoints are protected but no authorized mergers are configured."
    if wallet_address not in authorized:
        return False, "Permission denied. Merge checkpoints require maintainer access."
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
                if not stripped or stripped.startswith("#") or ":" not in stripped:
                    continue
                key, value = stripped.split(":", 1)
                key, value = key.strip(), value.strip()
                settings[key] = int(value) if value.isdigit() else value
    except Exception:
        return {"commitRetentionLimit": 25}
    return settings if "commitRetentionLimit" in settings else {**settings, "commitRetentionLimit": 25}


def _check_remote_repo_exists(repo_hash: str) -> bool:
    """Check if remote repository exists on backend."""
    try:
        with _client_with_auth() as client:
            response = client.get(f"{_base_url()}/api/repo/hash/{repo_hash}")
            return response.status_code == 200
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
    """Push commits to remote repository via negotiated Push Manifest Handshake.
    
    Executes an atomic 4-phase synchronization:
    1. Submits push manifest to negotiate missing commits and tokens.
    2. Immediately reconciles and exits if branch is already up-to-date.
    3. Serially uploads only missing parameter and ZKP artifacts.
    4. Batch finalizes signed envelopes atomically and updates branch HEAD.
    5. Reconciles local UUID commits with authoritative SHA-256 hashes.
    """
    try:
        recover_pending_reconciliation_if_needed()
        all_local_commits = _get_all_local_commits()
        if not all_local_commits:
            console.print("[red]No local commits found. Run 'flair add' first.[/red]")
            raise typer.Exit(1)
        
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
        
        console.print("[dim]Verifying local commit chain integrity...[/dim]")
        chain_valid, chain_error = _verify_local_commit_chain(commits_to_push, remote_head_hash=None)
        if not chain_valid:
            console.print(f"[red]✗ Chain integrity check failed: {chain_error}[/red]")
            console.print(f"[yellow]Cannot push commits with broken chain. Fix and try again.[/yellow]")
            raise typer.Exit(code=1)
        console.print("[green]✓ Local chain verified[/green]")

        if not _check_remote_repo_exists(repo_hash):
            console.print(f"[red]Error: Remote repository '{repo_hash}' not found on remote.[/red]")
            console.print("[yellow]Please create or link the repository on FlairHub first using:[/yellow]")
            console.print(f"  flair remote add origin <remote_repo_hash>\n")
            raise typer.Exit(code=1)
        
        head_info = _get_head_info()
        target_branch_name = branch_name or (head_info.get("currentBranch") if head_info else "main")
        
        # Lineage resolution (Q19 & Q22): retrieve parentBranchHash from .flair/branches.json
        local_branches = _load_branches()
        target_branch_rec = next((b for b in local_branches if b.get("name") == target_branch_name), None)
        local_branch_hash = target_branch_rec.get("branchHash") if target_branch_rec else None
        # Root branch ('main') or branches without recorded parent must pass None
        parent_branch_hash = None
        if target_branch_rec and target_branch_name != "main":
            parent_branch_hash = target_branch_rec.get("parentBranchHash")

        console.print(f"\n[cyan]Target branch: {target_branch_name}[/cyan]")
        
        current_session = session.load_session()
        user_principal = current_session.principal or current_session.wallet_address if current_session else None
        if not current_session or not user_principal:
            console.print("[red]Authenticated principal not found. Run 'flair auth login'.[/red]")
            raise typer.Exit(code=2)
        wallet_address = user_principal

        for idx, (c_data, c_dir) in enumerate(commits_to_push, 1):
            merged_parents = c_data.get("mergedParentHashes")
            is_merge = bool(merged_parents) or c_data.get("status") == "MERGER"
            if is_merge:
                allowed, denial_reason = _can_publish_merge_commit(repo_config, target_branch_name, wallet_address)
                if not allowed:
                    console.print(f"[red]✗ Commit {idx}: {denial_reason}[/red]")
                    raise typer.Exit(code=2)

        # Load SSH identity for commit envelope signing
        with _client_with_auth() as client:
            response = client.get(f"{_base_url()}/api/user/keys")
            response.raise_for_status()
            registered_keys = response.json().get("data", [])

        registered_fingerprints = {
            key.get("fingerprint") for key in registered_keys if isinstance(key, dict) and key.get("fingerprint")
        }
        agent_identities = load_ssh_agent_identities()
        if not agent_identities:
            console.print("[red]No SSH keys are loaded in ssh-agent. Load key with ssh-add.[/red]")
            raise typer.Exit(code=1)

        ssh_identity = None
        for fingerprint in registered_fingerprints:
            ssh_identity = find_agent_identity_by_fingerprint(fingerprint, agent_identities)
            if ssh_identity:
                break
        if not ssh_identity:
            console.print("[red]No ssh-agent key matches a registered Flair SSH public key.[/red]")
            raise typer.Exit(code=1)

        # -------------------------------------------------------------
        # Phase 1: Push Manifest Handshake Negotiation
        # -------------------------------------------------------------
        console.print("[cyan]Phase 1/4: Negotiating push manifest handshake...[/cyan]")
        remote_head_hash = None
        try:
            if local_branch_hash:
                remote_head_hash = _get_remote_latest_commit(repo_hash, local_branch_hash)
        except Exception:
            remote_head_hash = None

        manifest_payload = {
            "clientVersion": "1.0.0",
            "repoHash": repo_hash,
            "targetBranch": {
                "name": target_branch_name,
                "branchHash": local_branch_hash,
                "parentBranchHash": parent_branch_hash,
                "description": f"Push to {target_branch_name}"
            },
            "remoteHeadCommitHash": remote_head_hash,
            "commits": [
                {
                    "localCommitHash": c_data.get("commitHash"),
                    "previousCommitHash": c_data.get("previousCommitHash"),
                    "parentCommitHashes": (
                        sorted(c_data.get("mergedParentHashes"))
                        if c_data.get("mergedParentHashes")
                        else [c_data.get("previousCommitHash")] if c_data.get("previousCommitHash") else None
                    ),
                    "commitType": "CHECKPOINT" if c_data.get("commitType") in ("CHECKPOINT", "MERGE") else "DELTA",
                    "message": c_data.get("message", ""),
                    "timestamp": c_data.get("timestamp"),
                    "paramHash": (
                        _compute_param_hash(c_dir / c_data["params"]["file"])
                        if c_data.get("params") and (c_dir / c_data["params"]["file"]).exists()
                        else None
                    ),
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
            if negotiation_data and isinstance(negotiation_data, dict) and negotiation_data.get("pushSessionId"):
                manifest_negotiated = True
        except Exception:
            manifest_negotiated = False

        if not manifest_negotiated or not negotiation_data or not isinstance(negotiation_data, dict) or not negotiation_data.get("pushSessionId"):
            console.print("[dim]Backend does not support manifest handshake. Falling back to sequential push...[/dim]")
            return seqpush(branch_name=branch_name, upstream=upstream)

        push_session_id = negotiation_data.get("pushSessionId")
        branch_hash = negotiation_data.get("branchHash") or local_branch_hash
        up_to_date = negotiation_data.get("upToDate", False)
        existing_commit_hash_map: dict[str, str] = negotiation_data.get("existingCommitHashMap") or {}
        missing_commits_desc = negotiation_data.get("missingCommits") or []

        console.print(f"[green]✓ Manifest negotiated (Session: {push_session_id[:16]}...)[/green]")

        # -------------------------------------------------------------
        # Phase 2: Terminal Handshake on Up-to-Date (Q18)
        # -------------------------------------------------------------
        if up_to_date or len(missing_commits_desc) == 0:
            console.print("[green]✓ Remote branch is already up to date.[/green]")
            if existing_commit_hash_map:
                console.print("[dim]Applying local hash reconciliation for existing remote commits...[/dim]")
                apply_commit_hash_reconciliation(
                    existing_commit_hash_map,
                    branch_head=None,
                    target_branch=target_branch_name
                )
            raise typer.Exit(0)

        # -------------------------------------------------------------
        # Phase 3: Serial Upload of Missing Artifacts
        # -------------------------------------------------------------
        console.print(f"\n[cyan]Phase 2/4: Uploading missing artifacts for {len(missing_commits_desc)} commit(s)...[/cyan]")
        missing_desc_by_local_id = {item["localCommitHash"]: item for item in missing_commits_desc}
        commits_by_local_id = {c_data["commitHash"]: (c_data, c_dir) for c_data, c_dir in commits_to_push}

        staged_upload_receipts: dict[str, dict] = {}
        framework = repo_config.get("metadata", {}).get("framework") or repo_config.get("framework", "unknown")

        for idx, missing_item in enumerate(missing_commits_desc, 1):
            local_id = missing_item["localCommitHash"]
            if local_id not in commits_by_local_id:
                console.print(f"[red]✗ Unknown missing commit: {local_id}[/red]")
                raise typer.Exit(code=1)

            c_data, c_dir = commits_by_local_id[local_id]
            commit_type = "CHECKPOINT" if c_data.get("commitType") in ("CHECKPOINT", "MERGE") else "DELTA"
            merged_parent_hashes = c_data.get("mergedParentHashes")
            is_merge_commit = bool(merged_parent_hashes) or c_data.get("status") == "MERGER"

            if is_merge_commit:
                allowed, denial_reason = _can_publish_merge_commit(repo_config, target_branch_name, wallet_address)
                if not allowed:
                    console.print(f"[red]✗ Commit {idx}: {denial_reason}[/red]")
                    raise typer.Exit(code=2)

            console.print(f"[dim]Commit {idx}/{len(missing_commits_desc)}: {local_id[:16]}... ({commit_type})[/dim]")
            session_id = missing_item["sessionId"]
            initiate_token = missing_item["initiateToken"]

            # 1. ZKP Proof Upload (if needed and not already supplied by retry receipt)
            zkml_receipt_token = missing_item.get("zkmlReceiptToken")
            if missing_item.get("needsZkp") and not zkml_receipt_token:
                zkp_info = c_data.get("zkp") or {}
                proof_file = c_dir / zkp_info.get("proof_file", "proof.zlib")
                vk_file = c_dir / zkp_info.get("verification_key_file", "verification_key.zlib")
                settings_file = c_dir / zkp_info.get("settings_file", "settings.zlib")

                if not all([proof_file.exists(), vk_file.exists(), settings_file.exists()]):
                    console.print(f"[red]✗ Commit {idx}: ZKP files missing for non-checkpoint commit.[/red]")
                    raise typer.Exit(code=1)

                with _client_with_auth() as client:
                    data = {
                        "sessionId": session_id,
                        "initiateToken": initiate_token,
                        "zkmlToken": missing_item.get("zkmlToken", "")
                    }
                    with open(proof_file, "rb") as f_p, open(settings_file, "rb") as f_s, open(vk_file, "rb") as f_v:
                        files = {
                            "proof": ("proof.zlib", f_p.read(), "application/octet-stream"),
                            "settings": ("settings.zlib", f_s.read(), "application/octet-stream"),
                            "verification_key": ("verification_key.zlib", f_v.read(), "application/octet-stream")
                        }
                        r_zkp = client.post(
                            f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/zkml-upload",
                            files=files,
                            data=data
                        )
                        r_zkp.raise_for_status()
                        zkml_receipt_token = r_zkp.json().get("zkmlReceiptToken")
            elif commit_type == "CHECKPOINT" and not zkml_receipt_token:
                # Checkpoint proof upload
                with _client_with_auth() as client:
                    r_zkp = client.post(
                        f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/zkml-upload",
                        data={"sessionId": session_id, "initiateToken": initiate_token, "zkmlToken": missing_item.get("zkmlToken", "")}
                    )
                    r_zkp.raise_for_status()
                    zkml_receipt_token = r_zkp.json().get("zkmlReceiptToken")

            if not zkml_receipt_token:
                console.print(f"[red]✗ Commit {idx}: Failed to obtain ZKML receipt token[/red]")
                raise typer.Exit(code=1)

            # 2. Parameters Upload (if needed and not already supplied by retry receipt)
            params_receipt_token = missing_item.get("paramsReceiptToken")
            params_ipfs_id = missing_item.get("paramsIpfsId")

            params_info = c_data.get("params") or {}
            params_file = c_dir / params_info.get("file", "params.pt")
            if not params_file.exists():
                console.print(f"[red]✗ Commit {idx}: Parameters file not found ({params_file})[/red]")
                raise typer.Exit(code=1)

            param_hash = _compute_param_hash(params_file)
            if missing_item.get("needsParams") and not params_receipt_token:
                with _client_with_auth() as client:
                    with open(params_file, "rb") as pf:
                        files = {"params": (params_file.name, pf.read(), "application/octet-stream")}
                    data = {
                        "sessionId": session_id,
                        "initiateToken": initiate_token,
                        "zkmlReceiptToken": zkml_receipt_token,
                        "paramHash": param_hash
                    }
                    r_params = client.post(
                        f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/params-upload",
                        files=files,
                        data=data
                    )
                    r_params.raise_for_status()
                    p_data = r_params.json()
                    params_receipt_token = p_data.get("paramsReceiptToken")
                    params_ipfs_id = p_data.get("paramsIpfsId")

            if not params_receipt_token:
                console.print(f"[red]✗ Commit {idx}: Failed to obtain parameters receipt token[/red]")
                raise typer.Exit(code=1)

            staged_upload_receipts[local_id] = {
                "sessionId": session_id,
                "initiateToken": initiate_token,
                "zkmlReceiptToken": zkml_receipt_token,
                "paramsReceiptToken": params_receipt_token,
                "paramsIpfsId": params_ipfs_id or "",
                "paramHash": param_hash
            }

        console.print("[green]✓ All missing artifacts uploaded successfully[/green]")

        # -------------------------------------------------------------
        # Phase 4: Batch Envelope Signing & Finalization
        # -------------------------------------------------------------
        console.print("\n[cyan]Phase 3/4: Constructing signed envelopes and finalizing batch...[/cyan]")
        envelopes: list[dict] = []
        commit_id_to_authoritative_sha: dict[str, str] = dict(existing_commit_hash_map)

        for c_data, c_dir in commits_to_push:
            local_id = c_data.get("commitHash")
            if local_id not in staged_upload_receipts:
                continue

            receipts = staged_upload_receipts[local_id]
            commit_type = "CHECKPOINT" if c_data.get("commitType") in ("CHECKPOINT", "MERGE") else "DELTA"
            message = c_data.get("message", "")
            commit_metrics = c_data.get("metrics")
            session_jti = extract_jti_from_jwt(receipts["initiateToken"]) or receipts["sessionId"]
            signed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

            # Resolve parent hashes against known SHA-256 mapping
            previous_hash = c_data.get("previousCommitHash") or "_GENESIS_COMMIT_"
            resolved_parent_hash = commit_id_to_authoritative_sha.get(previous_hash, previous_hash)

            raw_merged_parents = c_data.get("mergedParentHashes")
            if raw_merged_parents:
                canonical_merged_parents = sorted([commit_id_to_authoritative_sha.get(p, p) for p in raw_merged_parents])
            else:
                canonical_merged_parents = None

            canonical_payload = build_canonical_payload(
                session_jti=session_jti,
                signed_at=signed_at,
                params_ipfs_id=receipts["paramsIpfsId"],
                param_hash=receipts["paramHash"],
                previous_commit_hash=resolved_parent_hash,
                architecture=framework,
                commit_type=commit_type,
                message=message,
                metrics=commit_metrics,
                merged_parent_hashes=canonical_merged_parents,
            )

            commit_signature = sign_canonical_payload(canonical_payload, ssh_identity)
            if not commit_signature:
                console.print(f"[red]✗ Failed to sign commit envelope for {local_id[:16]}[/red]")
                raise typer.Exit(code=1)

            envelope = {
                "localCommitHash": local_id,
                "sessionId": receipts["sessionId"],
                "initiateToken": receipts["initiateToken"],
                "zkmlReceiptToken": receipts["zkmlReceiptToken"],
                "paramsReceiptToken": receipts["paramsReceiptToken"],
                "signedAt": signed_at,
                "message": message,
                "paramHash": receipts["paramHash"],
                "architecture": framework,
                "metrics": commit_metrics,
                "commitType": commit_type,
                "commitSignature": commit_signature,
                "sshKeyFingerprint": ssh_identity.fingerprint.replace("ssh:", "").strip(),
                "previousCommitHash": resolved_parent_hash,
            }
            if canonical_merged_parents:
                envelope["parentCommitHashes"] = canonical_merged_parents

            envelopes.append(envelope)

        finalize_payload = {
            "pushSessionId": push_session_id,
            "commits": envelopes
        }

        finalize_resp = api_client.push_finalize(repo_hash, finalize_payload)
        server_commit_map: dict[str, str] = finalize_resp.get("commitHashMap", {})
        new_branch_head: str = finalize_resp.get("branchHead", "")

        console.print(f"[green]✓ Batch finalized by backend (HEAD: {new_branch_head[:16]}...)[/green]")

        # -------------------------------------------------------------
        # Phase 5: Local Journaled Commit Hash Reconciliation
        # -------------------------------------------------------------
        console.print("\n[cyan]Phase 4/4: Reconciling commit hashes with authoritative SHA-256...[/cyan]")
        complete_commit_map = {**existing_commit_hash_map, **server_commit_map}
        apply_commit_hash_reconciliation(
            complete_commit_map,
            branch_head=new_branch_head,
            target_branch=target_branch_name
        )

        # Update .flair/HEAD and .flair/REMOTE_HEAD
        flair_dir = _get_flair_dir()
        head_data = {
            "currentBranch": target_branch_name,
            "branchHash": branch_hash,
            "latestCommitHash": new_branch_head,
            "previousCommit": new_branch_head
        }
        with open(flair_dir / "HEAD", 'w') as f:
            json.dump(head_data, f, indent=2)
        with open(flair_dir / "REMOTE_HEAD", 'w') as f:
            json.dump(head_data, f, indent=2)

        # Garbage collect local commits if configured
        settings = _load_repo_settings()
        retention_limit = settings.get("commitRetentionLimit", 25)
        if isinstance(retention_limit, int) and retention_limit > 0:
            deleted_count = _garbage_collect_local_commits(retention_limit)
            if deleted_count > 0:
                console.print(f"[dim]Garbage collected {deleted_count} old local commit(s)[/dim]")

        # Summary
        console.print(f"[bold green]════════════════════════════════════════════════[/bold green]")
        console.print(f"[bold green]✓ Push complete![/bold green]")
        console.print(f"  [dim]Branch: {target_branch_name}[/dim]")
        console.print(f"  [dim]Commits pushed: {len(envelopes)}/{len(commits_to_push)}[/dim]")
        console.print(f"  [dim]Authoritative HEAD: {new_branch_head[:16]}...[/dim]")
        console.print(f"[bold green]════════════════════════════════════════════════[/bold green]")

    except typer.Exit:
        raise
    except (httpx.HTTPStatusError, httpx.RequestError, httpx.ConnectError, httpx.TimeoutException) as e:
        handle_cli_exception(e, context="Push", default_exit_code=3)
    except Exception as e:
        handle_cli_exception(e, context="Push", default_exit_code=1)


@app.command(name="seqpush")
def seqpush(
    branch_name: str = typer.Argument(None, help="Branch name to push to"),
    upstream: str = typer.Option(None, "-u", "--set-upstream", help="Set upstream (always 'origin')")
):
    """Legacy sequential push: Uploads commits using the 5-step per-commit RPC sequence.
    
    Maintained for backward compatibility and fallback diagnostic use.
    For standard offline-first pushes with manifest negotiation, use 'flair push'.
    """
    _seq_mod = sys.modules.get("flair_cli.cli.seqpush")
    _c_auth = getattr(_seq_mod, "_client_with_auth", _client_with_auth) if _seq_mod else _client_with_auth
    _api = getattr(_seq_mod, "api_client", api_client) if _seq_mod else api_client
    _check_remote = getattr(_seq_mod, "_check_remote_repo_exists", _check_remote_repo_exists) if _seq_mod else _check_remote_repo_exists
    _remote_latest = getattr(_seq_mod, "_get_remote_latest_commit", _get_remote_latest_commit) if _seq_mod else _get_remote_latest_commit

    try:
        recover_pending_reconciliation_if_needed()
        all_local_commits = _get_all_local_commits()
        if not all_local_commits:
            console.print("[red]No local commits found. Run 'flair add' first.[/red]")
            raise typer.Exit(1)
        
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

        commits_to_push = []
        for commit_data, commit_dir in all_local_commits:
            if _is_commit_complete(commit_data, commit_dir):
                commits_to_push.append((commit_data, commit_dir))
            else:
                console.print(f"[dim]Skipping incomplete commit: {commit_data.get('commitHash', 'unknown')[:16]}...[/dim]")
                break
        
        if not commits_to_push:
            console.print("[yellow]No complete commits to push.[/yellow]")
            raise typer.Exit(0)
        
        console.print("[dim]Verifying local commit chain integrity...[/dim]")
        chain_valid, chain_error = _verify_local_commit_chain(commits_to_push, remote_head_hash=None)
        if not chain_valid:
            console.print(f"[red]✗ Chain integrity check failed: {chain_error}[/red]")
            raise typer.Exit(code=1)
        console.print("[green]✓ Local chain verified[/green]")

        if not _check_remote(repo_hash):
            console.print(f"[red]Error: Remote repository '{repo_hash}' not found on remote.[/red]")
            console.print("[yellow]Please create or link the repository on FlairHub first using:[/yellow]")
            console.print(f"  flair remote add origin <remote_repo_hash>\n")
            raise typer.Exit(code=1)
        
        head_info = _get_head_info()
        target_branch_name = branch_name or (head_info.get("currentBranch") if head_info else "main")
        console.print(f"\n[cyan][seqpush] Pushing sequentially to branch: {target_branch_name}[/cyan]")
        
        branch_data = None
        try:
            branch_data = _api.get_branch_by_name(repo_hash, target_branch_name)
        except Exception:
            pass
        
        if not branch_data or isinstance(branch_data, list):
            console.print(f"[yellow]Branch '{target_branch_name}' not found. Creating...[/yellow]")
            parent_branch_hash = head_info.get("branchHash") if head_info else None
            try:
                branch_data = _api.create_branch(
                    repo_hash,
                    target_branch_name,
                    parent_branch_hash,
                    f"Created via seqpush command"
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
        
        remote_head_hash = _remote_latest(repo_hash, branch_hash)
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
        
        console.print(f"[cyan]Pushing {len(commits_to_push)} commit(s) sequentially (5-step RPCs)...[/cyan]\n")

        current_session = session.load_session()
        user_principal = current_session.principal or current_session.wallet_address if current_session else None
        if not current_session or not user_principal:
            console.print("[red]Authenticated principal not found. Run 'flair auth login'.[/red]")
            raise typer.Exit(code=2)

        wallet_address = user_principal
        with _c_auth() as client:
            response = client.get(f"{_base_url()}/api/user/keys")
            response.raise_for_status()
            registered_keys = response.json().get("data", [])

        registered_fingerprints = {
            key.get("fingerprint") for key in registered_keys if isinstance(key, dict) and key.get("fingerprint")
        }
        agent_identities = load_ssh_agent_identities()
        if not agent_identities:
            console.print("[red]No SSH keys are loaded in ssh-agent. Load key with ssh-add.[/red]")
            raise typer.Exit(code=1)

        ssh_identity = None
        for fingerprint in registered_fingerprints:
            ssh_identity = find_agent_identity_by_fingerprint(fingerprint, agent_identities)
            if ssh_identity:
                break
        if not ssh_identity:
            console.print("[red]No ssh-agent key matches a registered Flair SSH public key.[/red]")
            raise typer.Exit(code=1)

        parent_commit_hash = remote_head_hash if remote_head_hash else "_GENESIS_COMMIT_"
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
                    raise typer.Exit(code=2)
            
            console.print(f"[bold cyan]═══ [seqpush] Commit {idx}/{len(commits_to_push)} ═══[/bold cyan]")
            params_info = commit_data.get("params")
            params_file = commit_dir / (params_info["file"] if params_info else "params.pt")
            if not params_file.exists():
                console.print(f"[red]✗ Commit {idx}: Parameters file not found[/red]")
                raise typer.Exit(code=1)

            zkp_info = commit_data.get("zkp")
            is_checkpoint = commit_type == "CHECKPOINT"
            zkp_files = {}
            if not is_checkpoint:
                if not zkp_info:
                    console.print(f"[red]✗ Commit {idx}: ZKP info missing[/red]")
                    raise typer.Exit(code=1)
                proof_file = commit_dir / zkp_info.get("proof_file", "proof.zlib")
                vk_file = commit_dir / zkp_info.get("verification_key_file", "verification_key.zlib")
                settings_file = commit_dir / zkp_info.get("settings_file", "settings.zlib")
                if not all([proof_file.exists(), vk_file.exists(), settings_file.exists()]):
                    console.print(f"[red]✗ Commit {idx}: ZKP files missing[/red]")
                    raise typer.Exit(code=1)
                zkp_files = {
                    "proof_file": proof_file,
                    "vk_file": vk_file,
                    "settings_file": settings_file,
                    "proof_cid": zkp_info.get("proof_cid"),
                    "vk_cid": zkp_info.get("verification_key_cid"),
                    "settings_cid": zkp_info.get("settings_cid"),
                }

            # Step 1: Initiate
            console.print("[cyan]Step 1/5: Initiating commit session...[/cyan]")
            with _c_auth() as client:
                response = client.post(
                    f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/initiate",
                    json={"parentCommitHash": parent_commit_hash}
                )
                response.raise_for_status()
                init_data = response.json()
            session_id = init_data.get("sessionId")
            initiate_token = init_data.get("initiateToken")

            # Step 2: ZKML check
            console.print("[cyan]Step 2/5: Checking ZKML proof uniqueness...[/cyan]")
            with _c_auth() as client:
                req_data = {"sessionId": session_id, "initiateToken": initiate_token, "isCheckpoint": is_checkpoint}
                if not is_checkpoint:
                    req_data.update({"proofCid": zkp_files["proof_cid"], "settingsCid": zkp_files["settings_cid"], "vkCid": zkp_files["vk_cid"]})
                response = client.post(f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/zkml-check", json=req_data)
                response.raise_for_status()
                zkml_check_data = response.json()
            if zkml_check_data.get("unique") is False:
                console.print(f"[red]✗ Commit {idx}: {zkml_check_data.get('message', 'Duplicate zkML proof detected')}[/red]")
                raise typer.Exit(code=1)
            zkml_token = zkml_check_data.get("zkmlToken")

            # Step 3: ZKML upload
            console.print("[cyan]Step 3/5: Uploading ZKML proofs...[/cyan]")
            with _c_auth() as client:
                data = {"sessionId": session_id, "initiateToken": initiate_token, "zkmlToken": zkml_token}
                if is_checkpoint:
                    response = client.post(f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/zkml-upload", data=data)
                else:
                    with open(zkp_files["proof_file"], "rb") as f_p, open(zkp_files["settings_file"], "rb") as f_s, open(zkp_files["vk_file"], "rb") as f_v:
                        files = {
                            "proof": ("proof.zlib", f_p.read(), "application/octet-stream"),
                            "settings": ("settings.zlib", f_s.read(), "application/octet-stream"),
                            "verification_key": ("verification_key.zlib", f_v.read(), "application/octet-stream")
                        }
                        response = client.post(f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/zkml-upload", files=files, data=data)
                response.raise_for_status()
                zkml_upload_data = response.json()
            zkml_receipt_token = zkml_upload_data.get("zkmlReceiptToken")

            # Step 4: Upload parameters
            console.print("[cyan]Step 4/5: Uploading parameters...[/cyan]")
            param_hash = _compute_param_hash(params_file)
            with _c_auth() as client:
                with open(params_file, "rb") as pf:
                    files = {"params": (params_file.name, pf.read(), "application/octet-stream")}
                data = {"sessionId": session_id, "initiateToken": initiate_token, "zkmlReceiptToken": zkml_receipt_token, "paramHash": param_hash}
                response = client.post(f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/params-upload", files=files, data=data)
                response.raise_for_status()
                params_upload_data = response.json()
            params_receipt_token = params_upload_data.get("paramsReceiptToken")

            # Step 5: Finalize
            console.print("[cyan]Step 5/5: Finalizing commit...[/cyan]")
            session_jti = extract_jti_from_jwt(initiate_token)
            signed_at = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            raw_merged_parents = commit_data.get("mergedParentHashes")
            canonical_merged_parents = sorted([commit_hash_map.get(p, p) for p in raw_merged_parents]) if raw_merged_parents else None

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
            if canonical_merged_parents:
                finalize_payload["mergedParentHashes"] = canonical_merged_parents

            with _c_auth() as client:
                response = client.post(f"{_base_url()}/api/repo/hash/{repo_hash}/branch/hash/{branch_hash}/commit/create/finalize", json=finalize_payload)
                response.raise_for_status()
                finalize_data = response.json()

            returned_commit_hash = (
                finalize_data.get("data", {}).get("commitHash")
                if isinstance(finalize_data.get("data"), dict)
                else finalize_data.get("commitHash")
            ) or str(session_id)
            console.print(f"[bold green]✓ Commit {idx} created (hash: {returned_commit_hash[:16]}...)[/bold green]")
            commit_hash_map[commit_hash] = returned_commit_hash
            parent_commit_hash = returned_commit_hash
            pushed_count += 1

        if commit_hash_map:
            apply_commit_hash_reconciliation(commit_hash_map, parent_commit_hash, target_branch_name)

        if pushed_count > 0:
            flair_dir = _get_flair_dir()
            head_data = {
                "currentBranch": target_branch_name,
                "branchHash": branch_hash,
                "latestCommitHash": parent_commit_hash,
                "previousCommit": parent_commit_hash
            }
            with open(flair_dir / "HEAD", 'w') as f:
                json.dump(head_data, f, indent=2)
            with open(flair_dir / "REMOTE_HEAD", 'w') as f:
                json.dump(head_data, f, indent=2)

        console.print(f"[bold green]✓ Push complete![/bold green]")
        console.print(f"[bold green]✓ Sequential push complete! ({pushed_count} commit(s))[/bold green]")
    except typer.Exit:
        raise
    except (httpx.HTTPStatusError, httpx.RequestError, httpx.ConnectError, httpx.TimeoutException) as e:
        handle_cli_exception(e, context="SeqPush", default_exit_code=3)
    except Exception as e:
        handle_cli_exception(e, context="SeqPush", default_exit_code=1)


__all__ = [
    "push",
    "seqpush",
    "app",
    "_verify_local_commit_chain",
    "_check_remote_repo_exists",
    "_load_repo_config",
    "_load_repo_settings",
    "_can_publish_merge_commit",
    "_compute_param_hash",
    "_garbage_collect_local_commits",
    "_get_remote_latest_commit",
    "load_ssh_agent_identities",
    "find_agent_identity_by_fingerprint",
    "sign_canonical_payload",
]
