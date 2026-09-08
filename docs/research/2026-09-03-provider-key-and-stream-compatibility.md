> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# Provider 密钥录入与流式连通性调研

> 范围：Pi、Alibaba Open Code Review（OCR）和 DeepSeek / OpenAI 的一手文档或源码。
> 目的：为 Yipilot 的 `/model` 密钥录入与连接测试排障提供依据；不表示已完成修复。

## 结论

`MODEL_PROTOCOL_ERROR: model stream chunk is invalid` **不是 API Key 错误**。Yipilot 已经拿到 HTTP 成功响应并进入 SSE 解析；错误发生在某条 `data:` 事件不符合当前解析器硬编码的 `choices[0].delta` 形状时。应保存（并脱敏显示）该事件的结构、按正式 OpenAI Chat Completions SSE 兼容规则解析，或在连接测试使用 `stream: false` 的 JSON 探针。不要把协议错误提示成“连通失败”。

DeepSeek 官方当前的 OpenAI-format Base URL 是 `https://api.deepseek.com`，聊天路径是 `/chat/completions`，内置模型 `deepseek-v4-pro` 和 `deepseek-v4-flash` 均有效。因此 Yipilot 的 DeepSeek 默认地址和这些模型 ID 本身没有明显错误。[DeepSeek 模型与地址](https://api-docs.deepseek.com/quick_start/pricing/) [DeepSeek 最小调用](https://api-docs.deepseek.com/)

## 其他 Agent 如何处理 API Key

### Pi

- Pi 将“认证”与“选择模型”分开：`/login` 负责 OAuth 或 API Key，`/model` 负责使用哪个模型；API Key 可以来自环境变量，或由 `/login` 保存到 `~/.pi/agent/auth.json`。[Pi provider 文档](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md#api-keys)
- `auth.json` 是 Provider 名到带类型凭据对象的映射，例如 `{"deepseek":{"type":"api_key","key":"…"}}`；Pi 文档说明它按用户读写权限创建，且 auth file 优先于环境变量。[Pi auth-file 格式与优先级](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md#auth-file)
- OAuth token 同样在 `auth.json`，`/logout` 明确负责清除凭据。[Pi subscription auth](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/providers.md#subscriptions)

**可借鉴点：**Yipilot 已有独立 `auth.json` 是正确方向；应把页面文案改为“保存 API Key 到 Yipilot 本机凭据文件（输入将掩码显示）”，并在已有 Key 时显示短指纹和“替换 / 保留 / 删除”，而不是只写“终端不会显示输入”。模型选择不能混入凭据格式或存储细节。

### Alibaba Open Code Review

- OCR 的引导顺序为 Provider → Model → API Key，结束后自动执行连接测试；官方 README 将其作为 `ocr config provider` 的行为说明。[OCR README](https://github.com/alibaba/open-code-review/blob/main/README.zh-CN.md#快速开始)
- OCR 会将 API Key 写入它自己的 `config.json`，随后执行 `Testing connection...`。失败时配置仍保留，并提示运行 `ocr llm test` 复测。[OCR 保存与测试源码](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_cmd.go#L96-L144)
- 它的缺凭据检查接受当前输入、预配置 `api_key_cmd` 或 Provider 指定环境变量；这让 CI 不必把 Key 写进交互配置。[OCR 凭据优先级检查](https://github.com/alibaba/open-code-review/blob/main/cmd/opencodereview/provider_cmd.go#L502-L550)

**与 Yipilot 的刻意差异：**Yipilot 当前的“Probe 失败不落盘”符合已有产品决定，安全语义比 OCR 更严格；但失败信息需要区分鉴权、网络、HTTP、协议和模型 ID，不能全部归为“连接失败”。

## API 与协议事实

### DeepSeek

- 官方示例使用 `POST https://api.deepseek.com/chat/completions`，`Authorization: Bearer $DEEPSEEK_API_KEY`，并把 `stream` 设为 `false`；文档说明改为 `true` 才取得流式响应。[DeepSeek quick start](https://api-docs.deepseek.com/)
- 官方 Chat Completions 文档说明 `stream: true` 时返回 data-only SSE，并以 `data: [DONE]` 终止。[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)
- 官方模型表列出 `deepseek-v4-flash`、`deepseek-v4-pro`，OpenAI-format Base URL 均为 `https://api.deepseek.com`。[DeepSeek 模型表](https://api-docs.deepseek.com/quick_start/pricing/)

### OpenAI Chat Completions

- OpenAI 的 Chat Completions 路径为 `POST /chat/completions`；非流式响应含 `choices` 与 `message`，聊天消息内容可为 `string` 或 `null`。[OpenAI Chat API reference](https://developers.openai.com/api/reference/resources/chat)
- 因此“OpenAI-compatible”不是一个严格的流事件标准保证：网关、模型供应商或扩展字段可能发送仅带 usage、reasoning 或其他元数据的 SSE 事件。对外宣称兼容时，Yipilot 必须明确它支持的是哪一种协议面（目前应写成“OpenAI Chat Completions，SSE 响应需要 `choices[].delta`”），或把探针降级为非流式 JSON。

## 当前 Yipilot 的故障定位

当前实现固定发送 `stream: true`，并在 `_delta_content()` 中要求每个非 `[DONE]` 的 SSE `data:` JSON 都含 `choices[0].delta`；任何不含该结构的成功 SSE 事件均抛出所见的 `MODEL_PROTOCOL_ERROR`。[当前适配器](../../src/yipilot/models/openai_compatible.py)

这解释了为何“Key 正确”仍失败：密钥验证通过不等于某个 endpoint 的流事件形状与 Yipilot 的窄解析器一致。还不能仅凭该错误断言是 DeepSeek 默认 URL 问题；官方 URL 与当前默认值相符。下一步应以 **不含 Key 的脱敏诊断** 记录响应 `Content-Type`、HTTP 状态、首个异常事件的 JSON 键名和 provider/model/base URL，再决定是兼容该事件还是拒绝该网关。

## 最小修复建议

1. 连通测试改用 `stream: false`，只验证 `choices[0].message.content`；这与 DeepSeek 官方 quick-start 示例一致，能把“凭据 / endpoint / model 可调用”与“生产流协议兼容”分开。
2. 真实 Run 仍可使用流式；解析器仅忽略可证明无文本的 metadata 事件（如空 `choices` usage 事件），其他未知事件抛出新的、可行动的 `MODEL_STREAM_UNSUPPORTED`，并显示脱敏结构摘要。
3. `/model` 的密钥输入采用掩码字段；成功后只显示短指纹，管理页提供替换和删除。保留 `auth.json` / 环境变量两条来源，不把原始 Key 回显或写进 `providers.json`。
