# 当前实现状态

核对日期：2026-09-08。依据当前工作树源码、离线组合测试和 CLI help 检查，**不是发布版本或完整端到端验收声明**。产品目标见 [产品定义](vision.md)，技术目标见 [V2 架构](../superpowers/specs/2026-09-07-yipilot-v2-architecture.md)。

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

## 仍存在的缺口

- 没有从原始需求到 SRS/SDD 评审、编码、验证、发布的统一自动工作流。
- Runtime 仍主要围绕一次 PatchProposal；不是已完成的动态检索/编辑多轮 Agent。
- 真实 SRS/SDD 生成与语义评审尚未构成生产闭环；确定性 fixture 不代表理解了用户需求。
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
