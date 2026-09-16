# 真实需求与设计闭环

公共入口是 `ProjectApplication.start_specification / specification_status / clarify_specification / resume_specification`。CLI 仅解析输入并输出 JSON。固定路径为输入留存 → SRS → 独立需求评审 → SDD → 独立设计评审；`completed` 才表示本次需求和设计均通过评审。没有接入自动编码、Docker 验证、发布或合并。

## 使用现有项目、任务和仓库

```powershell
yipilot init --project C:\work\project
yipilot task init --project C:\work\project --taskid 1
yipilot task repo add --project C:\work\project --taskid 1 --url C:\work\repo --alias sample --sync
yipilot specification start --project C:\work\project --task 1 --repo sample --instruction "将 value() 的返回值改为 2，保留接口并更新测试" --context-file value.py --context-file test_value.py --request-id req-1
yipilot specification status --project C:\work\project --task 1
```

也可使用 `--prd C:\work\requirements.md`（本地 UTF-8 Markdown/TXT），并可与文本一起提供。无仓库时可只提供文本或 PRD；没有代码上下文时设计中的未知现状必须保持未知。只提供已登记仓库时先等待用户说明目标。项目和任务初始化、仓库同步继续使用已有入口；不会隐式 clone 未授权的其他仓库。

`--model connection/model[:variant]` 和 `--settings-dir` 复用现有模型配置；省略时使用全局默认模型。没有 fake/replay 参数。旧 `quality generate` 保持原有离线演练语义，不能作为本路径的语义评审证据。

代码上下文只包含用户用 `--context-file` 明确选择的文件，从登记仓库的提交快照读取，复用 Git-object source export 和路径保护，不执行仓库代码。单文件最多 8 KiB、最多 16 个、累计最多 24 KiB；整个模型提示最多 32 KiB。PRD 最多 16 KiB。超限停止，不静默截断。未提交的仓库修改不进入提交快照；要分析它们需先明确形成新的仓库基线。

模型配置的 `output_limit` 仍是正文 UTF-8 字节上限；新增 `max_output_tokens` 是发给供应商的 token 上限，旧配置缺省为 8192，model variant 可覆盖。它不会自动扩大正文预算。`context_limit` 按 token 窗口理解：共享客户端用提示 UTF-8 字节数作保守输入估计，加输出 token 预留和 256 的消息开销估计后检查。这不是供应商精确 tokenizer，可能提前拒绝；未配置窗口时仅执行既有输入字节限制。

8192 是本地默认值，不是自动发现的供应商能力；其他模型如有更低限制，需在既有公开模型配置的 `max_output_tokens` 中填写其支持值。连接超时和凭据仍由原配置管理。

共享客户端分别限制总传输 16 MiB、单个 SSE 行/事件 256 KiB、思考正文 1 MiB，以及模型配置的正文上限。SSE 包装和思考内容不消耗正文预算。只有明确 `finish_reason=stop` 且 SSE 收到 `[DONE]`，才能交付正文；`length`、工具调用、过滤、缺失结束原因或断流都不能放行。不会自动调大上限、关闭思考或重发失败网络请求。

## 澄清、继续和变更

返回 `waiting` 时，`questions` 提供问题 ID 和问题正文。一次回答全部待决问题，绑定返回的 `flow_id` 和 `input.sha256`：

```powershell
yipilot specification clarify --project C:\work\project --task 1 --flow <flow_id> --input-sha256 <input.sha256> --request-id answer-1 --answers '{"Q1":"返回整数 2"}'
yipilot specification resume --project C:\work\project --task 1 --flow <flow_id>
```

答案提交后自动继续，不要求普通阶段逐次批准。已生成文档后的澄清计入 SRS 修订预算；仅仓库输入的首次目标补充不算修订。`--max-revisions` 默认每类文档 2 次，范围 0–5。关键歧义等待，修订问题自动返工，致命问题或预算耗尽停止。`stopped` 的原因和失败回执保留；若需再次尝试，使用新的启动请求 ID，不能把未知外部调用当作从未发生。

相同请求复用已提交结果；同一请求 ID 对应不同输入被拒绝。更改需求/PRD/已同步仓库基线或选择的文件后，用新请求启动；旧工作流成为 `superseded`，其 `accepted` 清空，旧文档仍可审计。澄清也产生新输入快照并使关联 SRS、SDD、评审失效，再从需求生成推进。查询默认返回最新工作流；旧请求重放不会重新激活旧结果。

进程在阶段提交边界退出后，`resume` 从下一阶段继续。持久状态为 `calling` 时，无法确认模型是否已经执行，继续入口将其记为 `MODEL_RESULT_UNCERTAIN` 并停止，不自动重发。查询不抢占正在执行的实例；复用已有项目执行锁。

完整收到并正常结束的响应若只有 JSON 语法/字段类型/必填字段错误，每个阶段在整个 flow 内最多自动修复一次。`format_repairs` 和失败工件引用在再次调用前提交，重启不重置预算；第二次仍非法则 `SPECIFICATION_FORMAT_REPAIR_LIMIT` 停止。修订和澄清也不重置这项预算。未知协议版本、错误文档哈希、证据引用、评审结论矛盾及网络结果不确定均不进入格式修复。

## 工件与交接

`input`、`documents`、`reviews`、`accepted` 和 `history` 都是带 Task/Operation/Run、路径和 SHA-256 的工件引用。正文位于项目 `.yipilot/tasks/<task>/runs/<run>/artifacts/`；输入、文档和评审为结构化 JSON，`prompt.md` 保存该次完整 JSON 提示，`response.json` 保存返回正文。原始材料独立保存，标注来源和原字节哈希；沿用存储层的密钥脱敏，不承诺含密钥输入逐字保留。

SRS 验收项有唯一 ID 和用户材料出处；代码事实只能引用提供的代码文件 ID，模型假设独立列出。SDD 绑定已接受 SRS 的 SHA-256，每个验收项都必须有测试映射。评审以另一轮无会话历史的模型调用执行，可以使用同一已选模型；这不是不同供应商或人工独立评审的保证。程序检查结构、引用、未决问题和结论一致性，语义质量仍受模型能力与所给材料限制。

新 SRS、SDD、Review 必须携带 `schema_version: 1`，严格 schema 随每阶段提示发送；状态和响应回执也标记版本。历史无版本文档读取时仅在内存补版本，保留原文件及 SHA-256；新模型输出不能省略版本。已完成的旧流程可以查询/复用。未完成的旧流程若冻结配置没有新增 token 预算，继续会按配置变更停止，需新请求，不静默改变已冻结调用语义。

`response.json.completion` 和失败回执记录结束原因、token 用量（供应商提供时）、正文/思考/传输字节数、接收完整性和输出 token 上限。`received` 表示完整收到协议响应，`complete` 表示通过共享客户端结束判定；两者都不等于业务评审通过。思考原文、工具参数不写入工件。fake/replay 没有真实回执，该字段为 null。

交接时只取最新 `completed` 状态的 `accepted.srs`、`accepted.sdd` 及对应 `reviews`，不要根据文件名或历史“通过”记录推断当前有效性。本次没有将这些文档隐式接入旧 `code generate`，旧 generate/apply/verify 行为保持不变。

CLI 所有命令输出 JSON；`stopped`、`superseded` 或输入错误退出码为 1，`waiting` 是可恢复业务状态、退出码为 0。大输入/缺失代码等问题需调整输入后新建请求，没有自动检索整个仓库、自动扩权或自动扩大模型预算。
