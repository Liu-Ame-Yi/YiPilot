> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# Python agent 与软件交付工作流的职责边界

日期：2026-09-07。状态：**Proposed**。本文是官方资料研究，不代表已接受的架构决策或本项目已实现的能力。查阅的是当日公开文档，未安装、压测或验证这些框架的特定版本。

研究目标：项目目录 + 需求 → PRD/设计 → agent 审查 → 实现 → 测试 → 分支/PR → CI/Jenkins → 可选人工 → 合并。

## 官方资料观察

| 项目 | 实际提供的核心边界 | 与交付闭环的关系 |
| --- | --- | --- |
| OpenHands Software Agent SDK | `Agent.step()` 执行推理、工具调用、观察；`Conversation` 管生命周期、事件、持久化；Workspace/Agent Server 管执行环境。 | 是可嵌入的开发 agent 执行基础。会话结束不天然等于需求验收、CI 通过或合并成功。 |
| mini-SWE-agent | `DefaultAgent` + `Model` + `Environment`；主循环是查询模型、执行动作、追加观察，直到退出。 | 很适合参考单阶段执行器的最小边界，不提供本研究目标所需的完整业务阶段模型。 |
| LangGraph | 预定路径的 workflow 与动态选工具的 agent 均可构建；有 checkpoint、条件路由和 interrupt。 | 可以承载跨阶段流程，但节点内容、验收规则、Git/CI 集成仍由应用定义。 |

表中能力依据：[OpenHands Agent](https://docs.openhands.dev/sdk/arch/agent)、[Conversation](https://docs.openhands.dev/sdk/arch/conversation)、[mini 控制流](https://mini-swe-agent.com/latest/advanced/control_flow/)、[LangGraph workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents)。表中“与交付闭环的关系”是根据已查阅接口边界作出的判断，并非穷举各项目所有扩展。

### OpenHands：执行、会话、环境有分工

Agent 接收事件历史并执行一步；Conversation 负责驱动执行、维护状态与事件。持久化示例使用稳定的 `conversation_id` 和 `persistence_dir` 保存、恢复会话。值得参考的是会话与执行逻辑分离，而不是照搬类数量。[Agent](https://docs.openhands.dev/sdk/arch/agent)、[会话持久化](https://docs.openhands.dev/sdk/guides/convo-persistence)

远程路径把 SDK 放进 Agent Server，通过 HTTP/WebSocket 连接容器或远程工作区。隔离执行是 Workspace/Server 的职责，业务流程仍由调用方驱动。[Agent Server](https://docs.openhands.dev/sdk/guides/agent-server/overview)

官方 PR review 是独立 GitHub Actions 示例：标签或 reviewer 请求触发，调用 agent 后向 PR 写反馈。这个例子证明 review 可以放进 CI 的工作流；它本身没有证明 SDK 自带“PRD 到自动合并”的端到端产品。[PR Review](https://docs.openhands.dev/sdk/guides/github-workflows/pr-review)

### mini-SWE-agent：简单循环可以与清晰边界并存

控制流只围绕模型查询和环境执行，并包含步数、费用、时间限制。轨迹文件保存消息、配置、费用、退出状态和提交结果。轨迹留存可用于检查过程；据此不能推定已经具有跨业务阶段的故障恢复、外部操作幂等或审批调度。[控制流与代码](https://mini-swe-agent.com/latest/advanced/control_flow/)、[输出格式](https://mini-swe-agent.com/latest/usage/output_files/)

环境可替换：local 直接运行主机命令且没有隔离，Docker 使用容器执行。这个边界可参考；最少代码并不意味着 local 自动适合无人值守执行。[Environments](https://mini-swe-agent.com/latest/advanced/environments/)

### LangGraph：提供恢复机制，但不替应用定义事实

官方明确区分固定执行顺序的 workflow 与自行选择工具、过程的 agent。Checkpointer 保存图状态，支持恢复、人工介入与失败节点后的继续执行。[Workflows and agents](https://docs.langchain.com/oss/python/langgraph/workflows-agents)、[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)

`interrupt()` 需要 checkpointer、稳定 thread ID 以及外部 resume 输入。恢复会从节点开头重新执行，因此中断之前的副作用必须幂等。选择框架仍不能省略“创建 PR 是否已成功”“merge 请求超时后实际是否已合并”等外部事实核对。[Interrupts](https://docs.langchain.com/oss/python/langgraph/interrupts)

## 针对本项目目标的建议（推论）

建议先用固定 Python 工作流表达交付阶段，统一调用一个单阶段 agent 执行入口。暂不因为缺少 workflow 就立即引入 LangGraph。资料支持职责分离，没有证据表明完成这条固定流水线必须依赖通用图框架。

| 责任 | 建议归属 |
| --- | --- |
| 当前阶段、下一阶段、失败返工、尝试上限、等待外部检查、恢复 | 交付工作流；使用持久化业务状态 |
| 生成设计、修改代码、阅读失败日志、提出审查意见 | 单阶段 agent 执行器；各阶段使用不同输入与工具权限 |
| 文档版本、代码 SHA、测试证据、审查结果是否符合放行规则 | 确定性的验收与合并策略 |
| checkout/worktree、沙箱命令、文件变化、进程限制 | 执行环境与 Git 集成 |
| PR、CI/Jenkins、远端审核、合并结果查询 | 外部服务集成；结果返回工作流 |

agent 的“完成”只表示本次执行结束。是否进入下一阶段，应读取实际产物与结构化结果；不能直接采用聊天中的“测试通过”“可以合并”。文档审查应针对明确的产物版本，代码审查与 CI 应关联明确的 commit SHA；提交变化后需要重新判断旧证据是否仍有效。

自动化比例可以很高。人工审核可作为策略开关或异常出口，而不是每次必经节点；确定性的测试、检查和合并条件仍需存在。检查失败自动进入有限次修复；超过次数、需求矛盾、凭据缺失等进入可恢复的等待/失败状态。

固定工作流也不能退化成一个从头跑到尾的巨大函数：至少在阶段边界保存输入/产物引用、当前状态、尝试次数和外部操作标识。创建 PR、推送、合并遇到超时，应先查询现状再决定重试。第一版可复用已有存储，无需先引入分布式队列和通用插件节点系统。

当流程出现大量条件分支、并行子流程、多个长期中断、跨 worker 恢复，并且自行实现这些能力开始成为持续维护负担时，再评估 LangGraph。评估应包括迁移现有状态、checkpointer 后端和副作用恢复测试，而不是仅比较画图与节点 API。

本研究未核实所有框架扩展的 Jenkins 或自动合并支持，也未对仓库现有实现做独立代码审计；具体改动位置应以本次主架构梳理为准。
