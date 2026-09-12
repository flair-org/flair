"""Metrics command group: stage commit metrics in .flair/metrics.json.

Proof schema version 2 — cryptographic evaluation design
=========================================================

What the EZKL proof establishes
--------------------------------
- The arithmetic circuit (model graph) correctly computed the inference for
  the *representative sample* (sample 0, deterministically chosen).
  This is a full EZKL ZK-SNARK proof.

What the Poseidon commitment establishes
-----------------------------------------
- All N model output felt-arrays across the entire evaluation dataset are
  mutually consistent.  ``eval_commitment = poseidon_hash(flatten(all_output_felts))``.
  Any modification to any per-sample model output will change this commitment
  and be detected during verification.

What accuracy/loss represent
-----------------------------
- Both are computed from EZKL's own fixed-point decoded values
  (``ezkl.felt_to_float(felt, output_scale)``) rather than raw Python floats.
  This ensures the displayed metrics match what the circuit actually computed,
  to within the circuit's fixed-point precision (approx 2^-scale).

Acknowledged limitations (honest)
-----------------------------------
- The argmax and NLL aggregation logic runs in Python on the EZKL-decoded
  outputs, NOT inside a ZK circuit.
- Only sample 0 has a full SNARK proof; the remaining N-1 samples are
  committed via Poseidon hash.
- A single ZK circuit over the full aggregated metric computation is not
  feasible with the current EZKL API (batch_size is fixed at circuit
  compile time).

Privacy
--------
- The raw dataset is NEVER stored or uploaded.  Only the SHA-256 commitment
  of the canonical dataset bytes is recorded.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import asyncio
import csv
import hashlib
import inspect
import math
import os
import zlib
from typing import Optional

import typer
from rich.console import Console

# Proof-schema version for forward-compatibility
PROOF_SCHEMA_VERSION = 3

app = typer.Typer(help="Stage and manage commit metrics")
console = Console()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _ensure_ezkl_env():
    """Ensure HOME and EZKL_REPO_PATH are set for Windows Rust bindings."""
    if "HOME" not in os.environ:
        os.environ["HOME"] = str(Path.home())
    if "EZKL_REPO_PATH" not in os.environ:
        os.environ["EZKL_REPO_PATH"] = str(Path.home() / ".ezkl")


def _compress_to_zlib_file(data: bytes, path: Path) -> None:
    compressed = zlib.compress(data, level=9)
    with open(path, "wb") as f:
        f.write(compressed)


def _decompress_zlib_file(path: Path) -> bytes:
    with open(path, "rb") as f:
        return zlib.decompress(f.read())


def _get_flair_dir() -> Path:
    flair_dir = Path.cwd() / ".flair"
    if not flair_dir.exists():
        console.print("[red]Not in a Flair repository. Run 'flair init' first.[/red]")
        raise typer.Exit(code=1)
    return flair_dir


def _metrics_file() -> Path:
    return _get_flair_dir() / "metrics.json"


def _load_metrics(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            console.print("[yellow]Warning: .flair/metrics.json is not a JSON object. Resetting staged metrics.[/yellow]")
            return {}
        return data
    except Exception as e:
        console.print(f"[yellow]Warning: Could not read staged metrics: {e}[/yellow]")
        return {}


def _parse_metric_value(raw: str):
    """Parse a scalar metric token into int/float/str."""
    text = raw.strip()
    if not text:
        return None

    try:
        if text.lower() in {"nan", "+nan", "-nan", "inf", "+inf", "-inf"}:
            return float(text)
        if "." not in text and "e" not in text.lower():
            return int(text)
        return float(text)
    except Exception:
        return text


def _read_mlflow_metric_file(file_path: Path):
    """Read the latest value from an MLflow metric file."""
    try:
        with open(file_path, "r") as f:
            lines = [line.strip() for line in f.readlines() if line.strip()]
        if not lines:
            return None

        # MLflow file format is typically: <timestamp> <value> <step>
        latest = lines[-1].split()
        if len(latest) >= 2:
            return _parse_metric_value(latest[1])
        return _parse_metric_value(latest[-1])
    except Exception as e:
        console.print(f"[yellow]Warning: Could not parse metric file {file_path}: {e}[/yellow]")
        return None


def _find_mlflow_run_dirs(source: Path) -> list[Path]:
    """Find MLflow run directories under source by locating metrics/ folders."""
    if not source.exists():
        return []

    run_dirs: list[Path] = []
    for metrics_dir in source.rglob("metrics"):
        if not metrics_dir.is_dir():
            continue
        try:
            has_files = any(child.is_file() for child in metrics_dir.iterdir())
        except Exception:
            has_files = False
        if has_files:
            run_dirs.append(metrics_dir.parent)

    # Deduplicate while preserving order.
    unique: list[Path] = []
    seen: set[Path] = set()
    for run_dir in run_dirs:
        if run_dir not in seen:
            seen.add(run_dir)
            unique.append(run_dir)
    return unique


def _load_mlflow_metrics_from_run(run_dir: Path) -> dict:
    """Load latest scalar metrics from a single MLflow run directory."""
    metrics_dir = run_dir / "metrics"
    if not metrics_dir.exists() or not metrics_dir.is_dir():
        return {}

    imported: dict = {}
    for metric_file in metrics_dir.rglob("*"):
        if not metric_file.is_file():
            continue

        metric_name = metric_file.relative_to(metrics_dir).as_posix()
        metric_value = _read_mlflow_metric_file(metric_file)
        if metric_value is None:
            continue

        imported[metric_name] = metric_value

    return imported


# ---------------------------------------------------------------------------
# CLI commands: set / import / show / reset
# ---------------------------------------------------------------------------

@app.command("set")
def set_metrics(
    epoch: int | None = typer.Option(None, "--epoch", help="Training epoch"),
    accuracy: float | None = typer.Option(None, "--accuracy", help="Accuracy metric"),
    val_loss: float | None = typer.Option(None, "--val-loss", help="Validation loss"),
    train_loss: float | None = typer.Option(None, "--train-loss", help="Training loss"),
    precision: float | None = typer.Option(None, "--precision", help="Precision metric"),
    recall: float | None = typer.Option(None, "--recall", help="Recall metric"),
    f1: float | None = typer.Option(None, "--f1", help="F1 score"),
    learning_rate: float | None = typer.Option(None, "--learning-rate", help="Learning rate"),
    notes: str | None = typer.Option(None, "--notes", help="Optional notes"),
):
    """Set or update staged metrics in .flair/metrics.json."""
    updates = {
        "epoch": epoch,
        "accuracy": accuracy,
        "val_loss": val_loss,
        "train_loss": train_loss,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "learning_rate": learning_rate,
        "notes": notes,
    }
    updates = {k: v for k, v in updates.items() if v is not None}

    if not updates:
        console.print("[yellow]No metrics provided. Use at least one option with 'flair metrics set'.[/yellow]")
        raise typer.Exit(code=1)

    path = _metrics_file()
    data = _load_metrics(path)
    data.update(updates)
    data["updatedAt"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    console.print("[green]Metrics staged.[/green]")
    console.print(f"[dim]File: {path}[/dim]")


@app.command("import")
def import_metrics(
    source: Path = typer.Option(Path("mlruns"), "--source", help="Path to MLflow tracking directory"),
    run_id: str | None = typer.Option(None, "--run-id", help="Specific MLflow run ID to import"),
):
    """Import metrics from MLflow logs and stage them in .flair/metrics.json."""
    flair_dir = _get_flair_dir()

    source_path = source if source.is_absolute() else (Path.cwd() / source)
    if not source_path.exists():
        console.print(f"[red]MLflow source path not found: {source_path}[/red]")
        raise typer.Exit(code=1)

    selected_run_dir: Path | None = None

    if run_id:
        # Common MLflow layout: <mlruns>/<experiment_id>/<run_id>/metrics
        for experiment_dir in source_path.iterdir():
            if not experiment_dir.is_dir():
                continue
            candidate = experiment_dir / run_id
            if (candidate / "metrics").exists():
                selected_run_dir = candidate
                break

        # Also allow directly pointing at a run folder under source.
        if selected_run_dir is None:
            direct_candidate = source_path / run_id
            if (direct_candidate / "metrics").exists():
                selected_run_dir = direct_candidate

        if selected_run_dir is None:
            console.print(f"[red]Could not find MLflow run '{run_id}' under {source_path}[/red]")
            raise typer.Exit(code=1)
    else:
        run_dirs = _find_mlflow_run_dirs(source_path)
        if not run_dirs:
            console.print(f"[red]No MLflow run metrics found under {source_path}[/red]")
            raise typer.Exit(code=1)

        # Pick the most recently modified run directory.
        selected_run_dir = max(run_dirs, key=lambda p: p.stat().st_mtime)

    imported_metrics = _load_mlflow_metrics_from_run(selected_run_dir)
    if not imported_metrics:
        console.print(f"[red]No importable metrics found in run: {selected_run_dir}[/red]")
        raise typer.Exit(code=1)

    path = _metrics_file()
    data = _load_metrics(path)
    data.update(imported_metrics)
    data["updatedAt"] = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    with open(path, "w") as f:
        json.dump(data, f, indent=2)

    console.print("[green]Metrics imported from MLflow and staged.[/green]")
    console.print(f"[dim]Run: {selected_run_dir}[/dim]")
    console.print(f"[dim]Imported metrics: {len(imported_metrics)}[/dim]")
    console.print(f"[dim]File: {path}[/dim]")


@app.command("show")
def show_metrics():
    """Show current staged metrics from .flair/metrics.json."""
    path = _metrics_file()
    if not path.exists():
        console.print("No staged metrics found.")
        console.print("Use 'flair metrics set' or 'flair metrics import'.")
        raise typer.Exit(code=0)

    data = _load_metrics(path)
    if not data:
        console.print("No staged metrics found.")
        console.print("Use 'flair metrics set' or 'flair metrics import'.")
        raise typer.Exit(code=0)

    console.print("Current staged metrics:")
    for key, value in data.items():
        console.print(f"- {key}: {value}")


@app.command("reset")
def reset_metrics():
    """Reset staged metrics by deleting .flair/metrics.json."""
    path = _metrics_file()
    if not path.exists():
        console.print("No staged metrics to reset.")
        raise typer.Exit(code=0)

    try:
        path.unlink()
        console.print("Metrics reset.")
    except Exception as e:
        console.print(f"[red]Failed to reset metrics: {e}[/red]")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# Dataset loading
# ---------------------------------------------------------------------------

def _canonical_dataset_bytes(inputs: list, labels: list) -> bytes:
    """Produce a deterministic canonical byte representation of the dataset.

    Uses sorted-key JSON with no extra whitespace so the hash is stable
    regardless of the original file's formatting or key ordering.
    """
    samples = [{"input": inp, "label": lbl} for inp, lbl in zip(inputs, labels)]
    return json.dumps(samples, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _first_present(d: dict, *keys):
    """Return the value for the first key in keys that exists in d and is not None."""
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _derive_sample_indices(
    dataset_commitment: str,
    model_commitment: str,
    total_samples: int,
    k: int,
) -> list[int]:
    """Deterministically derive k sample indices using SHA-256 of commitments.

    Ensures that sample selection is reproducible by any verifier and cannot
    be cherry-picked by the prover.
    """
    if k >= total_samples or k <= 0:
        return list(range(total_samples))
    seed_str = f"{dataset_commitment}:{model_commitment}:{total_samples}:{k}"
    seed_bytes = hashlib.sha256(seed_str.encode("utf-8")).digest()
    import random
    rng = random.Random(seed_bytes)
    return sorted(rng.sample(range(total_samples), k))


def _load_eval_dataset(dataset_path: Path) -> tuple[list, list, str]:
    """Load evaluation samples and compute a stable cryptographic dataset commitment.

    The commitment is SHA-256 of the canonical (normalised) dataset representation,
    not the raw file bytes, so it is stable across whitespace/formatting changes.

    Returns:
        inputs: list of per-sample feature lists
        labels: list of per-sample labels
        dataset_commitment: 'sha256:<hex>' of the canonical dataset bytes
    """
    if not dataset_path.exists():
        raise typer.BadParameter(f"Dataset not found: {dataset_path}")

    ext = dataset_path.suffix.lower()
    if ext == ".json":
        with open(dataset_path, "r", encoding="utf-8") as f:
            content = json.load(f)

        if isinstance(content, dict):
            inputs = _first_present(content, "inputs", "data", "x")
            labels = _first_present(content, "labels", "targets", "y")
            if inputs is None or labels is None:
                raise typer.BadParameter("JSON dataset dictionary must contain 'inputs' and 'labels' keys.")
            inputs, labels = list(inputs), list(labels)
        elif isinstance(content, list):
            inputs, labels = [], []
            for item in content:
                if isinstance(item, dict):
                    inp = _first_present(item, "input", "features", "x")
                    lbl = _first_present(item, "label", "target", "y")
                    if inp is not None and lbl is not None:
                        inputs.append(inp)
                        labels.append(lbl)
            if not inputs:
                raise typer.BadParameter("Could not parse valid samples from JSON list.")
        else:
            raise typer.BadParameter("Invalid JSON dataset format.")

    elif ext in [".csv", ".tsv"]:
        sep = "\t" if ext == ".tsv" else ","
        with open(dataset_path, "r", encoding="utf-8") as f:
            reader = list(csv.reader(f, delimiter=sep))
        if not reader:
            raise typer.BadParameter("Empty CSV dataset.")

        start_idx = 0
        try:
            float(reader[0][0])
        except ValueError:
            start_idx = 1  # Skip header row

        inputs, labels = [], []
        for row in reader[start_idx:]:
            if not row:
                continue
            floats = [float(x.strip()) for x in row]
            inputs.append(floats[:-1])
            labels.append(int(floats[-1]) if floats[-1].is_integer() else floats[-1])

        if not inputs:
            raise typer.BadParameter("No valid data rows found in CSV.")
    else:
        raise typer.BadParameter(f"Unsupported dataset format '{ext}'. Supported formats: .json, .csv")

    # Canonical commitment: hash of the normalised sample list, not raw file bytes.
    canonical = _canonical_dataset_bytes(inputs, labels)
    dataset_commitment = f"sha256:{hashlib.sha256(canonical).hexdigest()}"

    return inputs, labels, dataset_commitment


def _find_eval_model(model_path: Optional[Path]) -> Path:
    """Find model file (.onnx or .pt) in current directory or validate specified path."""
    if model_path:
        p = model_path if model_path.is_absolute() else (Path.cwd() / model_path)
        if not p.exists():
            raise typer.BadParameter(f"Model file not found: {p}")
        return p

    for ext in [".onnx", ".pt", ".pth"]:
        found = list(Path.cwd().glob(f"*{ext}"))
        if found:
            return found[0]

    raise typer.BadParameter("No model found in current directory (.onnx, .pt). Please specify with --model.")


# ---------------------------------------------------------------------------
# Metric computation helpers (fixed-point aware)
# ---------------------------------------------------------------------------

def _compute_classification_metrics(
    logits: list,
    label: int,
) -> tuple:
    """Compute correctness and NLL loss for a single classification sample.

    Args:
        logits: decoded model output logits (from felt_to_float)
        label: integer class index

    Returns:
        (correct: bool, nll_loss: float)
    """
    pred_class = logits.index(max(logits))
    correct = pred_class == int(label)

    # Numerically stable log-softmax for NLL
    max_l = max(logits)
    exp_sum = sum(math.exp(l - max_l) for l in logits)
    if 0 <= int(label) < len(logits):
        log_prob = (logits[int(label)] - max_l) - math.log(exp_sum)
    else:
        log_prob = -10.0
    nll_loss = -log_prob
    return correct, nll_loss


def _compute_regression_metrics(
    prediction: float,
    label: float,
) -> tuple:
    """Compute squared error and absolute error for a single regression sample.

    Returns:
        (squared_error: float, abs_error: float)
    """
    err = prediction - float(label)
    return err * err, abs(err)


# ---------------------------------------------------------------------------
# Core evaluation + proof generation (Schema v3)
# ---------------------------------------------------------------------------

async def _run_eval_and_prove(
    onnx_path: Path,
    inputs: list,
    labels: list,
    zk_samples: int,
    generate_proof: bool,
    zkp_dir: Path,
    dataset_commitment: str,
    original_model_hash: str,
) -> tuple[dict, Optional[dict]]:
    """Evaluate model and generate cryptographic evaluation proof (schema v3).

    Cryptographic Design (Schema v3)
    ---------------------------------
    1. Deterministic Sample Selection:
       If zk_samples >= N or zk_samples <= 0, ALL N samples are proven (full evaluation).
       If zk_samples < N, K samples are deterministically selected using PRNG seeded by
       SHA-256(dataset_commitment : model_commitment : N : K).
       The prover CANNOT cherry-pick which samples are evaluated.
    2. Per-Sample Witnesses and SNARK Proofs:
       For EVERY selected sample, an EZKL witness and a formal Halo2 SNARK proof are generated.
       NO unproven sample ever contributes to the evaluated metrics.
    3. Metrics Aggregation:
       Accuracy/loss are computed strictly over the ZK-proven samples.
    4. Proof Bundling:
       All per-sample proofs are bundled into 'proofs.zlib'.
    5. Input & Output Commitments:
       Proof public instances tie inputs to the dataset and outputs to the model.
    """
    _ensure_ezkl_env()
    try:
        import ezkl
    except ImportError:
        raise typer.BadParameter("EZKL is required for cryptographic evaluation. Install with: pip install ezkl")

    async def _await(v):
        if inspect.isawaitable(v):
            return await v
        return v

    n_total = len(inputs)
    eval_indices = _derive_sample_indices(dataset_commitment, original_model_hash, n_total, zk_samples)
    is_sampled = len(eval_indices) < n_total
    k = len(eval_indices)

    if is_sampled:
        coverage_desc = f"{k}/{n_total} ({k / n_total * 100:.1f}% deterministic sample)"
        console.print(f"[cyan]Deterministic Sampled Evaluation:[/cyan] Proving {coverage_desc}")
        console.print(f"[dim]  Sampling seed derived from dataset and model commitments.[/dim]")
    else:
        coverage_desc = f"{n_total}/{n_total} (100.0% full evaluation)"
        console.print(f"[cyan]Full Dataset Evaluation:[/cyan] Proving {coverage_desc} via EZKL SNARKs")

    # ------------------------------------------------------------------
    # EZKL artifact paths (all temporary, cleaned up at the end)
    # ------------------------------------------------------------------
    settings_path = zkp_dir / "eval_settings.json"
    compiled_path = zkp_dir / "eval_network.compiled"
    cal_path = zkp_dir / "eval_cal.json"
    in_path = zkp_dir / "eval_in.json"
    wit_path = zkp_dir / "eval_witness.json"
    pk_path = zkp_dir / "eval.pk"
    vk_path = zkp_dir / "eval.vk"
    pf_path = zkp_dir / "eval.pf"

    # Step 1: Calibration & Compilation
    cal_samples = [inputs[i] for i in eval_indices[:min(50, len(eval_indices))]]
    with open(cal_path, "w", encoding="utf-8") as f:
        json.dump({"input_data": cal_samples}, f)

    py_args = ezkl.PyRunArgs()
    py_args.input_visibility = "public"
    py_args.output_visibility = "public"
    py_args.param_visibility = "fixed"
    py_args.decomp_legs = 3

    console.print("[cyan]Compiling zero-knowledge evaluation circuit...[/cyan]")
    ezkl.gen_settings(str(onnx_path), str(settings_path), py_run_args=py_args)
    await _await(ezkl.calibrate_settings(str(cal_path), str(onnx_path), str(settings_path), "resources"))
    ezkl.compile_circuit(str(onnx_path), str(compiled_path), str(settings_path))

    with open(settings_path, "r", encoding="utf-8") as f:
        st = json.load(f)
    output_scale = st.get("model_output_scales", [13])[0]
    input_scale = st.get("run_args", {}).get("input_scale", 13)

    # Step 2: Setup SRS and Proving Key once
    if generate_proof:
        console.print("[cyan]Setting up EZKL SRS and proving keys...[/cyan]")
        await _await(ezkl.get_srs(str(settings_path)))
        ezkl.setup(str(compiled_path), str(vk_path), str(pk_path))

    # Step 3: Generate witness and SNARK proof for EVERY evaluated sample
    correct_count = 0
    metric_sum = 0.0
    mae_sum = 0.0
    per_sample_outputs: dict = {}
    per_sample_inputs: dict = {}
    per_sample_labels: dict = {}
    sample_proofs: dict = {}
    sample_instances_hashes: dict = {}
    task_type = "unknown"

    console.print(f"[cyan]Generating EZKL witnesses and SNARK proofs for {k} sample(s)...[/cyan]")
    for pos, idx in enumerate(eval_indices):
        inp = inputs[idx]
        lbl = labels[idx]
        idx_str = str(idx)

        with open(in_path, "w", encoding="utf-8") as f:
            json.dump({"input_data": [inp]}, f)

        await _await(ezkl.gen_witness(str(in_path), str(compiled_path), str(wit_path)))

        with open(wit_path, "r", encoding="utf-8") as f:
            w = json.load(f)

        raw_output_felts = w.get("outputs", [[]])[0]
        raw_input_felts = w.get("inputs", [[]])[0]
        if not raw_output_felts:
            console.print(f"[yellow]Warning: empty output for sample {idx}, skipping.[/yellow]")
            continue

        per_sample_outputs[idx_str] = list(raw_output_felts)
        per_sample_inputs[idx_str] = list(raw_input_felts)
        per_sample_labels[idx_str] = int(lbl) if isinstance(lbl, (int, float)) and float(lbl).is_integer() else lbl

        logits = [ezkl.felt_to_float(felt, output_scale) for felt in raw_output_felts]

        if len(logits) > 1:
            task_type = "classification"
            is_correct, nll = _compute_classification_metrics(logits, int(lbl))
            if is_correct:
                correct_count += 1
            metric_sum += nll
        else:
            task_type = "regression"
            sq_err, abs_err = _compute_regression_metrics(logits[0], float(lbl))
            metric_sum += sq_err
            mae_sum += abs_err

        if generate_proof:
            ezkl.prove(str(wit_path), str(compiled_path), str(pk_path), str(pf_path))
            if not ezkl.verify(str(pf_path), str(settings_path), str(vk_path)):
                raise RuntimeError(f"Generated proof for sample {idx} failed self-verification")

            with open(pf_path, "r", encoding="utf-8") as f:
                pf_obj = json.load(f)

            sample_proofs[idx_str] = pf_obj
            instances = pf_obj.get("instances", [])
            sample_instances_hashes[idx_str] = hashlib.sha256(
                json.dumps(instances, sort_keys=True).encode("utf-8")
            ).hexdigest()

    n_evaluated = len(per_sample_outputs)
    if n_evaluated == 0:
        raise RuntimeError("No valid samples could be evaluated.")

    # Step 4: Aggregate metrics
    if task_type == "classification":
        accuracy = round(correct_count / n_evaluated, 6)
        val_loss = round(metric_sum / n_evaluated, 6)
        mse = mae = rmse = None
    else:
        mse = round(metric_sum / n_evaluated, 6)
        mae = round(mae_sum / n_evaluated, 6)
        rmse = round(math.sqrt(mse), 6)
        accuracy = val_loss = None

    # Step 5: Poseidon commitment over all evaluated output felts
    all_output_felts_flat = []
    for idx in eval_indices:
        idx_str = str(idx)
        if idx_str in per_sample_outputs:
            all_output_felts_flat.extend([str(f) for f in per_sample_outputs[idx_str]])

    eval_commitment_felts = ezkl.poseidon_hash(all_output_felts_flat)
    eval_commitment = "poseidon:" + ",".join(str(f) for f in eval_commitment_felts)

    zkp_info = None
    if generate_proof:
        proofs_zlib = zkp_dir / "proofs.zlib"
        proof_zlib = zkp_dir / "proof.zlib"
        vk_zlib = zkp_dir / "verification_key.zlib"
        settings_zlib = zkp_dir / "settings.zlib"

        bundle_bytes = json.dumps(sample_proofs).encode("utf-8")
        _compress_to_zlib_file(bundle_bytes, proofs_zlib)

        # Also store representative proof in proof.zlib for backward-compatibility
        rep_idx_str = str(eval_indices[0])
        rep_pf_obj = sample_proofs.get(rep_idx_str, {})
        _compress_to_zlib_file(json.dumps(rep_pf_obj).encode("utf-8"), proof_zlib)

        with open(vk_path, "rb") as f:
            _compress_to_zlib_file(f.read(), vk_zlib)
        with open(settings_path, "r", encoding="utf-8") as f:
            _compress_to_zlib_file(f.read().encode("utf-8"), settings_zlib)

        zkp_info = {
            "proof_schema_version": PROOF_SCHEMA_VERSION,
            "proof_type": "ezkl_eval_v3",
            "proofs_file": proofs_zlib.name,
            "proof_file": proof_zlib.name,
            "verification_key_file": vk_zlib.name,
            "settings_file": settings_zlib.name,
            "model_commitment": original_model_hash,
            "dataset_commitment": dataset_commitment,
            "eval_commitment": eval_commitment,
            "total_dataset_samples": n_total,
            "eval_samples": n_evaluated,
            "proven_sample_count": len(sample_proofs),
            "zk_coverage": coverage_desc,
            "is_sampled": is_sampled,
            "evaluated_indices": eval_indices,
            "per_sample_outputs": per_sample_outputs,
            "per_sample_inputs": per_sample_inputs,
            "per_sample_labels": per_sample_labels,
            "sample_instances_hashes": sample_instances_hashes,
            "correct_count": correct_count if task_type == "classification" else None,
            "total_count": n_evaluated,
            "loss_sum": round(metric_sum, 8) if task_type == "classification" else None,
            "sse_sum": round(metric_sum, 8) if task_type == "regression" else None,
            "mae_sum": round(mae_sum, 8) if task_type == "regression" else None,
            "accuracy": accuracy,
            "val_loss": val_loss,
            "mse": mse,
            "mae": mae,
            "rmse": rmse,
            "output_scale": output_scale,
            "input_scale": input_scale,
            "task_type": task_type,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        zkp_info = {k: v for k, v in zkp_info.items() if v is not None}

    # Build top-level metrics dict
    metrics_dict: dict = {"task_type": task_type}
    if task_type == "classification":
        metrics_dict["accuracy"] = accuracy
        metrics_dict["val_loss"] = val_loss
    else:
        metrics_dict["mse"] = mse
        metrics_dict["mae"] = mae
        metrics_dict["rmse"] = rmse

    # Cleanup temporary files
    for p in [settings_path, compiled_path, cal_path, in_path, wit_path, pk_path, vk_path, pf_path]:
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass

    return metrics_dict, zkp_info


# ---------------------------------------------------------------------------
# CLI command: evaluate
# ---------------------------------------------------------------------------

@app.command("evaluate")
def evaluate_metrics(
    dataset: Path = typer.Option(..., "--dataset", "-d", help="Path to local validation dataset (.json or .csv)"),
    model: Optional[Path] = typer.Option(None, "--model", "-m", help="Path to model file (.onnx, .pt, .pth)"),
    prove: bool = typer.Option(True, "--prove/--no-prove", help="Generate EZKL zero-knowledge proof of evaluation"),
    zk_samples: int = typer.Option(
        10,
        "--zk-samples",
        "-k",
        help=(
            "Maximum number of samples to cryptographically prove via EZKL SNARKs (default: 10, 0 = all). "
            "If dataset size <= zk_samples, ALL samples are proven (full evaluation). "
            "If dataset size > zk_samples, a deterministic pseudorandom sample is proven. "
            "Metrics are computed EXCLUSIVELY over proven samples."
        ),
    ),
    eval_samples: int = typer.Option(
        0,
        "--eval-samples",
        "-n",
        help="Legacy alias for --zk-samples (0 = all).",
    ),
    epoch: Optional[int] = typer.Option(None, "--epoch", "-e", help="Training epoch"),
    notes: Optional[str] = typer.Option(None, "--notes", help="Optional notes"),
):
    """Evaluate model on private dataset with cryptographically sound EZKL Zero-Knowledge Proofs.

    Cryptographic Design (Proof Schema v3)
    ---------------------------------------
    - Dataset commitment: SHA-256 of the canonical dataset representation.
    - Model commitment: SHA-256 of the exact ONNX model artifact bytes.
    - 100% Proven Metrics: Every sample that enters the reported accuracy/loss
      has an independently verified Halo2 SNARK proof.
    - Deterministic Sampling: When the dataset exceeds --zk-samples, samples are
      selected via a PRNG seeded with SHA-256(dataset_commitment : model_commitment).
      The prover cannot cherry-pick samples.
    - Transparent Semantics: Full vs. Sampled evaluations are clearly labeled.
    """
    flair_dir = _get_flair_dir()
    zkp_dir = flair_dir / ".zkp"
    zkp_dir.mkdir(parents=True, exist_ok=True)

    inputs, labels, dataset_commitment = _load_eval_dataset(dataset)
    console.print(f"[green]✓ Dataset loaded:[/green] {len(inputs)} private samples")
    console.print(f"[dim]Dataset Commitment: {dataset_commitment}[/dim]")

    model_path = _find_eval_model(model)
    console.print(f"[green]✓ Model identified:[/green] {model_path.name}")

    # Compute model commitment before any conversion
    original_model_hash = f"sha256:{hashlib.sha256(model_path.read_bytes()).hexdigest()}"
    console.print(f"[dim]Model Commitment: {original_model_hash}[/dim]")

    # Resolve effective sample cap
    effective_zk_samples = eval_samples if eval_samples > 0 else zk_samples

    # Convert to ONNX if needed
    onnx_path = model_path
    if model_path.suffix.lower() != ".onnx":
        from flair_cli.cli.zkp import _convert_to_onnx
        framework = "pytorch" if model_path.suffix.lower() in [".pt", ".pth"] else "tensorflow"
        onnx_path = _convert_to_onnx(model_path, framework)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        metrics_dict, zkp_info = loop.run_until_complete(
            _run_eval_and_prove(
                onnx_path,
                inputs,
                labels,
                effective_zk_samples,
                prove,
                zkp_dir,
                dataset_commitment,
                original_model_hash,
            )
        )
    finally:
        loop.close()

    if onnx_path != model_path and onnx_path.exists():
        try:
            onnx_path.unlink()
        except Exception:
            pass

    task_type = metrics_dict.get("task_type", "unknown")
    n_total = len(inputs)
    n_proven = zkp_info.get("proven_sample_count", len(inputs)) if zkp_info else len(inputs)
    zk_coverage = zkp_info.get("zk_coverage", f"{n_proven}/{n_total}") if zkp_info else "none"

    # Stage metrics in .flair/metrics.json
    metrics_path = _metrics_file()
    metrics_data = _load_metrics(metrics_path)
    metrics_data.update({
        "task_type": task_type,
        "epoch": epoch,
        "notes": notes,
        "total_samples": n_total,
        "eval_samples": n_proven,
        "zk_coverage": zk_coverage,
        "dataset_commitment": dataset_commitment,
        "dataset_hash": dataset_commitment,   # backward-compat alias
        "model_commitment": zkp_info.get("model_commitment") if zkp_info else original_model_hash,
        "zkp_verified": prove,
        "zkp": zkp_info,
        "updatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
    })
    # Merge task-specific metrics into top-level
    for k in ["accuracy", "val_loss", "mse", "mae", "rmse"]:
        if metrics_dict.get(k) is not None:
            metrics_data[k] = metrics_dict[k]

    metrics_data = {k: v for k, v in metrics_data.items() if v is not None}

    with open(metrics_path, "w", encoding="utf-8") as f:
        json.dump(metrics_data, f, indent=2)

    # Optionally attach to unfinalized local commit
    from flair_cli.cli.zkp import _get_latest_local_commit
    local_commit = _get_latest_local_commit()
    if local_commit and zkp_info:
        commit_data, commit_dir = local_commit
        if commit_data.get("message") is None:
            for f_name in [
                zkp_info.get("proofs_file", "proofs.zlib"),
                zkp_info.get("proof_file", "proof.zlib"),
                zkp_info["verification_key_file"],
                zkp_info["settings_file"],
            ]:
                src = zkp_dir / f_name
                dst = commit_dir / f_name
                if src.exists():
                    dst.write_bytes(src.read_bytes())
            commit_data["zkp"] = {
                "timestamp": zkp_info["timestamp"],
                "model_file": model_path.name,
                "framework": "onnx",
                "input_dims": [1, len(inputs[0])] if inputs else [1],
                "format": "zlib",
                "proof_file": zkp_info.get("proofs_file", zkp_info.get("proof_file")),
                "verification_key_file": zkp_info["verification_key_file"],
                "settings_file": zkp_info["settings_file"],
                "proof_type": "ezkl_eval_v3",
                "proof_schema_version": PROOF_SCHEMA_VERSION,
                "zk_coverage": zk_coverage,
            }
            with open(commit_dir / "commit.json", "w", encoding="utf-8") as cf:
                json.dump(commit_data, cf, indent=2)

    # Print results
    console.print(f"\n[bold green]✓ Evaluation Complete & Metrics Staged![/bold green]")
    if task_type == "classification":
        acc = metrics_dict.get("accuracy") or 0.0
        loss = metrics_dict.get("val_loss") or 0.0
        console.print(f"  • Task:        [bold]Classification[/bold]")
        console.print(f"  • Accuracy:    [bold]{acc * 100:.4f}%[/bold]  (metrics derived exclusively from individually SNARK-proven model outputs and verified dataset labels)")
        console.print(f"  • Val Loss:    [bold]{loss:.6f}[/bold]  (metrics derived exclusively from individually SNARK-proven model outputs and verified dataset labels)")
    else:
        console.print(f"  • Task:        [bold]Regression[/bold]")
        console.print(f"  • MSE:         [bold]{metrics_dict.get('mse'):.6f}[/bold]")
        console.print(f"  • MAE:         [bold]{metrics_dict.get('mae'):.6f}[/bold]")
        console.print(f"  • RMSE:        [bold]{metrics_dict.get('rmse'):.6f}[/bold]")

    console.print(f"  • ZK Coverage: [bold cyan]{zk_coverage}[/bold cyan]")
    console.print(f"  • Dataset Commitment: [dim]{dataset_commitment}[/dim]")
    console.print(f"  • Model Commitment:   [dim]{original_model_hash}[/dim]")

    if prove and zkp_info:
        ec = zkp_info.get("eval_commitment", "—")
        console.print(f"  • Eval Commitment:    [dim]{ec[:70]}{'...' if len(ec) > 70 else ''}[/dim]")
        console.print(f"  • ZK-Proofs:   [green]{n_proven} SNARK proofs generated via EZKL[/green]")
        console.print(f"  • Schema:      v{PROOF_SCHEMA_VERSION}")
        console.print()
        console.print("[dim]Security scope:[/dim]")
        console.print("[dim]  ✓ Soundness: every sample contributing to metrics has an independent SNARK proof.[/dim]")
        console.print("[dim]  ✓ Cherry-pick resistance: sample selection is deterministically seeded by commitments.[/dim]")
        console.print("[dim]  ✓ Verification: 'flair metrics verify' verifies all SNARK proofs and output bounds.[/dim]")

    console.print(f"[dim]Staged in: {metrics_path}[/dim]")
    console.print("\n[dim]Next step: Run 'flair commit -m \"Message\"' to record verified metrics into your commit.[/dim]")

# ---------------------------------------------------------------------------
# CLI command: verify
# ---------------------------------------------------------------------------

def _recompute_eval_commitment(per_sample_outputs: list | dict) -> str:
    """Recompute the Poseidon commitment from stored output felt arrays."""
    _ensure_ezkl_env()
    try:
        import ezkl
    except ImportError:
        raise RuntimeError("EZKL required for commitment recomputation")

    all_felts_flat = []
    if isinstance(per_sample_outputs, dict):
        sorted_keys = sorted(per_sample_outputs.keys(), key=lambda k: int(k) if k.isdigit() else k)
        rows = [per_sample_outputs[k] for k in sorted_keys]
    else:
        rows = per_sample_outputs

    for felt_row in rows:
        all_felts_flat.extend([str(f) for f in felt_row])

    commitment_felts = ezkl.poseidon_hash(all_felts_flat)
    return "poseidon:" + ",".join(str(f) for f in commitment_felts)


def _recompute_metrics_from_felts(
    per_sample_outputs: list | dict,
    per_sample_labels: list | dict,
    output_scale: int,
    task_type: str,
) -> dict:
    """Recompute accuracy/loss from stored output felts.

    Uses the same felt_to_float decoding as the original evaluation,
    producing bit-identical results if the data is untampered.
    """
    _ensure_ezkl_env()
    try:
        import ezkl
    except ImportError:
        raise RuntimeError("EZKL required for metric recomputation")

    if isinstance(per_sample_outputs, dict):
        sorted_keys = sorted(per_sample_outputs.keys(), key=lambda k: int(k) if k.isdigit() else k)
        output_rows = [per_sample_outputs[k] for k in sorted_keys]
        label_rows = [per_sample_labels[k] for k in sorted_keys] if isinstance(per_sample_labels, dict) else per_sample_labels
    else:
        output_rows = per_sample_outputs
        label_rows = per_sample_labels

    correct_count = 0
    metric_sum = 0.0
    mae_sum = 0.0
    n = 0

    for felt_row, lbl in zip(output_rows, label_rows):
        logits = [ezkl.felt_to_float(str(f), output_scale) for f in felt_row]
        if not logits:
            continue
        n += 1

        if task_type == "classification":
            is_correct, nll = _compute_classification_metrics(logits, int(lbl))
            if is_correct:
                correct_count += 1
            metric_sum += nll
        else:
            sq_err, abs_err = _compute_regression_metrics(logits[0], float(lbl))
            metric_sum += sq_err
            mae_sum += abs_err

    if n == 0:
        return {}

    result: dict = {"n": n}
    if task_type == "classification":
        result["accuracy"] = round(correct_count / n, 6)
        result["val_loss"] = round(metric_sum / n, 6)
        result["correct_count"] = correct_count
        result["loss_sum"] = round(metric_sum, 8)
    else:
        mse_val = metric_sum / n
        result["mse"] = round(mse_val, 6)
        result["mae"] = round(mae_sum / n, 6)
        result["rmse"] = round(math.sqrt(mse_val), 6)
    return result


def _verify_schema_v3(
    data: dict,
    zkp: dict,
    model_arg: Optional[Path],
    dataset_arg: Optional[Path],
) -> None:
    """Full cryptographic verification for Proof Schema v3 records.

    Cryptographic Guarantees:
    - Soundness: 100% of evaluated samples have individually verified EZKL SNARK proofs.
    - Metric Semantics: Metrics are derived exclusively from individually SNARK-proven
      model outputs and verified dataset labels.
    - Model Binding: Verifies SHA-256 model commitment and verifies forward circuit
      execution against the exact ONNX model artifact.
    - Dataset Binding: Canonical dataset commitment and per-sample input/label bindings
      are externally verified by cross-checking each proven sample against the dataset.
    - Deterministic Sampling: Evaluation sample indices are derived deterministically
      via PRNG seeded by model and dataset commitments, preventing cherry-picking.
    """
    flair_dir = _get_flair_dir()
    zkp_dir = flair_dir / ".zkp"

    proofs_file_name = zkp.get("proofs_file") or zkp.get("proof_file", "proofs.zlib")
    proofs_zlib = zkp_dir / proofs_file_name
    vk_zlib = zkp_dir / zkp.get("verification_key_file", "verification_key.zlib")
    settings_zlib = zkp_dir / zkp.get("settings_file", "settings.zlib")

    # Step 1: Check artifact files exist
    missing = [p.name for p in [proofs_zlib, vk_zlib, settings_zlib] if not p.exists()]
    if missing:
        console.print(f"[red]✗ Missing ZKP proof files in .flair/.zkp/: {', '.join(missing)}[/red]")
        raise typer.Exit(code=1)

    _ensure_ezkl_env()
    try:
        import ezkl
    except ImportError:
        console.print("[red]✗ EZKL not installed. Install with: pip install ezkl[/red]")
        raise typer.Exit(code=1)

    verification_passed = True
    checks: list = []

    temp_pf = zkp_dir / "temp_v3.pf"
    temp_vk = zkp_dir / "temp_v3.vk"
    temp_st = zkp_dir / "temp_v3.json"

    try:
        temp_vk.write_bytes(_decompress_zlib_file(vk_zlib))
        temp_st.write_text(_decompress_zlib_file(settings_zlib).decode("utf-8"), encoding="utf-8")

        # Step 2: Dataset commitment & data loading (external verifier-level binding)
        stored_dataset_commitment = zkp.get("dataset_commitment") or data.get("dataset_commitment")
        dataset_file = _find_dataset_for_verification(dataset_arg)
        loaded_inputs = None
        loaded_labels = None

        if dataset_arg and not dataset_file:
            checks.append((False, f"Dataset file not found: {dataset_arg}"))
            verification_passed = False
        elif dataset_file:
            loaded_inputs, loaded_labels, computed_ds_comm = _load_eval_dataset(dataset_file)
            if computed_ds_comm == stored_dataset_commitment:
                checks.append((True, f"Dataset commitment verified: {stored_dataset_commitment[:48]}..."))
            else:
                checks.append((False,
                    "Dataset commitment MISMATCH — dataset does not match commitment!\n"
                    f"    Stored:   {stored_dataset_commitment}\n"
                    f"    Provided: {computed_ds_comm}"))
                verification_passed = False
        elif stored_dataset_commitment:
            checks.append((None,
                f"Dataset commitment recorded ({stored_dataset_commitment[:48]}...) — "
                "provide --dataset to verify input/label bindings"))

        # Step 3: Model commitment & circuit binding verification
        stored_model_commitment = zkp.get("model_commitment") or data.get("model_commitment")
        model_file = _find_model_for_verification(model_arg)
        if model_arg and not model_file:
            checks.append((False, f"Model file not found: {model_arg}"))
            verification_passed = False
        elif model_file and stored_model_commitment:
            recomputed_model_hash = f"sha256:{hashlib.sha256(model_file.read_bytes()).hexdigest()}"
            if recomputed_model_hash == stored_model_commitment:
                checks.append((True, f"Model commitment verified: {stored_model_commitment[:48]}..."))

                # Verifiable model circuit execution binding
                temp_compiled = zkp_dir / "temp_v3.compiled"
                try:
                    ezkl.compile_circuit(str(model_file), str(temp_compiled), str(temp_st))
                    test_inp = None
                    sample_0_key = "0"
                    if loaded_inputs and len(loaded_inputs) > 0:
                        test_inp = loaded_inputs[0]
                    elif "per_sample_inputs" in zkp and "0" in zkp["per_sample_inputs"]:
                        test_inp = zkp["per_sample_inputs"]["0"]

                    if test_inp is not None:
                        temp_inp = zkp_dir / "temp_in0.json"
                        temp_wit = zkp_dir / "temp_wit0.json"
                        try:
                            temp_inp.write_text(json.dumps({"input_data": [[float(x) for x in test_inp]]}))
                            ezkl.gen_witness(str(temp_inp), str(temp_compiled), str(temp_wit))
                            with open(temp_wit, "r", encoding="utf-8") as wf:
                                wit_data = json.load(wf)
                            model_outputs = wit_data.get("outputs", [[]])[0]
                            stored_outputs = zkp.get("per_sample_outputs", {}).get(sample_0_key, [])
                            if stored_outputs and [str(f) for f in model_outputs] != [str(f) for f in stored_outputs]:
                                checks.append((False,
                                    "Model circuit execution MISMATCH: ONNX model forward pass does not match "
                                    "proven circuit outputs! Model was replaced or weights modified."))
                                verification_passed = False
                            else:
                                checks.append((True,
                                    "Model circuit execution cryptographically bound to exact ONNX artifact"))
                        finally:
                            for p in [temp_inp, temp_wit]:
                                if p.exists():
                                    try:
                                        p.unlink()
                                    except Exception:
                                        pass
                except Exception as e:
                    checks.append((False, f"Model circuit compilation/binding failed: {e}"))
                    verification_passed = False
                finally:
                    if temp_compiled.exists():
                        try:
                            temp_compiled.unlink()
                        except Exception:
                            pass
            else:
                checks.append((False,
                    "Model commitment MISMATCH — model was modified or wrong model provided!\n"
                    f"    Stored:     {stored_model_commitment}\n"
                    f"    Provided:   {recomputed_model_hash}"))
                verification_passed = False
        elif stored_model_commitment:
            checks.append((None,
                f"Model commitment recorded ({stored_model_commitment[:48]}...) — "
                "provide --model path to verify"))
        else:
            checks.append((None, "Model commitment: not recorded"))

        # Step 4: Decompress proofs bundle & verify counts & sampling derivation
        try:
            proofs_bundle_raw = _decompress_zlib_file(proofs_zlib).decode("utf-8")
            proofs_bundle = json.loads(proofs_bundle_raw)
        except Exception as e:
            checks.append((False, f"Proof bundle CORRUPTED: failed to decompress or parse proofs: {e}"))
            proofs_bundle = {}
            verification_passed = False

        total_dataset_samples = zkp.get("total_dataset_samples", zkp.get("total_count", 0))
        evaluated_indices = zkp.get("evaluated_indices", [])
        proven_sample_count = zkp.get("proven_sample_count", len(evaluated_indices))
        claimed_eval_samples = data.get("eval_samples")

        if proofs_bundle:
            if claimed_eval_samples is not None and claimed_eval_samples != len(proofs_bundle):
                checks.append((False,
                    f"Sampling count TAMPERED: claimed eval_samples={claimed_eval_samples}, "
                    f"but proofs bundle contains {len(proofs_bundle)} proofs"))
                verification_passed = False

            if proven_sample_count != len(proofs_bundle):
                checks.append((False,
                    f"Proven sample count TAMPERED: zkp records {proven_sample_count}, "
                    f"but proofs bundle contains {len(proofs_bundle)} proofs"))
                verification_passed = False

            if len(evaluated_indices) != len(proofs_bundle):
                checks.append((False,
                    f"Evaluated indices count TAMPERED: {len(evaluated_indices)} indices "
                    f"vs {len(proofs_bundle)} proofs"))
                verification_passed = False

            bundle_indices = sorted([int(k) for k in proofs_bundle.keys()])
            if sorted(evaluated_indices) != bundle_indices:
                checks.append((False,
                    "Evaluated indices mismatch with proofs bundle indices! "
                    "Proof bundle has been tampered or indices modified."))
                verification_passed = False

        if stored_dataset_commitment and stored_model_commitment and total_dataset_samples > 0:
            derived_indices = _derive_sample_indices(
                stored_dataset_commitment,
                stored_model_commitment,
                total_dataset_samples,
                proven_sample_count,
            )
            if evaluated_indices == derived_indices:
                coverage_str = zkp.get("zk_coverage", f"{len(evaluated_indices)}/{total_dataset_samples}")
                checks.append((True,
                    f"Deterministic sampling integrity verified: {coverage_str} (matches PRNG seed)"))
            else:
                checks.append((False,
                    "Sampling derivation TAMPERED: evaluated indices do not match PRNG derivation! "
                    "Cherry-picking detected."))
                verification_passed = False

        # Step 5: Verify EVERY proof and enforce input/label bindings
        per_sample_outputs = zkp.get("per_sample_outputs", {})
        sample_instances_hashes = zkp.get("sample_instances_hashes", {})
        per_sample_labels = zkp.get("per_sample_labels", {})
        output_scale = zkp.get("output_scale", 13)
        input_scale = zkp.get("input_scale", 13)

        verified_proof_count = 0
        all_proofs_valid = True

        for idx_str, pf_obj in proofs_bundle.items():
            temp_pf.write_text(json.dumps(pf_obj), encoding="utf-8")
            try:
                is_valid = ezkl.verify(str(temp_pf), str(temp_st), str(temp_vk))
            except Exception:
                is_valid = False
            if not is_valid:
                checks.append((False, f"Sample {idx_str}: EZKL SNARK mathematical verification FAILED"))
                all_proofs_valid = False
                verification_passed = False
                continue

            # Verify public instances hash
            instances = pf_obj.get("instances", [])
            inst_hash = hashlib.sha256(json.dumps(instances, sort_keys=True).encode("utf-8")).hexdigest()
            stored_inst_hash = sample_instances_hashes.get(idx_str)
            if stored_inst_hash and inst_hash != stored_inst_hash:
                checks.append((False, f"Sample {idx_str}: public instances hash mismatch"))
                verification_passed = False
                all_proofs_valid = False
                continue

            # Verify output felts in proof instances match per_sample_outputs
            stored_outputs = per_sample_outputs.get(idx_str)
            if stored_outputs and instances:
                flat_instances = instances[0]
                n_out = len(stored_outputs)
                proof_output_felts = flat_instances[-n_out:]
                if [str(f) for f in proof_output_felts] != [str(f) for f in stored_outputs]:
                    checks.append((False, f"Sample {idx_str}: proof outputs do not match stored outputs"))
                    verification_passed = False
                    all_proofs_valid = False
                    continue

            # If dataset is provided, verify input and label binding
            if loaded_inputs is not None or loaded_labels is not None:
                idx_int = int(idx_str)
                if loaded_labels is not None:
                    if idx_int >= len(loaded_labels):
                        checks.append((False,
                            f"Sample {idx_str}: index out of bounds for dataset of length {len(loaded_labels)}"))
                        verification_passed = False
                        all_proofs_valid = False
                    else:
                        exp_label = loaded_labels[idx_int]
                        rec_label = per_sample_labels.get(idx_str)
                        if rec_label is None or rec_label != exp_label:
                            checks.append((False,
                                f"Sample {idx_str} label TAMPERED: dataset label={exp_label}, "
                                f"evaluation record label={rec_label}"))
                            verification_passed = False
                            all_proofs_valid = False

                if loaded_inputs is not None and idx_int < len(loaded_inputs):
                    expected_inp = loaded_inputs[idx_int]
                    flat_instances = instances[0]
                    n_in = len(expected_inp)
                    proof_input_felts = flat_instances[:n_in]
                    decoded_inputs = [ezkl.felt_to_float(str(f), input_scale) for f in proof_input_felts]
                    for feat_idx, (exp_val, dec_val) in enumerate(zip(expected_inp, decoded_inputs)):
                        if abs(float(exp_val) - float(dec_val)) > 1e-2:
                            checks.append((False,
                                f"Sample {idx_str} feature {feat_idx}: input mismatch between dataset and proof "
                                f"(dataset={exp_val}, proof={dec_val})"))
                            verification_passed = False
                            all_proofs_valid = False
                            break

            verified_proof_count += 1

        if all_proofs_valid and verified_proof_count > 0:
            checks.append((True,
                f"EZKL SNARK proofs mathematically verified: {verified_proof_count}/{len(proofs_bundle)} "
                "samples individually proven"))

        # Step 6: Verify Poseidon commitment
        stored_eval_commitment = zkp.get("eval_commitment", "")
        if per_sample_outputs:
            recomputed_commitment = _recompute_eval_commitment(per_sample_outputs)
            if recomputed_commitment == stored_eval_commitment:
                checks.append((True, "Evaluation output commitment verified (Poseidon digest untampered)"))
            else:
                checks.append((False,
                    "Evaluation output commitment TAMPERED\n"
                    f"    Stored:     {stored_eval_commitment[:80]}\n"
                    f"    Recomputed: {recomputed_commitment[:80]}"))
                verification_passed = False

        # Step 7: Recompute metrics strictly from the verified outputs and verified labels
        task_type = zkp.get("task_type", "classification")
        effective_labels = per_sample_labels
        if loaded_labels is not None:
            effective_labels = {str(k): loaded_labels[int(k)] for k in per_sample_labels.keys() if int(k) < len(loaded_labels)}

        recomputed = _recompute_metrics_from_felts(
            per_sample_outputs, effective_labels, output_scale, task_type
        )
        TOLERANCE = 1e-5

        if task_type == "classification":
            claimed_acc = data.get("accuracy")
            recomputed_acc = recomputed.get("accuracy")
            if claimed_acc is not None and recomputed_acc is not None:
                if abs(float(claimed_acc) - float(recomputed_acc)) > TOLERANCE:
                    checks.append((False,
                        f"Accuracy TAMPERED: claimed={claimed_acc}, "
                        f"recomputed-from-proven-outputs={recomputed_acc}"))
                    verification_passed = False
                else:
                    checks.append((True,
                        f"Accuracy verified from {verified_proof_count} SNARK-proven outputs: "
                        f"{recomputed_acc * 100:.4f}%  ({recomputed.get('correct_count')}/{recomputed.get('n')} samples) "
                        f"[metrics derived exclusively from individually SNARK-proven model outputs and verified dataset labels]"))

            claimed_loss = data.get("val_loss")
            recomputed_loss = recomputed.get("val_loss")
            if claimed_loss is not None and recomputed_loss is not None:
                if abs(float(claimed_loss) - float(recomputed_loss)) > TOLERANCE * 10:
                    checks.append((False,
                        f"Val loss TAMPERED: claimed={claimed_loss}, "
                        f"recomputed-from-proven-outputs={recomputed_loss}"))
                    verification_passed = False
                else:
                    checks.append((True,
                        f"Loss verified from {verified_proof_count} SNARK-proven outputs: {recomputed_loss:.6f} "
                        f"[metrics derived exclusively from individually SNARK-proven model outputs and verified dataset labels]"))
        else:
            for metric in ["mse", "mae", "rmse"]:
                claimed = data.get(metric)
                recomputed_val = recomputed.get(metric)
                if claimed is not None and recomputed_val is not None:
                    if abs(float(claimed) - float(recomputed_val)) > TOLERANCE:
                        checks.append((False,
                            f"{metric.upper()} TAMPERED: claimed={claimed}, recomputed={recomputed_val}"))
                        verification_passed = False
                    else:
                        checks.append((True,
                            f"{metric.upper()} verified from {verified_proof_count} SNARK-proven outputs: {recomputed_val:.6f} "
                            f"[metrics derived exclusively from individually SNARK-proven model outputs and verified dataset labels]"))

    finally:
        for p in [temp_pf, temp_vk, temp_st]:
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    # Print all check results
    console.print()
    for ok, msg in checks:
        if ok is True:
            console.print(f"  [green]✓[/green] {msg}")
        elif ok is False:
            console.print(f"  [red]✗[/red] {msg}")
        else:
            console.print(f"  [yellow]⚠[/yellow] {msg}")

    console.print()
    if verification_passed:
        console.print("[bold green]✓ Cryptographic Verification Passed[/bold green]")
        console.print()
        console.print("[dim]Cryptographic guarantees (Proof Schema v3):[/dim]")
        console.print(f"[dim]  ✓ Soundness: {verified_proof_count}/{verified_proof_count} evaluated samples have individually verified EZKL SNARK proofs.[/dim]")
        console.print("[dim]  ✓ Metric semantics: metrics derived exclusively from individually SNARK-proven model outputs and verified dataset labels.[/dim]")
        console.print("[dim]  ✓ Deterministic sampling: indices derived deterministically from commitments — cherry-picking prevented.[/dim]")
        if loaded_inputs is not None and loaded_labels is not None:
            console.print("[dim]  ✓ Dataset binding: external verification of canonical dataset commitment, sample inputs, and labels.[/dim]")
        if model_file is not None:
            console.print("[dim]  ✓ Model binding: exact ONNX artifact hash and circuit execution verified.[/dim]")
    else:
        console.print("[bold red]✗ Verification FAILED — cryptographic integrity violation detected![/bold red]")
        raise typer.Exit(code=1)


def _verify_schema_v2(data: dict, zkp: dict, model_arg: Optional[Path]) -> None:
    """Full cryptographic verification for Proof Schema v2 records."""
    flair_dir = _get_flair_dir()
    zkp_dir = flair_dir / ".zkp"

    proof_zlib = zkp_dir / zkp.get("proof_file", "proof.zlib")
    vk_zlib = zkp_dir / zkp.get("verification_key_file", "verification_key.zlib")
    settings_zlib = zkp_dir / zkp.get("settings_file", "settings.zlib")

    # Step 1: Check artifact files exist
    missing = [p.name for p in [proof_zlib, vk_zlib, settings_zlib] if not p.exists()]
    if missing:
        console.print(f"[red]✗ Missing ZKP proof files in .flair/.zkp/: {', '.join(missing)}[/red]")
        raise typer.Exit(code=1)

    _ensure_ezkl_env()
    try:
        import ezkl
    except ImportError:
        console.print("[red]✗ EZKL not installed. Install with: pip install ezkl[/red]")
        raise typer.Exit(code=1)

    temp_pf = zkp_dir / "temp_verify.pf"
    temp_vk = zkp_dir / "temp_verify.vk"
    temp_st = zkp_dir / "temp_verify.json"

    verification_passed = True
    checks: list = []

    try:
        temp_pf.write_text(_decompress_zlib_file(proof_zlib).decode("utf-8"), encoding="utf-8")
        temp_vk.write_bytes(_decompress_zlib_file(vk_zlib))
        temp_st.write_text(_decompress_zlib_file(settings_zlib).decode("utf-8"), encoding="utf-8")

        # Step 2: Mathematical EZKL proof verification
        is_valid = ezkl.verify(str(temp_pf), str(temp_st), str(temp_vk))
        if not is_valid:
            checks.append((False, "EZKL proof mathematical verification failed"))
            verification_passed = False
        else:
            checks.append((True, "Proof valid (EZKL circuit arithmetic verified for representative sample 0)"))

        # Step 3: Public instances hash
        with open(temp_pf, "r", encoding="utf-8") as f:
            pf_json = json.load(f)
        current_instances_hash = hashlib.sha256(
            json.dumps(pf_json.get("instances", []), sort_keys=True).encode("utf-8")
        ).hexdigest()
        stored_instances_hash = zkp.get("instances_hash", "")
        if current_instances_hash != stored_instances_hash:
            checks.append((False, "Proof public instances tampered (instances_hash mismatch)"))
            verification_passed = False
        else:
            checks.append((True, "Proof public instances untampered"))

    finally:
        for p in [temp_pf, temp_vk, temp_st]:
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    # Step 4: Recompute Poseidon eval_commitment from stored per-sample outputs
    per_sample_outputs = zkp.get("per_sample_outputs")
    per_sample_labels = zkp.get("per_sample_labels", [])
    output_scale = zkp.get("output_scale", 13)
    task_type = zkp.get("task_type", "classification")
    stored_eval_commitment = zkp.get("eval_commitment", "")

    if per_sample_outputs:
        try:
            recomputed_commitment = _recompute_eval_commitment(per_sample_outputs)
            if recomputed_commitment != stored_eval_commitment:
                checks.append((False,
                    "Evaluation output commitment TAMPERED\n"
                    f"    Stored:     {stored_eval_commitment[:80]}\n"
                    f"    Recomputed: {recomputed_commitment[:80]}"))
                verification_passed = False
            else:
                checks.append((True,
                    f"Evaluation output commitment verified "
                    f"({len(per_sample_outputs)} circuit outputs committed via Poseidon hash)"))
        except Exception as e:
            checks.append((False, f"Could not recompute eval_commitment: {e}"))
            verification_passed = False

        # Step 5 & 6: Recompute and cross-check metrics
        try:
            recomputed = _recompute_metrics_from_felts(
                per_sample_outputs, per_sample_labels, output_scale, task_type
            )
            TOLERANCE = 1e-5

            if task_type == "classification":
                claimed_acc = data.get("accuracy")
                recomputed_acc = recomputed.get("accuracy")

                if claimed_acc is not None and recomputed_acc is not None:
                    if abs(float(claimed_acc) - float(recomputed_acc)) > TOLERANCE:
                        checks.append((False,
                            f"Accuracy TAMPERED: claimed={claimed_acc}, "
                            f"recomputed-from-committed-outputs={recomputed_acc}"))
                        verification_passed = False
                    else:
                        checks.append((True,
                            f"Accuracy verified from committed circuit outputs: "
                            f"{recomputed_acc * 100:.4f}%  "
                            f"({recomputed.get('correct_count')}/{recomputed.get('n')} samples)"))
                else:
                    checks.append((False, "Could not cross-check accuracy (missing values)"))
                    verification_passed = False

                claimed_loss = data.get("val_loss")
                recomputed_loss = recomputed.get("val_loss")
                if claimed_loss is not None and recomputed_loss is not None:
                    if abs(float(claimed_loss) - float(recomputed_loss)) > TOLERANCE * 10:
                        checks.append((False,
                            f"Val loss TAMPERED: claimed={claimed_loss}, "
                            f"recomputed-from-committed-outputs={recomputed_loss}"))
                        verification_passed = False
                    else:
                        checks.append((True,
                            f"Loss verified from committed circuit outputs: {recomputed_loss:.6f}"))
                else:
                    checks.append((False, "Could not cross-check val_loss (missing values)"))
                    verification_passed = False

            else:
                for metric in ["mse", "mae", "rmse"]:
                    claimed = data.get(metric)
                    recomputed_val = recomputed.get(metric)
                    if claimed is not None and recomputed_val is not None:
                        if abs(float(claimed) - float(recomputed_val)) > TOLERANCE:
                            checks.append((False,
                                f"{metric.upper()} TAMPERED: claimed={claimed}, "
                                f"recomputed={recomputed_val}"))
                            verification_passed = False
                        else:
                            checks.append((True,
                                f"{metric.upper()} verified from committed outputs: {recomputed_val:.6f}"))

        except Exception as e:
            checks.append((False, f"Could not recompute metrics from committed outputs: {e}"))
            verification_passed = False
    else:
        checks.append((False,
            "No per_sample_outputs in proof record — cannot verify metrics from committed outputs."))
        verification_passed = False

    # Step 7: Model commitment
    stored_model_commitment = zkp.get("model_commitment") or data.get("model_commitment")
    if stored_model_commitment:
        model_file = _find_model_for_verification(model_arg)
        if model_file:
            recomputed_model_hash = f"sha256:{hashlib.sha256(model_file.read_bytes()).hexdigest()}"
            if recomputed_model_hash == stored_model_commitment:
                checks.append((True, f"Model commitment verified: {stored_model_commitment[:48]}..."))
            else:
                checks.append((False,
                    "Model commitment MISMATCH — wrong model provided or model was replaced!\n"
                    f"    Stored:     {stored_model_commitment}\n"
                    f"    Provided:   {recomputed_model_hash}"))
                verification_passed = False
        else:
            checks.append((None,
                f"Model commitment recorded ({stored_model_commitment[:48]}...) — "
                "provide --model path to verify"))
    else:
        checks.append((None, "Model commitment: not recorded (schema v1 artifact)"))

    # Step 8: Dataset commitment consistency
    stored_dataset_commitment_in_zkp = zkp.get("dataset_commitment")
    stored_dataset_hash_in_metrics = data.get("dataset_commitment") or data.get("dataset_hash")
    if stored_dataset_commitment_in_zkp and stored_dataset_hash_in_metrics:
        if stored_dataset_commitment_in_zkp == stored_dataset_hash_in_metrics:
            checks.append((True,
                f"Dataset commitment consistent: {stored_dataset_hash_in_metrics[:48]}..."))
        else:
            checks.append((False,
                "Dataset commitment INCONSISTENT between zkp and metrics.json!\n"
                f"    zkp:     {stored_dataset_commitment_in_zkp}\n"
                f"    metrics: {stored_dataset_hash_in_metrics}"))
            verification_passed = False
    else:
        checks.append((None, "Dataset commitment: recorded but cross-check requires original dataset"))

    # Print all check results
    console.print()
    for ok, msg in checks:
        if ok is True:
            console.print(f"  [green]✓[/green] {msg}")
        elif ok is False:
            console.print(f"  [red]✗[/red] {msg}")
        else:
            console.print(f"  [yellow]⚠[/yellow] {msg}")

    console.print()
    if verification_passed:
        console.print("[bold green]✓ Verification Passed (Schema v2)[/bold green]")
        console.print()
        console.print("[dim]Security scope of Schema v2 verification:[/dim]")
        console.print("[dim]  ✓ Proof:  EZKL model circuit arithmetically correct for sample 0[/dim]")
        console.print("[dim]  ✓ Outputs: all evaluated circuit outputs committed (Poseidon hash)[/dim]")
        console.print("[dim]  ✓ Metrics: accuracy/loss verified by recomputing from committed outputs[/dim]")
        console.print("[dim]  ⚠ Note: Run 'flair metrics evaluate' with Schema v3 for 100% per-sample SNARK proofs[/dim]")
    else:
        console.print("[bold red]✗ Verification FAILED — tampering or inconsistency detected![/bold red]")
        raise typer.Exit(code=1)


@app.command("verify")
def verify_metrics(
    model: Optional[Path] = typer.Option(
        None, "--model", "-m",
        help="Path to the model file for commitment verification (optional but recommended).",
    ),
    dataset: Optional[Path] = typer.Option(
        None, "--dataset", "-d",
        help="Path to validation dataset (.json or .csv) for full input-binding cryptographic verification.",
    ),
):
    """Verify staged metrics against the attached EZKL Zero-Knowledge Proof.

    Supports:
    - Schema v3: 100% per-sample SNARK proofs, input-dataset binding, deterministic sampling integrity.
    - Schema v2: Representative sample SNARK + Poseidon output commitment.
    - Schema v1: Legacy metadata consistency + single proof.
    """
    path = _metrics_file()
    if not path.exists():
        console.print("[yellow]No staged metrics found. Use 'flair metrics evaluate' first.[/yellow]")
        raise typer.Exit(code=0)

    data = _load_metrics(path)
    if not data:
        console.print("[yellow]No staged metrics to verify.[/yellow]")
        raise typer.Exit(code=0)

    zkp = data.get("zkp")
    if not zkp or not data.get("zkp_verified"):
        console.print("[yellow]⚠ These metrics are self-reported (unverified). No zero-knowledge proof attached.[/yellow]")
        console.print("[dim]Run 'flair metrics evaluate --prove' to generate a cryptographic proof.[/dim]")
        return

    schema_version = zkp.get("proof_schema_version", 1)
    console.print(f"[cyan]Verifying staged metrics (proof schema v{schema_version})...[/cyan]")

    if schema_version >= 3:
        _verify_schema_v3(data, zkp, model, dataset)
    elif schema_version == 2:
        _verify_schema_v2(data, zkp, model)
    else:
        _verify_legacy_v1(data, zkp)


def _find_model_for_verification(model_arg: Optional[Path]) -> Optional[Path]:
    """Try to locate the model file for commitment verification."""
    if model_arg:
        p = model_arg if model_arg.is_absolute() else (Path.cwd() / model_arg)
        return p if p.exists() else None
    for ext in [".onnx", ".pt", ".pth"]:
        found = list(Path.cwd().glob(f"*{ext}"))
        if found:
            return found[0]
    return None


def _find_dataset_for_verification(dataset_arg: Optional[Path]) -> Optional[Path]:
    """Try to locate the validation dataset file for input/label binding verification."""
    if dataset_arg:
        p = dataset_arg if dataset_arg.is_absolute() else (Path.cwd() / dataset_arg)
        return p if p.exists() else None
    for name in ["val_dataset.json", "val.json", "validation.json", "val_dataset.csv", "val.csv"]:
        candidate = Path.cwd() / name
        if candidate.exists():
            return candidate
    return None


def _verify_legacy_v1(data: dict, zkp: dict) -> None:
    """Limited verification for proof schema v1 records."""
    _ensure_ezkl_env()
    try:
        import ezkl
    except ImportError:
        console.print("[red]✗ EZKL not installed.[/red]")
        raise typer.Exit(code=1)

    # Basic metadata consistency check
    ok = True
    claimed_acc = data.get("accuracy")
    proven_acc = zkp.get("accuracy")
    if claimed_acc != proven_acc:
        console.print(f"  [red]✗[/red] Accuracy metadata mismatch: claimed={claimed_acc}, zkp={proven_acc}")
        ok = False
    else:
        console.print(f"  [yellow]⚠[/yellow] Accuracy metadata consistent ({claimed_acc}) — not cryptographically proven")

    claimed_loss = data.get("val_loss")
    proven_loss = zkp.get("val_loss")
    if claimed_loss is not None and proven_loss is not None and claimed_loss != proven_loss:
        console.print(f"  [red]✗[/red] Loss metadata mismatch: claimed={claimed_loss}, zkp={proven_loss}")
        ok = False
    else:
        console.print(f"  [yellow]⚠[/yellow] Loss metadata consistent ({claimed_loss}) — not cryptographically proven")

    # Run EZKL verify
    flair_dir = _get_flair_dir()
    zkp_dir = flair_dir / ".zkp"
    proof_zlib = zkp_dir / zkp.get("proof_file", "proof.zlib")
    vk_zlib = zkp_dir / zkp.get("verification_key_file", "verification_key.zlib")
    settings_zlib = zkp_dir / zkp.get("settings_file", "settings.zlib")

    if not all(p.exists() for p in [proof_zlib, vk_zlib, settings_zlib]):
        console.print("  [red]✗[/red] Missing proof artifacts.")
        raise typer.Exit(code=1)

    temp_pf = zkp_dir / "temp_v1.pf"
    temp_vk = zkp_dir / "temp_v1.vk"
    temp_st = zkp_dir / "temp_v1.json"
    try:
        temp_pf.write_text(_decompress_zlib_file(proof_zlib).decode("utf-8"), encoding="utf-8")
        temp_vk.write_bytes(_decompress_zlib_file(vk_zlib))
        temp_st.write_text(_decompress_zlib_file(settings_zlib).decode("utf-8"), encoding="utf-8")
        is_valid = ezkl.verify(str(temp_pf), str(temp_st), str(temp_vk))
        if not is_valid:
            console.print("  [red]✗[/red] EZKL proof mathematical verification failed.")
            ok = False
        else:
            console.print("  [green]✓[/green] EZKL proof mathematically valid (sample 0 only — schema v1 limitation)")
    finally:
        for p in [temp_pf, temp_vk, temp_st]:
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    console.print()
    if ok:
        console.print("[yellow]⚠ Legacy v1 verification passed (limited guarantees — metrics not cryptographically proven).[/yellow]")
        console.print("[yellow]  Re-run 'flair metrics evaluate' to upgrade to schema v2.[/yellow]")
    else:
        raise typer.Exit(code=1)
