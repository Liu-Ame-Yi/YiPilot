# 当前代码与文件分级

日期：2026-09-08。范围：第一轮职责重构后的目录归位，保留现有执行行为；完整多轮 Agent、耐久工作流和远端交付仍未实现。

文件布局收尾：已核实并清除 src/yipilot 与 tests/unit 中 application、runtime、infrastructure 六个旧目录的缓存残留，磁盘上不再显示这些空旧分层。仅删除已确认的 Python 缓存；包边界测试重新通过，业务源码与数据未在此收尾中修改。

## 实际目录与所有权

```text
src/yipilot/
  app.py                  # ProjectApplication：当前公共入口与依赖装配
  workflow/               # 现有研发操作与Run生命周期
    code_service.py       # 提案生成
    repair_service.py     # 有限修复提案
    sandbox_application.py# 应用流程准备、执行与证据协调
    sandbox_verification.py
    run_executor.py
    project_service.py
    recovery_service.py
    quality_artifacts.py
    verification_service.py
  agent/                  # 现有单次规划/工具协议，尚非完整多轮Agent
    agent_runtime.py
    model_planner.py
    planner.py
    ports.py
  models/                 # 供应商协议、选择与用户模型设置
    selection.py
    service.py
    openai_compatible.py
    store.py
    secrets.py
    fixtures.py           # 现有离线质量演练及测试用模型，不是生产降级路径
  workspace/              # 代码/Git/工具执行；不决定业务阶段
    repo_service.py
    workspace_service.py
    sandbox_sources.py
    sandbox_output.py
    sandbox_apply.py
    docker_worker.py
    git_runner.py
    executors.py
    tool_registry.py
    tool_gateway.py
  persistence/            # SQLite 状态、文件工件、显式迁移与 OS 执行锁
    task_store.py
    artifact_store.py
    approval_store.py
    tool_run_store.py
    event_journal.py
    runtime_evidence.py
    filesystem.py
  security/               # 可信批准校验、路径与脱敏
    approval.py
    paths.py
    redaction.py
  domain/                 # 稳定类型、状态及数据合同
  harness/                # 确定性质量/证据检查
  transport/cli/          # Typer、交互和事件显示
```

先按已有能力分组，不创建 delivery、stages 或新状态机空文件。不将工作区代码拆得更碎；目录归位不改变存储文件位置、状态字符串、批准指纹或CLI参数。

## 已实施依赖约束

- transport 调 app/已有管理入口；app 组合 workflow、models、workspace 及持久化。
- workflow 处理业务规则，可调用 agent、workspace、models、harness 和持久化存储。
- agent 仅使用领域类型和注入协议，不导入 app、workflow、workspace、models实现或persistence。
- workspace 不导入 app、workflow 或 agent；工具网关通过已有工具合同适配。
- models/persistence/security/domain 不导入 app、workflow、workspace 或 agent。
- 所有核心包不导入 transport；旧 application/runtime/infrastructure 下不保留兼容Python文件。

security.approval 仍使用批准存储，models.store 复用持久化文件原语，harness 仍校验实际证据。这是已实现的适配依赖，不宣称所有包都已完全解耦；不能将 package 之间的所有交叉引用一概视为错误。

检查见 [包边界测试](../../tests/unit/architecture/test_module_boundaries.py)。测试先核实扫描目录非空，避免移动目录后空扫描假通过。

## 迁移对应关系

下表路径相对于 src/yipilot；左列为本轮开始时的路径。移除旧导入路径，本项目源码、测试及monkeypatch目标同步迁移；若外部脚本曾直接导入内部模块，需要改用新路径。公开CLI与数据格式不变。

| 迁移前 | 当前路径 |
| --- | --- |
| `application/project_application.py` | `app.py` |
| `application/model_selection_service.py` | `models/selection.py` |
| `application/provider_service.py` | `models/service.py` |
| `application/approval.py` | `security/approval.py` |
| `application/tool_registry.py` | `workspace/tool_registry.py` |
| `application/runtime_adapters.py` | `workspace/tool_gateway.py` |
| `infrastructure/executors.py` | `workspace/executors.py` |
| `infrastructure/git_runner.py` | `workspace/git_runner.py` |
| `infrastructure/openai_compatible.py` | `models/openai_compatible.py` |
| `infrastructure/model_port.py` | `models/fixtures.py` |
| `infrastructure/provider_store.py` | `models/store.py` |
| `infrastructure/secret_store.py` | `models/secrets.py` |
| `application/code_service.py` | `workflow/code_service.py` |
| `application/repair_service.py` | `workflow/repair_service.py` |
| `application/recovery_service.py` | `workflow/recovery_service.py` |
| `application/project_service.py` | `workflow/project_service.py` |
| `application/quality_artifacts.py` | `workflow/quality_artifacts.py` |
| `application/run_executor.py` | `workflow/run_executor.py` |
| `application/verification_service.py` | `workflow/verification_service.py` |
| `application/sandbox_verification.py` | `workflow/sandbox_verification.py` |
| `application/sandbox_cli.py` | `workflow/sandbox_application.py` |
| `runtime/agent_runtime.py` | `agent/agent_runtime.py` |
| `runtime/model_planner.py` | `agent/model_planner.py` |
| `runtime/planner.py` | `agent/planner.py` |
| `runtime/ports.py` | `agent/ports.py` |
| `infrastructure/task_store.py` | `persistence/task_store.py` |
| `infrastructure/artifact_store.py` | `persistence/artifact_store.py` |
| `infrastructure/approval_store.py` | `persistence/approval_store.py` |
| `infrastructure/event_journal.py` | `persistence/event_journal.py` |
| `infrastructure/filesystem.py` | `persistence/filesystem.py` |
| `infrastructure/runtime_evidence.py` | `persistence/runtime_evidence.py` |
| `infrastructure/tool_run_store.py` | `persistence/tool_run_store.py` |

tests/unit 按 agent、models、persistence、workspace、architecture 等实际归属整理；integration 保持用户流程维度，不按实现目录机械拆分。

## 验证与范围

迁移前包边界和 ProjectApplication 组合测试通过。迁移后完整 pytest：612 passed、41 skipped，477.66 秒；Ruff 通过，mypy 66 个源文件通过，CLI help、66 个模块的静态绝对导入无环检查和 git diff --check 通过。51 份 Markdown 的本地链接检查通过。跳过项为需显式镜像的真实 Docker、需显式启用的真实模型，以及 Windows 不支持的文件名情景；没有运行远端写操作。这些结果证明本轮目录迁移的离线验证，不代表 V2 功能完成。

历史文档链接已指向当前文件；历史正文中的旧目录名仍作为当时记录，不倒写历史。长期目标见 [V2](../superpowers/specs/2026-09-07-yipilot-v2-architecture.md)，当前能力见 [状态页](../product/status.md)。


## SQLite 持久化补充

目录分级保持不变。在 persistence 内增加 database.py、migration.py 和 ownership.py，并替换现有 Store 的实现；没有新建一套按表拆分的 Repository/Factory。RepoMetadataRecord 归入现有持久化合同，仓库服务与迁移工具共用，保持 persistence 不导入 workspace。

数据库表、写入方、事务、事件游标和恢复边界见 [SQLite 实施映射](sqlite-persistence.md)。上述 612 测试记录属于先前目录迁移；本轮验收以 [当前状态](../product/status.md) 的 SQLite 记录为准。
