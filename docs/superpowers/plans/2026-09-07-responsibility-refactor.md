# 第一轮职责边界重构：执行合同

状态：**当前专项指引，非完成记录。** 日期：2026-09-07。本文件只约束现有能力的结构重构；[V2](../specs/2026-09-07-yipilot-v2-architecture.md) 的完整目录是长期目标，不要求本轮全部创建。

## 0. 开始前核对工作树

本次检查已看到 application/project_application.py、application/run_executor.py、security/paths.py 和 workspace/；终端已引用 ProjectApplication。部分旧 application facade 已不存在。这些是正在演进的工作树事实，不是测试通过或迁移完成的声明。执行会话必须先检查 git status、真实文件、调用方和测试，保留已有修改，不按旧路径重新创建已删除包装，不重复另建一个统一入口。

## 1. 本轮结束时的结构

本轮保留 application/runtime/domain/infrastructure/harness 等现有包名，完成职责收敛；不同时推进第二轮 workflow/agent/models/persistence 全量改名。

```text
src/yipilot/
  application/
    project_application.py   # 现有研发操作的公共入口与装配
    run_executor.py          # Run 生命周期；复用现有实现，不重复封装
    code_service.py          # 生成提案的业务规则
    repair_service.py        # 修复请求/次数/结果的业务规则
    sandbox_cli.py           # 现有应用动作协调，名称后续按真实职责调整
    sandbox_verification.py  # 验证业务协调与结果判定
    ...                      # 配置/质量等已有服务，未触及时保留
  runtime/                   # 模型计划、工具执行与证据协议；不接触终端
  workspace/                 # repo、源码副本、Docker、输出检查、可信应用
  infrastructure/            # 当前文件存储、事件、模型/密钥等具体适配
  domain/                    # 现有值类型/状态/合同
  harness/                   # 现有确定性检查
  security/paths.py          # 真正跨模块共用的路径保护
  transport/cli/             # CLI和交互：输入、意图、呈现、退出码
```

目录只列职责，不要求创建不存在的占位文件。具体实现可在职责不变时保留当前名称；最终报告列出实际迁移清单。

## 2. 归属与依赖验收表

| 当前关注点 | 目标归属/规则 | 如何证明 |
| --- | --- | --- |
| interactive 中生成/应用/验证/修复衔接 | ProjectApplication 与现有业务服务；UI只调用入口 | 终端不直接串接 Docker/输出/批准存储；无stdin也可调用相同逻辑 |
| 审批 | UI返回同意/拒绝意图；业务层绑定对象、hash、策略并存储 | 伪造/过期/拒绝批准不执行，UI不直接new ApprovalRecord |
| Run开始/结束 | run_executor 的明确类型接口；不依赖隐藏参数次序 | 成功/失败/取消与证据关联经过同一生命周期实现 |
| application/runtime重复facade | 搜索所有调用方后合并、删除或保留有真实职责的适配 | 不通过 *args 或参数数量判断旧/新调用协议 |
| 源码导出、Docker、diff、worktree | workspace；应用层只作业务协调 | workspace不import transport，不决定下一研发阶段 |
| 模型协议/文件存储 | 本轮保留现有adapter位置，依赖在公共入口集中装配 | 不为改目录同时改协议、数据格式或迁移SQLite |
| 跨模块私有路径helper | security/paths 或唯一实际所有者的公开合同 | 调用方不再跨模块导入该私有helper；旧路径测试继续有效 |

允许依赖：transport → application → runtime/workspace/现有adapter；runtime → domain/所需协议；workspace → domain/security/必要底层adapter。禁止核心反向依赖transport；禁止workspace调用研发编排。基础设施保持现状的适配依赖，不为形式上的纯净制造大量一实现一接口。

公共入口仅覆盖当前能力：生成提案、列出可应用提案、应用、验证及有限修复衔接。沿用当前返回类型，必要时用少量明确结果类型。确认回调允许由UI提供，但返回的是意图，不能包含终端读写或代替持久化事实。完整 durable respond/resume 属于后续V2，不在本轮伪装实现。

## 3. 分批实施

1. 核实上述已开始迁移的文件，记录当前相关测试基线及未通过项。
2. 完成公共入口与终端分离；迁移所有对应CLI调用方，避免只改交互路径。
3. 收拢工作区能力与共享保护；保持Docker参数、路径检查、输出哈希和批准顺序。
4. 清理已无调用方的facade与隐式接口，移除重复装配；不批量重命名无关包。
5. 更新真实service组合测试、依赖约束及当前状态文档，再运行质量检查。

每批保持可调用；发现其他会话正在改同一文件时先协调，不覆盖其变更。

## 4. 行为保持与不做事项

必须保持现有CLI参数、确认语义、持久化schema、模型选择冻结、任务隔离、Docker与输出安全检查。Docker失败不回退宿主机。旧记录仍可读取。

不新增SRS/SDD生产流程、多轮Agent、SQLite、PR/CI、自动合并、TUI、独立Bugfix、多仓库或插件框架。独立功能缺陷记录在报告；阻碍迁移或影响本次安全边界的缺陷只做有证据的最小修复并单列。

## 5. 必须交付的证据

- 相关现有测试和CLI help 检查；Ruff、mypy、git diff --check。
- 公共入口的真实业务组合测试：生成→应用→验证；批准拒绝、执行失败、验证失败修复提案路径。可替换模型与Docker外部边界，不能整体mock掉正在验证的编排服务。
- 依赖检查：runtime/workspace不导入transport；终端不直接构造ApprovalRecord或执行Docker链路。
- 参数和schema兼容检查；证明结构迁移未静默扩大原操作权限。
- 说明测试范围与基线失败；没有启动真实Docker/模型就不宣称真实端到端通过。
- 最终报告列出实际目录、迁移前后调用链、删除的包装、仍有的耦合及后续入口。

判断完成的标准是相同业务可从CLI或非交互调用，职责集中且已有行为可验证；不是文件数增加或V2目录已经摆齐。
