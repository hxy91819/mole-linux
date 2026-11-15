# Linux DevCloud 存储设计

本机（Linux DevCloud）存储布局、设计取舍、决策与踩坑记录的唯一设计入口。
本文回答“为什么这样放、为什么这样清、哪些坑已经踩过”；具体到某个路径当前能不能清，
以 [storage-rules](../.claude/skills/devcloud-patrol/references/storage-rules.md) 为唯一规则源；
现行任务、命令、部署和复现步骤见 [DEVCLOUD_AUTOMATION](DEVCLOUD_AUTOMATION.md)。
本文的数字是撰写时实测（2026-11-14），容量与挂载以 `lsblk` / `df` 现场输出为准。

## 1. 目标与底线

### 目标

- 两块盘长期可用，不再回到“可用空间为 0、进程随机 ENOSPC”的状态。当前每日检查的告警线是
  `/` 与 `/data` 普通可用都不少于 150 GiB（见每日检查 prompt）。
- 不影响正在使用的业务、Agent、会话、数据库和正在跑的任务。
- 只清“非近期在用且可恢复”的东西；数据类默认不动，交 owner 决定。
- 在一台新机器上，除密钥外可以按文档复现同一套布局、软链、定时任务和检查。
- 知识和证据留在本仓库；一次性数字留在本机账本，不随仓库传承。

### 底线

owner 2026-11-04 的原话是这套体系的底线：

> 如果是正在用的业务和 Agent 一定不要影响他们使用。这个是底线，我们要清理的那些是没有正在用的。

由这条底线派生出三条硬规则：

1. **进程引用三态**：0 = 没有引用，1 = 有引用，2 = 查不清。2 一律保留；只有 0 才进入删除候选。
2. **数据类不自动删**：会话、凭据、在用数据库、命名卷、备份、迁移副本、唯一私有文件，
   未经 owner 明确批准不自动删除；`retained_volumes` 等显式保留声明优先于任何启发式。
3. **恢复性先于可回收性**：能重新下载/生成，或已经完成远端归档并核验 SHA，才算可恢复；
   目录叫 `cache`、`tmp`、`old` 都不构成证据。

### 范围

本文覆盖宿主机的磁盘布局、缓存、临时目录、Docker、包管理器 store、worktree/clone、
BB 数据与自动化形态。它不覆盖 Mole 产品自身的清理逻辑，也不覆盖服务暴露巡检
（后者见 `$devcloud-patrol` 与 `devcloud-exposure/`）。

## 2. 本机存储布局

### 2.1 盘与挂载

| 设备/挂载 | 容量 | 文件系统 | 角色 | 2026-11-14 实测 |
|---|---:|---|---|---|
| `/dev/vda` → `/dev/vda1` → `/` | 500 GiB | xfs | 系统盘：OS、工具链、`/root` 下大部分状态、`/var/tmp`、根 swap | 272G used / 229G avail / 55% |
| `/dev/vdb` → `/data` | 1000 GB 级，ext4 可用 984 GiB | ext4 | 数据盘：项目源码、Docker、缓存、会话、备份 | 722 GiB used / 222 GiB avail / 77% |
| `/dev/vdb[/system-tmp]` → `/tmp` | 同 `/data` | ext4 bind | 临时目录，物理在数据盘 | 与 `/data/system-tmp` 同一 inode |
| `/var/tmp` | 同 `/` | xfs | 系统盘上的持久临时目录 | 不在数据盘 |
| `/swapfile` | 8 GiB | file | 根盘 swap，当前启用 | `swapon --show` 可见 |
| `/data/swapfile` | 16 GiB | file | 数据盘旧 swap，2026-06-23 停用 | 文件仍在，未自动删 |

两条最容易记错的结论：

- `/` 是 **500 GiB**，不是 1 TiB；`vda` 也只有 500 GiB。历史上曾把控制台扩容或 `vdb`
  的容量误记到 `/`，之后一律以 `lsblk` / `df -B1` 为准。
- `/tmp` 不是系统盘。清空 `/tmp` 腾的是 `/data`；真正腾系统盘的是 `/var/tmp`、`/root`、
  `/var` 和 Docker 的根盘残留。`findmnt /tmp`、`stat -c '%d %i' /tmp /data/system-tmp`
  可以在一秒内证明这一点。

### 2.2 数据放在哪里

| 位置 | 内容 | 为什么在这里 | 清理/恢复 |
|---|---|---|---|
| `/root/.cache`（除两个共享缓存） | 浏览器、Playwright、工具缓存 | 2026-11-14 从 `/data/cache-migrated` 迁回；系统盘已有空间 | 可重建；不自动整目录清 |
| `/root/.cache/go-build`、`/root/.cache/golangci-lint` | 全机共享 Go/lint 缓存 | 软链到 `/data/bb-data/thread-storage/.shared-cache/`；见 3.1 | 只由 `go_cache_trim.py` 按 24h 修剪 |
| `/root/.npm` | npm 的 `_cacache`、`_npx`、`_logs` | 2026-11-14 迁回系统盘；`_npx`/`_logs` 可能被在跑的 `npm exec` 持有 | `_cacache` 可重建但不自动清；不整目录删 |
| `/root/go/pkg/mod` → `/data/root-dev-cache/go-pkg-mod` | Go module cache | 体积大、跨项目共享、重下载成本高 | 只有 owner 批准的 `go clean -modcache` |
| `/data/.pnpm-store` | 共享 pnpm store，内含 v3/v10/v11 | 版本化 store 体积大、跨项目共享 | 每周日 05:30 逐版本 `pnpm store prune`；见 3.4 |
| `/data/docker/lib` | Docker data-root（overlay2、volumes、buildkit） | 数据盘容量大；日志走 journald | 只用 Docker owner 命令；见 3.3 |
| `/data/code` | 主 checkout、`*-worktrees` 附加 worktree | 这里是工作负载本身 | 只清附加 worktree，主 checkout 永不动；见 3.5 |
| `/data/codex` | Codex home、sessions、worktrees、backups | 会话数据，体积大，不能当缓存 | 不自动删；备份待 owner 决策 |
| `/data/bb-data` | BB `thread-storage`、共享缓存 | BB 的线程产物和共享缓存；`/root/.bb/thread-storage` 软链到这里 | 只修剪 `.shared-cache`；线程目录按 BB/线程状态处理 |
| `/root/.bb` | BB 主库 `bb.db`、artifacts、插件状态 | 系统盘上的 BB 自有数据 | 不自动删；升级备份按 3.6 处理 |
| `/data/artifacts`、`/data/backups`、`/data/bb-backups`、`/data/bb-archives` | 产物与备份 | 数据类，可能含唯一副本 | 不自动删；owner 决策 |
| `/data/root-storage-20260912` | 2026-09-12 迁到数据盘的 13 个目录的剩余部分 | 早期系统盘只有 100 GiB 时的迁移目标 | 现存 `.acpx/sessions`、`.cursor`、`.local` 等会话/状态；不按日期清 |
| `/data/root-migrated` | Cursor/BB 部署检查点等 | 同上 | 数据/状态，不自动删 |
| `/data/system-tmp` | `/tmp` 的物理目录 | 数据盘承接临时写入 | 每天 03:30 整树 3 天清理；见 3.2 |
| `/data/ops`、`/data/output`、`/data/metrics`、`/data/tools` | 运维产物、任务输出、账本、不可变快照 | 运行证据与部署物 | 各自保留策略；不是通用缓存 |
| `/data/itfs` | ITFS FUSE 缓存与运行数据 | 长期服务，承载 Codex CLI 与凭据 | 全部保留；见 storage-rules「已知来源指纹」 |
| `/data/swapfile` | 停用的 16 GiB 数据盘 swap | 历史遗留，`swapoff` 会拖慢关机 | 待 owner 决策；当前不自动删 |

`/data/cache-migrated` 已于 2026-11-14 删除，不再存在。`/data/root-dev-cache` 现在只剩
`go-pkg-mod`。`/data/root-storage-20260912` 只剩会话/状态类目录，不是“旧日期目录可整棵清”。

### 2.3 为什么是这个形状

- 2026-10-10 之前系统盘只有约 100 GiB，大量缓存和工具目录被软链到 `/data`；同日在线扩容到
  500 GiB。2026-11-14 owner 批准把可重建的缓存/工具目录搬回系统盘，共 11 项：`.nvm/.cache`、
  `.pyenv/cache`、`.codenexus/cache`、`.rustup`、`.cursor-server`、`.kiro-server`、
  `.codebuddy-server-cn`、`.pulumi`、`.chromium-browser-snapshots`、`.cache`（排除 `go-build`）、
  `.npm`。首轮迁回 9 项；`/root/.cache` 与 `/root/.npm` 因浏览器和 Vite 持有引用，第二轮重启
  进程后迁回。迁回后 `/data` 可用空间明显回升，系统盘仍有充足余量。
- 仍留在 `/data` 的东西分三类：**会话/数据**（codex、cursor、bb-data、backups、数据库）、
  **大而共享的缓存**（Go module、pnpm、Docker、共享 Go/lint）、**工作负载本体**（code）。它们
  不是“懒得搬”，而是搬回去会碰到活跃进程、需要停写窗口，或会把数据盘的问题变成系统盘的问题。
- 系统盘放“系统 + 工具链 + 可重建缓存”，数据盘放“工作负载 + 大数据 + 会话/备份”。这个分工
  比“所有东西都塞数据盘”更稳：系统盘满会直接影响登录、包管理和 Agent 启动；数据盘满主要影响
  项目和缓存，二者故障面不同。

### 2.4 计量规则

- `df -B1` / `lsblk` 是容量与挂载的唯一事实源；`du` 只用于归因和找候选。
- `/data` 的 inode 用量是千万级（历史口径约 2900 万；2026-11-14 `df -i` 实测 19.5M used /
  65.5M total）。无论哪个量级，单线程 `du` 都会跑十几分钟并被超时打断；按顶层目录并行测，
  输出写到 `/var/tmp`，不要写到 `/tmp`（那会把测量结果又写回数据盘）。
- `du -x` 不跟随软链。共享缓存、`/root/go/pkg/mod`、`~/.codex` 这类入口必须先
  `readlink -f` 或 `du -sL`，否则会把软链本身当成几十字节。
- 一次扫描只报“候选”或只报“总量”都会误导：必须同时给总量与候选；超时、权限错误、
  扫描不完整一律记“未测”，不能记 0。2026-11-14 曾出现 `/tmp` 体量很大但测量超时，
  报告里只剩候选 0 的教训。协调线程提到过漏看 196 GiB；本机账本里找不到这个数字
  （唯一的 196 是 `/var/log/cron` 的 196 MiB），因此只把它当作“测量超时会漏看”的案例，
  不当作已核实容量。
- 管道会掩盖退出码：`timeout ... du | sort | head` 的 `$?` 是最后一个命令的 0，不是 `du`
  的 124。每个目录单独记 rc 和耗时；不完整扫描不参与总量，也不喂给删除计划。
- “0 字节”可能只是错误/超时的哨兵值：深度盘点曾把 `/data/docker`、`/data/codex` 等报成 0，
  有界 `du` 补测后是 95/33/33/12 GB。看到 0 先重测并标注 unknown，不要拿它和 `df` 对账。
- `du` 逐目录相加会重复计算硬链接和共享层；`df` 的净变化又包含并发写入。回收量以执行前后
  `df` 为准，同时把对象实占、镜像显示大小、df 净变分列，不把三者混成一个“已释放”数字。
  使用率也要写清分母：`used/(used+avail)` 会排除 ext4 保留块，和 `used/total` 能差几个百分点。

常用只读入口（都不会删除）：

```bash
./mo devcloud
./mo devcloud --json
./mo status --json
./mo analyze --json /data
```

任何清理动作都要另配 dry-run、owner/进程检查、精确路径证明和可恢复策略；上面的命令本身
只用于发现和取证。

## 3. 关键设计

### 3.1 共享 Go/lint 缓存

**布局**

```text
/root/.cache/go-build        -> /root/.bb/thread-storage/.shared-cache/go-build
/root/.cache/golangci-lint   -> /root/.bb/thread-storage/.shared-cache/golangci-lint
实际目标: /data/bb-data/thread-storage/.shared-cache/{go-build,golangci-lint}
```

**为什么放在这里**

这是 lightspace 阶段 312–314 的产物，但作用域是全机：所有项目、所有 Agent、所有终端的
Go 构建和 golangci-lint 都写这一份缓存。放 `/data/bb-data/thread-storage` 不是因为它属于
某个线程，而是因为 BB 的 Codex 沙箱可写根只有工作区、`TMPDIR`、Git 目录和
thread-storage，缓存目录不在其中（根因在 BB `apps/host-daemon/src/runtime-manager.ts` 的
`runtimeWorkspaceWriteRoots`，再由
`plugins/provider-codex/src/session-params.ts` 下发）。在沙箱里直接写
`~/.cache/go-build` 会只读失败；软链到已经被放开的 thread-storage 是最小改动，不用改
`sandbox_mode`、不用放开整个 `$HOME`、也不用改 BB 源码。

**维护**

- 每小时第 37 分，`go_cache_trim.py --execute --quiet`。
- 根路径取 `go env GOCACHE` 的 realpath，lint 根取 `readlink -f /root/.cache/golangci-lint`；
  不假设物理路径固定在某个日期目录。
- 只删名字匹配 `^[0-9a-f]{64}-[ad]$` 的普通文件，且 mtime 超过 24 小时；目录、README、
  `trim.txt`、`testexpire.txt`、fuzz、符号链接都不动。
- 修剪前核对 README 首行：Go 是 `This directory holds cached build artifacts from the Go build system.`，
  lint 是 `This directory holds cached build artifacts from golangci-lint.`；不符就不删并报错。
- Go 只会在条目被使用且 mtime 超过 1 小时时刷新 mtime（`cmd/go/internal/cache` 的
  `mtimeInterval`）。因此 24 小时门槛不会误删日内或隔夜还会复用的条目；2 小时会太激进，
  5 天则会让冷条目长期占盘。
- 每日 DSH 检查解析同一个 realpath，检查总量不超过 100 GiB、且没有超过 1520 分钟的
  `*-[ad]` 文件。`.shared-cache` 不是线程遗留，不得当普通 tmp 或线程目录删除。

**长期修法**：BB 把 `$HOME/.cache/go-build`、`$HOME/.cache/golangci-lint` 加入
`runtimeWorkspaceWriteRoots` 或 provider 的 `additionalWorkspaceWriteRoots` 后，软链和
对 `/data` 的依赖都可以去掉。在此之前，软链是现行方案。

**新机器 setup**：软链、验证和旧缓存处置的完整命令见
[DEVCLOUD_AUTOMATION](DEVCLOUD_AUTOMATION.md)，本文只解释为什么放在这里。

### 3.2 临时目录

- `/tmp` 是 `/data/system-tmp` 的 bind，清它只腾数据盘；`/var/tmp` 在系统盘。
- systemd-tmpfiles 自 2026-11-04 起不再按年龄清 `/tmp`：`/etc/tmpfiles.d/tmp.conf` 为
  `q /tmp 0700 root root -`（age 列为 `-`）。原来的按文件 3 天删除曾把闲置 worktree 里
  已跟踪的文件删掉（32 个 `/tmp` worktree 中 7 个受损），遇到孤立 FIFO 还会卡死，
  导致 tmpfiles 清理停过 4 天 20 小时。
- 现行清理由 crontab `# devcloud-clean-tmp` 每天 03:30 执行不可变快照里的 `clean-tmp`：
  先 `/var/tmp`，再 `/data/system-tmp`；普通项整树闲置 3 天，结构精确匹配的 Go build、
  pnpm store、Node compile cache 按树内最新普通文件 mtime 满 24 小时处理。
- 清理器不按“文件名像 tmp”判断，也不按根目录 mtime 判断。它保留仓库、凭据、数据库、
  备份、socket、符号链接、硬链接、外部可写树、挂载点和有进程引用的树；未知即保留。
- 每批最多 128 项共用一次完整 `/proc` 快照，最多 8 项并发；隔离后和最终 sink 只增量检查
  新增或 starttime 变化的 PID。取消信号会停止排队、终止 Mole 子进程组并恢复在途隔离项；
  遗留隔离项阻止下一轮。
- 预算：完整计划 240 秒；普通树 5 秒、缓存树 60 秒；`/var/tmp` 300 秒、`/data/system-tmp`
  1400 秒；外层 1790 秒；余项递延，不设数量上限。具体阈值以 storage-rules 和 crontab 为准。

### 3.3 Docker

- data root 是 `/data/docker/lib`；日志驱动是 `journald`，所以没有“Docker 日志文件”可以按
  路径删，日志归 journald 管。
- `overlay2` 不是按路径的清理目标：它会对挂载层双重计数；只使用 Docker 自己的 metadata
  和 owner 命令，绝不递归删目录。
- 每小时第 7 分，`docker_leftovers.py --execute --quiet`：
  - 满 1 小时的匿名测试 DB 卷：无任何容器引用、64 位十六进制名称、仅
    `com.docker.volume.anonymous` 标签、内容是 MySQL/PostgreSQL 数据目录或空；
  - 满 24 小时的悬空镜像；
  - 满 30 天、无任何容器引用、同一 repository 有更新镜像的旧标签；
  - BuildKit：既有 `until=72h`，再用 `--max-used-space 10GB`（旧 BuildKit 不支持时改用
    `--keep-storage 10GB`）限总量；
  - `mole.protect=true` / `devcloud.protect=true` 一律保留；`retained_volumes` 优先于一切；
  - 在 fork-maintenance 的 `run.lock` 上非阻塞 flock，拿不到锁就跳过，不绕锁。
- `docker system df` 在本机约 29 秒，每轮最多取一次并复用，不在循环里重复调用。
- 镜像不记录 last-used，`Created` 不是“最后使用”。报告只写实际取得的年龄/引用证据；
  镜像大小含共享层，不能逐个相加当可回收字节。回收量以 `df` 为准。
- 卷的规则比镜像更严：命名卷、compose 卷、任何被容器（含 stopped/created）引用的卷、
  retained 卷都不在自动范围；匿名卷也必须先证明“容器已删、没有程序能挂回去”。

**生产端边界（以 lightspace 为例）**：各项目只清自己的产物，不做全机清理。lightspace 的
`scripts/dev_machine_cleanup.py` 每天只处理它自己的 `/tmp` Go/lint/pnpm 缓存、源码副本、
Python venv 和工具副本，条件是结构可判定、归属明确、无进程占用、未被文档引用且最新 mtime
满 24 小时；归属不明只列出。它的 `scripts/cicd_watch.sh` 只用 `du -sL` 观测共享 go-build
是否超过 60 GiB，不执行删除。2026-11-04 之前的全机 `go clean -cache` 和 Docker prune 已从
lightspace 侧收窄；全机共享资源统一由本仓的 `go_cache_trim.py`、`docker_leftovers.py` 维护。

### 3.4 包管理器 store

- `/data/.pnpm-store` 是共享 store 根，里面按格式分版本目录：
  **v3 = pnpm 9，v10 = pnpm 10，v11 = pnpm 12**。当前项目 `pnpm store path` 返回
  `/data/.pnpm-store/v11`。
- 每周日 05:30，`pnpm_store_prune.py --execute --quiet`：
  - 工作目录固定 `/data/code/mole-linux`，PATH 用已安装 pnpm 的 Node 24.22.1；
  - 先用 `pnpm store path` 取共同根，再逐个存在的版本目录执行对应 pnpm 的
    `--store-dir /data/.pnpm-store store prune`；
  - 不能把 `vN` 目录本身当 store 根；每个版本先核 `--version` 和显式根下的 `store path`
    精确匹配；工具不可得或格式未知就跳过并记原因；
  - 只有可能写 store 的 pnpm 子命令才阻塞（install/ci/add/update/remove/fetch/import/dlx/
    create/pipeline/rebuild/store 等及已核实别名）；`npm exec vite`、`npx`、`pnpm run/exec`
    不阻塞；`/proc` 未知仍跳过。
- pnpm 的 `store prune` 没有 dry-run；预演只能报当前 store 大小，不能推算出“可回收量”。
  执行后以前后 `du` 和 `df` 分别记账，不把整个 store 大小当收益。
- npm 不自动清。`/root/.npm` 根里的 `_npx`、`_logs` 可能被正在跑的 `npm exec vite` 持有；
  只允许在明确 owner 命令下处理 `_cacache`，不整目录删。
- Go module cache 在 `/data/root-dev-cache/go-pkg-mod`，由 `/root/go/pkg/mod` 软链访问；
  它不参与自动修剪。重下载成本和“删了还能再下”是两件事，只有 owner 批准的
  `go clean -modcache` 才动。

### 3.5 worktree 与独立 clone

**worktree**

- 主 checkout 永远不自动删；只处理附加 worktree，且必须同时满足：没有进程
  （cwd/exe/fd/maps）、没有容器挂载、没有忙碌或 24 小时内更新的 BB 线程引用。
- 闲置满 24 小时后：HEAD 已在实时远端分支上且干净，直接 `git worktree remove`；否则把
  HEAD 或 WIP 快照推到 `archive/worktree/*`，`ls-remote` 核验 SHA 后再删。推送目的地优先
  同主机个人命名空间或内部 origin；公共上游且无个人 fork 的保留；不 force push；mono2
  不推 GitHub。
- 删除前要检查 ignored 文件：如果 ignored 里有凭据类文件，而主 checkout 同一相对路径没有
  内容相同的普通文件，整棵 worktree 保留。`git status` 默认不显示 ignored 文件，不能用
  “status 干净”证明没有私有状态。
- 归档 ref 带路径哈希，避免同名目录（`weekly-report`、`mono2`）互相覆盖。工蜂
  committer-check 只接受账号本人邮箱时，把同一棵树压成一个快照提交（全局身份，父提交为
  已发布基点）再推；原提交留在本地 archive ref。找不到已发布基点就保留并上报。
- 删除后写 JSONL：URL、HEAD、归档 ref、SHA、完整恢复命令
  `git fetch <url> <ref> && git worktree add <path> <sha>`。恢复信息是删除的一部分，不是
  可选的日志。

**独立 clone**

- `/data/system-tmp`（即 `/tmp`）和 `/var/tmp` 顶层的独立 clone 由每天 06:45 的
  `tmp_clone_sweep.py` 处理；`.git` 必须是真实目录，linked worktree 不在此入口。
- 条件比 worktree 更严：无 `/proc` 引用、无容器挂载、无 BB 环境路径落在其中；除 `.git` 外
  每个条目最新 mtime 满 24 小时；status（含全部 untracked）为空、无 stash、worktree list
  仅自身、无嵌套 `.git`；`/data/code` 和两个 tmp 根的其他仓库没有 remote/alternates 引用；
  ignored 无凭据类文件。
- 本地分支、HEAD、tag 逐个用实时 `ls-remote` 证明；缺失对象先推
  `archive/tmp-clone/*` 并核验 exact SHA；tag 用同前缀 tag ref；在待删 clone 内的本地
  bare remote 不算恢复副本。不 force push，mono2 不推 GitHub，无归档目的地就保留。

### 3.6 BB 环境与 BB 自有数据

- `/root/.bb` 在系统盘，放 `bb.db`（2026-11-14 约 12.7 GB）、artifacts、插件和部署检查点；
  `/root/.bb/thread-storage` 软链到 `/data/bb-data/thread-storage`，放线程产物和共享缓存。
  BB 自有数据（主库、artifacts）不在自动清理范围；只有 `.shared-cache` 由本仓修剪。
- `bb environment delete <id>` 只清理环境记录和 attempt 标记，**不删除 Git 分支或 ref**。
  本仓只在 worktree 已删除或路径已不存在时调用；路径仍存在时不调用 delete。仍有未归档线程时服务端返回 HTTP 409
  `Environment still has live threads`，保留，下一轮自然补齐。
- `worktree_sweep.py` 每轮在成功删除 worktree 后，按 `path` 精确全等处理未 `destroyed` 的
  BB 环境；每轮每个环境只尝试一次，被拒绝记原因、不重试、不令脚本失败；`bb` 不可用时
  fail open，只记 JSONL，不阻塞已授权的 worktree 删除。预演不调用 delete。
- 升级备份：`/data/bb-data/thread-storage/<thread>/upgrade-*` 只有在新版本稳定、owner 同意、
  且 `who-uses` 无引用、最新 mtime 早于升级窗口时才删。2026-11-14 已删 0.47 的
  `upgrade-047`（约 11 GiB）；0.49 的 `upgrade-049` 等新版稳定后按一次性提醒处理。

### 3.7 自动化形态

- 五个脚本任务跑在 `/data/tools/devcloud-storage-upkeep/releases/<sha16>/` 的不可变快照里，
  不是仓库工作区。目录名是 `manifest.json` 规范化内容 sha256 的前 16 位；文件 `0444`，
  目录 `0555`；用 `python3 -I` 运行（任务里是 `/root/.pyenv/versions/3.12.12/bin/python3`），
  不生成 `.pyc`。改仓库不会影响正在跑的任务，
  必须显式发布会话：新快照 → 复算 manifest → `--help` 验证 → 改 BB 命令 → 回读。
- `--quiet` 在“没有实际动作且没有命令失败”时保持安静，BB 记为 `skipped`；`skipped` 是正常
  无事，不是失败。每日 DSH 检查只看 `trigger=schedule` 的运行，`manual` 测试不算周期成功。
- 2026-11-05 起存储自动化统一为一套；重叠的旧任务（每周系统盘复盘、每周 DB 巡检、
  独立清理检查、fork-maintenance 09:00 闲置工作区清理）已退役。systemd 定时器不并入这套
  体系，继续由生产端自己维护（见自动化文档）。
- 锁竞争是正常状态：Docker 与 worktree 共用 fork-maintenance 的 `run.lock`，拿不到就静默
  跳过；跳过不等于清理有效，但也不创建诊断会话、不重试、不把锁竞争当故障。

## 4. 设计原则

- **先取证，再分类，再预览，最后只执行一个 owner 批准的动作。** 这是 Mole 的工作流，
  也是本机所有清理器的共同形状。
- **按恢复契约分类，不按目录名分类。** 可重新生成、可重新下载、已完成远端归档，才是
  可恢复；`cache`/`tmp`/`old` 只是名字。
- **owner 命令优先于递归删除。** `pnpm store prune`、`go clean -modcache`、`docker image rm`、
  `git worktree remove` 都是 owner 命令；路径级 `rm -rf` 不是。
- **未知即保留。** 进程引用、挂载、权限、扫描超时、BB 查询失败，任何一项查不清都保留。
- **系统目录和内核接口不是清理目标。** 不把 `/proc`、`/sys`、`/dev` 当作可清理目录
  （`/proc` 只用于进程引用探针），也不按路径删活动容器的挂载层；Docker 只走 owner 命令。
- **高风险数据只报告。** 数据库 binlog、PostgreSQL/MySQL 数据、Qdrant/MinIO、活动容器和
  源码依赖默认高风险，只列给 owner，不直接清理。
- **总量与候选同时报，未测不是 0。** 只报候选 0 会制造“没有空间可清”的假象。
- **共享资源集中维护，项目只清自己的产物。** 全机 Go/lint 缓存、Docker、pnpm store 由本仓
  统一维护；各项目不要自行做全机 `go clean -cache` 或 `docker prune`。
- **先修生产端，再清存量。** 每小时写十几 GiB 的流程不修，清理永远追不上。
- **不可变发布。** 定时任务不直接跑共享工作区；新脚本先做快照、清单、权限和真实预览。
- **一份规则只写一处。** 设计写“为什么”，storage-rules 写“能不能清”，自动化文档写
  “怎么跑/怎么复现”，skill 写“操作流程”。
- **不在仓库里放密钥。** 代理、token、凭据只在本机配置文件；文档不引用其值。

## 5. 决策记录

| 日期 | 决定 | 原因 |
|---|---|---|
| 2026-08-25 | 一次性测量留在本机账本，长期规则只留可复用条款 | 避免把某天的容量、路径、命令当成永久规则 |
| 2026-09-01 | 确认 `/tmp` 是 `/data/system-tmp` 的 bind，启用每日 03:30 清理 | 清 `/tmp` 腾数据盘，不是系统盘 |
| 2026-09-12 | 系统盘低于 70% 的一次性迁移：13 个目录软链到 `/data`，写入路由做 20 项验收 | 当时 `/` 只有约 100 GiB，必须先保系统盘 |
| 2026-09-17 | 数据库卷按显式模式/retained 声明管理；不产出 HTML 报告 | 匿名/年龄不能证明唯一真源；证据用 JSON/Markdown |
| 2026-10-10 | 在线把根分区扩到 500 GiB；Cursor/BB 检查点迁到 `/data` | 控制台扩容后分区未扩；系统盘 100% 影响服务 |
| 2026-11-03 | owner 长期授权“所有不是近期在用、可恢复的东西都能清理”，按 72h/引用/恢复校验执行 | 空间告急，需要可复用的授权边界而不是逐项请示 |
| 2026-11-04 | 建立 `$devcloud-storage-upkeep`；Go 缓存按小时修剪；worktree 24h 归档；停用 tmpfiles 的 `/tmp` 年龄规则 | 增长源是缓存/卷/worktree，旧规则追不上；tmpfiles 会删坏 worktree |
| 2026-11-05 | 存储自动化统一为一套，退役 4 个重叠任务 | 同一件事只留一个执行器，避免互相抢锁和口径分裂 |
| 2026-11-14 | Go/lint 修剪年龄从 2h 调到 24h；worktree 删除同步处理 BB 环境；`/var/tmp` 纳入 03:30 | 保留日内/隔夜复用；环境记录不该长期指向已删路径；系统盘也要清 |
| 2026-11-14 | 11 个缓存/工具目录从 `/data` 迁回系统盘；保留会话、数据和大共享缓存 | 系统盘已扩容；`/data` 仍是工作负载盘，不能继续吞缓存 |
| 2026-11-14 | pnpm 按 v3/v10/v11 逐版本 prune；共享 lint 缓存纳入同一 24h 规则 | 当前 pnpm 不会清旧版本 store；lint 和 Go 共用同一套缓存边界 |
| 2026-11-14 | 本设计文档取代旧根目录设计文档 | 旧文按日期堆叠，过时小节多；设计、决策、教训需要单一入口 |

## 6. 经验教训

| 现象 | 原因 | 规则或防护 | 落点 |
|---|---|---|---|
| `/data` 99.8%，可见目录只有约 20 GB | Docker overlay2/挂载层重复计数，`du` 又超时 | 账面与可见差额单列；只用 Docker metadata/owner 命令；超时记未测 | storage-rules「对账」；`bin/devcloud.py` |
| 以为清 `/tmp` 能救系统盘 | `/tmp` 是 `/data/system-tmp` 的 bind | 先 `findmnt`/`stat` 证明设备；`/tmp` 记数据盘，`/var/tmp` 记系统盘 | storage-rules「临时目录与 worktree」 |
| 单线程 `du /data` 十几分钟超时 | 千万级 inode，目录树大 | 顶层并行、输出到 `/var/tmp`、单树超时；不拿半成品做删除计划 | `$devcloud-storage-upkeep`「找增长源」 |
| 报告候选 0 就以为没空间可清 | 只量候选、超时当 0 | 同时报总量与候选；未知/超时明确写“未测” | 本文件 2.4；每日检查第 2/3 项 |
| `du` 对 `/root/.cache` 只报 27 B | 入口是软链，`du -x` 不跟随 | `readlink -f` 或 `du -sL`；共享缓存物理在 `/data` | `go_cache_trim.py`；本文件 3.1 |
| `who-uses` 并发扫描出现“自己被引用” | 并行探针的 `argv` 互相匹配 | 串行执行；正向命中保留，负向结论仍要复核 | storage-rules「Python 站点包」；ledger 2026-09-16 |
| `python3 -I bin/devcloud.py who-uses` 导入失败 | `-I` 隔离模式找不到同目录的 `devcloud_tmp` | 统一用 `./mo devcloud who-uses`，不改导入方式 | `bin/devcloud.py`；ledger 2026-11-04 |
| `/tmp` 别名探针返回 unknown | bind alias 不能按原路径解析挂载 | 传物理路径 `/data/system-tmp/...`，或先 samefile 转换 | storage-rules「临时目录与 worktree」；`who-uses` |
| tmpfiles 删掉闲置 worktree 的已跟踪文件 | 按文件年龄删除，不理解 Git 树 | `/etc/tmpfiles.d/tmp.conf` 的 `/tmp` age 改为 `-`；整树清理并保护仓库 | `tmp.conf`；`clean-tmp`；storage-rules |
| tmpfiles 卡在孤立 FIFO 上数天 | 逐文件删除遇到特殊文件 | 特殊文件/挂载/未知一律保留；整树扫描有超时和取消 | `bin/devcloud_tmp.py` |
| Go 缓存每小时涨十几 GiB，`go clean` 后又涨 | 缓存键含源码目录；lightspace 每任务复制到新 `/var/tmp` 路径，永不命中 | 生产端在原 worktree 跑或复用二进制/加 `-trimpath`；本仓只按 24h 修剪 | storage-rules「已知来源指纹」；`go_cache_trim.py` |
| 2 小时修剪误伤日内复用 | Go 只在条目使用且 mtime >1h 时刷新 | 24 小时门槛；README/哈希文件/桶规则 | storage-rules「owner 2026-11-04」；`test_recent_go_cache_entries_are_kept` |
| 构建中 `go clean -cache` 返回 rc=1，42.5 GiB 被重置 | 清缓存与活跃构建竞争 | 不做整库 reset；只删冷条目，构建中不执行 | ledger 2026-11-04；`go_cache_trim.py` |
| 匿名 MySQL 卷每天新增上百个 | `docker rm -f` 不带 `-v`，显式 rm 不清匿名卷 | 生产端改 `-v` 或 `--tmpfs`；本仓按 1h/无引用/内容特征清 | storage-rules「匿名测试 DB 卷满 1 小时即清」；`docker_leftovers.py` |
| 镜像/卷大小相加远大于实际释放 | 共享层、硬链接、overlay 重复计数 | 只报非独占口径；回收量看 `df` | storage-rules「Docker」；`docker_leftovers.py` |
| BuildKit 14 天规则只回收 130 B | `until` 按 last-used，条目都在窗口内 | 这是正常结论；不为凑数放宽门槛或改用创建时间 | storage-rules「Docker」；`docker_leftovers.py` |
| 旧标签镜像被误当“很久没用” | 镜像不记录 last-used，`Created` 不是使用时间 | 30 天 + 无引用 + 同仓库有更新镜像；保留仓库最新/保护标签 | storage-rules「2026-11-14 owner 批准：带标签镜像」；`docker_leftovers.py` |
| 删 worktree 可能丢唯一 `.env` | `git status` 不显示 ignored 文件 | ignored 凭据与主 checkout 比对；不同则整棵保留；先归档再删 | `worktree_sweep.py`；`test_ignored_env_missing_from_main_keeps_the_worktree` |
| 同名 worktree 归档 ref 互相覆盖 | 只用目录名做 ref | ref 带路径哈希 | `worktree_sweep.py`；`test_same_named_worktrees_get_distinct_archive_branches` |
| `bb environment delete` 后以为分支没了 | 它只清环境记录，不删 Git ref | 每轮每个环境只试一次；409 保留；恢复命令写 JSONL | storage-rules「2026-11-14 owner 批准：worktree 删除及每轮补扫 BB 环境」；`test_matching_bb_environment_is_deleted_with_recovery_command_after_worktree_removal` |
| Codex 沙箱写不了 `~/.cache/go-build` | 可写根不含缓存目录（只有工作区、TMPDIR、Git 目录和 thread-storage） | 软链到 `.shared-cache`；长期由 BB 加 writable root | 本文件 3.1；`go_cache_trim.py`；BB `runtimeWorkspaceWriteRoots` |
| 当前 pnpm 清不到旧版本 store | v3/v10/v11 是不同格式 | 按版本用 pnpm 9/10/12；显式共同根；版本不可得跳过 | storage-rules「pnpm store 每周日 05:30 owner prune」；`pnpm_store_prune.py` |
| `npm exec vite` 被当成包安装阻塞 | 它不是 pnpm store 写入命令 | 只拦 pnpm 写 store 子命令；npm/npx/vite/pnpm run 不拦 | `pnpm_store_prune.py`；`test_pnpm_only_store_writers_block` |
| 清理任务显示 `succeeded` 但从未跑过 | 只看单个状态字段，runCount 仍为 0 | 只认 schedule 触发的运行和真实 summary；`skipped` 也记录 | 每日检查 prompt；storage-rules「临时目录与 worktree」 |
| 策略阶段取“最大的 N 个”反而清不到 | 大多数候选会被策略拒绝，前缀里没有允许项 | 全量分片并行；任一分片失败/超时整轮 fail-closed | `bin/devcloud_tmp.py`；`test_policy_shards_cover_every_candidate_exactly_once` |
| 清理后 `df` 没降甚至上升 | 并发写入/其他任务同时清理 | 分列对象实占、df 净变；不把 df 差全归本轮 | storage-rules「盘点的计量与只读要求」；ledger 多次 |
| 硬链接/共享 inode 让逐目录求和失真 | pnpm/node_modules/缓存共享块 | 按同一文件系统 `df` 记回收；扫描时按 inode 去重 | storage-rules「盘点的计量与只读要求」 |
| 只读 `git status` 刷新了 index，伪造“近期活动” | 默认会刷新 stat cache | `GIT_OPTIONAL_LOCKS=0` 或 `git --no-optional-locks` | storage-rules「盘点的计量与只读要求」；ledger 2026-11-03 |
| reflog 文件 mtime 被 GC 批量重写 | 文件 mtime 不等于最后事件时间 | 读最后一条 reflog 事件时间，不读文件 mtime | 本文件 3.5；ledger 2026-11-03 |
| 迁移缓存时 `who-uses` 发现 Chrome/Vite 持有引用 | 浏览器/前端服务映射了旧缓存文件 | 先串行 `who-uses`；有引用就停或重启；两次 rsync + 原子 mv；再复核 | 本文件 2.3 / 3.1；ledger 2026-11-14 |
| 系统盘扩容后仍按旧的 100 GiB 布局思考 | 控制台扩容与分区扩容是两步 | 每次先 `lsblk`/`df`；分区/文件系统都扩了才算 | 本文件 2.1；ledger 2026-10-10 |
| 把 `/data/swapfile` 当活跃 swap | 它 2026-06-23 已停用，根 swap 是 `/swapfile` | `swapon --show`/fstab 证明；停用文件待 owner 决策，不自动删 | 本文件 2.1；ledger 2026-09-12 |
| DB 卷看起来“没人用” | 容器已删但匿名卷仍含数据；`pg_snapshots` 不是备份 | 匿名卷按 1h/无引用/内容；命名/retained 不动；`pg_snapshots` 不作恢复证据 | storage-rules「数据库卷」；`docker_leftovers.py` |
| ITFS 被当成普通残留 | 它是 systemd 管理的长期服务，承载 Codex 凭据 | 全部保留，不再逐轮询问 | storage-rules「已知来源指纹」 |
| Docker 日志找不到可轮转文件 | 日志驱动是 journald | 用 journald 管理；不按路径截断 | `/etc/docker/daemon.json`；本文件 3.3 |
| 管道超时却报成功 | `timeout du` 接管道后，rc 是最后一个命令的 0 | 每段单独记 rc；不完整扫描不参与总量 | `$devcloud-storage-upkeep`「找增长源」；本文件 2.4 |
| 深度盘点把大目录报成 0 | 错误/超时路径返回 0 哨兵；小写 `kB` 没被解析 | 0 先重测并标 unknown；不拿 0 与 `df` 对账 | `bin/devcloud.py`；本文件 2.4 |
| `/tmp` 在一个会话里 0 项，另一个会话 12 万项 | `/tmp` 是 bind，且可能有会话私有 tmpfs；`findmnt -T` 只显示最上层 | 用 `/proc/self/mountinfo` 还原真实挂载，测 bind 源，`who-uses` 传物理路径 | storage-rules「临时目录与 worktree」；ledger 2026-09-14/15 |
| `/tmp/.X11-unix` 被第一次执行删掉 | 已绑定 socket 在 `/proc/fd` 只暴露 inode，路径探针证明不了“无人用” | 静态保护已知系统目录；不靠“无路径引用”证明 socket 可删 | `bin/devcloud_tmp.py`；ledger 2026-09-01 |
| dangling 镜像漏数 7 个，删除前复查形同虚设 | `repo==<none>` 不是 dangling；`sha256:<hex>` 与裸 hex 比较永远不命中 | dangling 用 tag 判定；复查统一 ID 形式；查询失败 fail closed | `docker_leftovers.py`；ledger 2026-09-17 |
| Docker 的 GB 与系统 GiB 对不上 | Docker 用十进制 GB，账本用 GiB；镜像/层还共享 | 分列 Docker 自报、对象实占、df 净变；不逐项相加 | storage-rules「Docker」；`docker_leftovers.py` |
| 镜像年龄差 8 小时被误判到期 | 把 UTC `Z` 当本地时间解析 | 带时区解析；门限按绝对时间算，不按日期字符串 | `docker_leftovers.py`；ledger 2026-09-20 |
| `git worktree list` 看不到 `/tmp` 的独立 clone | 它只枚举已登记 worktree，不枚举普通 clone | 独立 clone 由每天的 `tmp_clone_sweep.py` 单独扫；不把 worktree 清单当全部 | `tmp_clone_sweep.py`；storage-rules「tmp 独立 clone」 |
| squash merge 后 HEAD 与 PR head 不同 | squash 改写了 commit identity | 用 `refs/pull/<n>/head` + `merge-base --is-ancestor`；PR 候选另做年龄过滤 | 本文件 3.5；旧巡检测试 |
| SIGTERM 后仍删了 15 项 / 进程退出窗口误报 | 取消没有共享事件和子进程组终止；`/proc` 读取与进程退出竞争 | 停止排队、终止进程组、恢复在途项；退出窗口有界重查，证据仍不全就拒绝 | `bin/devcloud_tmp.py`；`test_timeout_discards_plan_instead_of_deleting_prefix` |
| 配置写的路径与实际写入路径不同 | 配置是声明，运行时的 fd/cwd 才是事实 | 用 `/proc` fd/cwd、`du` 设备号和 `df` 证明实际落盘；不按配置路径删除 | `who-uses`；ledger 2026-10-04 gitcrawl |
| 保留声明中的对象仍可能消失 | retained 是“不自动删”，不是监控；外部删除不会通知 | 每轮重新核对 retained ID；消失也要记录，不假定是自己的清理 | storage-rules「数据库卷」；ledger 2026-11-01 |
| 同一目录不同轮次的“使用率”不可比 | 分母是否含 ext4 保留块不同；扩容后总量也变了 | 固定写 `used/total` 或 `used/(used+avail)`；跨扩容不直接比百分比 | storage-rules「临时目录与 worktree」；`bin/devcloud_tmp.py` |

**没有写成规则的未核实项**：这些只留在本机账本，作为调查线索，不当作容量结论或删除授权。

- 协调线程提到的 `/tmp` 196 GiB 漏看数字在本机账本里找不到对应测量行；本文只把它当作
  “超时不能当 0”的案例，不引用为已核实容量。
- `local-data-modes.json` 里退役库的 `delete_after` 删方写作 `auto_nxymfdcsy5c`，但该 ID
  不在当前 automation list 中，无法从实时配置核实；到期对象仍按 owner 指派处理。
- ITFS 的 retained 匿名卷 `37ad2a2e…` 在 2026-11-01 已不存在，账本未记录删除方；
  “retained”是保护声明，不是存活监控，每轮仍要重新核对对象是否还在。
- `/data` 在 2026-10-11/12 的总量增长有 dmesg 的 vdb/EXT4 resize 证据，但执行方未记录；
  跨这次扩容的 `used%` 不可直接比较。
- 旧账本对 pnpm 11/12 与 v11 的对应关系有过不一致记录；现行映射以
  `pnpm_store_prune.py` 的 `STORE_MAJORS`（9→v3、10→v10、12→v11）为准。
- 账本日期混用 UTC 与 `+0800`；跨日比较必须带显式偏移，不能按“同一天”字符串直接比。

## 7. 文档地图

### 水位刹车与增长归因

容量问题需要小时内响应：每日检查只能发现已经发生的压力，因此水位脚本以 `statvfs` 做只读判定，沿用现有清理器、锁和记账，不复制删除逻辑。增长计量与清理分离，采用并行且有预算的 `du`，失败保留为未测，避免把超时误报成零增长；连续结果才用于归因和通知。

| 文档 | 职责 | 读它的时候 |
|---|---|---|
| `docs/DEVCLOUD_STORAGE_DESIGN.md` | 本文：布局、为什么、决策、教训 | 第一次接手、改布局、做取舍 |
| `docs/DEVCLOUD_AUTOMATION.md` | 现行任务、命令、部署、复现、验证 | 查任务、改调度、换机器 |
| `docs/DEVCLOUD_DAILY_CHECK_PROMPT.md` | 每日 DSH 检查 prompt 原文 | 改检查项、同步 BB 任务 |
| `.claude/skills/devcloud-patrol/references/storage-rules.md` | 逐目标规则与授权，唯一规则源 | 判断某个具体路径能不能清 |
| `.claude/skills/devcloud-patrol/SKILL.md` | 只读巡检 + 服务暴露流程 | 做全机巡检、查监听面 |
| `.claude/skills/devcloud-storage-upkeep/SKILL.md` | 长期维护操作流程 | 手工预览/执行清理 |
| `docs/DEVCLOUD_SPACE_RECOVERY.md` | 空间告急时的手工入口 | 盘快满、需要应急 |
| `docs/DEVCLOUD_SPACE_RECOVERY_HISTORY.md` | 历史恢复记录 | 追溯旧轮次，不作当前授权 |
| `docs/WORKTREE_CLEANUP_REVIEW.md`、`docs/WORKTREE_CLEANUP_REVIEW_PROMPT.md` | 已退役监督流程 | 只读历史，不照做 |
| `storage-audit.log` | 本机逐轮账本（git-ignored） | 查某轮证据；不随仓库传承 |
| `AGENTS.md` | Agent 合同与 Ledger 五问 | 提交前、晋升规则前 |


2026-11-14 返工教训：预演的零副作用必须由 BB 桩调用计数和状态原文不变证明；
冷却是无输出的跳过，级别升级才越过冷却。查询失败用 unknown，避免误起并发清理。
cron 与刹车共用持锁包装入口，锁 fd 传给后台 timeout/执行器，执行结果追加既有 JSONL。
千万级 inode 的 `/data` 总量不适合 90 秒计量：改为逐顶层根 60 秒、8 并发、全轮 180 秒，
超时记录 unknown。比较只用连续有效测量，按真实间隔折算日增；重叠根分别观察而不相加。
归属由 storage-rules 的路径指纹表提供，内容证据才能识别的混合根保留 unknown；指标保留 30 天。

独立审查补正：旧 `devcloud.py` 依赖同目录 `devcloud_tmp`，底层保持 `-B`，仅新包装器用 `-I`；
两盘同时越线由一个 worker 顺序处理两根，避免本轮自己的锁把数据盘清理饿死。
冷却状态在清理派发后、通知之前落盘，通知故障不应使下一 tick 重复派发。

真实计量发现另一个预算陷阱：逐项 fork du 扫数万条 tmp 普通文件会耗尽全轮预算，
即使目录本身很小，也会把排序靠后的 BB 等重点根饿死。固定根及 BB 项先排队，
普通文件/符号链接用 lstat 的已分配块数，只有目录 fork du；保留总预算与 unknown 语义。


2026-11-15：数万根的少量正增长不应逐日报警。unknown 来源折算日增至少 1 GiB 才报警，
已登记来源仍需严格超过 20 GiB；Top5 保留小增长以便观察。
不可变快照也必须冻结归属规则：发布时把 storage-rules 的路径表复制为同目录
`storage_growth_fingerprints.md` 并纳入 manifest，运行只读此副本，更新规则须重新发布。


2026-11-15计量分项修正：BB父根的日增混合共享缓存与会话，按父根报警会把正常夜间构建误判为数据泄漏。
一次物理遍历拆出两共享Go缓存、thr_*目录合计和非线程条目；父根仍供Top5观察，缓存由每日大小/年龄检查兜底。
60秒对/data大根不足，改顶层优先、单根780秒、全轮840秒、ionice空闲级，BB900秒上限内结束，保持07:30前可验收。
Docker不直接du其物理树，以owner的system df四类数值比较；大根未测须单列，避免众多tmp条目掩盖覆盖缺口。
