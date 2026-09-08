> **Historical reference / Superseded — 文档整理：2026-09-07。** 本文保留旧阶段设计与实施记录，不再作为当前待办或产品范围。当前目标见 [产品定义](../../product/vision.md)，技术设计见 [V2](../specs/2026-09-07-yipilot-v2-architecture.md)，实际能力见 [状态页](../../product/status.md)。下文原状态、强制要求及命令仅在原历史范围内成立。

# Yipilot Spec 可行性与边界审查

- 审查日期：2026-08-05
- 审查对象：`2026-08-05-yipilot-python-design.md`
- 结论：**可行，但仅在严格按里程碑收缩、明确宿主执行风险并将公司集成保留为 Adapter 的条件下可行。**

## 1. 可行性结论

| 范围 | 结论 | 条件 |
| --- | --- | --- |
| M0–M2：文件化控制面与 Git 隔离 | 可行 | 使用临时 Git repo、文件锁和原子写；不依赖公司系统 |
| M3：受控执行器与代码证据 | 可行 | 自动化测试仅 fake；真实 CLI 执行必须显式接受 host-trusted 风险或在 sandbox 运行 |
| M4：SRS/SDD/测试工件闭环 | 可行 | 从 fixture/fake/replay 开始；真实模型是可选 Adapter，不能成为 CI 前置 |
| REST/SSE、ACP、企业系统、Bugfix、生产化 | 条件可行、未承诺 | 当前仅交付本地 CLI；扩展边界已保留，实施前另建里程碑、fixture 与验收 |

## 2. 已确认的范围边界

1. **名称边界**：Yipilot 是新产品；`zpilot` 只作为公司 Go 基线的参考名。CLI、包和运行目录不再承诺同名兼容。
2. **环境边界**：内部 MCP、Wiki、BugTask、GitLab、身份系统、ACP 私有差异均不是 MVP 依赖；它们只能通过 disabled/replay/production Adapter 接入。
3. **安全边界**：未沙箱化的外部 coding agent 继承启动用户潜在权限。运行目录、argv 和环境白名单不等于安全隔离，因此不能宣称最小权限。
4. **恢复边界**：可恢复的是持久化业务状态与已验证工件；中断中的外部副作用不会盲目 resume，而是以新 Run 重试。
5. **Harness 边界**：Harness 证明预定义的确定性检查，而不证明需求真实性、模型正确性或代码没有缺陷。

## 3. 发现并已修正的问题

| 问题 | 风险 | 修正 |
| --- | --- | --- |
| 规格仍使用 zpilot 命名 | 新产品名与 Go 兼容目标冲突 | 统一为 `yipilot`/`.yipilot`/`src/yipilot`，旧名只用于未来显式迁移 |
| M3 写代码似乎依赖 M4 的 SDD | 里程碑顺序不可执行 | M3 改为接收明确开发指令或用户提供的现有 SDD；M4 才生成工件 |
| CLI executor 被描述为具有最小权限 | 宿主进程无法提供该保证 | 引入 `fake/sandboxed/host_trusted` 信任级别与显式风险接受 |
| `skipped` 聚合未完全精确 | 可能把跳过误当作通过 | 规定 blocking skipped 的总状态、下游阻断和 policy 声明 |
| “恢复”描述过强 | 可能重复模型/Git/网络副作用 | 明确中断旧 Run 终止并创建新的 retry Run |

## 4. 仍需在实施计划前冻结的决定

1. Yipilot 内建 Agent Runtime 的本机受控工具执行是否允许以 `host_trusted` 方式启用，还是 M3 必须先提供 Docker sandbox。
2. 首版数据根是否固定在被管理项目中的 `.yipilot/`；若需要集中式控制目录，应另立数据根策略。
3. M4 的真实模型供应商、预算与凭据来源；在此之前仅实现 fake/replay。
4. 首版目标平台是否只支持 Windows；若是，文件权限和锁的验收须以 Windows ACL/reparse point 为准，而非 POSIX 0600/symlink 假设。
5. 单元测试执行是否只支持 Python 项目；跨语言测试 runner 应在后续以 adapter 添加。

## 5. 建议的“可启动”范围

先把 M0–M2 当成第一个 release：`yipilot init`、Task/Operation/Run/Event、原子 Store、临时 Git repo sync、隔离 worktree、GitWorkspaceHarness。它不需要模型、不改远端、不依赖公司条件，却能验证该项目最关键的业务控制能力。

第二个 release 才加入 M3 fake executor 和显式风险模型；第三个 release 添加 M4 的 SRS/SDD 与 UnitTest Harness。这样每一步都有可运行、可审计、可回归的 CLI 产品成果。
