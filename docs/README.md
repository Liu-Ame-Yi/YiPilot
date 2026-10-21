# 文档入口

文档分为产品目标、当前事实、技术提案和历史参考。**不要把整个 docs 目录合并成一份实施指令。**

## 当前阅读顺序

代码定位优先看 [当前代码与文件分级](architecture/code-layout.md)：记录 2026-09-08 已实施的目录归位、实际依赖约束及旧路径映射。第一轮职责重构合同保留其当时范围；后续文件分级以这份实施记录为准。

**当前职责重构任务**请先读 [第一轮职责边界重构执行合同](superpowers/plans/2026-09-07-responsibility-refactor.md)。这是 plans 中的现行专项指引，区别于下文所述旧计划；它约束本轮范围，V2 仍作为完整目标架构。

1. [产品定义与完整流程](product/vision.md)：单仓库、输入输出、PRD/SRS/SDD、阶段放行、人工介入及交接终点。
2. [术语](../CONTEXT.md)：业务名词的统一含义。
3. [当前实现状态](product/status.md)：源码已有能力与缺口。
4. [V2 架构提案](superpowers/specs/2026-09-07-yipilot-v2-architecture.md)：模块/目录、状态、恢复、权限、迁移与验收。
5. [实施路线](product/roadmap.md)：R0–R4 顺序。
6. [当前使用指南](guides/README.md)：实际可运行入口；不能直接运行架构提案中的未来命令。

[架构图](architecture/yipilot-v2.html) 是模块总览，完整业务阶段以产品定义和 V2 阶段表为准。[图检查记录](architecture/yipilot-v2.review.md) 只验证图，不验证业务实现。

目录职责迁移记录见 [职责边界重构实施记录](architecture/responsibility-refactor.md)。当前 SQLite 数据映射与事务边界见 [持久化实施记录](architecture/sqlite-persistence.md)，实际操作见 [迁移指南](guides/sqlite-migration.md)。

本地业务替换与检查过程见[第四阶段业务迁移记录](research/2026-10-21-opensandbox-business-migration.md)；[服务器业务演练](research/2026-10-21-opensandbox-business-validation.md)已完成六个真实 Job、交付工件与重启/清理检查，常驻和用户项目切换尚未完成。

后续[真实模型与 YiPilot 实测](research/2026-10-21-opensandbox-real-project-validation.md)已跑通模型生成和远程应用，但原仓库二进制资源与测试缓存被现有规则拒绝，完整真实项目验证未通过；该记录列明下一步修复顺序和119条任务的收尾审计。

## 历史与依据

本轮 OpenSandbox 专项的 **Proposed 实施安排**见[远程迁移计划](research/2026-10-19-opensandbox-implementation-plan.md)：包含四阶段、验收条件、代码整合范围与用户下一步。它不是当前可用能力或对产品 roadmap 的更新；服务器现状与源码分发分别见[安装记录](research/2026-10-19-opensandbox-server-installation.md)、[GitHub 同步记录](research/2026-10-19-yipilot-github-sync.md)。

2026-10-20 已授权开始第一阶段；实际代码基线修复、隔离实测和 G1 剩余门槛见[第一阶段记录](research/2026-10-20-opensandbox-phase1-validation.md)。

最新的[第三阶段记录](research/2026-10-20-opensandbox-reliability-validation.md)包含真实 1/2/4 轻任务、选定故障恢复、磁盘上限与保留期；[Runner 闭环记录](research/2026-10-20-opensandbox-runner-validation.md)保留第二阶段范围。隔离依据见 [v2 实测](research/2026-10-20-opensandbox-control-isolation.md)，[v1 失败记录](research/2026-10-20-opensandbox-profile-validation.md)保留历史证据。业务全面替换、项目锁和重型负载尚未完成，尚未放行正式不可信任务。

2026-10-21 的[第四阶段前置记录](research/2026-10-21-opensandbox-toolchain-validation.md)新增固定工具镜像、18 项隔离回归、512 MiB 小工具任务与轻任务混合并发、四轻任务和旧 profile 的真实回归；业务调用仍未切换。[镜像构建与分发](../deploy/toolchain/README.md)说明本地 image ID 和归档部署的限制。

| 位置 | 如何使用 |
| --- | --- |
| [原始规划书](../规划书.md) | AI 对 Go 原项目的二次总结，混有重写建议；只作参考，不要求兼容所有协议、目录或功能 |
| superpowers/specs 中其余日期规格、superpowers/plans、superpowers/decisions | 旧阶段设计/实施记录，已退出当前实施入口；保留源码背景，不续做旧清单 |
| [ADR](adr/) | 历史技术决策及理由；适用范围与替代关系以当前架构明确说明为准，不因 accepted 标签覆盖新产品范围 |
| [研究](research/) | 当日调查与证据；包括已放弃的自动合并、多仓库等讨论，不是产品承诺 |
| [历史追踪矩阵](traceability-matrix.md) | 对应旧实现/提交的证据；旧路径和测试可能已迁移 |
| [历史手测](manual-testing.md) | 旧 fake/replay/prepare-only 演练，不作为当前操作指南 |
| diagrams/ | 历史设计图，不是当前拓扑或实施承诺 |

冲突处理：用户最新明确决定优先；目标查产品定义，现状以当前代码/测试核验并修正状态页，设计查 V2。旧规格与研究不得扩大范围。[维护规则](DOCUMENTATION.md)。
