# SQLite 持久化实施与迁移映射

本轮只迁移当前已存在的本地状态，不代表 V2、多轮 Agent 或完整研发流程完成。

## 数据归属与写入路径

| 原数据、写入方 | 现在的唯一事实源 |
| --- | --- |
| config.yaml，TaskStore 项目登记与镜像配置 | state.db / project |
| tasks/*/task.yaml，TaskStore | tasks；保留 Task ID、创建时间、标题和目标 |
| operations/*.json，生成、应用、验证、修复服务 | operations；Task 作用域幂等键、冻结请求 JSON、SHA-256；数据库唯一约束 |
| runs/*/run.json，TaskStore / RunExecutor | runs；Operation、stage、attempt、状态、错误及最终工件 ID |
| repos/*/metadata.json，RepoService | records(kind=repo)；仓库 URL、缓存引用和基线 |
| artifacts/records/*.json，ArtifactStore | records(kind=artifact)；所有者、路径、哈希、输入指纹 |
| approvals/*.json，ApprovalStore / ProjectApplication | records(kind=approval)；原有目标、策略、决定和撤销标记 |
| tool-runs/*.json，ToolRunStore / Executors | records(kind=tool)；原有结构化结果及 stdout/stderr 工件引用 |
| events.jsonl，Journal / 各阶段 | events；原 Run sequence 保留，新增项目级单调 cursor |

records 复用现有校验合同，并按 kind 区分少量结构化记录，不为每种记录增加 Repository/Factory。表上的外键与请求唯一约束管理关联；业务读取仍核对 JSON 内的所有者和证据。没有用户模型配置、密钥、WaitItem、交付或多 Worker 空表。

正文、模型原始输出、补丁、stdout/stderr、报告、source/input manifest、Docker/Git 回执仍是文件。Git 缓存、worktree、执行副本仍是执行资源。原 ArtifactRef 的 ID、路径与哈希不变；不同时迁移为内容寻址目录。旧 FileTaskStore 等 Python 名称保留源代码兼容性，实际只有 SQLite 实现。JSONL 仅用于 CLI 渲染/导出；遗留 JSON/YAML 不再参与日常读取或写入。

## 提交、事件和恢复

sqlite3 使用显式 BEGIN IMMEDIATE / COMMIT / ROLLBACK，foreign_keys=ON、busy_timeout=5000、journal_mode=DELETE、synchronous=FULL。schema 通过 user_version 管理，建表在事务内；未知版本拒绝打开。连接按事务创建、关闭，同线程嵌套业务提交复用连接；不跨线程共享连接。

文件通过现有路径保护、原子发布和哈希校验完整落地后登记元数据。发布后的正文不允许用不同内容覆盖。Run 的最终工件 ID、状态和生命周期事件在同一短事务提交；提交前重新校验已登记工件。工具结果与 tool.completed、用户决定与审批事件也使用短事务。模型、Git、Docker 调用在这些事务以外执行。

进程在文件发布后、登记前退出，会留下未引用文件；保留供检查，不根据它推断成功，也不自动清理。数据库已提交后 UI 掉线，只回放事件，不重新执行。缺失文件、哈希不一致或损坏索引阻止放行。Run 序号保持原 CLI --after-sequence 合同；项目 cursor 是附加字段，消费者应允许未知字段。终态仅登记一次，订阅已读终态不会再次等待到超时。

项目 executor OS 文件锁保护现有服务的实际执行，maintenance 锁保护迁移期间的元数据写入。SQLite 写锁不代表执行所有权，不依据时间过期接管。遗留运行状态由显式恢复检查处理：模型中断记录 EXECUTION_INTERRUPTED；外部动作结果不能证明时记录 EXTERNAL_RESULT_UNCERTAIN，终止本次 Run，不自动重试。取消单独保留 cancelled，失败保留具体错误。没有恢复 Python 栈、自动断点续跑或分布式租约。

Docker 仍使用 Run 关联的容器与回执；Git 仍保存隔离目录、清理/结果和受控写入证据。操作系统锁释放不代表容器必已停止：不确定结果必须检查容器、进程和 worktree；不能重新提交原请求来重跑高风险动作。外部执行与 SQLite 不构成分布式事务。

| 恢复场景 | 允许的处理 |
| --- | --- |
| 已提交请求、事件读取或订阅重连 | 安全重放已有结果；不再调用模型、Git 或 Docker |
| 模型阶段进程中断 | 显式恢复登记 EXECUTION_INTERRUPTED；如需重新生成，提交新的请求，可能重复产生模型调用费用 |
| Docker/Git 动作中断或缺少完整回执 | 先核对 Run 对应的容器、进程、隔离目录和结果；无法证明结果时登记 EXTERNAL_RESULT_UNCERTAIN，等待人工处理 |
| 用户拒绝或取消 | 保留拒绝记录与 cancelled 状态；旧批准或重新订阅不能让它继续执行 |
| 孤立、缺失或哈希错误的工件 | 审计并保留现场；不以文件存在补记成功，不自动清除或接管执行 |

## 本机边界

活动数据库只能放在本机可靠磁盘；不支持 SMB/NFS、网络共享或 OneDrive/其他云同步数据库文件。备份需同时保留数据库及其引用文件。不要在线复制单个 state.db 来替代 SQLite backup API 或停机备份。当前迁移提供停机完整旧目录备份；持续在线备份与自动工件回收不在本次范围。

操作说明见 [迁移指南](../guides/sqlite-migration.md)。
