> **Historical reference / Superseded — 文档整理：2026-09-07。** 本文保留旧阶段设计与实施记录，不再作为当前待办或产品范围。当前目标见 [产品定义](../../product/vision.md)，技术设计见 [V2](../specs/2026-09-07-yipilot-v2-architecture.md)，实际能力见 [状态页](../../product/status.md)。下文原状态、强制要求及命令仅在原历史范围内成立。

# Yipilot 容器化分发决策

- 状态：Accepted
- 日期：2026-08-06
- 适用范围：GitHub clone 后的本地 CLI 使用与持续集成
- 关联规格：`2026-08-05-yipilot-python-design.md`

## 1. 决策

Yipilot 的官方运行入口采用 Docker。使用者 clone 仓库后，无需自行安装或配置 Python、uv、依赖版本或 Git Python 绑定；通过 `docker build` 和 `docker run` 即可启动 CLI。

本机 Python + uv 保留为维护者和贡献者的快捷开发路径，但不是 GitHub 使用者的必需前置条件。两种路径必须由同一个 `pyproject.toml` 和 `uv.lock` 定义依赖，不能形成两套依赖事实源。

首版不引入 `compose.yaml`、数据库或服务端。Yipilot 当前是单进程 CLI；Docker Compose 仅在未来出现 FastAPI 服务、多进程或多个基础设施服务时重新评估。

## 2. 用户启动合同

GitHub README 必须提供并持续验证以下最小路径：

```powershell
git clone <repository-url>
cd yipilot
docker build -t yipilot:local .
docker run --rm -it yipilot:local --help
```

若 Yipilot 要操作用户已有的目标 Git 仓库，用户必须显式 bind mount 目标目录；容器不会隐式读取主机目录：

```powershell
docker run --rm -it `
  --mount type=bind,src="D:\\path\\to\\target-repo",dst=/workspace `
  yipilot:local <yipilot-command> /workspace
```

命令的最终参数形式以 CLI 规格为准。文档不得承诺容器可在未挂载目标仓库时修改主机文件。

## 3. Dockerfile 合同

Dockerfile 是单阶段可运行镜像，基于受支持的 Python 3.11+ 基础镜像，并满足：

1. 复制 `pyproject.toml` 与 `uv.lock` 后安装锁定的运行依赖，再复制应用源码；构建不得忽略锁文件。
2. 安装 Git CLI，因为 M2 的 repository cache/worktree 功能依赖受控 Git 子进程。
3. 创建并使用非 root 用户运行 CLI。
4. 提供 `yipilot` 作为镜像 entrypoint 或等价默认命令；`--help` 和 `--version --output json` 无须网络、模型密钥或目标仓库即可成功。
5. 不包含 API key、provider 配置、SSH 私钥、主机 `.gitconfig`、Docker socket 或用户项目目录。
6. 不执行 Git push、merge 或真实模型调用作为镜像构建副作用。

`.dockerignore` 至少排除 `.git/`、`.venv/`、`.yipilot/`、`.worktrees/`、本地 `.env`、编辑器缓存与测试临时文件。它必须与 `.gitignore` 同步审查，但不能假定两者语义相同。

## 4. 密钥、Git 身份与主机边界

- 镜像、Dockerfile、README 示例和 Compose（若未来加入）均不得保存明文 API key。
- M3 的 provider key 只在运行时通过已批准的 SecretStore 或显式环境变量引用提供；不得以 `ARG`、`ENV` 或镜像层写入。
- 若未来某个受批准动作需要生成 Git commit，Git 作者身份以显式运行时设置或目标仓库自身配置提供；不得默认挂载用户主机的完整 home 目录或 SSH 目录。
- 不挂载 `/var/run/docker.sock` 或 Windows Docker daemon 管道。容器化运行不是获得主机 Docker 管理权限的理由。

## 5. Makefile 与跨平台命令

Makefile 是可选的 Linux/macOS/CI 快捷层，不能成为 Windows 使用者运行项目的必需工具。官方事实命令是 Docker CLI；维护者本机开发使用 `uv` 命令。若将来加入 Makefile，其目标只能封装以下等价动作：`image`、`docker-smoke`、`test`、`lint`、`typecheck`，不得隐藏密钥传递或改变默认安全策略。

## 6. 变更边界：M0-R 容器化交付

当前正在执行的 M0 不因本决策临时扩大范围。M0 的 CLI skeleton、领域模型与原生测试先按既定计划完成并验收。

M0 验收后，创建独立的 `M0-R`（Release Packaging）任务，依赖 M0 已合并的 `pyproject.toml`、`uv.lock` 与 `yipilot --version`。M0-R 只交付：

- `Dockerfile` 与 `.dockerignore`；
- README 的 Docker 快速启动、目标仓库挂载和安全说明；
- GitHub Actions 的镜像构建与 Docker smoke test；
- 可选 Makefile 快捷层（不作为完成前置条件）。

M0-R 明确不交付 FastAPI、SSE、Compose、数据库、镜像注册表发布、真实模型调用、API key 管理变更或 Docker 沙箱 Executor。Docker 镜像发布到 GHCR 和 GitHub Release 只在镜像构建稳定、版本策略确定后另立任务。

## 7. M0-R 验收条件

在干净的 Docker 环境中，以下检查必须通过：

```text
docker build -t yipilot:ci .
docker run --rm yipilot:ci --help
docker run --rm yipilot:ci --version --output json
```

此外，测试必须证明默认容器用户不是 root，构建上下文未包含 `.env`、`.venv`、`.yipilot` 或 `.worktrees`，并且构建/启动不要求网络、模型凭据或主机 Git 凭据。GitHub Actions 在 pull request 与 main push 上执行相同 smoke test。

## 8. 后续演进

M3 若需要对真实 Agent 做安全隔离，必须另写 Executor sandbox 设计；M0-R 的可运行镜像不自动构成最小权限安全沙箱。未来服务化后才评估 `compose.yaml`；若使用 Compose，配置须作为高权限可执行配置接受代码审查。
