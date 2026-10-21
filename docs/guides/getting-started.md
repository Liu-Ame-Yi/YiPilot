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

## OpenSandbox 应用与验证

本地业务代码已改为 Runner；当前版本的 CI 与服务器 G4 验收尚未完成。本节新命令用于部署该版本之后，不能视为服务器已经升级。

本机不再安装 Docker。服务器运行固定 OpenSandbox profile 和 Runner，本机通过 SSH 隧道连接回环服务；在独立终端持续运行：

```powershell
ssh -N -L 127.0.0.1:18090:127.0.0.1:8090 huawei-opensandbox
```

使用独立 Runner key，不能用 OpenSandbox 管理 key；不要把密钥放到项目、聊天或命令参数。可使用隐藏输入写入当前用户的 SecretStore keyring：

```powershell
python -c "from getpass import getpass; from yipilot.models.secrets import SecretStore; SecretStore().set_keyring('runner', getpass('Runner key: '))"
yipilot sandbox configure --endpoint http://127.0.0.1:18090 --credential-ref keyring:runner --settings-dir C:\work\yipilot-settings
yipilot sandbox doctor --settings-dir C:\work\yipilot-settings --output json
```

配置和交互会话使用同一 settings-dir。配置只保存 endpoint 和密钥引用，也支持已有 env:/file: 引用；公网端点必须使用 HTTPS。doctor 是只读检查，核验认证、调度、OpenSandbox 健康和缓存镜像，不创建沙箱。

/apply 选择提案，审批展示 Runner、固定 profile/镜像和 512 MiB 任务预算；可信输出 diff 再次批准后写入新的任务工作区。/verify 审批三个独立验证请求，按全局预算排队执行。任务不临时联网安装依赖，不会缩小预算或回退到宿主机；额外依赖需要固定新镜像并验收。

验证失败可能生成新的有限修复提案；需要继续 /apply 和 /verify，并非已经自动完成修复闭环。通过时保存 Change Handoff；当前不会自动 commit/push 或创建 PR/MR。

查询记录使用 run list、run show、run watch。连接丢失后，本地 Run 保留 paused；`run recover --project <path> --run-id <id> --settings-dir <settings>` 只查询/停止原 Job，确认后把中断 Run 记为失败，不重新执行或自动应用结果。停止未知或清理未完成时仍保持 paused；这不是恢复原业务工作流的命令。旧 v1 执行只读，旧审批不能授权 v2。参数以各命令 --help 为准。交互 /logs、/cancel 尚未实现。不要照搬历史 --planner 或 code apply --prepare-only 命令。
