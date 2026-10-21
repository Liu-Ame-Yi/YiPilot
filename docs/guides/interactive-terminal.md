# 当前交互式终端

核对：2026-09-07 当前工作树。启动：

```powershell
yipilot --project C:\work\demo --settings-dir C:\work\yipilot-settings
```

| 命令 | 当前行为 |
| --- | --- |
| /help | 显示可用命令 |
| /task | 创建、选择或查看任务；创建只保存标题与目标，不启动模型 |
| /model | 配置/选择模型；输入 Key 后自动进行小型联网探测 |
| /status | 查看当前项目、任务与模型等状态 |
| /run | 读取选定任务、模型、仓库和上下文，确认后生成真实提案 |
| /apply | 选择提案并审阅；确认远程 OpenSandbox 执行及可信输出，应用到任务 worktree |
| /verify | 对成功应用的结果执行确认后的 OpenSandbox 验证；失败时可生成有限修复提案 |
| /exit | 退出当前会话 |
| /logs、/cancel | 当前不可用 |

/task 不注册仓库；使用 task repo add 为该任务登记一个仓库。模型选择器使用方向键、Enter 和 Esc；任务/模型的当前会话选择与已持久化任务、全局默认是不同概念。

/apply 要求已配置的 Runner 与凭据引用，且使用与会话相同的 settings-dir；新业务代码的 CI/服务器验收尚未完成。/verify 不复用旧执行结果，Runner 不可用时失败或保留待对账状态，不静默切换宿主执行。新修复提案需要继续应用和验证，不代表失败已被解决。

当前多个确认是过渡实现。[目标流程](../product/vision.md)要求任务预授权后自动推进，只在关键歧义、越界、阻塞或配置人审时等待。这里不把尚未实现的自动控制写成现有操作。

安装、注册与命令示例见 [快速开始](getting-started.md)，密钥与模型说明见 [Provider 配置](provider-configuration.md)。

## 调用职责

/run 与 code generate 共用 ProjectApplication.generate；/apply 与 /verify 分别调用同一应用入口的 apply/verify。终端仍负责展示提案和精确 diff、收集每次确认，但不创建 ApprovalRecord，不直接编排沙箱 或决定是否生成修复提案。拒绝批准保存在对应 Run，并取消尚未执行的工作。

非交互调用见 [Python 应用入口](application-api.md)。这些方法只覆盖已有能力，不会自动推进至 PR/MR，也没有后台恢复或自动合并。
