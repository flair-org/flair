"""
Entry point for the Flair CLI.
This creates a Typer app and mounts subcommand groups from the `cli` package.
"""
import os
import sys

# Silence verbose TensorFlow C++ runtime and oneDNN notices
os.environ.setdefault("TF_ENABLE_ONEDNN_OPTS", "0")
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from typing import Optional
import typer
from rich.console import Console

from flair_cli.cli import auth, config, init, clone, remote, basemodel, branch, add, zkp, push, pull, params, new, commit, revert, reset, metrics, merge, status as status_cmd, log as log_cmd, diff as diff_cmd

app = typer.Typer(help="Flair - versioning Machine Learning models")
console = Console()

# Mount command groups (subcommands)
app.add_typer(auth.app, name="auth", help="Authentication commands (SIWS login + SSH setup)")
app.add_typer(config.app, name="config", help="Configuration management")
app.add_typer(remote.app, name="remote", help="Manage remote repository connections")
app.add_typer(basemodel.app, name="basemodel", help="Manage base models")
app.add_typer(params.app, name="params", help="Extract and create model parameters")
app.add_typer(metrics.app, name="metrics", help="Stage and manage commit metrics")
app.add_typer(merge.app, name="merge", help="Create lineage-based merge candidates")
app.add_typer(zkp.app, name="zkp", help="Zero-Knowledge Proof operations")

# Mount top-level Git-like commands
app.command(name="init", help="Initialize repository in current directory")(init.init)
app.command(name="clone", help="Clone a remote repository")(clone.clone)
app.command(name="new", help="Create sample model files")(new.new)
app.command(name="add", help="Create a new local commit")(add.add)
app.command(name="commit", help="Finalize commit with message and determine type")(commit.finalize)
app.command(name="push", help="Push commits to remote repository")(push.push)
app.command(name="pull", help="Pull latest commit statuses from remote")(pull.pull)
app.command(name="revert", help="Revert to previous commit")(revert.revert)
app.command(name="reset", help="Reset HEAD to previous local commit")(reset.reset)
app.command(name="branch", help="List all branches or create a new branch")(branch.list_or_create_branch)
app.command(name="checkout", help="Switch to a different branch with intelligent artifact caching")(branch.checkout)


@app.command()
def status():
    """Show branch, head, local commit completeness, and unpushed commit count."""
    status_cmd.status()


@app.command()
def log(
    graph: bool = typer.Option(False, "--graph", help="Show a simple graph-style prefix"),
    branch: str = typer.Option(None, "--branch", help="Show history for a specific branch"),
    limit: int = typer.Option(50, "--limit", help="Maximum number of commits to display"),
):
    """Show commit history, newest first."""
    log_cmd.log(graph=graph, branch=branch, limit=limit)


@app.command()
def diff(
    commit_a: str = typer.Argument(..., help="First commit hash to compare"),
    commit_b: str = typer.Argument(..., help="Second commit hash to compare"),
    detailed: bool = typer.Option(False, "--detailed", help="Show all layers (not just top 5)"),
    json: bool = typer.Option(False, "--json", help="Output machine-readable JSON"),
):
    """Compare two model commits and produce a semantic summary of changes.
    
    This command supports federated learning and model reproducibility workflows.
    It detects architecture changes, computes overall statistics, per-layer diffs,
    and provides merge readiness assessment.
    
    Example:
        flair diff 9f2c... b71e...
        flair diff <commitA> <commitB> --detailed
        flair diff <commitA> <commitB> --json
    """
    diff_cmd.diff(commit_a=commit_a, commit_b=commit_b, detailed=detailed, json_output=json)

@app.callback(invoke_without_command=True)
def main(ctx: typer.Context, json: Optional[bool] = typer.Option(False, "--json", help="Output machine-friendly JSON")):
    """Flair CLI — record-only model repository and commit ledger for ML model evolution.
    Note: Flair never performs training or stores private keys.
    """
    if ctx.invoked_subcommand is None:
        console.print("Use 'flair --help' to see available commands.")

if __name__ == "__main__":
    app()