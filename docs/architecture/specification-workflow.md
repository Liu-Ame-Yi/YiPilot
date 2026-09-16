# 需求与设计闭环：当前实现

2026-09-12。本页描述实际代码，不替代完整 V2 目标架构。

调用链：CLI specification → ProjectApplication → SpecificationService → 现有 ModelSelectionService/ModelPort。SpecificationService 复用 FileTaskStore 的 SQLite、FileArtifactStore、Run 生命周期事件、exclusive_execution、RepoService 与 SandboxSourceService；没有新增数据库、模型客户端、工作流框架或 Agent Runtime 循环。

2026-09-15 验证修正：将原代码生成路径的共用上下文读取函数公开命名为 read_context_files，两个调用点复用同一实现，符合仓库禁止跨模块私有导入的检查；没有复制读取逻辑或改变原代码生成的权限边界。

## 记录和事务边界

- 现有 `operations` 保存启动和澄清请求的幂等键、输入指纹；原始正文保存为工件，不写入幂等键。
- 现有 `runs` 为输入、每次生成和每次评审保存尝试、状态及生命周期事件。
- 现有 `records` 中 `kind=specification` 保存递增序号的不可变状态快照。状态包含输入引用、阶段、模型公开快照、文档版本、修订次数、文档/评审/已接受引用、待决问题和停止原因。
- 文档和评审正文使用 Run 级不可变工件。工件先写入并校验哈希，Run 成功、工件索引和工作流阶段状态在同一 SQLite 事务提交。回滚不发布内存中的验收状态；未引用文件按已有 orphan 审计规则处理。
- 外部模型调用前提交 `calling` 与 Run running；成功后校验输出再原子提交下一阶段。调用时不持有数据库事务，持有现有执行锁防止两个实例推进同一项目。

模型配置在第一次调用冻结；继续时通过原 selector 解析凭据、对比公开配置，配置变化停止。凭据值不进入状态或提示。响应在已有 ModelPort 内有字节/时间预算，工作流也复核输出预算，不扩大预算绕过失败。

输入留存不是外部模型调用。输入捕获未完成时 Run 保存失败或中断状态，需要新请求重新捕获；此时没有新的已提交输入边界。已提交输入之后，任何生成/评审边界均可继续。模型调用中断后的结果不确定，不保证外部恰好一次；恢复停止且保留调用 Run，明确要求新请求重试。

## 放行与失效

SRS/SDD 和 Review 使用严格 Pydantic 合同，拒绝额外字段和不合法引用。需求评审必须引用所有用户材料；设计评审必须引用接受的 SRS 和实际提供的所有代码证据。评审文档哈希必须等于本次文档，`accept` 不得同时带任何阻塞问题，未决问题不能通过。SDD 只能读取状态中的已接受 SRS，测试映射必须精确覆盖其验收 ID。

每个 `revise` 消耗相应文档的修订次数；`clarify` 保存问题并等待答案；`stop`、放行规则校验失败或调用失败停止。2026-09-16 起，正常完整响应中的格式错误允许每阶段一次持久化修复，详见下文。答案绑定输入哈希和问题 ID，形成新输入工件，清除旧文档和评审的有效引用。新启动输入使前一工作流 `superseded`。历史快照与正文保留作审计，不作为当前下游输入。

状态查询会复核工件哈希。语义评审的独立性是独立调用/上下文；不承诺相同模型没有共同盲点。代码证据只覆盖明确选择的提交快照文件，没有动态代码检索、多轮工具 Agent 或自动补齐未知现状。

## Git 实施基线

开始时 HEAD 为 `634c5f5fae87831041030eca9a12edd154c581d3`，分支 `codex/live-github-deepseek-validation`，暂存区为空。工作树已有 SQLite、公共入口、代码路径、测试和文档修改，包含尚未跟踪的 `database.py`、`ownership.py` 等文件。本任务建立 `codex/requirements-design-loop`，以该工作树作为实际运行基线；未 reset、clean、stash 或提交这些既有改动。

本任务提交只包含需求设计闭环新增文件及入口增量，依赖保留在工作树中的既有 SQLite 迁移。提交本身不等于包含完整 SQLite 基线的独立发布版本，不能用 HEAD 单独 checkout 后的结果冒充本次已验证工作树。开始时的 tracked binary diff、未跟踪文件指纹与状态另存于本机临时目录 `yipilot-requirements-baseline`，用于分辨归属。

入口与操作约束见 [使用说明](../guides/specification-workflow.md)，测试事实见当前状态和真实验收记录。

## 2026-09-16 流式边界与阶段合同

参考 pi 的 [消息类型](https://github.com/earendil-works/pi/blob/main/packages/ai/src/types.ts) 和 [OpenAI adapter](https://github.com/earendil-works/pi/blob/main/packages/ai/src/api/openai-completions.ts)，采用正文/思考/工具信息分离、显式 stop reason、token 用量回执。仅改造现有 OpenAICompatibleModelPort，保留 `stream(request) -> tuple[ModelChunk, ...]`；新增可选 `ModelCompletion` 诊断由 ModelPlanner 与 SpecificationService 持久化，fake/replay 返回 null。没有引入 pi 依赖或另一套 Agent Runtime。

正文先按 UTF-8 字节有界收集，正常结束后统一脱敏、切分旧 ModelChunk，避免秘密跨 SSE delta 时漏脱敏。思考只计数不保存，工具调用在这条只读文档/提案通道明确拒绝。分别检查传输量、单事件、思考量、正文量和既有超时/取消。`max_output_tokens` 与旧 `output_limit`（字节）分开；16 MiB/256 KiB/1 MiB 是 Yipilot 的本地保护上限，不声称是 pi 的参数。

阶段之间不传递供应商原始流或思考历史，而是传递已验证、带版本的 JSON 工件及 SHA-256 引用：原材料 → SRS → Review → accepted.srs → SDD → Review → accepted.sdd。schema 随提示发送；程序校验结构及业务约束后才提交版本。下游只消费当前 accepted 引用，已有失效、澄清和 revision 逻辑不变。

格式修复有独立预算（每阶段每 flow 一次）。正常完整但格式错误的响应先保存 response、只含错误类别和字段路径的诊断、失败 Run 和 ready 状态，再开始新 Run；恢复可从这一已提交边界继续。新提示重带权威材料、schema 和失败回执，让模型重新生成符合格式的完整对象。未知版本和语义放行错误不修复；不拼接截断 JSON，也不把网络未知结果当成收到非法正文。任何 calling 状态的进程中断仍停在不确定结果。

pi 的 [compaction](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/compaction.md) 处理长会话输入窗口，不能代替输出/传输预算。本轮没有增加自动摘要、动态代码检索或分章节生成；现有显式代码证据和 32 KiB 提示上限保留。扩展这些能力需单独定义证据完整性与分段验收，不能静默丢材料。

本轮分支 `codex/model-stream-contracts`，起点 `e2e0f63`，索引为空；沿用上节未提交 SQLite 工作树作为实际基线。新的初始 diff/status 保存于本机临时目录 `yipilot-stream-contracts-baseline-20260916`。本轮提交不包含这些既有修改。
