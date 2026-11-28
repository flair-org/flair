"""
Initialize a Flair repository in the current directory.
Creates a .flair folder locally with offline UUID identities.
"""

from __future__ import annotations
import typer
from rich.console import Console
from pathlib import Path
import json
import uuid
from datetime import datetime, timezone

from ..api import client as api_client
from ..core.config import ALLOWED_BASE_MODEL_EXTENSIONS
from ..core.session import load_session
from .utils.errors import handle_cli_exception

app = typer.Typer()
console = Console()

def _find_base_model_files() -> list[Path]:
    """Find base model files in current directory."""
    files = []
    exclude_names = {"config.yaml"}
    for ext in ALLOWED_BASE_MODEL_EXTENSIONS:
        for p in Path.cwd().glob(f"*{ext}"):
            if p.is_file() and p.name not in exclude_names:
                files.append(p)
    return files


@app.command()
def init(
    name: str = typer.Option(None, help="Repository name (defaults to current folder name)"),
    description: str = typer.Option("", help="Short description"),
    use_case: str = typer.Option("General", "--use-case", help="Use case description"),
    framework: str = typer.Option("PyTorch", help="ML framework (PyTorch / TensorFlow / Tensorflow)"),
    skip_base_model: bool = typer.Option(False, "--skip-base-model", help="Skip base model upload prompt")
):
    """Initialize a new local-first Flair repository in the current directory."""
    flair_dir = Path.cwd() / ".flair"

    # If already initialized, show repo info and exit
    if flair_dir.exists():
        console.print("[yellow]✓ This directory is already initialized as a Flair repository[/yellow]")
        repo_file = flair_dir / "repo.json"
        if repo_file.exists():
            try:
                with open(repo_file, "r") as f:
                    repo_data = json.load(f)
                console.print(f"Repository: {repo_data.get('name')} (ID: {repo_data.get('id')})")
            except Exception:
                console.print("[dim]Existing .flair/repo.json could not be read[/dim]")
        return

    # Check authentication before initializing
    current_session = load_session()
    if not current_session or not current_session.token:
        console.print("[red]Not authenticated. Run 'flair auth login' first.[/red]")
        raise typer.Exit(code=1)

    owner = current_session.principal or current_session.wallet_address or "local"

    # Default repo name to current folder name
    if not name:
        name = Path.cwd().name
        console.print(f"[dim]Using current folder name as repository name: {name}[/dim]")

    try:
        repo_uuid = str(uuid.uuid4())
        branch_uuid = str(uuid.uuid4())

        flair_dir.mkdir(exist_ok=True)
        (flair_dir / ".params").mkdir(exist_ok=True)
        (flair_dir / ".zkp").mkdir(exist_ok=True)
        (flair_dir / ".prev_params").mkdir(exist_ok=True)
        (flair_dir / ".prev_zkp").mkdir(exist_ok=True)
        (flair_dir / ".local_commits").mkdir(exist_ok=True)

        repo_data = {
            "id": repo_uuid,
            "hash": repo_uuid,
            "repoHash": repo_uuid,
            "name": name,
            "owner": owner,
            "ownerAddress": owner,
            "metadata": {
                "name": name,
                "creator": owner,
                "description": description,
                "useCase": use_case,
                "framework": framework,
            },
            "defaultBranch": "main",
            "defaultBranchHash": branch_uuid,
            "remote": None,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }

        repo_file = flair_dir / "repo.json"
        with open(repo_file, "w") as f:
            json.dump(repo_data, f, indent=2)

        # Initialize HEAD file
        head_file = flair_dir / "HEAD"
        head_data = {
            "currentBranch": "main",
            "branchHash": branch_uuid,
            "previousCommit": None
        }
        with open(head_file, "w") as f:
            json.dump(head_data, f, indent=2)

        # Initialize branches file with default branch
        branches_file = flair_dir / "branches.json"
        branches_data = [
            {
                "name": "main",
                "branchHash": branch_uuid,
                "parentBranchHash": None,
                "description": "Default main branch",
                "createdAt": datetime.now(timezone.utc).isoformat()
            }
        ]
        with open(branches_file, "w") as f:
            json.dump(branches_data, f, indent=2)

        # Create basic ignore file
        ignore_file = Path.cwd() / ".flairignore"
        if not ignore_file.exists():
            with open(ignore_file, "w") as f:
                f.write("__pycache__/\n*.pyc\n.venv/\nvenv/\n.env\n*.tmp\n")

        settings_file = Path.cwd() / "config.yaml"
        if not settings_file.exists():
            settings_content = "commitRetentionLimit: 25\n"
            with open(settings_file, "w") as f:
                f.write(settings_content)

        console.print("✓ Repository initialized successfully!", style="green")
        console.print(f"  Name: {repo_data.get('name')}")
        console.print(f"  Hash: {repo_data.get('repoHash')}")
        console.print(f"  Location: {flair_dir}")
        console.print("[dim]Local-first repository initialized. No remote network calls made.[/dim]")

        # Check for base model files without making remote upload calls
        if not skip_base_model:
            base_model_files = _find_base_model_files()
            if base_model_files:
                console.print(f"\n[cyan]Found {len(base_model_files)} base model file(s) in current directory:[/cyan]")
                for idx, file in enumerate(base_model_files, 1):
                    console.print(f"  {idx}. {file.name}")
                console.print("[dim]Base model detected. It will be uploaded when pushing to a remote repository.[/dim]")

    except typer.Exit:
        raise
    except Exception as e:
        handle_cli_exception(e, context="Repository initialization", endpoint="local-init", default_exit_code=1)
