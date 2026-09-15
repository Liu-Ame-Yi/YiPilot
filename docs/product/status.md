# 当前实现状态

核对日期：2026-09-15。依据当前工作树源码、离线组合测试和 CLI help 检查，**不是发布版本或完整端到端验收声明**。产品目标见 [产品定义](vision.md)，技术目标见 [V2 架构](../superpowers/specs/2026-09-07-yipilot-v2-architecture.md)。

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
| 任务与证据 | 文件化 Task/Operation/Run、事件、工件与任务 worktree | [TaskStore](../../src/yipilot/persistence/task_store.py)、[Git workspace](../../src/yipilot/harness/git_workspace.py) |
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
- 自动 commit/push、PR/MR 创建和目标仓库 CI/Jenkins 接入尚未完成。Yipilot 自身的 CI 不等于目标仓库交付集成。
- SQLite 迁移、通用上下文管理及完整需求追踪尚属设计提案。
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
