# YiPilot 私有仓库与服务器代码同步

日期：2026-10-19。用户指定新建 `YiPilot`，按已确认的私有仓库方案执行。

仓库：[Liu-Ame-Yi/YiPilot](https://github.com/Liu-Ame-Yi/YiPilot)。本机通过现有 Git Credential Manager 登录推送；服务器通过独立、仅限此仓库的只读部署密钥拉取。

## 同步范围

- 本机 Git 工作区为源码编辑位置，保留已有提交历史。首次发布使用 `codex/github-sync` 分支，包含当前项目源码、测试、锁文件、文档及已有 SQLite 整合改动，不代表这些历史改动由本轮重新实现。
- `.yipilot` 运行数据、虚拟环境、本地代码索引、临时输出及本地 `.env` 文件排除在发布范围。`.env.example` 和 `.env.template` 可保留模板。
- 服务器代码检出位置为 `/srv/yipilot/source`；每次部署绑定一个确切提交 SHA，避免运行时跟随分支漂移。
- OpenSandbox 仍是已安装的独立服务，其配置和密钥在 `/etc/opensandbox`，Python 环境在 `/opt/opensandbox/venv`；Git 同步不会覆盖这些目录。
- 本轮仅建立代码分发路径。YiPilot 的 OpenSandbox 调用改造、ExecutionRunner、任务输入快照和证据/审批/apply 整合仍待实现。拉取现有代码不会自动获得这些功能，也不代表完整业务已在 Linux 服务器启动。

## 连接与凭据

本机连接服务器：

```powershell
ssh huawei-opensandbox
```

服务器访问此仓库使用 SSH 别名 `github-yipilot`，连接 `ssh.github.com:443`；主机公钥和指纹由 GitHub 官方 HTTPS `/meta` 发布数据交叉核对后保存，启用严格主机校验。

服务器上的私钥为 `/root/.ssh/yipilot-github-readonly_ed25519`，权限 0600。私钥保留在服务器；GitHub 只保存对应公钥，部署密钥 `read_only=true`。本机个人 GitHub 凭据没有复制到服务器。

## 每次更新的最小流程

1. 本机完成源码修改并运行适用检查，审查本次提交文件，提交后推送到 `origin`。
2. 记录待发布提交 SHA。运行中的任务须保留原版本执行环境；将来 Runner 更新时先停止接收新任务，并处理在途任务，再切换执行服务。
3. 服务器确认代码目录无本地修改，fetch 对应分支，再检出确切 SHA。只检出 Git 版本，不用 `reset --hard` 或全局清理覆盖未知本地内容。
4. 核对本机 HEAD、GitHub 分支头和服务器 HEAD 完全一致。安装依赖、数据库变更、服务启动和健康检查属于后续具体发布步骤，不能用一次 fetch/checkout 代替。

本机查看当前版本：

```powershell
git rev-parse HEAD
```

服务器查看代码版本和工作区：

```bash
git -C /srv/yipilot/source rev-parse HEAD
git -C /srv/yipilot/source status --short
```

同步先按需执行，不安装定时 pull、Webhook 自动重启或自托管 Actions runner。GitHub 仓库中既有离线 CI 随推送触发；真实模型测试仍按其显式环境开关控制。

GitHub 部署密钥的权限和用途见[官方说明](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)。服务器 OpenSandbox 的实际安装和隔离验收边界见[基础安装记录](2026-10-19-opensandbox-server-installation.md)。
