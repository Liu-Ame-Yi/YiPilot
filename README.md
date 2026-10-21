# Yipilot

Yipilot 是本地运行、面向单个 Python 仓库的自动研发交付项目。目标流程是需求/PRD → SRS 与评审 → SDD 与评审 → 实现 → 验证/修复/代码评审 → 分支与 PR/MR → 可选 CI → 交接，**不自动合并**。

当前已接入真实模型提案；应用和验证代码已切换为远程 OpenSandbox Runner，正式放行需要完整 CI 与服务器业务验收；服务器尚未部署本次业务版本。完整自动流程尚未实现。准确边界见 [当前状态](docs/product/status.md)。

## 开始阅读

- [文档入口](docs/README.md)：当前文档与历史资料的区别。
- [产品定义](docs/product/vision.md)：输入输出、人工介入、完成条件。
- [V2 架构提案](docs/superpowers/specs/2026-09-07-yipilot-v2-architecture.md)与[实施路线](docs/product/roadmap.md)。
- [快速开始](docs/guides/getting-started.md)与[交互式终端](docs/guides/interactive-terminal.md)。

## 安装与启动

本地需要 Python 3.11+ 和 Git；执行应用/验证需要配置远程 Runner、SecretStore 凭据引用和可用的 SSH 隧道或 HTTPS。SDK、Docker 和工具镜像只在服务器安装。新版本部署前不要按历史本机 Docker 命令执行业务。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
.\.venv\Scripts\Activate.ps1
yipilot --help
yipilot --project C:\work\demo --settings-dir C:\work\yipilot-settings
```

会话内使用 /task、/model、/run、/apply、/verify；/logs 与 /cancel 当前不可用。/run 只生成提案，/apply 与 /verify 是单独的确认步骤，当前并非一键自动研发。

代码生成子命令没有 --planner 参数；不要继续使用旧 fake/replay code generate 或 code apply --prepare-only 示例。质量文档命令中的 fixture 模式也不能当成真实需求分析。

.yipilot 保存本地任务、工作区和证据，不提交到源码仓库；密钥使用用户设置或环境变量引用，不放进项目材料。当前操作方式见指南，旧规划书和 M0–M8 记录只作历史参考。
