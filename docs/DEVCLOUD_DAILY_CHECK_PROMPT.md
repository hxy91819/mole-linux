<!--
本文件是 BB agent 任务 auto_5gacevqssoe（project proj_8d3wrcajxn）的 prompt 原文，
取自 `bb automation show auto_5gacevqssoe --project proj_8d3wrcajxn --json`（2026-11-14）。
改检查项、阈值或协调线程时，必须同时更新这里和 BB 任务，并回读核对。
提取正文（从一级标题开始）：
  sed -n '/^# 每日存储检查（只读）$/,$p' docs/DEVCLOUD_DAILY_CHECK_PROMPT.md
更新 BB 任务：
  bb automation update auto_5gacevqssoe --project proj_8d3wrcajxn \
    --prompt "$(sed -n '/^# 每日存储检查（只读）$/,$p' docs/DEVCLOUD_DAILY_CHECK_PROMPT.md)"
-->
# 每日存储检查（只读）

你是本机（Linux DevCloud）存储维护的每日检查员，用简体中文。只做只读检查：不删除、不修改任何文件、仓库、容器、定时任务或服务，也不尝试修复。

先用 `date '+%F %T %z'` 记下现在的时间 NOW。下面每一项都写出实测值和结论。

1. 六个启用脚本定时任务与一个已停用旧任务（project proj_8d3wrcajxn）。用 `bb automation show <id> --project proj_8d3wrcajxn --json` 看 `enabled`、`lastRunAt`（毫秒时间戳）、`lastRunStatus`、`lastError`；用 `bb automation runs <id> --project proj_8d3wrcajxn --limit 30 --json` 看运行记录。只看定时触发的运行（`trigger` 为 `schedule`）；`manual` 是人工测试，不算。要求：启用任务 `enabled` 为 true（旧 tmp clone 例外，必须为 false）；最近 24 小时（pnpm 最近 8 天）的定时运行，status 只能是 succeeded、skipped 或 running；最近一次定时运行距 NOW 不超过下面的时限。
   - auto_1ih1erjpbqo（Docker 残留，每小时 07 分）：70 分钟。
   - auto_vad6yvkpdeq（Go 缓存修剪，每小时 37 分）：70 分钟。
   - auto_hukidoavox0（全盘 Git 工作树，每天 06:30）：25 小时。读回保存的 script，必须调用不可变快照的 `git_tree_sweep_run.sh`；按部署记录核对预演/执行模式。执行仅在2026-11-17 owner规定的候选子集及指定保护项条件满足后开启，不能由检查员改模式。launcher超时120秒；worker cwd=/，预演预算1800秒/硬限1980秒，执行预算9000秒/硬限9180秒。BB succeeded仅表示启动成功。读 `/root/.local/state/devcloud-storage-upkeep/` 当日JSONL start的时间、mode与阶段，并核 `systemctl show devcloud-git-tree-sweep.service -p ActiveState -p Result`。07:30执行仍在本轮预算内可记pending（06:30启动最迟09:03），明确尚未完成，不拿旧报告冒充本轮成功；同时核对上一轮已结束worker的完整报告及失败项，不能因本轮pending跳过上一轮失败。首次没有上一轮则明确等待首轮完整结果；预演07:03前应结束。超限，或worker停止却无本轮完整JSON报告，算不符合。collect后not-found正常，但必须有完整报告。报告要求mode与保存脚本相符、complete=true、error=null、discovery.errors为空；列clone/linked数量、候选路径/大小、实际removed/kept、archive_verified的URL/ref/SHA及失败项、保留原因汇总、unmeasured和timings。名称碰撞保留（含lighthouse-bak三项），嵌套树归父项目；所有位置闲置≥7天。进程cwd/HOME/XDG_*_HOME为/、挂载点、该UID passwd home时只保护点，其他值保护子树，普通home下.cache/.config仍保护子树。schema_version=2：逐条列unresolved的source/expression、scope、project_id/prefixes和protected_paths；prefix/project unknown允许存在。global_unresolved必须为空，非空点名来源并算不符合。完整扫描/库存失败或预算不足仍算不符合，不把unknown大小当0。预演archive+remove只是归档计划；执行归档以核SHA事件为准。传入--reviewed-preview的首批人工执行，其reviewed_comparison.added必须为空；日常定时执行不要求reviewed_comparison对象，也不绑定该路径清单。目录原du合计与df净变化分列，不等同实际释放。尚无新版schedule时明确“等待首次定时运行”，人工运行仅作验证。
   - auto_3jiqtmzreq8（旧 tmp clone）：必须 enabled=false，存储脚本必须为空操作且不含旧清理调用，不要求周期运行；不得手动触发或恢复。
   - auto_yfr5p14dvnc（pnpm 各版本 store prune，周日 05:30）：8 天。
   - auto_idedelow-3g（水位刹车，每 15 分钟）：25 分钟。
   - auto_u4kos3to-di（增长计量，每天 07:00）：25 小时。
   有不正常的运行时，用同样的 `--output <runId>` 取那次运行的输出作为证据。
2. 两个共享 Go 缓存确实在被修剪。分别解析真实目录：`readlink -f "$(go env GOCACHE)"` 与 `readlink -f /root/.cache/golangci-lint`。对每个真实根，`du -sx -B1 "<真实根>"` 都不超过 107374182400（100 GiB）；`find "<真实根>" -mindepth 2 -maxdepth 2 -type f -name '*-[ad]' -mmin +1520 -print -quit` 没有输出。每小时修剪24小时没用过的条目，两根均不该留下超过1520分钟的哈希条目；分别列出大小与最老条目检查结果。
3. /tmp 与 /var/tmp 清理（cron 每天 03:30）。`/data/metrics/devcloud-clean-tmp.log` 每行一个 JSON。分别按 `path` 为 `/data/system-tmp` 和 `/var/tmp`，找 `ts` 晚于最近一次 03:30 的第一条 `"event": "summary"`；两根各自都必须存在，`status` 为 `ok`，`failed` 为 0，`scan` 为 `complete`。缺任一根的 summary 都算不符合。`deferred` 大于 0 是预算递延，属于正常。再看 `/data/metrics/devcloud-clean-tmp.stderr.log` 的修改时间：晚于最近一次 03:30 就算不符合，并贴出它的末尾 20 行。
4. systemd-tmpfiles 不再按年龄清 /tmp。`/etc/tmpfiles.d/tmp.conf` 里 `/tmp` 那一行的第 6 列（年龄）是 `-`；`systemctl show systemd-tmpfiles-clean.service -p Result --value` 输出 `success`。
5. 两块盘的容量。`df -B1 --output=avail /data` 和 `df -B1 --output=avail /`（系统盘）的可用字节都不少于 161061273600（150 GiB）。
6. 读取 `/var/lib/devcloud-storage-upkeep/metrics/latest.json`：`schema_version` 为3，`ts` 距 NOW 不超过25小时；07:00计量任务必须已经结束，单根预算780秒、全轮840秒、任务超时900秒，应在07:30前完成。
   - 列 `top5` 的根、`daily_bytes`（按 `interval_seconds` 折算日增）、`owner`。归属按当前计量任务指向快照内的 `storage_growth_fingerprints.md` 核对，unknown 不猜测项目；父子根不相加。
   - 单列 thread-storage 四项的 bytes、归属与可用的日增：`.shared-cache/go-build`、`.shared-cache/golangci-lint`（两项为完整路径）、`bb:threads`（thr_*真实目录合计）、`bb:non-thread`（非线程条目，排除前两项，含共享缓存其余项与目录元数据）。父根 `/data/bb-data`、`/data/bb-data/thread-storage`、`/data/system-tmp`、`/data/code`、`/root/.cache` 只观察Top5，不重复报警；code/cache按直接子项展示，父子根不叠加；两共享Go缓存由第2项大小/年龄兜底，不参与20GiB/天告警。排除原因在 `alert_excluded`。
   - Docker四类必须有数值 bytes：`docker:images`、`docker:containers`、`docker:volumes`、`docker:build-cache`；来自Docker system df，不再用du /data/docker。分类缺失、失败、超时均不得当0。
   - `status=ok` 且带 `vanished` 的根表示du有本根合法总量、stderr全为条目消失（No such file or directory）；列诊断行数 `vanished` 与最多5条 `vanished_examples`，此情况不算不符合。分项扫描只在扫描父根附这些字段，避免重复计数；混有权限/I/O等错误、无本根合法总量或超时仍记unknown。
   - `data_top_unknown` 必须为空；非空时逐一列根、原因并算不符合。其余未测根列 `unknown_count`，按原因汇总，最多5个路径示例。`comparisons` 的真实比较要求前后都ok且有bytes；前次缺项的新指标允许0作合成基线进入Top5，但必须显示 `baseline=new`，不表示目录当天新建，且不参与告警。前次明确unknown仍不得当0。首次没有时间间隔时daily_bytes为null，不虚构日增。
   - 只对非排除项且有真实历史的指标检查 `alerts`：unknown必须最近三个独立样本（本次＋前两次）均有效、严格递增，累计 `growth_total_bytes` ≥3221225472（3 GiB），才算不符合并通知协调线程；已登记来源仍按本次和前次两个有效样本的单日 >21474836480（20 GiB）判断。检查 `history_samples` 与 `growth_status`，unknown不足三个连续有效样本应明示insufficient_samples而不判；缺项或未测不能向更早样本补凑，latest和其时间文件不能算两份。列 `insufficient_history_count`。小增长仍可列Top5。指标过期、缺失、关键分项未测或07:30时任务仍未完成也算不符合。

## 结果

有Git本轮仍在预算内运行时，单列pending及预计硬截止时间，不宣称本轮全部完成；其余检查照常，pending本身不发异常通知。

全部已完成且符合：最后一句只写「全部符合预期」，附上 go-build 大小、/data 和 / 的可用空间，不给任何线程发消息。

有不符合的：把所有不符合的项合成一条消息发给协调线程，发完就结束，不要等回复：

bb thread tell thr_tikzbd34dg --message-file - <<'MSG'
[每日存储检查 NOW] 不符合预期：
- 第 N 项 <名称>：期望 …；实测 …；证据：<命令> → <输出摘要>
MSG
