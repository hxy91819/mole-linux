# 本机存储维护落地方案

## 当前任务（2026-11-14）

定时任务现状以 `bb automation list` / `bb automation show` 为准，下表为撰写时快照。五个脚本任务运行
`/data/tools/devcloud-storage-upkeep/releases/<16 位快照哈希>/` 下的不可变副本，文档不写死版本号；
/tmp 与 /var/tmp 清理使用独立快照 `cbb5caf329a61b57`。

| 任务 | 时间 | 入口 | 范围 | 状态 |
| --- | --- | --- | --- | --- |
| 存储：Docker 残留（每小时） | 每小时第 7 分钟 | BB 脚本任务：`docker_leftovers.py --execute --quiet`（storage-upkeep 快照） | 满 1 小时的匿名测试 DB 卷、满 24 小时的悬空镜像、满 72 小时的 BuildKit 缓存；持 fork-maintenance run.lock 非阻塞 flock，拿不到即跳过，删卷前排除 retained_volumes | 启用 |
| 存储：worktree 清理（每天） | 每天 06:30 | BB 脚本任务：`worktree_sweep.py --execute --quiet --budget-minutes 12`（storage-upkeep 快照） | 无人用且闲置满 24 小时的附加 worktree：已在远端直接删，否则归档核验后删；每轮补删路径已不存在的 BB 环境，live-thread 拒绝只记账不重试、不报失败 | 启用 |
| 存储：Go 缓存修剪（每小时） | 每小时第 37 分钟 | BB 脚本任务：`go_cache_trim.py --execute --quiet`（storage-upkeep 快照） | Go build 与 golangci-lint 里 24 小时未用的哈希普通文件；根取 realpath、分别核固定 README；共享 .shared-cache 仅由此脚本修剪 | 启用 |
| 存储：/tmp 独立 clone 清理（每天） | 每天 06:45 | BB 脚本任务 `auto_3jiqtmzreq8`：`tmp_clone_sweep.py --execute --quiet --budget-minutes 12`（storage-upkeep 快照） | 两 tmp 根顶层独立 clone，无占用且闲置 24h、干净无 stash/嵌套/关联 worktree、无外部引用及 ignored 凭据；本地 ref 均远端可恢复才删，缺失先精确归档；逐项 JSONL | 启用 |
| 存储：pnpm store 每周 prune | 周日 05:30 | BB 脚本任务 `auto_yfr5p14dvnc`：`pnpm_store_prune.py --execute --quiet`（storage-upkeep 快照） | 仅 pnpm store 写入子命令或探针 unknown 阻塞；npm/npx/Vite、pnpm run/exec 不阻塞。v3/pnpm9、v10/pnpm10、v11/pnpm12 均显式用同一 store 根；不可得版本跳过记账 | 启用 |
| 存储：每日 DSH 检查 | 每天 07:30 | BB agent 任务 `auto_5gacevqssoe`（acp-dsh，DeepSeek V4.1 Flash），只读 | 核对五个脚本任务的定时运行、Go 缓存修剪、03:30 的 /tmp 与 /var/tmp 清理、systemd-tmpfiles 和 / 与 /data 的可用空间（都 ≥150 GiB）；全部符合不出声，不符合时合成一条消息发给协调线程 thr_tikzbd34dg。协调线程换了要同步改这个任务的 prompt | 启用 |
| /tmp 与 /var/tmp 清理 | 每天 03:30 | crontab `# devcloud-clean-tmp`：`clean-tmp --path <固定根> --execute --age-days 3 --json`（外层 timeout 1790 秒，先 /var/tmp：预算 300/timeout 325 秒，再 /data/system-tmp：预算 1400/timeout 1435 秒，kill-after 10 秒） | 两根普通项整树闲置 3 天；按结构识别的 Go/pnpm/Node 缓存满 24h。128 项一批完整 /proc 快照、8 项并发；普通树 5 秒、缓存树 60 秒，不限数量，余项递延；跳过 systemd-private-* | 启用 |

pnpm 任务工作目录为 `/data/code/mole-linux`，使用已安装 pnpm 的 Node 24.22.1 PATH；先用 `pnpm store path` 取共同根，
逐版本工具从中立目录运行 `--store-dir <共同根>`，再核 store path；不得把版本目录本身作为根。
当前共同根 `/data/.pnpm-store` 的实测兼容选择为 9→v3、10→v10、12→v11，工具不可得只跳过该版本并记原因。

`/root/.cache/go-build`、`/root/.cache/golangci-lint` 已由 lightspace 指向
`/data/bb-data/thread-storage/.shared-cache/` 中的共享缓存。该目录不是线程遗留，不得删除或误报；
只能由 go_cache_trim 按 24h 修剪。README 首行分别为 Go build system 与 golangci-lint 的固定标记，哈希文件规则见 storage-rules。

2026-11-05 owner 决定，存储自动化统一使用上表这套配置。已删除：`auto_aburimdwdjg`（每周系统盘容量复盘，系统盘容量改由每日 DSH 检查覆盖）、`auto_caovoqtwj9e`（每周 DB 存储巡检与安全清理）、`auto_cvheqhfkaf4`（空间清理独立检查，原已暂停），以及项目 proj_bdg8tywu4a 的 `auto_o3pwesztbpg`（fork-maintenance 09:00 闲置工作区清理）。删除前的完整配置存档在 `/root/.local/state/devcloud-storage-upkeep/retired-automations-20261105.json`。下列 systemd 定时器不属于这套存储维护，继续保留：`safe-system-cleanup.timer`（周日 05:00 清 DNF 缓存和归档 journal）、`clean-openclaw-residue.timer`（每小时清 OpenClaw 卸载残留）、`html-preview-gc.timer` 和 `codebuddy-logrotate.timer`（生产端自带的过期清理与日志轮转）。

systemd-tmpfiles 自 2026-11-04 起不再清理 `/tmp`（`/etc/tmpfiles.d/tmp.conf` 为 `q /tmp 0700 root root -`）：
原来的按年龄删除曾删掉闲置 worktree 里已跟踪的文件，遇到孤立 FIFO 还会卡死。阈值与授权的规则源：
[storage-rules](../.claude/skills/devcloud-patrol/references/storage-rules.md)；手工入口：
[快速释放空间手册](DEVCLOUD_SPACE_RECOVERY.md)。

**历史**：2026-09 的设计采用两部分：**一次性确定写入位置并验收；日常复用已有每日清理、每周复盘。** 迁移不进定时任务。本文替代此前的小时巡检、增长预测与通用策略框架方案。

手工处理空间告急时，先读 [当前服务器快速释放空间手册](DEVCLOUD_SPACE_RECOVERY.md)：包含现有入口、保护范围、3 天闲置检查、worktree 归档恢复、Docker/工具缓存命令和失败处理。它不增加自动删除权限，也不把历史候选清单当成新的执行清单。

**历史**：2026-11-03 最新监督配置见 [本会话的空间清理监督与干预](WORKTREE_CLEANUP_REVIEW.md)：沿用原复盘任务并绑定当前会话 `thr_rd3ahpzrae`，按使用率≤70%且普通可用空间至少30%验收；程序或最多一个复用DSH执行，当前会话独立检查；每轮必须复盘和自我迭代；未达标每2小时主动干预，达标且稳定后才降频，每轮报告补齐的规则、测试、部署和效果。此配置取代下文11-02复盘任务的初始频率与另开会话方式，原09:00清理仍独立运行。

**历史**：2026-11-02 新增 [worktree 清理效果复盘](WORKTREE_CLEANUP_REVIEW.md)：每日 10:15 核查 09:00 清理的真实终态、覆盖及回收，按有效性逐步降到每周三次、每周一次，退化恢复每日。现役清理入口已被安全维护流程替换为 `/data/code/fork-maintenance/scheduled.py idle-cleanup`（11-01 曾改为附加 worktree 30 天；owner 11-02 明确要求恢复连续闲置 3 天/72 小时，现役扫描与执行复查统一采用 3 天），下文 10-29 的工具入口仍是历史状态。复盘保存逐轮证据并跟进差异，不新建第二个删除器。

**历史**：2026-10-29 复查：独立 worktree 自动化实际使用 `--days 3`；通用 tmp 工具仍要求至少 7 天，默认策略阶段仍可能超时。本轮一次性延长检查预算后完成预览和 50 项执行，并非新的定时版本已经上线。下文保留原部署历史和边界，不能据“2026-09-17 修复”推断今天一定能跑完。

**历史**：状态：迁移与 20 项实际写入验收已完成；清理器安全修复、固定版本部署、每日 cron、日志轮转和每周 BB 提示词已上线。2026-09-12 已按 cron 环境手动验收：109 秒成功处理 50 项，候选块大小共 21.7 MiB，零失败、无隔离遗留。首次定时触发为 2026-09-13，真实触发结果仍需由后续日志确认。

**历史**：2026-09-17 修复：09-14～09-17 连续四天 `status=failed, scan=incomplete, deleted=0`。根因是策略阶段逐候选串行调用 `validate_path_for_deletion`（约 25 ms/次）与 `should_protect_path`（约 17 ms/次），`/data/system-tmp` 增长到 6.7k 条目后单进程撞上硬编码的 30 秒子进程上限，整轮 fail-closed。修法是把同一策略脚本**分片并行**（16 片，逐项语义不变、候选集不预截断），并把子进程预算改为 `POLICY_SECONDS`；新快照 `84ffa318d2107e59` 实测 preview 14 s、`--execute` 150 s 处理 50 项 / 3.17 GB、零失败。

## 1. 写入位置一次性做好

对当前常用工具，先读取工具实际使用的路径：npm cache、pnpm store、pip cache、Go GOCACHE/GOMODCACHE，以及临时目录。已有正确配置就沿用，不再叠加一组全局环境变量。

需要迁移的目录在一次维护中处理：核查活动引用 → 复制并校验 → 保留原路径入口 → 验证读写 → 记录恢复方式。对于正在使用的运行环境或数据库，单列一次性维护项，在可停写、可验证的维护窗口处理；不能记为已完成，也不能交给定时任务迁移。

一次性验收包含：

- 原路径能访问，逻辑路径和物理目录的映射正确，权限保持。
- 在原入口或工具配置解析出的目录中实际创建、写入、同步、删除专属测试文件，验证文件设备属于数据盘。配置目录尚未创建时验证其现存祖先，并明确标注验证范围。
- npm/pnpm 等可能受项目配置影响的工具，同时核查用户目录与实际项目的路径。
- 验证主要工具可运行，数据盘有开机挂载配置，记录迁移映射和恢复步骤。

这些入口今后的正常新增写入会直接落到数据盘，无需周期性搬运。此验收覆盖当前配置，不承诺所有未来软件版本都维持相同目录布局；升级若改变布局，当作一次具体兼容性问题修复，不自动覆盖新目录。

本机验收、保留项与映射记录在 `storage-audit.log`、`/data/metrics/root-space-20260912/write-routing-validation.json` 和 `/data/root-storage-20260912/manifest.json`。数据盘目标是正式存储，备份要覆盖目标内容，不能按日期目录清理。

## 2. 历史：2026-09 设计时的两个日常任务（现行任务见文首表格）

| 任务 | 固定职责 | 不承担的动作 |
|---|---|---|
| 每日 03:30，现有 cron | 通过修好的执行器清理已授权的临时产物；记录系统盘和数据盘可用空间、候选数、结果与失败原因 | 不迁移、不扫描整机后猜删、不自动清数据库或工具版本 |
| 每周日 04:00，现有 BB 任务 | 比较上次记录，必要时对系统盘做有时间限制的目录测量；说明增长最多的目录及处理建议 | 不自行拼删除命令，不搬会话或工具目录，不维护另一套临时清理规则 |

首次落地只用系统盘实际占用 **70%** 作为报告线，同时显示可用 GiB。任务失败或扫描不完整也要报告；正常结果保持简短。两次目录测量的范围和完整性一致才比较增量，不维护小时级预测或另一套常驻服务。

/tmp 实际位于数据盘，清理它不能给系统盘腾空间。系统盘增长若来自必须保留的数据，周任务应指出具体原因及一次性维护建议，而非扩大自动删除范围。Docker 数据库继续由单独的维护工作负责。

**历史**：Worktree 由另一个已有任务负责：每日 09:00，入口 `/data/code/worktree-cleanup`，状态在 `/data/code/.worktree-cleanup/state/`。截至 2026-10-29，已保存的执行脚本明确传入 `--days 3`；通用 CLI/README 的默认 7 天不能代替这台机器的实际调度参数。具体 scan/apply 范围和远端核验见快速手册；本段不新建或修改定时任务。

## 3. 已部署的清理器

`bin/devcloud.py clean-tmp` 路由到内部 `bin/devcloud_tmp.py`。参数与原入口兼容，默认只预览；执行边界为：

- 只接受 `/data/system-tmp`、验证过的 `/tmp` bind 别名和固定系统盘根 `/var/tmp`。数据盘须正确挂载；根及祖先须 root 所有且不可被其他用户写入，唯 `/var/tmp` 根允许 sticky 写权限且不得是挂载点；候选仍不可被其他用户写入。始终跳过 `systemd-private-*`（及已有 `systemd-*`）。systemd-tmpfiles 的 `/var/tmp` 30 天规则继续被覆盖，不恢复逐文件年龄清理。
- 每批最多 128 项共用完整 /proc PID/starttime/引用快照，最多 8 项并发；检查 cwd/exe/fd/maps、绝对参数、mount namespace、inotify，未知整批拒绝。隔离后及 Mole 最终 sink 只增量检查新增/starttime 变化的 PID（既有 PID 可变引用不重扫，owner 明确授权），新增探针 unknown 仍拒绝。
- 父目录旧但内部有新文件时保留；不完整或超时的扫描不产生可执行候选。
- 预览与执行都检查 Mole 保护名单和用户 whitelist，覆盖原路径及 `/tmp` 别名。保留仓库、凭据、数据库、备份、符号链接、特殊文件、共享硬链接和可被其他用户修改的树。
- 每次完整扫描最多 240 秒，单普通树 5 秒、结构缓存树 60 秒；普通项保留 3 天，按树内最新文件 mtime 判断的结构缓存保留 24h。不设数量上限；/var/tmp 内预算 300 秒、timeout 325 秒，/data/system-tmp 内预算 1400 秒、timeout 1435 秒，外层 timeout 1790 秒兜底，余项递延。读取 /proc 证据时进程正好退出的，对同一 PID 最多重查 3 次，证据仍不全才拒绝。
- 单实例锁覆盖扫描和删除。候选先移到源文件系统的私有隔离区（/tmp 用 `/data/metrics/devcloud-maintenance/`，/var/tmp 用 `/var/lib/devcloud-maintenance/`），再检查实际对象的引用和完整树指纹，通过 Mole dry-run 及最终删除边界复查后交给 `mole_delete`。每项重核结构、年龄、物理身份和完整指纹；失败尝试恢复原名，原名被占用时保留隔离原件并报告位置。取消停止排队、终止 Mole 子进程组并恢复在途项；遗留隔离项阻止下一轮。
- 退出 0 表示完整成功/预览，1 表示部分执行失败，2 表示拒绝或证据未知；每根执行只写一条最终 summary，以 `path` 区分，保留独立 intent/deleted 事件及 Mole 操作日志。

本次部署：`/data/tools/devcloud-maintenance/releases/cbb5caf329a61b57/`（2026-11-14 批量探针及结构缓存；上一版 `7ca528d7c5f3f004`、 `df3e2b353d8e1c72` 及 `f1e330c325c73fd0` 与 `1c2d71d3ab21cc79` 保留作回滚），包含执行器及配套 Mole `lib/`，全部文件均有 SHA-256 清单；快照目录名即清单内容的 sha256 前 16 位，可由 `manifest.json` 复算。cron 直接引用该版本和系统 `/usr/bin/python3 -B`，每日 DSH 检查读取两根 summary，不执行共享工作区或可变 current 链接。后续升级需新建快照、验证后更新 cron 版本路径并回读 DSH 检查范围；不自动拉取源码。

```bash
/usr/bin/python3 -B /data/tools/devcloud-maintenance/releases/cbb5caf329a61b57/bin/devcloud.py clean-tmp --path /var/tmp --age-days 3 --json
/usr/bin/python3 -B /data/tools/devcloud-maintenance/releases/cbb5caf329a61b57/bin/devcloud.py clean-tmp --path /data/system-tmp --age-days 3 --json
# 已授权的手工执行：同一命令增加 --execute
/usr/bin/python3 -B -m unittest discover -s tests -p test_devcloud_cleanup.py -v
```

每日 JSONL：`/data/metrics/devcloud-clean-tmp.log`；启动/异常 stderr：`/data/metrics/devcloud-clean-tmp.stderr.log`；锁和待恢复原件：`/data/metrics/devcloud-maintenance/` 与 `/var/lib/devcloud-maintenance/`。summary 按 `path` 分根，增记 cache_roots（路径、类型、allocated bytes、最新文件 mtime）及 deleted_bytes，包含两盘前后实际使用率、可用字节、候选数量/字节、删除数、跳过原因、失败原因、耗时和系统盘 70% 报告标记。`/tmp` 删除字节属于数据盘，`/var/tmp` 属于系统盘；不能把并发造成的 df 差值全算作回收量。每日 DSH 检查第 3 项分别核对两根最近一次 03:30 后的 summary，均需 complete/ok、failed=0。

已有 `logrotate.timer` 每日检查两个日志，配置为每日轮转、检查时超过 5 MiB 提前轮转、保留 14 份/最长 30 天，压缩旧文件；不截断活跃日志。周任务读取 summary 和轮转日志，核查失败/漏跑并输出容量复盘；必要时只预览同一清理器，已撤掉独立的 14 天删除规则。

23 项自建夹具回归及真实 Mole 删除夹具通过；关闭 whitelist 加载后，两项对应回归会失败。完整仓库测试退出 1，已有测试依赖 `/var/root` 等 macOS 路径及 BSD `stat`，在本机 Linux 上失败；格式脚本因缺少 shfmt 未运行完成。部署的全部 shell 文件通过 `bash -n`。完整输出和本次实际执行证据见 `storage-audit.log`、`/data/metrics/devcloud-adjust-20260912/`。`who-uses` 已复用同一有界进程探针：退出 0/1/2 分别为未发现引用/有引用/未知；输出仅显示进程名，单独结果不授权删除。

## 4. 模型只处理需要上下文的判断

模型负责解释“哪个项目持续制造残留”“旧构建是否仍有消费者”“应修哪个测试流程”。脚本负责确定路径、核查使用状态和执行已授权操作。

第一版自动删除范围维持现有临时产物。以后只有出现明确收益、且可以验证 owner 清理方式的新类别，才单独补一条固定规则。一次性允许清理某个旧版本不等于长期自动删除许可；会话、凭据、工作区、活跃数据库和工具链仍不自动删除。

上线验收以真实运行结果为准：一次性写入检查通过；每日任务按时运行，失败能看见；周任务能解释增长且不越过既有清理范围。不要仅凭调度记录里的一个 succeeded 字段认定定期维护已经可靠。
