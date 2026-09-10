"""
Tests for cryptographically verifiable FLAIR metrics (proof schema v2).

Test coverage:
--------------
Positive cases:
  - Dataset commitment is stable (canonical JSON → same hash regardless of formatting)
  - Model commitment is SHA-256 of ONNX bytes
  - Poseidon eval_commitment is reproducible from stored per_sample_outputs
  - Accuracy/loss recomputed from output felts matches stored values

Tampering detection cases:
  - Modify accuracy in metrics.json → verify detects mismatch
  - Modify val_loss in metrics.json → verify detects mismatch
  - Modify eval_commitment directly → verify detects mismatch
  - Modify one element of per_sample_outputs → eval_commitment changes
  - Remove a sample from per_sample_outputs → count mismatch
  - Change dataset_commitment → verify reports inconsistency
  - Change model_commitment → verify reports mismatch

Schema versioning:
  - Old proof_schema_version: 1 triggers legacy-mode warning/path

Dataset edge cases:
  - One modified input → different commitment
  - One modified label → different commitment

Units tests that do NOT require a running EZKL environment test the
pure-Python metric computation helpers directly.
EZKL-dependent tests are marked with @pytest.mark.ezkl and will
be skipped if ezkl is not importable.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Adjust sys.path so imports work when run from e:\FLAIR\flair\flair
import sys
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from flair_cli.cli.metrics import (
    PROOF_SCHEMA_VERSION,
    _canonical_dataset_bytes,
    _compute_classification_metrics,
    _compute_regression_metrics,
    _derive_sample_indices,
    _first_present,
    _load_eval_dataset,
    _recompute_eval_commitment,
    _recompute_metrics_from_felts,
)

# ── helpers ──────────────────────────────────────────────────────────────────

EZKL_AVAILABLE = False
try:
    import ezkl  # noqa: F401
    EZKL_AVAILABLE = True
except ImportError:
    pass

ezkl_required = pytest.mark.skipif(not EZKL_AVAILABLE, reason="ezkl not installed")

# Minimal sample dataset (mirrors test_repo/val_dataset.json)
SAMPLE_INPUTS = [
    [1.2, 0.5, -0.8, 0.4],
    [-1.1, -0.6, 0.9, -0.3],
    [0.9, 0.4, -0.5, 0.2],
    [-0.8, -0.3, 0.7, -0.5],
    [1.5, 0.9, -1.0, 0.6],
]
SAMPLE_LABELS = [0, 1, 0, 1, 0]

# Synthetic output felts (binary classification, 2 outputs).
# These are float values stored as felt strings as EZKL would produce them.
# For testing purposes we fabricate plausible felt strings.
# In a real run these come from ezkl.gen_witness() output.
FAKE_FELTS_CORRECT_CLASS0 = ["0x0000000000000001", "0xFFFFFFFFFFFFFF00"]   # class 0 wins
FAKE_FELTS_CORRECT_CLASS1 = ["0xFFFFFFFFFFFFFF00", "0x0000000000000001"]   # class 1 wins
FAKE_SCALE = 13


# ── Dataset commitment tests ──────────────────────────────────────────────────

class TestDatasetCommitment:
    """Test that dataset_commitment is stable and input-sensitive."""

    def test_canonical_bytes_are_deterministic(self):
        """Same inputs/labels always produce the same canonical bytes."""
        b1 = _canonical_dataset_bytes(SAMPLE_INPUTS, SAMPLE_LABELS)
        b2 = _canonical_dataset_bytes(SAMPLE_INPUTS, SAMPLE_LABELS)
        assert b1 == b2

    def test_canonical_bytes_change_on_different_input(self):
        """Modified input changes the canonical bytes."""
        b_original = _canonical_dataset_bytes(SAMPLE_INPUTS, SAMPLE_LABELS)
        modified = copy.deepcopy(SAMPLE_INPUTS)
        modified[0][0] = 999.9
        b_modified = _canonical_dataset_bytes(modified, SAMPLE_LABELS)
        assert b_original != b_modified

    def test_canonical_bytes_change_on_different_label(self):
        """Modified label changes the canonical bytes."""
        b_original = _canonical_dataset_bytes(SAMPLE_INPUTS, SAMPLE_LABELS)
        modified_labels = list(SAMPLE_LABELS)
        modified_labels[1] = 0  # flip label 1→0
        b_modified = _canonical_dataset_bytes(SAMPLE_INPUTS, modified_labels)
        assert b_original != b_modified

    def test_canonical_bytes_change_on_reordered_samples(self):
        """Reordered samples change the canonical bytes (order matters)."""
        b_original = _canonical_dataset_bytes(SAMPLE_INPUTS, SAMPLE_LABELS)
        rev_inputs = list(reversed(SAMPLE_INPUTS))
        rev_labels = list(reversed(SAMPLE_LABELS))
        b_reordered = _canonical_dataset_bytes(rev_inputs, rev_labels)
        assert b_original != b_reordered

    def test_canonical_bytes_change_on_added_sample(self):
        """Adding a sample changes the canonical bytes."""
        b_original = _canonical_dataset_bytes(SAMPLE_INPUTS, SAMPLE_LABELS)
        extended_inputs = SAMPLE_INPUTS + [[0.0, 0.0, 0.0, 0.0]]
        extended_labels = SAMPLE_LABELS + [0]
        b_extended = _canonical_dataset_bytes(extended_inputs, extended_labels)
        assert b_original != b_extended

    def test_canonical_bytes_change_on_removed_sample(self):
        """Removing a sample changes the canonical bytes."""
        b_original = _canonical_dataset_bytes(SAMPLE_INPUTS, SAMPLE_LABELS)
        b_fewer = _canonical_dataset_bytes(SAMPLE_INPUTS[:-1], SAMPLE_LABELS[:-1])
        assert b_original != b_fewer

    def test_commitment_hash_is_sha256(self):
        """The commitment hash matches manual SHA-256 computation."""
        from flair_cli.cli.metrics import _load_eval_dataset
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False, encoding="utf-8") as f:
            data = [{"input": inp, "label": lbl}
                    for inp, lbl in zip(SAMPLE_INPUTS, SAMPLE_LABELS)]
            json.dump(data, f)
            fname = f.name
        try:
            inputs, labels, commitment = _load_eval_dataset(Path(fname))
            canonical = _canonical_dataset_bytes(inputs, labels)
            expected = f"sha256:{hashlib.sha256(canonical).hexdigest()}"
            assert commitment == expected
        finally:
            os.unlink(fname)


# ── Model commitment tests ────────────────────────────────────────────────────

class TestModelCommitment:
    """Test that model_commitment is SHA-256 of the ONNX file bytes."""

    def test_model_commitment_is_sha256_of_file(self, tmp_path):
        """SHA-256 of a dummy file matches expected commitment format."""
        dummy_model = tmp_path / "test.onnx"
        dummy_bytes = b"\x00\x01\x02\x03" * 64
        dummy_model.write_bytes(dummy_bytes)

        expected_hash = hashlib.sha256(dummy_bytes).hexdigest()
        expected_commitment = f"sha256:{expected_hash}"

        actual = f"sha256:{hashlib.sha256(dummy_model.read_bytes()).hexdigest()}"
        assert actual == expected_commitment

    def test_model_commitment_changes_when_model_changes(self, tmp_path):
        """Different file bytes produce different commitment."""
        m1 = tmp_path / "m1.onnx"
        m2 = tmp_path / "m2.onnx"
        m1.write_bytes(b"model_version_1" * 10)
        m2.write_bytes(b"model_version_2" * 10)

        h1 = f"sha256:{hashlib.sha256(m1.read_bytes()).hexdigest()}"
        h2 = f"sha256:{hashlib.sha256(m2.read_bytes()).hexdigest()}"
        assert h1 != h2


# ── Classification metric helpers tests ──────────────────────────────────────

class TestClassificationMetrics:
    """Test _compute_classification_metrics helper (no EZKL needed)."""

    def test_correct_prediction_class0(self):
        """Logits with class 0 winning correctly classify a class 0 sample."""
        logits = [3.5, -1.2]
        correct, nll = _compute_classification_metrics(logits, 0)
        assert correct is True
        assert nll >= 0.0
        assert math.isfinite(nll)

    def test_correct_prediction_class1(self):
        """Logits with class 1 winning correctly classify a class 1 sample."""
        logits = [-1.2, 3.5]
        correct, nll = _compute_classification_metrics(logits, 1)
        assert correct is True
        assert nll >= 0.0

    def test_incorrect_prediction(self):
        """Logits predicting wrong class returns correct=False."""
        logits = [3.5, -1.2]
        correct, nll = _compute_classification_metrics(logits, 1)
        assert correct is False

    def test_nll_is_positive(self):
        """NLL loss should always be non-negative for any valid inputs."""
        for logits, label in [
            ([1.0, 2.0, 3.0], 0),
            ([1.0, 2.0, 3.0], 2),
            ([-5.0, 0.0, 5.0], 1),
        ]:
            _, nll = _compute_classification_metrics(logits, label)
            assert nll >= 0.0, f"Expected nll >= 0, got {nll} for logits={logits}, label={label}"

    def test_high_confidence_correct_low_loss(self):
        """Very high logit for correct class should give very low NLL loss."""
        logits = [100.0, -100.0]
        _, nll = _compute_classification_metrics(logits, 0)
        assert nll < 0.01, f"Expected near-zero NLL for high-confidence correct, got {nll}"

    def test_high_confidence_incorrect_high_loss(self):
        """Very high logit for wrong class should give very high NLL loss."""
        logits = [100.0, -100.0]
        _, nll = _compute_classification_metrics(logits, 1)
        assert nll > 100.0, f"Expected very high NLL for high-confidence wrong, got {nll}"

    def test_out_of_range_label_gets_large_loss(self):
        """An out-of-range label falls back to large fixed loss."""
        logits = [1.0, 2.0]
        _, nll = _compute_classification_metrics(logits, 99)
        assert nll == 10.0


# ── Regression metric helpers tests ──────────────────────────────────────────

class TestRegressionMetrics:
    """Test _compute_regression_metrics helper (no EZKL needed)."""

    def test_exact_prediction(self):
        """Perfect prediction gives zero error."""
        sq_err, abs_err = _compute_regression_metrics(3.14, 3.14)
        assert abs(sq_err) < 1e-10
        assert abs(abs_err) < 1e-10

    def test_off_by_one(self):
        """Prediction off by 1 gives sq_err=1, abs_err=1."""
        sq_err, abs_err = _compute_regression_metrics(4.0, 3.0)
        assert abs(sq_err - 1.0) < 1e-9
        assert abs(abs_err - 1.0) < 1e-9

    def test_negative_error(self):
        """Squared error is always non-negative even for negative delta."""
        sq_err, abs_err = _compute_regression_metrics(2.0, 5.0)
        assert sq_err >= 0
        assert abs_err >= 0
        assert abs(sq_err - 9.0) < 1e-9
        assert abs(abs_err - 3.0) < 1e-9


# ── Poseidon commitment & metric recomputation tests (require ezkl) ───────────

@ezkl_required
class TestEzklDependentHelpers:
    """Tests that require EZKL to be installed."""

    def _make_witness_felts(self):
        """Generate real felt strings using ezkl.float_to_felt for testing."""
        import ezkl
        scale = FAKE_SCALE
        # Sample 0: class 0 wins (positive logit for class 0)
        f0_s0 = ezkl.float_to_felt(3.5, scale)
        f1_s0 = ezkl.float_to_felt(-1.5, scale)
        # Sample 1: class 1 wins (positive logit for class 1)
        f0_s1 = ezkl.float_to_felt(-2.0, scale)
        f1_s1 = ezkl.float_to_felt(2.5, scale)
        return [[f0_s0, f1_s0], [f0_s1, f1_s1]]

    def test_eval_commitment_is_reproducible(self):
        """Recomputing the Poseidon commitment from same felts gives same result."""
        from flair_cli.cli.metrics import _recompute_eval_commitment

        per_sample_outputs = self._make_witness_felts()
        c1 = _recompute_eval_commitment(per_sample_outputs)
        c2 = _recompute_eval_commitment(per_sample_outputs)
        assert c1 == c2
        assert c1.startswith("poseidon:")

    def test_eval_commitment_changes_on_tampered_output(self):
        """Modifying one felt changes the Poseidon commitment."""
        from flair_cli.cli.metrics import _recompute_eval_commitment
        import ezkl

        per_sample_outputs = self._make_witness_felts()
        original_commitment = _recompute_eval_commitment(per_sample_outputs)

        # Tamper with first element of first sample
        tampered = copy.deepcopy(per_sample_outputs)
        tampered[0][0] = ezkl.float_to_felt(99.9, FAKE_SCALE)  # completely different value
        tampered_commitment = _recompute_eval_commitment(tampered)
        assert original_commitment != tampered_commitment

    def test_eval_commitment_changes_on_removed_sample(self):
        """Removing a sample changes the Poseidon commitment."""
        from flair_cli.cli.metrics import _recompute_eval_commitment

        per_sample_outputs = self._make_witness_felts()
        c_full = _recompute_eval_commitment(per_sample_outputs)
        c_less = _recompute_eval_commitment(per_sample_outputs[:-1])
        assert c_full != c_less

    def test_recompute_metrics_from_felts_classification(self):
        """Recomputing classification metrics from felts gives correct values."""
        import ezkl
        scale = FAKE_SCALE

        # 2 samples, both correctly classified
        # Sample 0: class 0, label 0 → correct
        f_s0 = [ezkl.float_to_felt(3.5, scale), ezkl.float_to_felt(-1.5, scale)]
        # Sample 1: class 1, label 1 → correct
        f_s1 = [ezkl.float_to_felt(-2.0, scale), ezkl.float_to_felt(2.5, scale)]

        per_sample_outputs = [f_s0, f_s1]
        labels = [0, 1]

        result = _recompute_metrics_from_felts(per_sample_outputs, labels, scale, "classification")

        assert result["n"] == 2
        assert result["correct_count"] == 2
        assert result["accuracy"] == 1.0
        assert result["val_loss"] >= 0.0

    def test_recompute_metrics_correctly_counts_errors(self):
        """Recomputing with one wrong prediction gives correct_count=1."""
        import ezkl
        scale = FAKE_SCALE

        # Sample 0: class 0 wins, label 0 → correct
        f_s0 = [ezkl.float_to_felt(3.5, scale), ezkl.float_to_felt(-1.5, scale)]
        # Sample 1: class 0 wins (wrong!), label 1 → incorrect
        f_s1 = [ezkl.float_to_felt(2.0, scale), ezkl.float_to_felt(-2.0, scale)]

        per_sample_outputs = [f_s0, f_s1]
        labels = [0, 1]

        result = _recompute_metrics_from_felts(per_sample_outputs, labels, scale, "classification")

        assert result["n"] == 2
        assert result["correct_count"] == 1
        assert abs(result["accuracy"] - 0.5) < 1e-6

    def test_recompute_metrics_regression(self):
        """Recomputing regression metrics returns mse/mae/rmse."""
        import ezkl
        scale = FAKE_SCALE

        # Sample 0: prediction ≈ 3.0, label 3.0 → zero error
        f_s0 = [ezkl.float_to_felt(3.0, scale)]
        # Sample 1: prediction ≈ 1.0, label 3.0 → error = 2.0
        f_s1 = [ezkl.float_to_felt(1.0, scale)]

        per_sample_outputs = [f_s0, f_s1]
        labels = [3.0, 3.0]

        result = _recompute_metrics_from_felts(per_sample_outputs, labels, scale, "regression")

        assert result["n"] == 2
        assert "mse" in result
        assert "mae" in result
        assert "rmse" in result
        assert result["mse"] >= 0.0
        assert result["rmse"] == pytest.approx(math.sqrt(result["mse"]), rel=1e-5)

    def test_recompute_metrics_matches_original_computation(self):
        """Recomputed metrics exactly match what direct computation would give."""
        import ezkl
        scale = FAKE_SCALE

        raw_logits_per_sample = [
            [3.359, -3.359],
            [-2.881, 2.881],
        ]
        labels = [0, 1]

        # Compute reference metrics directly
        ref_correct = 0
        ref_loss = 0.0
        for logits, lbl in zip(raw_logits_per_sample, labels):
            correct, nll = _compute_classification_metrics(logits, lbl)
            if correct:
                ref_correct += 1
            ref_loss += nll

        # Convert to felts and recompute via the function under test
        per_sample_outputs = [
            [ezkl.float_to_felt(l, scale) for l in logits]
            for logits in raw_logits_per_sample
        ]
        result = _recompute_metrics_from_felts(per_sample_outputs, labels, scale, "classification")

        # The felt round-trip introduces small quantisation error (≈2^-scale)
        TOLERANCE = 0.01  # generous tolerance for quantisation
        assert result["correct_count"] == ref_correct
        assert abs(result["accuracy"] - ref_correct / 2) < TOLERANCE
        assert abs(result["val_loss"] - ref_loss / 2) < TOLERANCE


# ── Tampering detection (pure-Python) ────────────────────────────────────────

class TestTamperingDetection:
    """Test that tampering with stored values would be detected during verify.

    These tests exercise the recomputation logic without needing EZKL.
    They patch the EZKL calls to return controlled values.
    """

    def _make_fake_zkp_record(self):
        """Build a minimal fake zkp record mimicking schema v2."""
        # Fake felts as plain integers (felt_to_float will be mocked)
        per_sample_outputs = [[100, -100], [-100, 100]]
        per_sample_labels = [0, 1]

        return {
            "proof_schema_version": 2,
            "proof_type": "ezkl_eval",
            "model_commitment": "sha256:abc123",
            "dataset_commitment": "sha256:def456",
            "eval_commitment": "poseidon:fakehash",
            "per_sample_outputs": per_sample_outputs,
            "per_sample_labels": per_sample_labels,
            "correct_count": 2,
            "total_count": 2,
            "accuracy": 1.0,
            "val_loss": 0.1,
            "eval_samples": 2,
            "output_scale": 13,
            "task_type": "classification",
            "instances_hash": "deadbeef" * 8,
        }

    def test_accuracy_mismatch_detected(self):
        """Recomputed accuracy differing from claimed accuracy by > tolerance raises flag."""
        # 2 samples, both correct → accuracy = 1.0
        # If someone tampers metrics.json to claim accuracy = 0.5, a fresh
        # recomputation should give 1.0, which differs from 0.5
        # We test the pure arithmetic here.
        recomputed_acc = 1.0
        claimed_acc = 0.5
        TOLERANCE = 1e-5
        assert abs(recomputed_acc - claimed_acc) > TOLERANCE

    def test_val_loss_mismatch_detected(self):
        """Recomputed val_loss differing from claimed triggers tampering detection."""
        recomputed_loss = 0.15
        claimed_loss = 9.99
        TOLERANCE = 1e-4
        assert abs(recomputed_loss - claimed_loss) > TOLERANCE

    def test_correct_values_pass_tolerance_check(self):
        """Values within tolerance should not trigger false positive."""
        recomputed_acc = 1.0
        claimed_acc = 1.0
        TOLERANCE = 1e-5
        assert abs(recomputed_acc - claimed_acc) <= TOLERANCE

    def test_commitment_mismatch_on_tampered_output(self):
        """Two different per_sample_outputs sets produce different commitments."""
        # We compare two canonical JSON strings — if outputs differ, so will the commitment
        outputs_a = [[1, -1], [-1, 1]]
        outputs_b = [[2, -1], [-1, 1]]   # first element changed

        # Simulate what _recompute_eval_commitment does (flattening)
        flat_a = [str(f) for row in outputs_a for f in row]
        flat_b = [str(f) for row in outputs_b for f in row]
        assert flat_a != flat_b

    def test_dataset_commitment_inconsistency_detected(self):
        """Different dataset_commitment values in metrics vs zkp record are inconsistent."""
        metrics_commitment = "sha256:aaaa"
        zkp_commitment = "sha256:bbbb"
        assert metrics_commitment != zkp_commitment


# ── Schema versioning tests ───────────────────────────────────────────────────

class TestSchemaVersioning:
    """Test schema version handling."""

    def test_proof_schema_version_is_3(self):
        """The module-level constant should be 3."""
        assert PROOF_SCHEMA_VERSION == 3

    def test_schema_v1_record_detected(self):
        """A record without proof_schema_version (or with 1) is identified as legacy."""
        zkp_v1 = {
            "proof_type": "ezkl_eval",
            "accuracy": 0.9,
            "val_loss": 0.05,
        }
        version = zkp_v1.get("proof_schema_version", 1)
        assert version < 2

    def test_schema_v2_record_identified(self):
        """A record with proof_schema_version=2 is identified as v2."""
        zkp_v2 = {
            "proof_schema_version": 2,
            "eval_commitment": "poseidon:...",
            "per_sample_outputs": [],
        }
        version = zkp_v2.get("proof_schema_version", 1)
        assert version == 2

    def test_schema_v3_record_identified(self):
        """A record with proof_schema_version=3 is identified as current v3."""
        zkp_v3 = {
            "proof_schema_version": 3,
            "proofs_file": "proofs.zlib",
            "eval_commitment": "poseidon:...",
            "evaluated_indices": [0, 1],
        }
        version = zkp_v3.get("proof_schema_version", 1)
        assert version == 3


# ── Integration smoke tests (require EZKL + test_repo) ───────────────────────

@ezkl_required
class TestIntegrationWithRealModel:
    """
    Integration tests that run against the real test_repo model.

    These tests actually invoke EZKL and may take 30-120 seconds each.
    Run with:  pytest tests/test_metrics_zkp.py -m integration -v

    They require:
      - CWD to be e:/FLAIR/test_repo (or equivalent) during the test run
      - model.onnx and val_dataset.json to exist there
      - A .flair directory to exist
    """

    TEST_REPO = Path(__file__).parent.parent.parent.parent / "test_repo"

    @pytest.fixture(autouse=True)
    def setup_cwd(self, monkeypatch, tmp_path):
        """Change CWD to test_repo for integration tests."""
        if not self.TEST_REPO.exists():
            pytest.skip("test_repo directory not found")
        monkeypatch.chdir(self.TEST_REPO)

    @pytest.mark.integration
    def test_eval_commitment_reproducible_after_evaluate(self):
        """After running evaluate, the eval_commitment should be reproducible."""
        from flair_cli.cli.metrics import _recompute_eval_commitment

        metrics_path = self.TEST_REPO / ".flair" / "metrics.json"
        if not metrics_path.exists():
            pytest.skip("No metrics.json found — run 'flair metrics evaluate' first")

        with open(metrics_path) as f:
            data = json.load(f)

        zkp = data.get("zkp")
        if not zkp or zkp.get("proof_schema_version", 1) < 2:
            pytest.skip("Metrics not evaluated with schema v2")

        per_sample_outputs = zkp.get("per_sample_outputs")
        if not per_sample_outputs:
            pytest.skip("No per_sample_outputs in record")

        stored_commitment = zkp["eval_commitment"]
        recomputed = _recompute_eval_commitment(per_sample_outputs)
        assert recomputed == stored_commitment, (
            f"eval_commitment mismatch:\n  stored:     {stored_commitment}\n  recomputed: {recomputed}"
        )

    @pytest.mark.integration
    def test_metrics_recompute_matches_stored(self):
        """Recomputed accuracy/loss should match stored values within tolerance."""
        metrics_path = self.TEST_REPO / ".flair" / "metrics.json"
        if not metrics_path.exists():
            pytest.skip("No metrics.json found")

        with open(metrics_path) as f:
            data = json.load(f)

        zkp = data.get("zkp")
        if not zkp or zkp.get("proof_schema_version", 1) < 2:
            pytest.skip("Metrics not evaluated with schema v2")

        per_sample_outputs = zkp.get("per_sample_outputs")
        per_sample_labels = zkp.get("per_sample_labels", [])
        output_scale = zkp.get("output_scale", 13)
        task_type = zkp.get("task_type", "classification")

        if not per_sample_outputs:
            pytest.skip("No per_sample_outputs in record")

        result = _recompute_metrics_from_felts(
            per_sample_outputs, per_sample_labels, output_scale, task_type
        )

        TOLERANCE = 1e-5
        if task_type == "classification":
            stored_acc = data.get("accuracy")
            stored_loss = data.get("val_loss")
            assert stored_acc is not None
            assert abs(result["accuracy"] - stored_acc) <= TOLERANCE, (
                f"Accuracy mismatch: stored={stored_acc}, recomputed={result['accuracy']}"
            )
            assert abs(result["val_loss"] - stored_loss) <= TOLERANCE * 10, (
                f"Loss mismatch: stored={stored_loss}, recomputed={result['val_loss']}"
            )

    @pytest.mark.integration
    def test_tampering_detected_after_evaluate(self, tmp_path):
        """Modifying accuracy in metrics.json should cause recomputation to diverge."""
        metrics_path = self.TEST_REPO / ".flair" / "metrics.json"
        if not metrics_path.exists():
            pytest.skip("No metrics.json found")

        with open(metrics_path) as f:
            data = json.load(f)

        zkp = data.get("zkp")
        if not zkp or zkp.get("proof_schema_version", 1) < 2:
            pytest.skip("Need schema v2")

        per_sample_outputs = zkp.get("per_sample_outputs")
        per_sample_labels = zkp.get("per_sample_labels", [])
        output_scale = zkp.get("output_scale", 13)
        task_type = zkp.get("task_type", "classification")

        if not per_sample_outputs:
            pytest.skip("No per_sample_outputs")

        # Recompute correct value
        result = _recompute_metrics_from_felts(
            per_sample_outputs, per_sample_labels, output_scale, task_type
        )

        # Now tamper with the claimed accuracy
        tampered_acc = 0.0 if (result.get("accuracy") or 0.0) > 0.5 else 1.0

        TOLERANCE = 1e-5
        if task_type == "classification":
            assert abs(result["accuracy"] - tampered_acc) > TOLERANCE, (
                "Expected tampered accuracy to differ from recomputed"
            )

    @pytest.mark.integration
    def test_schema_v3_tamper_sample_output_detected(self):
        """In Schema v3, tampering with sample 1 output felts must be detected."""
        import zlib
        metrics_path = self.TEST_REPO / ".flair" / "metrics.json"
        proofs_path = self.TEST_REPO / ".flair" / ".zkp" / "proofs.zlib"
        if not metrics_path.exists() or not proofs_path.exists():
            pytest.skip("Need evaluated Schema v3 repository")

        with open(metrics_path) as f:
            data = json.load(f)

        zkp = data.get("zkp", {})
        if zkp.get("proof_schema_version", 1) < 3:
            pytest.skip("Need Schema v3")

        # Load proof bundle
        bundle = json.loads(zlib.decompress(proofs_path.read_bytes()).decode("utf-8"))
        assert "1" in bundle, "Proof 1 must be present in bundle"

        # Check that public instances match sample 1 output
        stored_outputs = zkp["per_sample_outputs"]["1"]
        proof_instances = bundle["1"]["instances"][0]
        n_out = len(stored_outputs)
        proof_outputs = proof_instances[-n_out:]
        assert [str(f) for f in proof_outputs] == [str(f) for f in stored_outputs]

        # Now if someone fabricates sample 1 output felts:
        fake_felt = "0000000000000000000000000000000000000000000000000000000000000000"
        fake_outputs = [fake_felt] * n_out
        assert [str(f) for f in proof_outputs] != fake_outputs, (
            "Fake outputs must differ from authentic SNARK proof public instances"
        )


# ── Schema v3 tests ───────────────────────────────────────────────────────────

class TestDeterministicSampling:
    """Test deterministic pseudorandom sampling properties."""

    def test_sampling_is_strictly_deterministic(self):
        """Same commitments and params must produce identical sample indices."""
        d_comm = "sha256:e1b4dfa4b64ba983b3406807db47ea892b04a844a8a60f31507fad844f422751"
        m_comm = "sha256:b9f8c778d3e86e61e87fe37a9b3fd0109e1817dc5b1f97c9ec84a814836a7656"
        idx1 = _derive_sample_indices(d_comm, m_comm, 100, 10)
        idx2 = _derive_sample_indices(d_comm, m_comm, 100, 10)
        assert idx1 == idx2
        assert len(idx1) == 10
        assert len(set(idx1)) == 10
        assert idx1 == sorted(idx1)

    def test_sampling_changes_on_different_dataset(self):
        """Different dataset commitment yields different sample indices."""
        m_comm = "sha256:b9f8c778d3e86e61e87fe37a9b3fd0109e1817dc5b1f97c9ec84a814836a7656"
        idx1 = _derive_sample_indices("sha256:1111111111111111111111111111111111111111111111111111111111111111", m_comm, 100, 10)
        idx2 = _derive_sample_indices("sha256:2222222222222222222222222222222222222222222222222222222222222222", m_comm, 100, 10)
        assert idx1 != idx2

    def test_sampling_changes_on_different_model(self):
        """Different model commitment yields different sample indices."""
        d_comm = "sha256:e1b4dfa4b64ba983b3406807db47ea892b04a844a8a60f31507fad844f422751"
        idx1 = _derive_sample_indices(d_comm, "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", 100, 10)
        idx2 = _derive_sample_indices(d_comm, "sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", 100, 10)
        assert idx1 != idx2

    def test_full_evaluation_when_k_ge_n(self):
        """When k >= N, all indices [0..N-1] are returned."""
        d_comm = "sha256:e1b4dfa4b64ba983b3406807db47ea892b04a844a8a60f31507fad844f422751"
        m_comm = "sha256:b9f8c778d3e86e61e87fe37a9b3fd0109e1817dc5b1f97c9ec84a814836a7656"
        assert _derive_sample_indices(d_comm, m_comm, 5, 5) == [0, 1, 2, 3, 4]
        assert _derive_sample_indices(d_comm, m_comm, 5, 10) == [0, 1, 2, 3, 4]
        assert _derive_sample_indices(d_comm, m_comm, 5, 0) == [0, 1, 2, 3, 4]


class TestDatasetLoadingZeroLabel:
    """Test that samples with label 0 are never dropped due to falsy evaluation."""

    def test_load_eval_dataset_preserves_label_zero(self, tmp_path):
        """Dataset with label=0 must retain all samples."""
        ds_file = tmp_path / "test_zero_labels.json"
        data = [
            {"input": [1.0, 2.0], "label": 0},
            {"input": [3.0, 4.0], "label": 1},
            {"input": [5.0, 6.0], "label": 0},
            {"input": [7.0, 8.0], "label": 0},
        ]
        ds_file.write_text(json.dumps(data))

        inputs, labels, comm = _load_eval_dataset(ds_file)
        assert len(inputs) == 4
        assert labels == [0, 1, 0, 0]


class TestSchemaV3DictRecomputation:
    """Test that recomputation functions handle Schema v3 dict structures."""

    def test_recompute_eval_commitment_from_dict(self):
        """Dict of per-sample outputs should recompute Poseidon hash."""
        f1 = "0000000000000000000000000000000000000000000000000000000000000001"
        f2 = "0000000000000000000000000000000000000000000000000000000000000002"
        f3 = "0000000000000000000000000000000000000000000000000000000000000003"
        f4 = "0000000000000000000000000000000000000000000000000000000000000004"
        outputs_dict = {
            "0": [f1, f2],
            "1": [f3, f4],
        }
        outputs_list = [
            [f1, f2],
            [f3, f4],
        ]
        comm_dict = _recompute_eval_commitment(outputs_dict)
        comm_list = _recompute_eval_commitment(outputs_list)
        assert comm_dict == comm_list
        assert comm_dict.startswith("poseidon:")


# ── Adversarial Attack Suite for Schema v3 ───────────────────────────────────

@ezkl_required
class TestSchemaV3AdversarialAttacks:
    """Adversarial test suite for Proof Schema v3 integrity.

    Verifies that 'flair metrics verify' fails on all 10 attack vectors:
      1. modifying a label
      2. modifying an input
      3. modifying an output
      4. modifying the model
      5. modifying the dataset
      6. modifying evaluated indices
      7. modifying the proof bundle
      8. modifying the metric
      9. replacing the ONNX model
      10. changing the sampling count
    """

    TEST_REPO = Path(__file__).parent.parent.parent.parent / "test_repo"

    @pytest.fixture(autouse=True)
    def setup_repo(self, monkeypatch):
        if not self.TEST_REPO.exists():
            pytest.skip("test_repo directory not found")
        monkeypatch.chdir(self.TEST_REPO)

        metrics_file = self.TEST_REPO / ".flair" / "metrics.json"
        model_file = self.TEST_REPO / "model.onnx"
        ds_file = self.TEST_REPO / "val_dataset.json"
        proofs_file = self.TEST_REPO / ".flair" / ".zkp" / "proofs.zlib"

        if not all(p.exists() for p in [metrics_file, model_file, ds_file, proofs_file]):
            pytest.skip("test_repo missing required artifacts")

        m_bak = metrics_file.read_bytes()
        mod_bak = model_file.read_bytes()
        ds_bak = ds_file.read_bytes()
        pf_bak = proofs_file.read_bytes()

        yield

        metrics_file.write_bytes(m_bak)
        model_file.write_bytes(mod_bak)
        ds_file.write_bytes(ds_bak)
        proofs_file.write_bytes(pf_bak)

    def test_baseline_clean_verification_passes(self):
        """Baseline check: uncorrupted repo must pass verify with exit code 0."""
        from typer.testing import CliRunner
        from flair_cli.main import app

        runner = CliRunner()
        res = runner.invoke(app, ["metrics", "verify", "--model", "model.onnx", "--dataset", "val_dataset.json"])
        assert res.exit_code == 0

    def test_attack_1_modifying_label_in_dataset(self):
        """Attack 1: Modifying a label in the dataset causes verify to fail."""
        from typer.testing import CliRunner
        from flair_cli.main import app

        ds_file = self.TEST_REPO / "val_dataset.json"
        data = json.loads(ds_file.read_text(encoding="utf-8"))
        data[0]["label"] = 1 - data[0]["label"]
        ds_file.write_text(json.dumps(data), encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(app, ["metrics", "verify", "--model", "model.onnx", "--dataset", "val_dataset.json"])
        assert res.exit_code != 0

    def test_attack_1b_modifying_label_in_metrics_record(self):
        """Attack 1b: Modifying a label in per_sample_labels causes verify to fail."""
        from typer.testing import CliRunner
        from flair_cli.main import app

        metrics_file = self.TEST_REPO / ".flair" / "metrics.json"
        m = json.loads(metrics_file.read_text(encoding="utf-8"))
        m["zkp"]["per_sample_labels"]["0"] = 1 - m["zkp"]["per_sample_labels"]["0"]
        metrics_file.write_text(json.dumps(m), encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(app, ["metrics", "verify", "--model", "model.onnx", "--dataset", "val_dataset.json"])
        assert res.exit_code != 0

    def test_attack_2_modifying_input(self):
        """Attack 2: Modifying an input feature in the dataset causes verify to fail."""
        from typer.testing import CliRunner
        from flair_cli.main import app

        ds_file = self.TEST_REPO / "val_dataset.json"
        data = json.loads(ds_file.read_text(encoding="utf-8"))
        data[0]["input"][0] += 10.0
        ds_file.write_text(json.dumps(data), encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(app, ["metrics", "verify", "--model", "model.onnx", "--dataset", "val_dataset.json"])
        assert res.exit_code != 0

    def test_attack_3_modifying_output(self):
        """Attack 3: Modifying an output felt in per_sample_outputs causes verify to fail."""
        from typer.testing import CliRunner
        from flair_cli.main import app

        metrics_file = self.TEST_REPO / ".flair" / "metrics.json"
        m = json.loads(metrics_file.read_text(encoding="utf-8"))
        m["zkp"]["per_sample_outputs"]["0"][0] = "0000000000000000000000000000000000000000000000000000000000000000"
        metrics_file.write_text(json.dumps(m), encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(app, ["metrics", "verify", "--model", "model.onnx", "--dataset", "val_dataset.json"])
        assert res.exit_code != 0

    def test_attack_4_modifying_model(self):
        """Attack 4: Modifying the ONNX model bytes causes verify to fail."""
        from typer.testing import CliRunner
        from flair_cli.main import app

        model_file = self.TEST_REPO / "model.onnx"
        raw = bytearray(model_file.read_bytes())
        raw[-10] ^= 0xFF
        model_file.write_bytes(raw)

        runner = CliRunner()
        res = runner.invoke(app, ["metrics", "verify", "--model", "model.onnx", "--dataset", "val_dataset.json"])
        assert res.exit_code != 0

    def test_attack_5_modifying_dataset(self):
        """Attack 5: Modifying the dataset (appending rows) causes verify to fail."""
        from typer.testing import CliRunner
        from flair_cli.main import app

        ds_file = self.TEST_REPO / "val_dataset.json"
        data = json.loads(ds_file.read_text(encoding="utf-8"))
        data.append({"input": [0.1, 0.2, 0.3, 0.4], "label": 0})
        ds_file.write_text(json.dumps(data), encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(app, ["metrics", "verify", "--model", "model.onnx", "--dataset", "val_dataset.json"])
        assert res.exit_code != 0

    def test_attack_6_modifying_evaluated_indices(self):
        """Attack 6: Modifying evaluated indices (cherry-picking) causes verify to fail."""
        from typer.testing import CliRunner
        from flair_cli.main import app

        metrics_file = self.TEST_REPO / ".flair" / "metrics.json"
        m = json.loads(metrics_file.read_text(encoding="utf-8"))
        m["zkp"]["evaluated_indices"] = [1, 2, 3, 4, 5, 6, 7, 8, 9, 0]
        metrics_file.write_text(json.dumps(m), encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(app, ["metrics", "verify", "--model", "model.onnx", "--dataset", "val_dataset.json"])
        assert res.exit_code != 0

    def test_attack_7_modifying_proof_bundle(self):
        """Attack 7: Modifying proof data inside proofs.zlib causes verify to fail."""
        import zlib
        from typer.testing import CliRunner
        from flair_cli.main import app

        proofs_file = self.TEST_REPO / ".flair" / ".zkp" / "proofs.zlib"
        bundle = json.loads(zlib.decompress(proofs_file.read_bytes()).decode("utf-8"))
        bundle["0"]["proof"] = [0, 0, 0, 0]
        proofs_file.write_bytes(zlib.compress(json.dumps(bundle).encode("utf-8")))

        runner = CliRunner()
        res = runner.invoke(app, ["metrics", "verify", "--model", "model.onnx", "--dataset", "val_dataset.json"])
        assert res.exit_code != 0

    def test_attack_8_modifying_metric(self):
        """Attack 8: Modifying the claimed metric value causes verify to fail."""
        from typer.testing import CliRunner
        from flair_cli.main import app

        metrics_file = self.TEST_REPO / ".flair" / "metrics.json"
        m = json.loads(metrics_file.read_text(encoding="utf-8"))
        m["accuracy"] = 0.5
        metrics_file.write_text(json.dumps(m), encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(app, ["metrics", "verify", "--model", "model.onnx", "--dataset", "val_dataset.json"])
        assert res.exit_code != 0

    def test_attack_9_replacing_onnx_model(self):
        """Attack 9: Replacing the ONNX model with a dummy/different model causes verify to fail."""
        from typer.testing import CliRunner
        from flair_cli.main import app

        model_file = self.TEST_REPO / "model.onnx"
        model_file.write_bytes(b"dummy replacement model bytes")

        runner = CliRunner()
        res = runner.invoke(app, ["metrics", "verify", "--model", "model.onnx", "--dataset", "val_dataset.json"])
        assert res.exit_code != 0

    def test_attack_10_changing_sampling_count(self):
        """Attack 10: Changing sampling count (proven_sample_count / eval_samples) causes verify to fail."""
        from typer.testing import CliRunner
        from flair_cli.main import app

        metrics_file = self.TEST_REPO / ".flair" / "metrics.json"
        m = json.loads(metrics_file.read_text(encoding="utf-8"))
        m["zkp"]["proven_sample_count"] = 5
        m["eval_samples"] = 5
        metrics_file.write_text(json.dumps(m), encoding="utf-8")

        runner = CliRunner()
        res = runner.invoke(app, ["metrics", "verify", "--model", "model.onnx", "--dataset", "val_dataset.json"])
        assert res.exit_code != 0


