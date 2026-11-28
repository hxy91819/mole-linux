# 本机存储维护：现行任务与复现

本文是 Linux DevCloud 存储维护的**运行手册**：每个任务在哪台机器、什么时间、什么工作目录、
跑什么命令、失败/静默怎么解释，以及换一台机器怎么复现。
设计原因和踩坑记录见 [DEVCLOUD_STORAGE_DESIGN](DEVCLOUD_STORAGE_DESIGN.md)；
逐目标能不能清的规则见
[storage-rules](../.claude/skills/devcloud-patrol/references/storage-rules.md)。
任务现状以 `bb automation list` / `bb automation show` 为准，本文是撰写时快照（Git 部分更新于 2026-11-19）。

## 1. 现行任务

水位刹车使用快照 `98fb64b672657560`，增长计量使用快照 `a2a7554c334dc601`：

| 任务 | ID | cron（Asia/Shanghai） | 命令 | 超时 |
|---|---|---|---|---|
| 水位刹车 | `auto_idedelow-3g` | `0,15,30,45 * * * *` | `/usr/bin/python3 -I <snap>/storage_watermark_brake.py` | 120 秒 |
| OpenCraft owner prune | `auto_-vwoibpctju` | `20 6 * * *` | `/usr/bin/python3 -I <owner-snap>/opencraft_local_runtime.py --apply`；cwd=/data/code/opencraft | 900 秒 |
| 增长计量 | `auto_u4kos3to-di` | `0 7 * * *` | `/usr/bin/python3 -I <snap>/storage_growth_measure.py` | 900 秒 |

均用 bash 启动脚本 exec 上述命令；OpenCraft 工作目录为 `/data/code/opencraft`，其余为 `/var/tmp`。水位只调用既有任务：/data 对应
Docker、全盘 Git 清理、pnpm 与 clean-tmp `/data/system-tmp`，/ 对应 Go 缓存修剪与 clean-tmp `/var/tmp`；旧 tmp clone ID 已停用、保存为空操作，从现行水位派发移除。
首次越线、级别升高或两小时冷却结束才触发并打印一行简报；正常/冷却完全静默（skipped）。
运行查询 unknown 跳过并在简报注明。`--dry-run` 只打印计划，不查 BB、不写状态、不启动清理。
状态在系统盘 `/var/lib/devcloud-storage-upkeep/watermark.json`，清理派发后、通知之前保存，
通知失败不丢冷却（该次仍非零退出）。

增长优先安排 `/data` 顶层各目录（`code`、`system-tmp`、`bb-data` 最先），再测
`/var/tmp`、`/root/.cache`、`/root/.bb` 各项，`system-tmp` 的逐项条目最后。
所有外部计量命令用 `ionice -c3`；8 并发，单根最多 780 秒，全轮最多 840 秒，BB 任务超时 900 秒。
07:00 启动，最迟约 07:15 终止，早于 07:30 检查；不靠延后每日检查解决测量超时。

`/data/bb-data` 一次 du 物理遍历（深度 2 输出，递归尺寸仍完整）产生父根与 thread-storage 两项。Go/lint 缓存来自 `/root/.cache` 遍历的直接子项：

| 指标根 | 归属 | 增长告警 |
|---|---|---|
| `/root/.cache/go-build` | shared Go build cache | 不参与；每日检查第2项核大小与年龄 |
| `/root/.cache/golangci-lint` | shared golangci-lint cache | 不参与；每日检查第2项核大小与年龄 |
| `bb:threads` | BB thread directories，`thr_*` 真实目录合计 | >20 GiB/天 |
| `bb:non-thread` | thread-storage 的其余空间，含目录元数据 | >20 GiB/天 |

`/data/code`、`/root/.cache` 各用一次物理 du 遍历输出父根与直接子项；只增加输出深度，
不对每个子目录重新遍历，硬链接仍由同一次du去重。它们与 `/data/system-tmp`、两个BB父根
只进Top5、不告警，`alert_excluded` 标明聚合原因。未登记子目录保持unknown，不凭名字猜归属。
同一次 `/data/code` 扫描另外输出 `/data/code/mono2/.worktrees`，mono2 父根只观察。
报告的 `fingerprint_coverage` 列出未命中的精确指纹和通配指纹；精确项缺失需要排查，
临时目录通配规则当天无匹配不告警。
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
迁移目录 `/data/root-storage-20260912` 使用同一次物理遍历，按原迁移项细分，Cursor
`versions` 和 pnpm `store` 是独立指标。中间汇总不参与告警；两项分别保留 20 GiB/天和
三次连续增长累计 3 GiB 的判据，新历史尚不足时不误报。
code/cache/BB及迁移目录的分项输出只在实际扫描父根附备注，避免重复计数。
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
Docker 使用快照 `eb8e04dc868e51f1`，Go 缓存修剪使用快照 `6117b7833a6f3773`；pnpm 使用 `8c2d819edb14ca13`。

| 任务 | ID | 时间 | 工作目录 | 命令 | 无动作时 |
|---|---|---|---|---|---|
| Docker 残留 | `auto_1ih1erjpbqo` | 每小时第 7 分 | `/var/tmp` | `${PY} -I <snap>/docker_leftovers.py --execute --quiet` | `skipped`，exit 0 |
| Go 缓存修剪 | `auto_vad6yvkpdeq` | 每小时第 37 分 | `/var/tmp` | `${PY} -I <snap>/go_cache_trim.py --execute --quiet` | `skipped`，exit 0 |
| BB环境→linked→独立clone | `auto_hukidoavox0` | 每天 06:30 | `/var/tmp` | `bash <git-snap>/git_tree_sweep_run.sh --execute` | 输出异步报告位置；实际结果以完整JSON/JSONL为准 |
| 旧 tmp 独立 clone | `auto_3jiqtmzreq8` | 已停用 | `/var/tmp` | 保留历史记录；新统一入口覆盖其范围 | enabled=false |
| pnpm store prune | `auto_yfr5p14dvnc` | 周日 05:30 | `/data/code/mole-linux` | `${PY} -I <store-snap>/pnpm_store_prune.py --execute --quiet` | `skipped`，exit 0 |

Docker 小时任务执行时最多等维护总锁 60 秒，锁忙超时明确输出未执行和缓存上限未检查，
不再把锁饥饿伪装为零回收。prune 后读取 Docker Build Cache 总量，超过 10GB 或测量未知
输出告警并 exit 1；无回收且确认未超限才静默。提交/版本镜像保留同仓库最新整批、固定别名、
容器引用及 protect 标签，旧批 Created >24h 才删。具体匹配和兼容参数见 storage-rules。
Docker 发布只替换其任务的不可变快照；manifest 含 docker_leftovers.py、go_cache_trim.py、
worktree_sweep.py，各文件 sha256/mode，目录名取 manifest 字节哈希前16位，文件0444/目录0555。
先预演，再 execute，查看 JSON 的删除/保留原因、cap 兼容尝试及 limit verification；不要只看 BB 状态。

脚本任务共同的解释器与 PATH：

```bash
PY=/root/.pyenv/versions/3.12.12/bin/python3
export PATH=/root/.nvm/versions/node/v24.15.0/bin:/root/.nvm/versions/node/v24.22.1/bin:/usr/local/go/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin
```

**pnpm 的 PATH 特例**：pnpm 任务把 Node 24.22.1 放在 PATH 最前：

```bash
export PATH=/root/.nvm/versions/node/v24.22.1/bin:/root/.nvm/versions/node/v24.15.0/bin:/usr/local/go/bin:/root/.local/bin:/usr/local/bin:/usr/bin:/bin
```

pnpm 从 `/data/code/mole-linux` 启动，联合各已安装 pnpm 在项目、HOME 和中立目录的
`store path`、HOME/XDG 默认根、npmrc 的 `store-dir` 和具备 pnpm CAS 布局的自定义根。
Docker 自管存储不纳入。逐格式 owner 命令从中立目录运行，
显式传已发现的真实共同根，不能把 `vN` 目录当根。人工只处理 HOME 根时，用同一个
`--root ~/.local/share/pnpm/store` 先看预演，再加 `--execute`；正常周任务不传 `--root`。

`<store-snap>` 包含 `pnpm_store_prune.py`、`local_storage_inventory.py`、`worktree_sweep.py`；
JSONL 写到 `~/.local/state/devcloud-storage-upkeep/pnpm-store-prune-*`。盘可用空间差
与目录分项的已分配字节差分列，前者含后台写入。

Cursor CLI 版本更新使用 Personal 既有 `auto_bgngb9bbyqk`（04:22）原入口
`python3 /root/.local/share/agent-cli-updates/update.py --tool cursor`。更新器目录独立于此仓；
每轮校验成功后的保留和删除记录写入 `~/.local/state/agent-cli-updates/cursor.json`。

Cursor SDK 会话清理使用 `auto_uj15re2dgsw`，每天 06:10（Asia/Shanghai），超时 600 秒，
工作目录 `/var/tmp`。BB 保存的脚本用 `flock` 互斥，从只读快照 `7f830a7b78aaaa6f`
调用 `/usr/local/bin/node` 执行 `cursor_session_sweep.mjs --execute`。部署参数指定物理 projects 根、
ACP sessions 目录、BB 本地数据库及已验证的 `@cursor/sdk` 1.0.32 安装目录。
SDK 升级时先用隔离 SQLite 副本核对删除边界，再更新运行时依赖路径。

规则见 [storage-rules](../.claude/skills/devcloud-patrol/references/storage-rules.md)。
人工预演去掉 `--execute` 和 `--sdk`，把 `--report` 指到独立文件。首轮执行可再加
`--reviewed-preview <报告>`。定时任务的完整 JSON 写到
`/var/lib/devcloud-storage-upkeep/cursor-sessions/latest.json`，BB 运行输出保存摘要；
日检按 `run_id` 对应定时记录。候选占用、实际删除字节和文件系统可用空间变化分别记录。
首轮人工预演与执行使用同一目录下的 `initial-preview.json`、`initial-execution.json`。

脚本任务超时：Docker/Go 600 秒，pnpm 900 秒（宿主清单有独立 650 秒预算）。Git 的 BB launcher 超时120秒（仅启动/检查 worker）；宿主机 worker 先给BB环境回收5分钟，再给 linked 15分钟，再给独立 clone 预演/执行各30分钟，systemd 整体硬限均53分钟。预算按两条路径实测分别设置，不缩小发现范围。`--quiet` 只表示“没有实际
清理且命令没有失败时保持安静”，BB 把它记为 `skipped`；`skipped` 不是失败。

每日检查是 agent 任务，不是脚本任务：

| 任务 | ID | 时间 | 执行 | 行为 |
|---|---|---|---|---|
| 每日 DSH 检查 | `auto_5gacevqssoe` | 每天 07:30 | `acp-dsh`，只读，复用环境 `env_enxy84wqn2` | 核对七个启用脚本任务的 schedule 运行及旧 tmp 任务停用、Go 缓存、03:30 的 /tmp 与 /var/tmp、tmpfiles、两盘可用、日增 Top5 与未知来源、Git 与 Cursor SDK 清理效果、BB 归档策略、prompt 同步和 BB CLI 版本；全部符合只回一句，不符合才发协调线程 |

[DEVCLOUD_DAILY_CHECK_PROMPT](DEVCLOUD_DAILY_CHECK_PROMPT.md) 是唯一来源。改完运行
`python3 -I scripts/sync_daily_check_prompt.py --apply`，脚本更新 BB 并回读比对完整内容（含末尾换行）。
每日检查运行同一脚本的 `--check`；不同则输出 diff 并以非零退出。
暂存 prompt 时，pre-commit 比对 Git 索引内的版本；缺少 BB CLI 或内容不一致都阻止提交。
协调线程换了也要同步改 prompt 里的 `bb thread tell` 目标。每日检查只看 `trigger=schedule`
的运行，`manual` 是人工测试，不算周期成功。

另有一次性提醒 `auto_5oawixqha0u`（2026-11-19）：BB 0.49 稳定且未回滚后，删除
`/data/bb-data/thread-storage/thr_2j6ff75sxy/upgrade-049`（cutover-backup 约 11.5 GiB）。
它不是周期任务，删除前仍要核实版本、回滚状态和 `who-uses`。

BB owner 盘点任务 `auto_ziaxnfa6kx4` 每天 05:45（Asia/Shanghai），调用同一 Git 快照的
`/usr/bin/python3 -I <git-snap>/bb_storage_maintenance.py --execute`，750 秒任务上限。
它仅扫描唯一联网的 BB 服务端宿主，最多等 600 秒，拒读过期报告；若 retention 已启用
“checkout 删除后清理开发数据”，直接拒绝 rescan，避免盘点隐式触发删除。策略本身不修改。
手工去掉 `--execute` 预演；执行模式零候选不调用删除，有候选按下面的边界标记 `deferred`：

| BB 命令 | 预览证据 | 当前接口的执行边界 |
|---|---|---|
| remove-orphans | `usage` 只给孤儿数量和字节 | 无精确路径计划；会重新查归属，且可停止 cwd 在候选内的进程，暂不自动执行 |
| remove-dev-instances | `developerStorage.entries` 的 missing、非 current 条目 | 批量删除会停止 missing checkout 下的进程，且不能绑定预演；`--instance` 能越过 checkout 缺失限制，不自动用 |
| retry-worktree-cleanup | `leftoverWorktrees` 可列已测残留 | 无 dry-run；执行实时选退休环境，可能包含扫描未测的对象，不以列表当完整计划 |
| remove-external-worktrees | `externalWorktrees` | 现有 BB 判断无活进程 cwd、24 小时或实时远端恢复证明，扫描只覆盖 BB 项目源；不替换 linked |

脏 Git 副本归档适用 storage-rules 的 30 天例外。线上 launcher 的脚本设置
`MOLE_DEVCLOUD_SITE` 为本机私有站点配置文件的绝对路径；未配置私有 origin 的脏副本保留。
部署前分别用 `--dirty-only` 预演 linked 和 clone，报告中的 `dirty_archive`、`archive_url`、`idle_hours`、
`bytes` 给出候选证据。执行加 `--execute --dirty-only --reviewed-preview <报告>`，只处理这批脏副本。推送核验后从
报告 `archives` 取 `restore`，或在空闲恢复副本里运行 `restore_dirty`，恢复原 HEAD、暂存内容
与未跟踪文件；如需恢复 stash，按报告 `stash_restore` 顺序执行。

## 2. 不可变快照与发布

仓库改动不会自动进入任务。Git 使用独立快照 `<git-snap>`（本轮 `81cc8ec05a845e29`），包含
`git_tree_sweep_bb_cli.py`、`bb_environment_sweep.py`、`git_tree_sweep.py`、`git_tree_sweep_run.sh`、
`git_tree_sweep_worker.sh`、`worktree_sweep.py`、`tmp_clone_sweep.py`、
`daily_git_cleanup_check.py`、`bb_storage_maintenance.py` 九个脚本和 `manifest.json`。上一版七脚本性能快照为 `3c35f7ad513b5140`。
旧 `9217bf8ab4fc6204` 是六脚本历史快照，缺少 CLI binder；上一正式版为 `b70f81e79242476d`（clone预算150m）。线上切换以本轮验证与 `bb automation show` 回读为准。
launcher创建同一个有时限的systemd unit，worker先回收遗弃BB环境，再运行linked登记路径和独立clone全盘路径；cwd=/，BB只负责120秒内启动。其他维护任务继续使用各自已有快照。

1. 九个脚本各记sha256，manifest稳定JSON+换行，目录名取manifest哈希前16位，文件0444/目录0555。两个Python入口与launcher的help均验证成功且无pycache。
2. 手动每次调用先检查绑定成功：`BB_CLI="$(/usr/bin/python3 -I <snap>/git_tree_sweep_bb_cli.py)" && export BB_CLI && python3 -I <snap>/bb_environment_sweep.py --json` 核对遗弃环境，再运行linked： `BB_CLI="$(/usr/bin/python3 -I <snap>/git_tree_sweep_bb_cli.py)" && export BB_CLI && python3 -I <snap>/worktree_sweep.py --json`；完整后用其专用预演文件执行 `--execute --reviewed-preview <linked-preview.json>`。只读Git登记定位不重扫磁盘；主仓来自实时BB source/环境和最近一次完整两盘发现库存。无缓存的初次安装先用clone全盘预演建立库存。
3. clone入口 `BB_CLI="$(/usr/bin/python3 -I <snap>/git_tree_sweep_bb_cli.py)" && export BB_CLI && python3 -I <snap>/git_tree_sweep.py --json` 保持7天、全部原引用/凭据/恢复证明规则；只送独立clone进通用计划，linked仅作为引用来源/下次主仓索引。其人工执行只绑定自己的clone预演，不混用linked预演。
4. 部署保存脚本 `bash <snap>/git_tree_sweep_run.sh --execute`，读回enabled、cron=30 6 * * *、Asia/Shanghai、timeout=120000及命令快照。launcher只接受无参数或--execute；手动绑定预演使用上面各自Python入口。
5. 旧tmp任务auto_3jiqtmzreq8保持disabled+noop，不恢复或手动触发；每日检查prompt同步读回。其余维护任务不跟随更换快照。
6. linked 按最新 Git 登记与实时证据判断。干净副本闲置 24h 后用普通 Git remove；脏副本适用 30 天和归档核验规则。网络后立即复核 BB、进程、登记、HEAD、闲置时间和工作树内容；实际Git状态文件计活动，不让管理目录的临时锁mtime刷活。Git prune按显式命令清失效登记，锁定登记仍保留。单主仓无法读登记只保留并报告该主仓，不扩大成全局阻断；BB/cwd整体读取未知则该阶段不执行。
7. BB环境回收预算5m；linked预算15m；clone预算预演/执行各30m；同一worker整体硬限均53m。一阶段失败仍尝试另一阶段并最终非零。06:30执行最迟07:23，07:30应已结束，linked必须已有完整结果；同时检查上一轮已结束结果。
8. 环境阶段另写bb-environment-sweep-<UTC>.json(l)（schema1/policy=bb-environments）；两条Git路径分别写linked-worktree-sweep-<UTC>.json(l)（schema3/policy=linked）和git-tree-sweep-<UTC>.json(l)（schema2/policy=clones）。逐路径删除/保留/原SHA归档与Git拒绝原因、prune输出、目录原du及两盘可用容量变化分列。人工单独linked验证不算首次完整schedule。

只读复现（命令不带路径筛选参数）：

```bash
(
  BB_CLI="$(/usr/bin/python3 -I <git-snap>/git_tree_sweep_bb_cli.py)" || exit 1
  export BB_CLI
  python3 -I <git-snap>/git_tree_sweep.py --budget-minutes 30 --json
)
# 定时等价：bash <git-snap>/git_tree_sweep_run.sh；环境→linked→clone，预演/执行整体硬限53分钟
python3 -m pytest tests/test_git_tree_sweep.py tests/test_devcloud_storage_upkeep.py -q
bb automation show auto_hukidoavox0 --project proj_8d3wrcajxn --json
bb automation show auto_3jiqtmzreq8 --project proj_8d3wrcajxn --json
bb automation runs auto_hukidoavox0 --project proj_8d3wrcajxn --limit 3 --json
```

旧测试的临时夹具固定写 `/var/tmp`，测试复现需在宿主可写环境运行；沙箱只读挂载的创建失败不算脚本行为回归。

每日用 `python3 -I <git-snap>/daily_git_cleanup_check.py` 只读核对新快照。
三阶段最终报告和 JSONL finish 有保留原因排名：`by_bytes` 含已计量字节、数量、未测数量和
最多五个路径，`by_count` 列出按数量排序的原因及计数。同原因内嵌套的已测目录不重复计量；
不同原因/阶段的字节不可相加，也不能视为可释放空间。证据缺口超过 20 GiB 告警；正常保护仅当同阶段、
同原因最近三个完整定时样本严格递增且累计 ≥3 GiB 告警，进程类别跨日忽略 PID。
近期活跃和等待闲置期届满等时间窗口类原因仍列排名，不参加保留原因趋势告警。
缺桶、未测或不完整报告不作趋势样本。
linked/clone 以 BB 定时 launcher 的 systemd invocation ID 对应报告；连续三个自然日的执行
均完整且零删除、同日 07:00–07:30 的三个有效样本显示保留路径对应的最具体计量根持续增长，
才告警。旧版、手动或不完整报告不计入；缺测不视为零。增长计量任务本身无需更换快照。
脚本同时在固定的干净 PATH 下比较 `bb --version` 与运行中服务端包的版本。
clone 报告的 `discovery.invalid_parent_markers` 单列 Git 不承认的父标记；报告只建议另行核查，
标记不进入 Git 树计量或候选，清理器不删除标记。复现用 `git -C <parent> rev-parse --show-toplevel`。依赖包生成内容不参与
ignored 凭据探测；已证实缺失的本地远端不提供 refs 证明，其他远端的网络或认证失败仍保留。
独立 clone 的 status 分类单列在 `summary.worktree_status`（棵数、目录实占、未测数）。
只有已识别的未跟踪可重建产物可继续判定；跟踪改动、未知文件或本地链接仍保留，
具体目录和证据见 storage-rules。120 秒是单棵复核上限，外层 30 分钟不变；
`summary.recheck_timeouts` 单列因单棵期限或整轮期限保留的路径及字节。

JSON 报告在 `~/.local/state/devcloud-storage-upkeep/git-tree-sweep-<UTC>.json`；
JSONL 同前缀，含阶段时点与每对象证据。schema_version=2 的 `unresolved` 每条含 source、expression、scope、
绝对 prefixes 或项目归属，以及实际 protected_paths；按前缀或项目限定的 unknown 不阻断其他路径。
名称引用明细在`discovery.configuration_name_references`（同时查看`satisfied_by/other_reason/protected_by_name`）；JSON状态按每次usage重新读取，无跨轮缓存。非Git项目容器的unknown标`project_scope_kind=source-points-and-environments`，不借环境remote扩散。
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

`bb automation` 的 succeeded 仅表示 worker 启动成功；实际成功以最新 JSON 报告和 JSONL 完整阶段为准。每日07:30读取两条路径的报告：linked 应已结束；全轮预演/执行硬截止均为07:23（均以06:30启动计）。clone 仍在自身预算内运行时记 pending。核对本轮JSONL start时间与mode，预算内仍运行记pending并明确尚无完成结论，不能拿前一轮报告冒充本轮成功，同时核对上一轮已结束的完整报告及失败项，防止每天pending掩盖上一轮失败；超限或停止后无完整报告算失败。`systemctl show devcloud-git-tree-sweep.service -p ActiveState -p Result` 可查在途状态；collect 后 unit not-found 属正常，仍必须有当日完整报告。

linked 删除恢复时从报告 urls/object/归档 ref `git fetch <url> <ref>` 后
`git worktree add <path> <sha>`；独立 clone 用记录 URL clone 后恢复每个 branch/tag。
恢复原始 SHA 的证据必须在删除前落账，不能仅记录成功的 installer/命令输出。

水位快照包含 `storage_watermark_brake.py` 和 `clean_tmp_runner.py`，计量快照包含
`storage_growth_measure.py` 与 `storage_growth_fingerprints.md`，Go 修剪快照包含 `go_cache_trim.py`，Cursor SDK 会话清理快照包含 `cursor_session_sweep.mjs`。
各快照附 manifest（每文件 sha256/mode），目录名为 **manifest 文件实际字节** sha256 前 16 位。
文件 `0444`、目录 `0555`；Python 入口都以 `python3 -I` 跑 `--help`，复算哈希并确认无 pycache，
发布前从 storage-rules 的 `growth-fingerprints` 标记段生成同目录指纹副本，测试核对两份表一致；
计量入口仅按自身目录读取副本，工作区规则改变不会影响已发布任务。确认各快照只含相应文件与 manifest。
只更新使用改动入口的任务，用短名称 bash `--script-file`，回读命令、时区、cron、enabled。已知坏目录
`8f4acd16bb88facd` 不保留；`916428875da4edee` 保留作回滚；其他旧快照按引用情况维护。

`clean-tmp` 执行器快照为 `/data/tools/devcloud-maintenance/releases/71d282c3d6426609/`，
由包装器的 `EXECUTOR` 引用。上一版 `64a16a707b7a1da1`、`cbb5caf329a61b57` 和旧版 `7ca528d7c5f3f004`、
`df3e2b353d8e1c72`、`f1e330c325c73fd0`、`1c2d71d3ab21cc79` 保留作回滚。升级执行器时按原 manifest
清单发布只读版本，复算文件哈希、权限和 manifest 目录名，再发布引用它的包装器快照。用新包装器
`--dry-run` 验收两根后，更新 cron 与水位入口并回读，按需同步每日 DSH 检查。

## 3. crontab

现行条目（root crontab，逐字）：

```cron
# devcloud-clean-tmp: ordinary entries 3d; structural caches idle 24h (owner-approved 2026-11-14)
30 3 * * * PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin /usr/bin/timeout --signal=TERM --kill-after=10s 1790 /usr/bin/python3 -I /data/tools/devcloud-storage-upkeep/releases/50d05aa5be8479af/clean_tmp_runner.py # devcloud-clean-tmp
```

- 先 `/var/tmp`（内部预算 300 秒，timeout 325 秒），再 `/data/system-tmp`（内部预算 1400 秒，
  timeout 1435 秒），外层 1790 秒，kill-after 10 秒。
- cron 与水位同用 `clean_tmp_runner.py`；全程持 `/var/lib/devcloud-storage-upkeep/clean-tmp.lock`，
  非阻塞拿锁，后台 timeout/执行器继承 fd；两盘同时越线合并为一个两根顺序 worker，
  执行器还保留自身每根 staging 锁。包装器用 `-I`，旧执行器维持 `-B` 以导入同目录 `devcloud_tmp`。
- summary/intent/deleted/skipped/failed 及包装器 runner_start/runner_finish（含退出码与 stdout）写 `/data/metrics/devcloud-clean-tmp.log`，
  stderr 追加到同名 `.stderr.log`。
- 执行复核发现活引用或候选变化时，保留该项并继续处理，已隔离的项先恢复原名。summary 的
  `skipped`/`skipped_reasons` 汇总执行阶段跳过，扫描过滤原因记录在 `scan_skipped`。
  Mole 拒绝、I/O 或恢复失败逐项记录，当前批次收尾后停止发下一批，避免在状态不确定时扩大删除范围。
- 水位用相同入口加 `--launch --path /var/tmp` 或 `--launch --path /data/system-tmp`；只派发不等待。
  包装器仅接受这两根。人工验收用同一包装器加 `--dry-run`，输出两根预览 JSON，不写执行日志。
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

## 6. Codex 沙箱与 Go 缓存

在支持原生 `writable_roots` 的 BB 上，先在标准路径创建真实缓存目录，再将其绝对路径
加入实际 `CODEX_HOME/config.toml`。本机的目录和配置如下。

```bash
mkdir -p /root/.cache/go-build /root/.cache/golangci-lint
```

```toml
[sandbox_workspace_write]
writable_roots = ["/root/.cache/go-build", "/root/.cache/golangci-lint"]
```

合并现有表和数组。配置后用 TOML 解析校验，在新 BB Codex `workspace-write` 线程内
运行 `go env GOCACHE` 和小包构建，确认 Go 与 lint 缓存可写、未配置的 HOME 子目录仍拒绝写入。
已有迁移软链时，等缓存写入者空闲后原子换成空目录。等沙箱验收通过并确认旧缓存没有进程引用，
再删除旧的可重建内容。设计背景见 [设计文档 3.1](DEVCLOUD_STORAGE_DESIGN.md#31-共享-golint-缓存)。

## 7. 换机器复现顺序

1. **盘与挂载**：确认 `/` 与 `/data` 的容量、文件系统；`/data` 写入 `/etc/fstab`；
   `/tmp` 做成 `/data/system-tmp` 的 bind。不要假设 `/` 一定是 500 GiB，先 `lsblk` / `df`。
2. **基础目录**：`/data/{bb-data,codex,docker,metrics,tools,artifacts,backups,output,ops}`；
   `/data/system-tmp`；`/data/metrics/devcloud-maintenance`；`/var/lib/devcloud-maintenance`。
3. **Docker**：`/etc/docker/daemon.json` 设 `"data-root": "/data/docker/lib"`，日志驱动
   `journald`；迁移已有 Docker 数据要在停写窗口做，不要热搬。
4. **工具链**：Python 3.12.12、Node 24.22.1（pnpm 12）、Go；路径以本机实际安装为准。
5. **共享缓存**：按第 6 节建标准目录、配置 Codex 可写根并验证。
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

2026-11-18复现：在新不可变快照运行默认全量预演；逐条导出unresolved的automation_id/name/enabled/last_run_at、expression、prefixes及protected_paths。对/data/code/mono2/.worktrees下每棵树列出最终原因与所有仍保护它的自动化，不只看优先命中的保留桶。比较新旧候选，新增项必须能追溯到去掉借用方alternates拒绝或按Python解释器修正未知引用；近期、脏树、引用、嵌套等条件不变。满足才更新每日执行并用--reviewed-preview绑定新预演手动执行。

前一轮预演（dd88e12c889a8f7c，20261118T012347Z）：709项回归通过，两轮只读审查发现均修复；扫描1,752,924目录、2,337标记，完整成功1039.615秒。4候选实占1.573666GiB，其中3项来自Python引用收窄；另1项 `/root/.agentflow/knowledge-cache/kb-git-1b7bb0e0b2f5a8bb`（128KiB）仅因自然满7天而新增，不符合本轮条件授权，因此未切换每日执行、未手动执行、未归档或删除。线上仍9656快照，工作区的新版每日检查prompt尚未部署。mono2/.worktrees总占24.980549GiB，19棵Git树全部因嵌套保留；其中14棵还受已暂停的auto_3yhlnd21-wo动态environmentPath保护。原mono2项目10条自动化的17条Python循环误报已解除，每条对这里的保护由17棵变为0。逐树、逐自动化及每条未知的全部保护路径见 `/root/.bb/thread-storage/thr_e7qrqpgykc/worktree-sweep/alternates-scope-20261118/REPORT.md`。新版本仍待本次新增候选审核，不以路径豁免绕过。

2026-11-18 owner复核澄清：已接受dd88预演4项；自然达到7天不需重新审批。新增同源嵌套例外，逐层比较当前common gitdir或有效fetch/push remote身份；异源、无remote及关系未知的树继续按父项目保留。发布需新快照全盘预演、规则来源核验、06:30脚本和每日检查prompt读回一致，再绑定新预演手动执行；auto_3yhlnd21-wo真实未知仍保留并单列。

最终发布与执行（0c547e019c0d46ea）：717项测试通过，Codex只读审查thr_g3zqb7f7mc的一项多remote桥接P1已修复并经旧版红灯/新版绿灯验证。完整预演1062.737秒，候选仍为owner接受的4项，无规则收窄新增；06:30保存脚本已切到新版--execute，每日检查prompt已更新并读回相同。首批绑定预演执行2066.148秒（34分26秒），added=[]：删除QQ修复linked worktree与agentflow知识库clone共2项，原du374571008B；12个tag按原SHA归档并独立远端复验。另agent-cli-profile-skills和hermes-agent由新出现的BB provider bridge worker（PID472396、cwd=/data/code）保护而保留，删除失败0。删除窗口可用净增368660480B；全程/data和/净变化为-2905645056B/-237510656B，含同期其他活动。mono2仍19树、容器26822660096B（24.980549GiB），本轮0删除/0释放；14同源树同时有名称碰撞与auto3y未知引用，5异源/未知保留，新BB进程cwd也覆盖这19树。证据=/root/.bb/thread-storage/thr_e7qrqpgykc/worktree-sweep/nested-origin-20261118/REPORT.md。尚待首次新版schedule，人工执行不算schedule。

2026-11-18三项引用范围收窄（1dfa3d6ab70611e6）：431项Git回归+336项兼容回归通过；独立审查thr_dyswdnb64k的两项JSON传播P1已修复并有12红/42绿回归。全盘预演1067.892秒，1,750,656目录/2,336树（2,182clone+154linked），34候选9.657GiB（29remove、5archive+remove）；117条unknown=72prefix+45project，0global。新候选来源均为三项授权收窄或不变规则自然生命周期，未发现意外扩大；06:30已切到1dfa执行，每日prompt已同步读回。mono2/.worktrees仍19棵、24.981GiB，无候选，原因已收敛为8棵近期7天/4BB环境/5异源嵌套/1alternates借出/1容器。auto3当前JSON精确保tooling，名称由mono2主树满足。每个sink现在都重做完整库存；本次34项积压的人工执行按实测668.759秒库存乘(N+2)、25%余量及30分钟网络/准备，单次预算540分钟/硬543分钟，仍绑定本次reviewed-preview。该轮日常预算为150/153分钟。该次人工执行在下述方向调整后于初始扫描阶段停止，归档/删除均0，不计首次新版schedule。证据=/root/.bb/thread-storage/thr_e7qrqpgykc/worktree-sweep/evidence-resolution-20261118/。

2026-11-18最新linked分工（以本段取代此前linked适用7天/全盘保护的描述）：

- `worktree_sweep.py` CLI调用 `git_tree_sweep.py:linked_main`，主仓来源是BB项目source/环境和最近一次完整两盘发现库存，实时Git登记是真相。只查locked、未销毁BB环境、活进程cwd在树内、当前分支/HEAD已推送或原SHA归档、24h。Git status用于预演，Git remove无force作为最终判断；之后Git prune，仅清失效登记。名称、项目fallback、嵌套、alternates、HOME/XDG、配置/容器等全盘clone规则不用于linked。
- `git_tree_sweep.py` 默认CLI只计划/执行独立clone，保持全部原7天保护；仍发现linked以建立clone引用索引和下次快速路径的已知主仓。`tmp_clone_sweep.py` CLI转发它。
- launcher调用5脚本快照内的worker，先linked15m，再clone45/150m；整体硬限63/168m，cwd=/，BB启动超时120秒。一条失败仍尝试另一条，最终非零；各阶段有独立锁及完整报告。
- linked报告=`linked-worktree-sweep-<UTC>.json(l)`，schema_version=3、policy=linked；clone报告仍`git-tree-sweep-<UTC>.json(l)`、schema_version=2、policy=clones。检查两份结果，不用clone耗时掩盖linked是否已完成。
- 手动只验证本次变更：`BB_CLI="$(/usr/bin/python3 -I <snap>/git_tree_sweep_bb_cli.py)" && export BB_CLI && python3 -I <snap>/worktree_sweep.py --json`，确认完整后 `BB_CLI="$(/usr/bin/python3 -I <snap>/git_tree_sweep_bb_cli.py)" && export BB_CLI && python3 -I <snap>/worktree_sweep.py --execute --reviewed-preview <linked预演JSON> --json`。每项最终复核仅重读BB/进程cwd/Git登记/状态及当前分支，不重复全盘扫描。独立clone人工命令仍使用它自己的预演文件；launcher仅接受无参数或--execute，不混用两类预演。
- 旧1dfa人工执行已于2026-11-18 12:56停止，尚在发现阶段，归档/删除均0。本轮人工执行针对linked；34个clone候选继续由原每日clone策略处理。尚未发生新版schedule时明示等待首次定时运行。

linked年龄补充：计算工作树文件/目录和自身gitdir的实际文件（index、HEAD、FETCH_HEAD、reflog等）mtime，不以Git管理目录自身mtime作为活动。实测3棵mono2树仅管理目录被临时锁/探针刷新，实际文件均旧于24h；目录mtime不应使它们永久刷活。此修正只用于linked，clone保持原7天全部元数据规则。

2026-11-18 linked最终发布与执行（6d0208714b86c771）：494项Git回归+336项兼容回归=830项通过；Codex只读审查thr_d2prih37sv的一项self local/file恢复证明P1已修复，8红/73绿验证，cwd容器bind子目录另1红→绿。独立clone九个核心判定函数与1dfa的AST相同。5脚本快照哈希/0444/0555/无pycache均核验。06:30 enabled、Asia/Shanghai、--execute、120秒launcher及每日prompt读回一致；新版首次schedule尚待运行。
最终linked预演128.080秒，1118主仓、79登记linked+81未登记/残缺gitfile、27候选（13个ref待归档）；完整执行278.043秒，complete=true/error=null、added=[]。实际删除10棵，其中mono2 6棵；17候选保留（13个Git回指校验拒绝、3个归档仓库不存在、1个内部远端凭据不可用）。实际9ref均原SHA归档并独立ls-remote复验，其中2棵随后删除、7棵Git仍拒绝。1118主仓prune均成功，清10个失效登记，不计目录删除。
删除10棵原du9732804608B（9.064GiB），删除窗口可用净增9804591104B（9.131GiB）；mono2六棵原du6141558784B（5.720GiB），对应窗口净增6167609344B（5.744GiB）。全程/data和/可用分别+7192014848B/+2202112000B，均含同期活动；执行后/data=275576782848B、/=210092060672B。mono2/.worktrees du从26983763968B降到20832604160B（25.131→19.402GiB）。13个有效登记linked中6删/7保留（4BB环境、1近期、1脏、1Git回指不一致）；另1残缺gitfile及5独立clone未删。17个独占测试根核无活cwd/exe/fd后清理，发生于容量采样后，单独记账。逐linked与mono2结果、删除/归档/失败清单、容量窗口和全部原始证据见/root/.bb/thread-storage/thr_e7qrqpgykc/worktree-sweep/linked-fastpath-20261118/REPORT.md。源码、测试、规则与文档留工作区由owner提交，无commit/push。

2026-11-18遗弃BB环境补充：`bb environment cleanup --help`和当前安装版实现已确认：仅providerOwnsPath/managed=true接受cleanup，有未归档线程时拒绝，成功异步返回。环境列表的providerId不代表拥有目录（project-checkout通常managed=false）。`delete`对provider=null路径型环境仅记录destroyed而不删目录；本流程不调用它伪造回收。BB git-worktree provider内部会强制移除并停止cwd用户，因此先确认环境和线程闲置、工作文件24h无活动、无cwd或共享活环境，再交owner处理，不手工改库或使用recover。
每日顺序改为bb_environment_sweep.py（5m）→worktree_sweep.py（15m）→git_tree_sweep.py（45/150m），整体硬限68/173m，06:30执行硬截止09:23。环境报告bb-environment-sweep-<UTC>.json(l)，schema_version=1/policy=bb-environments，逐环境含thread_ids、最后活动、idle_hours、protects/abandoned、action、cleanup结果/拒绝。51条首轮metadata候选含49条有路径、2条未物化error记录，预演不请求cleanup；含env_w7kw3b8puw，2线程均归档、BB闲置约76小时。最终部署/实测在本轮报告补齐。
复现：先`bb environment show <id> --json`和`bb thread list --environment <id> --include-archived --include-hidden --json`核证；再`python3 -I <snap>/bb_environment_sweep.py --json`预演，执行加--execute。随后分别预演/执行linked与clone；两条Git路径每次重读同一环境/线程判定，不依赖上轮遗弃名单。

遗弃环境审查修正：systemd worker仅传PATH、没有BB_THREAD_ID，`bb status --json`的thread=null，不能从线程环境推断本机。现用无线程也可读取的`bb machine list --all --json`，只接受唯一connected/persistent且machineProviderId=null的本地server身份，未知/歧义不回收。逐环境show/cleanup协议/网络错误继续其他环境，但累计failures、complete=false、退出非零；仅明确BB业务拒绝作为正常Git兜底。真实systemd无线程环境预演1.553秒完成，51路径型遗弃环境解除证据，无BB写操作。

本轮新版已发布9217bf8ab4fc6204，06:30与每日prompt读回一致，878项回归通过293.47秒。两轮只读review（thr_nxrq46ci98、thr_cxw4rqxnb7）发现2项P1均闭合，复审无发现。完整clone预演1223.785秒、1717993目录/2446Git标记、35候选，发现错误/global未知均0；32个既有候选不变，新增3个均来自遗弃环境：agent-cli-hermes-reset-20261028、lightcloud-ai-infra/skills、rea。人工验证动态选这3个BB解锁clone（无手写路径白名单），仍用同一core.execute、初始与每sink完整两盘复扫；其余32个继续每日全盘执行，不从生产计划排除。3项按实测四轮约800秒库存加准备设本次人工90分钟预算，生产clone仍150分钟。
环境人工阶段1.446秒，识别52条遗弃记录，其中50条有路径并解除环境证据，另2条error从未物化、无目录，不计回收（比预演51多1个自然24h到期），provider实际回收0，环境登记未直接修改。linked人工执行216.013秒，19候选中删除2项：/data/bb-data/thread-storage/mono2-upgrade-ctl-readiness-wait-20261115和/data/code/mono2/.worktrees/release-three-images；17项按Git/远端拒绝保留，0新增归档，prune1118主仓均成功。目录原du2256400384B；该阶段/data可用净增2298630144B，含同期活动；监测启动时这2项已删，删除窗口缺测，不能把窗口0读数当释放0。clone三项执行结果与总容量以最终报告为准。证据=/root/.bb/thread-storage/thr_e7qrqpgykc/worktree-sweep/abandoned-environments-20261118/。

遗弃环境本轮最终结果：三阶段complete=true/error=null，manual unit Result=success并collect。环境1.446秒；linked216.013秒；3个新增clone2678.488秒（44分38秒）。实际共删除5棵（2linked+3clone），17候选保留，0新增归档；5个删除路径独立确认不存在。50个有路径的遗弃环境解除其环境证据；2条无path/instance的error记录未物化，原本不占目录，不能算作目录回收；0provider-owned环境实际命中，BB登记未直接改动。
删除目录原du3553800192B/3.310GiB；linked阶段/data净增2298630144B/2.141GiB（监测启动晚于2次删除，短窗口缺测），clone三次删除窗口净增1297305600B/1.208GiB（全程提前挂监测）。两者为不同非重叠窗口观察，均含同期活动；全轮/data净增337158144B/0.314GiB，/净变-12926976B，不能将并发写入混为清理失败或把du当因果释放量。完整逐环境/逐树/失败清单及结束容量见/root/.bb/thread-storage/thr_e7qrqpgykc/worktree-sweep/abandoned-environments-20261118/REPORT.md与final-summary.json。原32个既有clone候选继续在每日全量阶段处理，未建立排除清单。代码与测试留工作区由owner提交，尚待首次新版schedule。

2026-11-19 CLI绑定修复：BB script真实探针auto_pam50rztcys/arun_sabavlvdfgg确认注入/root/.bb/bin/bb，version=0.49.0、include-archived受支持；探针已删除。launcher运行git_tree_sweep_bb_cli.py：先用BB_CLI，未注入时用systemctl show bb-dev-server.service --property=MainPID --value，再读取/proc/PID/exe（本机为包内bin/node），推导包内host-daemon/dist/bb。绝对可执行/选项探测成功后通过systemd --setenv=BB_CLI传入；worker再次验证，core.command所有bb调用改为该绝对路径且固定子进程BB_CLI。不修改全局/root/.local/bin/bb，不回落旧PATH。报告bb_cli必须是绑定路径，三阶段complete/error另行核对。证据在/root/.bb/thread-storage/thr_e7qrqpgykc/worktree-sweep/bb-cli-binding-20261119/。
BB外直接运行Python入口，使用上方已检查退出状态的绑定步骤；先独立赋值、成功后export，再运行Python，不能用export包住命令替换（它会吞掉绑定器退出码）。正常自动化由launcher处理。CLI不存在、服务不在运行、能力探测失败时按stderr点名的原因和命令修复再重试。7脚本快照包含新绑定helper。

MainPID回退的host-daemon入口是env-node脚本；binder的command_for为探针和所有阶段命令绑定同包bin/node，最小PATH=/usr/bin:/bin无需另装Node。非零能力探针保留退出码与首条stderr，和缺少选项分开诊断。普通回归直接验证当前worker，不依赖历史commit可用；修复前红灯只作审查证据。
本次完整人工执行使用 `/data/tools/devcloud-storage-upkeep/releases/13f015132208a4ba/`，只将clone预算从150m增至540m、整体硬限从173m增至563m；发现/判定/执行代码与日常快照相同。依据上轮3个clone耗2678.488秒且有32项存量，给完整复核有界时间；日常06:30仍用b70f81e79242476d和原预算，不保留人工路径清单。

第二轮审查补充：旧PATH回归现经过三条真实stage main及clone的ws.bb_anchors共享runner，只替换宿主磁盘发现和外部BB响应；三份隔离报告均完整。手动示例在扫描之前检查binder退出码，失败不启动Python。

2026-11-19 CLI修复最终实测：正式快照b70f81e79242476d（7脚本）已部署06:30/AsiaShanghai/--execute/timeout120s，旧tmp disabled+noop、每日prompt回读一致。真实BB script人工run arun_3efzqydrbty通过临时预算快照13f015132208a4ba启动，随即恢复正式任务。三阶段complete=true/error=null，CLI均/root/.bb/bin/bb且0.49.0；耗时环境2.274s、linked237.994s、clone8197.665s（136m38s，仍在日常150m内），worker成功退出，BB服务未重启。53个有路径遗弃环境解除证据，provider实际回收0；另2未物化无路径记录不计目录回收。删除33树（1linked+32clone），18归档ref/14原SHA独立核验，17linked按Git/远端拒绝保留，1115主仓prune成功。删除前du9.714GiB；clone按目标盘删除窗口净增/data9.494GiB、/0.132GiB，全程净变化/data2.085GiB、/0.391GiB，含同期活动不当因果释放量。891全量回归通过，补强29项再过，2轮readonly review共5项P1闭合，工作区留owner提交。完整清单/快照/证据=/root/.bb/thread-storage/thr_e7qrqpgykc/worktree-sweep/bb-cli-binding-20261119/REPORT.md。下一次schedule仍待11月20日06:30验证。

### 2026-11-19：clone执行性能与本轮快照

按owner明确选择，完整全盘引用库存在本轮复用。报告global_inventory.started_at/discovered_at/references_checked_at记录UTC，执行candidate_started及每行global_inventory_at绑定该轮；新建但不活跃的全局consumer下轮发现。每个sink实时检查cwd/fd/argv（源clone在--shared/--reference操作数里也保留）、status/untracked/stash/锁、该树所有refs/HEAD的实时远端或原SHA归档证明、BB环境。原exe/maps/进程环境、7天/凭据与物理身份保护继续保留。未知或单树复核超过10秒保留并记录，下一轮重试。

每树execution_timings是组件墙钟秒数，execution_subprocess_seconds是其内的子命令时间，二者不能重复求和；total是整项耗时。全局timings增加references_seconds、usage_seconds、planning_seconds、measurement_seconds、measurement_wait_seconds、execution_seconds。旧plan_seconds仍是从阶段开始到计划完成的累计值。du与全局引用/配置/计划并发，measurement_seconds因此和其他阶段重叠，只把execution_seconds作为本次执行循环耗时。

clone预演/执行预算均30分钟，worker仍先环境5分钟、linked15分钟，整体硬限53分钟；06:30轮最迟07:23，07:30检查应见完整结果。旧45/150与68/173为上述历史执行的预算，后续任务按本节。仅更新Git快照与其任务，不更换其他维护任务。发布、分项计时、全量预演和执行实测结果在本轮报告中补齐。

性能审查修正：全局进程快照必须先于du启动，usage复用该快照；否则du的argv/fd会误护自己正在计量的树。最终sink实时核mountinfo并拒绝根/后代挂载（同设备bind也一样），身份stat后再查截止时间。10秒限制删前复核，递归删除已开始则完成并单列delete耗时，不中途取消。shell argv用保留引用的操作符词法拆分，紧邻分号/管道/重定向的源路径与带引号标点路径均覆盖。

复审补强：mountinfo按ASCII空格/换行解析，保留路径中的NBSP等特殊空白；shell argv同时识别-lc/-xc等命令选项组合，保持argv唯一引用也会保护。生产快照必须包含这两项修复，99470bff9a1e5bda仅为二审前的历史准备版。

2026-11-19性能最终实测：943项回归通过，两轮只读审查的6项发现均修复。完整两盘预演604.474秒，执行556.413秒（9分16秒），均complete=true/error=null；执行循环106.365秒，32棵任务自有基准clone全部删除，平均3.319秒/棵、最慢4.164秒，归档/失败0，业务新增候选0，reviewed_comparison.added=[]。基准每棵100个tracked文件，8棵另有2500个ignored文件；此批与旧32棵业务仓库的文件量不同，不将两批总时间直接当作同负载加速比。同条件两样本旧218.858/530.170秒→新3.808/3.743秒，瓶颈为重复全盘发现和全局usage；本轮32棵的实时进程合计57.551秒、BB环境41.543秒、远端1.287秒、实际删除1.231秒。全局库存UTC开始06:57:06、发现07:00:08、引用完成07:01:07；轮内复用，逐树实时核查照常。

正式任务auto_hukidoavox0已回读：3c35快照、enabled、06:30/AsiaShanghai、--execute、120秒launcher；worker5/15/30分钟、硬限53分钟，每日prompt一致，旧tmp任务disabled+noop。人工本轮仅验证完整clone阶段，不冒充三阶段schedule；下一次定时验证为11月20日06:30。任务自有基准根及测试夹具已清理；未部署的8197/9947准备快照删除，补丁与审查日志保留。完整逐树耗时、容量净变与复现证据：`/root/.bb/thread-storage/thr_e7qrqpgykc/worktree-sweep/clone-performance-20261119/REPORT.md`。改动留工作区，由owner提交。

### OpenCraft owner prune（2026-11-28）

`auto_-vwoibpctju` 每日 06:20（Asia/Shanghai），工作目录 `/data/code/opencraft`，保存脚本执行
`/usr/bin/python3 -I /data/tools/devcloud-storage-upkeep/releases/7ec41b4155854883/opencraft_local_runtime.py --apply`。
命令使用 `/usr/bin/timeout --signal=TERM 840`，BB 超时 900 秒。包装器调用 checkout 当前的 `npm run maintenance:local-runtime -- --apply`，
不冻结或重写 opencraft owner 工具；其保留契约见 `docs/operations/local-runtime-retention.md`。
PR #670 合并后与线上 owner handoff 同步：Node 26.7.0，保留 `.runtime`、`.local/execution-separation` 和 personal-relay 下的恢复配置等 19 个精确路径（完整清单见包装器 `PROTECTED_PATHS` 与 storage-rules）。不再整棵保护 personal-relay；owner 按 current/previous 回收更旧 releases，按 `.opencraft-disposable.json` 回收已登记验收产物。凭据、工作簿/Git、审计、恢复状态、进程、容器 bind mount（含 stopped/created）及检查未知继续保留。

增长计量 `auto_u4kos3to-di` 切换为快照 `6d42fe369b4f58ca`，登记 `/data/code/opencraft` 的 owner 和清理方。
更新每日 prompt 后执行 `python3 -I scripts/sync_daily_check_prompt.py --apply` 和 `--check`。
任务保存命令、enabled、cron、时区、timeout、工作目录须读回一致；人工执行不替代首次 schedule 验收。
逐项首次清理清单、df/du、PR #286 后新增路径的生产证据只保存在本机账本及
`.local/devcloud/opencraft-runtime-20261128/`，不把私有运行记录提交到公开仓库。

此次 #670 同步将任务的 inline owner 命令替换为仓库 `scripts/opencraft_local_runtime_run.sh` 的精确副本，
其中仅有不可变快照入口，不执行维护记录中的可变脚本路径。发布后须比较 BB 读回 `execution.script`
与该源文件逐字一致、快照包装器与仓库逐字一致，并核 manifest 哈希、0444/0555、19 个精确保护路径、
Node 26.7.0、06:20/Asia/Shanghai、cwd 和 timeout。只更新此任务，不修改增长计量快照。
