# 当前实现状态

核对日期：2026-09-15。依据当前工作树源码、离线组合测试和 CLI help 检查，**不是发布版本或完整端到端验收声明**。产品目标见 [产品定义](vision.md)，技术目标见 [V2 架构](../superpowers/specs/2026-09-07-yipilot-v2-architecture.md)。

## 2026-09-16 模型响应和阶段协议增量

共享 OpenAICompatibleModelPort 已分离正文 bytes、输出 tokens、思考内容及 SSE 传输预算，明确拒绝 length/缺失正常结束/工具调用；安全回执记录结束原因、字节计数与供应商用量。SRS/SDD/Review 新输出必须标记 schema_version=1；正常完整响应的格式错误每阶段最多修复一次，次数和失败引用先持久化，语义门禁及未知调用结果不自动重试。公共入口、SQLite、工件存储和旧代码路径继续复用。

当前默认 `deepseek-flash` 未关闭思考，两次真实样例分别通过（58.59 秒、80.14 秒）。第一次四阶段直接通过；第二次设计评审拒绝初版、自动修订后通过，共六次调用。两次都验证了重启复用、源码未变和 SQLite 一致性；真实传输 0.4–1.6 MB，超过旧 256 KiB 限制而正文仍小于 4096 字节。详情与工件哈希见 [本次验收记录](../guides/model-stream-contracts-2026-09-16.md)。

全量离线检查 690 passed / 42 skipped（1367.37 秒）；全量收集后新增及收尾修改另由 111 项相关回归、12 项事务/修复复测、39 项最终客户端测试覆盖，数字有重叠，不相加。Ruff、mypy（72 个源文件）、specification help、diff 检查通过。尚无精确 tokenizer、自动上下文压缩、全仓库检索或分章节文档生成；不把两文件验收扩大为任意仓库成功保证。已有 SQLite 等未提交修改仍保留为本次实际工作树基线，不属于本任务提交。

## 2026-09-12 需求与设计闭环

新增真实输入 → SRS → 独立需求评审 → SDD → 独立设计评审，使用现有公共入口、模型选择与 ModelPort、SQLite 和工件存储。支持每类文档有界修订、澄清答案绑定、旧下游失效、幂等请求与阶段提交边界恢复。调用不确定时停止，不能声称外部恰好执行一次；普通阶段不逐次人审。

最终相关验证（2026-09-15）：39 项通过（198.97 秒），含全部 30 项闭环测试、架构边界检查及原真实提案路径的离线测试；覆盖正常路径、返工上限、澄清、失效、重启、不合法输出、模型错误、引用校验、事务回滚与路径边界。Ruff、mypy（72 个源文件）、specification CLI help 和 diff 格式检查通过。

此前全量离线结果：664 passed / 42 skipped / 1 failed（1430.31 秒）。唯一失败是跨模块导入私有读取函数，已将共享函数及两处调用统一为公开的 read_context_files，读取行为不变；该架构检查包含在上述 39 项修复后复测中。未再重复完整套件，不把这次结果写成“全量全部通过”。全量开始后补充的新需求替代中断调用测试也已纳入最终 30 项闭环测试。

真实 DeepSeek 样例四阶段通过（18.02 秒），使用临时公开配置副本的 reasoning_effort=none；原全局配置未修改。默认配置/low 推理预算触发现有响应上限，未完成；none 的第一次非法来源引用被程序拒绝，改进提示后新请求通过。成功只覆盖两文件样例，不代表默认配置或所有仓库可用；返工与故障分支仍是离线验证。详见 [真实验收记录](../guides/specification-live-acceptance-2026-09-12.md)。

此路径止于已接受的需求和设计工件，没有接入自动编码、GitHub、Actions 或合并。旧 quality fake/replay 和 code generate/apply/verify 的行为保留。[入口说明](../guides/specification-workflow.md) · [实现与事务边界](../architecture/specification-workflow.md)。

## 已接入的能力

2026-09-08 已将现有代码归入 app、workflow、agent、models、workspace、persistence、security、domain、harness、transport；详见 [文件分级与迁移映射](../architecture/code-layout.md)。下方第一轮重构记录中的 application/runtime 等名称是上一轮的历史归属，源码链接已经更新。

| 能力 | 当前事实 | 源码依据 |
| --- | --- | --- |
| 任务与证据 | SQLite Task/Operation/Run、事件和结构化索引；文件工件与任务 worktree | [TaskStore](../../src/yipilot/persistence/task_store.py)、[Git workspace](../../src/yipilot/harness/git_workspace.py) |
| 模型选择 | 用户设置、连接/模型、冻结选择和密钥引用 | [CLI](../../src/yipilot/transport/cli/app.py)、[交互入口](../../src/yipilot/transport/cli/interactive.py) |
| 真实提案 | /run 与 code generate 共用 ProjectApplication → CodeService → RunExecutor → runtime.ModelPlanner/AgentRuntime，返回 PatchProposal | [CodeService](../../src/yipilot/workflow/code_service.py) |
| 受控应用 | /apply 经用户确认，在离线 Docker 副本应用，再检查输出并确认写入任务 worktree | [ProjectApplication](../../src/yipilot/app.py)、[SandboxCliService](../../src/yipilot/workflow/sandbox_application.py) |
| 验证与修复提案 | /verify 调 Docker 验证；符合失败条件时触发有限修复提案，仍需后续应用和验证 | [验证](../../src/yipilot/workflow/sandbox_verification.py)、[修复](../../src/yipilot/workflow/repair_service.py) |
| 沙箱配置 | sandbox doctor/register 检查并登记本地固定镜像 | [CLI](../../src/yipilot/transport/cli/app.py) |
| 质量工件 | quality generate 保留 fake/replay 的确定性文档演练及结构检查 | [CLI](../../src/yipilot/transport/cli/app.py) |
| 真实需求与设计 | specification 经公共入口独立生成/评审 SRS 和 SDD，支持有界返工、澄清、输入失效和阶段恢复 | [SpecificationService](../../src/yipilot/workflow/specification_service.py)、[入口说明](../guides/specification-workflow.md) |

## 仍存在的缺口

- 没有从原始需求到 SRS/SDD 评审、编码、验证、发布的统一自动工作流。
- Runtime 仍主要围绕一次 PatchProposal；不是已完成的动态检索/编辑多轮 Agent。
- 真实 SRS/SDD 生成与独立语义评审已接入固定闭环；尚未自动连接编码及后续交付，确定性 fixture 不代表理解了用户需求。真实验收仅覆盖下述小样例和明确模型配置。
- 当前 /run、/apply、/verify 已通过 ProjectApplication 收敛业务路径，仍保留逐次人工确认；不是预授权自动推进或可持久恢复工作流。
- /logs、/cancel 尚不可用；不能将 V2 恢复/取消设计当成当前能力。
- 自动 commit/push、PR/MR 创建和目标仓库 GitHub Actions CI 接入尚未完成。Yipilot 自身的 CI 不等于目标仓库交付集成。
- SQLite 已接入现有业务状态，提供显式旧数据迁移；通用上下文管理及完整需求追踪仍未实现。
- 已有幂等、恢复、流式与测试判定仍有已知缺口，见 [代码缺口分析](../research/2026-09-07-yipilot-code-gaps.md)。已有记录/类不等于所有恢复情景均可靠。
- Docker 路径已经接入，但镜像、依赖和本机环境仍须单独检查；不能宣称任意 Python 仓库都已可验证。

## 已失效的旧描述

不再使用 code generate 的 --planner/--provider-profile 示例；当前签名没有这些参数。code apply --prepare-only 也不是当前 CLI 命令。旧文档“/run 未实现”“Docker 仅为设计”“真实模型在 Runtime 外单独执行”等表述已过时。

自动合并是明确排除的功能，不计入待补缺口。历史测试数量、已提交矩阵和真实 Docker 试验只证明其对应版本/范围，不作为 V2 完成证据。

## 本轮职责边界重构

- 公共入口负责现有生成、候选查询、应用、验证及失败修复衔接；终端只提交选择/批准意图并展示结果。可通过 Python 调用，无需 stdin。
- RunExecutor 管理模型阶段的开始/结束与中断；runtime 保留单次提案协议，不新增多轮 Agent。
- workspace 收拢 repo、worktree、源码副本、Docker、可信输出和写入；security 提供公共路径/受限证据读取和错误摘要脱敏。
- 删除 application 的 AgentRuntime 双签名兼容层、ModelPlanner facade、PlannerPort 再导出；保留 runtime 中唯一实现及实际的 RegistryToolGateway。
- 文件 schema、工件路径、批准指纹与 Docker 策略保持原合同；拒绝意图也落盘，审批事件复用已有 stage.progress。

实际迁移、验证范围与剩余耦合见 [实施记录](../architecture/responsibility-refactor.md)。本轮不等于 V2 R0 的全部产品/耐久状态要求已经完成。

本轮最终离线验证（2026-09-08）：pytest 612 passed / 41 skipped；Ruff 通过，mypy 65 个源文件通过，git diff --check 通过。未运行真实模型、真实 Docker 或远端验收。

第二轮文件分级验证（2026-09-08）：完整 pytest 612 passed / 41 skipped，477.66 秒；Ruff、mypy（66 个源文件）、CLI help、模块导入检查及文档链接检查通过。仅迁移现有代码与引用，未新增完整工作流能力。

提交前复核（2026-09-08）：当前两轮重构工作树完整 pytest 612 passed / 41 skipped（482.77 秒）；Ruff、mypy（66 个源文件）、CLI help 和暂存区 diff --check 通过。测试期间源码、测试及依赖配置指纹未变化。真实模型、Docker 和远端验收未运行。


## 本轮 SQLite 持久化迁移

现有 Task、仓库登记、Operation 幂等请求、Run 尝试和生命周期、Approval、ToolRun、业务事件及 Artifact 元数据已接入项目 `.yipilot/state.db`。正文与 Git/Docker 资源保留文件，用户级模型配置和密钥不变。旧文件元数据不再参与日常读写，旧项目必须显式迁移。

相关状态、结果引用和生命周期事件原子提交；文件先发布并核对哈希。服务执行使用独占 OS 锁，SQLite 写锁不代替执行所有权。生成、应用、验证和修复的显式请求可重放，修复计数不再改变原请求身份。中断的外部动作不自动重复，保留不确定错误和回执供核对；这不是完整断点续跑。

提供只读扫描、逐文件备份验证、独立目标转换、校验后切换、重复导入保护和工件审计。旧审批撤销执行资格，未结束旧 Run 归档，不自动继续。迁移和故障测试先在临时 fixture 上验收。2026-09-09 经用户明确授权，真实项目已完成停机迁移：项目记录 1 条，原始文件 1 个，无 Task/Run；备份与原文件哈希一致，SQLite integrity_check 为 ok、foreign_key_check 为空，应用读取配置一致，工件审计 errors/orphans 为空。备份为 `.yipilot-backup-f7c6f515a4bb5ab1`；原始文件保留，当前事实源已切换为 `.yipilot/state.db`。

详见 [事务与数据映射](../architecture/sqlite-persistence.md)、[操作、备份与回滚](../guides/sqlite-migration.md)。实现前基线：612 passed / 41 skipped（543.76 秒）。最终全量离线验证（2026-09-09）：636 passed / 41 skipped，916.04 秒，退出码 0；Ruff、mypy（69 个源文件）、CLI help 和 git diff --check 通过。源码、测试及依赖配置哈希在本轮完整验收期间未变化。迁移专项 23 项通过，覆盖并发请求、事务回滚、孤立/损坏工件、重启回放、旧数据导入与切换失败、旧审批失效及链接锁保护；实际 ProjectApplication 服务组合通过。未运行真实模型、真实 Docker 或远端验收。


## 2026-09-11 真实服务验收补充

真实 DeepSeek + 本机 Docker 已在带 shebang 的独立 Python 样例上完成生成、应用、pytest/Ruff/mypy 验证和结果重放；原样例仓库保持不变。普通无 shebang 样例曾因容器内 EXE002 失败，相关有限修复未成功，不代表 Windows 文件模式兼容性或环境故障自动修复已解决。GitHub MCP 已在用户指定 Test01 私有仓库创建草稿 PR #1，真实 Actions 完成通过、故意失败、修复后通过三轮验证，并核对各 head/base/tested merge SHA。PR 未合并；这是平台实测，产品 GitHub/Actions 交付集成与远端自动修复尚未实现。详见 [真实验收记录](../guides/live-acceptance-2026-09-11.md)。

### 当前就绪程度与后续建议

关键基础能力已验证，完整交付流程尚未接通，不能标记“所有能力就绪”或“V2 完成”。

| 能力 | 验收边界 |
| --- | --- |
| SQLite、幂等、审批、事件与工件校验 | 已实现并通过专项和全量离线测试；真实迁移仅含项目配置，无历史 Task/Run |
| DeepSeek 生成、Docker 应用与检查 | 指定独立样例真实通过；Windows 文件模式与环境故障修复仍有失败记录 |
| GitHub 分支、提交、PR 与 Actions | Codex 通过 MCP 在 Test01 真实操作通过，未接入 Yipilot 产品服务 |
| 自动发布、CI 跟踪与有限修复闭环 | 尚未实现；远端修复提交由本次测试显式操作，不是模型自动修复 CI |
| 当前 SHA 的 required checks 门禁、远端发布恢复 | 尚未实现或验收；已有本地恢复保护不代表完整断点续跑 |

针对远端交付，建议先接通最小路径：发布测试分支 → 创建草稿 PR → 跟踪当前提交检查 → 持久化结果，再接有限修复。验收需覆盖 head/base/tested merge SHA 关联、旧检查失效、缺失或未结束检查不放行，以及重复请求和外部结果不确定时不重复发布。保持人工决定合并，不自动部署。

这是后续建议，不表示本轮已实施，也不改变 [实施路线](roadmap.md) 的整体里程碑。后续每次能力变化同步更新本页，真实测试的提交、运行链接、失败与限制归入验收记录。
