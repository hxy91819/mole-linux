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
   - auto_hukidoavox0（每天06:30，先linked再独立clone）：25小时。保存script调用当前不可变快照的git_tree_sweep_run.sh，模式按owner授权/部署记录核对。BB timeout=120秒，worker cwd=/，linked预算15分钟，clone预算预演45分钟/执行150分钟，整体硬限预演63分钟/执行168分钟（06:30执行最迟09:18）。BB succeeded只表示启动。读取当日两个阶段的JSONL start与最新完整报告：linked-worktree-sweep-*.json（schema_version=3，policy=linked）及git-tree-sweep-*.json（schema_version=2，policy=clones）。linked必须先结束；clone尚在预算内记pending，并同时检查上一轮完整结果/失败项，不用旧报告冒充本轮成功。systemctl show devcloud-git-tree-sweep.service -p ActiveState -p Result核在途状态；collect后not-found正常，但已结束阶段必须有完整报告。两阶段complete=true、error=null，列候选、removed/kept、archive URL/ref/SHA、Git拒绝与prune结果、未计量大小和timings。linked只看实时Git登记、未locked、不是未销毁BB环境、活进程cwd不在树内、当前分支/HEAD有远端分支证明或原SHA归档、≥24h（工作文件/目录及自身Git实际文件mtime，忽略Git管理目录自身mtime）；unresolved_mains按单主仓保留原因汇总，不能当成全局阻断；不套名称/项目fallback/嵌套/alternates等clone规则，Git remove不用force，Git拒绝保留，prune仅清失效登记。clone仍全盘原7天/clean/全部branch HEAD tag远端证明/凭据/主仓有linked/引用等保护；名称先归属有其他保护的同remote副本，cwd只护包含它的树，HOME/XDG保留原子树与三例外；逐条unknown source/expression/scope/protected_paths，global_unresolved为空，发现errors为空。JSON状态只读解析，非Git项目容器不扩展。首批人工执行若带reviewed-preview，added必须为空；日常不绑定路径清单。目录原du与容量净变化分列。新版尚无schedule时写“等待首次定时运行”，人工单独linked验证不算完整schedule。
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
