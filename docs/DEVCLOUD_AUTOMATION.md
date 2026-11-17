# 本机存储维护：现行任务与复现

本文是 Linux DevCloud 存储维护的**运行手册**：每个任务在哪台机器、什么时间、什么工作目录、
跑什么命令、失败/静默怎么解释，以及换一台机器怎么复现。
设计原因和踩坑记录见 [DEVCLOUD_STORAGE_DESIGN](DEVCLOUD_STORAGE_DESIGN.md)；
逐目标能不能清的规则见
[storage-rules](../.claude/skills/devcloud-patrol/references/storage-rules.md)。
任务现状以 `bb automation list` / `bb automation show` 为准，本文是撰写时快照（Git 部分更新于 2026-11-17）。

## 1. 现行任务

水位与增长两个任务使用独立快照 `56806e44ddcbe176`，不改变其他清理任务的快照（Git 使用下文独立快照）：

| 任务 | ID | cron（Asia/Shanghai） | 命令 | 超时 |
|---|---|---|---|---|
| 水位刹车 | `auto_idedelow-3g` | `0,15,30,45 * * * *` | `/usr/bin/python3 -I <snap>/storage_watermark_brake.py` | 120 秒 |
| 增长计量 | `auto_u4kos3to-di` | `0 7 * * *` | `/usr/bin/python3 -I <snap>/storage_growth_measure.py` | 900 秒 |

均用 bash 启动脚本 exec 上述命令，工作目录 `/var/tmp`。水位只调用既有任务：/data 对应
Docker、Go、全盘 Git 清理、pnpm 与 clean-tmp `/data/system-tmp`，/ 对应 clean-tmp `/var/tmp`；旧 tmp clone ID 已停用且改为空操作，即使旧水位快照手动派发也不会执行旧删除。
首次越线、级别升高或两小时冷却结束才触发并打印一行简报；正常/冷却完全静默（skipped）。
运行查询 unknown 跳过并在简报注明。`--dry-run` 只打印计划，不查 BB、不写状态、不启动清理。
状态在系统盘 `/var/lib/devcloud-storage-upkeep/watermark.json`，清理派发后、通知之前保存，
通知失败不丢冷却（该次仍非零退出）。

增长优先安排 `/data` 顶层各目录（`code`、`system-tmp`、`bb-data` 最先），再测
`/var/tmp`、`/root/.cache`、`/root/.bb` 各项，`system-tmp` 的逐项条目最后。
所有外部计量命令用 `ionice -c3`；8 并发，单根最多 780 秒，全轮最多 840 秒，BB 任务超时 900 秒。
07:00 启动，最迟约 07:15 终止，早于 07:30 检查；不靠延后每日检查解决测量超时。

`/data/bb-data` 一次 du 物理遍历（深度3输出，递归尺寸仍完整）同时产生父根及 thread-storage 四项：

| 指标根 | 归属 | 增长告警 |
|---|---|---|
| `.shared-cache/go-build`（完整路径） | shared Go build cache | 不参与；每日检查第2项核大小与年龄 |
| `.shared-cache/golangci-lint`（完整路径） | shared golangci-lint cache | 不参与；每日检查第2项核大小与年龄 |
| `bb:threads` | BB thread directories，`thr_*` 真实目录合计 | >20 GiB/天 |
| `bb:non-thread` | BB根非线程条目；含共享缓存其余项与目录元数据 | >20 GiB/天 |

`/data/code`、`/root/.cache` 各用一次物理 du 遍历输出父根与直接子项；只增加输出深度，
不对每个子目录重新遍历，硬链接仍由同一次du去重。它们与 `/data/system-tmp`、两个BB父根
只进Top5、不告警，`alert_excluded` 标明聚合原因。未登记子目录保持unknown，不凭名字猜归属。
不再 du `/data/docker`；`docker system df --format '{{json .}}'` 一次取得镜像、容器、卷、
构建缓存四类 Size，转为数值 bytes，以 `docker:images`、`docker:containers`、
`docker:volumes`、`docker:build-cache` 比较。缺类、失败或超时标unknown，不推成0。

指标写 `/var/lib/devcloud-storage-upkeep/metrics`，`schema_version=3`，历史保留30天。
最近样本按内容ts选取，latest与其带时间文件去重；unknown告警必须本次加前两次三样本均有效、
大小严格递增，且本次减最早样本累计 ≥3 GiB。缺项或未测不跳过补凑，不足时
`growth_status=insufficient_samples`，`history_samples` 写当前连续有效样本数，
`insufficient_history_count` 汇总未满足历史要求的unknown项。已登记来源仍按单日 >20 GiB。

前次没有指标的新子项以0为合成基线进入Top5，`baseline=new`，不参与任何告警；
这表示新指标，不证明目录本身新建。全局首次没有时间间隔时daily_bytes为null，按原始增量排名。
前次明确unknown的项仍不参与比较，不能用合成0替代失败测量。
`data_top_unknown` 单列/data顶层未测项（Docker分类不全也映射到/data/docker）；父子根不相加。

du以 `LC_ALL=C` 运行。非零退出时，仅当stdout有本根合法总量、stderr每行均为
条目消失（`No such file or directory`）才保留 `status=ok`；附 `vanished` 诊断行数与
最多5条 `vanished_examples`。子项随临时目录消失是正常并发变化，不算每日检查不符合。
code/cache/BB的分项输出只在实际扫描父根附备注，避免重复计数。
混有权限、I/O等其他错误、无本根合法总量、信号退出或超时仍记unknown。

预演和只读复现：

```bash
/usr/bin/python3 -I <snap>/storage_watermark_brake.py --dry-run
/usr/bin/python3 -I <snap>/storage_growth_measure.py
python3 -m pytest tests/test_devcloud_storage_upkeep.py -q
```


维护脚本任务都属于 project `proj_8d3wrcajxn`，时区 `Asia/Shanghai`。
它们跑的是 `/data/tools/devcloud-storage-upkeep/releases/<sha16>/` 下的不可变副本，
不是仓库工作区。`<sha16>` 是 `manifest.json` 规范化内容 sha256 的前 16 位；
2026-11-14 当前快照是 `0a809fb13caabb6c`，发布新快照后替换。

| 任务 | ID | 时间 | 工作目录 | 命令 | 无动作时 |
|---|---|---|---|---|---|
| Docker 残留 | `auto_1ih1erjpbqo` | 每小时第 7 分 | `/var/tmp` | `${PY} -I <snap>/docker_leftovers.py --execute --quiet` | `skipped`，exit 0 |
| Go 缓存修剪 | `auto_vad6yvkpdeq` | 每小时第 37 分 | `/var/tmp` | `${PY} -I <snap>/go_cache_trim.py --execute --quiet` | `skipped`，exit 0 |
| 全盘 Git 工作树 | `auto_hukidoavox0` | 每天 06:30 | `/var/tmp` | `bash <git-snap>/git_tree_sweep_run.sh --execute` | 输出异步报告位置；实际结果以完整JSON/JSONL为准 |
| 旧 tmp 独立 clone | `auto_3jiqtmzreq8` | 已停用 | `/var/tmp` | 保留历史记录；新统一入口覆盖其范围 | enabled=false |
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

脚本任务超时：Docker/Go 600 秒，pnpm 900 秒。Git 的 BB launcher 超时120秒（仅启动/检查 worker）；宿主机 worker 内部预算1800秒、systemd RuntimeMaxSec=1980秒。实测12分钟内部预算不足；完整一轮863秒距BB900秒硬上限仅37秒裕量，不能缩小发现范围来迁就BB上限。最终实测与裕量见交付快照报告。`--quiet` 只表示“没有实际
清理且命令没有失败时保持安静”，BB 把它记为 `skipped`；`skipped` 不是失败。

每日检查是 agent 任务，不是脚本任务：

| 任务 | ID | 时间 | 执行 | 行为 |
|---|---|---|---|---|
| 每日 DSH 检查 | `auto_5gacevqssoe` | 每天 07:30 | `acp-dsh`，只读，复用环境 `env_enxy84wqn2` | 核对六个启用脚本任务的 schedule 运行及旧 tmp 任务停用、Go 缓存、03:30 的 /tmp 与 /var/tmp、tmpfiles、两盘可用、日增 Top5 与未知来源；全部符合只回一句，不符合才发协调线程 |

prompt 原文在 [DEVCLOUD_DAILY_CHECK_PROMPT](DEVCLOUD_DAILY_CHECK_PROMPT.md)。改检查项时
两边同步：改文件 → 用文件里的提取命令更新 `bb automation update ... --prompt` → `show` 回读。
协调线程换了也要同步改 prompt 里的 `bb thread tell` 目标。每日检查只看 `trigger=schedule`
的运行，`manual` 是人工测试，不算周期成功。

另有一次性提醒 `auto_5oawixqha0u`（2026-11-19）：BB 0.49 稳定且未回滚后，删除
`/data/bb-data/thread-storage/thr_2j6ff75sxy/upgrade-049`（cutover-backup 约 11.5 GiB）。
它不是周期任务，删除前仍要核实版本、回滚状态和 `who-uses`。

## 2. 不可变快照与发布

仓库改动不会自动进入任务。Git 使用独立快照 `<git-snap>`，仅含
`git_tree_sweep.py`、`git_tree_sweep_run.sh`、`worktree_sweep.py`、`tmp_clone_sweep.py` 四个文件和 `manifest.json`；
后两个提供 helper/历史回归 API，CLI 均转发统一入口。当前 Git 专用快照为 `9656f1207a402a82`（2026-11-17；四个文件与 manifest，文件0444/目录0555），其他任务不跟随更换快照。launcher 用固定 transient unit `devcloud-git-tree-sweep.service` 启动 worker（cwd=/）；active/activating/deactivating 时跳过，状态未知拒绝启动；`--collect` 在结束后回收单元，不常驻监控。其他 Docker/Go/pnpm/水位/计量任务继续使用各自现有快照。

1. 每个脚本记录 sha256 与 mode=0444；`manifest.json` 用稳定 JSON 编码与末尾换行，
   `<sha16>` = **manifest 实际字节** sha256 前16位。新目录 `/data/tools/devcloud-storage-upkeep/releases/<sha16>`，
   文件0444、目录0555，不覆盖旧快照。
2. `python3 -I <git-snap>/git_tree_sweep.py --help` 后复算 manifest、脚本哈希与模式，确认仅五个文件、无 pycache。
3. 先在宿主机运行无 `--execute` 的首轮预演，保存完整 JSON/JSONL、按路径候选表及保留原因。
   沙箱 `/proc` 和人工 `.git` 挂载不代表宿主机；用 BB script runner 在宿主机运行。
4. 以短文件名 `git-tree.sh`，`bb automation update auto_hukidoavox0 --project proj_8d3wrcajxn
   --script-file <file> --timeout 2m`。更新必须检查返回码；失败时绝不继续 `run` 旧命令。
   用 show 读回存储脚本内容，确认当前预演不含 `--execute`、cron=`30 6 * * *`、Asia/Shanghai、timeout=120000。
5. 旧 `auto_3jiqtmzreq8` 替换为空操作脚本并 pause，确认 enabled=false；因为 paused 仍可能被旧水位逻辑手动派发，不能只 pause；
   每日检查 prompt 同步此状态、首轮清单/完整性和预演模式。保留旧任务历史，不让两个入口并行。
6. 2026-11-17 owner 已给条件授权：新完整预演候选 `(path,type)` 是旧131项的子集，且 tmux两项、实例HOME下codebuddy、dmca/upstream、evidence全部保留，即开启06:30执行并手动执行一次。有新增候选保持预演并回报。
   名称引用碰撞继续保留；lighthouse-bak 的 api/lightglobal/lightpro 不设路径豁免。
7. 条件满足后更新保存脚本为 `bash <git-snap>/git_tree_sweep_run.sh --execute`，check成功并读回核对模式后才手动执行。
   首批使用 `bash <git-snap>/git_tree_sweep_run.sh --execute --reviewed-preview <新预演JSON>`；若本批有新增候选，零归档零删除，恢复定时预演并回报。
   `--reviewed-preview` 仅约束本批，不给以后的动态清理建立路径白名单。
8. 每个 sink 前继续重读完整占用，不使用历史库存。预演30分钟/硬33分钟；执行150分钟/硬153分钟，首次实测后校准。
   BB脚本仍只负责120秒内启动，worker独立运行；记录实际删除/归档、保留/失败、目录原实占以及两盘df净变化。

只读复现（命令不带路径筛选参数）：

```bash
python3 -I <git-snap>/git_tree_sweep.py --budget-minutes 30 --json
# 定时等价：bash <git-snap>/git_tree_sweep_run.sh；异步 worker 受 systemd 33 分钟硬时限约束
python3 -m pytest tests/test_git_tree_sweep.py tests/test_devcloud_storage_upkeep.py -q
bb automation show auto_hukidoavox0 --project proj_8d3wrcajxn --json
bb automation show auto_3jiqtmzreq8 --project proj_8d3wrcajxn --json
bb automation runs auto_hukidoavox0 --project proj_8d3wrcajxn --limit 3 --json
```

旧测试的临时夹具固定写 `/var/tmp`，测试复现需在宿主可写环境运行；沙箱只读挂载的创建失败不算脚本行为回归。

JSON 报告在 `~/.local/state/devcloud-storage-upkeep/git-tree-sweep-<UTC>.json`；
JSONL 同前缀，含阶段时点与每对象证据。schema_version=2 的 `unresolved` 每条含 source、expression、scope、
绝对 prefixes 或项目归属，以及实际 protected_paths；按前缀或项目限定的 unknown 不阻断其他路径。
`global_unresolved` 只列无前缀且无归属的表达式，非空才使整轮因引用未知保留。暂停任务同样参与。
只读复现先查看每条 `unresolved[].protected_paths`，再导出 `items` 中 action 为 remove/archive+remove 的路径、类型、bytes、
idle_hours、objects、remote_proofs、archive_objects；archive+remove 尚未完成归档，不能报成可直接删除。
检查 `complete`、`error`、`global_unresolved`、`discovery.errors`、
`summary.unmeasured`、`timings` 和 `items`。unknown/超时不当0；嵌套目录的大小不能相加当可回收量。
2026-11-17 最终人工预演（034c4a1830a97ec5）：发现1,751,851目录、2,298个Git标记；
691.288秒完成发现、764.967秒完成引用库存、852.654秒完成计划，总1189.708秒（约19分50秒），
据此维持30分钟预算/33分钟硬限。complete=true、global_unresolved为空；115条未知表达式按67条prefix/48条project保留，
131候选（122 remove、9 archive+remove）；逐路径实占合计13.618 GiB，不是实际释放量。最终明细和逐条保护范围见线程审核包：
`/root/.bb/thread-storage/thr_e7qrqpgykc/worktree-sweep/scoped-rework/REPORT.md`。人工预演不算首次schedule运行。

`bb automation` 的 succeeded 仅表示 worker 启动成功；实际成功以最新 JSON 报告和 JSONL 完整阶段为准。每日07:30读取报告：预演应在07:03前结束；执行可能仍在150分钟预算内，硬时限为启动后153分钟（06:30启动则09:03）。核对本轮JSONL start时间与mode，预算内仍运行记pending并明确尚无完成结论，不能拿前一轮报告冒充本轮成功，同时核对上一轮已结束的完整报告及失败项，防止每天pending掩盖上一轮失败；超限或停止后无完整报告算失败。`systemctl show devcloud-git-tree-sweep.service -p ActiveState -p Result` 可查在途状态；collect 后 unit not-found 属正常，仍必须有当日完整报告。

linked 删除恢复时从报告 urls/object/归档 ref `git fetch <url> <ref>` 后
`git worktree add <path> <sha>`；独立 clone 用记录 URL clone 后恢复每个 branch/tag。
恢复原始 SHA 的证据必须在删除前落账，不能仅记录成功的 installer/命令输出。

水位/计量快照包含 `storage_watermark_brake.py`、`storage_growth_measure.py`、`clean_tmp_runner.py`
及 `storage_growth_fingerprints.md` 四个文件与 manifest（每文件 sha256/mode），目录名为 **manifest 文件实际字节** sha256 前 16 位。
文件 `0444`、目录 `0555`；三个入口都以 `python3 -I` 跑 `--help`，复算哈希并确认无 pycache，
发布前从 storage-rules 的 `growth-fingerprints` 标记段生成同目录指纹副本，测试核对两份表一致；
计量入口仅按自身目录读取副本，工作区规则改变不会影响已发布任务。确认目录只有四个文件与 manifest。
再更新两个任务的短名称 bash `--script-file`，回读命令、时区、cron、enabled。已知坏目录
`8f4acd16bb88facd` 不保留；`916428875da4edee` 保留作回滚；其他旧快照按引用情况维护。

`/tmp` 清理是另一条独立部署：快照 `/data/tools/devcloud-maintenance/releases/cbb5caf329a61b57/`，
由 crontab 直接引用；上一版 `7ca528d7c5f3f004`、`df3e2b353d8e1c72`、`f1e330c325c73fd0`、
`1c2d71d3ab21cc79` 保留作回滚。升级它时要同时改 cron 版本路径和每日 DSH 检查覆盖的根。

## 3. crontab

现行条目（root crontab，逐字）：

```cron
# devcloud-clean-tmp: ordinary entries 3d; structural caches idle 24h (owner-approved 2026-11-14)
30 3 * * * PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /usr/bin/timeout --signal=TERM --kill-after=10s 1790 /usr/bin/python3 -I /data/tools/devcloud-storage-upkeep/releases/56806e44ddcbe176/clean_tmp_runner.py # devcloud-clean-tmp
```

- 先 `/var/tmp`（内部预算 300 秒，timeout 325 秒），再 `/data/system-tmp`（内部预算 1400 秒，
  timeout 1435 秒），外层 1790 秒，kill-after 10 秒。
- cron 与水位同用 `clean_tmp_runner.py`；全程持 `/var/lib/devcloud-storage-upkeep/clean-tmp.lock`，
  非阻塞拿锁，后台 timeout/执行器继承 fd；两盘同时越线合并为一个两根顺序 worker，
  执行器还保留自身每根 staging 锁。包装器用 `-I`，旧执行器维持 `-B` 以导入同目录 `devcloud_tmp`。
- summary/intent/deleted 及包装器 runner_start/runner_finish（含退出码与 stdout）写 `/data/metrics/devcloud-clean-tmp.log`，
  stderr 追加到同名 `.stderr.log`。
- 水位用相同入口加 `--launch --path /var/tmp` 或 `--launch --path /data/system-tmp`；只派发不等待。
  包装器仅接受这两根；人工查看候选使用底层执行器去掉 `--execute` 的预览命令。
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
6. **快照**：按第 2 节分别部署全盘 Git、其他 storage-upkeep 与 clean-tmp 快照，复算 manifest、
   设 `0444/0555`、跑 `--help` 和预演。
7. **crontab**：按第 3 节写 clean-tmp 行；`crontab -l` 回读。
8. **tmpfiles**：按第 4 节写 `/etc/tmpfiles.d/tmp.conf`；`systemd-tmpfiles --cat-config` 回读。
9. **BB 任务**：按第 1 节创建/更新现行启用脚本任务和每日 DSH 检查，并停用旧 tmp clone 任务；时区 `Asia/Shanghai`；
   回读 ID、cron、工作目录、命令、enabled。每日检查 prompt 从
   [DEVCLOUD_DAILY_CHECK_PROMPT](DEVCLOUD_DAILY_CHECK_PROMPT.md) 复制。
10. **验证**：先手动跑各入口的默认预演；Git 按本轮子集条件核验后才开启执行，其他已授权任务验证 exit 0 / skipped；
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

2026-11-17追加证据规则的全量预演（9656f1207a402a82）：扫描1,754,216目录、2,417个Git标记；738.916秒完成发现，790.177秒引用库存，822.100秒计划，总1157.459秒。20个clone候选（15 remove、5 archive+remove，9个ref待归档），实占6.210GiB，全部是旧131项子集，无新增候选。118条unknown为70 prefix/48 project、0 global，实际保护132棵树。tmux/codebuddy/dmca/evidence已保留，lighthouse-bak三项按名称碰撞保留。授权条件已核验，06:30保存脚本已切换--execute；首次人工执行使用--reviewed-preview绑定本次完整预演。报告见 `/root/.bb/thread-storage/thr_e7qrqpgykc/worktree-sweep/owner-evidence/REPORT.md`、`subset-comparison.json` 与 `protections.md`（同线程审核包目录）。

首批人工执行（20261117T075718Z，9656快照）：完整成功，20/20个clone已删除，9个archive ref/5个唯一SHA均核验，未删除与失败0；耗时3633.691秒（60分34秒），reviewed_comparison.added=[]。删除目录原实占6.209614GiB；删除窗口可用净增6.155987GiB，全程系统盘/数据盘可用净增0.330288/1.995007GiB（含同期其他活动）。证据见同审核包 `EXECUTION.md`、`archives.tsv`、`removed.tsv`、`final-validation.json`。06:30已启用--execute；首次新版schedule尚待次日，人工执行不算schedule。
