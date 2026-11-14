# 本机存储维护：现行任务与复现

本文是 Linux DevCloud 存储维护的**运行手册**：每个任务在哪台机器、什么时间、什么工作目录、
跑什么命令、失败/静默怎么解释，以及换一台机器怎么复现。
设计原因和踩坑记录见 [DEVCLOUD_STORAGE_DESIGN](DEVCLOUD_STORAGE_DESIGN.md)；
逐目标能不能清的规则见
[storage-rules](../.claude/skills/devcloud-patrol/references/storage-rules.md)。
任务现状以 `bb automation list` / `bb automation show` 为准，本文是撰写时快照（2026-11-14）。

## 1. 现行任务

五个脚本任务都属于 project `proj_8d3wrcajxn`，时区 `Asia/Shanghai`。
它们跑的是 `/data/tools/devcloud-storage-upkeep/releases/<sha16>/` 下的不可变副本，
不是仓库工作区。`<sha16>` 是 `manifest.json` 规范化内容 sha256 的前 16 位；
2026-11-14 当前快照是 `0a809fb13caabb6c`，发布新快照后替换。

| 任务 | ID | 时间 | 工作目录 | 命令 | 无动作时 |
|---|---|---|---|---|---|
| Docker 残留 | `auto_1ih1erjpbqo` | 每小时第 7 分 | `/var/tmp` | `${PY} -I <snap>/docker_leftovers.py --execute --quiet` | `skipped`，exit 0 |
| Go 缓存修剪 | `auto_vad6yvkpdeq` | 每小时第 37 分 | `/var/tmp` | `${PY} -I <snap>/go_cache_trim.py --execute --quiet` | `skipped`，exit 0 |
| worktree 清理 | `auto_hukidoavox0` | 每天 06:30 | `/var/tmp` | `${PY} -I <snap>/worktree_sweep.py --execute --quiet --budget-minutes 12` | `skipped`，exit 0 |
| /tmp 独立 clone | `auto_3jiqtmzreq8` | 每天 06:45 | `/var/tmp` | `${PY} -I <snap>/tmp_clone_sweep.py --execute --quiet --budget-minutes 12` | `skipped`，exit 0 |
| pnpm store prune | `auto_yfr5p14dvnc` | 周日 05:30 | `/data/code/mole-linux` | `${PY} -I <snap>/pnpm_store_prune.py --execute --quiet` | `skipped`，exit 0 |

脚本任务共同的解释器与 PATH：

```bash
PY=/root/.pyenv/versions/3.12.12/bin/python3
export PATH=/root/.nvm/versions/node/v24.15.0/bin:/root/.nvm/versions/node/v24.22.1/bin:/usr/local/go/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin
```

**pnpm 的 PATH 特例**：pnpm 任务把 Node 24.22.1 放在 PATH 最前：

```bash
export PATH=/root/.nvm/versions/node/v24.22.1/bin:/root/.nvm/versions/node/v24.15.0/bin:/usr/local/go/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin
```

工作目录必须是 `/data/code/mole-linux`，因为 `pnpm store path` 会随项目/目录变化；
在 `/var/tmp` 或 `/data` 下解析会得到不同的 store。拿到共同根 `/data/.pnpm-store` 后，
逐版本工具从中立目录运行并显式传 `--store-dir /data/.pnpm-store`，不能把 `vN` 目录当根。

所有脚本任务超时：Docker/Go 600 秒，worktree/tmp/pnpm 900 秒。`--quiet` 只表示“没有实际
清理且命令没有失败时保持安静”，BB 把它记为 `skipped`；`skipped` 不是失败。

每日检查是 agent 任务，不是脚本任务：

| 任务 | ID | 时间 | 执行 | 行为 |
|---|---|---|---|---|
| 每日 DSH 检查 | `auto_5gacevqssoe` | 每天 07:30 | `acp-dsh`，只读，复用环境 `env_enxy84wqn2` | 核对五个脚本任务的 schedule 运行、Go 缓存、03:30 的 /tmp 与 /var/tmp、tmpfiles、两盘可用；全部符合只回一句，不符合才发协调线程 |

prompt 原文在 [DEVCLOUD_DAILY_CHECK_PROMPT](DEVCLOUD_DAILY_CHECK_PROMPT.md)。改检查项时
两边同步：改文件 → 用文件里的提取命令更新 `bb automation update ... --prompt` → `show` 回读。
协调线程换了也要同步改 prompt 里的 `bb thread tell` 目标。每日检查只看 `trigger=schedule`
的运行，`manual` 是人工测试，不算周期成功。

另有一次性提醒 `auto_5oawixqha0u`（2026-11-19）：BB 0.49 稳定且未回滚后，删除
`/data/bb-data/thread-storage/thr_2j6ff75sxy/upgrade-049`（cutover-backup 约 11.5 GiB）。
它不是周期任务，删除前仍要核实版本、回滚状态和 `who-uses`。

## 2. 不可变快照与发布

仓库里的脚本不会自动进入定时任务。发布一次存储脚本变更：

1. 把 `scripts/docker_leftovers.py`、`go_cache_trim.py`、`worktree_sweep.py`、
   `tmp_clone_sweep.py`、`pnpm_store_prune.py` 复制到新目录
   `/data/tools/devcloud-storage-upkeep/releases/<sha16>/`。
2. 同目录写 `manifest.json`：每个文件的 sha256 和 mode；目录名 = 这份 manifest 规范化内容
   sha256 的前 16 位。目录 `0555`，文件 `0444`。
3. 对五个入口各跑一次 `--help`，确认目录仍只有五个脚本 + manifest，没有 `__pycache__`，
   哈希和模式一致。
4. 用真实 `--help` / 预演验证后，把五个 BB 任务命令里的 `<sha16>` 换成新目录；
   回读 `bb automation show` 核对命令、`Asia/Shanghai`、cron、enabled。
5. 保留旧快照用于回滚；不要覆盖旧目录。

`/tmp` 清理是另一条独立部署：快照 `/data/tools/devcloud-maintenance/releases/cbb5caf329a61b57/`，
由 crontab 直接引用；上一版 `7ca528d7c5f3f004`、`df3e2b353d8e1c72`、`f1e330c325c73fd0`、
`1c2d71d3ab21cc79` 保留作回滚。升级它时要同时改 cron 版本路径和每日 DSH 检查覆盖的根。

## 3. crontab

现行条目（root crontab，逐字）：

```cron
# devcloud-clean-tmp: ordinary entries 3d; structural caches idle 24h (owner-approved 2026-11-14)
30 3 * * * PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /usr/bin/timeout --signal=TERM --kill-after=10s 1790 /bin/sh -c 'code=0; /usr/bin/timeout --signal=TERM --kill-after=10s 325 /usr/bin/python3 -B /data/tools/devcloud-maintenance/releases/cbb5caf329a61b57/bin/devcloud.py clean-tmp --path /var/tmp --execute --age-days 3 --json || code=$?; /usr/bin/timeout --signal=TERM --kill-after=10s 1435 /usr/bin/python3 -B /data/tools/devcloud-maintenance/releases/cbb5caf329a61b57/bin/devcloud.py clean-tmp --path /data/system-tmp --execute --age-days 3 --json || code=$?; exit "$code"' > /dev/null 2>> /data/metrics/devcloud-clean-tmp.stderr.log # devcloud-clean-tmp
```

- 先 `/var/tmp`（内部预算 300 秒，timeout 325 秒），再 `/data/system-tmp`（内部预算 1400 秒，
  timeout 1435 秒），外层 1790 秒，kill-after 10 秒。
- stdout 丢弃；summary/intent/deleted 事件写 `/data/metrics/devcloud-clean-tmp.log`，
  stderr 追加到同名 `.stderr.log`。
- 同一命令去掉 `--execute` 就是预览；不接受其他路径。
- 日志轮转由 `logrotate.timer` 管：每日检查，超过 5 MiB 提前轮转，保留 14 份/最长 30 天，
  压缩旧文件，不截断活跃日志。

## 4. `/etc/tmpfiles.d/tmp.conf` 覆盖

```conf
# Keep the existing root-only /tmp policy. Age-based cleanup of /tmp is done by the Mole
# clean-tmp cron (# devcloud-clean-tmp): whole entries idle >= 3 days, skipping git trees and
# in-use files. The per-file 3d rule here was removed on 2026-11-04 because it deleted tracked
# files inside idle git worktrees under /tmp and hung on an orphan FIFO from 2026-10-30.
q /tmp 0700 root root -
```

`q` 表示按配额/权限管理，第 6 列年龄是 `-`，因此 systemd-tmpfiles 不再按年龄逐文件删除
`/tmp`。验证：

```bash
systemd-tmpfiles --cat-config | grep -E '^q /tmp '
systemctl show systemd-tmpfiles-clean.service -p Result --value
```

这条 `/etc/tmpfiles.d/tmp.conf` 与 vendor 的 `/usr/lib/tmpfiles.d/tmp.conf` 同名，会整份覆盖后者，
所以 `/var/tmp` 的默认 30 天规则也一并被挡住；不要恢复逐文件年龄清理。

## 5. 保留的 systemd 定时器

这四个不属于本仓存储维护，继续由生产端维护，不要删、不要改成仓库脚本：

| timer | 时间 | 作用 |
|---|---|---|
| `safe-system-cleanup.timer` | 周日 05:00，随机延迟 30 分钟 | 清 DNF 缓存、归档 journal |
| `clean-openclaw-residue.timer` | 启动后 5 分钟，之后每小时 | 清 OpenClaw 卸载残留 |
| `html-preview-gc.timer` | 每 5 分钟 | HTML 预览过期清理 |
| `codebuddy-logrotate.timer` | 每小时，随机延迟 5 分钟 | CodeBuddy 日志轮转 |

`logrotate.timer` 是系统标准日志轮转，另行保留；它负责 clean-tmp 的日志。

## 6. 共享缓存软链 setup

物理目录：

```text
/data/bb-data/thread-storage/.shared-cache/{go-build,golangci-lint}
/root/.bb/thread-storage -> /data/bb-data/thread-storage
/root/.cache/go-build -> /root/.bb/thread-storage/.shared-cache/go-build
/root/.cache/golangci-lint -> /root/.bb/thread-storage/.shared-cache/golangci-lint
```

新机器执行一次（比 lightspace 原块多保留一份旧缓存，确认后再删）：

```bash
set -e
shared="${BB_DATA_DIR:-$HOME/.bb}/thread-storage/.shared-cache"
mkdir -p "$shared/go-build" "$shared/golangci-lint" "$HOME/.cache"
for name in go-build golangci-lint; do
  if [ -d "$HOME/.cache/$name" ] && [ ! -L "$HOME/.cache/$name" ]; then
    cp -a "$HOME/.cache/$name/." "$shared/$name/"
    mv "$HOME/.cache/$name" "$HOME/.cache/$name.pre-shared-cache"
  fi
  ln -sfn "$shared/$name" "$HOME/.cache/$name"
done
```

验证：

```bash
readlink -f "$HOME/.cache/go-build" "$HOME/.cache/golangci-lint"
go env GOCACHE
# 在 Codex 沙箱线程里不设任何缓存变量，跑一次项目构建/lint；缓存目录应出现新 mtime
```

确认 `$shared` 内容完整、构建可写后，再删除 `.pre-shared-cache`。这个软链是全机共享的：
所有项目、Agent、终端的 Go/lint 都写这里；它不是线程遗留，只有 `go_cache_trim.py` 可以修剪。
为什么放在这里、长期修法是什么，见
[设计文档 3.1](DEVCLOUD_STORAGE_DESIGN.md)。

## 7. 换机器复现顺序

1. **盘与挂载**：确认 `/` 与 `/data` 的容量、文件系统；`/data` 写入 `/etc/fstab`；
   `/tmp` 做成 `/data/system-tmp` 的 bind。不要假设 `/` 一定是 500 GiB，先 `lsblk` / `df`。
2. **基础目录**：`/data/{bb-data,codex,docker,metrics,tools,artifacts,backups,output,ops}`；
   `/data/system-tmp`；`/data/metrics/devcloud-maintenance`；`/var/lib/devcloud-maintenance`。
3. **Docker**：`/etc/docker/daemon.json` 设 `"data-root": "/data/docker/lib"`，日志驱动
   `journald`；迁移已有 Docker 数据要在停写窗口做，不要热搬。
4. **工具链**：Python 3.12.12、Node 24.22.1（pnpm 12）、Go；路径以本机实际安装为准。
5. **共享缓存**：按第 6 节建目录和软链；旧缓存先复制、验证、再删。
6. **快照**：按第 2 节部署五个 storage-upkeep 脚本和 clean-tmp 快照，复算 manifest、
   设 `0444/0555`、跑 `--help` 和预演。
7. **crontab**：按第 3 节写 clean-tmp 行；`crontab -l` 回读。
8. **tmpfiles**：按第 4 节写 `/etc/tmpfiles.d/tmp.conf`；`systemd-tmpfiles --cat-config` 回读。
9. **BB 任务**：按第 1 节创建/更新五个脚本任务和每日 DSH 检查；时区 `Asia/Shanghai`；
   回读 ID、cron、工作目录、命令、enabled。每日检查 prompt 从
   [DEVCLOUD_DAILY_CHECK_PROMPT](DEVCLOUD_DAILY_CHECK_PROMPT.md) 复制。
10. **验证**：先手动跑五个脚本的默认预演，再跑一次 `--execute` 并确认 exit 0 / skipped；
    跑一次 DSH 检查（`manual` 不算周期）；确认两盘可用 ≥150 GiB、共享缓存可写、
    `/tmp` summary 两个根都有、tmpfiles 的 `/tmp` 年龄为 `-`。

密钥、token、代理凭据不在本仓库；新机器沿用本机已有配置，不把值写进文档或命令。

## 8. 验证清单

```bash
bb automation list --project proj_8d3wrcajxn --json
bb automation show auto_5gacevqssoe --project proj_8d3wrcajxn --json
bb automation runs auto_hukidoavox0 --project proj_8d3wrcajxn --limit 5 --json
crontab -l | grep -A1 devcloud-clean-tmp
systemd-tmpfiles --cat-config | grep -E '^q /tmp '
systemctl list-timers --all --no-pager
df -B1 / /data
readlink -f /root/.cache/go-build /root/.cache/golangci-lint
```

每日 DSH 的判定逻辑就是第 1 节表格和 prompt 文件；改任务、改阈值、改协调线程时，三处
（BB 任务、prompt 文件、本文）必须同步。

## 9. 历史

- 2026-11-05 owner 决定存储自动化统一为一套。已删除 `auto_aburimdwdjg`（每周系统盘复盘）、
  `auto_caovoqtwj9e`（每周 DB 巡检）、`auto_cvheqhfkaf4`（独立清理检查，原已暂停）、
  `auto_o3pwesztbpg`（fork-maintenance 09:00 闲置工作区清理）。删除前配置存档在本机
  `/root/.local/state/devcloud-storage-upkeep/retired-automations-20261105.json`（0600，不随仓库）。
- 2026-09 的一次性写入迁移、旧执行器、worktree 复盘和空间恢复记录分别见
  [DEVCLOUD_SPACE_RECOVERY](DEVCLOUD_SPACE_RECOVERY.md)、
  [DEVCLOUD_SPACE_RECOVERY_HISTORY](DEVCLOUD_SPACE_RECOVERY_HISTORY.md)、
  [WORKTREE_CLEANUP_REVIEW](WORKTREE_CLEANUP_REVIEW.md)。这些是历史，不是当前授权。
- 本文件替代此前按日期堆叠的落地方案；旧根目录设计文档已并入
  [DEVCLOUD_STORAGE_DESIGN](DEVCLOUD_STORAGE_DESIGN.md)。
