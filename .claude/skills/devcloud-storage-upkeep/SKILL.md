---
name: devcloud-storage-upkeep
description: 本机（Linux DevCloud）数据盘的长期存储维护：查清谁占了空间、谁在持续写入，按恢复契约清理 worktree、可重建缓存和测试残留，并把泄漏修回生产端。用户说磁盘满了、空间清不出来、要清理本机空间，或任务中撞上 /data 写满时使用。修改 Mole 产品自身的清理代码、服务暴露巡检（$devcloud-patrol）不使用。
---

# 本机存储长期维护

如果是正在用的业务和 Agent 一定不要影响他们使用。这个是底线，我们要清理的那些是没有正在用的。

这台机器的空间问题是**写入速度**问题：增长源每小时写十几 GiB 时，再谨慎的逐项清理也追不上。每次处理按这个顺序：先找出增长源并修生产端，再按恢复契约清理，最后记账。

逐目标规则只在本仓 `.claude/skills/devcloud-patrol/references/storage-rules.md` 维护（`CLAUDE.md` 指定的唯一规则源），其中「owner 2026-11-04 存储维护策略」一节是本技能的授权边界；与旧条款冲突时以该节为准。

## 三类对象

| 类别 | 例子 | 处置 |
|---|---|---|
| 可重建缓存 | go-build、npm `_cacache`、pnpm store、Docker 悬空镜像和 BuildKit、无主匿名测试 DB 卷 | go-build 每小时只删 2 小时以上没用过的条目；npm、pnpm 不自动清。Docker 残留仍由 `scripts/docker_leftovers.py` 每小时清 |
| Worktree | 附加 worktree（主 checkout 永远不动） | 没人用且闲置满 24 小时：已在远端就删，否则先推 `archive/worktree/*` 再删：`scripts/worktree_sweep.py` |
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

三个脚本都是默认预演、`--execute` 执行；先读预演结果，再执行同一条命令。

```bash
python3 -I <本技能根>/scripts/docker_leftovers.py            # 预演；确认后加 --execute
python3 -I <本技能根>/scripts/go_cache_trim.py               # 预演；确认后加 --execute
python3 -I <本技能根>/scripts/worktree_sweep.py --measure    # 预演，并统计每个项目全部 worktree 及其中候选的大小
python3 -I <本技能根>/scripts/worktree_sweep.py --execute
```

- `worktree_sweep.py` 执行时按 worktree 逐个记 JSONL 到 `~/.local/state/devcloud-storage-upkeep/`，含归档 ref 和 SHA；恢复命令是 `git fetch <url> <ref>`，再 `git worktree add <路径> <sha>`。
- 删 worktree 之后再跑一次 `docker_leftovers.py --execute`，只清没人用的 Docker 残留。`node_modules` 大多是指向 pnpm store 的硬链接，但 pnpm store 有进程在用，不自动 prune。逐个目录 `du` 的合计会明显大于实际能腾出的空间，回收量以执行前后的 `df` 为准。
- 保留下来的项都写有原因：`in use`、`active within 24h` 等下一轮自然会处理；`unpushed work and no personal or internal remote` 和 `archive would push risky files` 需要 owner 决定推到哪里，或手动处理那个文件。

完成条件：执行前后的 `df`、按项目统计的删除和归档数量、每一类保留原因都已记录；失败项给出原因和下一步。

## 4. 记账

向本仓 `storage-audit.log` 追加一节：时间、前后容量、增长源证据、本轮动作和数量、保留原因、转给生产端的修复。规则有变化时同步改 storage-rules，并补 `tests/test_devcloud_storage_upkeep.py` 的回归。

## 易错点

- `/tmp` 是 `/data/system-tmp` 的 bind mount：清 `/tmp` 腾的是数据盘，不是系统盘。
- 全局 gitignore（`/root/.gitignore_global`）忽略了 `.env`、`*.log`、`local_conf/`。这些文件不会出现在 `git status` 里，也不会被推送。其中凭据文件名如果在主 checkout 的同一相对路径上没有一份内容完全相同的普通文件，worktree 会整棵保留；`node_modules` 和带 `pyvenv.cfg` 的虚拟环境不在这条检查里。其余被忽略的文件仍随 worktree 一起删除。
- mono2 有一个指向公开 GitHub 的 `gh-public` 远端。脚本只把归档推到同主机下你的命名空间或内部 origin，不要绕过这条规则手工推送。
- git.example.invalid 的 committer-check 只接受账号本人的邮箱，而 mono2 等仓库的仓库级配置是个人 GitHub 身份（`hxy91819 <…@proton.me>`）。所以推到内部主机的 WIP 快照改用全局身份。已经用个人身份提交、但还没推送过的本地提交会被拒，脚本会把同一棵树压成一个快照提交（全局身份，父提交为已发布的基点）再推，原提交只留在本地的 archive ref 上；找不到已发布基点时保留并上报。
- 本机推 GitHub 没有 SSH agent（`Permission denied (publickey)`），要走 `gh` 的 https 凭据，脚本会自动改写 GitHub 的地址。GitHub 偶尔返回 500，失败项下一轮重跑即可。
- 归档分支名带路径哈希：BB 托管的 worktree 和 `/data/ops/*` 里常有同名目录（`weekly-report`、`mono2`），只用目录名会互相覆盖，推送被 non-fast-forward 拒绝。
- 停进程用 PID。`pkill -f <模式>` 会匹配到执行它的 shell 自己的命令行，把自己也杀掉。
- 两个清理器各只覆盖一隅。`clean-tmp` 已改为 3 天、不限数量，但仍然不处理含 `.git`、凭据、数据库等内容的树：/tmp 下约 39 个独立 clone（带 `.git`，非 worktree）和一直有新运行写入的容器目录（opencraft 本地 smoke）不会被它清，只能由创建它们的任务自己收尾。fork-maintenance 的 09:00 任务仍会保留含任何 ignored 文件的 worktree。
- systemd-tmpfiles 已不再清理 /tmp（`/etc/tmpfiles.d/tmp.conf` 不再按年龄删），破坏闲置 worktree 的原因见 storage-rules「临时目录与 worktree」一节。
- 定时任务的现状以 `bb automation list` 为准，不要凭记忆。定时运行用的是 `/data/tools/devcloud-storage-upkeep/releases/<sha16>/` 里的不可变副本，不是仓库里的脚本。重新部署时，把 `scripts/docker_leftovers.py`、`scripts/go_cache_trim.py` 和 `scripts/worktree_sweep.py` 复制到新目录，同目录写 `manifest.json`（每个文件的 sha256 和 mode）；`<sha16>` 是这份 manifest 内容 sha256 的前 16 位；文件模式 `0444`，目录模式 `0555`。然后把定时任务里的命令改到新目录。
