# DevCloud 存储清理规则

本机存储调查沉淀的持久规则，按目标分组，是磁盘侧分类与清理的唯一规则源。
新规则先写进当轮 `storage-audit.log`，跨运行成立并通过 AGENTS.md DevCloud
Audit Ledger 的五问后晋升到这里；每条晋升规则要附回归检查，或写明为何只能
做活体只读探针。

## 本机可再生残留的长期授权

- **owner 2026-11-03**：「所有不是近期在用、可恢复的东西都能清理。」按本机现有年龄及引用判据，
  自动处理须同时满足：非近期在用（至少 72 小时，完整检查待清对象与相关活动）以及恢复性已验证
  （可重新下载、重新生成，或按已有 worktree 规则完成远端归档）。查询不明、扫描不完整或恢复校验失败时保留。
  授权长期有效，在范围内自主 preview、执行、逐对象记录，不重复请求批准。
- 仍保护活跃/排队任务、共享引用、会话、凭据、唯一数据、主 checkout、数据库及明确 retained/备份。
  活跃的 Go/npm/pnpm 混合缓存不能因为部分对象旧就整库清空；对象的恢复性不等于整个父目录都可删。
  服务停启和未知数据处置不从本授权推导。
- **2026-11-03 执行纠偏（对 go-build/npm/pnpm/Docker 缓存已由下节 owner 2026-11-04 策略取代，留作历史）**：owner支持reset/prune、命令在手册中或数据可再生，
  均不豁免上述72小时、恢复与引用条件。`go clean`、npm内部`cacache.rm.all`、`pnpm store prune`、
  `uv cache clean`同样是删除入口，不能在受测计划之外直接调用。当天5项缓存动作缺完整年龄/预览/锁/末端
  引用证据，按安全验收失败保留原始回执和更正；实测容量回升不记为合规执行。证据：
  `/root/.local/state/worktree-cleanup-review/runs/single-dsh-recovery-20261103T133406/correction-appendix.json`。
- 操作使用 owner 原生命令，或已确认可再生的精确路径经 Mole preview、保护规则及末端身份/年龄/
  引用复查。引用覆盖全部容器状态、BB、cwd/exe/fd/maps/argv、inotify 和挂载别名；未知拒绝。
  远端下载副本须核对固定制品及摘要；错误摘要不视为“还能下载所以可删”。
- 注册表镜像适用本授权时：全部容器均无引用、无保护声明、镜像与本机 image metadata 均超过
  72 小时，且固定 registry digest 的 manifest 当轮可获取；逐 tag 复核 ID 后无 force 删除。
  Created 不是 last-used，报告只写实际取得的年龄/引用证据。没有 registry 证明的本地产物仍按既有
  精确项目规则和恢复证据处理，不推广通配删除；卷不套用本条。
- 活体验证：2026-11-03 依赖/缓存经 Mole 最终 guard 后删除；8 个 registry-backed 未引用镜像
  删除后运行容器 ID/StartedAt/镜像均不变；3 个 registry 查询失败的候选保留。固定安装包摘要不符
  被保留。证据在 `/root/.local/state/space-recovery-20261103/`，后续每轮重新检查，不复用删除计划。

## owner 2026-11-04 存储维护策略（与旧条款冲突时以本节为准）

owner 在 `thr_tikzbd34dg` 看过增长诊断后确认：紧急释放「可以」；worktree「可以，只在本地的，如果是我的
fork或者我的个人项目，推送上去，以分支形式存在不就行了。我觉得worktree，除了当下正在用得，没啥不能推送
上去的吧」；长期做法写成 `$devcloud-storage-upkeep` 技能。**底线（owner 2026-11-04 原话）**：
「如果是正在用的业务和 Agent 一定不要影响他们使用。这个是底线，我们要清理的那些是没有正在用的。」
按恢复契约分三类：

- **go-build 每小时只删 24 小时以上没用过的条目；npm、pnpm 不自动清（2026-11-04 建立，2026-11-14 owner 批准由 2 小时改为 24 小时）**：owner 原话：
  「我觉得有意义啊，可以考虑加上，而且落地为仓库的规则。不然这个点确实会很坑。」原因是缓存键含源码目录：
  lightspace 每个任务都在 `/var/tmp` 新建一份源码副本，各 worktree 路径也不同，旧路径的条目再也不会命中，
  而 Go 自己要等 5 天才删。依据：Go 用到一个条目时，若其 mtime 已超过 1 小时，就刷新为当前时间
  （`cmd/go/internal/cache` 的 `mtimeInterval`）。2026-11-14 owner 批准将修剪年龄从 2 小时调整为
  24 小时（每日检查上限同步从 60 GiB 调为 100 GiB、最老条目阈值改为 1520 分钟），保留日内与隔夜复用的缓存条目，
  同时清掉超过 24 小时未用的冷条目。只删名字匹配 `^[0-9a-f]{64}-[ad]$` 的普通文件；目录、README、trim.txt、
  testexpire.txt、fuzz 和符号链接不动。脚本 `scripts/go_cache_trim.py`，默认预演、`--execute` 才删。
  npm `_cacache` 与 pnpm store 仍不自动清。原 30/10/20 GiB 整体重置作废。回归：
  `test_old_go_cache_entries_are_deleted`、`test_recent_go_cache_entries_are_kept`、
  `test_non_matching_go_cache_names_are_kept`、`test_go_cache_symlink_is_kept_and_target_is_untouched`、
  `test_missing_go_cache_readme_deletes_nothing_and_exits_1`、`test_go_cache_dry_run_deletes_nothing`、
  `test_go_cache_quiet_is_silent_when_nothing_is_deleted`。
- **Docker 残留由 `docker_leftovers.py` 清（每小时定时任务）**，清理满 1 小时的匿名测试 DB 卷、
  满 24 小时的悬空镜像，以及下述带标签镜像和 BuildKit 缓存（2026-11-14 owner 批准扩展）。执行时在 `/root/.local/share/fork-maintenance/state/run.lock`
  上非阻塞 flock，拿不到锁本轮跳过；删卷前先排除
  `/data/metrics/ai-storage-maintenance/local-data-modes.json` 中 `retained_volumes` 列出的卷。
  匿名测试 DB 卷的证据链见下一条。
- **2026-11-14 owner 批准：带标签镜像**同时满足以下条件才删：没有任何容器（含 stopped、created）
  引用 image ID、Created 严格超过 30×24h、同一 repository 有 Created 严格更新的镜像。
  每个 repository 的最新镜像（含并列最新）始终保留；`mole.protect=true` / `devcloud.protect=true`
  一律保留，悬空镜像 prune 也排除这两个标签。删除前重新检查全部容器引用、标签指向、保护标签和更新镜像证据；
  更新镜像证据明确选仓库最新，不依赖 Docker 列表顺序；单标签复查失败保留并报错，继续报告其余标签。
  查询失败最终 exit 1。只用 `docker image rm <repo:tag>`，不加 force。报告的 image_size_bytes
  是含共享层的镜像大小，不是独占可回收字节。
- **2026-11-14 owner 批准：BuildKit 总量最多 10GB**：保留现有
  `docker builder prune -f --filter until=72h`，另跑不带年龄过滤的总量 prune；先查询本机
  `docker builder prune --help`，支持时用 `--max-used-space 10GB`，否则用 `--keep-storage 10GB`。
  新 buildx 客户端可能列出 `--max-used-space`，但旧 BuildKit 服务端拒绝；仅当返回明确的
  `buildkit v0.17.0+ is required for max-used-space and min-free-space filters`（执行前能力检查）时，
  记录该诊断并改用兼容的 `--keep-storage 10GB`。两者都不支持、查询失败或真实 prune 失败则 exit 1，
  不对其他错误重试。Docker 自身锁定、不可回收的缓存仍由 Docker 保留。
- **匿名测试 DB 卷满 1 小时即清**：无任何容器引用、名称为 64 位十六进制且只带
  `com.docker.volume.anonymous` 标签、内容是 MySQL/PostgreSQL 数据目录或为空，创建满 1 小时即
  `docker volume rm <id>`。容器已删的匿名卷没有任何程序还能挂回去；命名卷、compose 卷与 retained 登记照旧。
- **Worktree 没人用且闲置满 24 小时即处理**：没有进程（cwd/exe/fd/maps）、容器挂载或忙碌/24 小时内更新的
  BB 线程引用。HEAD 在实时远端分支上且干净的直接 `git worktree remove`；否则把 HEAD 或 WIP 快照推到
  `archive/worktree/*`，`ls-remote` 核验后删除。推送目的地：同主机下你的命名空间（hxy91819、masonxhuang，
  以及 owner 2026-11-04 确认「算」自己项目的 opencraft-app 组织）优先，内部仓库只推 git.woa.com 上的 origin；
  公共上游且无个人 fork 的保留待定。opencraft：owner 2026-11-04 原话「只要你不重写历史都是可以的」——
  归档可以推到公开仓库 `opencraft-app/opencraft`，开源前的旧历史也可以；绝不 force push，也不改写远端
  已有分支。ignored 文件仍随目录删除、不推送，但新增三条保留条件：① 有被忽略的凭据类文件（匹配
  `SECRET_NAME`；跳过 `node_modules`、含 `pyvenv.cfg` 的目录和符号链接）而主 checkout 同一路径没有
  内容相同的文件 → 整个 worktree 保留；② 归档目标远端在 `ls-remote` 时就失败 → 保留；③ 读取 `/proc`
  出现进程退出以外的错误，或 `docker ps` / `docker inspect` 失败 → 本轮全部保留。仍保留：主 checkout、
  locked、进行中的 git 操作、已填充子模块，以及 WIP 含凭据文件名或 >20 MiB 文件的 worktree。
- **2026-11-14 owner 批准：worktree 删除时同步处理 BB 环境**：`worktree_sweep.py --execute`
  每次成功删除一个 worktree 后，在既有 JSONL 日志记录恢复命令
  `git fetch <url> <ref> && git worktree add <path> <sha>`，并按 `path` 精确全等筛选
  `bb environment list --json` 中 `status != "destroyed"` 的 BB 环境（父目录、子目录、主 checkout
  概不匹配）。经核查 BB `environment-git-worktree` 插件源码（`removeWorktree` 在目标目录已不存在时只清理
  `.completed` 标记并返回，绝不删除任何 Git 分支或 ref），逐个调用 `bb environment delete <id> --json`；
  若因仍有未归档线程（live threads）等被 BB 拒绝，保留原环境、记录拒绝原因，不重试。若 `bb` 不可用或查询/删除命令失败，
  fail open：worktree 照常删除，在 JSONL 记录失败信息而不阻塞清理。回归：
  `test_matching_bb_environment_is_deleted_with_recovery_command_after_worktree_removal`、
  `test_non_exact_bb_environment_paths_are_untouched`、
  `test_refused_bb_environment_delete_is_logged_and_not_retried`、
  `test_bb_failure_does_not_block_worktree_deletion_and_logs_error`。
- **数据类不自动删**：会话（codex、bb-data）、凭据、在用数据库、命名卷、备份与迁移副本只列给 owner 决定。

执行入口：`$devcloud-storage-upkeep` 的 `scripts/worktree_sweep.py`、`scripts/docker_leftovers.py`、
`scripts/go_cache_trim.py`，默认预演、`--execute` 执行。回归：`tests/test_devcloud_storage_upkeep.py`（已合并删除、仅本地提交归档、WIP 快照、
凭据文件保留、进程占用保留、近期活动保留、凭据类 ignored 文件与主 checkout 不同内容则整树保留、
node_modules/虚拟环境内凭据不阻塞、ls-remote 失败保留、/proc 非退出错误整轮保留、docker ps/inspect
失败整轮保留、删除后同步处理精确匹配 BB 环境与拒绝/故障 fail-open、busy 锁跳过、retained_volumes 排除；go_cache_trim：`test_old_go_cache_entries_are_deleted`、
`test_recent_go_cache_entries_are_kept`、`test_non_matching_go_cache_names_are_kept`、
`test_go_cache_symlink_is_kept_and_target_is_untouched`、
`test_missing_go_cache_readme_deletes_nothing_and_exits_1`、`test_go_cache_dry_run_deletes_nothing`、
`test_go_cache_quiet_is_silent_when_nothing_is_deleted`）。定时任务现状以 `bb automation list` 为准。

## 盘点的计量与只读要求

- 同一对象跨扫描批次出现时，先按路径/物理身份合并；单独运行的`du`不跨调用去重硬链接，
  路径实占相加不能称文件系统物理下界或独占可回收量。登记worktree按目标device区分主checkout、
  附加worktree及异盘，再与output/ops/tmp父scope去重。未完成扫描是unknown，不是recent；
  已闭合删除证明为0不表示实际可回收量为0。未来可处理时间只从本轮精确对象证据推导，历史批次日期不复用。
- 只读Git盘点复用owner helper，或设置`GIT_OPTIONAL_LOCKS=0`/`git --no-optional-locks`；
  普通`git status`即使输出为空，也可能刷新stat cache及index mtime，制造虚假近期活动。
  2026-11-03父fixture已验证默认status刷时间、关闭optional locks不刷；生产index缺before快照时影响记unknown，
  不回改mtime制造闲置。当前`core.command`和`maintenance.run`已有此设置，临时盘点脚本也必须沿用。
  计量重复与只读Git复现证据在`/root/.local/state/worktree-cleanup-review/runs/single-dsh-inventory-20261103T143000/parent-review/`。

## Docker

- `overlay2` 不是按路径的清理目标：它对挂载层双重计数。只用 Docker 自己的
  metadata/owner 命令，绝不递归删除其目录。
- Dangling image 先分类容器引用再谈清理：仍有引用时一揽子批准不安全；虚拟
  大小共享层，不能当可回收字节估计。
- **owner 授权（2026-09-17 第二轮）：dangling 镜像按年龄自动清理。** 判定：`docker images -f dangling=true`
  且 Created 超过 **14×24h**（比卷的 7 天更长），并排除任何容器（含 stopped）在用的镜像 ID
  （实例：itfs 的 `9d9722601084`）。只用精确 `docker image rm <id>` 分批执行，禁止无过滤
  `docker image prune` / `system prune`。执行后核对 `docker images -f dangling=true -q | wc -l` 与
  `docker ps -a` 无重启。2026-09-17 首次执行 47/47 成功、0 失败。
- **构建缓存按“不活跃时间”自动清理**：判据为 BuildKit 记录 `Last used` 超过 14 天；唯一 owner 命令
  `docker buildx prune -f --filter until=336h`（buildx 的 `until` 按 last-accessed 过滤），执行前先跑
  `docker buildx du --verbose` 输出创建/最后使用/大小。2026-09-17 实测 121 条记录全部落在 14 天活跃
  窗口内，仅回收 130B——**这是正常结论，不得为凑数放宽门槛或改用创建年龄**。
  **2026-11-04 执行纠偏（buildx-serial-owner-contract）**：该 owner 命令同样属于删除入口，
  必须遵守本监督已有的 owner 总锁/仓库锁串行、fresh 完整资格及末端证据要求；
  不因命令有14天过滤就豁免。16:10直调未持锁，回执Total 0B，控制验收不合格；
  17.15GB du reclaimable不得记为收益。该直调路径停止使用，修复并验证受测入口前保持hold，
  不重跑原命令补造锁证据。原始日志与纠正在监督
  `runs/manual-available70-20261104T160000/executor/raw/` 和父报告中。
- **owner 授权（2026-09-17 第三轮）：可重建的 Go 缓存命名卷按不活跃时间清理。** 判定须全部满足：
  ① 命名卷且无任何容器（含 stopped）引用；② 无 compose 标签；③ 内容可验证为 Go 构建缓存
  （≥16 个十六进制桶目录）或 Go module 缓存（`cache/download`）；④ **不活跃** = now − 树内最新文件
  mtime > 14 天；⑤ 树扫描在超时内完成，扫不完 fail closed 不删。删除用 `docker volume rm <name>`。
  执行器 `auto_clean.py`（`--cache-idle-days`，默认 14）。2026-09-17 实测 6 个卷不活跃 12.9 天，
  09-18 约 13:00 到期后自动删除；12 天门槛预演命中同一组 6 个卷 / 2.26GB。
- **不活跃时间必须按“树内最新文件 mtime”计算，不能用目录自身 mtime**：Go 构建缓存顶层目录只在新建
  桶目录时变化，用顶层 mtime 会把仍被使用的缓存误判为不活跃。回归检查：同一卷
  `find <mp> -type f -printf '%T@\n' | sort -rn | head -1` 与脚本 `idle_days` 一致。
- **`docker system df`（含 `-v`）在本机稳定约 29s**（daemon 端开销，2026-09-17 实测 29.0/28.9/29.1s）：
  每轮最多各取一次并落 `raw/.df_cache.json` 复用，禁止在循环里重复调用；测量回收量用 `df -h /data`。
  回归检查：单轮所有脚本对 `docker system df` 的调用 ≤2 次。
- 容器清理以标签为准：`mole.protect=true` / `devcloud.protect=true` 是显式
  保留标记；stopped、test-like 命名、无标签但在运行，都只是审查分类。
  Docker 不支持原地加标签——在 owning Compose 文件里加，且仅在服务 owner
  批准中断后重建。

- **owner 授权（2026-10-27）：已登记的带标签测试/验证镜像按年龄自动清理。**
  用户批准原文：“带标签的测试或验证镜像，只要没有容器引用、建好超过 14 天、同一仓库有更新的标签，
  就自动删除，每个仓库保留最新一个”；用户确认原话：“可以。”
  “测试/验证镜像”只按登记名单认定：目前仅精确 repository `lighthouse-workflow-agent`，不按名称相似推断。
  `dsh-*`、`tkc-*`、`hermes-m1a-*` 等其他候选仓库未登记，巡检报告逐项列为“需决策”，不得套用本条。
  自动删除须同时满足：① 有标签且属于已登记 repository；② image Created 超过 **14×24h**；
  ③ 无任何容器（包括 running、stopped、created）引用该 image ID；④ 同一 repository 有 Created
  更晚、实际保留的标签/镜像。每个 repository 保留最新一个；镜像不记录 last-used，不用年龄冒充不活跃时间。
  既有 owner 保留/保护声明优先。先落精确 tag→ID、创建时间、全部容器引用及最新保留项清单；删除前重查，
  标签指向变化、引用查询失败或不满足条件时保留。只用 `docker image rm <repository:tag>`，不加 force，
  不用全局 prune。原执行器“DB 存储巡检与安全清理”（`auto_caovoqtwj9e`）已于 2026-11-05 退役，本条目前没有定时执行器，登记仓库出现旧标签时按本条手动执行；
  本条不授权删除已停止测试容器，也不改变任何卷规则。
  回归检查：只读预览中，登记仓库的超过 14 天旧标签且有更新保留项时才入候选；最新标签、任何容器引用、
  保护声明、未登记仓库及证据查询失败均不入候选；执行后核对最新保留项仍在、运行容器 ID/StartedAt/镜像/挂载
  不变、容器和卷未删除。

## 数据库卷

- **旧 worktree 调度（`auto_o3pwesztbpg` 已于 2026-11-05 退役删除，下文仅作历史；现行规则见上文 owner 2026-11-04 策略，执行入口 `worktree_sweep.py`）**：附加 worktree 连续闲置满
  **3 天（72 小时）**才可归档并核验后清理，替代 11-01 引入的 30 天；扫描与删除前复查
  使用同一门槛。`auto_o3pwesztbpg` 每日 09:00（Asia/Shanghai）调用
  `/data/code/fork-maintenance/scheduled.py idle-cleanup`，默认扫描 `/data/code`。
  最新任务与报告在 `/root/.local/share/fork-maintenance/state/`，恢复信息在
  `archive-index.jsonl`；旧 `/data/code/.worktree-cleanup/state/` 仅作旧执行器历史。
  活跃/排队会话、进程与共享依赖、未知 ignored 数据继续保护；主 checkout 不自动删除。
  回归：`tests/test_idle_cleanup.py` 核验不足 72 小时、恰满 72 小时及 4 天的扫描边界，
  不足门槛执行不归档/删依赖，恰满门槛远端保存后移除；未知 `.env`、近期开发活动、
  推送失败及共享依赖的原有测试继续执行。独立复盘 `auto_cvheqhfkaf4` 检查真实部署与回收。
  DB 存储巡检及匿名卷 7 天门槛不受此更改影响。
- **2026-11-03 活跃时间修正**：`fork-maintenance@7537807` 的扫描及 `workspace_activity.observe`
  读取最后一条 HEAD reflog **事件的时间**，不以 `logs/HEAD` 文件重写时间续期；Git GC/reflog expire
  可批量重写日志，实测旧事件为10月27日前后而日志文件均在11月3日08:57更新。
  HEAD/index、真实源码变动、开发会话与72小时门槛仍生效；不批量倒改已记录的用户活动时间。
  回归：`test_reflog_maintenance_does_not_renew_worktree_activity` 和
  `test_recent_reflog_event_is_retained_even_if_file_mtime_is_old`，真实 Git 的红/绿验证及全套123项通过。
  源码已在本机原入口生效；该仓未配置远端，提交仅本地保存。扫描预算、ignored分类与末端引用覆盖
  仍需分别验证，不以这项修复宣称清理自动化已全面有效。

- **2026-11-03 正常锁竞争**：`fork-maintenance@3d2e2a1` 把维护总锁和单任务调度锁的
  确认竞争记录为静默跳过；回执 `*-lock-skip.json` 保存锁路径、job token和完成时间。
  已有真实故障报告与incident保留；正文执行错误、仅错误文案相同和权限错误仍走真实故障处理。
  跳过不算空间清理有效，监督按token核回执，不因正常竞争创建诊断会话。
  现场曾观测repair-queue、branch-sync因此各产生诊断会话；其磁盘字节贡献未计量。
  回归为4个真实flock正反例，含调度锁、保留旧故障和callback异常；整合127项及末次4项通过。
  本机入口已生效，仓库无远端，提交仅本地保存。
- **2026-11-03 删除前引用证据**：`fork-maintenance@8eff12d` 在归档前后、每个依赖删除前及
  最终worktree删除前调用 `workspace_guard.py`。固定部署探针模块每次重新核SHA，缓存仅复用已核字节的
  编译结果；cwd/exe/fd/maps/绝对argv、挂载别名与inotify均纳入。`/tmp`别名实测samefile后才转换。
  不跟随链接完整扫描树，探针前后身份/指纹一致才接受；30秒预算在循环、扫描结束和成功返回前均复核。
  未知、读失败、挂载、变化或超时保留，记录检查时间、根身份、条目数与观测实占（含共享，非独占回收）。
  验收：3个真实fd/inotify/归档后新引用负例及无引用正例；12个边界测试；全套143项通过，
  `/usr/bin/python3`下12+20项通过。独立审查thr_sj778pv3zh的2项P1均复现并用回归修正。
  该仓无远端，提交本地部署。**这只补齐引用控制**：依赖自身72小时、末端身份/Mole sink、dirty归档
  的移除契约与force差异仍未闭合；监督任务继续使用完整核验后的非force原生命令，不据此调用旧force路径。
- **2026-11-03 原worktree入口保护修复**：`fork-maintenance@8d87f7a` 的`idle_cleanup.clean_one`
  对尚无可信逐依赖年龄/恢复/末端Mole契约的目标返回`retained/dependency_contract_unverified`，逐对象列路径，
  不再直接`shutil.rmtree`或`unlink`。这不扩大清理范围，也不把当前standalone依赖原型当作已启用能力。
  worktree改用非force原生命令；最终引用检查结束后重新核根dev/ino、未归档/ignored、HEAD/index、dirty
  和归档source_tree，任何变化保留；保留对象不写maintenance快照。已归档但本地仍dirty也保留，不reset/stash。
  回归：真实clean无依赖正例；最终guard期间新增`.env`、源码变化、根替换均保留；不足72h/活跃fd/inotify/
  归档证明原断言保留。父隔离全套232项通过，曾复现nonforce仍会删ignored的问题，修复后父独立复测保留。
  已提交本机原入口，无配置远端，未push；生产apply未运行，实际新回收0。新依赖原型、cache reset的机械
  接入和全局扫描吞吐仍未验收；不能据此恢复无证据缓存命令。部署/测试证据：
  `/root/.local/state/worktree-cleanup-review/runs/20261103T141500/raw/owner-deployment.json`。
- **2026-11-03 维护进程收尾（控制修复）**：`fork-maintenance@d7aa3eb` 在正常返回、超时和
  entrypoint 的 SIGTERM 退出时复查自己创建的 session，处理同 SID 不同 PGID 的残留成员；
  信号前绑定 starttime/session，优先使用 pidfd，保留原 owner 的资源 scope 收尾。
  权限/I/O/格式错误和过期查询不作空列表；查询与终止使用同一 deadline，正常消失的进程不误报失败。
  外来进程、服务和数据库仍依各自 owner 契约保护；本修复不接通未验收的 artifact 或临时镜像脚本。
  验证包含直前版影子全套 241 项、闭合分支修正后的 12 项取消及 24 项资源/失败路径；
  检查线程在 Python 3.11/3.12 各复验 12 项，部署后新进程 smoke 成功。
  已本机提交、无远端未 push；真实周期回收和容量收益待验，不计新清理目标或合规回收字节。
  证据：`/root/.local/state/worktree-cleanup-review/runs/single-dsh-session-cost-20261103T182352/parent-review/deployment.json`。
- **2026-11-04 worker 收尾期间保持总锁**：`fork-maintenance@4eaa4bb` 让原 scheduler
  worker 从 command 启动、等待、既有 `stop_tree` 收尾直至终态记录持续持有
  `/root/.local/share/fork-maintenance/state/run.lock`。command 和 `m.run` helper 继承同一
  open-file-description；统一核验 FD 有效、regular、dev/ino 匹配并重新取得排他 flock，
  子上下文结束不 unlock 父锁。仅真实锁竞争作为 busy；非法 FD、权限/I/O 错误拒绝。
  保留显式 pass_fds、非总锁的独立互斥、原队列顺序和 deferred/acquired 回执，
  等待沿原 job started_at 的24小时期限，获取锁及最后写回后仍复核 deadline。
  这是原 owner 的控制修复，无新清理命令、目标或年龄豁免；用户任务和外来 session 不属本控制回收范围。
  父在完整已提交基线上独立跑246项、257.205秒，以及6项 FD 边界、2项末端超时回归。
  实际 scheduler→Mole bridge→不同 SID 的 Git fixture 在旧版复现提前解锁，候选在 command
  SIGTERM 后、writer仍活的收尾阶段保持锁；终态前无 writer、marker不出现、锁随后可取。
  已仅本机提交自有4文件，无远端未push；控制修复本身回收0B，不归因既有tmp删除。
  这不接通 artifact 删除入口；原 `stop_tree` 的查询未知/身份绑定、worker和command同时
  SIGKILL后的准入仍须另证，不据本条宣称所有取消或删除路径已闭合。
  版本、精确文件、回滚快照和独立验收见监督 `runs/20261104T141500/parent-review/deployment.json`。
- **2026-11-03 扫描阶段日志（可观测性修复）**：`fork-maintenance@c1db93e` 为现有 discover、
  共享依赖、逐仓活动、inventory 和 apply 输出立即 flush 的 stderr JSONL；stdout 仍保留单个 JSON 总结。
  普通保留的 continue 也有逐仓结束事件；取消不写成功，inventory 异常仍交原 entrypoint/finish 处理。
  `complete` 仅表示该阶段函数正常返回，不证明全盘覆盖、删除资格或回收成功；删除条件和 sink 未变。
  原任务日志 `/root/.local/share/fork-maintenance/state/job-idle-cleanup.log` 及逐 token job-history
  捕获这些事件，下一真实任务才能验证吞吐；11-03 的旧 09:00 日志仍为 0B，不能补写历史。
  全局 discover 和所有 peer 的依赖扫描仍位于 apply 前，`--root` 也不能保证 Git 登记的 peer 只在该目录内；
  分仓预算与续办尚未部署，阶段日志不等于该瓶颈已修好。
  验证：检查端独立 31 项（26 项原入口、5 项进度/异常/stdout/continue 回归）、ruff、部署后隔离 smoke；
  clean_one 原字节及其他函数 AST 保留。本机提交、无远端未 push，本轮新增删除类别和回收均为 0。
  证据：`/root/.local/state/worktree-cleanup-review/runs/20261103T201500/parent-review/deployment.json`。
- **2026-11-03 共享引用扫描提速（吞吐修复）**：`fork-maintenance@934f1ad` 在每个 peer 开始时
  绑定 canonical root，在循环内复用；每目录只计算一次深度，末端重新核 canonical root 与 dev/ino。
  换根、权限错误等未知拒绝；保留原排除目录、深度和 local pnpm 三个前缀，dangling `.git` 仍继续扫描。
  该优化没有跨轮缓存，没有用父 mtime 代替现测，也没有改变 clean_one、72小时、ignored 或删除 sink。
  同一120 peer样本为33.424s→25.871s，47条引用集合一致；部署后只读全量观测555/555 peer、7950条引用、
  0错误，总332.307s。它证明列出的引用扫描阶段完成，不证明物理总账、对象闲置或删除资格，不能复用为后续删除计划。
  验证：父独立37项（原26、进度5、引用边界5及重复root resolve成本回归1）、ruff；10个外链fixture
  的root resolve从旧20次降到新2次。跨worktree/scoped逃逸、sibling前缀、深度6/7、dangling `.git`、
  权限错误、真实换根均有回归。已本机提交、无远端未push；新增删除类别和本轮回收均0，真实日任务效果待验。
  证据：`/root/.local/state/worktree-cleanup-review/runs/20261103T221500/parent-review/deployment.json`、
  同目录`postdeploy-independent-verification.json`。
- **2026-11-04 引用树相对路径复用与循环预算**：`fork-maintenance@9bda0d6` 只在
  `workspace_guard.tree_evidence` 的栈中携带相对路径，避免逐条重新解析祖先；
  fingerprint 字段与顺序、inode/大小/条目/根身份、全部挂载/类型/no-follow 检查不变，
  子目录条目循环增加同一30秒绝对deadline检查，未知仍拒绝，不跳过任何子树。
  父6项独立等价与18项持久/旧guard回归通过，原idle26项通过；真实209,931条目、
  187,736 inode的完整指纹一致。最终候选父单树测量26.535秒；安装后交互环境Python3.12
  的完整引用检查仍30秒超时，**对象保留，不能称29项超时已经解决**。
  早先6.835秒属于旧候选且解释器元数据缺失，不归于最终版本、不宣称稳定倍数。
  短时只读profile显示3.12剩余主要成本是mount/realpath；partial不作删除证据。
  本机仅提交自有2文件、无远端未push；可回退自有提交，保留其他dirty。
  验收与后续成本证据：监督 `runs/20261104T081500/parent-review/tree-deployment.json`
  和 `executor/raw/runtime-cost-diagnosis.json`；本控制修复新增删除类别/回收均0。
  **10:15运行时更正**：09:00真实任务的PID/starttime/exe已核为`/usr/bin/python3.11`，
  上轮3.12是交互探针，旧报告保留并附更正，不倒补已退出进程的解释器。
  旧29超时对象在09真实周期中28项完成引用检查后依赖保留、1项因真实fd引用拒绝；
  不能把交互探针超时等同于09任务超时，也不能把检查完成等同于移除。
  同轮严格mountinfo集合/generation实现`c15553dc`通过父61项检查及私有namespace
  真实file/dir/root bind拒绝验证。首次安装因`baseline-watch`持总锁跳过；随后确认
  持锁/排队任务全部终态、内核锁已释放，才在原owner/repo锁内串行安装自有3文件，
  提交`5b658b0`。完整表严格解析与末端generation重验，未知/表变/超时仍拒绝，
  全部类型/无跟随/身份/指纹/进程/inotify及72h条件保留；不跨树缓存，不启用直接删除。
  最终3.11单树4.364秒且指纹等价，安装后17项smoke/编译/ruff和实际import通过，
  新真实清理周期效果仍待验，不把本轮3条恢复proof或性能测量计为回收。
  候选、锁竞争和解释器更正证据：监督`runs/20261104T101500/parent-review/`。
- **2026-11-04 未验依赖提前保留（吞吐修复）**：`fork-maintenance@84d36d3` 在原
  `check_activity` 完成后检查 `installed_directories`；非空即按
  `dependency_contract_unverified` 保留，省去随后无效的归档和 peer 引用扫描。
  原活动检查自身的 snapshot/reference 成本仍存在；原归档后的 late guard 完整保留，
  72小时、BB/进程引用、根身份、唯一 ignored 与非 force 删除条件不变。
  不增加依赖删除类别，不跳过活动检查；无依赖对象继续原恢复证明与末端复查。
  回归：父独立40项及ruff；安装后3项early-retain smoke、精确源码SHA与三文件提交复核。
  已本机提交，无远端未push；新版本真实周期吞吐和回收待验。此前fb轮移除5项属于旧版934，
  其3,816,771,584B为末端allocated观测，不能计作本修复收益或独占释放。
  部署/回滚证据：`/root/.local/state/worktree-cleanup-review/runs/20261104T041500/parent-review/deployment.json`。
- **2026-11-04 工蜂自建WIP身份与失败intent恢复**：`fork-maintenance@1ca8823` 仅对
  `git.woa.com` 归档查询已安装 `gongfengcli users me`，固定官方API地址，校验active账号的
  username/email；查询、格式或字段未知时保留，不回退到仓库中的其他平台身份。
  原远端已保存的proof先实时核验复用，不要求重新查询身份。未验证intent仅在原ref确认不存在、
  源snapshot未变且旧WIP身份与当前已认证身份不符时，先保留旧证据副本，再生成和验证新WIP；
  同身份重试复用原SHA/ref，未知查询或源码变化拒绝。原HEAD/index/config、旧local ref和既有
  verified proof均保留，GitHub/local归档和全部72小时/引用/ignored/非force保护不变。
  回归：父在真实Git/bare remote验证旧错身份WIP重建、配置/源码/index/hook不变；补齐同身份
  重试push、远端已保存而local未verified、verified复用、未知拒绝和源变化。父主体38项通过，
  最终绑定API版本的10项影响回归及ruff通过。已本机提交，无远端未push；新周期仍pending，
  不把既有5项移除算作此修复收益。证据与回滚：
  `/root/.local/state/worktree-cleanup-review/runs/20261104T041500/parent-review/identity-deployment.json`。
- **2026-11-04 工蜂身份执行环境修正**：`fork-maintenance@db28fe2` 修复非交互任务缺认证变量、
  最小PATH缺CLI的问题。只读解析当前用户既有NVM中的唯一完整CLI/Node安装对，解析后的两者
  均约束在同一安装根；缺失、损坏、跨版本或歧义拒绝，不猜版本、不安装或删除工具链。
  固定login shell通过参数数组启动该Node/CLI，复用现有登录认证；不复制令牌到状态/日志，
  不改变仓库/global Git身份或原归档条件。错误只输出有限原因码，未知响应不原样回显。
  父在无继承认证变量、最小cron PATH及`/usr/bin/python3`下实际只读验证身份匹配；23项影响回归
  和ruff通过，`archive_source`与其余删除保护AST不变。已本机提交，无远端未push。
  11-04真实09周期中auth/gateway/hermes三条私有归档ref/SHA已由父远端精确核对，
  WIP身份匹配已验证服务账号；对象仍按本地数据保护保留，整轮0移除，不把归档当回收。
  前8a轮0移除、3项身份API失败仍保留原件，不把后续成功回填旧历史。
  证据与回滚：`/root/.local/state/worktree-cleanup-review/runs/20261104T061500/parent-review/deployment.json`。
- **owner 授权（2026-09-17）：无主匿名 DB 卷按 7 天保留期自动清理。**（2026-11-04 起满 1 小时即可清，见上文 owner 策略；本执行器仍按 7 天运行。）该执行器已随监督于 2026-11-04 暂停，匿名卷现在由 `docker_leftovers`（每小时定时任务）按上文 owner 策略处理。唯一执行器
  `/data/metrics/ai-storage-maintenance/<round>/auto_clean.py`（默认 `--preview` 只读，
  `--execute` 执行）；只删同时满足全部条件的精确对象：① 卷名为 64 位十六进制匿名卷
  （命名卷永不自动删）；② 标签仅 `com.docker.volume.anonymous`（compose 声明卷除外）；
  ③ 无任何容器（含 stopped）引用；④ 内容特征为 MySQL/PostgreSQL 数据目录
  （非 DB 归属如 itfs 的 logs 卷除外）；⑤ 数据目录最后写入日 == 创建日；
  ⑥ 创建已满 7×24 小时；⑦ 属于“批次日”（同日同引擎 ≥5 个满足 ①–⑤ 的卷），
  零散/单例卷保留待人工确认——这是安全失败方向。删除只用 `docker volume rm <64位ID>`，
  禁止 `docker volume prune` / `system prune`；每批前重查引用与身份。
  回归检查：`auto_clean.py --preview` 候选集只随新批次增长；执行后 `docker ps -a` 无重启、
  被引用卷仍在。2026-09-17 首次执行：338 个 PG 卷 / 15.84GB 精确删除，0 失败。
- **2026-11-04 匿名DB卷fresh计划与末端回执（db-volume-fresh-plan）**：本监督只启用
  已验证的DB窄入口，部署在系统盘监督
  `runs/manual-available70-20261104T160000/executor/deployed/auto_clean.py`，SHA `6932c48b…`。
  必须携带既有 `--out` 持久回执，CLI自身持现役owner总锁与repo锁；Busy跳过，不绕锁。
  每次建立完整fresh卷/全部容器引用与retained计划；保护表读取、schema、缺失inspect或
  扫描非零/超时均拒绝。只接受local driver、空Options、canonical DockerRootDir内的
  匿名精确卷；无跟随、同文件系统、无祖先链接、完整树最新写入日与创建日相同且满7天，
  同日同引擎批次至少5个。逐对象在exact非force rm前重新绑定卷元数据/devino/完整树、
  全部容器volume和父子bind引用、宿主FD/maps/inotify/namespace及retained；未知保留。
  每次rm前持久intent、每次结果fsync；中途异常保留先前结果并非零退出。
  原Go cache分支与历史fail-open脚本继续hold，不因本次修复启用；不新增调度器。
  父独立18项持久回归及7项边界/真实final-inspect文件变化测试通过，真实锁内CLI preview
  完成。首个execute因锁竞争正常跳过；新现场一次fresh执行移除21卷，父核21/21卷和原路径
  消失，guard allocated合计4,462,632,960 B，不是独占释放；Docker SI估算和同期statvfs净变
  分列。命名retained主卷与原24容器ID保持；历史itfs保留ID在本轮初始清单已不存在。
  来源仅有lightspace库名与仓库fixture匹配，缺历史创建关联仍记“未定位”。
  证据/回滚：同run父`db-independent-acceptance.json`、部署receipt；回滚为停用本轮新入口，
  不回退执行旧fail-open删除代码。该能力复用原7天/批次授权，不扩大到命名卷、共享缓存或备份。
- `pg_snapshots` 是 PostgreSQL 标准数据目录（与 `base`/`global`/`pg_wal` 同级，活体
  PG16 集群同样存在），**不得作为备份/快照/恢复依赖证据**。回归检查：活体 PG 集群
  `ls $PGDATA` 必含 `pg_snapshots`；探针不得把它计入 backup/restore 依赖。
- 无主 MySQL 卷的 binlog 不构成自动清理的保留理由（批次日 + 冻结 + 无引用已覆盖），
  但**命名** E2E MySQL 卷仍按 binlog 规则处理：检查 owning container，用
  `PURGE BINARY LOGS` 加持久化 retention 设置收敛；MySQL 运行中绝不直接删 `binlog.*`。
- 精确容器 `lighthouse-ailab-e2e-mysql` 的可弃规则仍有效，但该容器/卷当前不存在。
- 其他 MySQL/PostgreSQL/Qdrant/MinIO **命名**卷即使名字像测试也按高风险处理，
  不在自动清理范围内。
- **owner 指定的项目级保留策略（2026-09-17）**：`schedule` 项目只保留**最新一个**残留对象，
  其余按项目精确删除；`SaiensCreditSystemRails` 名下的卷 owner 明示**不保留**，含命名卷
  `saiens_bundle` 与三个 compose 声明卷（`saienscreditsystemrails_redis_data`、
  `fast-feedback-rules-followup_postgres_data`、`scan-preview-drag-experiment_postgres_data`），
  compose 卷删除后下次 `up` 会重建为空。该授权**只覆盖这两个项目**，其他项目的命名卷仍按
  “永不自动删”处理。执行记录：2026-09-17 删除 schedule 5 个 + Saiens 4 个，0 失败。

## 本地数据模式（显式 rebuildable / retained）

- **原则（owner 2026-09-17）：清理资格来自显式模式声明，不得用“匿名卷”或“年龄”推断某份配置是唯一真源。**
  过程数据（构建/发布/部署/事件/运行/同步游标/审计）默认丢弃；需要长期保留的历史必须先转成脱敏、
  可审查的 fixture，而不是延长卷寿命。
- 声明源：① `.local-test.yml` → `database.mode: rebuildable`（默认）/ `retained`（显式例外），
  见 mason-skills@a70f0fc `common-skills/local-test`；② Operations 本地数据重建契约
  `docs/LOCAL_DATA_SEED.md`（`operations-worktree-local-seed@5e26812`）：仓库基线 `deploy/local/seed/baseline.json`
  + 应用内置配置 + 0600 私有 overlay `.local-test/seed.private.json`；`bin/dev seed:status|seed:apply`
  做空库回读验证；`bin/dev stop/destroy/gc` 精确删除本环境库，最后一个环境停止后停共享 MySQL。
- 机器可读登记：`/data/metrics/ai-storage-maintenance/local-data-modes.json`
  （`retained_volumes` + `project_modes`）。`auto_clean.py` / `attribute_sources.py` 必须先读它，
  **retained 优先级高于容器引用与一切启发式**。
- **当前 retained：`lighthouse-ailab-mysql-data`**（Operations 共享主卷）。2026-09-17 状态：seed 实现已整合
  到本地主干 `0216358`（记录 `d04cc52`，clean commit 黄金 smoke 通过 build 238663998 / deploy 238665437），
  但 owner 明确“当前不要提前清理或扩大范围”，故**继续 retained**，解除需 owner 明示。
- **退役登记是保护声明源**：`~/.local-test/operations-template/retired-databases.json`
  （schema `lighthouse-local-database-retirement/v1`）记录 `status: retired-read-only` 与 `delete_after`。
  `delete_after` 之前一律保护——不清理、不建议清理、不扩大范围；到期后也只由 owner 指派的自动化按精确
  对象删除。当前条目：库 `ailab`（48 表 / 10876 行；备份 SHA256 `72458c1e…aa4f`；
  `delete_after=2026-10-17T08:23:30Z`；owner 通报删方为 automation `auto_nxymfdcsy5c`）。
- **`.local-test/backups/**` 是审计证据，不是空间回收目标**：0600/0700 备份一律不清理、不建议；
  只按退役登记的 `delete_after` 由 owner 指派方处置。登记未列明的备份同样保护，不推断。
- retained 例外必须登记“不可重建内容 / owner / 清理方式”；未登记的命名卷不因“看起来没人用”被自动删。


## 报告形式

- owner 于 2026-09-17 决定：DB 存储巡检**不再产出 HTML 报告**；证据（JSON + 原始采集）
  落 `/data/metrics/ai-storage-maintenance/<round>/`，结论与执行量在回复中简报，
  并追加本仓 `storage-audit.log`。**owner 2026-09-18 扩展到全部自动化**：DB、Worktree、
  每周系统盘复盘一律不产出 HTML，产物只保留 JSON / Markdown / `raw/`；需要图文材料时
  由 owner 单独一次性要求，不写回流程默认值。
- **每轮必须给出“待确认对象的来源项目”**（owner 2026-09-17 追加要求）：卷/镜像/构建缓存逐项标注
  项目 + 证据 + 置信度，不能只写“无主/未确认”。唯一执行器
  `/data/metrics/ai-storage-maintenance/<round>/attribute_sources.py --out raw/attribution.json --table`
  （只读，约 3s）。判定链路：compose 标签 → 同名检出目录/分支 → 内容特征 → 镜像 label/repo/层重叠。
  归属仍不明确时写“未确认”，并列出已被排除的候选。
- **最终报告一律用表格**，每张表至少含：**归属项目、年龄、不活跃时间、结论**（外加数量/大小）。
  口径：年龄 = now − 创建时间；不活跃 = now − 树内最新文件 mtime（卷）或 `Last used`（构建缓存）；
  镜像不记录 last-used，写“不可得”，年龄用 Created。跨轮比较必须用同一口径。
- **“结论”列只能取四类**：`已清理` / `自动清理（本轮）` / `不清理（原因）` / `需决策（原因）`。
  判定优先级：**owner 项目级策略 > 容器引用 > 通用规则**。凡因“未到期”不清理的，必须写出
  `MM-DD HH:MM 起自动清理`；到期时间按 0.1 天精度（约 ±2.4h），只作排期参考，执行时刻以脚本实算为准。
  表末给一行汇总：`自动清理 N · 不清理 M · 需决策 K`。**不允许只写“待确认”而不给处置。**
- **回复格式统一契约**：`devcloud-patrol` 巡检报告的 owner 回复形状
  由 [automation-report-format.md](automation-report-format.md) 定义（一屏汇总表 → 按结论分组明细表 →
  安全边界表 → 下一步表），各任务不再自行发明格式；worktree 的对象级内容契约仍见
  [worktree-report-format.md](worktree-report-format.md)。

## 已知来源指纹（归属用，跨轮复核）

- **匿名 PostgreSQL 卷（文件 uid=70 → `postgres:16-alpine`，库名 `schedule`/`schedule_test`/`temp`
  或仅系统库）→ `schedule` 项目**（`/data/code/schedule/tests/backend-blackbox/.../managed.py`
  的 `PostgresContainer`）。排除项：`lightcloud/skill-telemetry` 虽同用该镜像，但其夹具固定
  库名 `telemetry`；`schedule/services/api/tests/integration` 用 `postgres:16`（uid 999）。
- **匿名 MySQL 卷（文件 uid=999 → `mysql:8.0`/`8.0.46`）→ `lighthouse_ailab_operations`**：
  `scripts/test_migrate_test_environment_data.py` 等用 `docker run -d --rm mysql:8.0.46` 后
  `docker rm -f`（未带 `-v`），显式 rm 不触发 AutoRemove 的匿名卷清理 → 泄漏。库名
  `story03_*`/`story06_*`/`ailab_e2e_*`/`lighthouse_meta` 均属该项目 e2e 命名。
- **匿名 MySQL 卷，库名 `lightspace` / `lightspace_sandbox` → `lightspace`**（2026-11-04 实测 234 个，
  24 小时新增 122 个）：`scripts/test_mysql.sh` 的 trap 与 `scripts/test_stack.sh` 都是 `docker rm -f`
  不带 `-v`，同上泄漏；修复已转 lightspace 主会话 `thr_c8885ghubu`。
- **go-build 每小时十几 GiB → lightspace 验证流程**（2026-11-04）：把源码复制到
  `/var/tmp/<任务>/merged-source` 再 `go run ./cmd/infractl`/测试，路径每次不同，缓存从不命中；指纹是缓存里
  一批约 58 MiB 的 `*-d/infractl` 可执行文件，及 `go` 进程 cwd 位于该目录。
- Go 缓存卷（`lighthouse-*go*`、`lighthouse-ops-go-*`）→ `lighthouse_ailab_operations`：build cache
  编译对象内嵌 `git.woa.com/lighthouse/lighthouse_ailab_operations/backend-go/...`。
- `saiens_bundle`、`saienscreditsystemrails_*`、`fast-feedback-rules-followup_*`、
  `scan-preview-drag-experiment_*` → `SaiensCreditSystemRails`（后两者是其 worktree 分支名）。
- `echo-acme-challenge` / `echo-letsencrypt` → `echo-protocol-ai-puzzle-game`；
  `schedule_pgdata` / `schedule_redisdata` → `schedule`；`crabbox-cache-*` → `openclaw/crabbox`；
  含 `itfsd.pid`/`logs` 的匿名卷 → `itfs` 服务（`mirrors.tencent.com/rtx_team/itfs.linux-client`）。
- **`itfs` 服务及其全部对象一律保留、不再逐轮询问（owner 2026-09-18 明示）。**
  核实结论：`itfs` 是 systemd 管理的长期服务（`itfs-docker` / `itfs-healthcheck` 每 2 分钟 /
  `itfs-codex-update` 每日 04:30），容器 `itfs` 用 `rtx_team/itfs.linux-client` 把云端 ITFS
  space 以 FUSE 挂到 `/mnt/itfs/<spaceid>`，承载 Codex CLI 与权威 `auth.json`；仅监听回环
  `127.0.0.1:49252`。因此其容器、镜像（含 dangling 的 `9d9722601084`，被运行容器引用）、
  云空间挂载、`/data/itfs/cache`、当前数据卷，以及早期运行遗留的匿名 `itfsd.pid`+`logs` 卷
  `37ad2a2ee5ab3bd8360218b6caaa8dc989a855e0af9da8dbea6f2ae3b9489576` **全部保留**。
  该遗留卷已登记进 `local-data-modes.json` 的 `retained_volumes`，`attribute_sources.py` 会直接判为
  「不清理（owner 明示 retained）」，不得再作为「需决策」上报。回归检查：`attribute_sources.py` 输出中
  该卷结论以「不清理（owner 明示 retained」开头，且不出现在「需 owner 逐项决策」清单。
- **opencraft 本地 smoke（`/tmp/oc-local-smoke-20261024/runs/<时间>-<pid>-<hash>/`）→ opencraft**：
  每次运行留下一份构建和 npm 产物，10-24 到 11-04 共 260 次、14.5 GiB。容器目录一直有新的运行写入，
  clean-tmp 永远不会把它当成候选；修法在生产端，只保留最近几次运行。
- **`/tmp` 下带 `.git` 目录的独立 clone（非 worktree，约 39 个、约 17 GiB）→ 创建它的任务**：
  两个清理器（clean-tmp、worktree_sweep）都不处理这类树，只能由创建任务自己收尾。

## 包缓存

- `/data/.pnpm-store` 这类 package store 是 owner 管理的缓存，不是通用递归
  删除目标；确认没有活跃包任务后，优先 `pnpm store prune`。

## Python 站点包

- pyenv 全局解释器的 site-packages 不按“目录大、名字像缓存”判死重：daemon 化
  的项目服务可能正在导入其中的包（2026-09-01 实证：lighthouse 的
  kb_vector_service.py 常驻导入 pyenv 3.12 的 torch/nvidia/triton）。卸载任何
  站点包前必须先跑 `./mo devcloud who-uses <路径>`（扫 /proc 的 exe/cwd/fd/maps），
  发现引用进程即停手、转 owner 决策；maps 里的 mmap 和 fd 一样算引用，
  “看起来没人用”不构成证据。回归检查：who-uses 对活进程必须能报出引用
  （活体探针；卸载动作无法离线固化测试）。

## 临时目录与 worktree

- **owner 2026-11-14 批准**：`/tmp` 即 `/data/system-tmp` 的 bind mount（数据盘），
  `/var/tmp` 是系统盘普通目录；两根沿用同一整项闲置 3 天规则，自动清理由
  每日北京时间 03:30 cron 执行（crontab `# devcloud-clean-tmp` 行）：外层 timeout 1790 秒，
  顺序执行 `/usr/bin/python3 -B <执行器> clean-tmp --path <固定根> --execute --age-days 3 --json`，
  先 `/var/tmp` 再 `/data/system-tmp`；每根另有 timeout 875 秒、kill-after 10 秒，两根最多 1770 秒。
  同一命令去掉 `--execute` 即预览，不接受其他路径。
  `systemd-private-*`（及已有 `systemd-*`）始终跳过。保留期 3 天。唯一执行器为固定部署版
  `/data/tools/devcloud-maintenance/releases/7ca528d7c5f3f004/bin/devcloud.py`；上一版
  `df3e2b353d8e1c72`（仅 /tmp）、`f1e330c325c73fd0` 与 `1c2d71d3ab21cc79` 保留作回滚；后续升级先验收新快照，再同步 cron 版本路径与每日 DSH 检查，
  不执行共享工作区。
- **systemd-tmpfiles 自 2026-11-04 起不再按年龄清理 `/tmp`**：`/etc/tmpfiles.d/tmp.conf` 为
  `q /tmp 0700 root root -`（age 一栏为 `-`）。原 3d 规则逐个文件删除，曾把闲置 worktree 里的已跟踪
  文件删掉（32 个 /tmp worktree 中 7 个受损）；遇到孤立 FIFO 时还会卡死，10-30 起整个 tmpfiles 清理
  因此停过 4 天 20 小时。活体探针：`systemd-tmpfiles --cat-config | grep -E '^q /tmp '`。
- 升级必须新建快照：目录名 = `manifest.json` 规范化内容的 sha256 前 16 位，全部文件逐项
  记录 sha256 与 mode，目录/文件置只读；验收 = 清单复算 + 真实 preview，再改 cron 与
  每日 DSH 检查覆盖的根（改完回读 cron 和检查任务核对）。
  回归：`tests/test_devcloud_cleanup.py::test_policy_shards_cover_every_candidate_exactly_once`
  与 `::test_policy_shard_refusal_discards_the_whole_candidate_set`。
- **历史｜2026-11-04 分片吞吐验证**：本次不可变快照的 43 个 payload 只改变
  `POLICY_SHARDS = 16` 为 `32`，年龄、保护、完整计划、末端 guard 与预算不变。
  实际 `build_plan` 上游产生 23,584 个 policy 输入（不是根目录 39,002 个名字）；
  同输入 old16 在剩余 72.644 秒超时，new32 完整完成于 61.647 秒。
  独立 25 项行为回归通过，新快照真实 preview 为 complete/ok、70.43 秒，
  50 项候选 3,134,709,760 B 仅作预览，不能计为回收。
  每日 cron 与每周 BB 入口已逐字段核对，只切换版本路径；旧快照保留。
  证据在监督 `runs/20261104T061500/parent-review/tmp-release-acceptance.json`
  与 `tmp-deployment.json`；实际删除另按 owner 逐项日志验收，不复用预览名单。
  同轮新鲜完整计划实际执行为 50 deleted / 0 failed，202.52 秒；逐项日志
  allocated 合计 3,136,368,640 B，父核 50/50 原路径及隔离路径消失、无遗留隔离项。
  该值不是已证明独占释放，项目归属未定位；普通可用净变化与删除量分开记账。
- **历史｜2026-11-04 容量统计防回退**：`1c2d71d3ab21cc79` 仅修复容量计算与文本标签，
  总量使用 `f_blocks`，物理空闲使用 `f_bfree`，普通可用使用 `f_bavail`，
  保留差额为物理空闲减普通可用；物理已用百分比与计入保留块的占用百分比分别输出。
  `used_percent` 兼容字段明确表示物理已用，不再使用排除保留块的分母；
  本监督自2026-11-04用户提高目标后，按 `ordinary_available/total >= 70%` 验收，
  对应计入保留块的使用率至多30%；旧报告保留历史目标，不能用物理已用替代。
  零、负或不一致容量拒绝；原7天、扫描/执行预算、候选与删除保护不变。
  同步干净源码的两个窄hunk及持久 `tests/test_devcloud_capacity.py`，源码分片数16保留，
  生产快照仍为32；只运行部署快照。父30项持久测试与5项独立probe通过，真实完整preview
  65.87秒。每日/每周入口只换版本路径，旧版本保留；本修复本身不计回收字节。
  部署/回滚证据：监督 `runs/20261104T081500/parent-review/capacity-deployment.json`。
- 策略阶段逐候选约 50ms（`validate_path_for_deletion` ~25ms + `should_protect_path` ~17ms），
  候选数上千后**禁止**用「按大小取前缀」省时间：绝大多数候选会被策略拒绝，取前缀会让允许项
  永远排不进来。只能分片并行（`POLICY_SHARDS`），任一分支片失败或超时必须整轮 fail-closed，
  不得接受部分结果。
- 清理器要求数据盘和别名身份正确，根及祖先为 root 所有且不对其他用户可写。
  `/var/tmp` 是唯一例外：根本身允许 sticky 写权限，祖先仍须可信，必须在系统盘且不是挂载点；
  所有候选仍须 root 所有且不对其他用户可写。隔离区分别为
  `/data/metrics/devcloud-maintenance/` 和 `/var/lib/devcloud-maintenance/`，均在源文件系统且祖先可信。
  完整扫描检查 cwd/exe/fd/maps、绝对 argv、各 mount namespace 和 inotify；
  证据未知则拒绝。内部有新文件、仓库、凭据、备份、数据库、socket、symlink、
  硬链接、挂载或外部可写文件的树保留。Mole 保护与用户 whitelist 同时约束
  原路径、/tmp 别名和删除入口，不允许隔离路径绕开保护。
- 扫描预算 240 秒/单树 5 秒；不设候选数量上限，按大小排序处理，每根整轮预算 840 秒（含扫描）
  用完为止，剩余项递延到下一轮，每项开始前预留 120 秒。读取 /proc 证据时进程正好退出的，
  对同一 PID 最多重查 3 次（间隔约 100 ms）：原进程已退出、成为僵尸或 starttime 变了就继续；
  同一进程一直存活、证据仍不全才拒绝。
  单实例锁覆盖整轮；隔离后再核对对象引用与完整指纹，Mole dry-run 和最终
  sink guard 均通过后删除；失败尝试恢复，原名已占用时保留原件并报告位置。
  遗留隔离项会阻止下一轮执行，只可人工核实，不能当过期缓存清理。
- 每日账本 `/data/metrics/devcloud-clean-tmp.log`：intent/deleted 事件与每根各一条
  summary 分开，`path` 区分两个根；错误日志为同名 `.stderr.log`。summary 记录两盘前后容量和
  部分失败，失败非零。logrotate 每日检查，保留 14 份/最长 30 天、5 MiB
  检查阈值，不截断活跃日志。回归：`tests/test_devcloud_cleanup.py`；本机
  挂载与真实 Mole 路由另以专属测试文件验收，结果入 `storage-audit.log`。
- 每日 DSH 检查只读两根 summary、比较容量并解释增长，必要时预览同一执行器；
  没有第二套 14 天删除流程，也不执行迁移。系统盘实际使用率达到 70% 时
  突出报告；/tmp 清理释放数据盘，/var/tmp 清理释放系统盘。
  `/etc/tmpfiles.d/tmp.conf` 继续遮蔽系统默认 `/var/tmp` 30 天规则，禁止恢复逐文件年龄清理。
  每日 DSH 检查第 3 项分别核对两个根最近一次 03:30 后的 summary，均需 complete/ok、failed=0。
- `/data/tmp` 和其他测试产物需要结合 owner、持久数据及活动证据逐项审查；
  年龄和无进程引用不足以授权删除；目录叫 `tmp` 不构成可弃证据。

### 历史 worktree 巡检规则（仅供解读旧报告）

以下规则描述已退役的 `patrol_worktrees.py --execute` 流程，不适用于当前每日清理。
当前自动任务的判断与分支保留政策以 `/data/code/worktree-cleanup/README.md` 为准；
旧 `patrol_worktrees.py` 只保留手动只读预览用途。

- **worktree 本体回收（owner 2026-09-17 常设授权；2026-09-18 修订分支政策）**：worktree 生命周期通常
  小于 7 天，超过即视为无价值残留。三条**同时**满足才可整棵回收，满足即自动执行，
  不再逐项等人点头：
  1. age ≥ 7 天（168h，`worktree-cleanup` 的 `.git` mtime 口径）；
  2. 远端已有或主干已有——merged/closed PR head 包含本地 HEAD、HEAD 等于远端
     分支 tip、或 HEAD 含于远端默认分支；
  3. 工作区干净、非 locked/prunable、无活跃进程引用（`who-uses`，须串行探针）、
     无 BB 环境/线程指认。
  **分支政策（owner 2026-09-18：「只要有证据证明可以安全删除…不要都依赖人工决策」
  「默认删分支是合理的」）**：worktree 回收时其本地分支默认同步删除（删除依据就是
  该次回收的远端证明，squash 合并同样覆盖）；另对无 worktree 认领的本地分支做
  孤儿清扫：tip 并入默认分支谱系（origin/<default> 优先，本地 <default> 兑底）
  或 tip == 远端同名分支 tip 者删，其余（如 squash-merged 且远端分支已不存在）
  保留并报告原因。`--keep-branches` 可整体退出。清扫失败只计数，不回退 worktree 回收。
  **分支治理独立入口（采纳 Oracle 二轮）**：执行器 `--sweep-only` 模式
  （apply 执行 / dry-run 预览，无需审批来源，host 无关地探测默认分支）；
  patrol `--execute` 时对**全部扫描仓库**跑 sweep-only（已随 apply 清扫的
  repo 跳过）——最后一个 worktree 消失的仓库不退出分支治理范围。
  唯一执行器 `/data/code/mole-linux/.claude/skills/worktree-cleanup/scripts/cleanup_worktrees.py`
  为本次宿主解析的安装位置，脚本入口由调用方显式传入，不在巡检 Python 内猜测 scope。
  执行 `--dry-run --min-no-pr-age-hours 168 --write-approval-report …` 后接
  `--apply --approve <token>…`（**只传筛后集合的 token，采纳 Oracle P0 不整报审批**：
  同仓被 who-uses/BB/年龄排除的候选不得随报告获批；who-uses 未知、BB 归属查询
  失败一律本轮保留下轮重评；执行前重探 who-uses 刷新动态证据）；它内部执行
  `git worktree remove`（**非 `--force`**）、
  apply 前按 HEAD/lock/dirty/ignored 指纹复验、按上述政策删分支（`update-ref -d`
  旧 OID 校验删除，采纳 Oracle P0 关闭证明后删除竞态）、`.local/` 备份到
  `/root/.local/state/worktree-cleanup/<repo>-<hash>/<ts>/`。禁止 `rm -rf`、
  `git clean` 或 `--force` 来绕过它的拒绝；**删除分支必须走执行器**（有证据才删、
  可审计），不得在执行器外手工删。
  PR 仍 open、脏工作区、远端不可证、prunable、locked、活跃 BB 环境一律保留，
  并逐项列原因与 PR/MR 地址。
  **`--min-no-pr-age-hours` 只约束无 PR 分支**，PR-based 候选必须另行做 age 过滤，
  否则会把 7 天内的在办工作误判为可回收。
  **prunable 注册（owner 2026-09-18 授权）**：`--execute` 时对含失效注册的 repo 自动
  `git worktree prune --verbose`（git 已确认目录不存在，零数据丢失），结果入账本；
  dry-run 只报告。每轮报告给 ≥7 天对象附相对远端默认分支的 ahead/behind 与独有
  diff 规模（借鉴 gmc `wt list --diff-base`），仅作价值信号，不作删除授权。
  回归检查：同一 repo 在 168h 与 24h 两个门槛下 eligible 差异只出现在无 PR 项；
  回收后 `git cat-file -t <head>` 仍为 commit；分支默认已删（`branch --list` 不再命中，
  仍在则视为删除失败并报告）。
- **非 GitHub host（git.woa.com）必须用 `gongfengcli` 核验 MR，不得停在“无法判定”。**
  凭据：优先环境变量 `GONGFENG_PRIVATE_TOKEN`，否则读 owner 维护的
  `/data/code/lighthouse/developer/.env`（要求 root 所有且组/其他不可读，否则记「凭据缺失」，
  不降级、不猜测、不另找途径）。**token 只由脚本解析并经 `env=` 传给子进程**：不进 argv、
  不进报告/证据/账本/日志、不进 Agent 上下文；Agent 不得 `cat`/`grep` 该文件。
  查询仅限 `git.woa.com`：`mr list <project> --state <opened|merged|closed> --source-branch <branch> --all`；
  先查 opened，merged 优先完整 HEAD 全等的项；所有状态读全分页并验证响应结构。
  **`--state all` 恒返回 0 条（0.1.0 实测），禁止使用**，且必须在本地校验 `source_branch` 全等。
  MR 链接：`https://git.woa.com/<ns>/<project>/-/merge_requests/<iid>`（实测 200；不带 `/-/` 为 302）。
  判定同 GitHub 口径：`state=merged` 且 `source_commit` == 本地 HEAD 才算精确 head。
  **执行方式（owner 2026-09-18 决定「补充工具缺口」）**：唯一执行器不变
  （`cleanup_worktrees.py`：非 `--force`、分支按 2026-09-18 政策默认删除、`.local` 备份、
  apply 前按指纹复验），
  新增 `--remote-evidence <json>`；`patrol_worktrees.py --execute` 把工蜂核验结果按 repo
  写成证据文件，再跑 `--dry-run --write-approval-report` → `--apply --approve-report` 两跳
  （证据文件仅含筛后精确 head 项，报告 token 集合天然等于筛后集合；GitHub 路径则
  必须逐 token 传）。
  证据模式硬约束：**完全不调用 `gh`**（非 GitHub host 没有 GitHub API 可查）；只接受文件里
  `path`/`branch`/`head`/`source_commit` 与本地全等、`state=merged`、age ≥168h 的项；
  `head`/`source_commit` 必须是完整 40 位对象名；`--min-no-pr-age-hours < 168` 直接 usage
  error（fail closed）；文件里没有的 worktree 一律以「无证据」保留。回归：
  `tests/test_patrol_worktrees.py`（凭据缺失/权限不安全/不泄漏、逐状态查询、分支全等、
  证据只含精确 head 且不含 token）与 owner 工具
  `tests/test_cleanup_worktrees.py`（证据模式不调用 gh；非精确 head、分支不符、未合并、
  缺记录、年龄不足、短对象名全部保留；apply 时工作区漂移拒绝）。
- **含 submodule 的工作树不进入自动执行**（2026-09-18 实测）：`git worktree remove`（非
  `--force`）对它报 `fatal: working trees containing submodules cannot be moved or removed`
  —— 这是 git 的拒绝，不是工具缺口。巡检在执行前用 `git submodule status` 只读判定
  （仅当 `.gitmodules` 存在才探；探测失败时交给 sink 判，不据此放行），命中即保留并标注
  「含 submodule：git 拒绝非 --force 移除（需 owner 决定）」，退出 `failed` 计数、不再每轮
  重撞。**不得改用 `--force`，也不得先 `git submodule deinit`**——后者要先改动工作树，
  属 owner 决策。回归：`tests/test_patrol_worktrees.py`（无 `.gitmodules` 不调用 git；
  有 submodule 输出即判阻塞；探测失败不阻塞）。
- 此条为本机 owner 运维授权，作用于 DevCloud 巡检；Mole 产品自身 purge 的判定
  规则（`AGENTS.md` Working Rules「worktree staleness is not decidable」）不受影响。

## 日志

- 每轮清理明细记录来源项目及归属证据（Git主checkout/远端项目、owner清单或制品标签），
  并按项目汇总移除数量与大小；证据不足写“未定位”。上游镜像repository只说明镜像来源，
  不能据此断言哪个本地项目使用过它。对象实占、镜像共享层显示大小与同期df净变化分别列示。

- `/var/log/messages`、`/var/log/cron` 归 logrotate/journald 管辖；绝不按
  `rm` 或 shell 重定向截断活跃日志。
- AnyDev 轮转文件 `trpc.log.bk-*` 是 owner 管理日志：轮转要有 age 与
  open-file 证据；当前 `trpc.log` 绝不视为可弃。

## 对账

- 下"只剩几个 GB"的结论前，先用可见 du 对账 df；Docker overlay 遍历超时
  时，把差额记为调查发现并改用 Docker metadata/owner 命令，绝不按路径删
  overlay 目录。

## 分析器要求

- 分析器必须暴露：有界 deep inventory、BuildKit 可回收字节、容器标签状态、
  `/proc/*/fd` deleted-open 证据，让每轮可横向比较；一次性测量留在
  `storage-audit.log`，不进规则。
- 分析器默认只读；保持 Mole 工作流：inspect → classify → preview → 执行
  一个 owner 批准的动作。
