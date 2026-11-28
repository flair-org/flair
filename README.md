# Flair CLI

Flair CLI is an offline-first command-line tool for versioning trained machine learning models. It brings Git-style workflows—commits, branches, merges, rollback, and deterministic history—to model parameters and training artifacts, while preserving reproducibility and optional privacy-preserving collaboration.

Repository initialization, branching, and commit generation run **100% offline** without requiring credentials or network access. All remote synchronization is deferred to `flair push`, which negotiates missing artifacts through an atomic **Push Manifest Handshake** and reconciles local commit trees with authoritative backend SHA-256 digests.

## Installation

### Requirements

- Python >= 3.11
- **Windows (Python > 3.11)**: For Python versions greater than 3.11 on Windows, [Microsoft C++ Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) must be installed to compile native C++ extensions (such as `onnx` and `cryptography`).
  - In the installer, make sure to select the **Desktop development with C++** workload.

### Install with pip from GitHub

```powershell
python -m pip install "git+https://github.com/flair-org/flair.git"
flair --help
```

To install a specific branch or release tag:

```powershell
python -m pip install "git+https://github.com/flair-org/flair.git@main"
python -m pip install "git+https://github.com/flair-org/flair.git@v0.1.0"
```

This installs the declared dependencies and registers the `flair` executable from `flair_cli.main:app`.

For local installation (windows), clone the github repository
```powershell
python -m venv .venv
.\.venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -e .
```

For local installation (linux),
```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e .
```

## Table of Contents

- [Installation](#installation)
- [Authentication](#authentication)
   - [SSH Setup](#ssh-setup)
- [Configuration](#configuration)
- [Repository Commands](#repository-commands)
   - [Create sample model files](#create-sample-model-files)
   - [Init repository with automatic base model detection](#init-repository-with-automatic-base-model-detection)
   - [Skip base model prompt during init](#skip-base-model-prompt-during-init)
- [Clone a repository](#cloning-a-repository)
- [Remote Commands](#remote-commands)
- [Base model Commands](#base-model-commands)
   - [Upload base model manually](#upload-base-model-manually)
   - [Replace existing base model](#replace-existing-base-model-admin-command)
   - [Force upload without prompts](#force-upload-without-prompts)
   - [Check if base model exists](#check-if-base-model-exists)
   - [Delete base model](#delete-base-model-admin-command)
   - [Download base model](#download-base-model)
- [Commit Workflow](#commit-workflow)
   - [Create a new local commit](#create-a-new-local-commit)
   - [Create model params](#create-model-params)
   - [Metrics Command](#metrics-command)
   - [Zero-Knowledge Proofs (ZKP)](#zero-knowledge-proofs-zkp)
   - [Finalize commit with message](#finalize-commit-with-message)
- [Push Commits](#push-commits)
- [Pull Commits](#pull-commits)
- [Status Command](#status-command)
- [Merge Commits](#merge-commits)
- [Local Merging Architecture (Shared Folder)](#local-merging-architecture-shared-folder)
- [Log Command](#log-command)
- [Diff Command](#diff-command)
- [Revert Commits](#revert-commits)
- [Reset Commits](#reset-commits)
- [Branch Functions](#branch-functions)
   - [Create a new branch](#create-a-new-branch)
   - [List all branches](#list-all-branches)
   - [Delete a branch](#delete-a-branch)
   - [Switch branches](#switch-branches)
- [Directory structure (CLI)](#directory-structure-cli)
- [Complete Workflow Example](#complete-workflow-example)
- [Validation Rules](#validation-rules)
- [Testing & Quality Assurance](#testing--quality-assurance)

## Authentication

By default, `flair auth login` opens the shared browser-based `/signin` page. Choose either Google OAuth2 or Phantom wallet authentication there:

```bash
flair auth login
flair auth status
flair auth logout
```

`flair auth login` waits for the local callback after the selected provider completes authentication. Use `--force` to replace an existing valid session, or `--no-browser` to print the shared login URL instead of opening it automatically:

```bash
flair auth login --force
flair auth login --no-browser
flair auth login --auth-url https://auth.example.com/login
```

The shared authentication URL can also be configured with the `FLAIR_AUTH_URL` environment variable or with `flair config set --auth-url <url>`. It should point to the frontend `/signin` route.

`flair auth status` displays the logged-in principal and session expiration. `flair auth logout` clears the locally cached session token.

### Authentication callback flow

The selected browser flow uses a temporary local callback server:

1. The CLI starts a local callback URL and opens the shared `/signin` page with a `cli_redirect` query parameter.
2. The frontend displays Google OAuth2 and Phantom wallet options and completes the provider selected by the user.
3. After successful authentication, the frontend redirects the browser back to the CLI callback URL.
4. The CLI stores the returned session token and principal in `~/.flair/session.json`.
5. The browser displays a success message and can be closed.

Google OAuth2 returns `sessionToken`, `principal`, and `userId`. The Phantom wallet flow returns the wallet `token` and `wallet` address. The CLI accepts both callback formats.

### SSH Setup

Flair signs parameter commits using dedicated Ed25519 SSH keys managed by your system's `ssh-agent`.

```bash
# 1. Generate and register your signing key with Flair backend
flair auth ssh setup

# 2. Start ssh-agent and load key into memory (auto-detects CMD, PowerShell, Bash)
flair auth ssh env

# 3. Verify agent readiness before pushing
flair auth ssh status
```

For complete platform-specific instructions (Command Prompt `.cmd`, PowerShell `.ps1`, Bash `.sh`) and service troubleshooting, see the [SSH Setup Guide](docs/ssh-setup.md).

## Configuration

Flair stores user configuration in:

```text
~/.flair/config.yaml
```

On Windows, this is typically:

```text
C:\Users\<username>\.flair\config.yaml
```

Flair creates the `.flair` user directory and `config.yaml` automatically the first time a command loads the configuration. The file is initialized with the built-in defaults from the CLI, so no manual file creation is required. Existing configuration is preserved when Flair is reinstalled or upgraded.

View the active configuration and its source:

```powershell
flair config view
```

Set configuration values:

```powershell
flair config set --api-base-url https://api.example.com --auth-url https://frontend.example.com/signin --session-timeout-hours 168
```

Reset only `config.yaml` to the built-in defaults:

```powershell
flair config reset
```

The reset command preserves the authentication session and repository data. It does not delete `session.json`, SSH keys, or project `.flair` directories.

## Repository Commands

### New repository

Use `flair init` in any project directory to initialize it as a local-first Flair repository. **Initialization runs 100% offline—no authentication, credentials, or network connection are required.**

```bash
flair init --name my-model --description "My model" --use-case "Image classification" --framework PyTorch
```

If `--name` is omitted, Flair uses the current directory name as the repository name. The default framework is PyTorch.

During offline initialization, Flair:
- Generates a local client UUID v4 `repoHash` in `.flair/repo.json` with `remote: null`.
- Generates a default branch UUID v4 `branchHash` for `main` in `.flair/branches.json`.
- Points `.flair/HEAD` to `main` with genesis commit hash `_GENESIS_COMMIT_`.
- Creates `.flairignore` and local directories used for parameters, ZKPs, and commits.

#### Automatic base model detection

`flair init` checks the current directory for supported base model files. When it finds files, it records the detected base model locally. Physical upload to the backend is automatically deferred until you run `flair push`:

```bash
flair init --description "My model"
## Found 2 base model file(s):
##   1. model.pt
##   2. architecture.onnx
## Would you like to use a base model? [y/N]: y
## Select file number (1-2): 1
## ✓ Base model configured (upload deferred to 'flair push')
```

#### Skip the base model prompt

Use `--skip-base-model` to initialize the repository without checking for a base model.

```bash
flair init --skip-base-model
## ✓ Repository initialized without base model
```

Running `flair init` again in an already initialized directory reports the existing repository instead of recreating it.

### Clone an existing repository with flair clone

Use `flair clone` to download an existing Flair repository into a local directory. It saves repository metadata to `.flair/repo.json`, branch pointers to `.flair/HEAD` and `.flair/branches.json`, and downloads the latest available artifacts into the target repository:

- `base_model.<ext>`
- `params.<ext>`
- `zkml_proof.json`
- `zkml_settings.json`
- `zkml_verification_key.json`

```bash
flair clone <repo_hash>
flair clone <repo_hash> --target-dir ./repo
flair clone <repo_hash> --branch main
flair clone <repo_hash> --branch-hash main
## Cloning repository...
## ✓ Saved metadata to .flair/repo.json
## ✓ Downloaded base_model, params, zkml_proof, zkml_settings, zkml_verification_key
## Current branch: main
```

### Create a sample model scaffold with flair new

After or before repository setup, use `flair new` to generate a sample source file for PyTorch or TensorFlow.

```bash
flair new --example pytorch
## ✓ PyTorch sample model created: model_pytorch.py

flair new --example tensorflow
## ✓ TensorFlow sample model created: model_tensorflow.py

flair new --example pytorch --output my_model.py
## ✓ PyTorch sample model created: my_model.py
```

`flair new` requires `--example` and supports `pytorch` or `tensorflow`. The generated file is a source-code scaffold containing a sample architecture and example save/load functions; it does not create a Flair repository and does not generate trained model weights. You can modify or ignore the scaffold.

## Remote Commands

Remote commands connect an initialized local directory to an existing remote FlairHub repository.

> **Important (Local-First Design):** Flair will **never** automatically create a missing repository on the remote backend during `flair push`. You must first create or reference an existing repository on FlairHub and link it explicitly using `flair remote add`.

### Add a remote repository

Run this from the model directory you want to connect:

```bash
flair remote add origin <repo_hash>
```

Use `origin` as the remote name for the standard Git-like setup. This updates the local `.flair/repo.json` metadata with the remote repository hash, fetches remote branch metadata, and binds tracking references.

If you attempt to run `flair push` before linking a remote (or if the remote repository does not exist on FlairHub), `flair push` halts immediately:

```
Error: Remote repository '...' not found on remote.
Please create or link the repository on FlairHub first using:
  flair remote add origin <remote_repo_hash>
```

### List or view configured remotes

`list` and `view` are aliases:

```bash
flair remote list
flair remote view
```

The output shows the remote name, repository name, repository hash, and configured backend URL.

### Remove a remote reference

```bash
flair remote remove origin
```

Removing a remote only removes the remote name from `.flair/repo.json`; it does not delete the local repository history or the remote repository.

## Base model Commands

### Upload base model manually

```bash
flair basemodel add model.pt
## Uploading model.pt (42.0 MB)...
## ✓ Base model uploaded
```

### Replace existing base model (admin command)

```bash
flair basemodel add new_model.h5
## ⚠ Base model already exists
## Do you want to replace it? [y/N]: y
## Uploading new_model.h5 (55.3 MB)...
## ✓ Base model replaced
```

### Force upload without prompts

```bash
flair basemodel add model.keras --force
## Uploading model.keras (120.7 MB)...
## ✓ Base model uploaded (forced)
```

### Check if base model exists

```bash
flair basemodel check
## Base model exists: true
## Name: base_model.pt
## Size: 42.0 MB
```

### Delete base model (admin command)

```bash
flair basemodel delete
## ✓ Base model deleted
```

### Download base model

```bash
flair basemodel download
flair basemodel download --target-dir ./models
## Downloading base model to ./models/base_model.pt
## ✓ Download complete
```

## Commit Workflow

All commit operations (`add`, `params create`, `metrics set`, `zkp create`, `commit -m`) execute completely offline. 

### Create a new local commit
Creates a new commit directory in `.flair/.local_commits/` with a temporary client-generated UUID v4. This must be done before creating params or ZKP.

> **Identity Reconciliation Note:** While offline, local commits use temporary UUID v4 hashes (e.g. `a1b2c3d4-e5f6-...`). When pushed to FlairHub via `flair push`, the backend computes authoritative 64-character SHA-256 digests from canonical metadata, and the CLI automatically and atomically reconciles local commit folders using an ACID journal (`.flair/pending_reconciliation.json`).

Commit structure:

```json
{
   "commitHash": "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
   "architecture": "pytorch|tensorflow|onnx",
   "params": "parameters if a checkpoint commit",
   "deltaParams": "delta parameters if a delta commit",
   "zkp": "zkps",
   "metrics": {
      "epochs": null,
      "learning_rate": null,
      "accuracy": null
   },
   "message": "commit message",
   "commitType": "CHECKPOINT|DELTA",
   "architectureHash": "current hash of the model architecture",
   "previousArchitectureHash": "previous hash of the model architecture",
   "architectureChanged": "true|false",
   "createdAt": "time when flair commit is executed",
   "status": "CREATED"
}
```

```bash
flair add
## ✓ New local commit created
##   Commit hash: a1b2c3d4...
##   Location: .flair/.local_commits/a1b2c3d4.../
## 
## Next steps:
##   1. Run 'flair params create' to add model parameters
##   2. Run 'flair zkp create' to generate zero-knowledge proof
##   3. Run 'flair commit -m "Your message"' to finalize commit
##   4. Run 'flair push' to upload commit to repository
```

**Note:** You cannot create a new commit until the current one is complete (has both params and ZKP).

### Create model params
Extracts model parameters and saves them to the current commit directory.
Automatically detects framework (.pt, .pth for PyTorch, .h5, .keras for TensorFlow, or .onnx).

Metrics are no longer read from project-root files during `flair params create`.
Use `flair metrics set` to stage metrics in `.flair/metrics.json` before running `flair commit`.

Each params extraction also computes and stores an `architectureHash` for the commit.
The hash is deterministic and derived from parameter names, parameter order, and tensor shapes
(plus framework metadata when available).

**Prerequisites:** Must run `flair add` first to create a commit.

```bash
flair params create                # Auto-detect model in current directory
flair params create --model model.pt  # Specify PyTorch model
flair params create --model model.h5  # Specify TensorFlow model
## ✓ Parameters saved to commit directory
##   File: params.pt
##   Hash: 1a2b3c4d5e6f7g8h...
##   Architecture hash: ab12cd34ef56...
## 
## Next steps:
##   1. (Optional) Run 'flair zkp create' to generate zero-knowledge proof
##   2. Run 'flair commit -m "Your message"' to finalize commit and compute delta
##   3. Run 'flair push' to upload commit to repository
```

**Extraction details:**
1. PyTorch: Extracts state_dict and saves as .pt
2. TensorFlow: Extracts weights as numpy arrays and saves as .npz
3. ONNX: Extracts initializers and saves as .npz

**Storage optimization (Advanced):**
Full parameters are automatically managed by the `flair commit` command:
- **Genesis commit (CHECKPOINT)**: Full parameters retained
- **Matching architecture commits (DELTA)**: Delta computed and stored; cleanup deletes full params from older commits
- **Changed architecture commits (CHECKPOINT)**: Full parameters stored automatically (delta is skipped)
- **Latest 2 commits**: Always retain full parameters for reliable delta computation
- **Older commits**: Only store deltas (~90-95% smaller than full params)
- **Reconstruction**: If a previous commit's full params are deleted, they are automatically reconstructed from the nearest compatible CHECKPOINT by applying deltas serially

This strategy provides significant storage savings (95%+ for large commit histories) while maintaining data integrity.

**Note:** You cannot overwrite existing params. To create new params, run `flair add` to create a new commit first.

## Metrics Command

Use `flair metrics` to stage metrics in `.flair/metrics.json`.

Supported options for `flair metrics set`:
- `--epoch`
- `--accuracy`
- `--val-loss`
- `--train-loss`
- `--precision`
- `--recall`
- `--f1`
- `--learning-rate`
- `--notes`

```bash
flair metrics set --epoch 15 --accuracy 0.935 --val-loss 0.31
flair metrics import
flair metrics import --source ./mlruns --run-id <run_id>
flair metrics show
flair metrics reset
```

Behavior:
- Staging file is always `.flair/metrics.json`
- `set` merges provided fields and updates `updatedAt`
- `import` scans MLflow logs (default `./mlruns`), imports the latest run metrics, and merges into staged metrics
- `show` prints current staged metrics
- `reset` deletes staged metrics
- `flair commit` copies staged metrics into commit metadata under `metrics` and then clears `.flair/metrics.json`

## Zero-Knowledge Proofs (ZKP)

Flair uses Zero-Knowledge Machine Learning (ZKML) to generate cryptographic proofs that a committed neural network executes correctly without revealing its model parameters. Each proof is bound to the model's computational graph, allowing collaborators to verify the integrity of the model architecture and inference while preserving parameter privacy.

The Flair CLI uses the `ezkl` library to generate these Zero-Knowledge Proofs. By configuring the inputs and outputs as public and the model parameters as private, the ZKP provides cryptographic guarantees for the following:

- **Proof of Computational Integrity (Correct Execution):** It guarantees that the output was derived by correctly executing the exact computational graph of the model on a given input, without any shortcuts or tampering.
- **Proof of Architecture:** The generated verification key and settings act as a strict cryptographic fingerprint of the model's architecture. It ensures that the model structure (layers, dimensions, operations) matches exactly what the author claims.
- **Hidden Weights (Private Parameters):** The proof mathematically confirms that a valid set of parameters was used to run the model correctly, without ever revealing the actual weights to the verifier.

### What it does NOT currently prove
It is important to understand the boundaries of the current implementation. The ZKP does **not** prove:
- Provenance of training
- Correctness of SGD
- Optimizer execution
- Dataset authenticity
- Training metrics
- Federated aggregation correctness

Those guarantees would require much more sophisticated and compute-intensive zkML circuits.

### Create a ZKP for your model
Generates a zero-knowledge proof for the model in the current commit directory.
Automatically detects model framework, converts to ONNX if needed, and generates
the proof using EZKL directly in the CLI. No external server required.

**Prerequisites:** 
- Must run `flair add` to create a commit
- Must run `flair params create` to extract parameters (optional but recommended)

Supports:
- PyTorch models (.pt, .pth)
- TensorFlow models (.h5, .keras)
- ONNX models (.onnx)

```bash
flair zkp create                                    # Auto-detect model with default input dims [1, 3, 224, 224]
## Generating Zero-Knowledge Proof using EZKL...
## This may take several minutes depending on model complexity...
## 
## Step 1/10: Generating input data...
## Step 2/10: Generating calibration data...
## Step 3/10: Generating settings...
## Step 4/10: Calibrating settings...
## Step 5/10: Compiling circuit...
## Step 6/10: Getting SRS (Structured Reference String)...
## Step 7/10: Generating witness...
## Step 8/10: Setting up proving and verification keys...
## Step 9/10: Generating proof...
## Step 10/10: Verifying proof...
## ✓ All EZKL steps completed successfully!
## ✓ ZKP created successfully!
## Saved to: a1b2c3d4.../
## Proof files: proof.zlib, verification_key.zlib, settings.zlib
## 
## Next steps:
##   • Run 'flair commit -m "Your message"' to finalize this commit
##   • Run 'flair push' to push this commit to the repository

flair zkp create --input-dims "[1, 3, 256, 256]"   # Custom input dimensions
flair zkp create --model custom.pt                  # Specify model file
flair zkp create --backend pytorch                  # Specify backend (pytorch/tensorflow/numpy)
flair zkp create --model model.h5 --input-dims "[1, 224, 224, 3]"  # TensorFlow model
```

**Note:** You cannot overwrite an existing ZKP. To create a new ZKP, run `flair add` to create a new commit first.

### Verify a ZKP
Verifies a previously generated proof using EZKL directly. If a `[commit_hash]` is not provided, it defaults to the **latest local commit**. If a hash is provided, it checks your local cache, and if missing, it automatically fetches the ZKP artifacts from the active remote branch.

```bash
# Verify the latest local commit
flair zkp verify

# Verify a specific commit (downloads from remote if needed)
flair zkp verify e1f8c3a...

## Decoding proof artifacts from zlib files...
## Running EZKL verification...
## ✓ Proof verified successfully!
## Verification log saved to: .flair/.zkp/<commit_hash>/.verified
```

### Check ZKP status
Display information about created proofs and verification status. Like `verify`, it accepts an optional `[commit_hash]`.

```bash
# Check status of a specific commit
flair zkp status e1f8c3a...

## ZKP Status for my-model
## Location: .flair/.zkp/<commit_hash>
##
## Proof Information:
##   Model: model.pt
##   Framework: pytorch
##   Input Dims: [1, 3, 224, 224]
##   Created: 2026-01-22T10:30:45.123456
##
## Verification Status: ✓
##   Verified: 2026-01-22T10:35:12.789012
```

**Note:** ZKP generation requires EZKL and Python 3.9-3.11. Install with:
```bash
pip install ezkl
```

### Finalize commit with message
Finalizes the commit by setting the message, computing architecture metadata, determining commit type (`CHECKPOINT` or `DELTA`), computing parameter deltas when valid, and cleaning up old storage.

**Prerequisites:**
- Must run `flair add` to create a commit
- Must run `flair params create` to extract parameters
- Must run `flair zkp create` to generate zero-knowledge proof

**Commit Types:**
- **CHECKPOINT**: First commit in repository. Stores full parameters.
- **DELTA**: Commit with unchanged architecture. Stores only parameter differences from previous commit.
- **CHECKPOINT (Architecture Change)**: If architecture differs from previous commit, commit is automatically finalized as CHECKPOINT and stores full parameters.

**Architecture metadata stored in each commit:**
- `architectureHash`: Hash of parameter names + order + shapes (+ framework metadata when available)
- `previousArchitectureHash`: Previous commit architecture hash when known
- `architectureChanged`: Boolean flag indicating architecture transition vs previous commit

```bash
flair commit -m "Initial model commit"
## ✓ Computing delta from previous commit...
## Previous commit: Genesis (CHECKPOINT)
## 
## ✓ Commit finalized
##   Commit hash: a1b2c3d4f5g6h7i8...
##   Commit type: CHECKPOINT
##   Message: Initial model commit
##   Parameters: Full params retained (100 MB)
## 
## Next step:
##   Run 'flair push' to upload commit to repository

flair commit -m "Updated with training data v2"
## ✓ Computing delta from previous commit...
## Previous commit: a1b2c3d4f5g6h7i8...
## ✓ PyTorch delta computed (2.34 MB)
## 
## ✓ Commit finalized
##   Commit hash: b2c3d4e5f6g7h8i9...
##   Commit type: DELTA
##   Message: Updated with training data v2
##   Parameters: Delta stored (2.34 MB), full params retained for next delta
##   Storage cleanup: Removed full params from 1 old commit
## 
## Next step:
##   Run 'flair push' to upload commit to repository

flair commit -m "Added new classification head"
## ⚠ Architecture change detected: finalizing as CHECKPOINT.
##   Current architecture hash: 8a7b6c5d4e3f...
##   Previous architecture hash: 1a2b3c4d5e6f...
##
## ✓ Commit finalized
##   Commit hash: c3d4e5f6g7h8i9j0...
##   Commit type: CHECKPOINT
##   Message: Added new classification head
##   Parameters: Full params retained (architecture changed)
##
## Next step:
##   Run 'flair push' to upload commit to repository
```

**How it works:**
1. Computes or reads the current commit's `architectureHash`
2. Reads previous commit architecture metadata (`previousArchitectureHash`)
3. Chooses commit type:
   - `CHECKPOINT` for genesis
   - `DELTA` when architecture hash matches previous commit
   - `CHECKPOINT` when architecture hash differs (automatic fallback)
4. For DELTA commits only:
   - Loads current and previous parameters
   - Verifies architecture hash match before delta computation
   - Computes delta: `delta = current - previous`
   - Stores delta in `.delta_params/` folder
   - Cleans up full parameters from old DELTA commits (keeps latest 2 + all CHECKPOINT)
5. Stores `commitType`, `architectureHash`, `previousArchitectureHash`, and `architectureChanged` in commit.json
6. The push command uses this type to determine which file to upload:
   - CHECKPOINT: uploads full params from `.params/` folder
   - DELTA: uploads delta params from `.delta_params/` folder

**Parameter Reconstruction (Automatic Fallback):**
If a previous commit's full parameters were deleted during cleanup, they are automatically reconstructed:
- Traverses backward through commit history to find the nearest CHECKPOINT for the target commit
- Loads that CHECKPOINT full parameters
- Applies only DELTAs after that checkpoint to reconstruct the target commit
- Returns reconstructed parameters for delta computation
- Reconstruction is transparent and maintains data integrity

Example boundary handling:
- `C1 CHECKPOINT (architecture A)`
- `C2 DELTA (architecture A)`
- `C3 DELTA (architecture A)`
- `C4 CHECKPOINT (architecture B)`
- `C5 DELTA (architecture B)`

Reconstructing `C3` starts from `C1` and applies `C2` + `C3`.
Reconstructing `C5` starts from `C4` and applies `C5`.

**Storage Optimization Summary:**
- Genesis CHECKPOINT commit keeps full params
- Latest 2 commits always keep full params (for reliable delta computation)
- Older DELTA commits store only deltas (95% smaller)
- Total savings: 95%+ for large commit histories
- Safety: Automatic reconstruction ensures no data loss

**Note:** You cannot finalize a commit twice. To create a new commit, run `flair add` first.

## Push Commits

The `flair push` command synchronizes local commits with the remote FlairHub repository via an atomic **Push Manifest Handshake** and **Authoritative SHA-256 Commit Hash Reconciliation**.

### Push Workflow Phases

```
  Phase 0: Local Merkle Chain Validation
           (Verifies commit hash parentage locally before initiating any HTTP call)
               │
               ▼
  Phase 1: Pre-flight Remote Existence Check
           (GET /api/repo/hash/{repoHash} - halts with code 1 if 404 or unlinked)
               │
               ▼
  Phase 2: Push Manifest Handshake Negotiation
           (POST /api/repo/hash/{repoHash}/push/manifest)
           - Submits local commit list + target branch
           - Receives missingCommits checklist + upload session tokens
               │
               ▼
  Phase 3: Serial Upload of Missing Artifacts
           - Reads binary payloads into memory buffers and closes file handles immediately
           - Uploads ZKP proofs (if missing) and parameter tensors (if missing)
               │
               ▼
  Phase 4: Batch Finalization & Commit Hash Reconciliation
           (POST /api/repo/hash/{repoHash}/push/finalize)
           - Receives commitHashMap: { [uuid]: sha256 }
           - Executes journaled directory rename & updates .flair/HEAD
```

### Key Architectural Behaviors:
- **No Auto-Creation of Remote Repositories**: If the repository is not linked or does not exist on FlairHub, push halts immediately prompting you to link an existing repository with `flair remote add origin <repo_hash>`.
- **Pre-flight Integrity Check**: The local Merkle tree (parent hashes and merge parentages) is verified locally before any network calls are made. Broken chains fail fast.
- **Manifest Negotiation**: The backend identifies which commits and artifacts already exist (e.g., deduplicated weights or previously uploaded checkpoints) and returns pre-authorized upload tokens for only the missing artifacts.
- **Closed-Handle Stream Uploads**: Binary payloads are read into memory buffers and file handles are closed immediately prior to HTTP transmission, preventing Windows file-locking collisions during folder renames.
- **Atomic Two-Phase Reconciliation**: The server generates authoritative SHA-256 commit hashes from canonical JSON. The CLI writes `.flair/pending_reconciliation.json` and atomically renames local commit directories from temporary UUIDs to authoritative SHA-256 digests. If interrupted, the rename automatically completes on the next command.

```bash
flair push -u origin main
```

`flair push` negotiates the full local commit DAG with `POST /api/repo/hash/{repoHash}/push/manifest`, uploads only artifacts reported missing, and submits signed envelopes together to `POST /api/repo/hash/{repoHash}/push/finalize`. If the remote already has all commits, it reconciles the known local UUIDs and exits without uploading or finalizing.

The legacy `flair seqpush` command remains available for diagnostics and compatibility. It publishes each commit using five per-commit requests and uses the distinct route family `/api/repo/hash/{repoHash}/branch/hash/{branchHash}/commit/seqpush/create/{initiate,zkml-check,zkml-upload,params-upload,finalize}`. The original unprefixed `/commit/create/*` routes remain backend compatibility aliases. Both workflows use the same unprefixed ZKML and parameter artifact-upload routes; the manifest push uses the pre-authorized sessions returned by negotiation.

**Push Error Handling:**
- **Unlinked Remote**: Stops before upload if remote repository is missing or not configured.
- **Broken DAG**: Halts before connecting if local commit Merkle integrity is broken.
- **Fail-Fast Uploads**: Stops immediately on the first failed artifact upload, keeping already pushed commits authoritative on the server and local commits untouched.
- **Resilient Recovery**: In the event of a client crash or power loss during the commit folder rename, the write-ahead journal (`.flair/pending_reconciliation.json`) is automatically detected and recovered by any subsequent CLI command.
- Shows clear error messages for failed commits (✗ Commit X: reason)

## Pull Commits

### Pull commit statuses from remote repository
Synchronizes the local commit statuses (`PENDING`, `MERGER`, `MERGED`, `SUPERSEDED`) with the repository manager backend for a specific branch. Its usage is similar to `git pull`.

**Features:**
- **Branch Specific**: Operates on the current branch (using local `.flair/HEAD`) or a specified branch name.
- **Smart Syncing**: Automatically determines the last known synchronized state (last `MERGER` or `MERGED` commit) as a cursor, and fetches updates for commits pushed since then.
- **In-place updates**: Seamlessly updates the local `commit.json` files with the new remote statuses.

```bash
flair pull                          # Pull statuses for the current branch
flair pull main                     # Pull statuses for the 'main' branch
flair pull feature-branch -u origin # Specify upstream (optional)
```

## Status Command

Use `flair status` to quickly inspect local repository state and sync progress.

It reports:
- Current branch
- Current HEAD
- Current local commit
- Whether there is an unfinished local commit
- Whether params / ZKP / message are present for the latest local commit
- Number of unpushed commits

```bash
flair status
## Branch: main
## HEAD: a1b2c3d4...
## Current local commit: z9y8x7w6...
## Unfinished local commit: yes
## Params: ✓
## ZKP: ✗
## Message: ✗
## Unpushed commits: 2
```

Notes:
- `Unfinished local commit` is `yes` when the latest local commit is missing params, ZKP, message, or commit type.
- `Unpushed commits` counts complete local commits that are not yet part of the remote branch head.

## Merge Commits

Flair allows you to aggregate sibling commits (commits sharing the same parent and architecture) into a single merged model using Federated Averaging (FedAvg).

### Create a merge commit
Generates a local merger commit by fetching models from sibling commits, aggregating them using `flwr_serverless` nodes in a temporary `LocalFolder`, and producing a finalized merged commit.

```bash
flair merge create --min-children 2 --strategy fedavg
```

**Options:**
- `--min-children`: Minimum sibling commits required to trigger an aggregation (default: 2).
- `--strategy`: The federated strategy to use (currently supports `fedavg`).
- `--since-commit`: Optional override cursor hash to begin searching for siblings.
- `--include`: Optional list of commit hashes to specifically include in the merge. (e.g., `--include <hash1> --include <hash2>`)
- `--exclude`: Optional list of commit hashes to exclude from the merge. (e.g., `--exclude <hash3>`)

**Commit Selection Workflow (Star-Topology):**
The merge command follows a strict star-topology logic rather than a deep sequential chain:
1. **Find the Cursor:** It traverses local commits backward to find the very last merger commit (or Genesis if none exists).
2. **Slice the Timeline:** It filters the list of local commits to only include those created *after* this cursor.
3. **Group by Parent:** It groups these candidate commits by their `previousCommitHash`.
4. **Select Sibling Commits:** It specifically isolates the group of commits whose parent is exactly the cursor commit. These are "sibling commits" that all branched from the same base model. 
This ensures that the `flair merge` command averages peer contributors who trained on the same foundational weights, producing the next clear merger checkpoint.

**Behavior:**
1. Scans `.flair/commits/` for finalized sibling commits.
2. If enough mergeable commits are found, it initializes an `AsyncFederatedNode` connected to a `.flair/.temp_merge` temporary shared folder.
3. Performs a mathematical `FedAvg` using the `Aggregatable` properties (weights scaled by number of training examples) of the commits.
4. Generates a new finalized merger commit inside `.flair/.local_commits/<commit_uuid>/`.
5. Prompts the user to explicitly run `flair push` to upload the merged model (no ZKP generation is required for merger commits).

### List mergeable commits
Lists the available sibling commits after the last merger commit that are eligible for merging using the `flair merge create` command.

```bash
flair merge list --min-children 2
```

**Options:**
- `--min-children`: Minimum sibling commits required to aggregate (default: 2).
- `--since-commit`: Optional override cursor hash to begin searching for siblings.

## Local Merging Architecture (Shared Folder)

Flair utilizes a sophisticated local merging architecture to combine machine learning models from distributed contributors. Instead of performing heavy mathematical aggregations on a central server, Flair decentralizes the merging process to the user's local machine using a "Git-like" philosophy.

### How It Works

1. **Local Folder Strategy (`flwr_serverless`)**: When a user runs `flair merge create`, the CLI initializes a temporary `LocalFolder` acting as a localized shared directory.
2. **Sibling Commit Discovery**: The CLI scans the local repository for sibling commits (commits sharing the same parent and architecture).
3. **AsyncFederatedNode Integration**: The parameters of the eligible sibling commits are parsed and injected as `Aggregatable` objects into a `flwr_serverless` `AsyncFederatedNode`.
4. **Federated Averaging (FedAvg)**: The node performs a synchronous `FedAvg` operation, combining the weights mathematically based on the number of training examples in each sibling commit.
5. **Artifact Generation**: The resulting global model is extracted and saved as a merge candidate in the `.flair/.merge_candidates/` directory.

### Why Local Merging?

- **True Decentralization**: Offloads the immense compute and RAM requirements of aggregating large neural networks from the central Repository Manager to the client edges.
- **Scalability**: By bypassing the backend for the aggregation math, the system scales naturally without crashing the central API under heavy synchronous load.
- **Fault Tolerance & Safety**: Merges are sandboxed on the local machine. Network drops during a merge do not result in orphaned states on the server.
- **Standard Git Workflow**: Instead of a "magic" remote API call, users can inspect their `.merge_candidates/` locally before staging (`flair add`), finalizing (`flair commit`), and uploading the merged commit (`flair push`).

By treating merged models simply as standard commits with `commitType: "MERGER"`, the backend Repository Manager is entirely free from complex merging logic, serving exclusively as a fast, secure storage layer.

## Log Command

Use `flair log` to view readable commit history (newest first).

```bash
flair log
## d4e5f6g7... Fine-tuned hyperparameters
## c3d4e5f6... Improved accuracy to 95%
## b2c3d4e5... Updated with training data v2
## a1b2c3d4... Initial model commit
```

Optional flags:

```bash
flair log --graph
flair log --branch experimental
flair log --limit 10
```

Notes:
- `--graph` adds a simple graph-style prefix to each entry.
- `--branch` targets a specific branch head when available.
- `--limit` controls the maximum number of commits printed (default: 50).

## Diff Command

Use `flair diff` to semantically compare two model commits. This command is designed for federated learning and model reproducibility validation.

The diff is **semantic**, not raw tensors. It focuses on understanding how the model changed, not exact numerical values.

### Basic Usage

Compare two commits and get an overview:

```bash
flair diff 9f2c... b71e...
## Commit A: 9f2c...
## Commit B: b71e...
## 
## Architecture: unchanged
## Parameters changed: 12.4M / 14.1M (87.9%)
## 
## Overall Change
## ┌─────────────────────────────────────────────┐
## │ Mean parameter delta norm: 0.021            │
## │ Max parameter delta norm: 0.184             │
## │ Cosine similarity between models: 0.992     │
## └─────────────────────────────────────────────┘
## 
## Largest layer changes:
##   classifier.fc3.weight Δ norm: 0.184 (100.0% changed)
##   classifier.fc3.bias Δ norm: 0.071 (55.2% changed)
##   encoder.layer4.weight Δ norm: 0.053 (38.1% changed)
##   encoder.layer4.bias Δ norm: 0.028 (22.0% changed)
##   encoder.layer3.weight Δ norm: 0.019 (15.3% changed)
##
## Federated Merge Readiness
##   Architecture compatible: ✓
##   Parameter dimensions compatible: ✓
##   Class space compatible: ✓
##   Update magnitude: moderate
##   Recommended merge weight: 0.18
```

Notes:
- The diff output only shows metadata fields that are actually stored in both commits.
- Training-only values such as `epochs`, `learning_rate`, and `accuracy` are not shown unless they are explicitly stored in a structured `metrics` object.

### Detailed Mode

Show all changed layers, not just the top 5:

```bash
flair diff <commitA> <commitB> --detailed
```

This produces a full table with all layers:

```
Layer Statistics
┌──────────────────────────────┬───────────┬──────────┬────────────┬──────────────┐
│ Layer                        │ Shape     │ Δ Norm   │ % Changed  │ Max │Δ│      │
├──────────────────────────────┼───────────┼──────────┼────────────┼──────────────┤
│ classifier.fc3.weight        │ 10x512    │ 0.184234 │ 100.00%    │ 0.024582     │
│ classifier.fc3.bias          │ 10        │ 0.071456 │ 55.20%     │ 0.018934     │
│ encoder.layer4.weight        │ 128x256   │ 0.053189 │ 38.10%     │ 0.012745     │
│ encoder.layer4.bias          │ 128       │ 0.028456 │ 22.00%     │ 0.008921     │
│ encoder.layer3.weight        │ 128x128   │ 0.019234 │ 15.30%     │ 0.005678     │
└──────────────────────────────┴───────────┴──────────┴────────────┴──────────────┘
```

### JSON Output

For programmatic consumption, use `--json`:

```bash
flair diff <commitA> <commitB> --json
```

Returns a machine-readable JSON object with structure:

```json
{
  "commitA": "9f2c...",
  "commitB": "b71e...",
  "architectureCompatible": true,
  "overall": {
    "total_parameters": 14768384,
    "changed_parameters": 12957568,
    "percent_changed": 87.8,
    "mean_delta_norm": 0.021342,
    "max_delta_norm": 0.184234,
    "cosine_similarity": 0.9924
  },
  "layers": [
    {
      "name": "classifier.fc3.weight",
      "shape": [10, 512],
      "delta_norm": 0.184234,
      "percent_changed": 100.0,
      "max_abs_difference": 0.024582
    }
  ],
   "metadataChanges": {
      "contributor": { "old": "Rahul", "new": "Raj" },
      "notes": { "old": "baseline run", "new": "added augmentation" }
   },
   "metricChanges": {
      "validation_loss": { "old": 0.42, "new": 0.31 },
      "f1_score": { "old": 0.91, "new": 0.94 }
   },
  "mergeReadiness": {
    "architecture_compatible": true,
    "parameter_dimensions_compatible": true,
    "merge_possible": true,
    "update_magnitude": "moderate",
    "recommended_merge_weight": 0.18
  }
}
```

### What the Diff Computes

**Architecture Validation**

If the two commits have different `architectureHash` values, the diff terminates with an error:

```
Cannot diff commits with different model architectures.
  Architecture Hash A: a1b2c3d4...
  Architecture Hash B: x9y8z7w6...
```

**Overall Statistics**

- **Total parameters**: Count of all model weights and biases
- **Changed parameters**: Number of elements where values differ
- **Percent changed**: Percentage of total parameters that changed
- **Mean delta norm**: Average L2 norm of per-parameter deltas
- **Max delta norm**: Largest L2 norm across all parameters
- **Cosine similarity**: Similarity between full flattened parameter vectors (1.0 = identical, 0.0 = orthogonal)

**Per-Layer Statistics**

For each parameter/layer:
- **Δ Norm**: L2 norm of the difference tensor
- **Percent Changed**: Percentage of non-zero deltas in this layer
- **Max |Δ|**: Maximum absolute value of any single change

Results are sorted by delta norm (descending) to highlight the most-changed layers first.

**Metadata Comparison**

If commits have different non-internal metadata fields that are present in both commits, they're listed. Training-only values like `epochs`, `learning_rate`, and top-level `accuracy` are intentionally omitted unless stored under a structured `metrics` object:

```
Metadata changes:
   contributor: Alice -> Raj
   notes: baseline -> fine-tuned with augmentation
```

**Metric Comparison**

If both commits store a structured `metrics` object, matching metric keys are highlighted:

```
Metrics:
  validation_loss: 0.42 -> 0.31 ↓
   f1_score: 0.91 -> 0.94 ↑
```

If no `metrics` object exists in commit metadata, this section is omitted.
When present, metric values are sourced from each commit's stored `metrics` object (for example, values staged with `flair metrics set` and captured during `flair commit`).

**Federated Merge Readiness**

This section is designed for federated learning workflows:

- **Architecture compatible**: ✓ if `architectureHash` matches
- **Parameter dimensions compatible**: ✓ if all shapes match
- **Class space compatible**: ✓ only when every contributor trains against the same fixed label set and output layer size
- **Update magnitude**: `small` (mean delta < 0.01), `moderate` (0.01–0.1), or `large` (> 0.1)
- **Recommended merge weight**: Heuristic weight (0–1) for how much to blend this update into the global model, based on cosine similarity and magnitude

Important limitation:
- Flair cannot safely merge models trained on different classes if that changes the output layer or label space.
- If contributors train with different class sets, treat those commits as separate lineages and start a new CHECKPOINT or branch.

Example interpretation:
- A `small` update with high similarity (0.99+) → blend weight ~0.15
- A `moderate` update with good similarity (0.95+) → blend weight ~0.18
- A `large` update with lower similarity (< 0.9) → blend weight ~0.12

## Revert Commits

### Revert to the previous commit
Creates a new checkpoint commit with the exact parameters from the parent commit. This allows you to undo the latest commit without deleting history.

**Important constraints (v1):**
- Can only revert the latest commit (HEAD)
- Cannot revert arbitrary commits in history
- Reason: Earlier model updates are dependencies for later ones, so reverting a middle commit could create invalid model state

**What the revert command does:**
1. Validates that HEAD exists and is not the genesis commit
2. Reads the parent commit's full parameters (reconstructs from deltas if needed)
3. Creates a new checkpoint commit with:
   - Exact copy of parent's parameters
   - Metadata field: `reverts: <HEAD commit hash>`
   - Message: "Revert <HEAD hash>" (customizable with `-m`)
4. Updates HEAD to point to the new revert commit
5. Does NOT delete or modify any previous commits

**Result:**
Original commit chain remains intact:
```
Before:  C0 -> C1 -> C2 -> C3 (HEAD)
After:   C0 -> C1 -> C2 -> C3 -> C4 (HEAD)  # C4 reverts C3
```
Where C4 has: `params(C2) = C4.params`, `C4.previousCommitHash = C3`, `C4.reverts = C3`

```bash
# Simple revert - uses default message
flair revert
## Reverting latest commit...
## Current HEAD: a1b2c3d4...
## Parent commit: z9y8x7w6...
## 
## Step 1/4: Loading parent parameters...
## ✓ Parent parameters loaded
## Step 2/4: Creating revert commit...
## ✓ Revert commit directory created
## Step 3/4: Copying ZKP files...
## ✓ ZKP files copied from parent
## Step 4/4: Finalizing revert commit...
## ✓ Commit finalized
## 
## ═══════════════════════════════════
## ✓ Revert successful!
## ═══════════════════════════════════
## 
## Reverted latest commit a1b2c3d4...
## Created compensating checkpoint commit b2c3d4e5...
##
## Summary:
##   Reverted commit: a1b2c3d4...
##   New HEAD: b2c3d4e5...
##   Commit type: CHECKPOINT
##   Message: Revert a1b2c3d4...
##   Metadata: {'reverts': 'a1b2c3d4...'}
##
## Next steps:
##   Run 'flair push' to upload the revert commit to remote

# Revert with custom message
flair revert -m "Undo training error - bad data epoch"
## ✓ Revert successful!
## Created compensating checkpoint commit b2c3d4e5...
## Message: Undo training error - bad data epoch

# Explicit HEAD reference (same behavior as 'flair revert')
flair revert HEAD
## ✓ Revert successful!
## Created compensating checkpoint commit b2c3d4e5...
```

**Revert workflow:**
1. Reads current HEAD commit
2. Validates HEAD is not genesis (has a parent to revert to)
3. Loads or reconstructs parent's full parameters
4. Creates new commit directory with new UUID
5. Saves parent's parameters as new commit's full parameters
6. Copies ZKP files from parent (proof is same since parameters are same)
7. Creates commit.json with type=CHECKPOINT and metadata.reverts field
8. Updates `.flair/HEAD` to point to new revert commit
9. Reports success and next steps

**Error cases:**
- No commits found (need at least genesis + one more)
- HEAD is genesis commit (nothing to revert to)
- Cannot determine parent commit
- Failed to load or reconstruct parent parameters
- ZKP files unavailable from parent (warning only, user can regenerate)

**After reverting:**
- New commit is stored locally and ready to push
- If parent's ZKP files were not available, run `flair zkp create` first
- Run `flair push` to upload the revert commit to remote
- Original commits remain unchanged in history

## Reset Commits

### Reset HEAD to a previous local commit
Discards unpushed local commits and restores working model parameters to an earlier state.
**Important:** Can only reset to unpushed commits - cannot move HEAD before REMOTE_HEAD.

**When to use reset vs. revert:**
- **revert**: Creates a new commit that undoes the latest commit (permanent history)
- **reset**: Deletes unpushed local commits entirely (discards history)

**Reset constraints (v1):**
- Can only reset to recent unpushed commits via HEAD~N syntax
- Cannot reset to arbitrary commits
- Cannot reset past REMOTE_HEAD (pushing boundary)
- Must use `--hard` flag to confirm disk changes

**What the reset command does:**
1. Validates that target is >= REMOTE_HEAD (all commits after remote)
2. Collects unpushed commits to delete (from HEAD back to target, exclusive of target)
3. Loads target commit's full parameters (reconstructs from deltas if needed)
4. Permanently deletes local commit directories and all contents
5. Restores working directory model to target state
6. Updates HEAD only after all deletions and restorations succeed

**Result:**
All unpushed commits after target are gone:
```
Before:  C0 (pushed) -> C1 -> C2 -> C3 -> C4 (HEAD, unpushed)
After:   C0 (pushed) -> C1 -> C2 (HEAD)
```
Where C0 was pushed (REMOTE_HEAD), C3 and C4 are deleted.

```bash
# Simple reset - go back 1 commit (default)
flair reset
## Analyzing reset target...
## Target: HEAD~1 = a1b2c3d4...
## Current HEAD: z9y8x7w6...
## 
## Step 1/4: Collecting commits to delete...
## ✓ Will delete 1 commit(s):
##   • z9y8x7w6...
## 
## Step 2/4: Loading target commit state...
## ✓ Target commit state loaded
##   Parameters: 1024 items
## 
## Step 3/4: Deleting local commits...
##   ✓ Deleted z9y8x7w6...
## ✓ Deleted 1 local commit(s)
## 
## Step 4/4: Restoring working model...
## ✓ Working model restored
## 
## ═══════════════════════════════════
## ✓ Reset successful!
## ═══════════════════════════════════
## 
## Deleted 1 unpushed commit(s)
## HEAD moved to: a1b2c3d4...
## Working model restored to target state

# Reset multiple commits
flair reset --hard HEAD~3
## ✓ Reset successful!
## Deleted 3 unpushed commit(s)
## HEAD moved to: b2c3d4e5...

# Discard all unpushed commits (back to REMOTE_HEAD)
flair reset --to-remote
## ✓ Reset successful!
## Deleted 4 unpushed commit(s)
## HEAD moved to: c3d4e5f6... (REMOTE_HEAD)

# Explicit syntax (same as 'flair reset')
flair reset --hard HEAD~1
## ✓ Reset successful!
```

**Reset workflow:**
1. Reads current HEAD and REMOTE_HEAD tracking
2. Parses target (HEAD~N or --to-remote)
3. Validates target >= REMOTE_HEAD (prevents losing pushed commits)
4. Counts available commits and validates target is reachable
5. Collects all commits between HEAD and target (going backward)
6. Loads target commit state before deletion
7. Deletes each local commit directory and all contents
8. Restores working model parameters to target state
9. Updates `.flair/HEAD` to point to target
10. Reports deleted commits and new HEAD position

**Error cases:**
- No local commits to reset
- Invalid syntax (not HEAD~N or --to-remote)
- Target is behind REMOTE_HEAD (cannot reset pushed commits)
- Not enough commits available (HEAD~5 on 3 commits)
- Failed to load or reconstruct target parameters
- Failed to restore working model (warning only)

**After resetting:**
- Deleted commits are permanently gone (no recovery)
- Working model is restored to target state
- Can create new commits from target
- REMOTE_HEAD remains unchanged
- Deleted commits can be recreated if needed

## Branch Functions

All local branch operations (`create`, `list`, `delete`, and local `checkout`) run completely offline and track state within `.flair/branches.json` without contacting the backend. New branches are automatically registered on FlairHub during `flair push`.

### Create a new branch
Generates a local UUID v4 `branchHash` and registers the branch in `.flair/branches.json`:
```bash
flair branch new-feature  # Create new branch from current HEAD
## ✓ Branch 'new-feature' created from current
```

### List all branches
Lists branches tracked in local `.flair/branches.json`:
```bash
flair branch              # List all branches
## Branches:
## * main
##   new-feature
```

### Delete a branch
Deletes a branch locally from `.flair/branches.json`:
```bash
flair branch -d old-branch  # Delete a branch
## ✓ Branch 'old-branch' deleted
```

### Switch branches
Switches active branch. Checks local `.flair/branches.json` first; only queries remote if the branch is not cached locally:
```bash
flair checkout main              # Uses local cache if available, downloads otherwise
flair checkout feature --no-cache # Force fresh download from API
## Caching artifacts for 'main'...
## ✓ Restored artifacts from cache
## ✓ Switched to branch 'feature'
```

## Directory structure (CLI)

Here is the complete overview of the `.flair` directory structure:

```bash
.flair/
	repo.json                # Local repository metadata (remote: null or bound remote hash)
	HEAD                     # Current branch pointer (name + branchHash + latest commit)
	branches.json            # Local branch registry (offline and tracking)
	pending_reconciliation.json # Temporary write-ahead journal during push commit renaming
	class_space/             # Class space contracts defined per branch
		<branch_name>/         # Branch specific folder
			class_space.yaml     # The class space contract
	.local_commits/          # Local commits directory
		<uuid_or_sha256>/      # Client UUID offline; renamed to SHA-256 after push
			commit.json          # Commit metadata (params, zkp, commitType, architectureHash, status)
			params.pt|npz        # Extracted model weights (framework-dependent)
			.delta_params/       # Delta parameters directory
				delta.pt|npz       # Parameter differences from previous commit
			proof.zlib           # Compressed ZK proof
			verification_key.zlib  # Compressed VK
			settings.zlib        # Compressed settings
	.params/                 # Active parameter files for the current branch
	.zkp/                    # Active ZKML proof and settings files for the current branch
	.cache/                  # Per-branch cached artifacts (managed by checkout)
		<branch_name>/         # Cached params/zkp files for that branch
```

The `.flair` directory manages local state, distinguishing between the active branch, cached branches, and the commit DAG.

### 1. Current Branch Files
The active branch you are currently working on has its files directly exposed in the `.flair` directory for immediate use:

```bash
.flair/
	.local_commits/          # Local commits directory for the current state
		<uuid_or_sha256>/      # Directories (client UUID initially; server SHA-256 after push)
			commit.json          # Commit metadata (params, zkp, commitType, architectureHash, status)
			params.pt|npz        # Extracted model weights (framework-dependent)
			.delta_params/       # Delta parameters directory
				delta.pt|npz       # Parameter differences from previous commit
			proof.zlib           # Compressed ZK proof
			verification_key.zlib  # Compressed VK
			settings.zlib        # Compressed settings
	.params/                 # Active parameter files for the current branch
	.zkp/                    # Active ZKML proof and settings files for the current branch
	HEAD                     # Current branch pointer (name + branchHash)
```

### 2. Other Branches (Cached Files)
When you switch to a different branch using `flair checkout`, the CLI intelligently swaps out the current branch's artifacts and restores the target branch's files:

```bash
.flair/
	.cache/                  # Per-branch cached artifacts (managed by checkout)
		<branch_name>/         # Backed up .params and .zkp files for that branch
```
*(Note: To save space, the CLI is designed to only keep the artifacts of the 4 most recently used branches in this `.cache` directory).*

### 3. Global Repository State

```bash
.flair/
	repo.json                # Local repository metadata (contains remote link if configured)
	branches.json            # Local branch list for the repo (UUID v4 branch hashes)
```

### Repo Settings

`config.yaml` is the settings file in the project root which contains repository settings like `commitRetentionLimit`.

### `HEAD` File Structure

The `HEAD` file contains the following metadata about the repository state:
- `currentBranch`: The name of the current branch.
- `branchHash`: The hash of the current branch (UUID v4).
- `description`: The branch description.
- `latestCommitHash`: The hash of the latest commit.
- `previousCommit`: The hash of the previous commit.

**Example `HEAD` file:**
```json
{
   "currentBranch": "main",
   "branchHash": "c2b64d0a-9d29-4fb5-97e3-0d3f2a1b9e84",
   "description": "Default branch",
   "latestCommitHash": "4a7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d",
   "previousCommit": "4a7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d"
}
```

### `commit.json` Structure

The `commit.json` metadata file includes architecture-aware fields:
- `commitHash`: Temporary UUID v4 offline; overwritten with authoritative SHA-256 upon `flair push`
- `commitType`: `"CHECKPOINT"` or `"DELTA"`
- `architectureHash`: `...`
- `previousArchitectureHash`: `...` or `null`
- `architectureChanged`: `true` or `false`

## Complete Workflow Example

Here's a complete workflow from initialization through collaborative training,
local merge creation, and pushing the merged result:

```bash
# 1. Initialize repository 100% offline (no login or internet required)
flair init --name my-model --description "My ML model repo" --framework PyTorch

# 2. Create local commits, extract parameters, stage metrics, and generate ZKP (all offline)
flair add
flair params create --model model.pt --classes cat,dog,bird
flair metrics set --epoch 1 --accuracy 0.91
flair zkp create --model model.pt --input-dims "[1, 3, 224, 224]"
flair commit -m "Initial model checkpoint"

# 3. When ready to publish or share on FlairHub:
# Authenticate and link an existing FlairHub remote repository
flair auth login
flair auth ssh setup
flair auth ssh env
flair remote add origin <remote_repo_hash>

# 4. Push to remote:
# Executes local chain validation -> remote check -> manifest handshake -> serial upload -> SHA-256 DAG reconciliation
flair push -u origin main

# 5. Repeat add/params/metrics/zkp/commit for compatible sibling updates.
# Sibling commits must share the same parent and compatible class space.
flair add
flair params create --model contributor_a.pt --classes cat,dog,bird
flair zkp create --model contributor_a.pt
flair commit -m "Contributor A update"

flair add
flair params create --model contributor_b.pt --classes cat,dog,bird
flair zkp create --model contributor_b.pt
flair commit -m "Contributor B update"

# 6. Inspect mergeable commits and create a local FedAvg candidate
flair merge list --min-children 2
flair merge create --min-children 2 --strategy fedavg
# Candidate output is stored under .flair/.merge_candidates/.

# 7. Formalize the merge candidate as a new checkpoint commit
flair add
flair params create --model .flair/.merge_candidates/<candidate_hash>/params.pt --classes cat,dog,bird
flair zkp create --model .flair/.merge_candidates/<candidate_hash>/params.pt
flair commit -m "FedAvg merge of contributor updates"
flair push main

# 8. Inspect or compare the resulting history
flair log --graph
flair diff <commitA> <commitB>

# 9. Revert a pushed commit, or reset an unpushed local commit
flair revert -m "Undo accidental change"
flair push main
flair reset --hard HEAD~1

# 10. Continue independent work on another branch
flair branch experimental
flair checkout experimental
flair add
flair params create --model experimental.pt --classes cat,dog,bird
flair zkp create --model experimental.pt
flair commit -m "Experimental architecture"
flair push
```

## Validation Rules

The CLI enforces a strict sequential workflow to prevent incomplete commits:

1. **Creating a new commit (`flair add`):**
   - Blocked if the current commit is incomplete (missing params or ZKP)
   - Error: "Cannot create a new commit yet. Complete the current commit first."

2. **Creating params (`flair params create`):**
   - Blocked if params already exist in the current commit
   - Error: "This commit already has parameters. Run 'flair add' to create a new commit."

3. **Creating ZKP (`flair zkp create`):**
   - Blocked if ZKP already exists in the current commit
   - Error: "This commit already has a zero-knowledge proof. Run 'flair add' to create a new commit."

4. **Pushing (`flair push`):**
   - Requires both params and ZKP to be complete
   - Error: "Commit incomplete. Run 'flair params create' and/or 'flair zkp create'."

5. **Architecture-aware delta safety (`flair commit`):**
   - Delta computation runs only when `architectureHash` matches `previousArchitectureHash`
   - On mismatch, the CLI emits a warning and automatically finalizes as `CHECKPOINT`
   - Merge-style delta application across differing architectures is rejected

This ensures every commit is complete and prevents accidental overwrites.

## Testing & Quality Assurance

Flair CLI includes a complete automated test suite covering all atomic commands and multi-actor federated learning workflows without external server or network dependencies.

```bash
# Run the entire test suite
python -m pytest -v

# Run atomic command contract tests
python -m pytest -m command -v

# Run multi-actor end-to-end workflow scenarios
python -m pytest -m workflow -v
```

For complete documentation on the test harness architecture, mock backend, fixtures, and how to write new tests, see [tests/README.md](tests/README.md).

