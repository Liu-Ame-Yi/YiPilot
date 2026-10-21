# OpenSandbox 固定工具镜像

状态：**工具镜像与受信样例已验收，业务调用尚未切换。** 证据见[第四阶段前置记录](../../docs/research/2026-10-21-opensandbox-toolchain-validation.md)。

`yipilot-tools-v3` 只允许 [image.json](image.json) 中的完整 Docker image ID。它与 `yipilot-python-v2` 的 Python 基础镜像分别绑定，不能交换 image/profile。旧 Job 的合同、指纹和默认值不变；新增 profile 不授权执行旧业务请求。

镜像包含 Git、Python 3.12.15，以及从当前 uv.lock 导出的 YiPilot 基础和开发依赖，包括 pytest、Ruff、mypy。没有复制项目源码、模型密钥或 Runner SDK。控制进程仍以受限 root 身份启动，任务身份由固定 profile 强制为 10001:10001；不得直接把镜像当作等价的隔离环境。

构建仅在可信宿主联网，APT 验证 Debian 签名，Python 包采用锁文件的版本与 hashes，禁止源码包。任务期间维持 deny-all 网络。Ruff/mypy 缓存写入受限 `/tmp`，pytest 使用 `-p no:cacheprovider`，防止缓存污染源码收集。

在具备 Docker、uv 和 Linux amd64 的可信构建机运行：

```sh
python deploy/toolchain/build.py /path/to/YiPilot /private/new-release/image.json
```

构建收据不能覆盖。`image.json` 固定本次验收产物，源码测试会校验其 Dockerfile/uv.lock 摘要；升级依赖或镜像必须重建、更新 image/profile 绑定并重新验收。

APT 使用签名仓库当前的软件包，构建时间也会进入镜像元数据，因此重新构建不保证得到同一 image ID。部署使用已验收的实际镜像，不通过 tag 或重建结果替代它。当前服务器已保存私有 `docker image save` 归档及 SHA-256，路径和摘要在验收证据中；迁移到另一 Docker 主机时，先验证归档摘要，再 `docker image load` 并核对完整 image ID。这个 ID 不是可从仓库拉取的 RepoDigest；未发布到镜像仓库，K8s 分发尚未实现。

`acceptance.py` 只在已排空的测试 Runner 上以 root 执行，使用自有小型 Python 工程：Git 应用补丁、pytest、Ruff、mypy，再验证混合两任务和四轻任务的真实重叠、内存及清理。它会提交 Job；不是只读 doctor。隔离回归使用 `deploy/opensandbox/acceptance.py --tools`。两组测试必须顺序运行，不能与业务或其他隔离探针共用活跃资源。

当前 768 MiB 总预算包含每任务 64 MiB sidecar。实测支持一个申请 512 MiB 的小工具任务与一个申请 128 MiB 的轻任务，或四个申请 128 MiB 的轻任务；不保证任意项目、重构建或四个完整验证同时运行。1 GiB 请求明确拒绝，不自动缩小预算。后续业务采用新预算时必须有新策略、明确的审批展示和新指纹，现有 1 GiB 审批不复用。项目额外依赖需要另行固定镜像，不能在任务内临时联网安装。
