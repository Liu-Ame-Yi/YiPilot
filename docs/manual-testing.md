> **Historical reference / Superseded — 文档整理：2026-09-07。** 下文是旧 fake/replay/prepare-only 验收记录，包含当前已移除命令，不再作为可执行教程。请使用 [当前快速开始](guides/getting-started.md)；没有用本次文档整理重新执行旧实验。

# 手动测试指南

**标签：Implemented（已实现）。** 本文只测试当前已实现的本地 CLI
流程。它不测试真实 AI 写代码、commit/push、Jenkins、创建 PR 或合并，因为
这些能力尚未实现。

## 此测试能证明什么

本流程会创建一个一次性的本地 Git 仓库，并验证 Yipilot 能够创建任务
worktree、保留 Run 证据、产生离线 fake Change Handoff、生成质量工件，以及
保持源检出不变。测试结束后可安全删除临时目录。

## 前置条件

- Windows、Git 与 Python 3.11+。
- 一个干净的 Yipilot 检出。
- 从 Yipilot 仓库根目录执行下列命令。

先把当前仓库以可编辑方式安装到虚拟环境，并激活它。后续命令均使用真实的
`yipilot` 控制台入口：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\Activate.ps1
yipilot --version
```

预期：最后一条命令输出 Yipilot 版本。请在同一个已激活的 PowerShell 窗口中
完成后续测试。

## 1. 创建一次性源仓库

```powershell
$manualRoot = Join-Path $env:TEMP "yipilot-manual-$([guid]::NewGuid())"
$sourceRepo = Join-Path $manualRoot 'source'
$projectRoot = Join-Path $manualRoot 'project'

New-Item -ItemType Directory -Path $sourceRepo, $projectRoot -Force | Out-Null
git -C $sourceRepo init
git -C $sourceRepo config user.name 'Yipilot Manual Test'
git -C $sourceRepo config user.email 'manual-test@example.invalid'
Set-Content -LiteralPath (Join-Path $sourceRepo 'README.md') -Value '# Manual fixture'
git -C $sourceRepo add README.md
git -C $sourceRepo commit -m 'Initial fixture'

$sourceHeadBefore = git -C $sourceRepo rev-parse HEAD
$sourceStatusBefore = git -C $sourceRepo status --porcelain
```

预期：`sourceHeadBefore` 包含一个 commit SHA，`sourceStatusBefore` 为空。

## 2. 初始化 Yipilot 并准备任务 worktree

```powershell
yipilot init --project $projectRoot
yipilot task init --project $projectRoot --taskid 1001
yipilot task repo add --project $projectRoot --taskid 1001 --url $sourceRepo --alias sample --sync
yipilot task workspace prepare --project $projectRoot --taskid 1001 --alias sample --run-id manual-workspace
```

预期：所有命令成功；Yipilot 在 `$projectRoot\.yipilot` 下保存本地状态；源
仓库仍是测试 fixture，而任务拥有独立 worktree。

## 3. 测试离线 Change Handoff 路径

运行确定性的 fake 计划，回放其持久化事件，然后准备一个不应用代码的
handoff：

```powershell
$codeRun = (
  yipilot code generate --project $projectRoot --taskid 1001 --alias sample `
    --instruction 'check the local workspace' --planner fake --output json |
  ConvertFrom-Json
).run_id

yipilot run watch --project $projectRoot --run-id $codeRun --after-sequence 0 --output jsonl

$applyRun = (
  yipilot code apply --project $projectRoot --taskid 1001 --run-id $codeRun `
    --prepare-only --request-id manual-handoff-1001 --output json |
  ConvertFrom-Json
).run_id

$handoff = Join-Path $projectRoot ".yipilot\tasks\1001\runs\$applyRun\artifacts\prepare-only-handoff.json"
Test-Path -LiteralPath $handoff
Get-Content -Raw -LiteralPath $handoff
```

预期：

- `run watch` 输出持久化 JSONL 事件并结束。
- `Test-Path` 返回 `True`。
- handoff 包含 Run、worktree、Git 证据及关联的 Artifact/ToolRun/Harness 引用。
- 命令不应用 diff、不 commit、不 push、不 merge，也不修改远端。

确认源检出没有变化：

```powershell
$sourceHeadAfter = git -C $sourceRepo rev-parse HEAD
$sourceStatusAfter = git -C $sourceRepo status --porcelain

if ($sourceHeadAfter -ne $sourceHeadBefore) { throw 'Source HEAD changed.' }
if ($sourceStatusAfter -ne $sourceStatusBefore) { throw 'Source working tree changed.' }
Write-Host 'PASS: source checkout is unchanged.'
```

预期：输出 `PASS: source checkout is unchanged.`。

## 4. 测试离线质量工件

```powershell
$qualityRun = (
  yipilot quality generate --project $projectRoot --taskid 1001 `
    --prd 'Build a redacted offline quality workflow.' --model replay --output json |
  ConvertFrom-Json
).run_id

yipilot quality harness --project $projectRoot --taskid 1001 --run-id $qualityRun --kind srs --output json
yipilot quality harness --project $projectRoot --taskid 1001 --run-id $qualityRun --kind sdd --profile-revision 1 --output json
Get-ChildItem -Recurse -File (Join-Path $projectRoot ".yipilot\tasks\1001\runs\$qualityRun")
```

预期：该 Run 包含 PRD、SRS、SDD 与 test-plan 证据，两个 Harness 命令都返回
通过的 JSON 报告。`replay` 离线运行，不会调用真实 Provider。

## 5. 验证受控 pytest 的 Approval 边界

下列命令只准备允许执行的精确 pytest 请求并返回 approval target；它不会执行
pytest：

```powershell
yipilot quality pytest --project $projectRoot --taskid 1001 --alias sample --prepare --output json
```

预期：JSON 给出 Run 与 approval target fingerprint。标准本地 CLI 有意不提供
“授予 Approval”的命令。

要实际执行 pytest，已获授权的外部审批流程必须先为相同 Run 和 fingerprint
持久化一个 Approval，并提供其 ID 与匹配的 confirmation token。之后才可运行：

```powershell
yipilot quality pytest --project $projectRoot --taskid 1001 `
  --run-id <pytest-run-id> --approval-id <approval-id> `
  --confirmation-token <confirmation-token> --output json
```

在 Approval 有效时，预期：Yipilot 只会在任务 worktree 内运行已注册的
`<configured Python> -m pytest` argv，并持久化有上限的 stdout/stderr、pytest
报告和当前 Git 证据。它不接受任意 shell 命令或 pytest 参数。

## 可选：Provider 连通性探测

这不是离线流程的必要步骤。先使用 `settings model configure` 或交互式
`yipilot --settings-dir <settings-directory>` → `/model` 配置一个专门的测试模型：
首次没有模型时服务商选择器会直接开始；已有模型时选择“添加模型服务商”。使用方向键和
Enter 进行选择，Esc 返回。模型列表和本地校验使用：

```powershell
yipilot settings model list --settings-dir <settings-directory> --output json
yipilot settings model test <connection-id>/<model-id> --settings-dir <settings-directory> --output json
```

要显式发送最小非流式网络探测，在 `/model` 选择“测试或删除已有模型”、选定模型后选择
“测试连接”，或执行：

```powershell
yipilot settings model test <connection-id>/<model-id> --request --settings-dir <settings-directory> --output json
```

它不会上传仓库源代码、`.git`、进程环境或任务 Artifact。模型设置与密钥处理
要求见 [模型与 Provider 配置](guides/provider-configuration.md)。

## 清理

先关闭所有使用该 fixture 的进程，然后只删除本流程生成的临时目录：

```powershell
Remove-Item -LiteralPath $manualRoot -Recurse
```

该 fixture 位于系统临时目录；不要把这条清理命令用于真实仓库路径。
