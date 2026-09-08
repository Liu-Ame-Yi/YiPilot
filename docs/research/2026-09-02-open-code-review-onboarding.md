> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# Open Code Review 的 Provider / Model 引导调研

> 范围：仅考察用户指定的 [Alibaba Open Code Review 官方仓库](https://github.com/alibaba/open-code-review) 中当前的 README 和源码。本文记录可验证的交互事实及其对 Yipilot 的适用性；不表示 Yipilot 已实现这些体验。

## 结论

Open Code Review 的好体验不在于“模型列表更长”，而是把第一次配置做成一个连续、可返回的向导：**先选连接来源，再选/录入模型，再录入凭据，保存后自动验证，失败也保留可修复的配置**。它还把日常“换模型”收敛成独立的模型选择器，避免用户每次改模型都重填 endpoint 和 key。

Yipilot 应借鉴这个流程，并采用 Pi 式的两文件分离：`providers.json` 保存公开的
连接/模型元数据，`auth.json` 保存 API Key；环境变量仍用于 CI。在线 Probe 必须说明
其网络与费用影响并明确确认；完成配置也不能改变已经冻结的 Run。

## 官方流程：用户看到什么

README 的最短开始路径是先执行 `ocr config provider`，再执行 `ocr config model`；README 明确说明第一个命令的交互界面会完成供应商选择、API Key 输入、模型配置并自动测试连通性。[官方中文 README](https://github.com/alibaba/open-code-review/blob/main/README.zh-CN.md#快速开始)

源码中的命令定义也区分了两个职责：`ocr config provider` 是“Interactive provider setup”，`ocr config model` 是“Interactive model selection”。配置默认写在用户目录 `~/.opencodereview/config.json`。[命令定义与默认路径](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/config_cmd.go#L60-L99)

### `ocr config provider`：一个连续的三步选择器

这个 TUI 使用全屏交互，Provider 配置主线有三个阶段：`Provider → Model → API key`；`Esc` 在后两步返回上一步，在第一步取消，方向键 / `j` / `k` 移动，`Enter` 确认。[状态与按键处理](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L15-L21) [按键行为](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L562-L684)

Provider 首屏有三个并列入口，而不是直接丢给用户多个输入框：

```text
○ Official    ○ Custom    ○ Manual

Select a provider
▸ <provider display name>  (<currently active model, if any>)

Enter to select · Tab/Arrow keys to navigate · Esc to cancel
```

其中 Official 为内置目录；Custom 显示已保存的自定义 Provider，并提供新增、编辑、删除；Manual 则一次性填写 endpoint。当前配置的 active model 会显示在 Provider 行旁边。[Tab 与列表渲染](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L1887-L1963)

选择 Provider 后进入该 Provider 的模型清单，最后一项永远是 `Enter custom model name...`。自定义模型录入后会成为该 Provider 的可管理模型；重复名称会在原地报错而不退出页面。[模型选择器](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L2129-L2177) [重复与无归属校验](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L686-L720)

Custom Provider 是分步表单：名称 → 协议（Anthropic、OpenAI Chat Completions、OpenAI Responses）→ Base URL → API key → 可选 Auth Header。名称重复、空 Base URL 和不支持的 Header 都原地显示错误；`Esc` 可以逐步返回修改。[字段和协议清单](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L32-L58) [逐步校验](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L990-L1057) [表单呈现](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L1989-L2051)

Manual 入口把 URL、协议、模型、Auth Token、Auth Header 放入相同的逐步表单。它先要求 URL 和模型非空，再保存配置。[Manual 表单与返回行为](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L1276-L1334) [保存前校验](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_cmd.go#L96-L144)

完成选择后，OCR 先保存“Provider + Model”，随后自动显示 `Testing connection...`。连接测试失败不会丢弃刚刚的配置，而是告诉用户配置已保存、修复后运行 `ocr llm test` 重新验证；成功后给出 `ocr config model` 的下一步提示。[官方 / 自定义 Provider 保存与验证](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_cmd.go#L244-L295) [Manual 保存与验证](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_cmd.go#L96-L144)

### `ocr config model`：日常切换只处理模型

该命令要求已经存在 active Provider；没有时直接报错并给出下一条精确命令：`Run 'ocr config provider' first`。有 Provider 时只打开该 Provider 的 model picker，选中后写 active model；用户录入自定义模型时会先加入该 Provider 的模型清单，再回到选择器，而不是把它当成另一个 Provider。[前置引导](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_cmd.go#L298-L348) [选择保存](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_cmd.go#L355-L391)

它支持删除用户手动添加的模型，但不会把内置目录模型误删；删除前询问 `Delete "…"? (y/n)`，失败会恢复内存中的上一个配置。[删除限制与确认](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L2167-L2176) [删除回滚](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L2650-L2691)

## 渐进披露和错误/凭据处理

值得学习的交互细节：

- 首屏只有 Official / Custom / Manual 三个概念；用户选中后才出现模型列表或表单，而不是先学习所有 Provider 字段。[首屏](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L1907-L1928)
- 每个界面底部只呈现当前阶段相关快捷键（确认、返回、编辑、删除），降低记忆负担。[上下文帮助](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L1918-L1927)
- 已保存 key 以密码字符显示，只有短 fingerprint 提示“将替换已有 key”，而不是回显全文；环境变量存在时，界面说明“留空会使用环境变量、输入则覆盖”。[掩码输入](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L226-L233) [fingerprint / 环境变量提示](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L2229-L2270)
- OCR 的缺 key 校验会接受已配置的 key、`api_key_cmd` 或环境变量（环境凭据 Provider 例外）；缺失时指出可操作的补救路径，而不是到模型调用时才失败。[凭据优先级校验](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_cmd.go#L217-L257)
- 需注意：OCR 当前将 API key 写进 `config.json`，文件权限设为 `0600`。这是其项目的实现取舍，不适合 Yipilot 复制；Yipilot 现有的凭据引用与 secret store 更适合 Windows、审计和证据脱敏。[OCR 保存实现](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_cmd.go#L393-L408)
- OCR 的向导会在某些“添加/删除模型”动作中即时写盘，因此最终 `Esc` 只能放弃尚未确认的 Provider/API key，不能回滚这些中途变更，并会明确说明已保存变更仍保留。Yipilot 不应照搬这种意外持久化语义。[取消提示](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_cmd.go#L35-L62) [即时保存路径](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_tui.go#L2519-L2535)

## 对 Yipilot 的 UX 建议

### 借鉴流程，不复制存储和运行语义

Yipilot 的 `/model` 应改为“模型与连接向导”的唯一日常入口；命令行 `settings provider ...` / `settings model ...` 保留给脚本、CI 与排错。首页不要先展示 Session choice / Global default 的内部术语，而要先给出用户可行动的入口。连接 ID 是存储内部主键，应自动生成，不能作为首次用户输入。

```text
模型设置

当前默认：尚未设置

1. 选择已配置的模型
2. 新增模型连接
3. 管理已有连接
0. 返回

请输入编号：
```

当没有任何可用模型时，直接进入“新增模型连接”，不要展示一个空列表后要求用户猜 `C`。连接向导采用 OCR 的顺序，但按 Yipilot 的数据边界调整为：

```text
连接类型（首版仅 OpenAI-compatible）
  → 连接名称
  → Base URL
  → API 密钥来源（Windows Credential Manager / 环境变量引用）
  → 本地校验（URL、名称、密钥引用可读）
  → 可选、二次确认的在线 Probe
  → 模型 ID（可录入多个，首个即当前选择）
  → 仅本会话 / 设为全局默认
  → 成功摘要与下一步：可开始新的 Run
```

这里的顺序与 OCR 略有不同：Yipilot 在写任何 `ModelEntry` 之前先完成连接与密钥引用校验，避免创建一个看起来可用但没有 credential reference 的模型；在线 Probe 仍在保存后，但必须明确它会发送固定文本、可能消耗额度且不含仓库/Task 内容。Probe 失败时也应保留连接和模型，状态标为“尚未验证”，并给出 `/model → 管理已有连接 → 测试连接` 的修复路径。

### 模型选择页

选择已有模型时，列表按连接分组，显示可理解的信息而非内部存储名：

```text
选择模型

已配置连接
1. DeepSeek API
   deepseek/deepseek-coder-v2  · 密钥可用 · 已验证
2. 公司网关
   gateway/qwen3-coder         · 密钥不可读 · 需要修复

N. 新增连接
0. 返回
```

选择可用模型后才问“本次终端会话”还是“设为全局默认”。这保留 Pi 类会话选择的便利性，又避免将持久化动作伪装成普通选择。列表项最终仍必须解析为严格的 `connection_id/model_id[:variant]`，而不是以显示文字或编号入库。

### 必须保留的 Yipilot 架构边界

- 交互层只驱动 `ModelSelectionService` 和 Provider/Model 配置服务；不得读取/写入 JSON、构造 `ModelPort` 或接触原始 secret。
- ProviderConnection、ModelEntry、default selector 是持久化控制面；Session choice 是 `InteractiveSession` 的短生命周期状态；`ResolvedModel` 在创建 Run 前由控制面冻结并写脱敏证据。
- `/model` 只能影响未来 Run。活动 Run 显示 frozen selector，但必须拒绝改写，不能模仿聊天 agent 的“下条消息换模型”。
- fake/replay 不出现在用户的真实模型选择器中，也绝不作为 live 失败后的 fallback。
- 不在首版照搬 OCR 的官方 Provider catalog、多协议、模型删除或 TUI 依赖。先以现在的 stdout/stdin 交互实现上述流程；待真实使用证明键盘导航/全屏列表必要时，再替换渲染层，不改变控制面服务。

## 建议的验收场景

1. 空配置进入 `/model` 后，用户无需知道内部快捷键即可完成一次 OpenAI-compatible 连接、凭据引用和模型选择。
2. 连接/模型的字段错误留在当前步骤并给出中文修复说明；任何错误输出不含 key、token 或 credential reference。
3. 用户拒绝在线 Probe 时，配置可用但显示“未验证”；Probe 失败后配置仍保留并可从管理页重试。
4. 选择已配置模型时，会话选择与“设为全局默认”是两个明确动作；编号不持久化。
5. 运行中的 Run 保持自己的 `ResolvedModel`；向导中的任何修改只影响后续 Run。
