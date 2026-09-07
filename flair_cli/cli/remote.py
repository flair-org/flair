"""
Remote repository management commands:
Connect local directory to existing remote Flair repositories (like 'git remote add origin').
"""
from __future__ import annotations
import typer
from rich.console import Console
from rich.table import Table
from pathlib import Path
import json
import httpx

from ..api import client as api_client
from ..api.utils import _base_url

app = typer.Typer(help="Manage remote repository connections")
console = Console()


def _get_flair_dir() -> Path:
    flair_dir = Path.cwd() / ".flair"
    return flair_dir


@app.command("add")
def add_remote(
    name: str = typer.Argument("origin", help="Name of the remote (defaults to 'origin')"),
    repo_hash: str = typer.Argument(..., help="Repository hash from web or backend"),
):
    """Link current directory to an existing remote repository.

    Example:
        flair remote add origin <repo_hash>
    """
    try:
        console.print(f"[cyan]Connecting to remote repository {repo_hash}...[/cyan]")
        clone_data = api_client.clone_repository(repo_hash)
        repo_info = clone_data.get("repo", {})
        branches = clone_data.get("branches", [])
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            console.print("[yellow]Authentication required. Run 'flair auth login' to sign in with your wallet before connecting to a remote repository.[/yellow]")
        else:
            console.print(f"[red]Error contacting remote: HTTP {e.response.status_code}[/red]")
        raise typer.Exit(code=1)
    except Exception as e:
        console.print(f"[red]Failed to fetch repository details: {e}[/red]")
        raise typer.Exit(code=1)

    if not repo_info:
        console.print(f"[red]Repository '{repo_hash}' not found on remote.[/red]")
        raise typer.Exit(code=1)

    repo_name = repo_info.get("name")
    resolved_hash = repo_info.get("hash") or repo_info.get("repoHash")

    # Ensure local directory structure
    flair_dir = _get_flair_dir()
    flair_dir.mkdir(parents=True, exist_ok=True)
    (flair_dir / ".params").mkdir(exist_ok=True)
    (flair_dir / ".zkp").mkdir(exist_ok=True)
    (flair_dir / ".prev_params").mkdir(exist_ok=True)
    (flair_dir / ".prev_zkp").mkdir(exist_ok=True)
    (flair_dir / ".local_commits").mkdir(exist_ok=True)

    # Save repo metadata
    repo_file = flair_dir / "repo.json"
    repo_data = {
        "name": repo_name,
        "hash": resolved_hash,
        "id": repo_info.get("id") or resolved_hash,
        "owner": repo_info.get("ownerAddress") or repo_info.get("owner"),
        "metadata": repo_info.get("metadata", {}),
        "baseModel": repo_info.get("baseModel"),
        "baseModelHash": repo_info.get("baseModelHash"),
        "remote": name,
    }
    with open(repo_file, "w") as f:
        json.dump(repo_data, f, indent=2)

    # Save branches and HEAD
    branches_file = flair_dir / "branches.json"
    with open(branches_file, "w") as f:
        json.dump(branches, f, indent=2)

    selected_branch = None
    default_hash = repo_info.get("defaultBranchHash")
    if default_hash:
        selected_branch = next((b for b in branches if b.get("branchHash") == default_hash), None)
    if not selected_branch and branches:
        selected_branch = branches[0]

    head_file = flair_dir / "HEAD"
    if selected_branch:
        latest_commit = selected_branch.get("latestCommit") or {}
        head_data = {
            "currentBranch": selected_branch.get("name", "main"),
            "branchHash": selected_branch.get("branchHash"),
            "description": selected_branch.get("description"),
            "previousCommit": latest_commit.get("commitHash"),
        }
    else:
        head_data = {
            "currentBranch": "main",
            "branchHash": None,
            "description": "Default main branch",
            "previousCommit": None,
        }

    with open(head_file, "w") as f:
        json.dump(head_data, f, indent=2)

    # Ensure config and ignore exist
    settings_file = Path.cwd() / "config.yaml"
    if not settings_file.exists():
        with open(settings_file, "w") as f:
            f.write("commitRetentionLimit: 25\n")

    ignore_file = Path.cwd() / ".flairignore"
    if not ignore_file.exists():
        with open(ignore_file, "w") as f:
            f.write("__pycache__/\n*.pyc\n.venv/\nvenv/\n.env\n*.tmp\n")

    console.print(f"[bold green]✓ Remote '{name}' added successfully![/bold green]")
    console.print(f"  Repository: [cyan]{repo_name}[/cyan] ({resolved_hash})")
    console.print(f"  Branch:     [green]{head_data['currentBranch']}[/green]")
    console.print(f"  Remote URL: [dim]{_base_url()}[/dim]")
    console.print("\n[dim]Next steps:[/dim]")
    console.print("  1. Upload model:    flair basemodel upload <weights_file>")
    console.print("  2. Stage params:    flair params <weights_file>")
    console.print("  3. Commit snapshot: flair commit -m \"initial model checkpoint\"")
    console.print("  4. Push to remote:  flair push")


@app.command("view")
@app.command("list")
def list_remotes():
    """List configured remotes for the current repository."""
    flair_dir = _get_flair_dir()
    repo_file = flair_dir / "repo.json"

    if not repo_file.exists():
        console.print("[yellow]No repository configured. Run 'flair init' or 'flair remote add' first.[/yellow]")
        return

    try:
        with open(repo_file, "r") as f:
            repo_data = json.load(f)
    except Exception as e:
        console.print(f"[red]Could not read .flair/repo.json: {e}[/red]")
        return

    remote_name = repo_data.get("remote", "origin")
    repo_hash = repo_data.get("hash") or repo_data.get("repoHash")
    repo_name = repo_data.get("name")

    table = Table(title="Configured Remotes")
    table.add_column("Remote Name", style="cyan")
    table.add_column("Repository Name", style="green")
    table.add_column("Repository Hash", style="magenta")
    table.add_column("Backend URL", style="dim")

    table.add_row(remote_name, str(repo_name), str(repo_hash), _base_url())
    console.print(table)


@app.command("remove")
def remove_remote(
    name: str = typer.Argument("origin", help="Name of the remote to remove"),
):
    """Remove remote repository reference."""
    flair_dir = _get_flair_dir()
    repo_file = flair_dir / "repo.json"

    if not repo_file.exists():
        console.print("[yellow]No repository configured.[/yellow]")
        return

    try:
        with open(repo_file, "r") as f:
            repo_data = json.load(f)
        repo_data.pop("remote", None)
        with open(repo_file, "w") as f:
            json.dump(repo_data, f, indent=2)
        console.print(f"[green]✓ Removed remote '{name}'.[/green]")
    except Exception as e:
        console.print(f"[red]Failed to remove remote: {e}[/red]")
