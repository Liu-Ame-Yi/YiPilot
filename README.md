# Yipilot

Yipilot is a local, Windows-first Python CLI agent harness. M0–M2 establish a
deterministic local control plane for durable tasks, runs and events, plus
isolated Git workspaces and evidence bundles. It is designed for auditable
development workflows; it is not a hosted coding agent or a model client.

## Prerequisites

- Windows with Python 3.11 or later.
- Git CLI available on `PATH`; local repositories used by M2 must be valid Git
  repositories.
- [uv](https://docs.astral.sh/uv/) to create the project environment and install
  locked dependencies.

Install uv with its official Windows installer, then initialize dependencies in
the repository root:

```powershell
uv sync --group dev --frozen
```

Use the resulting environment with `uv run` (or `.venv\Scripts\python.exe`).

## Common local commands

```powershell
uv run yipilot init --project C:\work\demo
uv run yipilot task init --project C:\work\demo --taskid 1001
uv run yipilot task repo add --project C:\work\demo --taskid 1001 --url C:\src\sample --alias sample --sync
uv run yipilot task repo sync --project C:\work\demo --taskid 1001 --alias sample
uv run yipilot task workspace prepare --project C:\work\demo --taskid 1001 --alias sample --run-id prepare-1
uv run yipilot run watch --project C:\work\demo --run-id <run-id> --after-sequence 0 --output jsonl
```

`run watch` replays durable events after its cursor and then polls every 100 ms.
It stops at a terminal Run event. `--output jsonl` emits exactly one event JSON
object per stdout line; diagnostics use stderr. `--follow-timeout-seconds` must
be positive and returns exit code 4 if a nonterminal run remains unfinished.

## Local state and scope boundary

`.yipilot` is local runtime state. It may contain task records, repository
metadata, Run events, workspace manifests and evidence. Do not commit it or
publish it: it may reveal local project structure and development evidence.

M0–M2 do not call a model provider, Codex CLI or a network LLM. They do not run
Git push or Git merge. The only Git effects are local, argv-only cache sync and
task-owned worktree preparation.

LLM/provider integration, FastAPI, HTTP SSE and any remote deployment are
future work and are intentionally outside this milestone.
