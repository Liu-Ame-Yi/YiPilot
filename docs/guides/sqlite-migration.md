# 迁移旧项目到 SQLite

新项目初始化会建立 `.yipilot/state.db`。已有 config.yaml 的旧项目不会自动导入；普通命令会报告 MIGRATION_REQUIRED。模型与密钥的用户级存储不变。

先对目标项目做只读扫描。此命令不初始化数据库，也不创建备份或锁：

```powershell
.venv/Scripts/yipilot.exe storage migrate --project D:\ly112233\agent --dry-run
```

检查输出的 format、counts、errors、warnings 和 source_hash。counts 是业务记录数；原始正文和执行资源逐文件计算哈希并保留。损坏记录、未知字段、无效冻结请求、矛盾或重复终态、无效所有者、缺失或哈希不符的工件都会报告；有错误不允许切换。未结束旧 Run 会被归档为 cancelled / LEGACY_INTERRUPTED，旧审批只保留历史并撤销执行资格；历史成功不作为新生成/应用的授权。

关闭所有旧版、新版 Yipilot 进程，并核对相关 Docker 容器、Git/工具进程已停止。迁移期间不要启动旧版程序；旧版程序不理解新版 maintenance 锁。然后显式执行：

```powershell
.venv/Scripts/yipilot.exe storage migrate --project D:\ly112233\agent --executors-stopped
```

`--executors-stopped` 是操作者对外部进程已停止的确认；工具还会取得项目执行锁、维护锁及存在的旧锁。活动锁冲突会拒绝迁移。此标记不代表程序能够证明任意遗留外部进程已停止。

迁移先生成 `.yipilot-backup-<source-hash>`，逐文件核验备份，再在独立 `.yipilot-migration-*` 临时项目转换、检查关联与工件、SQLite integrity_check / foreign_key_check，全部通过才发布 state.db。保留原 ID、时间、Run 内事件顺序、有效 ArtifactRef 和原始文件。报告含备份及临时目录位置。失败时不切换原目录，保留临时结果供排查；不要靠删除损坏记录使报告“变绿”。

迁移完成后再次执行只返回原迁移报告，不重复导入，不覆盖后续新增任务或执行历史。旧元数据保留但不再是事实源，修改它们不会影响 SQLite。未注册正文作为孤立文件保留，不凭文件存在宣称成功。

回滚仅适用于迁移后尚未接受新写入的停机状态：保留完整当前目录，恢复已核验旧备份，并使用匹配的旧版程序。SQLite 接受新写入之后，旧备份不含新增历史，不能直接切回；必须先保存新数据库及其工件，明确处理新增记录后再决定回滚。本工具没有无损反向同步。

转换、备份、故障及切换验收先使用临时项目与 fixture；随后经用户明确授权完成真实项目迁移，记录见本文末尾。测试结果以 [当前状态](../product/status.md) 为准。


工件检查命令：

```powershell
.venv/Scripts/yipilot.exe storage audit --project D:\ly112233\agent
```

它报告损坏引用及未登记正文，不删除、不登记孤立文件，不更新阶段状态。缺失的成功证据必须先排查，不能凭同名文件恢复放行。

兼容性：ArtifactRef 的 ID、路径、哈希及报告正文合同不变；事件增加可选项目 cursor，原 sequence 和 --after-sequence 保持。Store 类的旧名称和业务 get/list 接口保留；ApprovalStore/ToolRunStore.save 返回的旧逻辑定位符不再对应 JSON 文件，读取记录必须使用 get/list。旧批准新增 revoked 标记，迁移记录的 Run 新增 legacy 标记；不要用旧版 JSON 读取器解释新数据库。

临时 fixture 的 CLI dry-run 实测：项目、Task、Operation、Run、审批、工件各 1 条，事件 2 条，保留文件 8 个，errors 为空；警告为旧审批保留历史但撤销执行资格。dry-run 后数据库不存在；同一临时项目完成显式迁移后 audit 的 errors、orphans 均为空。此结果不代表真实项目已扫描。

真实项目 `D:\ly112233\agent` 的只读 dry-run（2026-09-08）：legacy-v1，project 1 条，保留文件 1 个，errors/warnings 均为空，没有 Task、Operation 或 Run。source_hash 为 `f7c6f515a4bb5ab148678c935ddd801246e723a56d68d3904ecd8b3602c5c310`。迁移前应重新扫描；若数据变化，以新的报告为准。

真实项目迁移完成（2026-09-09）：重新 dry-run 的 source_hash 与上述记录相同，随后显式执行 `--executors-stopped` 并成功切换。数据库为 `D:\ly112233\agent\.yipilot\state.db`，schema_version=1、journal_mode=delete；项目记录 1 条，其余业务表为空。完整性检查、外键检查及工件审计通过；应用读取配置与旧配置一致。

已核验备份：`D:\ly112233\agent\.yipilot-backup-f7c6f515a4bb5ab1`，旁边的 `.yipilot-backup-f7c6f515a4bb5ab1.manifest.json` 保存文件哈希。原始 `.yipilot/config.yaml` 仍保留，临时转换目录 `.yipilot-migration-qi271k4u` 保留供排查；这些运行数据均由 Git 忽略。本次没有新增业务任务或执行记录，也没有提交或推送 Git。后续开始业务写入后仍须遵守前述回滚限制。
