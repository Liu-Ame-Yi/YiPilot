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

## Offline code-runtime rehearsal

M3 exposes Yipilot's own built-in Runtime through a deterministic, offline
workflow. It does not invoke Codex CLI, Claude Code, Pi, or another external
agent. First register a local repository as above, then run the fixed fake
diagnostic plan:

```powershell
uv run yipilot code generate --project C:\work\demo --taskid 1001 --alias sample --instruction "check the local workspace" --planner fake --output json
uv run yipilot run watch --project C:\work\demo --run-id <run-id-from-json> --after-sequence 0 --output jsonl
uv run yipilot code apply --project C:\work\demo --taskid 1001 --run-id <run-id-from-json> --prepare-only --output json
```

`fake` never starts a subprocess or writes project source files. It uses one
fixed read-only tool request, persists an isolated worktree, Run events,
ToolRun/Artifact evidence, a simulation-only approval record, and a passed
CodeHarness report. The instruction is represented by a hash in durable
records; it is not copied into events or the prepare-only handoff.

`replay` is equally offline but only accepts the exact built-in recording:

```powershell
uv run yipilot code generate --project C:\work\demo --taskid 1001 --alias sample --instruction "yipilot replay diagnostic" --planner replay --output json
```

Any other replay instruction produces a failed Run with
`PLANNER_REPLAY_NOT_FOUND`; no tool is executed. `code apply` has no mode other
than `--prepare-only`. It revalidates the saved CodeHarness report, approval,
ToolRuns, Artifacts, workspace and Git evidence, then writes the run-scoped
handoff at `.yipilot/tasks/<task-id>/runs/<run-id>/artifacts/prepare-only-handoff.json`.
It does not commit, push, merge, reset, alter a remote, or apply the diff.

The handoff contains identifiers, workspace metadata, Git and evidence refs,
ToolRun/Artifact/report refs, and fixed suggested manual checks. A
simulation-only fake approval is evidence for an offline rehearsal, never a
human approval for a real write. Future `host_trusted` execution has the full
permissions of the launching user and must require an explicit, fresh human
approval; it is not enabled by these commands.

## Local state and scope boundary

`.yipilot` is local runtime state. It may contain task records, repository
metadata, Run events, workspace manifests and evidence. Do not commit it or
publish it: it may reveal local project structure and development evidence.

M0–M2 do not call a model provider, Codex CLI or a network LLM. They do not run
Git push or Git merge. The only Git effects are local, argv-only cache sync and
task-owned worktree preparation.

LLM/provider integration, FastAPI, HTTP SSE and any remote deployment are
future work and are intentionally outside this milestone.
