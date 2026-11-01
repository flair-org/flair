# Flair CLI Test Suite Documentation

Welcome to the **Flair CLI** testing suite. This directory contains the complete automated test harness, regression test suite, and multi-actor end-to-end workflow scenarios for the Flair federated learning command-line interface.

---

## 1. Architecture & Design Principles

The Flair CLI testing suite is designed around three architectural pillars:

1. **In-Memory Hermetic Test Harness**: Tests run completely offline without dependencies on a live backend server, IPFS node, or blockchain network. All remote REST interactions are intercepted and handled by an in-memory `MockFederatedBackend`.
2. **Two-Tier Separation**:
   - **Tier 1: Atomic Command Contracts (`tests/commands/`)**: Every CLI subcommand is tested individually for input validation, file manipulation, flags, and exit code compliance.
   - **Tier 2: Multi-Actor Workflows (`tests/workflows/`)**: Real-world federated workflows across multiple independent actors (e.g. Admin and Contributors) operating in isolated file workspaces and distinct authentication environments.
3. **Strict 4-Code Standardized Exit Code Contract**:
   - `0`: **Success** — The operation completed and local/remote state was updated correctly.
   - `1`: **State / Validation Error** — Not in a Flair repository, invalid arguments, broken commit chain, or base model immutability violation.
   - `2`: **Auth / Permission Error** — Unauthenticated user, expired JWT, or non-admin attempting privileged actions (e.g., base model upload or merge push).
   - `3`: **Network / HTTP Error** — Remote server unreachable, socket timeouts, or upstream 5xx errors.

For deeper architectural decisions and context, see [docs/adr/0002-cli-test-harness-architecture.md](file:///e:/RIO%20project/Flair/official/flair-org/flair/docs/adr/0002-cli-test-harness-architecture.md).

---

## 2. Directory Structure

```
tests/
├── README.md                                  # This documentation
├── conftest.py                                # Global test harness, fixtures, loop stubs & mock backend
├── commands/                                  # Tier 1: Atomic Command Contracts
│   ├── test_auth_cmd.py                       # flair auth (login, logout, status, ssh)
│   ├── test_basemodel_cmd.py                  # flair basemodel (add, check, download, delete, RBAC)
│   ├── test_branch_cmd.py                     # flair branch & flair checkout
│   ├── test_config_cmd.py                     # flair config (view, set, reset)
│   ├── test_history_cmd.py                    # flair status, flair log, flair diff
│   ├── test_metrics_cmd.py                    # flair metrics (set, show, verify, reset)
│   ├── test_recovery_cmd.py                   # flair reset (HEAD~N, --to-remote), flair revert
│   └── test_remote_cmd.py                     # flair remote (add, view, list, remove)
├── workflows/                                 # Tier 2: Multi-Actor E2E Scenarios
│   ├── test_workflow_linear_fedavg.py         # Scenario A: 9-step FedAvg lifecycle
│   ├── test_workflow_branching_collab.py      # Scenario B: Branch branching, caching & switching
│   └── test_workflow_recovery_rollback.py     # Scenario C: Faulty commit rollback & diff inspection
├── test_architecture.py                       # Model architecture tracking & delta guard rules
├── test_base_model_immutability.py            # Base model immutability & RBAC enforcement
├── test_cli_exit_codes.py                     # Rigorous exit code compliance tests (0, 1, 2, 3)
├── test_commit_pipeline.py                    # 5-step commit creation pipeline (add->params->zkp->commit->push)
├── test_commit_signing.py                     # SSH agent signature & canonical payload tests
├── test_diff.py                               # Semantic weight diffing & class-space compatibility
├── test_metrics_zkp.py                        # EZKL metrics validation, Poseidon hashing, schema v1-v3
└── test_multi_actor_workflow.py               # Standalone multi-actor federated lifecycle
```

---

## 3. Command Coverage Matrix

All commands documented in the project root [README.md](file:///e:/RIO%20project/Flair/official/flair-org/flair/README.md) are tested according to the matrix below:

| Command Group | Subcommand / Syntax | Test File | Covered Scenarios |
| :--- | :--- | :--- | :--- |
| **Auth** | `flair auth status` | [test_auth_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_auth_cmd.py) | View current session wallet & status |
| | `flair auth logout` | [test_auth_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_auth_cmd.py) | Session deletion & cleanup |
| | `flair auth login` | [test_auth_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_auth_cmd.py) | Browser callback server & token handling |
| | `flair auth ssh env` | [test_auth_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_auth_cmd.py) | Inspect SSH agent status & keys |
| | `flair auth ssh status` | [test_auth_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_auth_cmd.py) | Verify SSH key registration & fingerprint |
| **Base Model** | `flair basemodel add` | [test_basemodel_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_basemodel_cmd.py) | Upload base model, format validation, RBAC |
| | `flair basemodel check` | [test_basemodel_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_basemodel_cmd.py) | Query model existence & download URL |
| | `flair basemodel download` | [test_basemodel_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_basemodel_cmd.py) | Target path resolution & stream download |
| | `flair basemodel delete` | [test_basemodel_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_basemodel_cmd.py) | Admin deletion with confirmation bypass (`-y`) |
| **Branch** | `flair branch` | [test_branch_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_branch_cmd.py) | Branch listing, HEAD indicator |
| | `flair branch <name>` | [test_branch_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_branch_cmd.py) | Branch creation from current HEAD |
| | `flair branch -d <name>` | [test_branch_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_branch_cmd.py) | Safe branch deletion & local cache sync |
| | `flair checkout <name>` | [test_branch_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_branch_cmd.py) | Switching branches, stashing active params |
| **Config** | `flair config view` | [test_config_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_config_cmd.py) | View CLI parameters & config directory |
| | `flair config set` | [test_config_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_config_cmd.py) | Update backend URL, timeout, limits |
| | `flair config reset` | [test_config_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_config_cmd.py) | Restore factory defaults |
| **History & State** | `flair status` | [test_history_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_history_cmd.py) | HEAD, uncommitted state, unpushed commits |
| | `flair log` | [test_history_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_history_cmd.py) | Merkle commit DAG traversal, authors, timestamps |
| | `flair diff` | [test_history_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_history_cmd.py), [test_diff.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/test_diff.py) | Layer-by-layer weight stats, class-space diffs |
| **Metrics** | `flair metrics set` | [test_metrics_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_metrics_cmd.py) | Stage evaluation accuracy, loss, and epoch |
| | `flair metrics show` | [test_metrics_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_metrics_cmd.py) | Pretty-print staged metrics |
| | `flair metrics verify` | [test_metrics_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_metrics_cmd.py) | ZKP mathematical verification against weights |
| | `flair metrics reset` | [test_metrics_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_metrics_cmd.py) | Clear `.flair/metrics.json` |
| **Recovery** | `flair reset HEAD~N` | [test_recovery_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_recovery_cmd.py) | Rewind HEAD back N commits, discard local |
| | `flair reset --to-remote` | [test_recovery_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_recovery_cmd.py) | Discard all unpushed commits to REMOTE_HEAD |
| | `flair revert` | [test_recovery_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_recovery_cmd.py) | Create compensating checkpoint commit |
| **Remote** | `flair remote add` | [test_remote_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_remote_cmd.py) | Connect working folder to remote repo |
| | `flair remote view`/`list` | [test_remote_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_remote_cmd.py) | Display table of configured remotes |
| | `flair remote remove` | [test_remote_cmd.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/commands/test_remote_cmd.py) | Disconnect origin remote |
| **Commit Pipeline** | `flair add` | [test_commit_pipeline.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/test_commit_pipeline.py) | Step 1: Detect architecture & create commit dir |
| | `flair params create` | [test_commit_pipeline.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/test_commit_pipeline.py) | Step 2: Extract & stage delta parameters |
| | `flair zkp create` | [test_commit_pipeline.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/test_commit_pipeline.py) | Step 3: Fast deterministic ZK-SNARK generation |
| | `flair commit` | [test_commit_pipeline.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/test_commit_pipeline.py) | Step 4: Package commit metadata and hashes |
| | `flair push` | [test_commit_pipeline.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/test_commit_pipeline.py) | Step 5: SSH sign payload & upload to backend |
| **Federated Merge**| `flair merge create` | [test_workflow_linear_fedavg.py](file:///e:/RIO%20project/Flair/official/flair-org/flair/tests/workflows/test_workflow_linear_fedavg.py) | FedAvg aggregation of sibling commits |

---

## 4. Key Fixtures in `tests/conftest.py`

### `multi_actor_env`
Creates an isolated testing sandbox with two distinct actors:
- **`actor_a` (Admin)**: Has its own workspace directory, isolated `FLAIR_CONFIG_DIR`, and wallet address `ActorAAdminWalletAddress1111111111111111`.
- **`actor_b` (Contributor)**: Has its own workspace directory, isolated `FLAIR_CONFIG_DIR`, and wallet address `ActorBContributorWalletAddress22222222222222`.

Switching actors in a test is as simple as:
```python
os.environ["FLAIR_CONFIG_DIR"] = str(actor_b["config"])
os.chdir(actor_b["cwd"])
```

### `MockFederatedBackend`
An in-memory stateful mock of the Flair Repository Manager server:
- Stores registered repositories, branches, commits, base models, and user keys.
- Implements `handle_get`, `handle_post`, and `handle_delete` simulating backend API endpoints (`/repo/hash/...`, `/commit/create/...`, `/branch/...`, etc.).
- Automatically mocks file downloads without network calls.

### `FakeLoop` (Windows Asyncio Sandbox Compatibility)
In sandboxed or restricted Windows environments without network interfaces, default Python `asyncio.new_event_loop()` instantiates a `ProactorEventLoop` that attempts to open a local IPC socket pair (`socket.socketpair()`) and hangs indefinitely.
`conftest.py` implements a synchronous subclass of `asyncio.AbstractEventLoop` (`FakeLoop`) and injects it into `mock_zkp`, allowing ZKP proof generation to execute synchronously in milliseconds.

### `mock_ssh_agent`
Simulates an active SSH agent:
- Pre-loads a deterministic key fingerprint (`SHA256:mockfingerprint...`).
- Stubs `load_ssh_agent_identities` and `sign_canonical_payload` so commits are cryptographically signed during `flair push`.

---

## 5. How to Run the Tests

### Quick Start (PowerShell / Windows)

```powershell
# Activate your repository virtual environment
& .\.venv\Scripts\Activate.ps1

# Run all tests
python -m pytest -v
```

### Running by Marker

The test suite uses custom registered pytest markers:

```powershell
# 1. Run all Tier 1 Atomic Command Tests (~3s)
python -m pytest -m command -v

# 2. Run all Tier 2 End-to-End Workflow Scenarios (~8s)
python -m pytest -m workflow -v

# 3. Run Cryptographic & ZKP Tests
python -m pytest -m crypto -v

# 4. Run Integration Workflows
python -m pytest -m integration -v
```

### Running Specific Files or Tests

```powershell
# Run a specific command test file
python -m pytest tests/commands/test_basemodel_cmd.py -v

# Run a specific workflow scenario
python -m pytest tests/workflows/test_workflow_linear_fedavg.py -v

# Run a single test method
python -m pytest tests/commands/test_branch_cmd.py::test_checkout_switch_branch -v

# Filter tests by keyword pattern
python -m pytest -k "immutability" -v
```

### Debugging Options

```powershell
# Print standard output directly to console (don't capture stdout)
python -m pytest -s tests/commands/test_history_cmd.py

# Drop into the Python debugger on failure
python -m pytest --pdb tests/commands/test_recovery_cmd.py

# Short traceback formatting
python -m pytest --tb=short -m command
```

---

## 6. Guide: Writing New Tests

### Adding a Tier 1 Command Test
When adding a new command test in `tests/commands/`:

1. Apply the `@pytest.mark.command` marker at module level:
   ```python
   import pytest
   pytestmark = pytest.mark.command
   ```
2. Inject `multi_actor_env`, `mock_backend`, and `cli_runner`:
   ```python
   def test_my_new_command(multi_actor_env, mock_backend, cli_runner):
       actor = multi_actor_env["actor_a"]
       os.environ["FLAIR_CONFIG_DIR"] = str(actor["config"])
       os.chdir(actor["cwd"])

       # Execute command via Typer CliRunner
       result = cli_runner.invoke(app, ["my-command", "--option"])
       assert result.exit_code == 0
       assert "expected text" in result.output.lower()
   ```
3. Assert the expected standardized exit code (`0`, `1`, `2`, or `3`).

### Adding a Tier 2 Multi-Actor Workflow Test
When testing collaborative multi-actor behaviors in `tests/workflows/`:

1. Apply the `@pytest.mark.workflow` marker:
   ```python
   pytestmark = pytest.mark.workflow
   ```
2. Request `mock_ssh_agent`, `mock_zkp`, and `dummy_pytorch_model` fixtures alongside `multi_actor_env`.
3. Switch actors by updating `FLAIR_CONFIG_DIR` and changing the current working directory:
   ```python
   # Actor A steps:
   os.environ["FLAIR_CONFIG_DIR"] = str(actor_a["config"])
   os.chdir(actor_a["cwd"])
   ...

   # Actor B steps:
   os.environ["FLAIR_CONFIG_DIR"] = str(actor_b["config"])
   os.chdir(actor_b["cwd"])
   ...
   ```

---

## 7. Common Pitfalls & Solutions

- **`PermissionError: [WinError 5] Access is denied: .../.flair/config.yaml`**:
  *Cause*: `flair_cli.api.utils` calls `load_config()` upon module import. If `FLAIR_CONFIG_DIR` is not set prior to the import, it attempts to access the Windows user root directory.
  *Fix*: Always set `os.environ["FLAIR_CONFIG_DIR"]` before invoking CLI commands, or rely on `tests/conftest.py` which initializes a fallback directory at startup.
- **Rich Table Truncation**:
  *Cause*: Rich auto-detects console width and truncates long strings (e.g. `mock-repo-hash-abc...` or `test-federated-re...`) with ellipses (`…`).
  *Fix*: When asserting command output from Rich tables, assert against stable substrings (e.g. `assert "origin" in res.output`) or substrings shorter than the column width limit.
- **Async Socketpair Hang**:
  *Cause*: Calling `asyncio.new_event_loop()` on Windows inside a restricted sandbox defaults to `ProactorEventLoop`, which blocks on loopback creation.
  *Fix*: Use the `mock_zkp` fixture, which patches `asyncio.new_event_loop` to return `FakeLoop`.

