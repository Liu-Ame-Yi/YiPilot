> **Historical reference / Superseded — 文档整理：2026-09-07。** 本文保留旧阶段设计与实施记录，不再作为当前待办或产品范围。当前目标见 [产品定义](../../product/vision.md)，技术设计见 [V2](../specs/2026-09-07-yipilot-v2-architecture.md)，实际能力见 [状态页](../../product/status.md)。下文原状态、强制要求及命令仅在原历史范围内成立。

# M5-C: real CodeAgent Runtime convergence

**Status:** Accepted not implemented\
**Date:** 2026-09-03

## Decision

Yipilot will have one user-visible execution path:

```text
/model + /task + /run
  -> frozen ResolvedModel
  -> real ModelPlanner
  -> AgentRuntime
  -> policy-registered tools
  -> Events and Artifacts
  -> PatchProposal
```

The current M5-A direct live PatchProposal implementation is an interim,
auditable capability. It must not become a permanent second CodeAgent path.

`fake` and `replay` have no user-facing planner mode, no interactive fallback,
and no production behavior. Tests may inject deterministic ModelPort responses
or recorded fixtures, but they must exercise the same AgentRuntime, tool
policy, evidence, and failure handling as a real Run.

## Scope

1. Use a real configured ModelPort through ModelPlanner for a user `/run`.
2. Retain the existing Provider/model resolution outside the Runtime and freeze
   `ResolvedModel` before the Run begins.
3. Allow only policy-registered, task-worktree-scoped tools. Initial capability
   is read/search plus bounded PatchProposal creation; source writes remain
   subject to the later M5-B approval boundary.
4. Persist user-visible single-Turn events (`turn.started`, terminal Turn
   event) and existing Run/Model/Tool/Artifact evidence.
5. Delete obsolete product-facing fake/replay execution branches when the real
   path has deterministic injected-port coverage.

## Non-goals

- Do not remove deterministic test doubles from `tests/`.
- Do not give the model arbitrary shell, filesystem, Git, network, commit,
  push, PR, or merge authority.
- Do not implement M5-B controlled apply, M6 repair, Jenkins, PR flow,
  steering, background workers, or multi-user execution.

## Acceptance criteria

- An interactive `/run` always uses a real selected/default model in production;
  it cannot silently select fake or replay behavior.
- A deterministic injected ModelPort exercises the same Runtime path as a real
  provider and covers successful proposal, invalid proposal, tool denial and
  provider failure.
- No product CLI option exposes `fake` or `replay` as an ordinary user Run
  mode after convergence.
- All model inputs, tool requests, turn transitions, artifacts, and terminal
  outcomes remain bounded, durable, and redacted.
- Source mutation remains impossible before the separate M5-B approval and
  controlled-apply implementation.
