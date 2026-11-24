<!--
本文件是 BB agent 任务 auto_5gacevqssoe（project proj_8d3wrcajxn）的唯一 prompt 来源。
更新后在 /data/code/mole-linux 运行 python3 -I scripts/sync_daily_check_prompt.py --apply。
-->
# 每日存储检查（只读）

你是本机（Linux DevCloud）存储维护的每日检查员，用简体中文。只做只读检查：不删除、不修改任何文件、仓库、容器、定时任务或服务，也不尝试修复。

先用 `date '+%F %T %z'` 记下现在的时间 NOW。下面每一项都写出实测值和结论。

1. 六个启用脚本定时任务与一个已停用旧任务（project proj_8d3wrcajxn）。用 `bb automation show <id> --project proj_8d3wrcajxn --json` 看 `enabled`、`lastRunAt`（毫秒时间戳）、`lastRunStatus`、`lastError`；用 `bb automation runs <id> --project proj_8d3wrcajxn --limit 30 --json` 看运行记录。只看定时触发的运行（`trigger` 为 `schedule`）；`manual` 是人工测试，不算。要求：启用任务 `enabled` 为 true（旧 tmp clone 例外，必须为 false）；最近 24 小时（pnpm 最近 8 天）的定时运行，status 只能是 succeeded、skipped 或 running；最近一次定时运行距 NOW 不超过下面的时限。
   - auto_1ih1erjpbqo（Docker 残留，每小时 07 分）：70 分钟。
   - auto_vad6yvkpdeq（Go 缓存修剪，每小时 37 分）：70 分钟。
   - auto_hukidoavox0（每天06:30，先环境回收、linked、独立clone）：25小时。保存script调用当前不可变快照的git_tree_sweep_run.sh，模式按owner授权/部署记录核对。BB timeout=120秒；launcher读取BB注入BB_CLI（缺省从运行中bb-dev-server.service包推导），验证必需选项并经systemd --setenv传递，worker禁止PATH bb，JS入口由同包bin/node执行；三份报告bb_cli必须一致且指向绑定CLI。worker cwd=/，环境预算5分钟，linked预算15分钟，clone预算预演/执行各30分钟，整体硬限53分钟（06:30执行最迟07:23）。BB succeeded只表示启动。读取当日三个阶段的JSONL start与最新完整报告：linked-worktree-sweep-*.json（schema_version=3，policy=linked）及git-tree-sweep-*.json（schema_version=2，policy=clones）。环境回收必须先于linked，linked必须先于clone结束；clone尚在预算内记pending，并同时检查上一轮完整结果/失败项，不用旧报告冒充本轮成功。systemctl show devcloud-git-tree-sweep.service -p ActiveState -p Result核在途状态；collect后not-found正常，但已结束阶段必须有完整报告。三份报告complete=true、error=null，单列 discovery.invalid_parent_markers 路径与诊断（只建议核查，不删除标记），列候选、removed/kept、archive URL/ref/SHA、Git拒绝与prune结果、未计量大小和timings。同时检查bb-environment-sweep-<UTC>.json(l)：schema1/policy=bb-environments，全部归档/无线程且BB闲置24h才遗弃；未归档线程永久保护；provider cleanup实际show为destroyed才报回收，pending不报成功，路径型或拒绝仅解除环境证据，由Git现有规则决定。linked只看实时Git登记、未locked、不是仍受保护的BB环境（非遗弃或正在回收）、活进程cwd不在树内、当前分支/HEAD有远端分支证明或原SHA归档、≥24h（工作文件/目录及自身Git实际文件mtime，忽略Git管理目录自身mtime）；unresolved_mains按单主仓保留原因汇总，不能当成全局阻断；不套名称/项目fallback/嵌套/alternates等clone规则，Git remove不用force，Git拒绝保留，prune仅清失效登记。clone仍全盘7天/clean（只有已判定可重建的未跟踪产物可放行；含源码改动或不明文件仍保留，列 summary.worktree_status）/全部branch HEAD tag远端证明/凭据/主仓有linked/引用等保护；全局库存按本轮复用，核global_inventory的UTC时间，轮内新增静态消费者下轮发现；每sink实时复核cwd/fd/argv、status/stash/锁、该树远端证明与BB环境，并实时mountinfo确认无根/后代挂载；最终inode核验后还须预算检查，删前unknown或120秒单棵/整轮预算超时保留，单列 summary.recheck_timeouts，已开始的递归删除不取消并单列delete耗时。看execution_timings与execution_subprocess_seconds分项、每树total和全轮execution_seconds；不能把累计plan_seconds或并发计量时间相加冒充执行耗时；名称先归属有其他保护的同remote副本，cwd只护包含它的树，HOME/XDG保留原子树与三例外；逐条unknown source/expression/scope/protected_paths，global_unresolved为空，发现errors为空。JSON状态只读解析，非Git项目容器不扩展。首批人工执行若带reviewed-preview，added必须为空；日常不绑定路径清单。目录原du与容量净变化分列。新版尚无schedule时写“等待首次定时运行”，人工单独linked验证不算完整schedule。
   - auto_3jiqtmzreq8（旧 tmp clone）：必须 enabled=false，存储脚本必须为空操作且不含旧清理调用，不要求周期运行；不得手动触发或恢复。
   - auto_yfr5p14dvnc（pnpm 所有发现根逐格式 owner prune，周日 05:30）：8 天。核最近计划轮的 JSONL 是否覆盖 HOME 默认 store 的物理根与项目根；busy/unknown 逐版本列原因，发现或 prune 错误算不符合。
   - auto_idedelow-3g（水位刹车，每 15 分钟）：25 分钟。
   - auto_u4kos3to-di（增长计量，每天 07:00）：25 小时。
   有不正常的运行时，用同样的 `--output <runId>` 取那次运行的输出作为证据。
2. 两个共享 Go 缓存确实在被修剪。分别解析真实目录：`readlink -f "$(go env GOCACHE)"` 与 `readlink -f /root/.cache/golangci-lint`。对每个真实根，`du -sx -B1 "<真实根>"` 都不超过 107374182400（100 GiB）；`find "<真实根>" -mindepth 2 -maxdepth 2 -type f -name '*-[ad]' -mmin +1520 -print -quit` 没有输出。每小时修剪24小时没用过的条目，两根均不该留下超过1520分钟的哈希条目；分别列出大小与最老条目检查结果。
3. /tmp 与 /var/tmp 清理（cron 每天 03:30）。`/data/metrics/devcloud-clean-tmp.log` 每行一个 JSON。分别按 `path` 为 `/data/system-tmp` 和 `/var/tmp`，找 `ts` 晚于最近一次 03:30 的第一条 `"event": "summary"`；两根各自都必须存在，`status` 为 `ok`，`failed` 为 0，`scan` 为 `complete`。缺任一根的 summary 都算不符合。列出两根的 `skipped` 和 `skipped_reasons`。复核发现活引用或候选变化而保留的项计入 `skipped`，属于正常。`deferred_reason` 为 `execution budget` 的递延属于正常。若 `failed` 大于 0，摘录对应 `event=failed` 的路径和错误。再看 `/data/metrics/devcloud-clean-tmp.stderr.log` 的修改时间：晚于最近一次 03:30 就算不符合，并贴出它的末尾 20 行。
4. systemd-tmpfiles 不再按年龄清 /tmp。`/etc/tmpfiles.d/tmp.conf` 里 `/tmp` 那一行的第 6 列（年龄）是 `-`；`systemctl show systemd-tmpfiles-clean.service -p Result --value` 输出 `success`。
5. 两块盘的容量。`df -B1 --output=avail /data` 和 `df -B1 --output=avail /`（系统盘）的可用字节都不少于 161061273600（150 GiB）。
6. 读取 `/var/lib/devcloud-storage-upkeep/metrics/latest.json`：`schema_version` 为3，`ts` 距 NOW 不超过25小时；07:00计量任务必须已经结束，单根预算780秒、全轮840秒、任务超时900秒，应在07:30前完成。
   - 列 `top5` 的根、`daily_bytes`（按 `interval_seconds` 折算日增）、`owner`。归属按当前计量任务指向快照内的 `storage_growth_fingerprints.md` 核对，unknown 不猜测项目；父子根不相加。
   - 核 `fingerprint_coverage`：`missing_exact` 非空须报告准确路径并算不符合；`unmatched_globs` 单列观察，临时生产者当天无目录可以正常不命中。
   - 单列 thread-storage 四项的 bytes、归属与可用的日增：`.shared-cache/go-build`、`.shared-cache/golangci-lint`（两项为完整路径）、`bb:threads`（thr_*真实目录合计）、`bb:non-thread`（非线程条目，排除前两项，含共享缓存其余项与目录元数据）。父根 `/data/bb-data`、`/data/bb-data/thread-storage`、`/data/system-tmp`、`/data/code`、`/root/.cache` 只观察Top5，不重复报警；code/cache按直接子项展示，父子根不叠加；两共享Go缓存由第2项大小/年龄兜底，不参与20GiB/天告警。排除原因在 `alert_excluded`。
   - Cursor 由 Personal 的 `auto_bgngb9bbyqk`（04:22）更新后清理；核对该任务的最新 schedule 记录及 `~/.local/state/agent-cli-updates/cursor.json`。版本校验失败或清理错误算不符合；有活进程引用的版本保留。
   - 单列 `/data/root-storage-20260912/root/.local/share/cursor-agent/versions` 和 `/data/root-storage-20260912/root/.local/share/pnpm/store` 的 bytes、日增、owner 与清理任务；迁移父根和中间节点只观察。两个已登记子项也适用下述三次持续增长规则。
   - 单列 `/data/code/mono2/.worktrees`（owner=mono2 agent worktrees，清理方=linked/clone sweep；`/data/code/mono2` 总量只观察）、
     `/data/root-migrated/agent-state/cursor/projects`（owner=BB Cursor provider sessions）和
     `/data/root-migrated/agent-state/bb-deployment-checkpoints`（owner=BB deployment checkpoints）。后两项清理策略均为
     “待 owner 决定”，不自动删除；root-migrated 父根及中间目录只观察，子项按三次持续增长规则。
   - Docker四类必须有数值 bytes：`docker:images`、`docker:containers`、`docker:volumes`、`docker:build-cache`；来自Docker system df，不再用du /data/docker。分类缺失、失败、超时均不得当0。
   - `status=ok` 且带 `vanished` 的根表示du有本根合法总量、stderr全为条目消失（No such file or directory）；列诊断行数 `vanished` 与最多5条 `vanished_examples`，此情况不算不符合。分项扫描只在扫描父根附这些字段，避免重复计数；混有权限/I/O等错误、无本根合法总量或超时仍记unknown。
   - `data_top_unknown` 必须为空；非空时逐一列根、原因并算不符合。其余未测根列 `unknown_count`，按原因汇总，最多5个路径示例。`comparisons` 的真实比较要求前后都ok且有bytes；前次缺项的新指标允许0作合成基线进入Top5，但必须显示 `baseline=new`，不表示目录当天新建，且不参与告警。前次明确unknown仍不得当0。首次没有时间间隔时daily_bytes为null，不虚构日增。
   - 只对非排除项且有真实历史的指标检查 `alerts`：unknown和上述两个迁移子项的持续增长，必须最近三个独立样本（本次＋前两次）均有效、严格递增，累计 `growth_total_bytes` ≥3221225472（3 GiB），才算不符合并通知协调线程；已登记来源仍按本次和前次两个有效样本的单日 >21474836480（20 GiB）判断。检查 `history_samples` 与 `growth_status`，unknown不足三个连续有效样本应明示insufficient_samples而不判；缺项或未测不能向更早样本补凑，latest和其时间文件不能算两份。列 `insufficient_history_count`。小增长仍可列Top5。指标过期、缺失、关键分项未测或07:30时任务仍未完成也算不符合。

7. 在仓库执行 `python3 -I /data/code/mole-linux/scripts/sync_daily_check_prompt.py --check`。
   非零算不符合，报告 diff 或命令错误；每日检查不执行 `--apply`。
8. 从 1 项 Git 任务保存的命令确定当前 Git 不可变快照，执行
   `/usr/bin/python3 -I <git-snap>/daily_git_cleanup_check.py`。脚本用干净环境和固定 PATH 运行
   `bb --version`，和从运行中 bb-dev-server.service MainPID 推导的同包 CLI 版本比较；列两条
   路径、版本，找不到、执行失败或不一致均算不符合。
   - 列出三个阶段的 `summary.retention`：按实测字节及按数量的前五原因、未计量数量；同原因
     内已计量的嵌套项只计外层路径。不同原因、阶段可能重叠，不能相加当可释放空间。
   - 证据缺口（Git 登记或 gitdir 不可用、查询失败、未知引用、名称碰撞、超时等）按当前实测超过 20 GiB 告警。
     正常保护（BB 项目源/环境/未归档线程、活进程、脏树、近期活跃、未推送等）只在最近三个完整定时
     样本中同类保留字节严格递增且累计 ≥3 GiB 时告警。进程 PID 不是跨日类别；缺测、缺桶、人工或不完整
     报告不计入。告警点名阶段、原因、字节和路径。
   - linked 或 clone 在
     最近三个连续日期的完整定时执行均为零删除、且其保留路径所在的最具体计量根同期连续
     增长，也要告警，点名阶段、根路径、三日计量值和保留原因。
   - 定时执行以 BB launcher 的 systemd invocation ID 对应报告；人工或预演不算。
     旧版缺 invocation ID、缺已完成报告或缺计量样本时明示证据不足；不补凑，也不把
     缺测当零。第 1 项仍负责判断当日任务是否已完成。脚本非零时，把 JSON 内的告警或错误纳入结果。

9. BB 原生存储盘点：核对 `auto_ziaxnfa6kx4`（同项目每天 05:45）最近一次定时运行，须成功且距 NOW
   不超过 25 小时。用 `bb automation runs auto_ziaxnfa6kx4 --project proj_8d3wrcajxn --output <runId> --json`
   查 JSON 输出：列孤儿目录、缺失 checkout 的开发实例、待重试 worktree 的对象数和候选字节；
   有 `deferred`、无测量或任务失败算不符合，单列原因和可用路径；
   来源未知的开发实例保留，不计候选。零候选表示本轮不执行删除。保留原 linked 清理和 retention 策略。

## 结果

有Git本轮仍在预算内运行时，单列pending及预计硬截止时间，不宣称本轮全部完成；其余检查照常，pending本身不发异常通知。

全部已完成且符合：最后一句只写「全部符合预期」，附上 go-build 大小、/data 和 / 的可用空间，不给任何线程发消息。

有不符合的：把所有不符合的项合成一条消息发给协调线程，发完就结束，不要等回复：

bb thread tell thr_tikzbd34dg --message-file - <<'MSG'
[每日存储检查 NOW] 不符合预期：
- 第 N 项 <名称>：期望 …；实测 …；证据：<命令> → <输出摘要>
MSG
