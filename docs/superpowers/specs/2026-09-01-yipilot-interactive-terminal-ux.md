> **Historical reference / Superseded — 文档整理：2026-09-07。** 本文保留旧阶段设计与实施记录，不再作为当前待办或产品范围。当前目标见 [产品定义](../../product/vision.md)，技术设计见 [V2](../specs/2026-09-07-yipilot-v2-architecture.md)，实际能力见 [状态页](../../product/status.md)。下文原状态、强制要求及命令仅在原历史范围内成立。

# Yipilot interactive terminal experience

**Status:** Accepted; `/model`基础流程已实现，其余交互 Run 能力未实现\
**Date:** 2026-09-01

## Purpose

Yipilot's everyday entry point is an interactive terminal application, not a
collection of long management commands. A user starts it with `yipilot` (or
`yipilot --project <path>`), then uses slash commands such as `/model` inside
the session. A leading slash is therefore an in-application command; the shell
command is not `/yipilot`.

The existing `settings provider ...` commands remain supported as a
non-interactive automation and recovery surface. They are not the primary
operator experience once this capability exists.

## Product boundary

The interactive terminal is a transport Module. It renders state, collects a
user choice, and calls the existing application/control-plane interfaces. It
does not resolve models, access secrets, construct a ModelPort, create Git
commands, or bypass approval/evidence policy. This keeps model selection and
Run behavior identical for interactive, JSON CLI, and future UI callers.

The first implementation uses the standard terminal and no full-screen TUI
dependency. Add richer keyboard navigation, colours, or a terminal framework
only when the simple numbered selection is shown to be inadequate.

## Session contract

Launching `yipilot` opens a persistent session with a compact status line:

```text
Yipilot  |  project: D:\work\demo  |  task: none  |  model: not selected  |  idle
Type /help for commands.
Yipilot>
```

The session accepts ordinary text only when a future stage explicitly supports
a task prompt. Lines beginning with `/` are commands. Empty input redraws the
prompt; unknown commands show a short error and `/help`, without terminating
the session. Every command returns the session to a visible `idle`, `running`,
or `needs attention` state. The application must never appear to be silently
waiting during a provider request.

| Command | User purpose | Current/target behavior |
| --- | --- | --- |
| `/help` | Learn the available actions. | Shows the short command list and one-line safety notes. |
| `/status` | See project, Task, selected model, Run/Turn, and next safe action. | Reads durable state; never calls a Provider. |
| `/model` | Choose or configure a model for future live Runs. | Opens the model-selection screen below. |
| `/task` | Create or select a Task. | Target Task flow; it must use durable Task records. |
| `/run` | Start an explicitly confirmed supported stage for the selected Task. | Target Run flow; it previews scope before the first external call. |
| `/logs` | Follow durable Run/Turn events. | Target presentation over the existing event journal. |
| `/cancel` | Abort the active Run. | Requests the existing conservative cancellation path; it never invents a result. |
| `/exit` | Leave the application. | Refuses only while a local confirmation is required; it never kills an unknown external process. |

Commands unavailable at the current delivery stage are displayed as unavailable
with their reason and the next usable command. They must not be represented as
working functionality.

## `/model` interaction

`/model` is a model-selection screen, not a free-text model-name parser. In
an interactive terminal it uses a framed keyboard picker: arrow keys
move the focused option, Enter confirms it and Esc returns. It lists only
configured, public metadata; exact `ModelSelector` values remain internal UI
data, never a user-entered identity. Plain or scripted standard input retains
a numbered text fallback for automation tests.

It uses progressive screens rather than exposing an empty model list. With no
configured model, it goes directly to the first-use setup. With existing
models, it starts from this menu:

```text
模型设置
当前会话：qwen/coder（未保存）
全局默认：deepseek/coder

选择已配置的模型
添加模型服务商
测试或删除已有模型
返回
```

- Selecting a row shows its public display name and three explicit actions:
  `use for this session`, `make global default`, or `back`. A session choice stays only in
  the terminal process; making it the default changes Settings only after
  confirmation. The user can also clear the session choice and return to the
  global default.
- `2` starts a guided setup: choose official or custom setup, then choose a
  supplier, choose or enter its model, and enter an API key for user-local
  `auth.json`. Known suppliers supply their compatible URL;
  custom services and region-specific 百炼 configurations ask for one. The
  internal connection ID is generated, never entered. API Key entry writes to
  user-local `auth.json` and immediately starts the minimal live probe; success
  offers session/default/return, while failure offers API-Key retry/return. A key
  is hidden while entered and is never echoed, written into
  project files, events, artifacts, or command history.
- “测试或删除已有模型” lists local status for each configured model. Choosing
  “测试连接” sends only `Reply with OK.`, may consume credits, and sends no
  repository or Task data; deletion has its own confirmation screen.
- If nothing is configured, the setup begins immediately; cancellation leaves
  no connection or model setting behind.
- If a Run is active, the screen names its frozen selector and revision.
  Changing either the session choice or global default remains allowed for a
  later Run, but the screen states clearly: `Active Run remains qwen/coder
  (revision 3).`

Exact selector parsing, connection/model enablement, credential readability,
and the priority `explicit Run choice > session choice > global default > local failure` remain
the control-plane rules in the [model-selection specification](2026-09-01-yipilot-model-selection.md).
There is no provider discovery, fuzzy lookup, automatic fallback, OAuth, or
session-time switch for an active Run.

## Run and Turn feedback

Before the first live Provider request, `/run` renders a confirmation summary:

```text
About to start a live PatchProposal Run
Task: 1001   Model: qwen/coder (revision 3)
Input: instruction + 2 selected files (6.1 KiB) + passed task documents
Authority: proposal only; no source file, Git, or shell action is allowed
Continue? [y/N]
```

Once confirmed, the terminal prints durable progress rather than leaving a
blank prompt:

```text
Run run-... started — qwen/coder (revision 3)
Turn turn-1 started — requesting a PatchProposal
Model response in progress …
Turn turn-1 completed — proposal validated
Run succeeded — PatchProposal evidence is ready for review
```

Model text is streamed only within the configured output budget. Tool and
Harness updates use concise status lines and retain their full evidence in the
Run. A provider failure, timeout, cancellation, or validation failure uses the
same shape: state, safe cause code, what was not changed, and the next safe
action. It must never print a credential, request Authorization header, or
unredacted provider payload.

Yipilot does not support steering. While a Run is active, ordinary input is
rejected with `Run is active; use /logs or /cancel. Start a new Run after
reviewing the result.` A user may change the default through `/model`, but that
changes only a later Run; the active Run keeps its frozen `ResolvedModel`.

## First-use journey

```text
yipilot --project D:\work\demo
  -> /model
  -> choose supplier and model, then enter API Key
  -> automatic minimal probe
  -> select qwen/coder as global default
  -> /task and choose Task 1001
  -> /run, inspect the data/authority summary, confirm
  -> receive visible Run/Turn progress and a reviewable PatchProposal
```

This journey does not authorize M5-B source writes, M6 repair, Git push,
Jenkins, PR creation, merge, or steering. Those actions retain their own
future approvals and evidence requirements.

## Documentation and acceptance criteria

When implemented, update `README.md` with the first-use journey and the full
slash-command reference; update `docs/product/status.md`, the traceability
matrix, and `docs/manual-testing.md` with executable evidence. Do not describe
`/model` as a current command before its tests and control-plane migration
exist.

The implementation is acceptable only when:

- `yipilot` starts the session without requiring a project and `/help` and
  `/exit` behave predictably.
- `/model` can configure, list, select, and test a model without exposing a key;
  API-key entry and selecting an existing model for test are the explicit probe actions.
- A numbered choice resolves to one exact selector; `/status` distinguishes
  the in-memory session choice, global default, and any frozen active-Run
  selection.
- A live Run displays its frozen selector/revision, input scope, and Run/Turn
  progress, and an active Run cannot be steered or switched.
- Errors are actionable and redacted, and every interactive path reuses the
  same application/control-plane rule as its non-interactive counterpart.
- Deterministic tests cover the text interaction through scripted input/output;
  live Provider tests remain explicit and disabled by default.
