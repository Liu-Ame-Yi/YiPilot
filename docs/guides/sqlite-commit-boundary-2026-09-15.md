# SQLite 提交边界核对（2026-09-15）

> 执行结果（2026-09-16）：已在独立工作区 `.worktrees/sqlite-persistence` 完成拆分、验证并提交 `68df1a4`（`codex/sqlite-persistence`）。共 52 个文件；636 passed / 41 skipped，Ruff、mypy（69 文件）、CLI help、文档本地链接及暂存区差异检查通过。独立工作区干净，main 未合并，未推送。下文保留提交前的边界分析与失败尝试记录；其中“尚未产生独立候选”已由本次执行解决。主工作区源码与原修改未改动，仅本说明补记提交结果。


本次只整理提交边界，没有暂存、提交、合并或改写历史。

## Git 现状与依赖

main 和 codex/sqlite-persistence 指向 634c5f5。当前 codex/requirements-design-loop 已有 faf0540（需求设计流程）和 e2e0f63（对应文档），SQLite 仍在未提交工作区。

faf0540 的 specification_service.py 已调用 store.db.save_record、store.db.transaction 和 find_operation，依赖尚未提交的 SQLite 实现。不能把该提交在不含 SQLite 的 main 上独立交付。后续流程不属于本次 SQLite 提交。

## 推荐提交顺序

1. 从 634c5f5 的独立工作区整理一个完整 SQLite 功能提交：数据库、现有 Store 接入、所有实际调用方的事务/幂等/恢复修正、迁移 CLI、配套测试与迁移文档一起提交。建议标题：feat(persistence): migrate existing project state to SQLite。不要拆成仅数据库层的不可用中间提交。
2. 单独提交 GitHub Actions 方向调整及架构图；它们不是 SQLite 实现。
3. 单独提交真实服务验收记录及状态摘要。
4. 将已有需求设计代码与文档提交接在 SQLite 之后，解决共享文件冲突并重新验证。保留原分支和提交作为参照，不直接重写或重置当前工作区。

## SQLite 代码与测试候选清单

以下是当前未提交的源码/测试路径。提交时仅取工作区相对 HEAD 的 SQLite 变化，不能直接将当前文件整体覆盖到 main，否则会带入已提交的需求设计功能。

- `src/yipilot/app.py`
- `src/yipilot/domain/execution.py`
- `src/yipilot/harness/git_workspace.py`
- `src/yipilot/persistence/approval_store.py`
- `src/yipilot/persistence/artifact_store.py`
- `src/yipilot/persistence/event_journal.py`
- `src/yipilot/persistence/task_store.py`
- `src/yipilot/persistence/tool_run_store.py`
- `src/yipilot/transport/cli/app.py`
- `src/yipilot/workflow/code_service.py`
- `src/yipilot/workflow/project_service.py`
- `src/yipilot/workflow/quality_artifacts.py`
- `src/yipilot/workflow/recovery_service.py`
- `src/yipilot/workflow/repair_service.py`
- `src/yipilot/workflow/run_executor.py`
- `src/yipilot/workflow/sandbox_application.py`
- `src/yipilot/workflow/sandbox_verification.py`
- `src/yipilot/workflow/verification_service.py`
- `src/yipilot/workspace/executors.py`
- `src/yipilot/workspace/repo_service.py`
- `src/yipilot/workspace/sandbox_output.py`
- `tests/integration/test_cli_run_watch.py`
- `tests/integration/test_code_harness.py`
- `tests/integration/test_controlled_pytest_harness.py`
- `tests/integration/test_live_patch_proposal.py`
- `tests/integration/test_model_port_events.py`
- `tests/integration/test_project_application.py`
- `tests/integration/test_quality_artifacts.py`
- `tests/integration/test_repo_sync.py`
- `tests/integration/test_sandbox_sources.py`
- `tests/unit/harness/test_git_workspace.py`
- `tests/unit/harness/test_task_integrity.py`
- `tests/unit/models/test_provider_secrets.py`
- `tests/unit/persistence/test_approval_tool_runs.py`
- `tests/unit/persistence/test_artifact_store.py`
- `tests/unit/persistence/test_event_journal.py`
- `tests/unit/persistence/test_task_store.py`
- `tests/unit/workspace/test_tool_registry.py`
- `src/yipilot/persistence/database.py`
- `src/yipilot/persistence/migration.py`
- `src/yipilot/persistence/ownership.py`
- `tests/helpers/sqlite_records.py`
- `tests/integration/test_sqlite_migration.py`

## 文档与忽略规则

- 整体纳入：.gitignore 新增的迁移备份/临时目标忽略项；docs/README.md；docs/architecture/code-layout.md；新增 docs/architecture/sqlite-persistence.md、docs/guides/sqlite-migration.md。
- 按片段纳入：docs/guides/README.md 的 SQLite 指南链接；docs/product/status.md 的 SQLite 迁移与对应验收段；roadmap.md 的 SQLite 状态；V2 规格的持久化章节及 SQLite 实现状态说明。
- 留给其他提交：vision.md 的 GitHub Actions 决策、V2 规格的 CI 部分、yipilot-v2 架构图及截图、live-acceptance-2026-09-11.md 和相应状态/索引、需求设计流程文档。
- 排除运行数据：.yipilot、备份、迁移临时目录、.pytest_cache、.codegraph，不加入本次提交。

## 共享文件与拆分验证

app.py、transport/cli/app.py、workflow/code_service.py 同时被后续需求设计提交修改；status.md 同时承载多轮进度。尤其 app.py 的 SQLite 装饰器补丁上下文已包含 specification 方法，需要在独立候选中人工重建 SQLite 片段。

已在临时导出的 634c5f5 副本执行常规 git apply --check：app.py 补丁不能直接应用。无上下文补丁虽然能被 Git 接受，但装饰器位置错误，因此该方案已否决；反向剥离需求设计补丁也在 app.py 上发生上下文冲突。临时副本不是可提交候选，不得复制回正式工作区。原工作区源码与 Git 索引未更改。

下一步应在隔离工作区以 main 为基线，重建上述三个共享源码文件的 SQLite 变化，并确认没有 SpecificationService 等新功能引用，再加入其余 SQLite 文件。随后运行迁移专项、ProjectApplication 组合测试、完整离线 pytest、Ruff、mypy、CLI help、diff --check，检查暂存内容和敏感文件后才提交。

历史 636 passed / 41 skipped 证明当时 SQLite 工作树的验收；不等于拆分候选已验证。本次尚未产生或验收可直接提交的独立候选。
