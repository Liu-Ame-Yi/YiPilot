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
uv run yipilot code apply --project C:\work\demo --taskid 1001 --run-id <run-id-from-json> --prepare-only --request-id handoff-1001 --output json
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
handoff at `.yipilot/tasks/<task-id>/runs/<apply-run-id>/artifacts/prepare-only-handoff.json`.
It does not commit, push, merge, reset, alter a remote, or apply the diff.

The handoff contains identifiers, workspace metadata, Git and evidence refs,
ToolRun/Artifact/report refs, and fixed suggested manual checks. A
simulation-only fake approval is evidence for an offline rehearsal, never a
human approval for a real write. Future `host_trusted` execution has the full
permissions of the launching user and must require an explicit, fresh human
approval; it is not enabled by these commands.

`--request-id` makes `code generate` and `code apply --prepare-only` idempotent:
repeat the same request ID to retrieve the same durable Run and handoff; choose
a new ID only to make a new attempt.

## Local state and scope boundary

`.yipilot` is local runtime state. It may contain task records, repository
metadata, Run events, workspace manifests and evidence. Do not commit it or
publish it: it may reveal local project structure and development evidence.

M0–M2 do not call a model provider, Codex CLI or a network LLM. They do not run
Git push or Git merge. The only Git effects are local, argv-only cache sync and
task-owned worktree preparation.

FastAPI, HTTP/SSE serving, a web UI and cloud deployment are not capabilities of
this local CLI release, and are not permanently abandoned. They are a later,
separate service milestone after the offline CLI/Docker workflow: it must define
authentication, authorization, SSE reconnection, multi-process behavior, secret
isolation and deployment boundaries before any implementation begins.

## Provider profiles and secret references

Provider profiles are user-local metadata stored outside the project `.yipilot`
tree. Yipilot supports only OpenAI-compatible `POST /chat/completions`. Use an
HTTPS endpoint; plain HTTP is permitted only for a loopback development service
such as `http://localhost`. Never put URL credentials or an API key in a profile,
command line, project file, Event or Artifact.

```powershell
uv run yipilot settings provider add --name local --base-url https://api.example.test/v1 --model example-model --api-key-ref env:YIPILOT_API_KEY --settings-dir C:\work\yipilot-settings --output json
uv run yipilot settings provider use --name local --settings-dir C:\work\yipilot-settings --output json
uv run yipilot settings provider test --settings-dir C:\work\yipilot-settings --output json
uv run yipilot settings provider test --request --settings-dir C:\work\yipilot-settings --output json
```

The first test is local-only: it validates the selected profile, URL and secret
reference and never opens a network connection. Adding `--request` explicitly
permits one small live probe, which sends only the fixed text `Reply with OK.`
with a tiny output budget. It may consume provider credits. It never sends the
repository, `.git`, environment variables, user instruction, task Artifacts or
other project content.

A configured runtime request sends only its controlled user instruction to the
chosen provider endpoint. It does not upload repository source, `.git`, the
process environment or unapproved Artifacts. The key is read only from
`env:YIPILOT_API_KEY` or Windows Credential Manager through `keyring`; it is
kept only in the in-memory Authorization header and is redacted from diagnostics.

To revoke access, remove `YIPILOT_API_KEY` from the launching environment (or
delete the `yipilot` credential in Windows Credential Manager). To disable a
profile, save the same profile name with an increased `--revision` and
`--disabled`; a disabled profile cannot make a request. Use a new profile
without an `--api-key-ref` when you no longer need the secret reference.

Live pytest is disabled by default, including in CI. Run it only after selecting
a configured profile and explicitly opting in:

```powershell
$env:YIPILOT_RUN_LIVE = "1"
$env:YIPILOT_LIVE_PROFILE = "local"
uv run pytest -m live -q
```

The live test makes the same one-probe request. Normal `pytest` remains fully
offline.

## Offline quality artifacts and Harnesses

Generate quality artifacts only from an explicit PRD text or a named, run-scoped
Markdown PRD Artifact. The default `fake` and `replay` modes are offline and
deterministic; they never scan, upload, or summarize the repository.

```powershell
uv run yipilot quality generate --project C:\work\demo --taskid 1001 --prd "Build a redacted offline quality workflow." --model replay --output json
uv run yipilot quality harness --project C:\work\demo --taskid 1001 --run-id <quality-run-id> --kind srs --output json
uv run yipilot quality harness --project C:\work\demo --taskid 1001 --run-id <quality-run-id> --kind sdd --profile-revision 1 --output json
# returns run_id and approval_target_fingerprint; this only prepares the fixed test request
uv run yipilot quality pytest --project C:\work\demo --taskid 1001 --alias sample --prepare --output json
# an authorized external approval workflow persists an Approval for this exact Run and fingerprint
# pass its existing ID and the matching confirmation token; this CLI never creates approvals
uv run yipilot quality pytest --project C:\work\demo --taskid 1001 --run-id <pytest-run-id> --approval-id <approval-id> --confirmation-token <token> --output json
```

Each command stores only run-scoped Artifact refs and bounded summaries in
Events. `srs.md`, `sdd.md`, `test-plan.md`, their metadata, model-call record,
model response, bounded `pytest-report.json`, pytest stdout/stderr, ToolRun, Approval, Git evidence and
Harness reports live below `.yipilot/tasks/<task>/runs/<run>/`.

`quality pytest` is deliberately limited to the policy-generated fixed argv
`<configured Python> -m pytest` in the task workspace. It has no user supplied
shell, interpreter, pytest arguments, language runner or environment values.
A missing or stale Approval returns `APPROVAL_REQUIRED` before pytest starts. This command only consumes a persisted, exact Approval; it never grants one and does not modify Git.

`quality generate` currently supports only offline `fake` and `replay` fixtures; the Task 16 live adapter is not wired to this command. A future explicit live boundary may charge the configured provider and send only its approved prompt and Artifact inputs—not repository source, `.git`, or process environment. The SDD `--semantic-review` flag remains disabled by default; when explicitly enabled it blocks as not implemented and makes no network request or design-correctness claim. No command automatically applies,
commits, pushes, merges, or repairs Git changes.

## Docker: reproducible offline CLI environment

Docker packages the supported Python, locked dependencies, Git and the
`yipilot` CLI for a reproducible command-line environment. It is not a complete
security sandbox. Build the image from a clean clone; `.git`, `.venv`,
`.yipilot`, worktrees, `.codegraph`, caches, `.env` files and common secret-file
extensions are excluded from the build context.

```powershell
git clone <repository-url> yipilot
Set-Location yipilot
uv sync --group dev --frozen
uv run yipilot --version
docker build -t yipilot:local .
docker run --rm --network none yipilot:local --version
```

Mount only the project directory that should receive local `.yipilot` state;
the image runs as the unprivileged `yipilot` user. The default examples keep the
container offline:

```powershell
docker run --rm --network none -v "${PWD}:/workspace/project" yipilot:local init --project /workspace/project
docker run --rm --network none -v "${PWD}:/workspace/project" yipilot:local code generate --project /workspace/project --taskid 1001 --alias sample --instruction "offline fake diagnostic" --planner fake --output json
```

Do not mount a home directory, `.git`, a keyring, or a directory containing
credentials. A real provider is an explicit risk acceptance: use only a named
environment reference such as `env:YIPILOT_API_KEY` in the provider profile and
pass that variable deliberately when network access is explicitly enabled. The
container does not use the host keyring. Never pass a key on the command line
or bake it into the image. After a live experiment, remove `YIPILOT_API_KEY`
from the launching environment and delete any Windows Credential Manager entry
you created; inspect and remove the mounted project's `.yipilot` state if it is
no longer needed.

There is no docker-compose file, Kubernetes manifest, image publication, cloud
deployment, PyPI release, default network access, automatic Git apply/commit/
push/merge, external Agent CLI, multi-agent mode or arbitrary command runner in
this release. Live tests are separate (`pytest -m live`) and remain skipped by
default, including in CI; they require a configured provider and may incur cost
or send the approved request to that provider.
