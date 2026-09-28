# NexIoT FreeSWITCH 源码分支

维护分支：`codex/nexiot-freeswitch`，基于官方 `v1.11.3`。RTP 扩展头/CSRC 修复已直接提交在 `src/switch_rtp.c`，回归检查在 `tests/nexiot/`。后续修改、合并官方更新和发布都在这个 FreeSWITCH Git 仓库中进行。

本目录从当前分支的**已提交源码**构建镜像。FreeSWITCH 的输入由 `git archive HEAD` 生成；构建过程不会下载另一份官方 FreeSWITCH，也不再应用 `.patch`。`build.lock.json` 只记录官方基线、发行版本、Debian 摘要及 Sofia-SIP/SpanDSP 依赖来源。镜像中的 `sources.lock.json`、`context.manifest.json` 和 OCI revision 记录实际分支 commit，便于定位和回滚。

## 构建镜像

在本仓库根目录执行；需要 Python 3.9+、Git 和 Docker，宿主机不需要 C/C++ 工具链：

```bash
git switch codex/nexiot-freeswitch
python3 docker/nexiot/build_image.py --platform linux/amd64 --jobs 2
```

当前默认镜像为 `nexiot/freeswitch:1.11.3-nexiot.2`，版本来自 `docker/nexiot/build.lock.json`。国内主机访问 Debian 源慢时可加 `--debian-mirror mirrors.aliyun.com`；APT 签名校验保持启用。ARM 主机如需本机镜像，选择 `--platform linux/arm64`；为 amd64 服务器构建时优先使用 amd64 Linux 构建机。

源码及构建文件需要先提交；脚本拒绝未提交的变更和未跟踪文件，确保镜像内容对应明确 commit。忽略文件不会进入 Git 归档。不要把运行凭据提交进源码仓库。

也可以先生成上下文，再直接用 Docker：

```bash
python3 docker/nexiot/build_image.py --prepare-only \
  --context-dir /tmp/nexiot-freeswitch-context
# context-dir 必须是新目录；再次构建时使用另一个目录名。
docker build --platform linux/amd64 --build-arg BUILD_JOBS=2 \
  -t nexiot/freeswitch:1.11.3-nexiot.2 /tmp/nexiot-freeswitch-context
```

原始 Dockerfile 含版本占位符，应通过上述脚本生成上下文。依赖源码按 commit 缓存并校验 SHA256；FreeSWITCH 归档只从本地 Git 生成。Debian APT 尚未固定 snapshot，因此不同时间重建可能产生不同镜像 ID，发布时应记录实际 ID 或受控仓库 digest。

## 运行与验收

```bash
docker run --rm nexiot/freeswitch:1.11.3-nexiot.2 -version
docker image inspect --format '{{.Id}} {{index .Config.Labels "org.opencontainers.image.revision"}}' \
  nexiot/freeswitch:1.11.3-nexiot.2
python3 tests/nexiot/test_rtp_headers.py src/switch_rtp.c
```

C 检查在 Linux Docker 构建中自动以 ASan/UBSan 执行。macOS ASan 运行环境不可用时可在本地加 `--sanitizers undefined`，Linux 构建仍会保留两种 sanitizer。

镜像提供 `/usr/bin/freeswitch` 和 `/usr/bin/fs_cli`；默认 `CMD` 仅打印版本，实际服务使用显式 XML、声音目录和运行参数启动。现有 NexIoT `poc.py` 启动方式可继续使用，新建/升级实例显式选择该镜像 ID。它不包含真实账号或生产配置。当前手机 profile 继续使用 VP8；启用 H264 的实例需配置 `h264Module=mod_av`，1.11 已删除 `mod_h26x`。

完整 SIP/RTP 验收使用 NexIoT 部署目录已有脚本，在 Linux Docker 主机执行：

```bash
# 先进入含这些脚本的 NexIoT deploy/ 目录。
python3 -B test_image_modules_live.py --image nexiot/freeswitch:1.11.3-nexiot.2 --result /tmp/fs-modules.json
python3 -B test_mobile_routes_live.py --image nexiot/freeswitch:1.11.3-nexiot.2 --repeat 5
python3 -B test_rtp_extensions_live.py --image nexiot/freeswitch:1.11.3-nexiot.2 --result /tmp/fs-rtp.json
```

这些检查不替代真机声音/画面、公网 NAT 或 SRTP 验收。切换生产需要保留旧镜像和运行配置，并重建容器；单纯 `docker restart` 不会更换镜像。

## 合并官方更新

`origin` 当前保留为官方 `https://github.com/signalwire/freeswitch`，本地 `master` 也保留。维护分支不把官方分支设置成默认 pull/push 目标。

先查看官方 release，选择明确的正式 tag，再合并。以下变量应填写实际发布的 tag；不要把示例当作已存在的新版本：

```bash
git switch codex/nexiot-freeswitch
git fetch origin --tags
# 将 FS_UPSTREAM_TAG 设置为本次确认的正式 tag。
git merge --no-ff "$FS_UPSTREAM_TAG"
```

也支持 `git merge --no-ff origin/v1.11` 合并官方维护分支；正式镜像优先采用已发布 tag，避免混入未发布变更。不要在日常升级时 reset 维护分支到官方分支，否则会丢掉自己的提交。

合并后：

1. 若 `src/switch_rtp.c` 有冲突，对照收包、jitter buffer 和 raw 转发路径解决；官方采用等效修复时，删除重复实现并调整检查，不能机械地保留两份修复。
2. 更新 `docker/nexiot/build.lock.json` 的 `upstreamTag`、`upstreamCommit`、`freeswitchVersion` 和 `imageVersion`。`upstreamCommit` 用 `git rev-parse "$FS_UPSTREAM_TAG^{commit}"` 获取；同时核对官方依赖要求及模块变更。若选择未发布的维护分支提交，应先完成相应基线/版本校验适配，不冒用旧 release 的身份。
3. 运行边界检查，提交解决冲突及版本更新，再构建新镜像并执行隔离 SIP/RTP 验收。
4. 验收后创建自己的发布 tag，例如本次可使用 `nexiot-1.11.3-2`；发布 tag 不重复覆盖，保留每个已发布 commit 和镜像 ID。

源码补丁和构建配置各自独立成提交，后续 `git log`/`git merge` 可查看来源。`git merge` 不会重复应用已在历史中的同一提交；上游代码变化仍可能需要手工解决冲突。

## 保存到自己的远程仓库

当前只创建本地分支。准备好自己的空仓库或官方 fork 后，添加单独远程并推送：

```bash
# 将下面的占位地址替换为自己的仓库；origin 继续指向官方。
git remote add fork <自己的FreeSWITCH仓库地址>
git push -u fork codex/nexiot-freeswitch
```

之后从 `origin` 拉官方更新，向 `fork` 保存自己的维护分支；镜像仓库是另外的发布目标。
