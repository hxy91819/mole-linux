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
| `/data/code` | 主 checkout、`*-worktrees` 附加 worktree | 这里是工作负载本身 | 全盘 Git 规则判定，受保护主树保留；见 3.5 |
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

### 3.5 全盘 Git 工作树（2026-11-17；2026-11-18拆分linked）

当前 `worktree_sweep.py` 走 Git 登记的 linked 快速路径（24h），`git_tree_sweep.py` 走独立 clone 全盘路径（7天）；详见文末2026-11-18决策。以下先说明全盘发现与clone恢复契约的建立过程。

全盘发现入口 `git_tree_sweep.py` 同时发现独立 clone 和 linked worktree。两块本地盘 `/`、`/data`
分别 `-xdev`，不按路径、深度、隐藏目录或是否受另一仓库忽略来筛选。发现与删除资格分开：
每个发现对象都进入报告，损坏 gitdir 也列出，但无法读取状态就保留。

旧实现只从 `/data/code` 三层主 checkout 的登记列表找 linked worktree，再从两个 tmp 根的顶层找
独立 clone。这漏掉 `/data/code/mono2/.worktrees` 中约 13 GiB 的独立 clone、其他仓库 linked
worktree 和 thread-storage。容器路径不是恢复契约：扩几个白名单只会把相同遗漏移到下个目录。
因此发现从磁盘开始；只裁剪 owner 管理的 Docker/包管理/语言缓存和有 owner 元数据证明的依赖树，具体名单及理由唯一维护在
[storage-rules](../.claude/skills/devcloud-patrol/references/storage-rules.md) 的 2026-11-17 条目。

`.pyenv`、`.nvm` 本身可能是 Git clone，但工具安装不能因 clean status + old mtime 被当成遗留源码。
新增真实使用证据：BB 项目 source、未销毁环境、软链、进程路径环境变量、shell 初始化文件、systemd、
cron、BB 自动化命令。保护的是含引用的工作树；引用 thread-storage 父目录并不笼统保护其每个子 clone。
主仓库有 linked worktree、父树含其他仓库时也保留，避免破坏共同 Git 元数据或嵌套树。

独立clone判定取两个旧脚本的严格条件（统一闲置7天；linked已按最新方向另用24h）：含全部 untracked 的 clean status、无 stash/操作/锁、
ignored 无凭据，每个 branch/HEAD/tag 都能恢复。旧 linked 的 WIP 快照、ignored 凭据主树同内容豁免，
以及 committer-check 压缩归档都不再允许。归档必须保留原始 SHA 和历史，失败保留。
全批远端 IO 完成后刷新发现与引用，随后重核本地状态再进入 sink。linked 只调主仓库的无 force
`git worktree remove`；clone 绑定 ancestor fd 后只删除自身，不跟随软链。

旧版先删目录再删除 BB 环境，曾为 dangling 环境补扫；当前政策改为非遗弃或正在回收的未销毁环境保护路径（遗弃标准见文末），
因此不再自动调用 `bb environment delete`。旧两个脚本仍提供 helper 和旧行为回归 API；worktree CLI转linked_main，tmp clone CLI转clone主函数；旧06:45任务停用且改为空操作，防止水位旧快照手动派发旧执行器。
全盘发现约150万目录，旧12分钟轮次预算不足；完整实测一轮863秒，距BB单次900秒硬上限仅37秒余量，无法覆盖负载波动。因此06:30任务只启动systemd transient service，结束后collect；当前先linked15分钟，再clone预演45分钟/执行150分钟，整体硬限63/168分钟。维持全盘发现范围，不用深度或路径白名单换速度。
最初仅部署预演，owner完成逐轮审核后已授权06:30执行；每次规则变化仍先验证预演和对应授权。

2026-11-17 引用未知范围修订：首轮 25 条解析未知把所有 2,358 个 Git 标记都保留，
其中许多来自暂停/过期任务的循环；这种全局传播会使执行长期没有候选。unknown 仍是保护证据，
但它的作用域必须与证据能指向的范围相同：绝对字面前缀优先，其次所属 BB 项目及同 remote 的所有检出，
只有归属也无法确定才传播到全盘。具体规则以 storage-rules 为准。
每条未知保留原表达式、任务/项目归属和实际保护路径，不以最终首个保留原因反推影响范围。
真实进程环境的多值全部代入；显式自动化 env、项目/环境 cwd 来自 BB 库存；不执行任意 shell。
Go profile 的固定 GOPATH/GOROOT 查询使用独立只读 Go 元数据替换；systemd Description 等文字不作命令解析。
远端 URL 身份索引复用完整引用扫描（含 fetch/push 重写），SSH/HTTPS 与 `.git` 形式统一后仅用于引用保护，
删除资格仍使用实时远端 SHA/历史证明。预演与删除前复核共用同一个作用域实现，防止只修预演而执行仍整轮拒绝。

全量复跑还发现 `/data/system-tmp/cursor-acp-r/.git` 缺失 HEAD，但 config 中仍有 remote。
这类树自身状态未知，始终保留；可读配置中的 URL/alternates 仍须参与引用索引。
正常 Git 上下文不可用时，只把 remote/URL 重写设置交给自建临时 bare Git 上下文解析，不修复原仓库，
也不把它从发现清单排除；条件 include 无法确定、配置不可读或 Git 仍无法解析时继续整轮保留。
第一次预演在该项处阻断后停止，仅有只读扫描；修正后必须用新快照重跑全盘，不能用局部补查冒充完成。

独立审查补足的作用域边界：纯 glob 即使尚无匹配也按其绝对前缀保护；for 的命令替换保留原单词中的前缀；
`${VAR:?}` / `${VAR:+...}` 先替换已知的实际环境值，不能退化为不相关的项目保护；残缺 gitfile 的 config 读取使用中性上下文兜底；
项目元数据的 URL 与仓库 URL 使用同一 Git fetch/push 重写规则。临时 bare 上下文清除继承的 GIT_*，显式绑定自身 gitdir，
只导入源上下文已物化的 URL 规则，不重新套用临时目录的条件 include；自动化显式 HOME 优先于默认 /root。网络 URL 统一 `.git` 后缀；本地 `/foo` 与 `/foo.git` 可能是不同存储，
只按物理路径及 `/foo/.git` 地址归一，避免用名字把两个仓库混为一谈。

增量复审后补齐：Environment 的动态值先替换已知值，未知值持续传播到裸变量路径字段；源配置读取也清理继承的 Git 仓库/注入变量，
保留显式 global/system 配置选择，避免 -C 被 GIT_DIR 重定向。残缺 Git 的 system/global fetch/push 重写有真实 Git 回归；
本地 foo/foo.git 的区别使用两个真实物理仓库验证。部分目录名前缀也不能直接解析最后组件的软链：
`/tool$variant` 需要同时覆盖 `/tool-old` 等字面匹配与 `/tool` 已有物理目标下的树，`/tmp$variant` 的 bind alias 同理。

实测教训：引用解析必须保留引号内空格并传播命令替换的 unknown；Git URL 先由 Git 展开 insteadOf/pushInsteadOf，闲置判据覆盖 fetch/ref/reflog，不能只看 HEAD/index。
扫描中临时目录消失是正常并发变化，不应中断其余目录；权限/I/O/超时则必须报告不完整。
沙箱会制造只读/空 `.git` 挂载，沙箱 PID 视图也不是宿主机 `/proc`，所以首轮完整预演由 BB
script runner 在宿主机运行，不拿沙箱清单当全机清单。命令更新必须 check=True 成功再启动人工运行，
BB script 的 timeout 最大 900 秒，不能因为更新失败而继续运行旧的执行命令。

### 3.6 BB 环境与 BB 自有数据

- `/root/.bb` 在系统盘，放 `bb.db`（2026-11-14 约 12.7 GB）、artifacts、插件和部署检查点；
  `/root/.bb/thread-storage` 软链到 `/data/bb-data/thread-storage`，放线程产物和共享缓存。
  BB 自有数据（主库、artifacts）不在自动清理范围；只有 `.shared-cache` 由本仓修剪。
- 旧版会在删 worktree 后补删 BB 环境，2026-11-17 已停用此流程；当前任何未销毁环境都保护路径，
  查询失败保留，不调用环境删除。BB 本身的环境生命周期仍由 BB 管理，不由存储清理器代管。
- 升级备份：`/data/bb-data/thread-storage/<thread>/upgrade-*` 只有在新版本稳定、owner 同意、
  且 `who-uses` 无引用、最新 mtime 早于升级窗口时才删。2026-11-14 已删 0.47 的
  `upgrade-047`（约 11 GiB）；0.49 的 `upgrade-049` 等新版稳定后按一次性提醒处理。

### 3.7 自动化形态

- 维护脚本任务跑在 `/data/tools/devcloud-storage-upkeep/releases/<sha16>/` 的不可变快照里，
  不是仓库工作区。目录名是 `manifest.json` 规范化内容 sha256 的前 16 位；文件 `0444`，
  目录 `0555`；用 `python3 -I` 运行（任务里是 `/root/.pyenv/versions/3.12.12/bin/python3`），
  不生成 `.pyc`。改仓库不会影响正在跑的任务，
  必须显式发布会话：新快照 → 复算 manifest → `--help` 验证 → 改 BB 命令 → 回读。
- `--quiet` 在“没有实际动作且没有命令失败”时保持安静，BB 记为 `skipped`；`skipped` 是正常
  无事，不是失败。每日 DSH 检查只看 `trigger=schedule` 的运行，`manual` 测试不算周期成功。
- 2026-11-05 起存储自动化统一为一套；重叠的旧任务（每周系统盘复盘、每周 DB 巡检、
  独立清理检查、fork-maintenance 09:00 闲置工作区清理）已退役。systemd 定时器不并入这套
  体系，继续由生产端自己维护（见自动化文档）。
- 锁竞争是正常状态：Docker 使用 fork-maintenance 的 `run.lock`，全盘 Git 使用自身 `git-tree-sweep.lock`，拿不到就静默
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
| 2026-11-17 | 全盘 Git 发现、两个脚本最严条件合并；06:30 只预演；BB 环境改为保护 | 深层独立 clone / 跨仓库 linked 漏扫；工具 clone 必须按真实引用保护 |
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
| 删 worktree 可能丢唯一 `.env` | `git status` 不显示 ignored 文件 | ignored 凭据整棵保留，不因主 checkout 有同内容豁免 | `worktree_sweep.py`；`test_ignored_env_missing_from_main_keeps_the_worktree` |
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
| `git worktree list` 看不到 `/tmp` 的独立 clone | 它只枚举已登记 worktree，不枚举普通 clone | 全盘发现 clone / linked，包括 ignored 后代；不把登记清单当全部 | `tmp_clone_sweep.py`；storage-rules「tmp 独立 clone」 |
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


2026-11-15持续增长修正：混合根的日内项目进出会制造正常正增长。unknown改为最近三个独立样本
严格递增且累计至少3GiB；样本按ts取最近，latest与带时间副本去重，缺测不向前补凑。
code/cache用一次du完整物理遍历输出直接子项，维持总遍历量；这些父根及system-tmp仅供Top5观察。
新指标按合成0进入Top5并标new，方便发现新目录；合成基线与明确unknown均不能作为告警历史。

2026-11-17条目消失修正：临时目录在du遍历期间被正常收尾，会使du退出1但仍输出本根总量。
只读计量在总量合法且stderr全为ENOENT诊断时保留ok，并附消失数量和最多5条例子；
混有权限/I/O错误、无本根总量、信号退出或超时仍为unknown。分项结果仅扫描父根携带备注，
避免同次遍历重复计数；这不改变清理候选扫描必须完整成功的要求。

### 2026-11-17：从预演审核补齐引用与项目归属

名称引用也是使用证据：TPM在.tmux.conf中只保存tmux-plugins/tmux-resurrect等身份，不一定含安装绝对路径。名称索引读取小文本配置，保留所有可能匹配的clone；IDE配置中的namespace也可能同时匹配主检出和lighthouse-bak，owner明确选择全部保留，不加入手写路径豁免。历史文件和其临时副本不作为配置，避免把已经执行过的安装命令当持续引用。

进程HOME/cwd/XDG目录通常持有未打开的运行状态和插件，不能只检查当前fd或data-dir。它们默认保护子树，但通用的/、mountpoint和进程UID的passwd home只保护点本身，否则数百个cwd=/和HOME=/root会把整盘或整棵/root永久保护。~/.cache、~/.config及实例自己的.runtime/.../home仍保持完整子树。

另一Git树下的ignored克隆属于父项目的保留契约。DMCA等取证快照未来可能失去上游，不能仅凭当前远端可恢复就代项目删除。所有位置的闲置门槛统一为7天；lightops约68h仍是近期使用。

owner授权新候选为旧131项子集且指定在用/证据树排除后即可开启执行，首次执行再绑定新预演候选集合，出现新增项不进行归档或删除；定时任务本身继续按全盘规则动态发现，不积累路径白名单。执行保留原有逐项完整占用复核和归档后的第二次全盘发现，因此不能套用仅预演的30分钟预算。实测一次usage约115秒、约19项的两次逐项usage约73分钟，加初始计划、测量、第二次发现约105分钟，先给150分钟预算/153分钟硬限；以首次真实执行耗时校准。

只读BB自动化列表现按项目并行采集，任一项目失败仍拒绝消费部分库存；每个执行复核都重新采集，不缓存保护证据。宿主实测usage由115.46秒降到84.39秒，预算仍先保留150分钟，等待真实执行测量。

首次实测：预演1157.459秒，执行3633.691秒（20个clone、9个archive ref），准备开始时与归档后完整重扫，且每个sink重新usage/local_state。按已测较慢usage115.46秒估算40次检查加两次发现和计量，保留150分钟/硬153分钟预算覆盖负载差异；超预算仍保留，不以削减范围或复核频率换速度。原目录du合计6.209614GiB；删除窗口可用净增6.155987GiB，全程两盘仅净增2.325294GiB。后两者都含同期其他活动，不能将du或长窗口df差当成严格因果释放量。

### 2026-11-18：alternates方向与自动化解释器

对象借用是有方向的：删借用方只移除自身文件，不会删除外部对象库；需要保护的是被借用对象的一方。过去把“本仓有alternates”直接当保留条件混淆了恢复证明与反向引用。现取消该条件，保留所有真实alternates引用源扫描，并将借用配置mtime纳入活动证据；借用方的全部refs仍必须有实时远端证明或先归档核原SHA。

mono2的10个Python自动化曾产生17条shell式for未知表达式，导致同remote的整组clone被保留。按execution.interpreter区分Python3后，通过AST只读收集Path/文件调用及可证明的前缀；Python数据循环不再成为路径未知。未知文件操作数、语法错误、无法界定的局部绑定仍保留项目范围；不执行脚本、Shell或其导入。原子写入临时文件、BB automation-storage真实cwd和datetime.replace各自按其语义处理。

本次发现mono2/.worktrees另有独立保护：所有19棵已发现树均嵌套于Git工作树/data/code/mono2，按11-17 owner规则属于父项目；修正alternates/未知引用不会放行。保留此规则，并逐树报告所有自动化阻断，供owner决定后续是否调整生产端或项目保留策略。

2026-11-18计时校准：当日完整执行含2次删除及最终复扫，耗1905.630秒，不能直接当作纯预演耗时；本轮纯预演实测1039.615秒。新预演预算45分钟/硬48分钟，为全盘发现和计量的负载波动留余量；执行预算保留150/153分钟，不缩小发现范围。

本轮审查补齐：容器下标/原地变更及别名使有限路径失效，循环内变更不得复用首轮值；文件调用的关键字/不透明kwargs、相对replace目标和chdir参数均进入引用判定。无法解析的值保留unknown，不因路径为相对字符串或参数不在位置列表而漏保。

### 2026-11-18：嵌套保护限定第三方快照

把所有嵌套树都归父项目保留，会让mono2自己的.worktrees永远不能清理。owner明确收窄：是父仓的linked工作树，或与父仓任一规范化有效remote同源的副本，不适用嵌套保护，仍完整检查7天、干净、BB环境/进程/引用与原SHA恢复证明；异源/无remote/未知关系继续保护DMCA和evidence。多层包含逐层核验，不能借内层同源越过异源外层。

同源结论在每次local_state重读common gitdir与有效fetch/push URL，复用Git rewrite和本地物理地址归一逻辑；父仓改remote不一定触动子树mtime，所以不能在删除时复用计划时的remote身份。linked的登记、锁和主仓可用性仍由原metadata检查；同源不是删除证明。回归覆盖同源linked/clone候选、异源dmca和无remote保留、无remote的linked继续恢复检查、脏树/BB环境保留、父remote变更后sink拒绝、多remote及重写归一、多层异源外层保护。

旧131项子集检查用来验证当次规则差异；把自然达到7天的128KiB候选当作新规则扩张停下，是对该条件的过度解释。owner已接受dd88的4项候选，今后自然到期按既有规则处理；本轮只需证明规则变化带来的新增来自同源嵌套收窄，不建立路径白名单。

独立审查发现：remote集合相交不具有传递性，A外仓/B证据仓中的A+B子clone可与两个祖先分别相交，不能据此越过证据仓的异源边界。现逐对核验相邻Git祖先。桥接回归在1318旧快照明确失败，修复后381项Git回归及336项兼容测试全通过，最终快照0c547e019c0d46ea。

逐树报告必须展示叠加保护：本轮解除mono2的14棵同源嵌套保护后，才在首要原因桶看到系统unit中的wisp/mono2名称碰撞（全部14棵都命中）、BB环境和容器引用；auto_3yhlnd21-wo也同时保护这14棵。只看先命中规则会把“删除一个阻断就能释放”误当结论。报告现同时列名称引用、BB环境、未知自动化及首要原因，并明确先命中保护后尚未继续评估的Git状态/闲置条件。


2026-11-18三项引用范围修正：上轮同源嵌套已放行，但cwd=/data/code的bridge worker仍使所有子仓库受保护；cwd只证明包含它的那棵树在用，现仅锚定cwd点。HOME和XDG仍可能装有未打开的插件，维持子树及根/挂载点/登录home例外。

名称引用证明同源副本中至少一份在用，不证明每份都在用。现先收集非名称证据，再把名称绑定到已有保留副本；名称证据不能互相循环满足。不存在的目录不算副本，选择前后核物理身份。其他副本仍须通过所有清洁、7天和远端证明条件；没有其他保留副本时歧义全保。lighthouse-bak遵守同一规则，不设置路径豁免。

auto_3yhlnd21-wo从字面路径deploy-after-security.json读取environmentPath，当前值是/data/code/bb-codex-home-package/.worktrees/tooling。此前不读取状态而将helper *args视为未知，按其非Git项目源/data/code和登记环境remote扩展，误护mono2副本。现静态识别标准JSON读取，读取当前有界普通文件并核身份，跟踪字段、get和已知参数向量；缺失、定制decoder、无法证明的改写或逃逸保持unknown，不执行自动化代码。每轮占用复核重新读取状态；容器项目的unknown仅保护源点和登记环境，不能从这些环境remote传播到别的项目。

删除前复核补齐先前审查缺口：全批网络IO后的一次完整库存不足以服务多个sink。现每项删除都重新完成两盘发现、remote/alternates引用和占用解析；真实回归在首项删除后创建第二项的本地remote消费者，旧版误删第二项，新版保留。新增扫描耗时纳入150分钟执行预算，预算用尽保留余项，不能为追求单轮删除量省略复核。

本轮独立审查thr_dyswdnb64k指出两项JSON传播缺口：对象嵌入容器/星号参数逃逸后不能继续信任旧字段，os.open写flags与os.write也可能改变稍后读取的文件。已补容器别名、参数污染与低层写入识别；非UTF8文本打开不能套用UTF8字段值，同步保留unknown。12条针对性回归在修复前失败，修复后JSON相关42项通过，真实auto3y仍精确解析tooling且无unknown。


2026-11-18 linked快速路径：owner反馈通用清理太难太慢，明确要求把linked从全盘clone判定中拆出。旧流程三项收窄虽然预演成功，但每个sink前全盘复核让34项clone积压需要数小时；已在初始扫描停止旧人工执行，归档/删除均为0。新linked路径只读取BB项目/环境及最近完整发现库存来定位主仓，重新读Git登记；每项只复核locked、BB环境、cwd、所在分支和24小时活动，由Git remove最终判断clean/ignored/submodule，不使用force和raw delete fallback。其他共享分支/tag/stash保存在主仓，无需随每个linked重复归档；主仓仍按原clone规则保护。

24h应衡量该worktree自己的工作文件、gitdir和分支ref/reflog，不能用共享common gitdir的fetch活动把全部副本刷新为近期。名称/项目fallback/嵌套/alternates等证据保留给独立clone，不再推导linked所有权。`git worktree prune`仅让Git清理已失效登记，不删除无法判定的残缺工作目录。BB/进程读取未知仍保留对应快速路径。

部署仍只有一个06:30入口，但worker先linked（15m），后clone（45m预演/150m执行），两条路径分别写完整JSON/JSONL；一条失败仍运行另一条并传播非零。整体硬限63m/168m。人工可以单独预演/执行linked，不因验证Git登记清理而等待clone全盘扫描；clone完整发现继续刷新后续快速路径可用的主仓索引。旧linked helper API留作历史回归，生产CLI指向linked_main；tmp helper CLI指向clone主函数。

linked审查收尾：thr_d2prih37sv发现self local/file remote会把共享本地refs误当外部恢复证明，现排除落在待删树内、同common Git存储及无法证明独立的本地endpoint。主仓登记还会再从实际primary重读，避免备份复制的gitdir覆盖真实列表。当前分支在origin取得实时heads证明就不等待无关remote；无证明时只可计划允许的原SHA归档，执行核验失败仍保留。8项回归在修复前失败，修复后通过；实际执行报告保留最新远端证明。prune按显式Git命令的默认语义判断失效登记，不额外传expire覆盖项；锁定登记仍由Git保留。实测gc.worktreePruneExpire约束git gc，不阻止显式worktree prune。

linked年龄补充：计算工作树文件/目录和自身gitdir的实际文件（index、HEAD、FETCH_HEAD、reflog等）mtime，不以Git管理目录自身mtime作为活动。实测3棵mono2树仅管理目录被临时锁/探针刷新，实际文件均旧于24h；目录mtime不应使它们永久刷活。此修正只用于linked，clone保持原7天全部元数据规则。

linked首次实测：完整预演128.080秒、执行278.043秒，27候选中10删除/17保留，mono2删除6棵。Git对13棵回指不一致的登记拒绝remove，符合“Git拒绝就保留”，未修登记来绕过拒绝；其余4项因远端不存在/无凭据保留。9个原SHA归档ref独立核验通过，7棵虽归档但Git拒绝仍保留。15分钟预算有实测裕量；失败登记不需要恢复通用clone启发式。总删除窗口可用净增9.131GiB（mono2 5.744GiB），与原du9.064GiB（mono2 5.720GiB）分列，差异含并发活动。06:30已部署6d0208714b86c771，两条路径仍分开报告；完整逐linked证据在linked-fastpath-20261118/REPORT.md。本轮不再等待独立clone数小时批次，独立clone由原每日策略继续处理。

2026-11-18遗弃环境：未销毁的BB环境不能无条件永久保护worktree。owner明确以“没有未归档线程且BB闲置满24小时”判断遗弃；存在未归档线程即保留，无论线程idle多久。时间用环境及线程created/updated/archived/attention/read最新值，未知保留。对managed=true的provider-owned路径先交BB cleanup，BB负责hook、目录及登记；路径型和BB拒绝则不再作为环境证据，回到现有Git判定。共同分类同时用于linked和clone/项目unknown兜底，避免只修预演或漏掉第二条保护。
只读核验发现release-three-images的env_w7kw3b8puw是managed=false的路径型环境，2个线程均归档。cleanup拒绝这种环境；delete在provider=null时只改生命周期，不能把成功响应误报为目录已删。本流程保留其环境登记，等待Git确认是否能删工作树。providerId=project-checkout也可能managed=false，因此不能只看provider名字选回收命令。BB cleanup异步成功后仍需show确认destroyed；接受但未完成的回收继续保护，失败/拒绝走Git兜底，不用recover绕过BB。BB provider内部有force/remove与停止cwd进程，调用前校线程、环境、实时cwd和工作文件24h闲置，避免把“无未归档线程”误当“没有业务使用”。

不能拿交互线程中的BB status当定时任务上下文：systemd仅传PATH，thread=null。回归现包含无线程身份查询及主机歧义拒绝。正常保留/明确拒绝与基础设施错误也必须分开：逐环境查询或响应协议失败要保留对象、继续后续对象和阶段，并最终非零；否则每日检查会把坏查询报成清理成功。

本轮环境分类计量要分清“记录”和“目录”：执行识别52条遗弃记录，其中50有路径，另2条error从未物化、没有path/instance，在旧规则中也不代表目录。只把50条算作解除路径证据；provider实际回收0，不把路径型记录标成已销毁。Git最终删除2个linked和3个clone，原du合计3.310GiB；17项拒绝均保留。linked阶段/data净增2.141GiB、clone删除窗口净增1.208GiB，全程仅净增0.314GiB，体现同期写入；linked监测启动较晚的0窗口读数不能当0释放。3个clone保持每sink全盘复核，耗2678.488秒，性能成本仍由原clone规则承担；BB环境阶段只耗1.446秒，不能把两者混称“BB回收慢”。

2026-11-19 CLI注入丢失：BB script会注入BB_CLI，真实只读探针得到/root/.bb/bin/bb、0.49.0且支持include-archived；但launcher只把清理过的PATH传给systemd。worker的PATH命中/root/.local/bin/bb，shim再转到旧0.46.1，三个阶段都因不支持include-archived而fail-closed。交互线程带BB_CLI，先前人工验证绕开了这个生产环境差异。现保留注入路径，缺省由服务端MainPID的bin/node推导安装包CLI；实测能力后以--setenv传递，每个Python BB调用显式绝对路径，不依赖shim是否自行尊重BB_CLI。回归把旧bb置于PATH头部；修复前db685c34的红灯仅保留于审查证据，常规测试直接验证当前worker三阶段仍访问服务端假CLI，不依赖Git历史。CLI不可得/能力不符时不再用模糊PATH尝试，错误给出具体探测命令和下一步。

首次CLI审查补充：只绑定JS入口路径仍不够；host-daemon/dist/bb的#!/usr/bin/env node在干净PATH找不到Node。探针与阶段命令现共用command_for，显式以同包bin/node运行JS入口；BB注入的绝对shell wrapper本身已绑定Node，仍直接调用。新增最小PATH的JS/shebang三阶段与MainPID回退测试，缺包内Node拒绝、能力探针非零保留stderr，避免把解释器错误误报成选项缺失。部署文档引用七文件manifest新哈希，六文件9217仅列历史。

第二轮审查核验：仅让假Python调用command三次不足以覆盖共享helper的runner绑定；实际clone usage还会经过ws.bb_anchors。回归改跑三条真实main，外部响应与磁盘发现隔离，定向去掉ws.run=command必须红灯。手动shell中export包住命令替换会抹掉退出码，示例改为独立赋值检查成功后才export并启动扫描。

实测确认：绑定运行中CLI后，三阶段均完整成功；32个存量clone全量执行没有路径裁剪，耗136m38s，落在原150m日常预算内。人工临时预算只提供上限并在launcher完成后立即恢复正式任务。root/data删除窗口与全程净变化分列，证据见bb-cli-binding-20261119/REPORT.md；容量受并发写入影响，不能只用全程净增评判清理是否发生。
