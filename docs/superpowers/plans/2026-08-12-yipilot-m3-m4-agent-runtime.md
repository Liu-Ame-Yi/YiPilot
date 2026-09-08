> **Historical reference / Superseded — 文档整理：2026-09-07。** 本文保留旧阶段设计与实施记录，不再作为当前待办或产品范围。当前目标见 [产品定义](../../product/vision.md)，技术设计见 [V2](../specs/2026-09-07-yipilot-v2-architecture.md)，实际能力见 [状态页](../../product/status.md)。下文原状态、强制要求及命令仅在原历史范围内成立。

# Yipilot M3–M4：内建 Agent Runtime 与模型闭环实施计划

## 1. 目标、前提与非目标

本计划承接已合并的 M0–M2（`main` 的 `2a19acf`）：项目已有本地事实源、Task/Operation/Run/Event、Artifact、Harness、隔离 Git workspace 与 CLI event watch。本计划的目标是让 **Yipilot 自己** 从用户任务出发，在受控 workspace 内规划和执行工具动作，并最终使用用户配置的模型 provider 进行流式推理。

这是 Yipilot 的内建 Agent Runtime，不是外部 Agent 的编排层。任何外部 Agent CLI 或类似产品只能作为架构、交互和 Harness 参照；不得被启动、解析、委托或作为 runtime dependency。`ModelPort` 只连接模型服务；`ExecutorPort` 只执行 Yipilot 管控的本地工具或 fake。

本计划仍保持单机、CLI-first、Python-only。每项普通测试必须离线、无需 API key、无需公司网络、无需真实远程 Git。真实模型请求只能是显式 opt-in 的 live test 或用户命令，且必须可审计、可限额、可脱敏。

本计划不包含：FastAPI、HTTP/SSE server、浏览器 UI、远程部署、多用户/多 worker、自动 Git push/merge、外部 Agent CLI 集成。CLI 的 JSONL event stream 已在 M2 完成，可作为未来 HTTP/SSE 的语义来源，但本计划不实现服务端 transport。

## 2. 建议执行顺序

```text
M0–M2 (已完成)
  -> Task 9  执行领域模型与安全契约
  -> Task 10 ToolRegistry / fake / 受控本机执行器
  -> Task 11 Approval、CodeHarness 与证据 gate
  -> Task 12 内建 Agent Runtime（fake/replay planner）
  -> Task 13 CLI code generate 与 prepare-only handoff
  -> Task 14 Provider profile、SecretStore 与脱敏
  -> Task 15 ModelPort fake/replay 与流式事件契约
  -> Task 16 openai_compatible adapter（显式 live opt-in）
  -> Task 17 SRS/SDD、Python 测试与质量 Harness
  -> Task 18 M3–M4 端到端验收、文档与发布检查
```

Task 9–13 是 M3。它们先以 fake/replay planner 验证 Yipilot 的执行闭环；因此不把 API key、网络波动或模型不可重复性混入安全控制面的验证。Task 14–18 是 M4：在既有、已验证的 Runtime 中接入真实模型。

## 3. 全局完成定义

每个 Task 的 PR/合并前必须满足：

1. 只修改该 Task 列出的文件或为满足测试所必需的紧邻文件；超范围必须先更新本计划。
2. 先写失败测试，再实现，且每个 bug 修复补回归测试。
3. 所有副作用保持 argv-only；不得使用 `shell=True`，不得将用户文本拼入 shell 字符串。
4. Run、Event、ToolRun、Artifact、Approval、HarnessReport 均必须可关联到唯一 Task/Operation/Run，且路径经 `PathGuard` 或等价 guard 校验。
5. 不得打印 API key、Authorization、环境变量原值、模型全文 prompt、未经批准源码或敏感工件；日志、Event、Harness 和报错全部经 redactor。
6. Task 分支合并前至少运行：

   ```powershell
   uv run pytest -o addopts='' -q -p no:cacheprovider
   uv run ruff check src tests --no-cache
   uv run mypy src --cache-dir "$env:TEMP\yipilot-m3-m4-mypy"
   git diff --check
   ```

7. 每项在 `docs/traceability-matrix.md` 追加 requirement、实现模块、精确测试、Harness ID（若有）和首次引入能力的 commit hash。

提交粒度：每个 Task 一个 feature commit；仅在审查和验证通过后 fast-forward 合并到 `main`。根仓库中与当前工作无关的未提交内容不得被暂存或覆盖。

## 4. M3：Yipilot 内建受控执行闭环

### Task 9 — 执行领域模型、工具请求与安全策略

**目的**：建立 Runtime 的不可变审计对象，避免后续工具调用只有 stdout、没有“为什么执行、准许什么、结果是什么”的记录。

**实现范围**：

- 在 Domain 中定义受控枚举和 Pydantic 模型：`ToolKind`、`ToolRunStatus`、`ExecutionTrustLevel`、`ToolRequest`、`ToolResult`、`ApprovalDecision`、`ApprovalRecord`、`ExecutionPolicy`。
- `ToolRequest` 最少绑定 `task_id`、`operation_id`、`run_id`、workspace 相对路径、允许的工具 ID、argv、超时、预算、输入 Artifact refs 与输入 fingerprint。
- `ToolResult` 最少保存开始/结束时间、退出码、由终态枚举表示的取消/超时结果、stdout/stderr Artifact refs、变更摘要与执行信任级别；禁止把大输出直接塞进 Task JSON。
- `ApprovalRecord` 必须绑定 action type、目标 fingerprint、policy revision、发起 Run 与决定时间；目标或 policy 变化即拒绝复用旧 Approval。
- 定义受控工具 policy：默认 deny；工具 ID、工具类型、可执行文件、argv schema、允许环境变量名、输出上限、超时上限均显式声明；cwd 固定为 Task workspace 根目录，不提供可配置 allowlist。

**非目标**：不执行子进程、不调用模型、不提供 CLI、不写真实 approval 文件。

**验收证据**：

- unit：非法状态转换、越界 timeout/budget、绝对路径/`..` 路径、未知工具、裸命令字符串、跨 Run artifact 引用、陈旧 approval 都被拒绝。
- property：合法 policy/request 不会产生 workspace 根外路径；序列化/反序列化保持 fingerprint。
- 追溯项建议：`M3-DOMAIN-001`、`M3-POLICY-001`。

**依赖 / 后继**：依赖 M0–M2；Task 10–13 都依赖本 Task。

### Task 10 — ToolRegistry、fake executor 与受控本机 CommandRunner

**目的**：让 Yipilot 自己安全地执行其允许的本地工具，而不是把执行委托给另一个 Agent。

**实现范围**：

- 实现 `ToolRegistry`：由 policy 显式注册工具，验证 ToolRequest，不接受任意命令、任意 cwd 或任意环境变量。
- 实现 `ExecutorPort` 与两个 adapter：确定性的 `FakeExecutor`；以及仅执行注册 argv 的 `LocalCommandRunner`。
- `LocalCommandRunner` 使用 `subprocess` argv、固定 cwd、最小环境白名单、timeout、取消和输出大小限制；stdout/stderr 写入 run-scoped Artifact，再返回引用。
- 在进程启动、结束、超时、取消、异常时追加稳定 Event（如 `tool.started`、`tool.completed`、`tool.failed`），但 Event 不泄露敏感文本。
- 默认只允许无写入的诊断工具；任何可能写 workspace 的工具留给 Task 11 的 approval gate。

**非目标**：不接模型，不运行外部 Agent CLI，不允许网络工具、任意 Python、shell、Git push/merge。

**验收证据**：

- unit：未知工具 / shell 字符串 / cwd 逃逸 / 环境变量泄露 / 输出超限均失败且无进程副作用。
- integration：fake 的事件、Artifact、退出码可重放；本机受控命令在临时 workspace 执行，timeout 和 cancel 留下完整 ToolRun 证据。
- 追溯项建议：`M3-EXECUTOR-001`、`M3-EXECUTOR-002`。

**依赖 / 后继**：依赖 Task 9；Task 11–13 依赖本 Task。

### Task 11 — Approval、CodeHarness 与执行证据 gate

**目的**：把“模型/Runtime 想修改代码”与“允许修改、变更可验证、可进入下游”分开，确保自动化不绕过 Harness。

**实现范围**：

- 保存和读取 run-scoped Approval；针对高风险工具、host_trusted 执行、写 workspace、测试执行分别要求明确 action type 与 confirm token。
- 将 approval freshness、工具 policy、workspace manifest、Git evidence、ToolRun/Artifact 完整性组合成 `CodeHarness`。
- CodeHarness 产出版本化 `HarnessReport`，包含稳定 check IDs；失败或 stale 阻断 `code_apply_prepare`。
- 基于 Task 10 的受控写工具，在临时 workspace 产生最小变更并采集 Git evidence；保留变更，不自动 reset。

**非目标**：不做远程 Apply，不做 Git push/merge，不做模型调用。

**验收证据**：

- unit：缺失 / 目标变更 / policy revision 变更后的 approval 被拒绝。
- integration：无 approval 的写动作不启动；通过 approval 后生成 evidence；任一 ToolRun、Artifact、Git evidence 或 approval stale 时 CodeHarness 阻断。
- 追溯项建议：`M3-APPROVAL-001`、`M3-HARNESS-001`。

**依赖 / 后继**：依赖 Task 9–10；Task 12–13 依赖本 Task。

### Task 12 — 内建 Agent Runtime 与 fake/replay planner

**目的**：实现 Yipilot 自己的 Agent Loop，先在无模型网络依赖条件下验证“指令 -> 计划 -> 工具 -> 事件 -> Harness”的确定性闭环。

**实现范围**：

- 定义 `PlannerPort` / 内部 agent loop：读取任务指令与批准的上下文，产生结构化 plan steps；Runtime 逐步校验、执行、记录事件，并在任一步失败时保守结束 Run。
- 实现 `FakePlanner` 和 `RecordedReplayPlanner`：前者用于确定性 fixture，后者从版本化 fixture 重放计划；两者都走同一 Runtime。
- Runtime 不接受模型直接输出的 shell 字符串；只接受可校验的 tool ID + typed args，由 ToolRegistry 转成 argv。
- 所有计划步骤、拒绝、调用、结果、Harness gate 都写入同一 Run 的 Event/Artifact 证据；Run 终态与事件一致。
- 崩溃/中断后旧 Run 标记 `execution_interrupted` 或失败，创建新 retry Run；不得从不确定副作用中间继续。

**非目标**：不发送真实模型请求；不要求复杂自主规划；不支持多 Agent 或并行 worker。

**验收证据**：

- integration：fake plan 成功、工具失败、策略拒绝、approval wait、取消、进程中断恢复都可复现且事件 sequence 正确。
- deterministic replay：同 fixture 的 plan / ToolRun / Harness 结果具有稳定结构；不稳定时间字段用 fake clock 或断言 schema。
- 追溯项建议：`M3-RUNTIME-001`、`M3-RUNTIME-002`。

**依赖 / 后继**：依赖 Task 9–11；Task 13、15、16 依赖本 Task。

### Task 13 — `code generate` CLI 与 prepare-only handoff

**目的**：把 M3 Runtime 变成可由你使用的 CLI 工作流，但不开放不可逆的远程应用操作。

**实现范围**：

- 增加 `yipilot code generate`：接受明确开发指令、Task、repo alias、planner mode（fake/replay）；创建 Operation/Run，调用 Runtime，并可交给已有 `run watch` 实时观察。
- 增加 `yipilot code apply --prepare-only`：只在 CodeHarness 通过且 approval 新鲜时生成包含 diff、证据 refs、建议命令和人工检查项的交接包；不执行 Git commit/push/merge。
- JSON 输出保持机器可读；人类输出只作呈现；错误码和 Event schema 稳定。
- README 补充离线 fake/replay 的端到端示例及 host_trusted 风险确认说明。

**非目标**：不读取 API key，不发网络请求，不默认执行真实写工具；不实现 FastAPI/SSE server。

**验收证据**：

- integration：从临时 Git repo 开始，`code generate --planner fake` 生成 Run/事件/ToolRun/Artifact/Harness；`code apply --prepare-only` 只生成 handoff，源仓库与 remote 均未变化。
- CLI：jsonl stdout 无污染、拒绝路径有稳定错误码、未批准/失败 Harness 均不能产出可用 handoff。
- 追溯项建议：`M3-CLI-001`、`M3-APPLY-001`。

**M3 出口条件**：在没有 API key、没有网络、没有外部 Agent CLI 的环境中，Yipilot 可用 fake/replay planner 独立完成一个受控的代码变更模拟、保留所有证据、通过 CodeHarness，并仅生成 prepare-only 交接包。

## 5. M4：模型接入与研发工件质量闭环

### Task 14 — Provider profile、SecretStore 与统一脱敏

**目的**：让你可以设置 API key 和模型 provider，同时确保密钥不进入 shell history、配置明文、事件或日志。

**实现范围**：

- 实现全局 provider profile 文件与 schema：`openai_compatible`、base URL、model、timeout、output budget、capability、enabled、revision、`api_key_ref`。
- 实现 `SecretStore`：Windows keyring 与 `env:<NAME>` 引用；`settings provider set-key` 使用无回显输入，不提供 `--api-key`。
- 实现 `settings provider add/list/show/use/test`；`test` 默认只作 schema/secret/URL 校验，只有 `--request` 显式允许网络探测。
- 实现统一 `SecretRedactor`，至少覆盖 CLI 错误、Event、ToolRun、Harness、HTTP trace、debug log。

**非目标**：本 Task 默认不调用真实模型；不把 provider 当作外部 Agent。

**验收证据**：

- unit：裸 key、URL credential、文件路径、非法 env ref 被拒绝；list/show 永不输出 secret；redactor 覆盖已知值和 Authorization 样式。
- integration：keyring 使用 fake adapter；`--request` 必须被显式确认且在测试中用 mock transport。
- 追溯项建议：`M4-PROVIDER-001`、`M4-SECRET-001`。

### Task 15 — ModelPort fake/replay 与流式 Agent 事件

**目的**：在不接真实网络前，验证模型流式输出如何被 Runtime 消费、持久化与在 CLI 中显示。

**实现范围**：

- 定义 typed `ModelRequest`、`ModelChunk`、`ModelResponse`、`ModelCallRecord` 与 `ModelPort`。
- 实现 `FakeModelPort` 和 `RecordedReplayModelPort`；fixture 以 request fingerprint 匹配，未知输入必须失败而不是虚构输出。
- 在调用前记录 provider/model/profile revision、预算、允许输入 Artifact、timeout、input fingerprint；默认仅发送用户指令和批准的 Markdown 工件。
- 将流式结果变为 `model.delta`、`model.message_completed`、`model.failed` Event；复用 M2 `run watch` 的 human/jsonl 渲染语义。
- Runtime 将模型输出解析成受限结构化计划，再走 Task 10 的 ToolRegistry；模型不能直接执行命令。

**非目标**：不实现真实 HTTP adapter；不向模型默认上传仓库源码、附件或图片。

**验收证据**：

- contract：fake、replay、disabled 三种 ModelPort 对同一请求拥有一致的事件和错误语义。
- integration：`code generate` 由 replay model 产生流式 delta 与合法工具计划；非法计划被拒绝、无工具执行。
- 追溯项建议：`M4-MODEL-001`、`M4-EVENT-001`。

### Task 16 — `openai_compatible` 流式 Adapter 与显式 live 验证

**目的**：把你选择的兼容模型服务连接到已被 fake/replay 验证的内建 Runtime。

**实现范围**：

- 用共享 `httpx` transport 实现 `openai_compatible` streaming adapter；将协议差异隔离在 infrastructure adapter 中。
- 明确连接/read/overall timeout、TLS/URL allowlist、请求预算、最大响应字节、取消、重试边界和错误映射；不对 uncertain 写类操作静默重试。
- 所有 HTTP 记录通过 redactor；不记录 Authorization、key、完整 prompt 或响应正文。
- 增加单独 `live` pytest marker 和 `settings provider test --request`；默认 CI 和普通 `pytest` 不运行网络测试。

**非目标**：不支持任何 provider “万能兼容”开关；不接入其他 Agent；不进行自动 apply。

**验收证据**：

- contract：mock HTTP streaming 覆盖 chunk、非 streaming fallback、401/429/5xx、超时、取消和脱敏。
- live：只有使用者显式设置 provider、key、allowlist、预算且选择 marker 后才执行；输出仅显示脱敏摘要。
- 追溯项建议：`M4-MODEL-002`。

### Task 17 — SRS/SDD、Python 测试与质量 Harness

**目的**：把 Agent 的输出从“执行过工具”提升为“可验证的研发工件与质量结论”。

**实现范围**：

- 输入以 PRD fixture 或明确文本为边界，生成 run-scoped `srs.md`、`sdd.md`、test plan 与对应 metadata/hash。
- 实现 `SrsHarness`、`SddHarness`、`UnitTestHarness`；检查结构、引用、输入 fingerprint、新鲜度、workspace/Git evidence 与测试结果。
- 第一版仅支持 Python/pytest；测试命令仍经 ToolRegistry/ExecutorPort，结果作为 Artifact，不把终端全文写入 Event。
- SDD 语义检查以 feature flag 发布；不能声称与原 Go 项目字节或协议兼容。

**非目标**：不做任意语言/任意命令 runner；不自动提交代码或上传工件。

**验收证据**：

- fixture integration：PRD -> fake/replay ModelPort -> SRS/SDD -> harness report -> Python pytest report 的可重复闭环。
- fault：输入 artifact 修改、模型 profile revision 变化、workspace HEAD 变化或测试报告损坏后，旧报告标记 stale 并阻断下游。
- 追溯项建议：`M4-ARTIFACT-001`、`M4-HARNESS-001`、`M4-TEST-001`。

### Task 18 — M3–M4 端到端验收、可用性文档与发行检查

**目的**：确认真实用户能够从 clone、配置、离线试跑到（可选）受限模型调用地使用 Yipilot，而不错误地把实验能力当作安全自动化。

**实现范围**：

- 新增端到端 acceptance：临时 repo、fake/replay model、内建 Runtime、ToolRun、Harness、prepare-only handoff；并核对 source checkout/remote 未变化。
- 可选 live smoke test 保持独立，文档明确费用、数据发送范围、风险接受、如何撤销 key 与如何清理 `.yipilot`。
- 更新 README、traceability matrix、Docker/容器分发说明：容器用于复现 Python/Git 运行环境与可选 sandbox，不打包 key、不默认联网、不把容器误称为完整安全隔离。
- CI 保持离线 deterministic checks；live 与容器 smoke 只可手动触发。

**非目标**：不部署云服务，不引入 FastAPI，不发布到 PyPI，除非另立发布任务。

**验收证据**：

- `uv sync --group dev --frozen` 后全量 pytest、ruff、mypy、Docker build（若 Dockerfile 已纳入范围）均通过。
- 端到端 test 报告引用每个 Run 的 Event、ToolRun、Artifact、HarnessReport 和 handoff。
- 追溯项建议：`M4-ACCEPTANCE-001`。

**M4 出口条件**：你可以配置一个模型 provider，先用 replay/fake 安全演练，再显式选择 live 调用；Yipilot 自己把模型输出转为受控工具计划、保存全部审计证据、运行 Harness，并只生成可人工确认的 apply handoff。

## 6. 暂缓队列（不创建实现任务）

- FastAPI、HTTP SSE、网页 UI、云部署不是永久否决，也不是 Yipilot 的运行时依赖。它们在 Task 18 完成离线 CLI/Docker 验收后，按实际使用需求另立服务化规格和实现任务。服务化任务必须重新定义鉴权、权限、SSE 断线恢复、多进程、密钥隔离和部署边界，不能混入 M3–M4 验收任务。
- Docker sandbox：可作为 M3 host_trusted 的风险降低实验；只有白名单挂载、网络、凭据和取消行为可验证后才可宣称 `sandboxed`。
- 中心化服务、PostgreSQL、Redis、多 worker：遵循设计规格第 16.4 节，独立里程碑。
- 自动 Git commit/push/merge、企业 Git provider、ACP：仍需要独立 approval、身份和审计设计。

## 7. 启动 M3 前的准备

1. 先将当前设计文档和本计划作为一个意图明确的 docs commit（不混入 `.codegraph/` 或其他用户本地内容）。
2. 从该 commit 创建 `codex/m3-execution-domain` worktree，只执行 Task 9。
3. Task 9 通过审查并合并后，再依序创建 Task 10；不要并行实现有前置依赖的 Task 10–13。
4. Task 13 合并时，先进行一次无 API key 的人工验收，再决定是否启动 M4。
