# Flair CLI Domain Glossary

Core domain language for the Flair CLI model-weight version control and federated learning system.

## Roles & Identity

**Admin**:
A repository maintainer with authority to register the base model, configure repository governance, and publish federated merge commits.
_Avoid_: Superuser, root, owner, manager

**Contributor**:
A participant who clones a repository, pulls model weights, trains locally, and submits validated parameter updates for merge.
_Avoid_: Worker, client, agent, node

## Model Lifecycle & Storage

**Base Model**:
The immutable founding model weights registered at repository initialization, serving as the root ancestor for all parameter deltas.
_Avoid_: Pretrained model, seed weights, root model

**Checkpoint Commit**:
A commit storing a complete snapshot of all model parameters, serving as a boundary for reconstruction.
_Avoid_: Full commit, snapshot commit, baseline

**Delta Commit**:
A commit recording only the mathematical difference between updated model weights and their parent commit.
_Avoid_: Diff commit, incremental commit, gradient update

**5-Step Commit Lifecycle**:
The atomic sequence (`add` → `params create` → `zkp create` → `commit` → `push`) required to stage, prove, sign, and publish model weights.
_Avoid_: Commit workflow, staging pipeline

**Federated Merge**:
The process where an Admin combines one or more Contributor parameter updates and stages an authoritative checkpoint commit on a branch.
_Avoid_: Weight aggregation, Git merge, sync

## Verification & Proofs

**Zero-Knowledge Proof (ZKP)**:
A succinct cryptographic proof generated via EZKL demonstrating verifiable execution of model inference or metric computation without exposing private evaluation datasets.
_Avoid_: ZKML proof, audit proof, validity token

