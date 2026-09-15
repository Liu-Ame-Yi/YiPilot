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

每个 `revise` 消耗相应文档的修订次数；`clarify` 保存问题并等待答案；`stop`、不合法输出或调用失败停止。答案绑定输入哈希和问题 ID，形成新输入工件，清除旧文档和评审的有效引用。新启动输入使前一工作流 `superseded`。历史快照与正文保留作审计，不作为当前下游输入。

状态查询会复核工件哈希。语义评审的独立性是独立调用/上下文；不承诺相同模型没有共同盲点。代码证据只覆盖明确选择的提交快照文件，没有动态代码检索、多轮工具 Agent 或自动补齐未知现状。

## Git 实施基线

开始时 HEAD 为 `634c5f5fae87831041030eca9a12edd154c581d3`，分支 `codex/live-github-deepseek-validation`，暂存区为空。工作树已有 SQLite、公共入口、代码路径、测试和文档修改，包含尚未跟踪的 `database.py`、`ownership.py` 等文件。本任务建立 `codex/requirements-design-loop`，以该工作树作为实际运行基线；未 reset、clean、stash 或提交这些既有改动。

本任务提交只包含需求设计闭环新增文件及入口增量，依赖保留在工作树中的既有 SQLite 迁移。提交本身不等于包含完整 SQLite 基线的独立发布版本，不能用 HEAD 单独 checkout 后的结果冒充本次已验证工作树。开始时的 tracked binary diff、未跟踪文件指纹与状态另存于本机临时目录 `yipilot-requirements-baseline`，用于分辨归属。

入口与操作约束见 [使用说明](../guides/specification-workflow.md)，测试事实见当前状态和真实验收记录。
