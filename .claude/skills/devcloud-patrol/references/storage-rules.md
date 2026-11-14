# DevCloud 存储清理规则

本机存储调查沉淀的持久规则，按目标分组，是磁盘侧分类与清理的唯一规则源。
设计原因、布局、决策和踩坑记录见
[DEVCLOUD_STORAGE_DESIGN](../../../../docs/DEVCLOUD_STORAGE_DESIGN.md)；
现行任务和复现见 [DEVCLOUD_AUTOMATION](../../../../docs/DEVCLOUD_AUTOMATION.md)。
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
- **owner 命令也是删除入口**：`go clean`、`npm cache clean`、`pnpm store prune`、
  `uv cache clean`、`docker ... prune` 不因“命令在手册里”或“数据可再生”豁免年龄、预览、
  锁和末端引用证据；受测计划之外不直接调用。2026-11-03 的纠偏回执和证据已移入设计文档
  「决策记录」与本机账本，本文不再重复历史叙事。

## owner 2026-11-04 存储维护策略（与旧条款冲突时以本节为准）

owner 在 `thr_tikzbd34dg` 看过增长诊断后确认：紧急释放「可以」；worktree「可以，只在本地的，如果是我的
fork或者我的个人项目，推送上去，以分支形式存在不就行了。我觉得worktree，除了当下正在用得，没啥不能推送
上去的吧」；长期做法写成 `$devcloud-storage-upkeep` 技能。**底线（owner 2026-11-04 原话）**：
「如果是正在用的业务和 Agent 一定不要影响他们使用。这个是底线，我们要清理的那些是没有正在用的。」
按恢复契约分三类：

- **go-build 与 golangci-lint 每小时只删 24 小时以上没用过的条目
  （2026-11-04 建立，2026-11-14 扩展共享 lint 缓存）**：owner 原话：
  「我觉得有意义啊，可以考虑加上，而且落地为仓库的规则。不然这个点确实会很坑。」
  为什么是 24h、为什么缓存键会让旧条目永不命中、Go 的 1 小时 mtime 刷新依据，见
  设计文档 3.1 与「经验教训」。2026-11-14 owner 批准 24 小时（每日检查上限 100 GiB、
  最老条目阈值 1520 分钟），保留日内与隔夜复用，同时清掉超过 24 小时未用的冷条目。
  只删名字匹配 `^[0-9a-f]{64}-[ad]$` 的普通文件；目录、README、trim.txt、
  testexpire.txt、fuzz 和符号链接不动。脚本 `scripts/go_cache_trim.py`，默认预演、`--execute` 才删。
  Go 根取 `go env GOCACHE` 的 realpath；lint 根取 `readlink -f /root/.cache/golangci-lint`。
  README 第一行分别必须为 `This directory holds cached build artifacts from the Go build system.` 和
  `This directory holds cached build artifacts from golangci-lint.`，不符则该缓存不删并报错。
  两者复用同一 24h、哈希普通文件、桶和末端复查规则。pnpm 每周 owner prune 见下条；npm 既有策略不变。
  原 30/10/20 GiB 整体重置作废。回归：
  `test_old_go_cache_entries_are_deleted`、`test_recent_go_cache_entries_are_kept`、
  `test_non_matching_go_cache_names_are_kept`、`test_go_cache_symlink_is_kept_and_target_is_untouched`、
  `test_missing_go_cache_readme_deletes_nothing_and_exits_1`、`test_go_cache_dry_run_deletes_nothing`、
  `test_go_cache_quiet_is_silent_when_nothing_is_deleted`。
- **Docker 残留由 `docker_leftovers.py` 清（每小时定时任务）**，清理满 1 小时的匿名测试 DB 卷、
  满 24 小时的悬空镜像，以及下述带标签镜像和 BuildKit 缓存（2026-11-14 owner 批准扩展）。
  执行时在 `/root/.local/share/fork-maintenance/state/run.lock` 上非阻塞 flock，
  拿不到锁本轮跳过；删卷前先排除
  `/data/metrics/ai-storage-maintenance/local-data-modes.json` 中 `retained_volumes` 列出的卷。
  匿名测试 DB 卷的证据链见本节「匿名测试 DB 卷满 1 小时即清」。
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
- **2026-11-14 owner 批准：worktree 删除及每轮补扫 BB 环境**：删除成功后仍记录 SHA、归档 ref 和恢复命令
  `git fetch <url> <ref> && git worktree add <path> <sha>`，删除 hook 只匹配 `path` 精确全等的未销毁环境。
  每轮 `--execute` 还枚举全部 `status != destroyed`、路径已不存在的环境，调用
  `bb environment delete <id> --json`。每轮每 ID 仅尝试一次；有未归档线程等被拒时保留、记录原因、不重试、
  不令脚本失败。线程归档后由后续轮次自然清理。dangling symlink 不算路径不存在；权限未知则不删该环境。
  BB 查询或删除故障继续 fail-open 记账，不阻断已授权的 worktree 删除。经核查插件，路径不存在时只清环境记录及
  attempt 标记，绝不删除 Git 分支/ref。预演不调用 delete。
- **2026-11-14 owner 批准：tmp 独立 clone 每天 06:45 清理**：`tmp_clone_sweep.py` 只处理
  `/data/system-tmp`（即 `/tmp`）及 `/var/tmp` 的顶层独立 clone（`.git` 为真实目录，非 linked worktree）。
  必须同时满足：无 `/proc` cwd/exe/fd/maps 引用、无容器挂载、无未销毁 BB 环境路径落在其中，探针未知即保留；
  除 `.git` 外每个条目最新 mtime 已满 24h；status 含全部 untracked 为空、无 stash、worktree list 仅自身、无嵌套 `.git`；
  `/data/code` 及两 tmp 根的其他仓库（含 bare）无 remote URL 或 objects/info/alternates 引用；ignored 无凭据类文件。
  每个本地分支、HEAD、tag 均经实时 `git ls-remote` 证明；在待删 clone 内的本地 bare remote 不算恢复副本。
  缺失对象复用 worktree 归档器推同主机个人命名空间或内部 origin 的 `archive/tmp-clone/*`，精确核验 object SHA；
  tag 用同前缀 tag ref，fetch/push URL 分离时以核验过的 push URL 为准。绝不 force push，mono2 不推 GitHub；
  无可推位置、归档失败或只能压缩内容而不能保存原历史时保留。本批网络查询与归档结束后完整刷新引用，
  随后重核占用及本地全条件，中间不再发网络请求。只删物理根内的该目录，fd-based 删除不跟随符号链接；
  每删一个立即写 JSONL（URL、HEAD、归档 ref、bytes），保留项也记录原因。默认预演、`--execute` 才删。
- **2026-11-14 owner 批准：pnpm store 每周日 05:30 owner prune**：`pnpm_store_prune.py` 在
  `/data/code/mole-linux` 用 `pnpm store path` 得到当前版本路径，再取共同 store 根（当前 `/data/.pnpm-store`）。
  逐个存在的版本目录执行对应 pnpm 的 `--store-dir <共同根> store prune`；禁止把 `vN` 目录再当 store 根。
  本机实测选用 pnpm 9 → v3、pnpm 10 → v10、pnpm 12 → v11；工具通过现有 pnpm 或 `npx --yes pnpm@<major>`
  获取，先核版本及同一显式根下的 `store path` 精确匹配；工具不可得、格式未知或路径不符则跳过该版本并记原因。
  仅可能写 store 的 pnpm 子命令阻塞：install/i/ci/install-test、add/update/up/remove/fetch/import/dlx/rebuild、
  create/pipeline、store add/prune 等及其已核实别名（含 pnpx/pnx）；recursive/multi/m/pm 按内层命令判断。
  正确消费 pnpm/Node 前置取值选项与 `--`，不把选项值或脚本参数误当写命令；
  pnpm run/exec/start/脚本、npm、npx、yarn、vite 不阻塞，pnpm 子进程按自身命令判断。`/proc` 未知仍跳过，
  每次 prune 前再查一次。只调用 owner 命令，不直接递归删 store。逐版本 JSONL 记录前后字节及结果；真实 prune
  失败记账并继续其余版本，最终非零。默认预演、`--execute` 执行、`--quiet` 无动作静默。
- **共享缓存不是线程遗留**：`/data/bb-data/thread-storage/.shared-cache/` 是 lightspace 建立的共享 Go 和
  golangci-lint 缓存，`/root/.cache/go-build` 与 `/root/.cache/golangci-lint` 的 realpath 指向其中。
  只能由 `go_cache_trim.py` 按上述 24h 规则修剪；不得当作线程遗留删除或误报为待清线程目录。
- **数据类不自动删**：会话（codex、bb-data）、凭据、在用数据库、命名卷、备份与迁移副本只列给 owner 决定。

执行入口：`$devcloud-storage-upkeep` 的 `scripts/worktree_sweep.py`、`scripts/docker_leftovers.py`、
`scripts/go_cache_trim.py`、`scripts/tmp_clone_sweep.py`、`scripts/pnpm_store_prune.py`，默认预演、`--execute` 执行。回归：`tests/test_devcloud_storage_upkeep.py`（已合并删除、仅本地提交归档、WIP 快照、
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
- **dangling 镜像**：现行年龄与清理见上文 owner 2026-11-14 策略（24 小时、精确 ID、
  排除任何容器引用、保护标签）。只用精确 `docker image rm <id>`；禁止无过滤
  `docker image prune` / `system prune`。旧 14 天规则、`repo==<none>` 误判、
  `sha256:<hex>` 与裸 hex 比较导致复查失效的教训见设计文档「经验教训」。
- **BuildKit**：现行规则见上文（72h prune + 总量 10GB）。`until` 按 last-used；
  121 条都落在 14 天窗口内、只回收 130B 是正常结论，不得为凑数放宽门槛或改用创建年龄。
  新客户端列出 `--max-used-space` 但旧服务端拒绝时，只在明确的 v0.17 诊断下改用
  `--keep-storage 10GB`；其他错误不重试。owner 命令也要持锁、fresh 计划、末端证据，
  16:10 直调未持锁的回执不能计为收益；历史执行细节见设计文档。
- **命名 Go 缓存卷**：旧 `auto_clean.py` 的 14 天规则随执行器退役，不再有定时执行器。
  若 owner 重新授权，判据必须是“树内最新文件 mtime”，不能用目录自身 mtime；命名卷和
  retained 卷仍不自动删。
- **`docker system df`（含 `-v`）在本机稳定约 29s**（daemon 端开销，2026-09-17 实测
  29.0/28.9/29.1s）：每轮最多各取一次并落 `raw/.df_cache.json` 复用，禁止在循环里重复调用；
  测量回收量用 `df -h /data`。回归检查：单轮所有脚本对 `docker system df` 的调用 ≤2 次。
- 容器清理以标签为准：`mole.protect=true` / `devcloud.protect=true` 是显式保留标记；
  stopped、test-like 命名、无标签但在运行，都只是审查分类。Docker 不支持原地加标签——
  在 owning Compose 文件里加，且仅在服务 owner 批准中断后重建。
- **旧登记镜像规则**：`lighthouse-workflow-agent` 的 14 天旧标签规则随
  `auto_caovoqtwj9e` 于 2026-11-05 退役，不再有定时执行器；现行带标签镜像按上文
  2026-11-14 的 30 天规则。`dsh-*`、`tkc-*`、`hermes-m1a-*` 等未登记仓库仍不得套用；
  镜像不记录 last-used，不用 Created 冒充不活跃时间。历史原文见设计文档「决策记录」。

## 历史执行器与旧报告

- **历史 worktree 执行器**：旧 `auto_o3pwesztbpg` 及 fork-maintenance 09:00 闲置工作区
  清理已于 2026-11-05 退役。72 小时/30 天门槛、分支治理、reflog 事件时间、总锁竞争、
  引用树指纹、工蜂身份等实现细节只用于解读旧报告，已移到设计文档「决策记录」与
  「经验教训」；现行 worktree 规则见上文 owner 2026-11-04 策略和 `worktree_sweep.py`。
  主 checkout、活跃/排队会话、进程/共享依赖、未知 ignored 数据继续保护，这条不变。

## 数据库卷

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
- **`/tmp` 下带 `.git` 目录的独立 clone（非 worktree）→ 创建它的任务**：
  现由每日 tmp_clone_sweep 按 owner 策略节处理，不再仅依靠创建任务收尾。

## 包缓存

- `/data/.pnpm-store` 这类 package store 是 owner 管理的缓存，不是通用递归
  删除目标；按 owner 策略节逐版本调用 `pnpm store prune`，仅 pnpm store 写入命令阻塞，unknown 跳过；npm 和脚本进程不阻塞。

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
  `/var/tmp` 是系统盘普通目录；两根普通项整树闲置 3 天、结构缓存满 24h，自动清理由
  每日北京时间 03:30 cron 执行（crontab `# devcloud-clean-tmp` 行）：外层 timeout 1790 秒，
  顺序执行 `/usr/bin/python3 -B <执行器> clean-tmp --path <固定根> --execute --age-days 3 --json`，
  先 `/var/tmp`（内部预算 300 秒、timeout 325 秒），再 `/data/system-tmp`（内部预算 1400 秒、timeout 1435 秒），
  均 kill-after 10 秒，外层仍为 1790 秒。
  同一命令去掉 `--execute` 即预览，不接受其他路径。
  `systemd-private-*`（及已有 `systemd-*`）始终跳过。普通项保留期 3 天。唯一执行器为固定部署版
  `/data/tools/devcloud-maintenance/releases/cbb5caf329a61b57/bin/devcloud.py`；上一版
  `7ca528d7c5f3f004` 及 `df3e2b353d8e1c72`（仅 /tmp）、`f1e330c325c73fd0` 与
  `1c2d71d3ab21cc79` 保留作回滚。后续升级先验收新快照，再同步 cron 版本路径与每日
  DSH 检查，不执行共享工作区。
- **systemd-tmpfiles 自 2026-11-04 起不再按年龄清理 `/tmp`**：`/etc/tmpfiles.d/tmp.conf` 为
  `q /tmp 0700 root root -`（age 一栏为 `-`）。原 3d 规则逐个文件删除，曾把闲置 worktree 里的已跟踪
  文件删掉（32 个 /tmp worktree 中 7 个受损）；遇到孤立 FIFO 时还会卡死，10-30 起整个 tmpfiles 清理
  因此停过 4 天 20 小时。活体探针：`systemd-tmpfiles --cat-config | grep -E '^q /tmp '`。
- 升级必须新建快照：目录名 = `manifest.json` 规范化内容的 sha256 前 16 位，全部文件逐项
  记录 sha256 与 mode，目录/文件置只读；验收 = 清单复算 + 真实 preview，再改 cron 与
  每日 DSH 检查覆盖的根（改完回读 cron 和检查任务核对）。
  回归：`tests/test_devcloud_cleanup.py::test_policy_shards_cover_every_candidate_exactly_once`
  与 `::test_policy_shard_refusal_discards_the_whole_candidate_set`。
- 2026-11-04 的分片吞吐验证、容量统计口径修正和旧快照升级过程只用于解读历史报告，
  已移到设计文档「决策记录」和「经验教训」；现行预算、分片和容量规则见本节和
  `/data/tools/devcloud-maintenance/releases/cbb5caf329a61b57/` 的部署清单。
- 容量统计固定用 statvfs：总量 `f_blocks`、物理空闲 `f_bfree`、普通可用 `f_bavail`，
  保留差额为 `f_bfree - f_bavail`；同时输出物理已用与含保留块的占用百分比，不能混用分母。
  零、负或不一致容量拒绝；`used_percent` 兼容字段明确表示物理已用。
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
- **结构缓存按整目录闲置 24h**：仅两批准根的非符号链接直接物理子项可执行。普通项仍为 3 天；
  精确匹配的 Go build cache、pnpm store、Node compile cache 以树内最新普通文件 mtime 判 24h，不用根 mtime。
  Go 要求固定 README 首行、完整 00–ff 桶、对应哈希的 -a/-d 产物；pnpm 要求版本目录及 files、index/metadata
  布局（v3 内嵌 *-index.json，v11 可为 SQLite index.db），校验桶、文件、已知元数据；Node 要求官方
  版本–架构–V8 tag–uid 目录与 8 位十六进制文件。识别不出走原规则；混入仓库、凭据、未知内容或非空
  pnpm 部分安装树则保留。符号链接、socket、硬链接、foreign owner、外部可写内容、挂载、inotify 继续保留。
- **批量 /proc 快照与吞吐**：每批最多 128 项共用一次完整 PID/starttime/引用快照，未知整批拒绝。
  owner 2026-11-14 明确授权：隔离后和 Mole 最终 sink 逐项只增量检查新增或 starttime 变化的 PID；
  既有 PID 的可变引用不重扫。所有新增探针 unknown 仍拒绝；每项删除前重核结构、年龄、物理身份与完整指纹，
  仍经 Mole dry-run 和最终 guard。最多 8 项并发；取消信号停止排队、终止 Mole 子进程组、恢复在途隔离项再退出，
  遗留隔离项阻止下一轮。完整计划扫描上限 240 秒，单普通树 5 秒、结构缓存树 60 秒；不设数量上限，余项递延。
  读取 /proc 时进程退出窗口错误只在有界复核证实消失、僵尸或 PID 重用时视为退出，证据仍不全则拒绝。
- tmp 独立 clone 由每日 06:45 的 `tmp_clone_sweep.py` 处理，完整恢复条件见 owner 策略节；
  worktree 清理每轮另补扫缺失 BB 环境，pnpm 每周日 05:30 逐版本 owner prune，均沿用该节规则。
  `/data/bb-data/thread-storage/.shared-cache/` 是共享缓存，不在本段普通临时项或线程遗留范围。
- 每日账本 `/data/metrics/devcloud-clean-tmp.log`：intent/deleted 事件与每根各一条
  summary 分开，`path` 区分两个根；错误日志为同名 `.stderr.log`。summary 记录两盘前后容量和
  部分失败，失败非零；summary 增记 cache_roots（路径、类型、allocated bytes、
  最新文件 mtime）及 deleted_bytes。logrotate 每日检查，保留 14 份/最长 30 天、
  5 MiB 检查阈值，不截断活跃日志。回归：`tests/test_devcloud_cleanup.py`；本机
  挂载与真实 Mole 路由另以专属测试文件验收，结果入 `storage-audit.log`。
- 每日 DSH 检查只读两根 summary、比较容量并解释增长，必要时预览同一执行器；
  没有第二套删除流程，也不执行迁移。当前告警线是 `/` 与 `/data` 普通可用都不少于
  150 GiB；旧的 70% 报告线见设计文档「决策记录」。/tmp 清理释放数据盘，/var/tmp 释放系统盘。
  `/etc/tmpfiles.d/tmp.conf` 继续遮蔽系统默认 `/var/tmp` 30 天规则，禁止恢复逐文件年龄清理。
  每日 DSH 检查第 3 项分别核对两个根最近一次 03:30 后的 summary，均需 complete/ok、failed=0。
- `/data/tmp` 和其他测试产物需要结合 owner、持久数据及活动证据逐项审查；
  年龄和无进程引用不足以授权删除；目录叫 `tmp` 不构成可弃证据。

### 历史 worktree 巡检规则（仅供解读旧报告）

旧 `patrol_worktrees.py --execute` 和 `cleanup_worktrees.py` 的流程已退役；7 天/分支政策、
工蜂 MR 核验、执行器 token、`--sweep-only` 等细节只用于解读旧报告，已移到设计文档
「决策记录」和「经验教训」。现行入口是 `worktree_sweep.py`，规则见上文 owner 2026-11-04
策略；旧报告不构成删除授权。

- 含 submodule 的工作树不进入自动执行：git 会拒绝非 `--force` 的 `worktree remove`。
  不得改用 `--force`，也不得先 `git submodule deinit`——后者要先改动工作树，属 owner 决策。

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
