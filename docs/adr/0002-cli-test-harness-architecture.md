# 0002. End-to-End CLI Test Harness Architecture

## Context
Flair CLI provides Git-style model weight version control and federated learning workflows across multiple actors (`Admin`, `Contributor`). Testing commands from `flair auth` to `flair diff` and complex multi-step pipelines requires simulating network interactions, cryptographic Zero-Knowledge Proofs (EZKL), SSH signing, and cross-actor timeline state. Testing against live backend infrastructure creates network flakiness, requires cloud credentials, and prevents offline CI execution. In addition, Windows environments encounter asyncio `ProactorEventLoop` socketpair hangs in sandboxed execution, and import-time config file access attempts to touch `~/.flair/config.yaml`.

## Decision
1. **In-Memory Mock Backend Harness (`MockFederatedBackend`)**: Provide an in-memory backend fixture in `tests/conftest.py` simulating all REST endpoints, 5-step commit sessions (`initiate`, `zkml-check`, `zkml-upload`, `params-upload`, `finalize`), branch registries, and IPFS CIDs without making network calls.
2. **Two-Tier Test Suite**:
   - **Tier 1 (`tests/commands/`)**: Modular, atomic tests validating argument parsing, filesystem mutations in `.flair/`, and standardized exit codes (`0` Success, `1` State/Validation, `2` Auth/Permission, `3` Network/I/O) for every command in `README.md`.
   - **Tier 2 (`tests/workflows/`)**: Multi-actor federated workflows simulating end-to-end timelines (linear federated training, branching collaboration, and disaster recovery/rollback).
3. **Computation & Loop Mocking**:
   - Mock heavy EZKL circuit compilation using deterministic compressed `.zlib` stubs and mock CIDs.
   - Provide a sandboxed `FakeLoop` inheriting from `asyncio.AbstractEventLoop` to avoid Windows `socket.socketpair` loopback hangs in restricted environments.
4. **Environment Isolation**:
   - Enforce per-actor directory isolation using `FLAIR_CONFIG_DIR` pointing to `pytest`'s `tmp_path`.
   - Initialize `FLAIR_CONFIG_DIR` in `pytest_sessionstart` to eliminate import-time permission errors on user home directories.

