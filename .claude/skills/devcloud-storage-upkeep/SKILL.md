---
name: devcloud-storage-upkeep
description: 本机（Linux DevCloud）数据盘的长期存储维护：查清谁占了空间、谁在持续写入，按恢复契约清理 worktree、可重建缓存和测试残留，并把泄漏修回生产端。用户说磁盘满了、空间清不出来、要清理本机空间，或任务中撞上 /data 写满时使用。修改 Mole 产品自身的清理代码、服务暴露巡检（$devcloud-patrol）不使用。
---

# 本机存储长期维护

设计背景、布局、决策和踩坑原因见 [DEVCLOUD_STORAGE_DESIGN](../../../docs/DEVCLOUD_STORAGE_DESIGN.md)；
现行任务、部署和复现步骤见 [DEVCLOUD_AUTOMATION](../../../docs/DEVCLOUD_AUTOMATION.md)。
本文只写操作流程；逐目标能不能清以 storage-rules 为准。

如果是正在用的业务和 Agent 一定不要影响他们使用。这个是底线，我们要清理的那些是没有正在用的。

这台机器的空间问题是**写入速度**问题：增长源每小时写十几 GiB 时，再谨慎的逐项清理也追不上。每次处理按这个顺序：先找出增长源并修生产端，再按恢复契约清理，最后记账。

逐目标规则统一在本仓 `.claude/skills/devcloud-patrol/references/storage-rules.md` 维护，其中「owner 2026-11-04 存储维护策略」及 2026-11-17 全盘 Git 修订是本技能的授权边界。定时任务现状查实时 CLI；不复用历史候选名单。

## 维护对象

| 类别 | 例子 | 处置 |
|---|---|---|
| 可重建缓存 | go-build、golangci-lint、npm `_cacache`、pnpm 各版本 store、Docker 悬空/旧标签镜像和 BuildKit、无主匿名测试 DB 卷 | Go build 与 golangci-lint 每小时只删 24 小时以上没用过的哈希普通文件（2026-11-14 owner 批准由 2 小时改为 24 小时）；npm 不自动清；pnpm 每周日 05:30 仅在无 pnpm store 写入子命令且探针完整时用 `scripts/pnpm_store_prune.py` 对 v3/v10/v11 分别用实测匹配的 pnpm 9/10/12 调 owner 命令，显式指定同一根。Docker 残留由 `scripts/docker_leftovers.py` 每小时清：2026-11-14 owner 批准无容器引用、Created >30 天且仓库有更新镜像的旧标签；仓库最新及 owner 保护标签保留；BuildKit 在 72h prune 外限总量 10GB，完整判据见 storage-rules 的 owner 策略节 |
| Git 工作树 | 两盘所有 clone / linked worktree；含隐藏、嵌套与 thread-storage | 每天06:30先 `bb_environment_sweep.py` 回收遗弃环境，再 `worktree_sweep.py` 的Git登记linked路径，再 `git_tree_sweep.py` 的全盘clone路径；现行执行版本见部署记录；规则调整先全量预演核验，自然闲置到期按既有规则处理，完整授权见 storage-rules |
| 数据 | 会话（`/data/codex`、`/data/bb-data`）、凭据、在用数据库、命名卷、备份与迁移副本 | 不自动删；列出大小和归属，交 owner 决定 |

## 1. 找增长源

完成条件：说清楚**最近一小时**是谁、在哪个项目里写满了盘，证据精确到进程或文件，而不是只给一张目录大小表。

- 容量：`df -B1 /data`。普通可用为 0 时 root 仍能写进约 4% 的保留块，此时进程会陆续报 ENOSPC。
- 目录：本盘约 2900 万 inode，单线程 `du` 要十几分钟，而且会被超时中断。按顶层目录并行测，输出写到系统盘（`/var/tmp`），因为 `/tmp` 就在 `/data` 上：
  `find /data -mindepth 1 -maxdepth 1 -print0 | xargs -0 -P 8 -n 1 du -sx -B1 > /var/tmp/du-top.txt`
- 写入者：对 `/proc/*/io` 的 `write_bytes` 做两次采样取差，30 秒就能排出谁在写。写入量大不等于净增长，SQLite、MySQL 的覆写只是流量；再看谁的文件真的在变大。
- 缓存新增：`find "$(go env GOCACHE)" -type f -mmin -60 -printf '%b\n' | awk '{s+=$1*512} END {print s/2^30 " GiB"}'`。
- 悬空匿名卷：用 datadir 里的库名定归属（`ls <卷>/_data`），容器已经不在了，库名是唯一线索。
- 已知生产者指纹在 storage-rules「已知来源指纹」一节；新发现补到那里。

## 2. 修生产端

把泄漏点和修法交给所属项目（它的主会话或仓库），不要只清不修。这台机器上反复出现的三种泄漏：

- 用 `docker run --rm` 起容器，退出时却 `docker rm -f` 不带 `-v`：主动删除时 Docker 不清匿名卷，每次留下一个数据库卷。改成 `docker rm -f -v`，测试库更好的做法是 `--tmpfs /var/lib/mysql`。
- 每次把源码复制到新路径再 `go run`/`go test`：缓存键里含目录路径，永远不命中，每跑一次就整套重新缓存。改为在 worktree 里直接跑，或者 `go build -o` 一次后复用二进制。复制源码再编译的流程加 `-trimpath` 也可以让缓存命中。
- 任务结束不收尾：推送或合并后，删掉自己建的 worktree（`git worktree remove`）和 `/tmp` 产物。

## 3. 清理

脚本默认预演、`--execute`执行；Docker/Go/pnpm的`--quiet`在没有实际清理或命令失败时保持安静，Git两阶段始终保存完整报告。人工维护先读预演结果，再执行同一条命令；下述已获授权的定时任务按相同判据自动执行。

```bash
python3 -I <本技能根>/scripts/docker_leftovers.py            # 预演；确认后加 --execute
python3 -I <本技能根>/scripts/go_cache_trim.py               # 预演；确认后加 --execute
python3 -I <本技能根>/scripts/bb_environment_sweep.py --json # 全部线程归档/无线程且BB闲置24h；provider回收
python3 -I <本技能根>/scripts/worktree_sweep.py --json       # linked：Git登记、当前分支、24h；默认预演
python3 -I <本技能根>/scripts/git_tree_sweep.py --json       # 独立clone：全盘原规则、7天；默认预演
python3 -I <本技能根>/scripts/pnpm_store_prune.py             # 当前 store 大小；可回收量只有 prune 后才能知道
```

- BB环境先按storage-rules的遗弃规则处理：有未归档线程始终保留；全部归档/无线程且24h以上闲置的provider-owned环境用BB cleanup回收，路径型或BB拒绝时解除环境证据，再由原Git规则判定；异步回收只在show为destroyed后报完成，不直接改BB数据。
- Git分工以storage-rules最新方向为准：linked只查Git登记、locked、未销毁BB环境、进程cwd、当前分支的远端/原SHA归档及24小时活动，移除/prune交Git，不加force；不走名称/项目兜底/嵌套/alternates等clone规则。独立clone保留完整全盘发现、7天和所有原保护。先预演，再按已有授权执行；每日launcher先环境回收，再linked和clone，linked无需等待另一次全盘扫描。
- 删 worktree 之后再跑一次 `docker_leftovers.py --execute`，只清没人用的 Docker 残留。pnpm 在本项目目录用 `pnpm store path` 取得当前版本路径及共同根，再逐个存在版本调用 `--store-dir <共同根> store prune`；实测选择 pnpm 9→v3、10→v10、12→v11，当前版本匹配用现有 pnpm，否则通过 `npx --yes pnpm@<major>` 获取。先核 major 与显式根下的 store path 精确匹配，不可得或未知版本则跳过记原因；不能用当前 pnpm 去冒充旧格式。仅可能写 store 的 pnpm 子命令阻塞（含 ci/install-test、create/pipeline、pnpx/pnx 及别名；recursive/multi/m/pm 按内层命令判断，消费 pnpm/Node 前置选项与 `--`），npm/npx/yarn/Vite 及 pnpm run/exec/start/脚本不阻塞；`/proc` unknown 仍跳过。默认预演，执行在命令边界再查一次进程，JSONL 逐版本记录前后占用；真实 prune 失败记账并继续其余版本，最终非零。预演不能推算 pnpm 未引用对象大小，不把整个 store 大小报成可回收量。逐个目录 `du` 的合计会明显大于实际能腾出的空间，回收量以执行前后的 `df` 为准。
- 保留下来的项都写有原因：`in use`、Git 的 `active within 7d (168h)` 等后续自然会处理；`unpushed work and no personal or internal remote` 和 `archive would push risky files` 需要 owner 决定推到哪里，或手动处理那个文件。

完成条件：执行前后的 `df`、按项目统计的删除和归档数量、每一类保留原因都已记录；失败项给出原因和下一步。

## 4. 记账

向本仓 `storage-audit.log` 追加一节：时间、前后容量、增长源证据、本轮动作和数量、保留原因、转给生产端的修复。规则有变化时同步改 storage-rules、相关文档与每日检查，并补 `tests/test_devcloud_storage_upkeep.py` 的回归。

## 易错点

- `/tmp` 是 `/data/system-tmp` 的 bind mount：清 `/tmp` 腾的是数据盘，不是系统盘。
- 全局 gitignore 会隐藏凭据，clean status 不能证明没有私有状态。新全盘入口不豁免主 checkout 中相同内容，也不豁免依赖目录内 ignored 凭据。
- mono2 有一个指向公开 GitHub 的 `gh-public` 远端。脚本只把归档推到同主机下你的命名空间或内部 origin，不要绕过这条规则手工推送。
- git.example.invalid 的 committer-check 可能拒绝历史中的个人邮箱；当前全盘归档必须保留原 SHA，拒绝就保留，不能沿用旧 linked helper 的压缩快照 fallback。
- 本机推 GitHub 没有 SSH agent（`Permission denied (publickey)`），要走 `gh` 的 https 凭据，脚本会自动改写 GitHub 的地址。GitHub 偶尔返回 500，失败项下一轮重跑即可。
- 归档分支名带路径哈希：BB 托管的 worktree 和 `/data/ops/*` 里常有同名目录（`weekly-report`、`mono2`），只用目录名会互相覆盖，推送被 non-fast-forward 拒绝。
- 停进程用 PID。`pkill -f <模式>` 会匹配到执行它的 shell 自己的命令行，把自己也杀掉。
- 清理器按恢复契约分工。`clean-tmp` 普通项 3 天、结构精确匹配的 Go/pnpm/Node 缓存按最新普通文件 mtime 满 24h，不限数量，但仍然不处理含 `.git`、凭据、数据库等内容的树：所有 Git 工作树改由 `git_tree_sweep.py` 每天按统一规则处理；一直有新运行写入的容器目录（opencraft 本地 smoke）仍由创建任务自己收尾。
- lightspace 已把 `/root/.cache/go-build` 与 `/root/.cache/golangci-lint` 指向 `/data/bb-data/thread-storage/.shared-cache/` 的两个共享缓存。取 realpath 后分别核固定 README 首行（Go build system / golangci-lint），共用 24h 哈希普通文件修剪；`.shared-cache` 不得当线程遗留删除或误报，只有 go_cache_trim 可修剪。
- clean-tmp 每批 128 项完整 `/proc` 快照、8 项并发；隔离及最终 sink 只探新增/starttime 变化 PID（owner 明确授权），unknown 拒绝；每项仍校结构、年龄、物理身份、完整指纹，过 Mole dry-run/final guard。普通树 5 秒、缓存树 60 秒；两根预算 300/1400 秒，外层 timeout 1790 秒，实际 cron 见规则源。
- systemd-tmpfiles 已不再清理 /tmp（`/etc/tmpfiles.d/tmp.conf` 不再按年龄删），破坏闲置 worktree 的原因见 storage-rules「临时目录与 worktree」一节。
- 定时任务以 `bb automation list/show` 为准。Git快照包含两条判定路径、systemd launcher/worker与旧两个helper（共6脚本），文件 `0444`、目录 `0555`，manifest 字节哈希前 16 位为目录名；`python3 -I --help` 后复算哈希且无 pycache。仅更新相关任务，不改其他维护任务的快照。短文件名 `--script-file` 更新后 check=True，再读回核对命令模式符合本轮已满足的授权条件、06:30、Asia/Shanghai 与 timeout。旧 06:45 tmp clone 任务停用且替换为空操作（避免旧水位任务手动触发）；每日检查同步验证其停用。完整部署与预算见自动化手册。
