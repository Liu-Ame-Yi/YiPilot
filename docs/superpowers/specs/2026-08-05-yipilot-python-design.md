# Yipilot 实施设计规格（业务兼容、能力分级版）

- 状态：Draft，待评审
- 日期：2026-08-05
- 规范包名与 CLI：`yipilot`
- 代码仓库名：`yipilot`
- 事实来源：`规划书.md`（公司 Go 项目的行为与合同梳理）以及公开工程实践
- 实施策略：业务合同优先；仅本地 CLI；无法取得的公司环境不得虚构，必须显式降级、禁用或以可替换 Adapter 表达。

## 1. 执行摘要

Yipilot 是一个本地优先的 AI 辅助研发流程控制器。用户以 `task_id` 建立研发单元，系统将需求、设计、代码变更、测试、审批、Git 证据和运行事件持久化为可审计工件。模型或外部 coding agent 只负责受控的推理和生成；状态机、权限、路径、工作区、质量门禁和发布动作必须由确定性程序控制。

第一版不追求复制 Go、Eino、Hertz、公司 MCP、内部 Wiki、BugTask、GitLab 或 ACP 的私有实现。第一版应做到：在一台开发者机器上，从空目录创建 Project，登记一个 Git 仓库，为一个 Task 创建隔离 worktree，记录 Operation/Run/Event，调用一个受控执行器，收集 Git 证据，并由 Harness 阻止未经验证或未经批准的下一步动作。

## 2. 目标、非目标与成功标准

### 2.1 产品目标

1. 用可追溯的工件链替代一次性 prompt：`PRD → SRS → SDD → Code → Test → Apply`。
2. 任意 Task 的状态、输入、输出、工具调用和人工决定可在重启后读取和解释。
3. 任何会修改代码的执行都只发生在任务专属 Git worktree，而非用户主工作区。
4. 所有模型输出均可被 Harness 检查；模型自评不能作为质量证明。
5. 所有高风险副作用（执行器写代码、Git push、外部回写）具备最小权限、证据与显式审批边界。

### 2.2 非目标

1. 不实现 Go/Eino 私有 checkpoint 的二进制兼容或直接恢复。
2. 不在 M1–M4 依赖公司内网服务、真实 GitLab、真实 MCP、真实 Wiki、BugTask 或生产 token。
3. 不自动合并 PR/MR，不自动 push，除非用户在未来版本明确批准 Apply。
4. 当前项目只承诺交付本地 CLI；GUI、REST、SSE、ACP、云端多租户、多 worker 集群和服务端认证不属于当前实现范围，但其扩展边界保留在第 16 节，后续是否实施另行决定。
5. 不声称 Harness 证明需求正确、代码无缺陷或模型输出真实；它只证明明确列出的、可重复执行的检查已通过。

### 2.3 Definition of Done

MVP 完成时，离线测试必须证明：

- 可 `yipilot init`、`task init`、`repo add --sync`；不需要真实模型。
- 同一个 `task_id` 的重复 Operation 请求遵守幂等规则；不同请求冲突时明确失败。
- 两个 Task 的路径、锁、Run、事件和 workspace 绝不混用。
- 执行器的写入路径被限制到任务 workspace；主工作区的 Git 状态保持不变。
- 每次受控执行至少保存输入摘要、stdout/stderr、Git 四件套和 HarnessReport。
- Harness 失败、取消、超时、审批拒绝、进程重启均产生可解释的终态或可恢复状态。

### 2.4 产品命名与 Go 基线的关系

Yipilot 是新的产品、Python 包、CLI 和运行目录名称：`yipilot`、`src/yipilot/`、`.yipilot/`。公司 Go 基线中的 `zpilot` 名称仅作为行为参考，不是 Python 的外部兼容合同。因此第一版不提供 `zpilot` CLI alias，也不读取/覆盖 `.zpilot/`；未来若需导入旧数据，必须新增只读、显式确认的 `yipilot migrate zpilot` 工具，并按 C0–C3 规则写迁移账本。

## 3. 兼容性模型与外部依赖策略

### 3.1 能力等级

| 等级 | 名称 | 定义 | 示例 |
| --- | --- | --- | --- |
| C0 | unavailable | 显式不可用，不伪装成功 | 公司 BugTask Adapter 未配置 |
| C1 | inspectable | 可读取、展示或归档，不能继续执行 | 历史 Go checkpoint |
| C2 | equivalent | 用 Python 记录实现同一业务效果 | Task/Run/Event、Git evidence |
| C3 | resumable | 重启或暂停后可在安全条件下继续 | Python 自己创建的 Run/Approval |
| C4 | byte/protocol compatible | 字节或对外协议严格兼容 | 仅在 fixture 证明后声明 |

项目 README、CLI help 和 `compatibility-report.json` 必须逐能力声明等级，禁止使用笼统的“完全兼容”。

### 3.2 Adapter 状态

每个外部端口都必须有至少四种实现形态：

- `fake`：用于单元与集成测试，结果完全确定。
- `recorded_replay`：读取脱敏 fixture，模拟曾经获得的真实响应。
- `disabled`：给出稳定错误码、配置说明和下一步建议，不发生网络请求。
- `production`：仅在显式配置、allowlist、凭据和审批满足时启用。

公司依赖暂时均为 `disabled` 或 `recorded_replay`。Git 本地命令和本地 Python/CLI 执行器可作为 MVP 的 production Adapter，但仍需路径与审批控制。

### 3.3 可借鉴但不直接复用的公开项目

- Pi 提供 agent runtime、工具调用、模型统一接口和交互 CLI；其默认不包含文件、进程、网络、凭据权限边界，因此 Yipilot 不应把它视为安全控制面。
- OpenHands 表明 agent backend、worktree 隔离和本地/远程执行环境可解耦；本项目采用同样的 `ExecutorPort` 思路，但不引入其完整 UI/服务平台。
- SWE-agent 的 Issue→代码修复闭环可作为 Bugfix 的参考；本项目额外要求持久 Task、审批与阶段工件。
- LangGraph 的 checkpoint/interrupt 模式可用于阶段内部图，但不替代 Application 层的 Operation、Run、事件与审计事实源。

参考链接：

- <https://github.com/earendil-works/pi>
- <https://github.com/OpenHands/OpenHands>
- <https://github.com/SWE-agent/SWE-agent>
- <https://docs.langchain.com/oss/python/langgraph/persistence>

## 4. 总体架构

```text
CLI
 │
Application services
              │
 Domain: Task · Operation · Run · Event · Approval · Artifact
              │
 Ports: Store · Clock · Lock · Repo · Executor · Model · PRD · Identity
              │
 Infrastructure: filesystem · git · subprocess · adapters
              │
         local `.yipilot/` evidence tree
```

依赖只能自外向内。Domain 不能导入 Typer、Pydantic CLI DTO、LangGraph、subprocess 或 Git SDK。Application service 是唯一能同时协调 Domain、Store、Harness 与外部 Port 的位置。CLI 只能做输入校验、呈现与调用 service；不得绕过状态机直接写文件。

### 4.1 Python 技术基线

- Python 3.12+、`uv` 或等价受锁定依赖管理。
- `Typer`：CLI；`Pydantic v2`：边界 DTO 与文件 schema；`PyYAML`：配置。
- `pytest`、`pytest-xdist`、`Hypothesis`：测试与属性测试。
- `filelock` 或自研跨平台文件锁：单机互斥。
- `httpx`：外部 HTTP Adapter；所有 network 必须经过共享 transport。
- `FastAPI`、`uvicorn`：仅作为可选 `server` extra；当前 CLI 核心不安装、不导入。
- 可选 LangGraph：只作为 graph runtime；其持久化记录不得成为业务唯一事实源。

任何新增依赖必须记录：用途、替代方案、许可证、升级策略和是否执行生命周期脚本。依赖安装和 CI 默认禁用非必要脚本。

### 4.2 建议包布局

```text
src/yipilot/
  domain/          # 纯模型、枚举、不变量、领域错误
  application/     # 用例与编排服务
  ports/           # Protocol/ABC 接口
  infrastructure/  # filesystem、git、subprocess、http、adapter
  harness/         # 独立质量门禁工程
  workflows/       # stage graph 与输入/输出组装
  transport/       # cli；future extras: rest、sse、acp
  bootstrap/       # composition root、配置与 DI
  assets/          # 版本化 SKILL、模板、JSON Schema
tests/
  unit/ contract/ integration/ live/ fixtures/
docs/
  adr/ specs/ compatibility/
```

## 5. 领域模型与不变量

### 5.1 核心实体

| 实体 | 作用 | 不变量 |
| --- | --- | --- |
| Project | `.yipilot/config.yaml` 所在根 | 不可在未初始化根外写运行数据 |
| Task | 长生命周期研发单元 | `task_id` 为非负十进制字符串；拥有独立根目录 |
| Stage | 稳定业务阶段 | 名称是受控枚举，不由用户任意输入 |
| Operation | 一次用户意图 | 有 request fingerprint 与 idempotency key |
| Run | 一次 stage 尝试 | 只能单向进入终态；属于唯一 Operation 与 Task |
| Event | 追加式运行事实 | 同 Run 的 `sequence` 严格递增 |
| Artifact | 可引用业务工件 | 有 producer、输入 fingerprint、相对路径与 hash |
| Approval | 高风险动作的人类决定 | 决定绑定目标 hash/revision；陈旧即失效 |
| HarnessReport | 质量门报告 | 关联唯一 Run、Stage、输入 fingerprint 与 checks |

### 5.2 状态机

`RunStatus = pending | running | paused | succeeded | failed | cancelled`。

- `pending → running`：取得 stage lock 且前置条件通过。
- `running → paused`：需要澄清或审批；保存 interaction/approval 与 checkpoint 提示。
- `running → succeeded`：产物与必需 Harness 均达到策略要求。
- `running/paused → cancelled`：用户显式取消；后续子进程必须终止。
- `running/paused → failed`：不可恢复错误、超时、Harness 阻断或恢复校验失败。
- 终态不可倒退。重试必须创建新 Run，不覆盖旧 Run。

`OperationStatus = planned | running | waiting | completed | failed | cancelled`，由其 Run 集合投影得出。Task 中的阶段摘要只是投影，可从 Operation/Run 重建，不能成为唯一事实源。

### 5.3 错误模型

每个用户可见错误具有稳定 `code`、安全 `message`、`stage`、`occurred_at` 与可选 `retryable`。内部异常、路径、token、命令参数和响应正文仅写受控日志；绝不回显 secret。

建议基础错误码：`PROJECT_NOT_INITIALIZED`、`TASK_NOT_FOUND`、`INVALID_TASK_ID`、`PRECONDITION_FAILED`、`OPERATION_CONFLICT`、`LOCK_UNAVAILABLE`、`HARNESS_FAILED`、`APPROVAL_REQUIRED`、`APPROVAL_STALE`、`PATH_DENIED`、`EXTERNAL_UNAVAILABLE`、`EXECUTOR_TIMEOUT`、`GIT_FAILED`、`UNSUPPORTED_CAPABILITY`。

## 6. 本地持久化、权限与恢复

### 6.1 目录合同

```text
.yipilot/
  config.yaml
  locks/
  tasks/<task-id>/
    task.yaml
    operations/<operation-id>.json
    runs/<run-id>/
      run.json
      events.jsonl
      checkpoints/
      inputs/
      artifacts/
      harness/
      tool-runs/
      git/
      logs/
    repos/<alias>/
      metadata.json
      workspace/             # Git worktree；不得纳入本仓库
    approvals/
  cache/repos/<url-hash>/
  audit/
```

所有内部引用使用 Project 根目录相对路径。任何绝对路径、`..`、符号链接逃逸或不在 allowlist 下的真实路径均拒绝。`workspace` 与 cache 的路径由 repo URL 的规范化 hash 派生；alias 仅作人类显示，不能决定真实存储位置。

### 6.2 写入与锁

- 权威 JSON/YAML：同目录临时文件、fsync、原子 rename；写后 re-read 验证 schema。
- JSONL：持锁追加，单行 JSON，带 sequence 与 timestamp；损坏尾部作为诊断项，不静默吞掉。
- 配置、token、任务、Operation、Run、Approval 默认仅当前用户可读写；跨平台无法实现 POSIX `0600` 时记录降级警告。
- 锁以细粒度为原则：project config、task、stage、operation、workspace、approval 不共用一把全局锁。
- 任一写入失败不得让 Run 标为成功；保留失败证据并让恢复逻辑重新判定。

### 6.3 恢复原则

恢复不重新“猜测”模型或外部副作用。它先验证 Run 状态、锁/lease、输入 fingerprint、可用 artifact、执行器记录和 approval；只有能证明之前副作用完成时才跳过。无法证明时标为 failed 或 waiting，要求用户建立新的 Run/Operation。

## 7. Harness 工程规格

### 7.1 定位与边界

Harness 是独立质量门禁子系统，负责对确定性输入执行确定性检查、产生持久化证据，并向 Application service 返回可用于状态机决策的结论。Harness 不是：

- LLM 对其输出的自我评价；
- 可从任意 prompt 调用的通用“工具”；
- 代替测试框架、代码审查或人工审批的万能判定器；
- 仅在终端打印一行结果的 helper。

### 7.2 公共数据合同

```python
HarnessStatus = Literal['passed', 'warning', 'failed', 'skipped']

HarnessCheck = {
  'id': str,                 # 稳定 ID，例如 GIT-WORKTREE-003
  'description': str,
  'status': HarnessStatus,
  'message': str,
  'suggestion': str | None,
  'evidence_refs': list[str],
}

HarnessReport = {
  'schema_version': 1,
  'report_id': str,
  'task_id': str,
  'operation_id': str,
  'run_id': str,
  'stage': str,
  'harness_name': str,
  'harness_version': str,
  'started_at': str,
  'ended_at': str,
  'input_fingerprint': str,
  'inputs': list[dict],
  'status': HarnessStatus,
  'checks': list[HarnessCheck],
  'summary': str,
}
```

聚合规则必须版本化：任一 blocking `failed` 则总体 `failed`；无失败且至少一个 blocking `skipped` 则总体 `skipped`；无 failed/skipped 但有 warning 则 `warning`；只有全部 blocking check 通过才 `passed`。`skipped` 默认不等于通过，且不得推进下游 stage。某一阶段能否接受 `warning` 必须由 stage policy 显式声明；non-blocking check 必须在策略中显式声明，不能由 Runner 临时判断。

### 7.3 代码结构

```text
src/yipilot/harness/
  models.py              # schema、状态、聚合规则
  protocol.py            # HarnessRunner Port
  registry.py            # stage + policy → runner
  fingerprint.py         # 工件、Git、配置、版本的摘要
  report_store.py        # 原子报告存取
  task_integrity.py
  git_workspace.py
  srs.py
  sdd.py
  code.py
  unit_test.py
  policies.py
```

Runner 只读取 `HarnessContext` 允许访问的工件和目录；它不直接读取任意主机路径，不启动未登记命令，不修改权威 SRS/SDD/源代码。需要执行测试的 Unit-test Harness 只能通过受控 `CommandRunnerPort` 调用白名单命令。

### 7.4 Harness 类别与初始检查

| Harness | 首版稳定检查 | 阻断策略 |
| --- | --- | --- |
| TaskIntegrity | task/run schema、目录归属、event sequence、原子记录 | failed 阻断所有后续 stage |
| GitWorkspace | cache/worktree 存在、HEAD 基线、主目录未改、变更路径合法、Git 四件套齐全 | failed 阻断 code 成功与 Apply |
| SRS | 存在/非空、FR/NFR、未决 clarify、需求 ID、来源引用 | M4 先允许部分 warning；缺失结构 failed |
| SDD | 输入 SRS 可用、主文档/细节文档、链接、追踪矩阵、模块/文件矩阵 | C2 基础检查，增强语义检查 feature flag |
| Code | executor record、workspace、diff/stat/changed-files、验证命令证据 | 缺关键证据 failed；无变更为明确 `no_changes` |
| UnitTest | Python/pytest 计划与结果一一对应、命令日志、失败诊断、禁止未解释 skipped、超时清理 | failed 阻断 Report/Apply |

检查 ID 永不因文案变更而重用。新增严格语义检查必须使用新 ID、新 harness version 和 feature flag，避免破坏业务兼容。

### 7.5 输入 fingerprint 与新鲜度

每个 Report 至少包含：stage、任务元数据版本、输入 artifact hash、repo URL、Git HEAD、workspace 相对路径、Harness 版本、配置策略版本。任一相关值变化后，旧 Report 标记 `stale`（通过 metadata 表达，不伪造新 Report）。下一阶段不得把 stale report 当作通过证据。

### 7.6 Harness 的测试要求

- 每个 Check 有 passed/failed/边界 fixture。
- report schema、聚合、顺序、错误码和输出 JSON 做 golden tests。
- 路径穿越、符号链接、损坏 JSON、超大工件、恶意 Markdown/YAML 做负向测试。
- Unit-test Harness 需验证子进程超时后确已停止、日志截断、未运行用例被明确标记。
- M4 增加 property tests：事件序列递增、任意合法 path 不逃逸根、报告 fingerprint 与输入一一对应。

## 8. Git、Repo 与受控执行

### 8.1 Repo 生命周期

`repo add` 接受 URL、alias、默认分支和只读/可写策略。系统规范化 URL，建立 cache，记录 remote HEAD 和同步时间。clone/fetch 发生在 cache；Task 的开发发生在 workspace worktree。所有 Git 子进程使用 argv 数组，禁止 shell 拼接。

### 8.2 受控 worktree

1. 创建 Task 后先记录 base commit 和 workspace manifest。
2. 以受控分支名创建或复用 worktree，例如 `yipilot/task-1001/<run-id>`。
3. Executor 的 cwd 必须等于 workspace，允许写入集合由 PathGuard 计算。
4. Executor 结束后收集 `status.txt`、`diff.patch`、`diff-stat.txt`、`changed-files.txt`、HEAD 与 base SHA。
5. GitWorkspaceHarness 验证证据与路径；失败时保留变更、阻断 Apply，不自动 reset。

### 8.3 ExecutorPort

`ExecutorPort.execute(request) -> ExecutorResult` 支持 `codex_cli`、未来 `pi`、未来 `claude_code` 和 `fake`。request 必须含 workspace、任务上下文摘要、允许工具、超时、预算、输入 artifact refs；result 必须含 argv/provider、开始/结束、退出码、stdout/stderr refs、取消状态、可解析的变更摘要。

Executor 的信任级别必须随每次 Run 持久化：`fake`、`sandboxed`、`host_trusted`。只有 `sandboxed`（容器或可验证 OS sandbox，白名单挂载 workspace、网络和凭据）可宣称最小权限。未沙箱化的 Codex/Pi/Claude Code 只能以显式 `--risk-accept host_trusted` 启动，报告必须标记它拥有启动用户的潜在权限；运行目录和环境变量白名单只能降低风险，不能构成安全隔离。M3 的默认自动化测试只使用 fake；真实宿主执行是用户手动 opt-in 的试验能力。

### 8.4 Apply

MVP 的 `apply` 只做“准备发布证据”，不 push。启用真实 push 的后续版本必须要求：workspace Harness passed、测试 Harness passed、相对 base 有提交、approval 绑定当前 commit hash、remote/branch 在 allowlist、显式用户确认。永不自动 merge。

## 9. Workflow 与业务阶段

### 9.1 阶段策略

| 阶段 | MVP | 输入 | 输出 | 外部依赖 |
| --- | --- | --- | --- | --- |
| project_init | M1 | 项目路径、配置 | config、目录 | 无 |
| task_init | M1 | task_id | task record | 无 |
| repo_sync | M2 | repo metadata | cache/metadata | 本地 Git/远端 Git |
| code_prepare | M2 | task/repo/base | workspace manifest | 本地 Git |
| code_generate | M3 | 明确开发指令、workspace 与可选现有 SDD | executor/evidence | fake 或本地 executor |
| srs_generate | M4 | PRD 文本/fixture | srs.md/report | fake/ModelPort |
| sdd_generate | M4 | SRS/repo evidence | sdd bundle/report | fake/ModelPort |
| unit_test_execute | M4 | Python/pytest test plan/workspace | reports/logs | pytest CommandRunner |
| code_apply_prepare | M3 | code evidence/approval | 本地交接包 | 无 |

### 9.2 典型闭环

```text
Task init
  → Repo sync
  → Workspace prepare + GitWorkspaceHarness
  → SRS/SDD（可用 fixture/fake 模型）
  → Code generate
  → CodeHarness + UnitTestHarness
  → Approval
  → Apply（默认仅生成交接包）
```

每一个箭头都是 Operation；每个实际执行是 Run；每个重要输出是 Artifact；每个质量判断是 HarnessReport。任一 stage 可以被替换为 disabled/fake/replay Adapter，但必须使状态与报告明确反映该事实。

## 10. CLI

### 10.1 CLI（M1–M4）

```text
yipilot init
yipilot task init --taskid <id>
yipilot task show --taskid <id>
yipilot task repo add --taskid <id> --url <url> --alias <alias> [--sync]
yipilot task repo sync --taskid <id> --alias <alias>
yipilot run list --taskid <id>
yipilot run show --taskid <id> --run-id <id>
yipilot srs generate --taskid <id> [--source file]
yipilot sdd generate --taskid <id>
yipilot code generate --taskid <id> --executor fake|codex_cli
yipilot harness run --taskid <id> --stage <stage>
yipilot harness show --taskid <id> --run-id <id>
yipilot code apply --taskid <id> --prepare-only
```

默认输出为人类可读摘要；`--output json` 供自动化。所有 mutating command 支持 request id/idempotency key；解析失败不得创建部分文件。

CLI 是当前唯一 transport。交互式确认采用标准输入/输出，但所有决定仍写成 Approval/Event；脚本化使用 `--output json` 与明确的 confirm token。HTTP、SSE、ACP、浏览器 UI 或远程身份认证不在当前实现中，也不进入核心运行时依赖；若后续启用，遵循第 16 节的 extension 设计。

### 10.2 实时事件流与终端呈现

CLI 需要实时呈现模型文字、工具进度、Harness 结论和人工确认提示，但这不是 HTTP SSE。Yipilot 的核心能力是 Run Event Stream：每个 Run 将严格递增的事件写入 `events.jsonl`，并由 `EventPublisherPort` 同步发布给当前进程的订阅者。CLI 是第一个订阅者；未来的 SSE/ACP 只是同一事件流的协议适配器，不能各自维护状态或生成第二份事件。

事件的最小合同如下：

```text
sequence        # 同一 run 内从 1 开始严格递增
event_id        # UUID
run_id/task_id/stage
timestamp       # UTC RFC 3339
type            # 受控枚举
visibility      # user | audit
payload         # 受 schema 与体积上限约束
```

第一版事件类型为：`run.started`、`stage.progress`、`model.delta`、`model.message_completed`、`tool.started`、`tool.output`、`tool.completed`、`harness.completed`、`approval.required`、`run.paused`、`run.succeeded`、`run.failed`、`run.cancelled`。`model.delta` 与 `tool.output` 必须先脱敏、截断并受总大小/频率预算限制；完整、可审计的最终产物另存为 Artifact，不能依赖 terminal 回显恢复事实。

CLI 行为：

```text
yipilot code generate ...              # 订阅当前 Run，实时渲染给人阅读
yipilot code generate ... --output jsonl  # 逐行输出同一事件 schema，供脚本消费
yipilot run watch --run-id <id> --after-sequence <n>
                                      # 从持久事件日志回放并继续跟随
```

人类模式可以使用终端进度/流式文本；`jsonl` 模式只能输出事件 JSON，不混入进度条、颜色码或日志。`run watch` 先从指定 cursor 回放已持久化事件，再以短轮询或平台文件通知跟随 JSONL 尾部；终态事件出现后正常退出。这样在单进程、跨 CLI 进程和未来 HTTP 客户端之间保持相同的 replay/cursor 语义。

## 11. 安全与可靠性

### 11.1 必须从 M1 执行的规则

- path guard 在所有文件、副作用和子进程边界执行；不使用 `shell=True`。
- 配置、日志、错误和 Harness evidence 统一脱敏 token、Authorization、常见密钥格式。
- 外部 URL、Git remote、输出大小、超时、并发数和可写路径全部配置化并有安全默认值。
- 取消会终止受控子进程；超时产生事件与诊断，不静默重试高风险副作用。
- 任意外部 Adapter 的 uncertain response 必须标记 uncertain，不能自行重复写请求。

### 11.2 分期韧性目标

M1–M3：单机文件锁、原子写、崩溃后保守失败、可人工重试。

M4：checkpoint 提示、审批陈旧检查和 fault injection。

HTTP/SSE、ACP、服务端认证、Postgres/Redis lease、多 worker、凭据轮换和负载演练不在当前实现范围；其接口与验收方向保留在第 16 节。实现前必须新增里程碑、fixture 和专项验收。

## 12. 测试与交付策略

| 层级 | 目的 | 默认是否执行 |
| --- | --- | --- |
| unit | 领域、schema、Harness checks、path/lock | 是 |
| contract | Port 与 fake/replay/disabled 一致性 | 是 |
| integration | 临时目录、临时 Git repo、CLI | 是 |
| fault | kill/rename/lock/timeout 注入 | 夜间或显式 |
| live | 真模型、真实 Git host、真实外部系统 | 否，显式 opt-in |

所有普通测试必须无需 API key、公司网络或真实仓库。live 测试必须有单独 marker、预算、允许目标和清理策略。每项 Bug 修复加入回归 fixture；每个扩展依赖加入禁用态测试。

## 13. 分期交付计划

### M0：项目治理（约 5 项）

- Git、`.gitignore`、Python packaging、锁文件、测试命令、pre-commit/CI。
- `CONTEXT.md`、ADR 模板、贡献与安全策略。
- 最小 `yipilot --version` 与 fake adapter 测试基线。

### M1：确定性本地控制面（约 10 项）

- Project/Task/Operation/Run/Event/Artifact 模型和状态机。
- 文件 Store、原子写、锁、权限、恢复扫描。
- CLI init/task/run/harness 查询；TaskIntegrityHarness。

### M2：Git 隔离与证据（约 8 项）

- Repo cache、sync、worktree、workspace manifest。
- 受控 Git runner、四件套 evidence、GitWorkspaceHarness。
- 临时 Git repo 的并发 Task 与主目录不变测试。

### M3：受控 Agent 执行（约 7 项）

- ExecutorPort、fake executor、codex adapter（可选启用）。
- tool-run record、审批模型、PathGuard、CodeHarness。
- 基于明确开发指令的 `code generate` 与 prepare-only Apply 交接包；它不依赖尚未生成的 SDD。

### M4：研发工件与质量闭环（约 10 项）

- PRD fixture input、SRS/SDD artifact、ModelPort fake/replay。
- SRS/SDD Harness v1、Python/pytest test plan、UnitTestHarness。
- input fingerprint、新鲜度、report schema/golden tests。

总量约 40 个当前可验收交付项；每项必须有范围、非目标、依赖、Harness/测试证据和 DoD。M0–M3 是不依赖模型或公司环境的本地控制面 MVP；M4 是首个研发工件闭环。服务化、ACP、Bugfix、企业 Git Provider、迁移、多 worker 和生产化属于未承诺的可选扩展，而非当前交付范围。

## 14. 决策与待确认项

1. 首个真实 executor 选 Codex CLI；Pi/Claude Code 以后以同一 Port 接入。
2. 当前 CLI 项目采用文件 Store；Postgres/Redis 不在本规格的评估范围内。
3. M4 的 SDD 语义检查以 feature flag 发布，避免伪称与 Go 行为相同。
4. `apply` 在未明确提供远端仓库、用户身份和审批策略前固定为 `--prepare-only`。
5. 公司私有协议仅在获得脱敏 schema、fixture 和明确授权后提升兼容等级。

### 14.1 崩溃后的恢复边界

文件 Store 支持 C3 的范围仅限于 Yipilot 自己持久化的 Task、Operation、Run、Event、Approval 和已完整写入的 Artifact。若进程在纯本地、无副作用的步骤中断，恢复可从最后一个验证通过的 checkpoint 提示创建新的 Run。若进程在模型调用、Git 写操作、测试子进程或网络请求中中断，旧 Run 必须标记 `execution_interrupted`，不得从中间命令盲目续跑；Application service 先验证可用 evidence，随后创建一个新的、可审计的 retry Run。只有未来接入具备事务性/幂等证明的 Adapter 后，才可为该 Adapter 声明更强的 resumable 行为。

## 15. 规格评审检查表

- [ ] 所有声称 C2/C3 的能力都有相应 fixture 与测试。
- [ ] 所有外部系统都有 fake、replay、disabled、production 的处理说明。
- [ ] 每个写路径都有原子性、锁、权限与崩溃后结果定义。
- [ ] 每个 Harness 检查有稳定 ID、输入、状态、证据和测试。
- [ ] 每个 agent/executor 都有路径、命令、网络、审批与日志脱敏策略。
- [ ] 任何 `skipped` 均不会被 UI、CLI、API 或 Apply 当作通过。
- [ ] 不存在“自动 push/merge”“读取任意路径”“恢复未知副作用”的隐含行为。
- [ ] 计划以 M0–M4 为独立可验收范围，不依赖公司环境或服务端组件。

## 16. 可选扩展路线（不构成当前承诺）

本节保留未来可能需要的边界，避免现在的 CLI 领域模型阻断后续演进。它们均不进入 M0–M4 的依赖、测试或完成定义。

### 16.1 Server extra：FastAPI、REST 与 SSE

若需要本地或远程 API，选择 FastAPI 作为 `yipilot[server]` 的首选框架：它以 Python 类型和 Pydantic 模型定义请求/响应，并可自动生成 OpenAPI/JSON Schema 文档。REST 只调用现有 Application service；写操作要求 `Idempotency-Key`。SSE endpoint 只把既有 Event Stream 转成 `text/event-stream`，以 event `id` 映射 `sequence`、以 `Last-Event-ID` 映射 `after_sequence`，先 replay 后 follow；它不创建、修改或重排事件。CLI Core 不可导入 FastAPI，禁止让 HTTP DTO 进入 Domain。

### 16.2 ACP、Human Review 与多 worker

ACP 只能映射到既有 Operation/Run/Event；不可建立第二状态机。Human Review 必须使用不可变 transcript、candidate/diff、Harness 和 hash-CAS approval。多 worker 仅在文件锁不足以支持目标部署时考虑，并需先为 lease、队列、数据库与恢复建立专项设计和故障注入测试。

### 16.3 企业 Adapter 与 Bugfix

Git provider、MCP、Wiki、BugTask、企业身份系统均通过 Port 接入。它们必须先具备脱敏 schema、fake、recorded replay、disabled 状态和生产授权，再被添加为独立 stage；任何缺少这些输入的集成都保持 disabled。

## 17. 框架决策

| 层 | 当前选择 | 原因 | 不选择/延后 |
| --- | --- | --- | --- |
| CLI | Typer | 类型标注驱动的命令、参数、帮助与 shell completion，适合命令树 | 不手写 argparse；不引入 GUI |
| Schema | Pydantic v2 | 配置、CLI DTO、持久化记录和 HarnessReport 的严格校验；可导出 JSON Schema | Domain 规则仍保持普通 Python，不让 Pydantic 侵入所有业务行为 |
| 配置 | PyYAML + Pydantic 校验 | 人类可编辑 YAML，加载后严格校验/版本化 | 不直接把 dict 当配置事实源 |
| 持久化 | `pathlib` + 原子 JSON/YAML/JSONL + 文件锁 | 单用户、本地审计、可直接检查与归档 | 当前不引入 ORM、Postgres、Redis |
| Git/子进程 | stdlib `subprocess` argv + 自有 Port | 命令、cwd、超时、环境与证据完全可控 | 不让 Agent 传入 shell 字符串 |
| 测试 | pytest + Hypothesis | Yipilot 和受管项目首版只支持 Python；断言、fixture、property test 统一 | 不支持 Go/Node/Java 或通用任意命令 runner |
| 工作流 | 自定义 StageRunner/状态机 | M0–M4 阶段固定且需精确审计、重试与 Harness gate | LangGraph 延后为可选 graph runtime |
| API extension | FastAPI + Uvicorn extra | 与 Pydantic 类型/JSON Schema 复用，未来 OpenAPI、依赖注入和测试路径清晰 | 当前不安装；不使用 Django/Flask |

LangGraph 若在未来引入，只能用于某个 stage 内部的图执行。其节点在恢复时可能从节点开头重新运行，因此每个含副作用的节点仍必须使用 Yipilot 的 Operation、idempotency key、evidence 与 Approval 边界；LangGraph checkpoint 永远不是业务事实源。
