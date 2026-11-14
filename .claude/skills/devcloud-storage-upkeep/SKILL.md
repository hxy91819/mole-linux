---
name: devcloud-storage-upkeep
description: 本机（Linux DevCloud）数据盘的长期存储维护：查清谁占了空间、谁在持续写入，按恢复契约清理 worktree、可重建缓存和测试残留，并把泄漏修回生产端。用户说磁盘满了、空间清不出来、要清理本机空间，或任务中撞上 /data 写满时使用。修改 Mole 产品自身的清理代码、服务暴露巡检（$devcloud-patrol）不使用。
---

# 本机存储长期维护

如果是正在用的业务和 Agent 一定不要影响他们使用。这个是底线，我们要清理的那些是没有正在用的。

这台机器的空间问题是**写入速度**问题：增长源每小时写十几 GiB 时，再谨慎的逐项清理也追不上。每次处理按这个顺序：先找出增长源并修生产端，再按恢复契约清理，最后记账。

逐目标规则统一在本仓 `.claude/skills/devcloud-patrol/references/storage-rules.md` 维护，其中「owner 2026-11-04 存储维护策略」及 2026-11-14 扩展是本技能的授权边界。定时任务现状查实时 CLI；不复用历史候选名单。

## 维护对象

| 类别 | 例子 | 处置 |
|---|---|---|
| 可重建缓存 | go-build、golangci-lint、npm `_cacache`、pnpm 各版本 store、Docker 悬空/旧标签镜像和 BuildKit、无主匿名测试 DB 卷 | Go build 与 golangci-lint 每小时只删 24 小时以上没用过的哈希普通文件（2026-11-14 owner 批准由 2 小时改为 24 小时）；npm 不自动清；pnpm 每周日 05:30 仅在无 pnpm store 写入子命令且探针完整时用 `scripts/pnpm_store_prune.py` 对 v3/v10/v11 分别用实测匹配的 pnpm 9/10/12 调 owner 命令，显式指定同一根。Docker 残留由 `scripts/docker_leftovers.py` 每小时清：2026-11-14 owner 批准无容器引用、Created >30 天且仓库有更新镜像的旧标签；仓库最新及 owner 保护标签保留；BuildKit 在 72h prune 外限总量 10GB，完整判据见 storage-rules 的 owner 策略节 |
| Worktree | 附加 worktree（主 checkout 永远不动） | 没人用且闲置满 24 小时：已在远端就删，否则先推 `archive/worktree/*` 再删，并同步清理 `path` 精确匹配的 BB 环境（2026-11-14 owner 批准）：`scripts/worktree_sweep.py` |
| tmp 独立 clone | `/data/system-tmp`（即 `/tmp`）、`/var/tmp` 的顶层独立 clone | 每天 06:45 用 `scripts/tmp_clone_sweep.py`；全条件满足才删除，未发布 ref 先归档，详见下文 |
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

五个脚本都是默认预演、`--execute` 执行；`--quiet` 在没有实际清理或命令失败时保持安静。人工维护先读预演结果，再执行同一条命令；下述已获授权的定时任务按相同判据自动执行。

```bash
python3 -I <本技能根>/scripts/docker_leftovers.py            # 预演；确认后加 --execute
python3 -I <本技能根>/scripts/go_cache_trim.py               # 预演；确认后加 --execute
python3 -I <本技能根>/scripts/worktree_sweep.py --measure    # 预演，并统计每个项目全部 worktree 及其中候选的大小
python3 -I <本技能根>/scripts/worktree_sweep.py --execute
python3 -I <本技能根>/scripts/tmp_clone_sweep.py --json       # 各仓库判据、数量、预计字节数
python3 -I <本技能根>/scripts/pnpm_store_prune.py             # 当前 store 大小；可回收量只有 prune 后才能知道
```

- `worktree_sweep.py` 执行时按 worktree 逐个记 JSONL 到 `~/.local/state/devcloud-storage-upkeep/`，含归档 ref、SHA 和完整恢复命令（`git fetch <url> <ref> && git worktree add <路径> <sha>`）。每个 worktree 删除成功后同步处理 `bb environment list --json` 中 `path` 精确全等且未 `destroyed` 的 BB 环境：调用 `bb environment delete <id>`（BB 插件不删 Git 分支/ref；仍有未归档线程拒绝时记录原因不重试），`bb` 不可用时 fail open 只记 JSONL 不阻塞删除（2026-11-14 owner 批准）。每次执行还补扫所有未销毁且路径已不存在的环境；本轮每个环境只删一次，被 BB 拒绝仍记账、exit 0，后续在线程归档后由下一轮自然补齐。预演不调用 delete。
- 删 worktree 之后再跑一次 `docker_leftovers.py --execute`，只清没人用的 Docker 残留。pnpm 在本项目目录用 `pnpm store path` 取得当前版本路径及共同根，再逐个存在版本调用 `--store-dir <共同根> store prune`；实测选择 pnpm 9→v3、10→v10、12→v11，当前版本匹配用现有 pnpm，否则通过 `npx --yes pnpm@<major>` 获取。先核 major 与显式根下的 store path 精确匹配，不可得或未知版本则跳过记原因；不能用当前 pnpm 去冒充旧格式。仅可能写 store 的 pnpm 子命令阻塞（含 ci/install-test、create/pipeline、pnpx/pnx 及别名；recursive/multi/m/pm 按内层命令判断，消费 pnpm/Node 前置选项与 `--`），npm/npx/yarn/Vite 及 pnpm run/exec/start/脚本不阻塞；`/proc` unknown 仍跳过。默认预演，执行在命令边界再查一次进程，JSONL 逐版本记录前后占用；真实 prune 失败记账并继续其余版本，最终非零。预演不能推算 pnpm 未引用对象大小，不把整个 store 大小报成可回收量。逐个目录 `du` 的合计会明显大于实际能腾出的空间，回收量以执行前后的 `df` 为准。
- 保留下来的项都写有原因：`in use`、`active within 24h` 等下一轮自然会处理；`unpushed work and no personal or internal remote` 和 `archive would push risky files` 需要 owner 决定推到哪里，或手动处理那个文件。

- tmp 独立 clone 必须同时满足：`.git` 是真实目录；没有 `/proc` 引用、容器挂载或路径落在仓库中的任何未销毁 BB 环境（未知即保留）；除 `.git` 外每个条目最新 mtime 已满 24h；status（含全部 untracked）为空、无 stash、worktree list 仅自身、无嵌套 `.git`；`/data/code` 及两个 tmp 根中其他仓库没有 remote URL 或 objects/info/alternates 引用；ignored 中无凭据类文件。逐个验证本地分支、HEAD 和 tag 在实时 `ls-remote` 远端上（位于待删 clone 内的本地 bare remote 不算恢复副本）；缺失对象复用 worktree 归档器推到同主机个人命名空间或内部 origin 的 `archive/tmp-clone/*`，核验 exact SHA 后删除。tag 对象用同前缀 tag ref 保存；不 force push，mono2 不推 GitHub，无归档目的地或拒绝推送就保留。内部 committer-check 只接受压缩快照时保留 clone，以免丢失原本地历史。
- tmp clone 先完成本批所有远端查询与归档，再完整刷新三根下所有仓库的 remote/alternates 引用；随后逐个重核占用与本地全条件，最终复核后不再请求远端。引用发现按容器顶层目录并行，仍遍历全部后代，不按依赖目录名排除。删除时物理路径必须是两根下真实顶层目录，fd-based 删除不跟随符号链接。每删一个立即写一行 JSONL（URL、HEAD、归档 ref、字节数）到同一 state 目录；保留项也记录原因。两个 tmp 根和 `/data/code` 的引用检查失败时本轮全部保留。

完成条件：执行前后的 `df`、按项目统计的删除和归档数量、每一类保留原因都已记录；失败项给出原因和下一步。

## 4. 记账

向本仓 `storage-audit.log` 追加一节：时间、前后容量、增长源证据、本轮动作和数量、保留原因、转给生产端的修复。规则有变化时同步改 storage-rules、相关文档与每日检查，并补 `tests/test_devcloud_storage_upkeep.py` 的回归。

## 易错点

- `/tmp` 是 `/data/system-tmp` 的 bind mount：清 `/tmp` 腾的是数据盘，不是系统盘。
- 全局 gitignore（`/root/.gitignore_global`）忽略了 `.env`、`*.log`、`local_conf/`。这些文件不会出现在 `git status` 里，也不会被推送。其中凭据文件名如果在主 checkout 的同一相对路径上没有一份内容完全相同的普通文件，worktree 会整棵保留；`node_modules` 和带 `pyvenv.cfg` 的虚拟环境不在这条检查里。其余被忽略的文件仍随 worktree 一起删除。
- mono2 有一个指向公开 GitHub 的 `gh-public` 远端。脚本只把归档推到同主机下你的命名空间或内部 origin，不要绕过这条规则手工推送。
- git.woa.com 的 committer-check 只接受账号本人的邮箱，而 mono2 等仓库的仓库级配置是个人 GitHub 身份（`hxy91819 <…@proton.me>`）。所以推到内部主机的 WIP 快照改用全局身份。已经用个人身份提交、但还没推送过的本地提交会被拒，脚本会把同一棵树压成一个快照提交（全局身份，父提交为已发布的基点）再推，原提交只留在本地的 archive ref 上；找不到已发布基点时保留并上报。
- 本机推 GitHub 没有 SSH agent（`Permission denied (publickey)`），要走 `gh` 的 https 凭据，脚本会自动改写 GitHub 的地址。GitHub 偶尔返回 500，失败项下一轮重跑即可。
- 归档分支名带路径哈希：BB 托管的 worktree 和 `/data/ops/*` 里常有同名目录（`weekly-report`、`mono2`），只用目录名会互相覆盖，推送被 non-fast-forward 拒绝。
- 停进程用 PID。`pkill -f <模式>` 会匹配到执行它的 shell 自己的命令行，把自己也杀掉。
- 清理器按恢复契约分工。`clean-tmp` 普通项 3 天、结构精确匹配的 Go/pnpm/Node 缓存按最新普通文件 mtime 满 24h，不限数量，但仍然不处理含 `.git`、凭据、数据库等内容的树：独立 clone（`.git` 为目录，非 worktree）改由 `tmp_clone_sweep.py` 每天清理；一直有新运行写入的容器目录（opencraft 本地 smoke）仍由创建任务自己收尾。
- lightspace 已把 `/root/.cache/go-build` 与 `/root/.cache/golangci-lint` 指向 `/data/bb-data/thread-storage/.shared-cache/` 的两个共享缓存。取 realpath 后分别核固定 README 首行（Go build system / golangci-lint），共用 24h 哈希普通文件修剪；`.shared-cache` 不得当线程遗留删除或误报，只有 go_cache_trim 可修剪。
- clean-tmp 每批 128 项完整 `/proc` 快照、8 项并发；隔离及最终 sink 只探新增/starttime 变化 PID（owner 明确授权），unknown 拒绝；每项仍校结构、年龄、物理身份、完整指纹，过 Mole dry-run/final guard。普通树 5 秒、缓存树 60 秒；两根预算 300/1400 秒，外层 timeout 1790 秒，实际 cron 见规则源。
- systemd-tmpfiles 已不再清理 /tmp（`/etc/tmpfiles.d/tmp.conf` 不再按年龄删），破坏闲置 worktree 的原因见 storage-rules「临时目录与 worktree」一节。
- 定时任务的现状以 `bb automation list` 为准，不要凭记忆。定时运行用的是 `/data/tools/devcloud-storage-upkeep/releases/<sha16>/` 里的不可变副本，不是仓库里的脚本。重新部署时，把 `scripts/docker_leftovers.py`、`scripts/go_cache_trim.py`、`scripts/worktree_sweep.py`、`scripts/tmp_clone_sweep.py` 和 `scripts/pnpm_store_prune.py` 五个文件复制到新目录（新脚本从同目录加载 worktree 共享函数，`python3 -I` 也可运行，直接编译已哈希的同目录源文件，避免生成或读取 `.pyc`），同目录写 `manifest.json`（每个文件的 sha256 和 mode）；`<sha16>` 是这份 manifest 内容 sha256 的前 16 位；文件模式 `0444`，目录模式 `0555`。对两个新入口运行 `--help` 后确认目录仍仅有五个脚本及 manifest、哈希和模式一致；然后把五个脚本定时任务里的命令改到新目录，读回核对命令、Asia/Shanghai 时区和调度；保留旧快照便于回滚。现有 Docker 每小时 07 分、Go 每小时 37 分、worktree 每天 06:30；tmp clone 每天 06:45，pnpm 每周日 05:30。pnpm 任务用已安装 pnpm 的 Node 24.22.1 PATH，工作目录为 `/data/code/mole-linux`，使 `pnpm store path` 与本项目预演一致；`/var/tmp` 或 `/data` 会选择不同 store。各版本从中立目录运行时必须显式传同一根 `/data/.pnpm-store`，不能传 `/data/.pnpm-store/vN`；每次更新用短文件名 `--script-file`，避免 BB inline 更新不断追加 UUID 导致 scriptFile 超过 200 字符。每日检查同步五个脚本任务，pnpm 周期时限 8 天，其余按实际频率。
