# 当前版本快速开始

核对：2026-09-07 当前工作树。这里只描述实际入口，完整自动研发仍是 [目标](../product/vision.md)。

## 安装与任务

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\Activate.ps1
yipilot --version

yipilot init --project C:\work\yipilot-demo
yipilot task init --project C:\work\yipilot-demo --taskid 1001
yipilot task repo add --project C:\work\yipilot-demo --taskid 1001 --url C:\src\sample --alias sample --sync
yipilot --project C:\work\yipilot-demo --settings-dir C:\work\yipilot-settings
```

sample 必须是有效 Git 仓库。当前产品每个任务只使用一个仓库；管理状态保存在 project 的 .yipilot 中，不提交到业务仓库。交互 /task 也可创建带标题和目标的任务；仓库仍需通过 task repo add 注册到对应 taskid。

## 模型与提案

使用 /model 配置连接和模型。输入密钥后向导会自动发起小型联网探测；命令行 settings model test 默认只本地检查，加入 --request 才联网。详见 [Provider 配置](provider-configuration.md)。

/task 选择任务，/run 选择上下文文件并确认发送后生成 PatchProposal，不修改源码。也可以使用真实子命令：

```powershell
yipilot code generate --project C:\work\yipilot-demo --taskid 1001 --alias sample --instruction "add validation for empty input" --model <connection-id>/<model-id> --context-file src\service.py --settings-dir C:\work\yipilot-settings --output json
```

替换模型和文件占位值。当前仅发送显式选择的受限文件及可用的已通过任务文档；不会自动遍历仓库理解全部上下文。生成失败应查 Run 证据，不能将失败提案视为可应用结果。

## Docker 应用与验证

应用前需要本机 Docker Desktop Linux 模式及本地已准备的兼容镜像；doctor/register 不负责自动 build/pull。将占位值替换为实际完整镜像 ID：

```powershell
yipilot sandbox doctor --image-digest <sha256:image-id>
yipilot sandbox register --project C:\work\yipilot-demo --image-digest <sha256:image-id>
```

回到同一项目会话：/apply 选择提案，确认 Docker 执行及可信输出后写入任务工作区；/verify 选择成功应用记录并确认验证。镜像或策略检查失败时不会回退到宿主机执行。

验证失败可能生成新的有限修复提案；需要继续 /apply 和 /verify，并非已经自动完成修复闭环。通过时保存 Change Handoff；当前不会自动 commit/push 或创建 PR/MR。

查询记录使用 run list、run show、run watch，参数以各命令 --help 为准。交互 /logs、/cancel 尚未实现。不要照搬历史 --planner 或 code apply --prepare-only 命令。
