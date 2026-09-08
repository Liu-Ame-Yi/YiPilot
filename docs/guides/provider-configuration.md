# 模型与 Provider 配置

**状态：Implemented（已实现）。** 当前只支持 OpenAI-compatible 的
`POST /chat/completions`。设置将连接与模型分开保存：连接保存 endpoint、能力、
限额和密钥引用；模型保存精确模型 ID 与限制。

## 安全原则

- 模型设置是用户本地元数据，建议通过 `--settings-dir` 放在专门目录，不写入
  项目的 `.yipilot`。
- `providers.json` 保存供应商和模型；同目录的 `auth.json` 保存 API Key。两者都
  只能位于用户本地设置目录，绝不能提交到 Git 或复制到项目 `.yipilot`。
- API key 不能放入 URL、命令行、Event 或 Artifact。
- 默认将 API Key 写入用户本地 `auth.json`；CI 才适合使用由 CI Secret 注入的
  `env:NAME` 引用。
- 只使用 HTTPS Base URL；仅 `localhost` 或 loopback 开发服务允许 HTTP。

## 推荐：交互式配置

```powershell
$settingsDir = 'C:\work\yipilot-settings'
yipilot --settings-dir $settingsDir
```

进入后输入 `/model`：首次没有模型时会直接打开服务商选择器；已有模型时选择
“添加模型服务商”。选择器使用方向键选择、Enter 确定、Esc 返回。向导按用户能理解的顺序询问：

1. **配置类型**：选择“官方服务商”或“自定义 OpenAI-compatible 服务商”。
2. **服务商**：官方列表提供 DeepSeek、OpenAI、阿里云百炼（千问）；自定义服务
   要求填写显示名称。
3. **服务地址**：常用服务商自动填写；百炼和自定义服务才要求从控制台复制兼容
   地址。
4. **模型**：选择推荐模型，或输入供应商控制台中的模型 ID。
5. **API Key**：在掩码单行输入框中输入；按 Enter 后开始测试，值不会显示。测试成功后
   才保存到 `auth.json`。脚本/CI 才使用环境变量。
6. 输入后立即自动发送一次最小非流式 Chat Completions probe，验证 Key、地址和模型；成功后选择当前终端使用、设为默认模型
   或返回。失败时可重新输入 API Key 或返回；失败的首次配置不会写入
   `providers.json` 或 `auth.json`。

向导完成后会做本地连接、模型和密钥引用验证。它不会回显密钥；内部连接 ID 由
Yipilot 自动生成，不是需要理解或输入的配置项。

默认目录下，两个文件位于
`C:\Users\<你>\AppData\Local\Yipilot\yipilot\`；使用 `--settings-dir` 时，它们
位于你指定目录。`auth.json` 含可用 API Key，请像 Pi 的 `auth.json` 一样仅保留在
自己的用户目录，并勿同步、分享或提交。

## 脚本与 CI 使用

先通过 `/model` 或 `settings model configure` 在用户本地创建连接和模型。自动化
场景用精确 selector 选择它，并让 CI 平台负责将密钥注入进环境：

```powershell
yipilot settings model select ci-local/example-model --settings-dir C:\work\yipilot-settings --output json
```

这里的 `env:YIPILOT_API_KEY` 只是引用名，不是密钥值。请在 CI 的 Secret 设置中
配置真实值；不要把真实 key 放入脚本、终端历史或仓库。

## 验证与测试

```powershell
# 仅本地校验：不联网
yipilot settings model test ci-local/example-model --settings-dir C:\work\yipilot-settings --output json

# 明确允许一次最小网络探测：可能产生费用
yipilot settings model test ci-local/example-model --request --settings-dir C:\work\yipilot-settings --output json
```

`--request` 只发送固定探测文本 `Reply with OK.`，以非流式 Chat Completions 验证配置，
不上传仓库、`.git`、环境变量、
Task 文件、Artifact 或用户指令。

## 查看与切换

```powershell
yipilot settings model list --settings-dir C:\work\yipilot-settings --output json
yipilot settings model select ci-local/example-model --settings-dir C:\work\yipilot-settings --output json
```

当前这一个交付切片未提供连接/模型编辑命令。需要变更 endpoint、模型或限额时，
先通过 `/model` 的“添加模型服务商”新建一个连接/模型，再选择新的精确 selector。
脚本和 CI 使用环境变量引用的密钥。

## 撤销或停止使用

- 在 `/model` 的“测试或删除已有模型”页面选定模型，再选择“删除此模型”并确认。
  删除最后一个模型会同时删除连接、默认选择和对应的 `auth.json` Key。
- 环境变量：从启动进程或 CI Secret 中移除该变量。

禁用、缺少密钥、无 chat 能力或 URL 不安全时，Yipilot 会在建立 live Run 的
worktree 前失败；它不会改用其他模型或自动降级到 fake/replay。

## 未来变化

旧 settings provider 命令仍可在当前 --help 中查询；code generate 已没有 --provider-profile 参数。日常配置使用 /model，脚本使用 settings model，不依据旧 M5 时间承诺推断兼容性。
