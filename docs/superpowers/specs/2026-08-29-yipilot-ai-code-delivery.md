> **Historical reference / Superseded — 文档整理：2026-09-07。** 本文保留旧阶段设计与实施记录，不再作为当前待办或产品范围。当前目标见 [产品定义](../../product/vision.md)，技术设计见 [V2](../specs/2026-09-07-yipilot-v2-architecture.md)，实际能力见 [状态页](../../product/status.md)。下文原状态、强制要求及命令仅在原历史范围内成立。

# Yipilot AI code delivery: M5–M8 specification

**Status:** M5-A implemented; M5-B–M8 accepted not implemented
**Date:** 2026-08-29

## 1. Purpose

Yipilot must make AI-assisted code writing a core capability for Python
projects. A configured model Provider helps a Task progress from an approved
requirement to a tested, review-ready Pull Request; deterministic control-plane
rules retain authority over files, commands, evidence, CI, and merge decisions.

This specification is the product direction after M0–M4. M5-A implements only
an auditable live PatchProposal: it does not write source or execute model
actions. M5-B–M8 are not implemented.

## 2. Provider and Pi Agent relationship

Yipilot reuses its versioned Provider profiles, model selection, secret
references, redaction, timeout, and output-budget controls. Its first live
Provider protocol remains OpenAI-compatible Chat Completions.

Pi Agent is an experience reference for Provider configuration, model choice,
and authentication interaction. Yipilot is not a wrapper around Pi Agent and
must not call, embed, or require the Pi Agent CLI at runtime.

API keys are read only from approved environment or credential-store references.
They must not appear in command lines, repository files, Artifacts, Events,
diagnostics, Pull Requests, or Jenkins parameters.

## 3. End-to-end workflow

```text
PRD / requirement
  -> SRS / SDD / Test Plan
  -> approved Provider and repository context
  -> model produces structured plan or patch
  -> Yipilot validates and writes only in task worktree
  -> pytest / Ruff / mypy
  -> bounded AI repair when verification fails
  -> Change Handoff
  -> human approval
  -> feature-branch commit and push
  -> Jenkins CI for the same commit SHA
  -> Pull Request with evidence summary
  -> human review and merge
```

Yipilot owns Task, Operation, Run, worktree, evidence, policy, and handoff.
Jenkins owns external deterministic CI. GitHub owns Pull Request and protected
branch policy. A human owns every high-risk approval and the final merge.

### 3.1 Run, Turn, and ToolRun lifecycle

One **Run** is the outer delivery loop: it starts when Yipilot has created the
Operation and Run for one explicit user action, and ends only when all required
evidence is durable and the Run reaches a terminal status. A successful Change
Handoff is an output of the Run, not another step inside it.

```text
Run created (pending)
  -> running
  -> plan, controlled tool work, evidence, Harness
  -> succeeded | failed | paused | cancelled
```

`failed` means an error or failed quality gate and includes an error code.
`paused` means the Run is waiting for a required human Approval. `cancelled`
means a user or an external controller explicitly stopped the Run; Yipilot does
not use a separate `aborted` state. A ToolRun may additionally be `timed_out`;
its parent Run records the resulting non-successful outcome and evidence.

The current M0–M4 event journal already records `run.started`, `plan.created`,
`tool.started`, `tool.output`, `tool.completed`, `harness.completed`,
`approval.required`, and terminal `run.*` events. `yipilot run watch` replays
and follows those durable events. Its current code-generation path is
fake/replay, so it does not yet show a live model response or a real code diff.

M5 adds a **Turn** inside a Run. A Turn is one user-visible model decision
cycle, not a synonym for a Run; one Run may contain several Turns when M6
performs bounded repair. M5 must persist and expose the following lifecycle:

```text
turn.started
  -> model.delta* / tool events*
  -> turn.completed | turn.failed | turn.cancelled
```

The user-facing stream shows phase progress, approved context summary, Provider
and model identity, bounded model output, tool status, and links to retained
Artifacts. It must not expose API keys, hidden model reasoning, or unbounded
raw logs. A change of goal or scope does not steer an active Run: the user
cancels it if needed and starts a new Run, preserving the old evidence.

## 4. M5: real AI code generation and controlled writes

### 4.0 Implemented M5-A boundary

`code generate --planner live` resolves one enabled chat-capable Profile before
the Runtime boundary, using `--provider-profile` before the saved default. It
uses that Profile's configured model and the existing OpenAI-compatible
ModelPort. Before requesting the model it persists a sanitized Provider
snapshot; it sends only the instruction, SRS/SDD/test-plan Artifacts with passed
SRS and SDD Harness reports when present, and explicit bounded task-worktree
files. The response
must be a bounded strict JSON PatchProposal with relative, unique unified-diff
paths. Response, ModelCall, context hashes, proposal and progress events are
durable Run evidence. M5-A does not apply a patch or execute a model command.

### 4.1 Approved model context

The model may receive only:

- the explicit user requirement and approved PRD/SRS/SDD/Test Plan Artifacts;
- files and searches selected by a task-scoped context policy inside its
  worktree;
- redacted, bounded results from previously allowed tools and verification.

The system records the selected file paths, content hashes, Artifact references,
Provider profile revision, model, timeout, input budget, and output budget. It
must not silently send the whole repository, `.git`, process environment,
credentials, unrelated worktrees, or the user's home directory to a Provider.

### 4.2 Structured agent actions

The model cannot return an arbitrary shell command as an executable action. It
may produce a typed plan containing only policy-registered requests, initially:

- list or read approved worktree files;
- search approved worktree files;
- create, replace, or delete an approved worktree file;
- apply a structured patch to an approved worktree file;
- request the fixed Python verification commands defined in M6.

Every request is associated with one Task, Operation, Run, workspace, policy
revision, and instruction fingerprint. The Runtime validates this association
before a tool starts.

### 4.3 Write boundary and evidence

Writes are permitted only below the task-owned worktree and only after the
applicable Approval. Before and after each write, Yipilot retains changed-file,
diff, base/head revision, ToolRun, stdout/stderr, and Artifact evidence.

The initial product may expose an explicit `host_trusted` preview only if it
clearly states that it has the launching user's authority and requires a fresh
human Approval. It must not be described as a sandbox. A production safety
claim requires a separately designed and verified sandbox.

### 4.4 M5 acceptance criteria

For a Python fixture repository and explicit requirement, the system proves:

1. the configured Provider received only approved inputs;
2. the model generated a valid typed plan or patch;
3. all writes remained in the Task worktree;
4. a real diff and its Git evidence were retained;
5. invalid plans, out-of-worktree paths, missing approval, timeout, cancellation,
   and persistence failure produce a conservative non-successful Run.

## 5. M6: verification and bounded repair

### 5.1 Fixed verification policy

The first Python verification policy runs only fixed argv forms in the task
worktree:

```text
<configured Python> -m pytest
ruff check src tests
mypy src
```

Each command has a policy revision, timeout, output budget, result Artifact,
and ToolRun. A project-specific change to this policy requires a focused
specification; users cannot supply arbitrary shell, interpreter, pytest
arguments, environment variables, or working directories.

### 5.2 Repair loop

A verification failure may be transformed into a bounded, redacted diagnostic
input for the same Task Run. The model may propose another approved patch, after
which all relevant verification runs again.

The loop has explicit limits for repair attempts, elapsed time, model budget,
tool timeout, output bytes, files changed, and total diff size. Reaching any
limit, cancellation, an unapproved action, or an invalid model response ends in
a failed or paused Run; it never becomes a successful Handoff by explanation.

### 5.3 M6 acceptance criteria

The system proves that a passing Handoff has a current plan, current worktree
revision, complete verification evidence, and no unexplained failure, timeout,
or skipped required check. A stale report, changed diff, altered Artifact, or
wrong SHA blocks the Handoff.

## 6. M7: Git delivery and Jenkins CI

### 6.1 Commit and push

Only a current, explicit Approval bound to the Run, Change Handoff fingerprint,
head SHA, target branch, and remote may permit a commit and push. The target
must be a generated feature branch such as `feature/yipilot-<task>-<run>`;
direct push to `main`, force push, and push to an unapproved remote are denied.

### 6.2 Jenkins result import

Jenkins uses a versioned `Jenkinsfile` and runs deterministic Python CI for the
pushed commit: Ruff, mypy, pytest, and Git format validation at minimum.
Yipilot imports Jenkins data read-only and stores provider, job/build identity,
commit SHA, external URL, conclusion, redacted summary, report hashes, and
collection time as a CI Result.

An unknown, stale, tampered, or mismatched SHA cannot satisfy the delivery gate.
Jenkins credentials and complete unredacted logs never enter Yipilot Artifacts.

### 6.3 M7 acceptance criteria

The system proves that a pushed feature branch was approved for the exact
revision and that the imported Jenkins result belongs to its current head SHA.
CI failure or missing CI blocks PR readiness but does not delete the worktree or
evidence.

## 7. M8: Pull Request and human review

Yipilot may create a Pull Request only after M7's delivery gate is satisfied.
The PR body contains a concise requirement, design, diff, test, and CI evidence
summary with links; it excludes secrets and unbounded logs. Yipilot may read PR
status, review state, required checks, and merge-queue state into its evidence.

Yipilot must not approve its own PR, dismiss review, bypass protection, or
merge a protected branch. GitHub branch protection requires the configured CI
checks and human review before merge.

### M8 acceptance criteria

The Pull Request references the correct feature branch and head SHA, exposes no
secret, and makes its evidence traceable back to the Task and Handoff. A new
push invalidates earlier CI and review evidence until the current revision is
revalidated.

## 8. Deferred work

FastAPI/SSE, cloud deployment, multi-user coordination, broad PRD ingestion,
GitHub webhooks, Merge Queue automation, SAST/SBOM/coverage policies, and a
complete sandbox design are separate follow-on specifications. They must not
weaken M5–M8's worktree, approval, evidence, or branch-protection rules.

## 9. Open decisions before M5 implementation

1. Is the first real code-writing mode an explicitly approved `host_trusted`
   preview, or must a sandbox precede all writes?
2. Does every write need Approval, or does one fresh Run-bound Approval cover a
   bounded sequence of writes and verification attempts?
3. Does the model apply a validated patch directly, or must a human approve a
   generated diff before the first write?
4. Is repository context explicit file selection only, or may a configured
   policy automatically select bounded files relevant to a task?

These decisions require an ADR or a focused M5 design before code changes
begin.
