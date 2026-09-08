> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# Pi 架构借鉴：应学习职责边界，不应复制整个产品

检索日期：2026-09-07。研究对象为官方 `earendil-works/pi` 仓库当日 `main` 的文档与源码（可变分支，未固定 commit）；Pi 为 TypeScript 项目。本报告没有安装依赖或修改实现。以下“事实”有官方依据，“建议”是针对 YiPilot 自动研发目标的设计判断。

## 1. Pi 真正分开的是什么

| 边界 | 官方实现的职责 | 对 YiPilot 的启示（建议） |
|---|---|---|
| `pi-ai` | 统一模型和 Provider API、认证解析、结构化流事件、工具调用数据、usage/cost | Provider 不决定文档阶段、审批、Git 或下一步；向执行层交付协议明确的消息、错误与流 |
| `pi-agent-core` | 状态化 Agent、模型调用与工具执行的循环、事件、取消和消息队列 | Runtime 应能独立完成“观察→工具→再观察”，不需要导入 CLI 或任务存储 |
| `pi-coding-agent` / `AgentSession` | 组合 Agent，管理消息历史、模型设置、压缩、重试和会话事件 | 需要一个可被终端及后台共同调用的应用入口，而不是每个 UI 自己串业务 |
| `SessionManager` | 会话记录持久化、分支路径、上下文重建 | 保存的完整历史，与每次交给模型的上下文应分开 |
| interactive / print / RPC | 同一会话能力的不同 I/O 形式 | 去掉终端后，任务仍应能执行、取消、恢复、查询 |

依据：[pi-ai README](https://github.com/earendil-works/pi/blob/main/packages/ai/README.md)、[agent-core README](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)、[SDK 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sdk.md)、[SessionManager 源码](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/session-manager.ts)。

Pi 并非“每层只放几个短文件”的架构样板。当前 `agent-session.ts` 约 3290 行，而且直接导入 `modes/interactive/theme/theme.ts`。这说明它也存在聚合过重和 UI 依赖渗入，不能凭项目成熟就全部照抄。真正值得保留的是独立循环、可替换工具执行、统一非 UI 入口。依据：[AgentSession 源码](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/agent-session.ts)。

## 2. 消息转换的两道边界

Pi 的调用边界是：

```text
AgentMessage[]
  → transformContext：选择、裁剪、注入上下文
  → AgentMessage[]
  → convertToLlm：转换成模型协议支持的消息
  → Message[]
  → Provider
```

这是两个职责。前者回答“本轮需要知道什么”，后者回答“如何表达成 Provider 接受的角色与内容”。循环本身持续使用应用消息类型，到 LLM 边界才转换。依据：[agent-loop.ts，streamAssistantResponse](https://github.com/earendil-works/pi/blob/main/packages/agent/src/agent-loop.ts)。

Coding agent 的转换会将 shell 执行结果、压缩摘要等变成普通 user 消息；标记为不进入上下文的 shell 消息被过滤。界面可显示的信息，不必全送进模型。依据：[messages.ts，convertToLlm](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/messages.ts)。

**建议：** YiPilot 将“原始日志/工件/审计记录”与“本阶段输入”分开。设计阶段读取需求与仓库摘要，编码阶段读取已评审设计及相关源码，修复阶段读取失败证据并允许继续查文件。不要把所有阶段共享成一条无限增长的聊天记录，也不要靠固定前 N 个文件代表上下文。

## 3. 工具语义与执行环境是不同的替换点

`AgentTool` 具有名称、参数 schema、执行函数。执行函数接收调用 ID、参数、取消信号及进度回调，结果区分供模型读取的 content 与结构化 details。Agent loop 校验参数、执行工具，并将错误转成模型可观察的工具错误。依据：[工具类型](https://github.com/earendil-works/pi/blob/main/packages/agent/src/types.ts)。

工具内部的系统操作另有接缝：`BashOperations.exec` 接收命令、cwd、输出回调、signal、timeout、env；`ReadOperations` 提供读文件、可读性检查等操作。换 SSH 或容器时，可保留工具参数及结果格式。依据：[bash.ts](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/tools/bash.ts)、[read.ts](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/tools/read.ts)。

官方扩展文档给出 SSH/容器的 operations 替换方式，也规定大工具输出应截断并保留完整结果位置。依据：[扩展文档，Remote Execution / Output Truncation](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md#remote-execution)。

**建议：** YiPilot 的 Docker 应替换读、写、进程执行的位置，不应另造一套完整编码/验证业务。开发工具和验收命令可以走同一环境执行能力，但验收是否通过仍由确定性规则判断。权限检查放在所有真实执行都经过的边界，不仅依赖模型提示词或工具显示名称。

## 4. 成熟度藏在生命周期的细节里

| 细节 | Pi 的实际处理 | YiPilot 应明确的设计合同（建议） |
|---|---|---|
| 实时事件 | 模型流在读取时发 `message_update`，工具有 start/update/end | 网络首个 delta 能立刻传到订阅端；最终事件有明确含义 |
| 工具参数截断 | 输出 token limit 截断时，不执行可能残缺的工具调用 | 禁止对半截 JSON 或不完整补丁执行副作用 |
| 并行顺序 | 可并行执行工具，最终 tool-result 消息保留源调用顺序 | 并发读可放开；相互影响的写入必须串行；历史不能因完成顺序随机变化 |
| 执行前后 | 参数校验后有 before hook，可阻断；执行后可处理结果 | 策略与审计应围绕执行边界，不能 scattered 到每个 CLI 命令 |

以上事实来自 [agent-loop.ts](https://github.com/earendil-works/pi/blob/main/packages/agent/src/agent-loop.ts)。建议按现有需求实现必要合同，不必立刻搭建插件系统。

Agent 还提供 abort、waitForIdle、steer、followUp；当前已执行工具批次完成后才消费 steering，follow-up 在本轮原本要结束时消费。`agent_end` 的 awaited 订阅仍属于结算过程。依据：[agent-core README，Control / Steering](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)。

**建议：** 区分“提出取消”“底层已经停止”“状态和事件已落盘”。取消不意味着撤销已写入文件；恢复也不意味着盲目重放最后命令。

## 5. 会话持久化与压缩：历史不是 prompt

Pi coding-agent 的 `SessionManager` 用带 `id/parentId` 的 append-only JSONL 树保存记录。当前 leaf 选择有效分支；上下文重建处理压缩及摘要。扩展状态可以仅持久化，也可以显式参与模型上下文。依据：[SessionManager](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/session-manager.ts)、[Sessions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/sessions.md)。

Compaction 用摘要替代送入模型的旧内容，保留近期工作；自动阈值触发和上下文溢出恢复是不同情况。依据：[Compaction 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md)。

会话层对临时 Provider 错误采用有上限、可取消的退避；上下文溢出另走压缩恢复，不当成普通网络重试；失败消息可留在历史但移出重试上下文。依据：[AgentSession 的 Auto-Retry / Auto-Compaction](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/agent-session.ts)。

**建议：** 至少分开三种重试预算：Provider 暂时失败、Agent 任务修复、外部交付操作重试。第二种会改代码，第三种可能已在远端成功。不要以一个 retry_count 和一个 FAILED 覆盖所有情况。阶段结果应引用稳定工件与 commit，而不是依赖压缩摘要记住验收决定。

## 6. 哪些不适合照搬

Pi 明确不内置 plan mode、sub-agents、permission popups，鼓励文件、容器和扩展；它优先提供可组合的编码 Agent。Python 应用若要直接集成 Pi，可用官方 stdin/stdout RPC，但这会增加外部进程与运行时依赖。依据：[Pi README，Philosophy / RPC](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)。

**建议及边界判断：**

1. YiPilot 目标是研发交付系统，必须自己拥有“需求→设计评审→实现→测试→CI→合并”的阶段和恢复语义。Pi 会话树不能替代这些状态。
2. 不应照搬 Pi 的全部扩展机制、主题、会话分叉 UI、多模型切换配置。当前主线只需要稳定循环、阶段输入、工具执行和交付策略。
3. 不应照搬默认任意 shell 的执行权限。YiPilot 已有的工作区隔离、审批证据和路径约束属于产品要求，应收敛入口而不是删除。
4. 不建议为了借鉴而改成 TypeScript，或现在就接 Node subprocess。先在 Python 内建立相同边界；只有确实决定用 Pi 替代自研 runtime 时，才评估 RPC 成本。
5. 不应把“少文件”当成低耦合。判断标准是：换终端是否影响执行，换容器是否重写流程，改 PR 策略是否触及 Agent loop，恢复是否必须猜测 UI 曾做过什么。

## 7. 最小可学习结构

```text
CLI / 后台入口
    ↓
研发 Workflow（阶段推进、验收、预算、交付与恢复）
    ↓
阶段执行 / Session（上下文、历史、运行生命周期）
    ↓
Agent loop ─── Provider
    ↓
Tools ─── workspace / sandbox operations
```

这是针对 YiPilot 的建议，不是声称 Pi 已实现这条研发流程。应先把当前业务编排从终端移到统一应用入口，再让编码、评审、修复复用同一个循环和同一个环境边界；不要先按这幅图机械拆成六个新包。
