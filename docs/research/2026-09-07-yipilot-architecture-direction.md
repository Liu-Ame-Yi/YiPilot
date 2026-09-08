> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# 参考 Pi、OpenHands 和 mini-SWE-agent 的 Yipilot 架构调整方向

日期：2026-09-07。性质：Proposed，架构建议，不是实现变更。

本轮回答的是模块应该如何组织、什么设计值得借鉴。前两份报告的缺陷与能力清单作为证据，不再把逐项修补当作架构答案。Pi 的一手资料和细节见 [Pi 架构参考](2026-09-07-pi-architecture-reference.md)。

## 1. 结论

Pi 值得重点学习，但应学习其通用 agent、编码会话、工具执行与界面的分工，而非复制 TypeScript 包布局或全部扩展系统。Yipilot 的目标还需要一层 Pi 通用执行循环之外的研发交付工作流。

推荐方向是：**薄入口 + 固定交付工作流 + 通用单阶段 AgentSession + 有明确权限的工具执行 + 独立仓库/沙箱管理**。保留现有可靠的证据、路径保护和隔离实现，逐步重新归属职责。不要先全仓搬文件，也不要再叠加一层空的通用 Service。

Pi 的执行模型提供参考；mini-SWE-agent 提醒我们基本循环可以很小；OpenHands 提醒我们执行环境和会话应独立演进。它们都不能替 Yipilot 定义“哪些设计评审、测试和 CI 结果允许自动合并”。

## 2. 现有代码是否真的耦合过重

### 静态证据

对 src 下 56 个非 `__init__.py` 模块做 AST import 扫描，包括函数内延迟 import；统计可解析的项目绝对模块 import，未覆盖动态加载和全部相对导入。

| 观察 | 结果 | 应如何理解 |
| --- | --- | --- |
| 已扫描依赖图中的模块循环 | 未发现 | 不能简单称为全仓互相引用或运行时循环依赖 |
| CodeService 直接项目模块依赖 | 25 | 与实际职责混合一起说明它是改动容易汇集的位置，数量本身不是质量判据 |
| CLI app / interactive 直接项目模块依赖 | 20 / 16 | 装配依赖合理，但当前还包含审批、选择运行、失败修复等业务决策 |
| RepairService 直接项目模块依赖 | 18 | 修复重复组装模型、上下文、证据和 Runtime，且知道沙箱 CLI 服务细节 |
| infrastructure → application | 2 条模块边 | Docker worker / executors 依赖上层的准备类型或审批逻辑，实际职责与目录标签不一致 |
| harness → application | 4 条模块边 | 检查实现依赖 application 的工作区类型和规则 |
| FileTaskStore 被引用 | 22 个模块 | 存储既是实现又提供核心记录类型，替换或改变记录会牵动广泛调用者 |

### 真正的问题是“调用者知道得太多”

1. **终端知道完整实施流程。** `_apply_proposal` 不只显示 diff，还准备 Docker 输入、创建批准、执行、检查输出、再应用和清理。增加无人值守入口就要重写这段编排。
2. **所谓通用 Runtime 已绑定代码补丁。** `RuntimeEvidencePort` 包含 `write_patch_proposal`；`ModelPlanner` 只解析 PatchProposal。文档作者、设计评审者难以共用它而不再增加旁路。
3. **业务步骤和底层操作身份混在一起。** ToolRequest 带 task/operation/run、工作区路径和批准信息；ModelRequest 也带业务记录与同 Run Markdown 工件限制。外部协议调用需要知道很多本属于上层调度的信息。
4. **执行协议太浅。** `ExecutorPort.execute` 还要接 cwd、project_root、environment、cancel_event、approval_gate、policy_revision、approval_id、confirm_token。调用者仍需要了解整个执行策略，没有被一个清晰执行会话隐藏。
5. **私有实现变成公共合同。** 多处跨模块导入 `_plain_path`、`_fingerprint_model`；sandbox_cli 导入 sandbox_verification 的 `_read`。真正共享的安全工具没有明确归属。
6. **Domain 内出现具体工具策略。** `domain/execution.py` 同时包含通用请求/结果、pytest 的特定 argv、policy revision 和当前 Python 解释器判断。更改一个验证工具会影响通用执行模型。

因此，目录名虽然已经“分层”，变化原因却没有分开。加一个验证方式、一个运行入口或一种模型输出，仍会牵动很多不应相关的模块。

## 3. 向成熟项目学什么

| 参考项目 | 值得学习的具体设计 | Yipilot 的对应调整 | 不照搬的部分 |
| --- | --- | --- | --- |
| Pi | 模型协议、通用 agent loop、编码会话、UI/SDK 分工 | 把模型与工具循环变成可供多个研发阶段共用的能力 | TypeScript 多包发布、完整扩展市场、默认宿主执行权限 |
| mini-SWE-agent | Model + Environment + 简单反馈循环 | 保持基本执行器小而可测试，不为每个角色造一套 runtime | 不把轨迹文件当跨阶段恢复，不直接开放任意 shell |
| OpenHands SDK | Agent 与 Conversation、Workspace 的职责区分 | 会话负责驱动与状态，工作环境负责本地/远程执行差异 | 不立刻引入远程 Agent Server、HTTP/WebSocket 和分布式部署 |

来源：[Pi agent-core](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)、[Pi SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)、[mini 控制流](https://mini-swe-agent.com/latest/advanced/control_flow/)、[OpenHands Agent](https://docs.openhands.dev/sdk/arch/agent)、[Conversation](https://docs.openhands.dev/sdk/arch/conversation)、[Overview](https://docs.openhands.dev/sdk/arch/overview)。上表第三、四列为本项目建议。

### Pi 的四点启发

**第一，模型循环不应懂 PRD、补丁或 Jenkins。** Pi 的 agent-core 围绕消息、模型、工具结果继续执行。对应 Yipilot，把 PatchProposal 的生成与校验放回“代码阶段”，通用 loop 只消费结构化消息/工具调用并返回终止结果。

**第二，调用模型前才构建模型上下文。** Pi 区分应用消息与 LLM 消息，通过 transformContext / convertToLlm 做筛选和转换。Yipilot 应把审计记录、工作流状态和模型输入分开：先筛选/压缩允许进入模型的上下文，再转换成 Provider 的消息，而不是把所有东西堆成一个 user prompt。[消息模型](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)

**第三，工具逻辑与执行位置可以分开。** Pi SDK 的工具支持替换底层 operations。Yipilot 的 read/search/edit/execute 可以统一输入输出，后端绑定 Docker 工作区；换环境时不应改文档/代码阶段的业务逻辑。[SDK 工具](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)

**第四，界面依赖同一个可编程会话入口。** 不论终端还是自动执行，应该调用同一套运行、订阅事件和取消能力，不能把交互代码当作唯一产品实现。[Pi SDK](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)

Pi 也不是整套完美清洁架构的证明，其上层会话聚合仍然很复杂；这里只学习其有效分工。Pi 明确说明默认执行权限与完整 OS 沙箱不是一回事，Yipilot 必须保留自己的隔离保障。[Pi Security](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md)

## 4. 建议的目标职责与依赖方向

```text
CLI / 后续 HTTP
        │ start / resume / cancel / events
        ▼
DeliveryWorkflow ────────→ Git host / CI / Merge integration
        │
        ├─ 阶段输入、评审/测试 gate、授权、修复/等待决策
        ├─ 固定输入版本与阶段结果 → 工作流状态/工件存储
        │
        ▼
AgentSession（一个阶段内的上下文与执行控制）
        ├─ AgentLoop ↔ ModelClient
        ├─ Context / Transcript / Budget / Cancel
        └─ ToolSet → Policy-checked execution → Workspace / Docker
```

这里 AgentSession 是职责名，不要求新增一个有独立 id 和状态机的 SessionRecord。第一版可直接依附阶段 Run；它负责一段会话的上下文、事件和执行控制。Task/Operation 仍负责业务任务，不把两种生命周期混成一张记录。

工作流不能把每个 token 写入业务 checkpoint，Session 也不能根据聊天文本自己决定合并。两者通过冻结的阶段输入和明确的 StageResult 交接。

### 目标目录示意（逐步迁移，不一次创建空目录）

```text
yipilot/
  workflow/       # 研发阶段、放行、返工、等待与恢复；阶段实现随需求加入
  agent/          # loop、session、context、工具定义、模型协议适配
  workspace/      # 源码快照、Docker 生命周期、输出校验与工作区应用
  delivery/       # 后续实现 commit/push/PR/CI/merge 时再加入
  storage/        # 已有文件记录、工件、事件存储
  transport/      # CLI，后续再加入 HTTP
  security/       # 共享路径/脱敏机制
```

类型跟随拥有它的模块：消息与 ToolResult 属于 agent，源码 manifest 属于 workspace，阶段与策略结果属于 workflow。只有 ArtifactRef 等真实跨模块合同共用；不再把所有类型、工具常量和业务规则塞进一个 domain/models 或 utils。

可以先在现有目录内完成这些职责调整，确认调用关系稳定后再迁移文件。目标是减少修改扩散，而不是制造一次巨大的纯改名 diff。

## 5. 各模块该暴露什么

以下是概念接口，不是待立即实现的完整 API：

| 模块 | 调用者关心的接口 | 内部必须隐藏的知识 |
| --- | --- | --- |
| Workflow | start / advance / resume / cancel / status | 阶段顺序、返工规则、等待 CI、checkpoint 与重复请求 |
| AgentSession | run(stage_input)、事件订阅、cancel | 消息历史、上下文裁剪、模型重试、turn 状态、工具结果回填 |
| ModelClient | stream(messages, tools, limits, cancel) | HTTP/SSE、供应商字段、finish reason、usage、协议错误 |
| ToolSet | 工具 schema 与调用结果 | read/edit/search/test 的具体执行和结果裁剪 |
| Workspace | prepare / execute / collect / close | baseline/execution 副本、容器、挂载、Git index、清理和 hash 校验 |
| Delivery | 发布候选、查询 checks、请求/查询 merge | Git host/Jenkins 数据结构及外部操作对账 |

依赖在一个装配入口创建并注入，不在每个 stage 里重新 new 一套 FileStore/ModelPlanner/Registry/GitRunner。也不需要一个通用 DI 框架。

安全检查仍必须在实际执行附近重新核对最新状态。隐藏策略细节不是删除新鲜度校验，也不是把所有 Tool 都视作已授权。

## 6. 现有代码如何归位

| 现有位置 | 保留的价值 | 建议归位 |
| --- | --- | --- |
| `runtime/agent_runtime.py` | 注入执行与证据的思路 | 改成消息/工具反馈循环；不查询业务 TaskStore 或解析补丁 |
| `runtime/model_planner.py` | 模型调用与输出检查 | 流处理归 ModelClient/Session；PatchProposal 校验归代码阶段 |
| `application/agent_runtime.py` | 启动/结束阶段 Run | 迁入统一阶段执行包装；删除双签名兼容分支后只有一个入口 |
| `application/code_service.py` | 代码阶段输入与结果 | 保留业务步骤，移出模型/沙箱/存储重复装配及旧 host apply |
| `application/repair_service.py` | 失败诊断与修复要求 | 工作流决定重试，代码阶段以修复上下文再次执行同一入口 |
| `application/quality_artifacts.py` | 文档工件和结构校验 | 复用同一 Session/ModelClient，独立文档输出合同 |
| `sandbox_*.py` + `docker_worker.py` | 严格隔离与输出验证 | 同属 workspace 能力；业务调用者只看到准备、执行、收集结果 |
| `harness/` | 确定性检查 | 测试/文档 gate 跟随 workflow；源码完整性校验归 workspace；统一报告类型保留 |
| `infrastructure/task_store.py` | 原子存储、锁 | 存储实现归 storage，核心业务记录迁到拥有它的模块 |
| `transport/cli/interactive.py` | 交互体验 | 只负责输入显示，真正的人工回答提交给 Workflow |

不把所有 sandbox 代码并进一个巨型文件。它们内部仍可拆 source、worker、output，但对外是一个完整能力；外部不再跨文件读取其私有 helper。

## 7. 需要补齐的细节设计，而非再加功能开关

| 设计问题 | 应明确的合同 | 建议归属 |
| --- | --- | --- |
| 上下文如何选取 | 需求/设计 refs 固定；按需读文件；token 与字节预算分开；压缩不删除审计原件 | Session/context + Workflow inputs |
| 工具调用失败后怎么办 | 非零退出/找不到文件可回给模型；权限拒绝不能由模型自行解除；基础设施故障单独分类 | ToolResult + Loop + Policy |
| 编辑如何防漂移 | edit 携带读到的版本/hash 或明确前置条件，失败后重读；不能静默覆盖 | Workspace tools |
| 多个工具能否并行 | 首版顺序执行即可；只读独立调用以后再并行，同一工作区写操作明确串行 | AgentLoop/tool execution |
| 哪些模型消息会被保留 | assistant toolCall 与 toolResult 配对，取消和截断有明确 finish reason，不伪造完整响应 | ModelClient + Transcript |
| 状态与事件谁负责 | 统一执行入口收敛 Run 终态；实时事件和耐久事件共用合同，写入失败有恢复规则 | Session + Workflow runner |
| 副作用能否重试 | 纯模型推理与 Git/PR/merge 分类；外部响应不确定时先查实际结果 | Workflow + Delivery |
| 测试通过意味着什么 | 结构化执行结果、必需验收用例、skip 政策、被测 tree/commit 与环境版本 | Verification gate |
| 人工是否必要 | 预授权范围内自动；歧义/越界才等待；PolicyDecision 与人工 Approval 区别记录 | Workflow policy |
| 如何防止上下文改变规则 | 仓库文本与模型输出是待处理数据，不能更新批准策略或给自己签发权限 | Context selection + Policy |
| 文档/代码评审如何复用 | 同一个 Session 执行机制，不同输入、prompt、工具集与结构化结果；评审不沿用作者整段对话 | Stage definitions |
| 如何清理长任务 | 工件保留策略、工作区所有权、清理幂等、活跃执行判别，避免每轮副本无限堆积 | Storage/Workspace/Workflow |

这些合同应与主线同时落实。不是要求第一版就有复杂压缩算法、多 agent 调度和任意语言支持；先保证一个 Python 项目、顺序工具执行和明确预算下的完整行为。

## 8. 调整顺序及检验方向是否有效

1. **提取统一产品执行入口。** 把 apply/verify/repair 的业务推进从 CLI 移走，终端与脚本调用同一入口。验收：无 stdin 也能执行同一段已授权流程。
2. **收敛模型、Session、上下文。** 文档、代码、修复共用调用与事件机制；代码补丁 schema 不再进入通用 loop。验收：增加“设计评审”阶段不改 Provider 和循环核心。
3. **收拢工作区执行能力。** 隐藏副本、Docker、输出校验与 apply 细节，保留所有安全验证。验收：阶段代码不认识 Docker argv 或 baseline/execution 的具体目录布局。
4. **补固定工作流和业务 gate。** 固定需求版本，自动设计、评审、实现、验证与有限修复；单一状态事实源。验收：失败可重试、断点可恢复、同一请求不重复执行。
5. **增加远端交付。** 先一种 Git host/CI，后自动合并。验收：这一步只修改 workflow/delivery/配置，不要求改模型循环或终端业务逻辑。

每步迁移已有行为并保留必要回归，不在新层之外保留另一条“旧但仍可用”的生产主线。测试替身注入同一个执行入口。

如果完成调整后，换模型仍要动代码阶段，增加 CI 仍要动 AgentLoop，或者增加命令行入口仍要复制 apply/verify 判断，就说明只是换了目录，耦合并没有真正改善。

## 9. 验证与限制

本轮以官方文档/源码核对外部设计，直接阅读本地接口并做 AST 依赖统计；没有安装 Pi、OpenHands 或 LangGraph，没有执行运行时替换。统计不等于完整架构证明；未扫描到循环不代表不存在动态依赖或运行时状态耦合。此轮没有修改业务代码，也不宣称建议架构已经完成或通过端到端验收。
