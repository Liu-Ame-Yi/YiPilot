> **Historical reference / Superseded — 文档整理：2026-09-07。** 本文保留旧阶段设计与实施记录，不再作为当前待办或产品范围。当前目标见 [产品定义](../../product/vision.md)，技术设计见 [V2](../specs/2026-09-07-yipilot-v2-architecture.md)，实际能力见 [状态页](../../product/status.md)。下文原状态、强制要求及命令仅在原历史范围内成立。

# Yipilot Python 测试、Jenkins CI 与 GitHub PR 集成方案

**状态：提案，待后续实施前确认**
**日期：2026-08-29**

## 1. 目的

Yipilot 保持为 Python 本地研发流程控制器。后续接入 Jenkins 和 GitHub
PR 时，目标是把需求、测试、本地运行和 CI 结果关联为可审计证据，不能让
Yipilot 绕过 CI 或人工审查直接合并主分支。

本方案借鉴原 zpilot 的流程形式：测试计划、被测代码、执行记录、报告和
审批必须可追溯；不引入 Go 测试器或跨语言任意命令执行器。

## 2. 已确认的技术选择

- 被测项目和 Yipilot 自身的第一版测试语言均为 **Python**。
- 唯一测试执行器为受策略限制的 **pytest**，不是用户输入的任意 shell
  命令。
- Jenkins 负责 PR 上的确定性 CI；GitHub 的分支保护和人工 Review 负责
  合并门禁。
- Yipilot 负责 Run、Artifact、Harness、审批和外部 CI 结果的证据关联。
- Yipilot 没有直接 merge `main` 的权限。

## 3. 目标工作流

```text
PRD / SRS / SDD / Test Plan
  -> Yipilot 在任务专属 worktree 中执行受控 pytest
  -> 保存测试报告、日志、Harness 结论和当前 commit SHA
  -> 人工批准推送 feature 分支（后续能力）
  -> 创建 GitHub Pull Request（后续能力）
  -> Jenkins 按 Jenkinsfile 运行同一套确定性 Python 检查
  -> Yipilot 导入 Jenkins 构建编号、commit SHA、报告摘要和结果
  -> GitHub 要求 CI 全绿和人工 Review
  -> 有权限的人或 GitHub Merge Queue 合并主分支
```

本地 pytest 与 Jenkins CI 是互补的：本地结果帮助开发阶段发现问题；Jenkins
针对 PR 的结果才是合并门禁。两者都必须绑定不可变的 commit SHA，不能用
旧报告证明新提交。

## 4. Python 测试分层

| 层级 | 当前目录/命令 | 目的 | CI 默认执行 |
| --- | --- | --- | --- |
| 静态检查 | `ruff check src tests` | 风格、错误模式与基础质量 | 是 |
| 类型检查 | `mypy src` | 静态类型一致性 | 是 |
| 单元测试 | `pytest tests/unit` | 领域规则、存储、Harness、适配器 | 是 |
| 集成测试 | `pytest tests/integration` | CLI、Git worktree、证据链和策略 | 是 |
| 验收测试 | M4 acceptance 测试 | 完整离线研发工件闭环 | 是，可单独 Job |
| 在线测试 | `pytest -m live` | 真实 Provider 的显式探测 | 否，需人工 opt-in |

当前仓库的 GitHub Actions 已执行 Ruff、mypy、pytest 和 `git diff --check`。
Jenkins 的首版 `Jenkinsfile` 应复用等价命令，不能因平台不同而静默减少门禁。

## 5. 测试证据合同

每次由 Yipilot 发起的受控 pytest 执行，至少记录：

- `task_id`、`operation_id`、`run_id` 与测试计划 Artifact；
- 被测仓库、分支、base SHA 和执行时的不可变 head SHA；
- 固定的 pytest argv、工作目录、超时、输出上限与审批引用；
- `pytest-report.json`、stdout、stderr、ToolRun 和 HarnessReport 的 Artifact
  引用及 SHA-256；
- 终态：通过、失败、超时、取消或因审批不足而暂停。

Harness 只验证这些确定性事实：计划/代码/报告是否属于同一 Run 和同一版本、
报告是否完整、测试是否超时、失败或跳过是否被正确表达。Harness 不声称代码
没有缺陷，也不取代 Code Review。

## 6. Jenkins 与 GitHub 的职责

### Jenkins

Jenkins 使用 Multibranch Pipeline 和版本化 `Jenkinsfile` 发现功能分支与 PR。
其职责是执行确定性检查、保存测试/覆盖率/扫描报告，并把构建结论回写到 PR。

Yipilot 后续可增加只读 `ci import`（名称待定）：按已登记的 commit SHA 和
Jenkins build URL 导入构建号、状态、报告摘要、外部链接和内容哈希。导入失败
不能修改 Git 状态，也不能把未知 SHA 的结果关联到 Run。

### GitHub Pull Request

`main` 和发布分支应由 GitHub 保护规则约束：

- 禁止直接 push、force-push 和删除；
- 必须经 Pull Request；
- 至少一位人工 reviewer 批准；关键目录可增加 Code Owners；
- Jenkins 必需检查成功，必要时要求分支同步最新基线；
- 所有 review 对话解决后才能合并；
- 新 push 改变 head SHA 后，旧 CI 和 Review 证据均视为过期。

Yipilot 可以显示和归档这些状态，但不得把自己的 Harness 结果视为绕过 GitHub
保护规则的凭据。

## 7. 分期实施

### Phase 1：稳定当前 Python 测试闭环

1. 保持现有 `quality pytest` 的固定 pytest 策略，不开放任意命令。
2. 明确把本地 pytest 报告与 workspace 的 head SHA 关联并在输出中展示。
3. 维持 live 测试默认关闭；CI 只运行离线测试。

**验收：** 任一测试报告能追溯到唯一的 Task、Run、测试计划和 commit SHA；
报告或 Git 证据被改动后，Harness 阻断后续动作。

### Phase 2：Jenkins 质量门禁与结果回流

1. 在一个 Python 示例仓库中编写 `Jenkinsfile`，运行 Ruff、mypy、pytest 和
   Git 格式检查。
2. 使用 Jenkins Multibranch Pipeline 处理 feature branch 和 GitHub PR。
3. 实现只读 CI 结果导入；结果必须绑定已登记 head SHA。

**验收：** PR 的 Jenkins 失败会阻止合并；Yipilot 能显示同一 SHA 的构建链接和
结果摘要；错误 SHA、过期构建或篡改报告不能被导入为通过。

### Phase 3：受审批的 push 和 PR 创建

1. 新增经审批的 feature branch push；禁止对 `main` 直接 push。
2. 新增 PR 创建与只读 PR 状态同步；PR body 只包含最小脱敏证据摘要和链接。
3. 对每个 push 使旧的 CI/Review 证据失效，等待新 head SHA 的结果。

**验收：** 缺失、过期或 SHA 不匹配的 Approval 均无法 push；Yipilot 无法执行
merge；GitHub 必需检查和人工批准仍是唯一合并条件。

### Phase 4：可选增强

- 覆盖率阈值、SAST、依赖漏洞、许可证和 SBOM 扫描；
- GitHub App webhook 驱动的 PR/Review/Check 状态回流；
- Merge Queue 支持；
- 受审批的 Jenkins 重跑。

这些均应作为独立规格实施，不能用“接入 CI”名义一次性扩大权限。

## 8. 明确非目标

- 不支持 Go、Node、Java 或通用任意命令 runner；
- 不让模型或 Yipilot 直接批准 PR、绕过 CI 或合并 `main`；
- 不将 Jenkins 凭据、GitHub token、完整日志或密钥写入项目状态和 Artifact；
- 不将 Docker 分发环境误称为 CI 或 Agent 执行沙箱。

## 9. 相关材料

- 当前质量实现：[quality_artifacts.py](../../../src/yipilot/workflow/quality_artifacts.py)
- 当前 Harness：[quality.py](../../../src/yipilot/harness/quality.py)
- 当前 GitHub Actions：[ci.yml](../../../.github/workflows/ci.yml)
- 调研依据：[Jenkins CI 与 GitHub PR 调研](../../research/2026-08-29-yipilot-ci-pr-integration.md)
