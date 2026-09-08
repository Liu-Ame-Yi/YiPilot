> **Research reference — 文档整理：2026-09-07。** 本文是对应日期的调查与推断，不是现行实施规格；自动合并、多仓库等讨论不属于当前范围。目标见 [产品定义](../product/vision.md)，现状见 [状态页](../product/status.md)。

# Agent 的供应商、模型选择与测试替身调研

> 范围：Pi Agent（用户指定的 `badlogic/pi-mono` 官方仓库；当前 GitHub 会重定向至 `earendil-works/pi`）及 OpenCode 官方文档/仓库。只记录可验证事实和对 Yipilot 的建议，不构成已实现功能。

## 结论

Yipilot 不应把 `FakeModelPort` “修改成真实模型”。正确边界是：**fake/replay 是确定性测试替身；live 是同一 `ModelPort` 协议的另一实现**。运行时仅在创建 Run 时显式选择其中一个，且将最终选择的 Provider、模型、配置版本写入证据。

当前 Yipilot 已具备这条最小骨架：`ProviderProfile`（不含密钥）→ `SecretStore` → `OpenAICompatibleModelPort` → `ModelPort`，并已在 M5-A 将其用于受控、proposal-only 的 live 调用。因此缺的不是第二套供应商框架，而是将过渡性的 `ProviderProfile` 迁移为连接与模型分离的控制层，并让未来受控写入消费已冻结的选择。

## 官方项目怎么做

### Pi Agent：目录、选择、真实调用、测试替身分层

Pi 将 Provider 注册、模型目录、认证和模型解析放在 Agent loop 之外。`ModelRuntime` 汇集内建 Provider、用户 `models.json` 和扩展 Provider，并合成“全部/可用模型”快照；自定义 Provider 可声明 `baseUrl`、API 类型、认证、headers 和模型元数据，而模型可覆盖 API、推理能力、上下文、输出上限和兼容性。[Model runtime 源码](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/src/core/model-runtime.ts)；[自定义模型配置](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/models.md)

选择使用明确的 `provider/modelId`。解析优先级是：CLI 显式选择 → scoped model → 已保存且已认证的默认值 → 已认证 Provider 的默认模型 → 第一个可用模型；模型 ID 有歧义时拒绝，而不是猜测。[model-resolver 源码](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/src/core/model-resolver.ts#L571-L659)

Pi 的 `fauxProvider()` 是内存、脚本化响应队列，专供测试和演示；生产则加载真实 Provider/凭据。两者共享模型流接口，但 faux 不作为生产降级路径。[Faux Provider 官方说明](https://github.com/badlogic/pi-mono/blob/main/packages/ai/README.md#faux-provider-for-tests)；[测试 harness](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/test/suite/harness.ts#L92-L210)

**可信度：高。**以上均来自 Pi 官方仓库；仓库组织迁移不影响这些源码路径的 GitHub 重定向。

### OpenCode：稳定引用、配置覆盖与变体

OpenCode 用 `provider_id/model_id` 作为稳定模型引用；用户可从交互式 `/models` 选择，也可在配置写默认 `model`。Provider 配置可以声明 OpenAI-compatible endpoint、凭据/headers 和模型清单；模型选项和“variant”（如不同 reasoning effort）是覆盖项，不必复制一个模型记录。[模型选择与 variants](https://opencode.ai/docs/models/)；[自定义 Provider 官方文档](https://opencode.ai/docs/providers/)

**可信度：高。**结论来自 OpenCode 官方文档；它有更大的 Provider 目录和 SDK 生态，不能直接当作 Yipilot 现在必须实现的范围。

### 交互式模型选择：值得借鉴的最小体验

Pi 将交互入口做成可发现的 slash command：在编辑器输入 `/` 会打开命令补全；`/model` 打开模型选择器，切换当前会话所用模型，选择器内按 `Ctrl+S` 才把该选择保存为下次启动的默认值。当前模型始终显示在 footer；`/login`/`/logout` 专门管理 OAuth 或 API-key 凭据，而非把凭据输入混进模型列表。源码表明一次切换会验证认证、更新当前 session，并写入一条 model-change session record；只有明确 persist 才改全局默认。Pi 还会在每次打开 `/model` 时重读 `models.json`，所以用户中途编辑模型目录不必重启会话。[Pi usage：交互区域与 slash commands](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/usage.md#interactive-mode)；[Pi settings：全局/项目设置与默认模型保存](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/settings.md#settings)；[Pi session model switching source](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/agent-session.ts#L1652-L1677)；[Pi custom models](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md#full-example)

Pi 的持久化分成全局 `~/.pi/agent/settings.json` 和项目 `.pi/settings.json`，并由项目设置覆盖全局设置；项目本地设置受显式 trust gate 保护。其 `models.json` 则用于自定义 provider/model 目录。这说明“模型选择”与“连接/凭据配置”虽然在一个交互体验中相邻，存储和安全责任不应混成一个对象。[Pi settings](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/settings.md#settings)；[Pi custom models](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/models.md#custom-models)

OpenCode 的引导顺序也相同：先用 `/connect` 添加凭据，再用 `/models` 选择已配置模型；对自定义 OpenAI-compatible provider，连接 ID、显示名、base URL 与模型清单在配置中分别声明，凭据操作会提示“仍需配置 provider”。稳定版文档的启动选择优先级是显式 `--model`、配置默认、上次使用、内部优先级。其 v2 文档将“当前 session 的选择”与配置默认明确拆开：选择器只改当前 session，显式 per-run 选择不改默认，且正在进行的请求保持启动时的设置、只影响后续请求。这是 Yipilot 的 Run 冻结语义可直接借鉴的交互规则。[OpenCode models](https://opencode.ai/docs/models/#select-a-model)；[OpenCode custom provider setup](https://opencode.ai/docs/providers/#custom-provider)；[OpenCode model loading priority](https://opencode.ai/docs/models/#loading-models)；[OpenCode v2：choose/default/per-run/caveats](https://opencode.ai/v2/docs/models#choose-a-model)

#### 对 Yipilot 的体验结论

Yipilot 可以借 Pi 的“**当前选择可见 + 选择器内明确区分临时使用与保存默认**”和 OpenCode 的“**先连通、再选模型**”引导，但不应照搬它们的自动兜底策略。Yipilot 的目标是受控交付：当没有明确模型时应阻止真实 Run 并引导 `/model`，而不是静默使用“上次模型”或目录中的第一个模型。

建议的 `/model` 首屏只做四件事，且全部调用 Control Plane、绝不直接构造 `ModelPort`：

```text
Current: acme/gpt-5  (default, key available)

1. Select a configured model
2. Add a provider connection
3. Test a provider connection
4. Set/clear the default model
```

选择已存在模型时显示 canonical selector、Provider 显示名、模型显示名、capability、配置 revision、密钥是否可读取以及“本次 / 设为默认”两个明确动作；不显示 API key。没有可选模型时直接进入“新增连接 → 保存凭据 → 验证连接 → 填入模型 ID → 选择/设默认”的向导。真实 Run 创建时只接受已经解析并冻结的 `ResolvedModel` 快照；运行中的 Run 不允许通过 `/model` 改写其模型，命令只能影响下一次 Run。最后一条是 Yipilot 为审计性作出的刻意差异，不能从 Pi/OpenCode 的会话聊天体验直接照搬。

## 对 Yipilot 的最小设计（历史首稿）

本节保留早期调研结论以说明演进背景；其中“保留 ProviderProfile”与
“项目默认”的建议已被后续接受的 [模型选择规格](../superpowers/specs/2026-09-01-yipilot-model-selection.md)
和 [ADR](../adr/2026-09-01-model-selection-control-plane.md) 取代。当前设计
采用连接/模型分离、无项目默认，以及“per-Run 显式选择 > 交互会话选择 >
用户全局默认 > 本地失败”。

Yipilot 现在只支持 OpenAI-compatible API，因此不要先引入 Pi/OpenCode 式多 Provider catalog、模糊搜索或自动切换。保留 `ProviderProfile` 作为“连接配置 + 默认模型”，再在每个 `code generate` Run 上固定一次选择：

```text
命令显式 --provider-profile / --model
  > Task/项目的已保存默认 Profile
  > ProviderProfile.model
  > 否则失败（不自动换供应商或模型）
```

建议新增一个小的应用层解析函数，而不是新的通用框架：

```text
resolve_model(profile_name, optional_model)
  -> 读取启用的 Profile
  -> 确认 chat capability、密钥引用可读取
  -> 产出不可变 ResolvedModel(profile, model, revision)
  -> 写入 Run 的 ModelRequest / Artifact
```

模型的 canonical 标识应是 `profile_name/model_id`；`model_id` 只在该 Profile 内有意义。第一版拒绝歧义与不存在的选择，且不自动 failover。这样一次 Run 始终可复现“谁、用哪个 endpoint、哪个模型、哪个配置版本”这一事实。

## fake/replay 如何变成真实能力

不是替换或删除 fake。应在 `CodeService` 的组合根新增显式模式：

```text
replay（默认）
  -> RecordedReplayModelPort / FakeModelPort + FakeExecutor

live（显式选择，要求已配置 Profile）
  -> OpenAICompatibleModelPort + ModelPlanner + 受控工具执行器
```

`runtime/model_planner.py` 的固定 diagnostic JSON 不能放宽为任意工具调用。M5-A 已将 live 模型输出收紧为 `PatchProposal`：受限文件列表、unified diff、简短说明，并仅作为 Artifact 展示。用户批准后，M5-B 的单独受控 patch 工具才可写入 worktree；模型绝不能获得任意 shell 或任意路径写入权限。

随后才进入有限修复 loop：`apply patch → pytest/ruff/mypy → 将脱敏失败摘要作为下一 Turn 输入 → 最多 N 次`。每次模型调用、patch、工具输出和质量结果均保留为 Artifact；取消、超时和 Provider 错误应终止或暂停 Run，而不是悄悄切回 fake。

## 建议的实施顺序

1. 增加 `live` 装配路径：复用已有 `OpenAICompatibleModelPort`、`ProviderService`、`SecretStore` 和 `ModelPlanner` 的证据记录；默认仍为 replay。
2. 将 live 模型输出从固定 diagnostic plan 改为严格 `PatchProposal`，只保存和审查，不写源文件。
3. 增加经批准的 patch 工具；写入仅限该 Run 的 worktree。
4. 接入已有质量命令，增加有限次数的 repair Turn。

最小测试应覆盖：已配置 profile 能解析；无密钥或模型请求失败时绝不进入写入；越界路径/非法 diff 被拒绝。真实 API 仅以 opt-in smoke test 验证，不能取代这些确定性测试。

## 与当前代码的对应关系

| 现有组件 | 角色 | M5 的处理 |
| --- | --- | --- |
| `domain/provider.py: ProviderProfile` | endpoint、默认模型、能力和密钥引用 | 保留；以 profile revision 固定 Run 选择。 |
| `domain/model.py: ModelPort` | 统一模型流边界 | 保留；fake/replay/live 都实现它。 |
| `infrastructure/model_port.py` | fake/replay/disabled 实现 | 保留为测试与离线模式，不能伪装为 live。 |
| `infrastructure/openai_compatible.py` | 真实 Chat Completions 调用 | 用于 live；不新增第二个 HTTP client。 |
| `runtime/model_planner.py` | 当前将模型 JSON 解析为受限 diagnostic plan | 改为/旁置解析 `PatchProposal`；不放宽为任意工具调用。 |
| `application/code_service.py` | 当前在此硬编码 fake/replay 和 FakeExecutor | 在此作为组合根根据显式模式装配 live 或 replay。 |

## 不做的事

- 不在 M5 建内建 Provider 大目录、动态模型发现、OAuth、自动 fallback、模糊模型匹配或跨模型 handoff。
- 不将 API Key 写进 Profile、Run、Event 或 Artifact。
- 不将模型输出直接视为 shell 指令或文件写入授权。
