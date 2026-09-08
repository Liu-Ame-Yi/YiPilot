> **Historical reference / Superseded — 文档整理：2026-09-07。** 本文保留旧阶段设计与实施记录，不再作为当前待办或产品范围。当前目标见 [产品定义](../../product/vision.md)，技术设计见 [V2](../specs/2026-09-07-yipilot-v2-architecture.md)，实际能力见 [状态页](../../product/status.md)。下文原状态、强制要求及命令仅在原历史范围内成立。

# Yipilot 后续能力边界与阶段性非目标

**状态：已确认**
**日期：2026-08-28**

## 决策

Yipilot 的当前重点是 Python CLI 与可审计的本地 Agent 闭环；这不构成对服务化、云部署、沙箱或受控 Git 自动化的永久排除。

后续任务描述必须将暂未实现的能力表述为“本任务非目标”或“后续里程碑”，不得表述为整个项目的“明确禁止”，除非该能力确实被项目永久排除。

## 已确认的后续方向

1. **FastAPI 与 SSE 服务层**：可在 CLI、Run/Event 语义和权限模型稳定后增加，复用既有持久化事件与 watch 语义。
2. **云部署**：可在身份、租户隔离、密钥管理、审计和运行隔离设计完成后单独规划；当前不作为 M0–M4 的交付范围。
3. **Agent 沙箱**：需要作为独立能力建设。Docker 分发镜像只解决运行环境一致性，不等于 Agent 执行沙箱。
   第一版沙箱应至少明确 workspace 挂载范围、默认网络策略、CPU/内存/时限、禁止 Docker socket 与宿主机凭据挂载、取消与清理行为，以及 Event/Artifact/Harness 证据。
4. **受审批的 Git 自动化**：未来可在隔离 workspace 中增加 commit；push/merge 必须另有更高风险的审批、远程身份、目标分支保护和审计设计。当前 `prepare-only` 是阶段性边界，不是永久限制。
5. **模型 Provider 配置**：可以参考 Pi Agent 的配置体验、模型选择和认证交互；Yipilot 仍保持独立实现，不将 Pi Agent 或其他外部 Agent CLI 作为运行时依赖。

## 当前阶段的解释

M0–M4 中不新增 FastAPI、云部署、自动 Git apply/push/merge 或完整 Docker 沙箱，是为了先验证本地 CLI、模型调用、证据与 Harness 闭环。它们应在闭环稳定后以独立规格、独立风险评审和独立验收进入后续里程碑。

## 对后续任务的要求

- 明确区分“Docker 分发环境”和“Docker 沙箱执行器”。
- 不能把容器称为完整安全隔离，除非挂载、网络、凭据、资源限制、逃逸边界和取消行为均已验证。
- 不得将“参考 Pi Agent”解释为调用、包装或依赖 Pi Agent。
- 任何新增 FastAPI、SSE、云部署、Git push/merge 或沙箱能力，都必须先补充其专门设计、权限边界和验收测试。
