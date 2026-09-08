# 第一轮职责边界重构实施记录

实施：2026-09-07；最终核验：2026-09-08。范围按 [执行合同](../superpowers/plans/2026-09-07-responsibility-refactor.md)，以开始时已有未提交工作树为基础；没有 reset、clean、提交或推送。下述是本轮增量，不把工作树中原有文档、Docker 修复和删除的旧测试计成本轮成果。

## 当前职责 → 归属

| 迁移前 | 本轮实际归属 |
| --- | --- |
| CLI 与交互分别构造 CodeService | application/project_application.py 的 generate；共享模型选择与 CodeService |
| 交互串接提案、Docker、输出、写入与批准存储 | ProjectApplication.apply；候选重新读取并核对审阅哈希，内部业务服务执行与复核 |
| 交互筛选应用 Run、验证、捕获失败并调用修复 | ProjectApplication.applied_runs/verify；VerificationOutcome 明确区分失败、修复提案与交接 |
| application.AgentRuntime 的两种位置参数协议 | application/run_executor.py 的显式 RunExecutor；Run 开始/结束与中断归应用层 |
| application.ModelPlanner 和 PlannerPort 再导出 | 直接使用 runtime.model_planner 与 runtime.planner |
| application 中 repo/worktree/source/output/apply，infrastructure 中 Docker | workspace 下原文件名的六个实际能力模块 |
| sandbox_sources._plain_path 与 sandbox_verification._read 被跨模块调用 | security.paths.require_plain_path/read_evidence |
| Git 的私有错误摘要被 Docker 引用 | security.redaction.safe_stderr_summary，保留原脱敏规则 |
| 私有 fingerprint/输出结果/容器策略检查 | 所有者公开 fingerprint_model、validate_result、check_container 合同 |
| code/repair 中无类型的工件引用转换 | infrastructure.artifact_store.artifact_reference |

## 真实调用链与依赖

生成：CLI code generate / 交互 /run → ProjectApplication.generate → CodeService（准备源码与冻结模型上下文）→ RunExecutor（生命周期）→ runtime.AgentRuntime + ModelPlanner（模型/工具协议）→ 注入的证据/工具适配。

应用：交互 /apply 或 Python → ProjectApplication.apply → 重新核验提案 → SandboxCliService.prepare → 用户执行意图落盘 → DockerSandboxWorker → SandboxOutputService.inspect → 用户精确输出意图落盘 → collect → SandboxApplyService → 任务 worktree。

验证：交互 /verify 或 Python → ProjectApplication.verify → 已应用证据复核 → SandboxVerificationService.prepare/run/handoff；符合失败条件时由 RepairService 读取失败证据和预算，生成新的提案。新的提案仍需后续应用/验证。

允许依赖是 transport → application → runtime/workspace/现有适配。runtime 不导入 application、workspace、infrastructure 或 transport；workspace 不导入 application、runtime 或 transport。文件存储和模型供应商适配本轮保留 infrastructure，安全与 domain 是共享基础。AST 测试守护这些已实施约束。

ProjectApplication 装配当前业务服务，共享 ModelSelectionService、任务存储与 GitRunner，并向 RepairService 注入同一套应用/验证/输出服务。没有为每个类建立 Protocol/Factory。RegistryToolGateway、FileRuntimeEvidence 保留，因为它们分别转换真实工具执行与持久证据协议，不是兼容再导出。

## 保持与最小修正

- CLI 参数、命令、人工确认语义保留；新增 Python 入口不扩大 generate 的执行权限。
- 原文件 schema、Run/Operation ID、工件路径、批准指纹算法与 JSONL 事件格式不变。批准和拒绝由业务层保存，关联事件使用已有 stage.progress。
- Docker 参数、无网络策略、链接/路径保护、预算、输出哈希、worktree 和 Git 证据检查保留。没有 Docker → host fallback。
- 本次安全边界的最小修正：绑定 Run 与 Operation 后才开始模型执行；意图必须是 bool；应用重新读取审阅对象并检查哈希；模型循环被 KeyboardInterrupt 中断时结束 Run 为 cancelled，其他逃逸异常记录 failed 后继续抛出。没有引入自动重试或持久恢复。

## 验证

基线：改动前启动的 `.venv/Scripts/python.exe -m pytest` 为 **602 passed、41 skipped**；Ruff 与 mypy 均通过。环境没有 uv，全部使用仓库 .venv。

新增真实服务组合测试只替换 ModelSelectionService.model_port 和 DockerSandboxWorker.execute 外部边界。它执行真实模型规划、源码导出、批准存储、diff 检查、worktree 写入、验证判断、失败诊断、RepairService 和工件校验；覆盖失败修复后再次应用/验证，以及批准拒绝、执行失败、过期哈希和非法意图。CLI 与交互生成使用同一入口；另验证审阅期间输出被修改时不能写入 worktree。Docker receipt 是测试输入，不能证明真实 Docker 或目标测试执行成功。

迁移过程中发现并修正了旧模块导入、旧 mock-only 终端修复测试和私有 helper 依赖；不保留兼容壳来掩盖遗漏。额外 AST 对比 HEAD 的 27 个 CLI 命令函数参数签名无变化，4 个迁移模块记录字段无变化；7 份当前文档无失效本地链接。最终核验（2026-09-08）：

| 实际命令/检查 | 结果 |
| --- | --- |
| `.venv/Scripts/python.exe -m pytest` | **612 passed, 41 skipped，470.73 秒** |
| `.venv/Scripts/python.exe -m ruff check src tests` | All checks passed |
| `.venv/Scripts/python.exe -m mypy src` | 65 个源文件无问题 |
| `git -c core.safecrlf=false diff --check` | 退出码 0；仅本次命令关闭换行警告，不修改 Git 配置 |
| 顶级 / code generate / sandbox doctor / sandbox register 的 CLI help | 均退出码 0；生成参数和沙箱命令保持可用 |
| AST 依赖与签名、记录字段、文档链接、新文件空白检查 | 全部通过 |

没有运行真实模型、真实 Docker 容器或远端检查。41 个跳过项不计入通过的验收；测试边界替身只验证业务组合和证据校验路径。目录迁移中一度发现旧导入和旧终端 mock 路径，均已修正，以上完整测试是在这些修正之后重新运行的结果。

## 剩余耦合与后续入口

- ProjectApplication 是当前同步操作入口；不是完整 Workflow，也不提供 durable WaitItem、respond/resume、后台执行或取消命令。
- runtime.ModelPlanner 仍解析一次 PatchProposal，证据协议仍带任务关联；通用多轮 Agent 和阶段无关结果 schema 留待后续。
- 未纳入本轮研发执行链的配置/查询/历史质量命令仍沿用既有服务及具体适配；没有同步重写全部终端命令。
- 具体 FileTaskStore/FileArtifactStore 仍由部分下层服务内部创建。它们使用同一既有文件格式；没有为了全面注入而新增接口矩阵。
- 历史 CodeService.apply / VerificationService 仍在源码中，当前生产入口不调用它们；旧任务/工件读路径保留。它们不是 Docker 失败时的备用路径。
- 原有请求幂等输入覆盖、模型 token/byte 预算语义、文件状态与事件非事务化、验证适用仓库限制等问题仍参见 [代码缺口分析](../research/2026-09-07-yipilot-code-gaps.md)，本轮不混入完整行为修复。
- 后续完整工作流从 ProjectApplication 扩展应用操作，在应用层增加真实阶段/gate；模型与工具循环沿现有 runtime 协议演进，终端无需重新编排阶段。SRS/SDD 生产评审、SQLite、PR/MR、CI 和交接发布都尚未实现；自动合并不在产品范围。
