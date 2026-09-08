# Yipilot V2：自动研发交付架构设计

版本：2.0 / 2026-09-07。状态：**Proposed，完整设计基线；现有能力的入口与职责收敛已部分实施**。

产品范围与输入输出以 [产品定义](../../product/vision.md) 为准；当前代码能力以 [状态页](../../product/status.md) 为准。本文件中的 SQLite、目录拆分、首个 Git host 等是技术提案，不是用户原始要求或已完成实现。旧 M0–M8 规格只提供历史依据，不与本方案并列指导实施。

本文承接用户最新目标：给出项目目录和需求，自动形成需求与设计、经 Agent 评审、实现、验证、发布分支/PR，并收集配置的 CI 结果后交接。用户已明确取消自动合并；合并不属于本项目的执行职责。本方案不声称现有代码已经具备这些能力。

阅读入口：[架构图](../../architecture/yipilot-v2.html) · [现状分析](../../research/2026-09-07-yipilot-architecture-assessment.md) · [代码缺口](../../research/2026-09-07-yipilot-code-gaps.md) · [Pi 参考](../../research/2026-09-07-pi-architecture-reference.md) · [持久化与远端交付依据](../../research/2026-09-07-durable-delivery-design.md)。本设计中的Operation始终指一次用户工作流意图；研究材料中泛指副作用的operation在本文统一称ExternalAction。

## 本轮已实施范围（2026-09-07）

按 [第一轮执行合同](../plans/2026-09-07-responsibility-refactor.md)，本轮保留 application/runtime/domain/infrastructure/harness 包名：新增 ProjectApplication 公共入口与装配，RunExecutor 管理现有模型 Run 生命周期；终端提交批准意图，业务层绑定并保存；工作区实际能力迁入 workspace，共用路径保护归 security。已删除三份旧 application facade，CLI/交互共用生成路径，应用/验证/有限修复可无 stdin 调用。

本段描述当前实现，下面的完整目录、SQLite、持久 respond/resume、多轮 Agent、需求/设计语义评审和 PR/CI 仍是目标。确认回调并非 durable WaitItem，代码仍以现有文件存储和单次提案运行。参见 [实际依赖、验证与限制](../../architecture/responsibility-refactor.md)。

## 1. 产品定义与首版边界

Yipilot 是面向 Python 仓库的本地自动研发交付控制器。Agent 负责理解与执行，确定性工作流负责阶段推进、验收和交付授权。系统保留可解释的需求、设计、审查、代码、测试与远端结果，不依赖某条聊天中的“已完成”。

首版支持：一个本机项目、一个目标 Git 仓库、一个任务分支、同一时间一个活动阶段；Python 3.11+；首个受控执行环境为现有 Docker Desktop Linux worker；首个模型 Adapter 为现有 OpenAI-compatible 协议升级版；首个代码托管 Adapter 为 GitHub，CI 可消费 GitHub required checks，Jenkins 是可选的外部结果 Adapter。没有 Jenkins 也能使用已配置的 CI。

不预先实现：多仓库分布式事务、多租户、远程 worker 集群、任意语言执行平台、可视化流程编辑器、动态插件市场。目录中标记“交付阶段新增”的文件只在对应功能实施时创建。

人只需在开始时提供目标、必要信息和授权范围；常规读写测试自动执行。真正歧义、越界、预算耗尽、外部状态不确定或策略要求的人审，才产生可恢复的等待项。首次模型/仓库/环境配置可交互完成，自动化任务不能靠反复 stdin 确认推进。

### 产品完成定义

默认交付终点为 review_ready：任务分支已推送、PR/MR 已创建、当前提交通过本地验证及已配置的必需 CI 检查，并形成交接报告。没有配置 CI 时明确记录 not_configured，不宣称 CI 通过。仅设计、仅验证模式各有完成标准。系统不执行合并，也不等待人工合并来标记任务完成。

## 2. 从成熟 Agent 设计吸收什么

| 参考 | 采用的设计思想 | 结合本业务的调整 |
| --- | --- | --- |
| Pi：模型、agent-core、编码会话与 UI | 通用模型/工具循环；完整历史与本轮模型上下文分离；多入口共用可编程会话 | 外加研发阶段、质量 gate 和交付策略；不复制 TypeScript 运行时或大型 Session 聚合 |
| Pi 工具 operations | 工具参数/结果与底层执行环境分离 | 工具绑定可信策略和 Docker 工作区，默认不提供宿主任意 shell |
| OpenHands SDK | Agent、Conversation、Workspace 分工 | 单阶段 Session 依附 Run；不因会话管理引入服务端或独立会话产品 |
| mini-SWE-agent | 简单、可观察的模型→动作→结果循环 | 保持核心小，格式错误可有限反馈，代码修复由上层控制总预算 |
| LangGraph | 阶段持久化、可中断执行的思路 | 当前固定流程用普通 Python 实现；checkpoint 不赋予外部副作用“恰好一次”语义 |

事实来源：[Pi core](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)、[Pi SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)、[Pi 工具替换](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md#remote-execution)、[OpenHands 架构](https://docs.openhands.dev/sdk/arch/overview)、[mini 执行循环](https://mini-swe-agent.com/latest/advanced/control_flow/)、[LangGraph 中断](https://docs.langchain.com/oss/python/langgraph/interrupts)。表格第三列是本方案设计，不是这些项目现有功能声明。

## 3. 七条架构原则

1. **以变化原因划分模块。** 更换模型只改模型 Adapter；更换执行环境只改 Workspace；新增 CI 不改 AgentLoop；新增入口不复制业务决策。
2. **两种循环，两个明确职责。** Workflow 做阶段/返工/等待循环；AgentLoop 做单阶段模型/工具反馈循环。只有 Workflow 可以提交业务阶段成功并推进。
3. **先冻结输入，再执行。** 需求版本、源代码基线、设计与评审 refs、模型设置、项目 profile、授权策略进入输入指纹；“最新结果”不作为隐式业务依赖。
4. **安全在实际执行处落实。** 模型只提出动作；策略引擎计算许可；执行环境复核许可与输入；输出回到可信侧校验。禁止用模型文本或 agent 自签 Approval 放行。
5. **少接口，深实现。** 一项能力对外有小接口，内部可以多个文件。真实替换点保留 Protocol；每个文件 Store、每个阶段不单独造抽象工厂。
6. **事务保存业务事实，文件保存大内容。** 状态推进、幂等、审批与耐久事件在一次本地事务内更新。模型 prompt、日志、diff 和报告正文用不可变工件保存。
7. **完成必须基于证据。** 模型结束不等于阶段通过；命令返回 0 不等于需求验收；PR 创建请求成功发送不等于远端已确认创建。

## 4. 模块与单向依赖

```text
transport ──→ app（装配与公共入口）──→ workflow
                                      ├──→ agent ──→ models
                                      │      └──→ 注入的 ToolSet
                                      ├──→ workspace（环境与可信输出）
                                      └──→ delivery（Git host / CI）

persistence 实现 workflow/agent 所需的存储与事件接口，由 app 注入。
contracts / security 为少量共享叶子模块，不导入上述业务模块。
```

箭头表示源码依赖，不是所有运行时消息。Workflow 不 import persistence 的具体 SQLite 类；Agent 不 import workflow、transport、Workspace 实现或 TaskStore；Workspace 不 import workflow 或 agent session；models 不知道 Task/Operation/审批。Adapter 可 import 它实现的协议和拥有类型的模块，反方向不得依赖具体 Adapter。

同一流程的数据流：公共入口 → Workflow 冻结阶段输入 → Session 运行 → 工具 Gateway 经策略执行 → Workspace 回传结果 → 阶段校验 → 原子记录 StageResult 与事件 → Workflow 决定下一步。

### 模块职责表

| 模块 | 拥有的事实/责任 | 明确禁止 |
| --- | --- | --- |
| app | 配置加载、对象装配、进程启动；面向入口的稳定 facade | 写阶段规则、维护第二套任务状态 |
| workflow | Task/Operation/Run、阶段结果、业务 gate、策略决策、重试与恢复 | 拼 Docker 命令、解析供应商 SSE、直接读终端 |
| agent | 本轮消息、工具调用配对、上下文、预算、单阶段循环 | 判断 PR 是否可合并、自行批准、解析所有阶段业务 schema |
| models | Provider 请求/响应、真实流、usage、finish reason、取消/错误分类 | 查业务数据库、生成批准、选取业务阶段 |
| workspace | 源码快照、隔离执行、编辑前置条件、输出校验、工作区清理 | 改任务完成状态、持有 Git host token、决定继续哪个阶段 |
| delivery | commit/push/PR/CI 的机械执行与结果查询 | 自行决定满足业务验收、自主发起新阶段 |
| persistence | 事务状态、工件、事件、迁移与备份 | 触发模型、执行业务重试、根据文件名猜阶段 |
| transport | 命令解析、输入、渲染、订阅与退出码 | 构造 Approval、选择有效结果、直接驱动多个底层 service |

## 5. 代码与文件分级

2026-09-08 已完成现有代码的第一步目录归位，见 [当前代码布局](../../architecture/code-layout.md)。下方仍是完整目标结构；当前保留 domain/harness 和现有服务文件，不代表 SQLite、stages 或 delivery 已经实现。

目录是目标，不是一次性脚手架。模块内部优先普通函数、dataclass/Pydantic 和组合，不为相同逻辑创建 N 个角色子类。

```text
src/yipilot/
  __init__.py
  app.py                         # 唯一 composition root；创建 ProjectApplication
  contracts.py                   # ArtifactRef、CorrelationId 等极少共享值类型

  workflow/
    service.py                   # start/advance/resume/cancel/status；唯一业务入口
    types.py                     # Task/Operation/Run、StageInput/Result、WaitReason
    ports.py                     # WorkflowStore、ArtifactWriter、交付查询等必要接口
    runner.py                    # 一次阶段执行的生命周期、预算与事务提交
    transitions.py               # 固定阶段转移和失效规则；纯函数
    policy.py                    # 授权范围、PolicyDecision、人审条件、发布 gate
    recovery.py                  # 活跃执行判别、遗留动作对账与新 Run 重试
    project_profile.py           # Python/依赖/验证/路径/交付 profile 与版本
    stages/
      specification.py           # 需求/验收标准/设计及有限修订
      review.py                  # 独立上下文的需求、设计与代码评审
      implementation.py          # 实现、基于诊断修复；共用同一执行入口
      verification.py            # 环境选择、固定检查、结构化测试与报告
      publication.py             # 交付阶段新增：提交、PR、CI 等待与交接
    prompts/
      requirements.md
      design.md
      review.md
      implement.md               # 提示模板是版本化产品资产，不是执行权限

  agent/
    session.py                   # 一次 Run 内的会话控制，薄组合
    loop.py                      # 模型→工具→观察；不含 PatchProposal 业务
    types.py                     # AgentInput/Outcome、Message、ToolCall/Result
    context.py                   # 选择/压缩本轮上下文；历史到模型输入转换
    budget.py                    # turn/token/bytes/deadline 的本次执行核算
    ports.py                     # TranscriptSink、Tool、ContextReader 等小接口
    tools.py                     # read/search/edit/run_check 等 schema 与工具包装

  models/
    types.py                     # Provider-neutral 消息/事件/limits/usage
    client.py                    # ModelClient Protocol；统一流语义
    openai_compatible.py          # 沿用 httpx 与现有协议代码，修正流与单位
    registry.py                  # 已有模型选择/connection/profile 迁移逻辑
    secrets.py                   # 已有 secret backend；与项目工件分离

  workspace/
    manager.py                   # prepare/execute/snapshot/close 的公共能力
    types.py                     # WorkspaceHandle、SourceManifest、ProcessResult
    source.py                    # Git 对象导出、文本/二进制分类、稳定快照
    edits.py                     # hash/CAS 文本编辑、PatchProposal 兼容应用
    tools.py                     # 实现 agent 工具所需的受控 operations
    policy.py                    # 执行时核对可信许可、路径/命令/网络限制
    docker.py                    # 现有 Docker worker 的环境实现
    output.py                    # 停止 worker 后，可信侧重算输出与 diff
    git.py                       # 仅本地 cache/worktree/index/Git 证据
    environment.py               # 依赖锁与镜像准备，不混进代码 Agent loop

  delivery/                      # 到交付里程碑才增加
    types.py                     # Candidate、PullRequestRef、CheckResult、Receipt
    ports.py                     # GitHost / CiBackend
    git_publish.py               # 受控 commit/push、本地提交与远端 SHA 对账
    github.py                    # PR/checks/review；带条件写入
    jenkins.py                   # 可选：trigger/queue/build/result 适配

  persistence/
    database.py                  # sqlite3 连接、事务、受控查询
    workflow_store.py            # 请求预留、Run 开始/结束、checkpoint 原子操作
    artifacts.py                 # 内容寻址文件工件、校验、引用关系
    events.py                    # DB 事件 cursor + 导出 JSONL + 订阅适配
    migrations.py               # schema 版本迁移与 v1 单向导入
    backup.py                    # 一致性备份、归档/回收，后续按需要落地

  security/
    paths.py                     # 公共路径保护；不从别处 import _plain_path
    redaction.py                 # 保留脱敏；业务内容与显示文本区分

  transport/cli/
    app.py                       # Typer 命令树，只调用 app facade
    interactive.py               # 模型/任务配置与输入，不内嵌业务流程
    events.py                    # 同一事件合同的人类/JSONL 渲染

tests/
  unit/                          # 转移/失效/预算/输入规则、输出解析
  contract/                      # ModelClient、Tool、Workspace、GitHost 合同
  integration/                   # SQLite+工件+真实 service 的主线组合
  recovery/                      # 阶段/外部动作 failpoint 和重复请求
  acceptance/                    # 用户完整路径；假模型同生产代码路径
  live/                          # 显式真模型、Docker、Git host 验收
```

边界用一项 AST import 检查保护：agent/models 禁止导入 workflow/transport/persistence 具体类；workspace 禁止导入 workflow；transport 除装配与纯显示类型外不导入底层实现；跨模块禁止 import 私有 helper。对协议/类型的必要依赖明确白名单，不以循环 import 延后加载掩盖问题。

## 6. 最小领域模型与所有权

| 记录 | 含义与关键字段 | 所有者 |
| --- | --- | --- |
| ProjectProfile | repo、Python、image/lock、checks、path scope、delivery policy、revision | workflow |
| Task | 用户需求单元；task_id/title、需求修订引用；任务结果是各 Operation 的投影 | workflow |
| Operation | 一次“按某需求版本完成某目标”的持久请求；target、policy、checkpoint、status/version | workflow |
| Run | 某阶段的一次尝试；operation_id、stage、attempt、input_fingerprint、status/outcome | workflow |
| Artifact | 不可变正文及 metadata；内容 hash、媒体类型、producer、输入/来源 refs | contracts + persistence |
| ReviewReport | 被审工件/代码 hash、阻塞 findings、证据、建议；不是授权 | workflow |
| PolicyDecision | allow/deny/require_input、授权来源、scope/version、实际动作目标 | workflow |
| HumanApproval | 人工身份/决定/时间、准确目标 fingerprint、有效范围；不能由 agent 签发 | workflow |
| ToolExecution | 一个工具调用的 started/result/uncertain 事实、输入hash与结果ref | agent + persistence |
| ExternalAction | push/PR/CI trigger 等写动作意图、幂等键、目标、执行状态与远端receipt | workflow，delivery 执行 |
| Event | 全项目递增 cursor、correlation、类型、版本、允许的 payload/ref | contracts + persistence |

不新增独立 SessionId/Job/PipelineRun 与 Operation/Run 重叠。AgentSession 是 Run 内对象；message_id/tool_call_id 用于消息关系。CorrelationId 可跨层传递，但底层不按 ID 查业务授权。

### StageInput 与 StageResult

StageInput 是冻结值：operation/run/stage、requirement revision、base snapshot、输入 artifact refs、resolved model、prompt version、profile/policy revision、预算预留。schema 校验和权限校验完成后才开始调用模型。

StageResult 区分 passed、needs_revision、needs_input、failed、cancelled；附 outputs、report refs、diagnostic class、消耗与代码 snapshot。Run succeeded 表示该次执行正确产出了有效结果，结果仍可能为 needs_revision；Workflow 只在 gate 通过后接受结果并推进。不能用一个布尔 harness 代替所有结果语义。

## 7. 业务流程及放行条件

默认完整路径：

```text
PREPARE → SPECIFY → REQUIREMENTS_REVIEW → DESIGN → DESIGN_REVIEW → IMPLEMENT
IMPLEMENT → VERIFY → CODE_REVIEW → PUBLISH → WAIT_CI → COMPLETE
需求评审失败 → SPECIFY；设计评审失败 → DESIGN
测试/代码审阅/CI 代码失败 → IMPLEMENT → 重验、重审
任意阶段 → WAIT_INPUT / FAILED / CANCELLED（按具体原因）
```

图中修复是回到同一 implementation 能力的新 Run，不创建另一套 Repair Runtime。设计修订和代码修复各有独立预算，所有尝试还受 Operation 总预算约束。

| 阶段 | 输入 | 必须产出 | 下一步前的 gate |
| --- | --- | --- | --- |
| PREPARE | repo目录、目标branch、需求、profile | 冻结 base、repo inventory、环境说明 | repo/权限/预算可用；主工作区未被修改 |
| SPECIFY | 原需求或已有PRD、必要仓库事实 | requirements.md + requirements.json | 需求ID、范围、验收标准；阻塞歧义已解决 |
| REQUIREMENTS_REVIEW | 冻结SRS、来源、代码证据 | ReviewReport | 无阻塞歧义/冲突/遗漏；验收可验证，否则回到SPECIFY修订或等待输入 |
| DESIGN | 已通过需求评审的SRS、只读仓库检索 | design.md + design.json，含实施步骤/测试映射 | 每个需求有设计或明确不适用理由 |
| DESIGN_REVIEW | 固定需求/设计/证据 | ReviewReport | 无 blocking finding；审查引用匹配当前版本 |
| IMPLEMENT | 已接受设计、base或修复snapshot | 代码与测试修改、change summary、snapshot | 工具许可、输出完整性和范围校验 |
| VERIFY | 候选snapshot、冻结环境/checks | 结构化测试/静态检查结果、日志refs | 必需测试实际执行、检查通过、受测内容匹配 |
| CODE_REVIEW | 同一snapshot、需求设计、验证报告 | ReviewReport | 无blocking，检查修改是否实现需求、是否削弱测试 |
| PUBLISH | 已通过候选、可信目标、策略 | commit/tree SHA、push receipt、PR ref | commit tree与受测snapshot匹配；远端head核实 |
| WAIT_CI | PR、expected head、required checks | 可信CI/check/review refs | 已配置时当前head所有必需检查通过；未配置明确记录not_configured；显式配置的人审满足 |
| COMPLETE | PR ref、当前head、验证/CI refs、需求版本 | review_ready 交接报告 | 发布事实完整；配置的必需检查通过；无需等待合并 |

### 需求来源与规范化路线

当前任务明确只涉及一个仓库。需求入口先支持直接文本、本地 Markdown/TXT，以及二者组合；后续按实际需求增加文档格式和外部来源适配，不预建通用插件框架。

原规划书第 2.5 节描述的是“从 MCP/RFC/Wiki 获取 PRD → 生成并澄清 SRS → 结合代码生成 SDD”。这是规划书对旧项目的描述，尚不代表已直接核实原 Go 源码。V2 保留来源到需求再到设计的关系，不要求用户预先写好正式 PRD。

1. PREPARE 接收材料：保存原始文本/文件快照、来源位置、内容哈希和材料用途（本次要求、背景参考、现状证据）。文件名不决定权威级别；来源内容不是执行授权。
2. SPECIFY 结合受限的只读仓库检索，形成目标、范围与非目标、使用场景、行为规则、约束和可验证验收标准。每条需求保留 source ref/段落定位；模型新增内容标明建议或假设，不能伪装成用户原话。
3. 有影响范围、数据或行为正确性的阻塞歧义时集中澄清；技术实现细节留到 DESIGN。用户本次明确要求优先于历史参考材料，材料冲突不能靠模型静默选择。
4. REQUIREMENTS_REVIEW 放行：程序检查字段、ID、引用和未决阻塞项；Agent 检查语义冲突、遗漏和可验证性。检查结论与冻结需求版本绑定，复用通用评审实现，不增加独立评审 Runtime。一般任务自动放行，有关键歧义才等待用户。
5. DESIGN 消费已接受需求和代码证据；后续保持“来源 → 需求 ID → 设计/实施步骤 → 验证证据”的追踪。新增来源或用户修订产生新版本，原材料和历史结果保留。

实施顺序：先跑通文本/本地文件到 requirements 的闭环；再补来源追踪、澄清恢复和版本失效验收；最后按真实使用接网页/Issue/企业文档/MCP。外部适配只负责获取和规范化材料，不能直接改需求结论或推进阶段。

### Bugfix 复用同一业务流程

当前不单独建设 Bugfix 子系统。普通任务可以描述“修复某个问题”，仍走需求整理/评审 → 设计/评审 → 实现 → 验证/代码评审 → 交付。缺陷任务在需求中记录实际行为、预期行为、复现条件和日志证据；设计中记录诊断、原因假设与修复方案；验证优先提供修复前失败、修复后通过的回归证据。无法复现时明确不确定性，需要时停留在分析或澄清，不声称已确认根因。

小修复使用简短需求/设计，不强制扩写产品长文。以后接 BugTask/Issue 只新增输入适配；只有出现无法由现有流程表达的实际生命周期要求时，才重新评估专用流程。原规划书中的独立 Bugfix 还承担外部 BugTask 查询、关联 MR/分支选择等职责，不直接照搬到当前单仓库产品。

### 文档产物与复杂度

默认只要求一份软件需求与验收（SRS，requirements）、一份技术设计（SDD，design）；不强制把相同内容扩写成 PRD/SRS/SDD 三套长文。JSON 是关联/验收依据，Markdown 是可读视图，均来自同一规范化模型；人工修改 Markdown 必须显式导入成新版本并重验，不能与 JSON 暗中分叉。已有 PRD 保留来源，缺什么补什么。

小任务设计可以很短，但不能省掉目标、影响范围、验证方法。复杂任务在 design 内形成有限实施步骤，先顺序执行；没有真实需求前不加入通用图编辑器或多 agent 并行。

### 失效与返工

- 需求变化：创建新 revision；旧设计、评审和依赖结果不再自动放行新需求。旧Operation标记输入已过期并阻止发布；若确需继续旧需求，必须显式保留为独立任务目标，不能和当前Task最新revision暗中混用。
- 设计变化：重新评审；已写代码作为候选保留，需要按新设计验证。
- 代码snapshot变化：先前测试与代码审阅失效；不得将旧绿灯转给新commit。
- 仅提交说明变化但tree未变：本地内容验证可按明确规则复用，远端CI仍按新SHA重新判定。
- 模型变更：已有证据保留其冻结模型，不静默替换；新调用明确新配置，阶段输入重新指纹化。
- profile/策略变化：新增授权不能回填旧执行；新的放行重新评估。撤销权限立即阻止后续动作。
- target branch前移：按仓库保护/集成策略重新检查；需要rebase时用新候选并重新验证，不自动force push覆盖他人提交。

## 8. Agent 核心的详细合同

### 一次会话如何执行

1. Session 接受已冻结 AgentInput、已绑定工具集、事件/历史sink、预算、取消句柄；不自己选择repo或授予工具权限。
2. Context 选择允许的文件/工件与相关历史；先裁剪/压缩，再转换成Provider消息；可信系统规则与仓库文本明确区分。
3. ModelClient 真正逐块返回 text/tool-call/usage/finish/error 事件；收到首个delta即可显示。
4. 工具参数完整且schema有效后才能调用。输出长度截断或残缺JSON绝不触发副作用。
5. 工具结果以原tool_call_id回填模型上下文；可修正的工具错误作为观察，权限和基础设施错误按结构化类别处理。
6. 模型完成后返回 AgentOutcome，业务阶段再校验目标schema与工件。失败格式只允许小预算的纠正回合。

首版同步worker、顺序工具执行即可；ModelClient 返回 Iterator[ModelEvent]，用显式cancel/deadline贯通现有httpx与进程执行，不为了流式同时重写为全栈async。交互输入与执行线程/进程分离，禁止主线程同步等待导致无法取消。

### 三层数据必须分开

- 原始执行证据：日志/正式模型输出/工具结果，按权限和保留策略存储。
- 会话历史：有序消息与调用关系，不等于全部业务记录。
- 本轮模型上下文：获准、相关、受预算约束的一部分；压缩摘要是派生数据，不能证明阶段放行。

ModelRequest 只含Provider需要的消息、模型选项、工具schema、limits及trace id；不要求其理解TaskStore、同Run Markdown限制或HumanApproval。业务工件在Context层校验后转成模型内容。

模型能力在启动时显式核对。首个Adapter优先使用供应商原生tool-call；仅有chat能力的兼容端点，可以配置明确的结构化Action输出协议并经同一schema校验，不静默假装支持原生工具。两者都归一到ToolCall，业务阶段不分叉；完全不支持可靠结构化动作的模型只能用于允许的纯文档阶段。

### 角色配置而非角色框架

需求作者、设计者、评审者、实现者共享Session，差异是prompt版本、输入选择、工具许可、输出schema。评审用新上下文，读取作者成果而非整段作者对话；第一版可复用同模型，不宣称同模型独立评审能保证正确。确定性gate与测试仍不可省。

### 预算与错误

独立计量 input/output tokens、bytes、turns、墙钟和模型调用次数；Provider提供usage时记录实际值，缺失时记录unknown并以本地限额控制，不能伪造0消耗。全任务硬费用上限只有在可预估/预留并核对usage时才能宣称；否则明确是近似费用预算。

三类重试分开：Provider临时失败的有限退避；设计/代码质量返工；远端写操作结果对账。认证/权限错误不重试生成，取消不映射为PLANNER_FAILED，上下文溢出走上下文收缩而非网络重试。预算先原子预留，已有request重放先查原记录，不重新累计attempt。

## 9. 工具、工作区与依赖环境

### 首批工具

read_file（范围/字节限制）、list_files、search_text、edit_file（expected hash）、write_new_file（路径不存在前置条件）、run_check（可信check id）。必要受控命令通过固定argv结构注册；没有通用“任意宿主shell”。模型工具调用中不出现可伪造的批准ID或自选policy revision。

工具给模型返回短content，给系统返回结构化details与完整工件ref。日志裁剪明确标注，允许获准的分段读取。失败时保留原文件，不能把编辑冲突当成功。二进制资源可以受限保留/hash校验，文本编辑权限与资源存在权限分开。

显示脱敏不能悄悄改变待应用补丁或其他机器执行内容。原始候选按受控权限保存并以原始bytes计算hash；若候选包含不允许存储/发布的敏感内容，应明确拒绝或要求重新生成，再形成新版本。审计显示可脱敏，但必须关联原始受控工件，而非把被改写的文本当原模型结果。

### 工作区生命周期

PREPARE 从Git对象冻结提交，主checkout保持原状。默认要求已提交基线；若用户选择包含未提交内容，显式导入为内容snapshot并记录base+overlay，绝不把用户主目录作为执行区。

WorkspaceManager 内部保留可信baseline、隔离execution和输出校验。一个Operation有一个逻辑候选，物理目录按尝试/阶段隔离；避免无界复制和“每次从旧base重建却丢掉累积修改”。成功阶段生成不可变snapshot；失败后可保留诊断候选，下一次从明确snapshot开始。

验证在候选snapshot副本执行，输出/缓存进单独临时区；不允许测试修改待发布内容。副作用执行结束后须确认进程/容器已停止，再在可信侧重新枚举、hash、验证diff。源码快照、Git tree和实际publish tree必须有可检查的映射，不能只存一份描述性summary。

批准的Docker输出进入任务worktree并生成本地Git证据；现有严格路径、链接/重解析点拒绝、挂载验证、资源预算、停止后输出检查继续使用。源码工具和验证工具可共用Workspace执行能力，但业务验收分别计算。

### 依赖构建与验证分离

ProjectProfile 定义Python版本、包管理/锁文件、源码路径、测试路径、固定check集合及镜像来源。只自动识别已支持的模式；无法可靠识别时请求一次profile配置，不猜任意命令。

环境准备可以使用经过项目授权的网络，在隔离build环境安装锁定依赖；依赖安装视为执行不可信代码，不在控制器主机执行。结果固定image digest与environment fingerprint；测试环境默认network=none且不注入Provider/Git凭据。已有预制镜像可直接登记，但必须验证它满足项目profile。

环境fingerprint至少覆盖base image、Python、锁文件内容、安装步骤版本、check工具版本/配置。依赖变化使验证证据失效。当前项目Python3.11本身不约束受管项目可用Python版本，具体支持范围由profile明确。

### ProjectProfile 示例

以下是拟定schema示意，路径、镜像digest、检查命令需在接入时验证并冻结，不能直接作为当前CLI配置使用：

```yaml
schema_version: 2
profile_id: python-service
repository:
  base_branch: main
  include_uncommitted: false
environment:
  python: '3.11'
  dependency_lock: uv.lock
  image_digest: '<已验证镜像的 sha256 digest>'
  test_network: none
scope:
  read: ['src/**', 'tests/**', 'docs/**', 'pyproject.toml', 'uv.lock']
  write: ['src/**', 'tests/**', 'docs/**']
checks:
  - id: unit
    argv: ['python', '-I', '-B', '-m', 'pytest', '--junitxml=/tmp/results/unit.xml']
    parser: junit
    required: true
    require_executed_tests: true
  - id: lint
    argv: ['ruff', 'check', 'src', 'tests']
    parser: exit_code
    required: true
budgets:
  max_model_calls: 40
  max_agent_turns_per_run: 12
  max_code_repairs: 2
  max_design_revisions: 2
  execution_seconds: 3600
  ci_wait_seconds: 21600
delivery:
  git_host: github
  target: review_ready
  human_review: on_exception
  allow_push: true
  required_checks: ['project-ci']
```

示例值是首轮评估起点，不是未经测试的推荐预算。执行时间与CI等待时间分开；检查XML在worker停止后由受控输出通道取回并绑定被测snapshot，不能因为位于/tmp就绕过大小、格式和来源校验。scope作用于具体规范化路径而非简单字符串前缀。Agent修改profile文件不改变当前可信配置，后续profile变更需新授权/新版本。

## 10. 授权与质量是两件事

任务开始固化授权：repo/target、允许读写路径、工具与网络范围、模型发送范围、预算、是否允许push/PR。策略由可信控制器配置提供；候选代码或模型不能修改本次有效策略。

| 决策 | 来源 | 如何落实 |
| --- | --- | --- |
| 范围内自动执行 | 用户已选择的任务授权 + 项目policy | PolicyDecision绑定实际动作，执行时复核 |
| 必须人工决定 | 需求歧义、越界、策略规定 | WaitItem + HumanApproval/回答，准确绑定目标版本 |
| 模型审阅结论 | ReviewReport | 作为gate输入，不形成执行权限 |
| 不可越过的限制 | 路径/凭据/branch保护等 | executor与Git host双重约束，不靠提示词 |

PolicyDecision 不只是一个allow布尔值：含策略revision、授权来源、作用范围、动作fingerprint、创建/失效条件。控制器在执行前重算或验证许可；审批发生后的代码变化不得沿用旧批准。内部可传不可伪造的绑定句柄，但不为本机架构发明签名服务。

人工模式为 never / on_exception / required；never 仍需遵守远端规则，遇到必须人审或无法决策则返回明确blocked/wait，不伪装自动通过。建议默认on_exception；人工审核只影响明确配置的阶段放行，不包含合并操作。不会在此次设计文档生成时改变实际远端保护规则或已有运行权限。

Provider凭据留控制器模型Adapter；Git host token留delivery；构建网络凭据只在获准build阶段以短期方式提供；execution/test容器默认无上述凭据。扩展代码若以后引入，不能把“同进程hook”宣传成隔离边界。

## 11. 持久化：采用 SQLite，而非继续手写跨文件事务

### 为什么现在调整

现有文件Store值得保留的是数据合同、hash、原子写与可检查证据，不必永远保留分散JSON作为元数据唯一事实源。V2需要一次动作同时预留request/attempt、更新阶段状态、保存wait/approval与事件；SQLite通过标准库提供本机事务，比新增一套文件WAL和恢复协议更简单。

目标：`.yipilot/state.db` 保存元数据，`.yipilot/objects/<hash-prefix>/<sha256>` 保存不可变大内容；`.yipilot/workspaces/`保存受控执行目录。JSON/JSONL作为导出与调试视图，不是第二事实源。

### 首版数据库配置

使用Python3.11可用的sqlite3显式事务：`isolation_level=None`，需要写操作时短`BEGIN IMMEDIATE`，成功COMMIT，异常ROLLBACK；每连接启用foreign_keys，设置有限busy_timeout。默认rollback journal（DELETE）和synchronous=FULL；不预先启用WAL优化。数据库只放本机可靠磁盘，不支持SMB/NFS/云同步目录作为活动数据库。

不跨模型/工具/网络调用保持DB写事务，不让SQLite连接跨线程共享。一个项目worker为主写入者，其他CLI只做短请求写入与查询；DB仍须靠约束保证重复/并发请求正确。未来有性能证据再评估WAL及实际运行SQLite版本。

官方依据：[Python sqlite3](https://docs.python.org/3/library/sqlite3.html)、[SQLite事务](https://www.sqlite.org/lang_transaction.html)。以上配置和文件布局为本方案选择，不是跨网络盘或硬件失效的可靠性承诺。

### 核心表与约束

| 表 | 核心约束 |
| --- | --- |
| tasks / operations | operation绑定task与目标需求revision；request key与canonical input hash唯一映射 |
| runs | UNIQUE(operation_id, stage, attempt)；status/version CAS；已结束Run不可重新执行 |
| artifacts / artifact_links | hash/size/media_type与owner；显式输入/输出/评审依赖，不按最新mtime推断 |
| events | 全项目INTEGER cursor；event_id唯一；与生命周期更新同事务 |
| tool_executions | UNIQUE(run_id, tool_call_id)；intent/started/result/uncertain，调用参数hash固定 |
| decisions / wait_items | scope/目标版本/有效性；只允许尚有效的回答解除等待 |
| external_actions | 稳定action key、request hash、状态、remote ID/receipt；不允许未知结果盲目重发 |

Operation checkpoint保存current_stage、accepted refs、candidate、wait reason、预算与next_check_at；所有外部ID用结构化列/受schema校验JSON。类型版本必须可迁移，禁止把任意Python对象pickle为业务事实。

### 工件与事务的提交顺序

1. 在受控临时文件写正文、flush/fsync并算hash；用同卷原子方式发布到内容地址。已存在内容要核实hash/长度；禁止覆盖。
2. DB事务登记工件metadata与refs，并提交Run结果、checkpoint和对应耐久事件。
3. 提交后发布通知；通知丢失可从events cursor回放。

崩溃在1之后2之前：留下未引用对象，可经保留期后回收；2提交后不得主动删除已引用对象。启动/消费时发现缺失或hash不符则阻断并报告，不补造成功。数据库备份与对象保留一起设计：SQLite backup API产生快照，保留其引用的对象再完成归档；不能只复制正在写入的DB文件。

## 12. 状态、事件、恢复和取消

Operation状态：queued/running/waiting/succeeded/failed/cancelled；waiting含明确reason和next_check_at。外部动作的不确定性保存在action.state=uncertain，并令Operation等待reconciliation，不能先标失败再直接重试。

Run状态：pending/running/succeeded/failed/cancelled；阶段暂停用WaitItem收口当前尝试，在输入齐全后创建新Run。这样不需要从工具执行中间恢复一个半活的Python栈；历史v1 paused通过迁移处理，不硬套新状态。

事件至少包括 operation.created、run.started、model.delta、tool.started/completed、artifact.created、review.completed、gate.completed、wait.required/resolved、run.finished、delivery.updated、operation.finished。状态事件事务落盘；高频delta可批量持久化并标明持久cursor，只承诺重连回放已提交事件。最终完整消息/工具结果在阶段结束前保存，不能只存在屏幕。

### 执行所有权

首版每项目一个执行worker，持有现有FileLock同类OS进程锁；记录owner nonce、PID、启动时间。heartbeat用于显示活跃与诊断，不单靠时间过期接管活进程。恢复先获得独占执行锁，并核实带run标签的进程/容器已停止；不能在旧worker仍执行时启动第二份写操作。

远端CI等待不占用运行中的Agent；提交wait checkpoint后释放执行轮次，通过持久next_check_at轮询。前台运行退出后可用resume启动新worker；可选background worker由本机普通进程管理，不需要Redis/Celery。没有活动worker时不声称会自行“醒来”，状态页须显示需要resume或后台进程。

### 恢复矩阵

| 中断位置 | 恢复动作 |
| --- | --- |
| request已落盘，Run未开始 | 原子找到请求，开始唯一新Run |
| 模型调用中 | 保存已知中断证据；在预算内新Run重试，不声称上次未计费 |
| 工具intent已落盘但执行结果缺失 | 核实worker停止与工作区快照；结果不能证明则放弃该副本、从已确认snapshot新尝试 |
| 工件发布后、DB结果提交前 | 未引用工件保留待回收，按动作类型核对实际副作用 |
| DB终态已提交、UI未收到 | cursor重放，不重新执行 |
| push/PR响应丢失 | query远端，以remote ID/head/key识别已发生事实；无法唯一判定则等待介入 |
| 等待人工/CI时重启 | 读取WaitItem与next_check_at，重新检查版本，继续等待或推进 |

取消分为请求取消、停止执行、收敛状态三步。取消标记持久化；模型/工具响应信号并关闭资源，允许固定宽限后终止所属进程。已经发生的写入不因取消被自动reset；已推送分支和已创建PR不会因取消被自动删除，应查询远端并记录真实发布结果。

## 13. 交付与 CI/Jenkins 设计

Candidate固定repo/base、snapshot/tree hash、requirement/design refs、verification/review refs、policy版本。提交只由可信GitPublish执行；Agent不能直接push或merge。

本地测试可在未提交snapshot运行，随后生成commit时核对Git tree完全相同（文件模式、删除/新增、资源亦计入）；不相同则重新验证。commit按动作键生成一次并保存SHA；push使用任务专用branch，不覆盖人工提交，分支冲突明确停止或选新branch后保存决策。

PR创建/更新使用稳定head/base和Operation标记；重放先查询已存在PR，不能每次开新PR。PR正文来自正式需求、diff与验证摘要，不把日志、密钥或未经选择的文件上传。

### CheckResult

记录provider、repository、head_sha、tested_sha、subject（head或merge_candidate）、base_sha（适用时）、check_name、external_id、status/conclusion、started/completed时间、URL与原始receipt ref。GitHub检查可能针对测试合并提交；必须核实其与当前head/base的映射，不能把所有tested_sha不同于head的结果一律误判，也不能接受无关联构建。success/pending/failure/cancelled/skipped/unknown分开；missing和unknown绝不当passed。必需check清单来自可信profile并服从Git host实际保护规则，模型不能缩减。

Jenkins触发传入准确SHA；queue item URL/ID与build number分别保存，不能把进入队列当构建成功。可信Pipeline必须按该SHA checkout并在结果中返回实际built SHA；仅看构建名称或SUCCESS不够。遇到响应丢失时按关联参数和已知queue/build查询，不能盲目重复触发。凭据/网络/SCM配置在Adapter外配置，未经实际企业fixture验证不得宣称公司Jenkins兼容。

### 交接边界

Workflow 在发布和检查完成后输出 PR/MR 链接、head SHA、变更摘要、需求/设计/评审引用及测试/CI 结果。完成状态描述该提交的交接事实，不承诺后续新提交仍满足检查。

不提供 merge 工具、自动合并开关、merge queue 接入或合并授权。后续人工审核与合并由代码托管平台及团队流程处理，不阻塞已完成的交接任务。


Jenkins接口依据：[Remote Access API](https://www.jenkins.io/doc/book/using/remote-access-api/)。远端系统不能与SQLite组成事务，本方案保证持久意图、对账与受条件约束的重试，不宣称跨系统exactly-once。

## 14. 公共入口与产品体验

以下是V2拟定命令，不是当前已实现命令：

```text
yipilot run --project <repo> --requirement-file <file> --profile <name>
yipilot run --project <repo> --requirement "需求" --target review_ready
yipilot status --project <repo> --operation <id>
yipilot logs --project <repo> --operation <id> --after <cursor>
yipilot resume --project <repo> --operation <id>
yipilot respond --project <repo> --wait <id> --answer-file <file>
yipilot cancel --project <repo> --operation <id>
```

首次接入提示建立profile，之后run可自动初始化内部Task与Operation并准备workspace，不让用户手动输入一串repo/run/approval ID。保留高级阶段命令用于诊断，但它们调用同一业务入口和gate，不能绕过工作流。

展示重点是业务阶段、正在做的工具、实际测试结果、等待原因、预算、PR链接；默认隐藏Approval fingerprint、Docker UID、artifact内部布局等实现细节，诊断模式再显示。交互界面和JSONL使用同一事件合同。

### 终端改进路线（技术建议，尚未实施）

普通用户在仓库内启动 yipilot，首次选择模型/环境，输入需求或本地材料，确认范围后进入自动工作流；显式 project 可覆盖当前目录。已有未完成任务时选择继续或新建，不静默绑定旧任务。原 /run 仅提案的含义与 V2 完整工作流不同，迁移时必须明确提示，不静默扩大同一旧操作的授权。

首先复用已有 prompt_toolkit 做轻量终端：多行输入、粘贴、命令/路径补全、固定状态提示、事件时间线、按需展开的文档/差异/测试摘要。需求正文不默认写入跨项目输入历史，原始材料中控制字符不能作为终端控制指令执行。状态从业务快照和带 cursor 的事件重建；高频 delta 可合批展示，不能丢弃阶段终态、等待项和失败。取消必须贯通模型/工具/容器并返回真实状态，不能只停止动画；UI 与执行分离，不在输入处理线程同步等待长调用。

CLI 命令和交互视图共用 start/status/respond/resume/cancel；视图不直接创建 Approval、选择有效 Run、调用多个底层 service 完成阶段。非 TTY 提供普通文本/JSONL 输出，避免全屏渲染和交互菜单进入管道。进度按实际阶段呈现，不猜测模型完成百分比。退出前说明任务是停止、等待还是仍由后台 worker 执行，不假装退出窗口后自动继续。

Pi 的可编程会话与 TUI 分离、差分渲染、粘贴与补全值得借鉴；不直接引入其 TypeScript TUI，也不自研终端渲染引擎。Python 首步不新增 UI 框架；有真实的多面板需求（同时浏览 SRS/SDD、diff、测试树）后才评估 Textual，替换 transport 不改工作流。Textual worker 也不能代替业务持久化与取消合同。

参考：[Pi SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)、[Pi TUI](https://github.com/earendil-works/pi/tree/main/packages/tui)、[prompt_toolkit](https://python-prompt-toolkit.readthedocs.io/en/master/pages/asking_for_input.html)、[Textual workers](https://textual.textualize.io/guide/workers/)。上面的选型与优先级是针对 Yipilot 的建议，不是这些项目提供了完整研发交付流程的声明。

验收关注 Windows Terminal 下中文/多行粘贴、终端缩放、持续输出时输入可用、取消实际执行、事件重连与非 TTY 输出；漂亮截图不能代替这些行为检查。

## 15. 现有优秀实现保留与迁移映射

| 现有能力 | 处理 | 迁移目标/注意点 |
| --- | --- | --- |
| GitRunner、repo cache、task worktree | 保留并收拢 | workspace/git；发布动作独立到delivery，不放开任意Git |
| 路径、链接/重解析点、输出漂移检查 | 保留 | security/paths + workspace；共享helper变明确合同 |
| Docker挂载/资源/退出/清理验证 | 保留并内部化 | workspace/docker/output；不因合并接口减少执行后校验 |
| Artifact hash与不可变证据思路 | 保留 | persistence/artifacts；迁移内容地址与DBrefs |
| Event cursor、JSONL渲染 | 保留合同与渲染 | DB为事实源，JSONL导出/订阅，不双写为两套权威日志 |
| 固定模型、connection/model分离、密钥存储 | 保留 | models；修正流、usage、token单位和取消合同 |
| SRS/SDD/test Harness | 保留有效检查 | 文档/验证gate；不把结构通过当语义正确 |
| ToolRegistry与审批模型 | 保留规则意图、重构接口 | workflow PolicyDecision + workspace执行复核 |
| v1 Runtime facade/双签名兼容 | 迁移后移除 | 一套Session/loop；不长期维护双主线 |
| 旧host apply/verification | 从生产主线移除 | 保留历史读取，不能成为Docker失败后的fallback |
| 测试fixtures | 保留并重新分层 | 注入同生产接口；清除仅证明提示语的伪主线覆盖 |

### v1数据迁移

迁移显式命令，先停止v1运行并获取项目独占锁；备份原`.yipilot`，扫描schema/hash与所有权，生成可审阅迁移报告。导入新DB时保留旧ID和source_version；历史Run标记legacy，不自动赋予新的gate资格或新PolicyDecision。

旧running/paused/pending不自动继续；确认worker停止后归档为interrupted，用户resume进入新Operation或明确的新尝试。缺证据对象隔离并列为unverified，不为凑完整记录创建passed报告。

导入完成做数量/refs/hash/抽样回放校验，再写schema marker切换读取；旧目录只读保留到用户确认清理。迁移失败不改v1读入口，重新执行按迁移ID去重。v2产生新操作后回退到v1不是无损降级，必须恢复迁移前备份并单独保留v2历史，不做静默双向同步。

## 16. 实施里程碑与验证

| 里程碑 | 可交付内容 | 退出标准 |
| --- | --- | --- |
| R0 入口与合同 | ProjectApplication、阶段输入/结果、现有CLI调用收敛；明确目录依赖 | 无stdin同样运行；没有新增第二条生产执行路径 |
| R1 状态可靠性 | SQLite/工件、幂等预留、Run结束事务、事件回放、v1导入 | 重放不重复；故障注入不会制造成功；旧数据可读 |
| R2 通用Agent与Workspace | 真流、多轮工具、上下文、取消、统一Docker工具执行 | 同一Agent能完成文档schema与小代码任务；主目录不变 |
| R3 本地研发闭环 | 需求/设计/评审/实现/验证/修复、profile环境 | 一次授权自动到verified；真实service失败修复后重验 |
| R4 分支/PR/CI | 一个Git host、checks、可选Jenkins、持久wait | 准确SHA对齐；重启不重复PR/push；旧CI不能放行 |

每个里程碑做到可运行再继续；调整目录随职责迁移发生，不先进行大规模纯搬家。R0可短暂保留内部兼容适配，但迁移某条入口后立即收敛调用方并写移除条件。模型/执行替身只存在测试注入，不恢复产品fake/replay开关。

### 必须通过的验收情景

1. 同一request同输入返回原Operation；同key不同输入明确冲突。
2. 文档有阻塞问题不编码；回答绑定正确需求版本，旧回答不能解除新问题。
3. 模型动态读取相关文件、编辑hash冲突后重读；不执行残缺工具参数。
4. 真实Service组合：测试失败→诊断→修复→重新应用/验证，不mock掉Repair主线。
5. 全skip、检查缺失、报告损坏、测试配置被削弱不能伪称需求验收通过。
6. 含正常二进制资源、不同包目录、第三方依赖的受支持Python项目可验证。
7. 每个持久化/副作用边界kill：旧worker确认停止后才重跑；预算和attempt不重复消费。
8. 取消模型/容器能停止并落盘；等待CI退出可resume，未有worker时如实显示。
9. push/PR成功但响应丢失，查询收敛且不重复创建或误标失败。
10. CI为旧SHA、PR被追加提交、target前移、保护要求改变，必须重新评估。
11. 自动模式无需逐步批准；要求人审时明确等待；不能由Agent缩减策略。
12. v1导入缺失/损坏证据可定位，迁移失败可回到备份，旧批准不冒充新策略授权。

快测用假ModelClient/Workspace/GitHost通过真实Workflow；契约测试核实流/取消/错误/结果schema；真实Docker与受控Git host独立opt-in验收。已有602离线测试记录是旧基线，不作为V2验收结果。

## 17. 关键设计选择与拒绝项

| 决策 | 选择与理由 | 再评估条件 |
| --- | --- | --- |
| 工作流框架 | 固定Python状态/转移表，业务状态归本项目 | 多分支/并行/长期等待复杂度确实超过自维护收益时评估LangGraph |
| 存储 | sqlite3事务元数据 + 文件工件，默认rollback journal | 多用户跨机器服务另立数据库部署设计 |
| Agent复用 | 自有Python通用核心，学习Pi而不引Node RPC | 有对比验收证明外部Runtime能降低总体维护成本时再替换 |
| 会话生命周期 | Session依附Run，无独立Session产品状态 | 出现真实多轮长期用户会话需求时另设计 |
| 并发 | 首版单阶段、顺序工具，项目worker独占 | 有吞吐目标并完成写冲突/预算/所有权测试后扩展 |
| 发布 | 推送分支、创建PR/MR、收集CI结果并交接；不自动合并 | 合并功能不在当前范围，只有用户重新提出才评估 |
| 多角色 | prompt/input/tools/result配置，复用一个循环 | 确有独立并行任务收益后增加子agent能力 |
| 安全 | 保留严格Docker/路径/证据；授权策略与执行隔离 | 不以性能或少代码理由删除现有必要保护 |

## 18. 实施前可确认的结论与限制

本方案已结合现有代码调用链与当日官方资料，具体目录、数据、阶段、恢复、权限、交付和迁移均给出设计选择。本轮仅收敛现有入口、Run生命周期与工作区职责，尚未完成通用Runtime、数据迁移或远端连接，不能称为完整已实现架构；真实企业Jenkins、Git权限和项目依赖环境仍需用对应fixture/opt-in测试验证。

方案成功的判断不是文件变多或变少，而是：增加设计评审不改模型循环；更换Docker执行后端不改研发流程；增加CLI/HTTP入口不复制审批与修复逻辑；新增CI不改Agent；任何成功都能追溯到正确版本的结果证据。
