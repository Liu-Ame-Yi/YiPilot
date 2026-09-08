> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# Yipilot 项目脉络与自动交付架构整理

日期：2026-09-07。性质：现状调查与 Proposed 调整建议，不是已经实施的架构变更。

本报告以当前工作区为基线，包括已有未提交修改；没有把 HEAD、历史规格或产品状态页直接当作当前实现。此次只增加研究文档，不修改代码或覆写原有规划。外部项目比较见 [Python agent 架构研究](2026-09-07-python-agent-workflow-architecture.md)。

## 1. 核心判断

Yipilot 已经积累了可复用的执行与审计基础，但产品主线仍偏向“人工逐步操作的补丁提案、应用、验证工具”。用户真正需要的是“输入仓库与需求后，能够自动推进到合并的研发工作流”。

最重要的调整不是再增加一套目录分层，也不是立刻换 agent 框架，而是明确两种循环的职责：

- **外层研发工作流**：决定现在处于哪个业务阶段、什么结果允许前进、失败回到哪里、何时等待外部 CI、何时需要用户。
- **内层 agent 执行**：在一个阶段内读取上下文、请求模型、使用受控工具、观察结果，直到产生该阶段要求的输出或达到预算。

当前底层证据与隔离机制较完整，外层工作流基本缺位，内层真实模型路径则仍是单次补丁生成。只补 Jenkins 或给现有命令加自动点击，都不能补上这两个缺口。

## 2. 项目为什么走到这里

| 时期/文档 | 当时的方向 | 对当前结构的影响 |
| --- | --- | --- |
| 根目录 `规划书.md` | Go Zpilot 的 Python 重写，保留研发阶段、状态、工件、隔离和兼容合同 | 引入较大的业务词汇表与长期范围；它本身就约定 Apply 不合并 |
| 8 月 5 日 Python 设计 | 收缩成 M0–M4 本地 CLI、文件存储、受控执行和离线验收 | 先建控制面和证据基础，应用命令成为推进单位 |
| 8 月 28 日 Runtime ADR | 将模型计划执行与存储、工具具体实现分离 | 出现 `runtime/` 与 `application/` 兼容入口 |
| 8 月 29 日产品方向 | AI 修改、验证、推送、Jenkins、PR、人工合并 | 交付终点仍是人审交接，而非自动合并 |
| 9 月模型与沙箱增量 | 真模型、交互终端、受控补丁、Docker、副本验证、修复 | 新路径已进入工作区，但旧路径及多处文档尚未同步收敛 |

因此问题有三层：目标发生变化；实现按能力横向增加；旧设计与新实现并存。不能把所有复杂性都归咎于 Python 或分层架构。

## 3. 当前实际能力

| 目标环节 | 当前代码事实 | 与目标的距离 |
| --- | --- | --- |
| 目录与需求输入 | Project、Task、repo 注册与同步、Task 开发目标已存在 | 普通用户仍需完成多步准备，不能一个入口跑完整流程 |
| 自动理解仓库 | 有冻结 base、源码副本、选定文件与上下文预算 | 主要依赖用户选择文件，缺少按需搜索/阅读的 agent 循环 |
| 生成 PRD/设计 | `QualityArtifactService.generate` 只接受 fake/replay；PRD 是输入 | 不是实际的“需求→模型生成 PRD/设计”路径 |
| Agent 审设计 | SRS/SDD 结构 Harness 已存在；SDD semantic review 开启会报未实现 | 缺少可消费的语义评审结论及修订回路 |
| 真实代码生成 | `CodeService` 经 application Runtime 进入 runtime ModelPlanner，保存 PatchProposal | 真模型已汇入 Runtime，不能沿用文档称它仍绕过 Runtime；但仍是一次请求 |
| 应用修改 | 交互 `/apply` 使用 Docker 副本、输出证据、再写任务 worktree | 已有代码；不是只做 prepare-only，但依赖多次终端确认 |
| 本地验证 | `SandboxVerificationService` 执行固定验证并生成 Handoff | 已有代码；尚不是完整任务自动推进与完成判据 |
| 自动修复 | `/verify` 失败可调用一次 `RepairService`，有最多两次、15 分钟的链约束 | 只自动生成下一份提案，仍提示用户 `/apply`，未自动闭环 |
| commit / push / PR / CI / merge | 当前生产代码未找到相应交付工作流 | 需要实现；仓库自身的 GitHub Actions 不等于受管项目 CI 集成 |
| 恢复 | 有持久 Run、事件回放、保守恢复 | Recovery 将运行/暂停 Run 标为失败，未实现跨阶段自动续行和外部结果对账 |

实现存在不等于已经在真模型、真实 Docker、远端仓库上完成端到端验证，验证范围见文末。

## 4. 当前调用脉络

```text
CLI / 交互终端
  ├─ quality generate → QualityArtifactService → fake/replay 文档 → 结构 Harness
  ├─ /run → CodeService.generate
  │           → 固定模型、冻结源码、组装选定上下文
  │           → application.AgentRuntime（启动/结束 Run 的兼容包装）
  │           → runtime.AgentRuntime → ModelPlanner → ModelPort
  │           → PatchProposal + 空 steps 的 AgentPlan → 本次生成成功
  ├─ /apply → 选择提案、用户确认
  │           → SandboxCliService → DockerSandboxWorker
  │           → SandboxOutputService → 用户确认 → SandboxApplyService
  └─ /verify → SandboxVerificationService → 验证报告 / Change Handoff
                └─ 失败 → RepairService → 新 PatchProposal → 返回等待 /apply

共享事实与执行基础：
Task / Operation / Run / Artifact / Event / Approval / ToolRun
File stores / 文件锁 / Git / 路径约束 / Docker / 脱敏
```

这条实际路径解释了为什么“代码里已有不少功能”与“用户仍然要一直操作”可以同时成立。

## 5. 设计与分层问题，按影响排序

### P0：没有负责完整任务的工作流

`TaskRecord` 只有身份、创建时间、标题和 development_goal；`RunRecord` 有 stage/status，却没有把整项需求的当前阶段、已接受工件、当前候选修改和下一步串起来的持久工作流。

`OperationRecord` 有请求与幂等标识，可以作为整次工作流执行的上层记录，不需要再发明 Job、Session、PipelineRun 等同义层。建议让用户的“完成此需求”对应一个顶层 Operation，已有阶段 Run 作为尝试记录，工作流 checkpoint 记录关联关系。

**后果：**某个 code_generate Run 成功只代表补丁提案生成成功，不能代表需求实现、测试通过或任务交付完成；当前程序也没有统一位置决定下一步。

### P0：产品规则与自动化目标冲突

`docs/product/vision.md` 以 human review merges it 为终点；roadmap M8 不允许系统合并；旧设计甚至使用“永不自动 merge”。这与本轮用户要求不同。应在下一次正式规格修订中明确：允许符合项目预授权策略的自动合并，人工审核可配置。

不能用程序替自己生成一份“人工批准”来模拟自动化。当前 `CodeService.apply` 的旧宿主路径内部直接构造 APPROVED，`VerificationService` 也保留内部批准写入逻辑；它们目前不在交互产品主路径上，但说明授权事实与业务动作没有完全分开。

建议保留真实人工 Approval，另由固定程序记录 PolicyDecision，标明授权来源、策略版本、作用范围与实际修改指纹。模型评审是质量证据，不是授权者。

### P1：终端承担业务编排

`interactive.py` 的 `_apply_proposal`、`_verify_change`、`_automatic_repair` 直接选择 Run、构造执行输入、写 Approval、触发修复、清理 worker。

这些并非纯显示逻辑。未来 CLI 脚本、Jenkins 回调或常驻进程会被迫复制同样流程。应把“选择下一步、执行、保存结果、决定等待/修复”放进 application 工作流；终端只提交请求、显示事件、回答真正需要用户的问题。依赖装配允许放在入口，但业务阶段决策不应依赖 stdin。

### P1：Runtime 的名称比真实能力更大

`runtime/agent_runtime.py` 只调用一次 planner，再遍历固定 steps，没有“工具结果→再次调用模型”的循环。真实 `ModelPlanner` 只解析 PatchProposal，并返回 `steps=()`；turn 编号固定为 1。

这是一种可用的受控补丁生成器，适合小范围修改，但不能等价于能探索仓库、观察测试反馈的通用 CodeAgent。当前生成和修复调用还传入 `harness=lambda: True`：JSON/schema 校验仍真实存在，但该回调不是代码质量验收。

短期可以保留这个最小补丁路径完成一个窄范围闭环；面向一般项目需求时，需要同一 Runtime 支持受控 read/search/edit/execute 的多轮执行。文档生成、设计评审等纯文本阶段也不能强制解析为 PatchProposal。

### P1：语义评审与确定性验证混在“通过”这个词里

`SddHarness` 的语义检查默认关闭，返回 nonblocking passed；开启则 `SEMANTIC_REVIEW_NOT_IMPLEMENTED`。结构正确不能证明设计正确。

建议分别保存：结构/引用验证报告、agent 语义评审报告、可执行测试报告。评审至少包含 blocking findings、证据引用和被审工件 hash。工作流根据策略组合这些结果，不能只看任意一个 passed。

### P1：跨阶段依赖靠“最新结果”推断

`CodeService._approved_documents` 遍历 Task 中最新的成功 quality Run 和 Harness；没有符合条件的文档就返回空列表。这对独立 code generate 命令可以成立，但不能充当“设计审核通过后才能编码”的工作流约束。

自动工作流必须固定本次接受的 requirements/design/review artifact refs 以及需求版本；需求或设计改变时，旧评审和依赖它的结果失效。不能把另一次运行中碰巧最新的文档当成本次阶段输入。

### P2：旧新执行路径并存，核心逻辑位置不够稳定

- `CodeService.apply` / `VerificationService` 保留宿主执行路径；当前交互入口走 `SandboxCliService` / `SandboxVerificationService`，测试显式防止回退到宿主。
- `application/agent_runtime.py` 是带生命周期逻辑、可变参数与旧构造兼容的包装，不是简单 re-export；`application/model_planner.py` 也有旧新签名兼容。
- 多个 application 文件分别创建 store、journal、artifacts、Run 生命周期和模型上下文；CodeService 与 RepairService 重复拼装同类执行链。
- `harness/` 导入 application 的 `PreparedWorkspace` 和 approval helper，并读取具体 FileStore；它没有严格处于独立规则层。文件读取本身不是问题，问题在于业务类型和规则归属交叉。
- Task、Operation、Run 等关键记录定义于 `infrastructure/task_store.py`，使应用需要经文件存储模块取得业务数据合同。

不建议为追求“绝对纯净”给每个 FileStore 加 Protocol。优先迁移被多处依赖的业务记录与规则；真正的外部差异点（模型、执行环境、代码托管/CI）再保留小接口。

### P2：文档和验收偏向历史切片

status 仍称 `/run`、受控 apply、Runtime 汇合及 Docker worker 未实现；README 仍有已被当前代码移除的 planner/prepare-only 使用描述。根规划书又是较大规模的 Go 重写历史参考。

这是工程治理问题：后续 agent 读旧“限制”可能继续添加兼容层，而不是完成当前产品。应维护一份当前产品目标与能力表，并明确历史记录不再扩张第一版需求。

## 6. 建议的产品流程

```text
项目目录 + 需求 + 项目策略
  → 冻结仓库基线，读取必要上下文
  → 生成需求/验收标准与设计
  → Agent 评审，有限次数修订
  → 实现与测试代码
  → 本地验证 + 代码评审
      失败 → 带诊断修复 → 重验；超预算或无法判定 → 请求用户
  → 固定候选修改，commit 到任务分支
  → push + 创建/更新 PR
  → 等待当前提交的 CI / Jenkins / 必需检查
      代码失败 → 修复并产生新提交 → 重新检查
      基础设施故障 → 有限重试/等待
  → 按策略可选人工审核
  → 满足分支保护后自动合并
  → 核实远端 merge SHA，记录交付完成
```

第一版可以把需求、验收标准写入一份 requirements.md，把实现设计写入 design.md；用户已有 PRD 就直接摄取并补足缺失信息，不必强制 PRD→SRS→SDD 三份重复长文。复杂任务需要拆分实施步骤，但暂不需要另做通用任务图平台。

Agent 审阅使用独立上下文与明确的评审要求；可以复用同一个模型连接，不要求一开始就有多进程、多角色常驻 agent。审核结果可以降低风险，但不能保证模型一定发现所有问题。

CI/Jenkins 的测试、lint、类型检查属于确定性检查；若需要 AI 代码审查，应作为独立审查结果呈现，不把它混称 Jenkins 自动理解了代码设计。

## 7. 建议保留的模块结构

先保持现有顶层目录，只明确职责并增补缺失主线；以下是目标职责，不是立即创建的目录清单。

| Module | 应负责 | 不应负责 |
| --- | --- | --- |
| `transport/` | 参数、输入、显示、事件订阅 | 决定修复、签发策略许可、选取有效阶段产物 |
| `application/workflow.py` | start/advance/resume、阶段调度、有限修复、等待外部结果 | 解析模型流、拼 Docker 命令 |
| application 阶段实现 | 文档、评审、实现、验证、交付的业务输入输出 | 各自发明一套顶层任务状态 |
| `runtime/` | 单阶段模型/工具循环、上下文与执行预算 | 判断整个需求已可合并、处理 Jenkins 生命周期 |
| `domain/` | Task/Run 等稳定记录、StageResult、策略与状态规则 | 文件路径布局、HTTP、Git 子进程 |
| `harness/` | 确定性检查与统一报告 | 隐式代替 agent 语义评审、决定流程下一站 |
| `infrastructure/` | 文件存储、Provider、Git、Docker、未来 Git host/CI Adapter | 决定哪项需求应进入下一业务阶段 |
| `security/` | 已有脱敏与跨路径执行保护 | 代替任务的产品完成标准 |

保留 application 对具体基础设施的合理装配。不要一次性重命名所有文件，不引入通用 ServiceFactory，不要求每个实现都抽接口。先清理运行主线和失去调用者的旧实现，再依据真实复用抽取公共执行逻辑。

## 8. 最小持久状态与恢复合同

复用 Task / Operation / Run / Artifact：一个 Task 可有多次顶层工作流 Operation，每个阶段的实际尝试仍为 Run。在顶层 Operation 下保存版本化 checkpoint，至少知道：

- 当前阶段/等待原因、工作流策略版本、是否请求取消。
- 冻结仓库与 base SHA，当前候选 diff/tree hash，提交后 head SHA。
- 本次接受的需求、设计、评审、验证产物引用，而非“最新文件”。
- 各阶段 Run 与 attempt，累计时间/模型调用预算。
- 待处理外部动作、目标 repo/branch、PR/build 标识和已知结果。

StageResult 可以仅有输出 refs、结果类别及诊断；阶段生命周期继续复用 RunStatus。暂不添加第二套与 Run 一一对应的状态机。

恢复先加载 checkpoint 并复核证据，再执行下一步。模型调用可以在预算内用新 Run 重试；push/PR/merge 网络超时后必须先查询远端是否已发生，不能盲目再写一次。外部 CI 等待需要可反复执行的 advance/resume，不需要一直挂着一个 agent 对话。

本地文件 store 目前可继续使用；先用任务/顶层 Operation 锁限制单一推进者，并规定产物、checkpoint 和事件的写入/恢复顺序。不要因为需要恢复就同时引入 PostgreSQL、Redis、Celery。若出现明确的多进程事务需求再评估存储升级。

## 9. 少人工干预如何落地

用户在任务开始时授权范围：目标仓库、允许路径、模型数据范围、执行环境、预算、可推送分支、CI 要求与自动合并条件。程序据此做后续明确决策，而不是每次测试都询问。

| 行为 | 推荐处理 |
| --- | --- |
| 范围内读取、生成文档、隔离环境编辑和测试 | 已获任务授权后自动进行，保留实际执行记录 |
| 范围内修复 | 工作流按预算自动继续；测试或文档失效时重新验证 |
| 无法从仓库与需求确定的产品决策 | 保存问题并等待用户，不自行猜造验收目标 |
| 超出允许目录、执行/网络范围、预算 | 保存具体原因，请求新的授权 |
| 推送任务分支、创建 PR | 按项目交付策略授权执行，记录远端结果 |
| 合并 | 由固定规则检查相同 head SHA 的评审与 required checks，服从分支保护；可使用托管平台自动合并机制 |

人工审核可配置为关闭、总是需要或仅指定范围需要。自动合并不是绕过保护；若远端要求人工审核，则需要满足现行规则或由项目维护者事先调整规则，不能让 agent 冒充审核人。

写入新提交会使旧验证/审阅至少重新接受适用性检查；目标分支变化也应由平台 merge 检查、必要时重新集成测试处理。确认远端合并结果后才能完成 Task，不能把 merge 请求发出当作成功。

## 10. 调整顺序与每步验收

| 顺序 | 工作 | 可见验收结果 |
| --- | --- | --- |
| 1 | 统一当前目标和能力表，明确自动合并、可选人工、首版一个 Python repo | 文档不再同时要求“永不合并”和“自动完成”；当前命令示例可用 |
| 2 | 引入固定工作流，从 CLI 移出业务推进，复用现有 proposal/apply/verify/repair | 一条命令完成窄范围实现→验证→失败修复→重验，中途无需逐步输入 |
| 3 | 接通真实需求/设计生成与 agent 评审，固定各阶段工件引用 | 需求→设计→评审→实现成立；评审阻塞时不会进入编码 |
| 4 | 统一生产 Runtime 和沙箱路径，按真实任务补按需读取与多轮工具执行 | 修复能读取所需上下文；假模型走同一生产控制流；旧宿主路径明确停用或移除 |
| 5 | 增加 commit/push/PR + 一种 CI 结果接入，状态可恢复 | 精确提交与 CI 对齐，重启不重复 PR，旧提交的绿灯不能放行 |
| 6 | 增加可选人审与自动合并、失败对账 | CI 通过后自动合并；响应丢失时通过远端核实收敛到同一结果 |

第 2 步使用 fake ModelPort 和 fake CI 做工作流回归是合理的，但它们属于测试依赖，不需要重新暴露 fake/replay 产品模式。真实试跑应另做有预算、可检查结果的验收。

关键工作流测试比继续增加浅层包装测试更有价值：正常交付；评审要求修订；测试失败后修复；预算耗尽；需求更新使设计审阅失效；CI SHA 不匹配；等待 CI 时进程退出；push/merge 成功但响应丢失；可选人工审核。

## 11. 暂不建议做的事

- 不整体重写，不把已有 worktree、Artifact hash、固定模型、路径保护和 Docker 验证删掉。
- 不先引入一个复杂 agent 编排框架来替代尚未定义的业务状态。
- 不把每个角色都实现成一个独立 agent 进程，不先造插件市场、分布式调度或服务端。
- 不把受控补丁模式当作永久产品能力上限；按真实跨文件任务证明是否需要工具循环。
- 不为了消除所有 import 跨层而创建大量空接口；先解决实际的业务决策分散与执行路径重复。

## 12. 主要源码锚点

以下路径相对仓库根目录，行号对应本次工作区快照：

- `src/yipilot/persistence/task_store.py:30` — Task/Operation/Run 当前数据合同。
- `src/yipilot/transport/cli/interactive.py:238` — /run 实际为提案入口。
- `src/yipilot/transport/cli/interactive.py:301` — UI 内应用编排与多次确认。
- `src/yipilot/transport/cli/interactive.py:393` — UI 内验证及失败后修复。
- `src/yipilot/workflow/code_service.py:321` — 当前真实 Runtime 调用链。
- `src/yipilot/workflow/code_service.py:383` — 从最新合格文档取上下文。
- `src/yipilot/agent/model_planner.py:37` — 单次模型响应解析并返回空步骤计划。
- `src/yipilot/agent/agent_runtime.py:57` — 单计划执行，而非反馈驱动模型循环。
- `src/yipilot/workflow/quality_artifacts.py:118` — 文档生成限制为 fake/replay。
- `src/yipilot/harness/quality.py:663` — 语义评审未实现。
- `src/yipilot/workflow/repair_service.py:57` — 一次修复提案与预算约束。
- `src/yipilot/workflow/recovery_service.py:17` — 保守失败，未自动续行。
- `src/yipilot/application/agent_runtime.py:21` — 生命周期与历史签名兼容包装。
- `src/yipilot/workflow/code_service.py:165` — 旧宿主 apply 路径。
- `src/yipilot/workflow/verification_service.py:84` — 旧宿主验证实现。
- `.github/workflows/ci.yml:1` — Yipilot 自身离线 CI。

## 13. 本轮验证范围

已逐段追踪模型生成、交互应用、Docker 验证、修复、质量文档及恢复主线，并搜索调用方与交付关键字；这是架构调查，不是逐行安全审计或完整漏洞清单。

本轮新运行的检查：

- `.venv/Scripts/python.exe -m pytest -m 'not live' --ignore=tests/integration/test_docker_worker.py -o addopts='' -q -p no:cacheprovider`：602 passed、4 skipped、1 deselected，409.99 秒。显式排除了真实 Docker 集成文件和 live 模型测试，不是完整生产环境验收。
- `.venv/Scripts/python.exe -m ruff check src tests --no-cache --output-format concise`：通过。
- `.venv/Scripts/python.exe -m mypy src --cache-dir .mypy_cache`：64 个源文件检查通过。
- `git diff --check`：通过，仅有已有文件的 LF/CRLF 提示；两份新增研究文档另行检查了尾随空白，报告的源码路径与行号均存在。

这些结果说明现有离线合同总体有回归保障，不说明缺失的自动交付流程已经可用。未调用真模型、未进行远端 Git 写入，也未在本轮重新验收真实 Docker 环境。
