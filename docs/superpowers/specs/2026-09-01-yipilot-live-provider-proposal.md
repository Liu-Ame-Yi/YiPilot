> **Historical reference / Superseded — 文档整理：2026-09-07。** 本文保留旧阶段设计与实施记录，不再作为当前待办或产品范围。当前目标见 [产品定义](../../product/vision.md)，技术设计见 [V2](../specs/2026-09-07-yipilot-v2-architecture.md)，实际能力见 [状态页](../../product/status.md)。下文原状态、强制要求及命令仅在原历史范围内成立。

# M5-A: Provider resolution outside the Agent loop and live Patch Proposal

**Status:** Implemented
**Date:** 2026-09-01

## Purpose

Make a configured OpenAI-compatible Provider produce real, auditable code
proposals without giving it file-write, shell, commit, push, or merge authority.
This is the first M5 increment. It consumes the existing Provider configuration
and real HTTP adapter; it does not replace deterministic fake/replay modes.

## Required architecture

Provider configuration and model selection are outside the AgentRuntime loop.
Before a live Run begins, resolve exactly one enabled Profile:

```text
explicit --provider-profile
  -> settings default_profile
  -> fail with PROVIDER_NOT_FOUND
```

The selected Profile's `model` is the selected model in this increment. Do not
add `--model` override, dynamic discovery, fuzzy matching, automatic fallback,
OAuth, or a Provider catalog yet. A Profile is immutable for the Run: retain a
sanitized snapshot of profile name, type, base URL, model, revision, timeout,
output budget, and capabilities as Run evidence. Never retain an API key or its
reference in that snapshot.

## Scope

1. Add an explicit `live` code-generation mode alongside current `fake`,
   `replay`, and `replay-model` modes. Existing modes and tests remain
   deterministic and behavior-compatible.
2. Resolve the Profile before entering `AgentRuntime`; validate enabled state,
   `chat` capability, and readable secret reference. Use the existing
   `ProviderService`, `SecretStore`, and `OpenAICompatibleModelPort`; do not
   add a second HTTP client or a second provider protocol.
3. For `live`, provide the model only the user instruction, approved task
   documents, and an explicit bounded list of files from the task worktree.
   Record selected paths and hashes. Do not send the whole repository, `.git`,
   environment, or unrelated worktrees.
4. Require a strict JSON `PatchProposal`: summary plus one or more unified
   diffs whose paths are relative, unique, inside the task worktree, and within
   explicit file-count, output-byte, and diff-byte limits. Persist it as an
   Artifact and show its reference in the code-generate result.
5. Do not write the proposal to source files. The current `FakeExecutor`,
   `code apply --prepare-only`, and no-commit/no-push boundary remain intact.
6. Persist model call metadata and redacted model response evidence; emit
   progress events suitable for `run watch`. Provider errors, timeout,
   cancellation, invalid JSON, invalid patch, or persistence failure must be a
   non-successful Run.

## Acceptance criteria

- A configured Profile can be selected explicitly or through the saved default
  and is frozen into a sanitized Run Artifact before the first model request.
- Missing, disabled, incapable, or unreadable-credential Profiles fail before
  any model request or worktree write.
- A mocked live `OpenAICompatibleModelPort` creates a valid PatchProposal
  Artifact and ModelCall evidence from only approved context.
- Invalid JSON, absolute/traversal paths, duplicate paths, over-budget output,
  and malformed diffs are rejected with evidence and no source modification.
- `fake` and `replay` tests remain offline and deterministic. A real provider
  smoke test is opt-in and is never required by CI.
- Update `README.md`, `docs/product/status.md`, and the M5 AI-delivery spec
  only for behavior actually implemented; record test evidence in the
  traceability matrix when the increment is complete.

## Implementation prompt

```text
Implement M5-A from docs/superpowers/specs/2026-09-01-yipilot-live-provider-proposal.md.

First inspect the existing ProviderProfile, ProviderService, SecretStore,
OpenAICompatibleModelPort, ModelPort, CodeService, AgentRuntime, Artifact
store, Event journal, and current code-generate tests. Preserve unrelated dirty
worktree changes.

Keep provider/model resolution outside AgentRuntime. Add a live mode to code
generation that resolves exactly one enabled OpenAI-compatible Provider Profile
before a Run enters AgentRuntime, using explicit --provider-profile first and
the saved default profile second. The Profile's configured model is final for
this increment: do not add model discovery, fuzzy matching, model fallback,
OAuth, or arbitrary provider plugins. Save a sanitized Profile snapshot as
Run-scoped evidence; exclude API keys and secret references.

Reuse the existing OpenAICompatibleModelPort and ModelPort. Do not turn
FakeModelPort into a real provider and do not add another HTTP client. Keep
fake/replay modes deterministic and offline.

For live mode, send only the user instruction, approved document Artifacts, and
an explicit bounded set of task-worktree files with recorded hashes. Require a
strict JSON PatchProposal containing a short summary and bounded unified diffs.
Validate every proposed path is unique, relative, and inside the task worktree;
persist the proposal and model-call evidence; expose the proposal reference in
the command result and run-watch events.

This increment is proposal-only: do not modify source files, run arbitrary
shell, commit, push, create a PR, add steering, or add automatic repair. Invalid
provider configuration, transport errors, timeout/cancellation, malformed JSON,
or invalid patches must leave no source change and produce a conservative
non-successful Run.

Add focused deterministic tests using mocked ModelPort/HTTP transport, plus an
opt-in live smoke test if appropriate. Run ruff, mypy, pytest, and git diff
--check. Update documentation according to docs/DOCUMENTATION.md, including the
implemented status and traceability evidence only after tests pass. Summarize
the changed files, exact verification output, and intentionally deferred M5-B
controlled patch application.
```

## Deferred to M5-B

Human-approved application of a PatchProposal through one controlled patch
tool, real diff verification, pytest/Ruff/mypy, and bounded repair Turns.
