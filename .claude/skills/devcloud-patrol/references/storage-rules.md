# DevCloud 存储清理规则

本机存储调查沉淀的持久规则，按目标分组，是磁盘侧分类与清理的唯一规则源。
新规则先写进当轮 `storage-audit.log`，跨运行成立并通过 AGENTS.md DevCloud
Audit Ledger 的五问后晋升到这里；每条晋升规则要附回归检查，或写明为何只能
做活体只读探针。

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

## 数据库卷

- **owner 授权（2026-09-17）：无主匿名 DB 卷按 7 天保留期自动清理。** 唯一执行器
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
- **回复格式统一契约**：`devcloud-patrol` 下所有自动化（DB / worktree / 每周复盘）的 owner 回复形状
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

- `/tmp` 即 `/data/system-tmp` 的 bind mount（数据盘），既有自动清理授权由
  每日北京时间 03:30 cron 执行，保留期至少 7 天。唯一执行器为固定部署版
  `/data/tools/devcloud-maintenance/releases/84ffa318d2107e59/bin/devcloud.py`（2026-09-17
  策略分片修复版；上一版 `cf2bbbefdbdee7af` 保留作回滚），用
  `/usr/bin/python3 -B <执行器> clean-tmp --age-days 7 --json` 预览，加 `--execute`
  执行；后续升级先验收新快照，再同步每日/每周版本路径，不执行共享工作区。
- 升级必须新建快照：目录名 = `manifest.json` 规范化内容的 sha256 前 16 位，43 个文件逐项
  记录 sha256 与 mode，目录/文件置只读；验收 = 清单复算 + 真实 preview，再改 cron 与
  每周 BB 任务里的版本路径（两处都要改，改完 `grep releases/` 核对）。
  回归：`tests/test_devcloud_cleanup.py::test_policy_shards_cover_every_candidate_exactly_once`
  与 `::test_policy_shard_refusal_discards_the_whole_candidate_set`。
- 策略阶段逐候选约 50ms（`validate_path_for_deletion` ~25ms + `should_protect_path` ~17ms），
  候选数上千后**禁止**用「按大小取前缀」省时间：绝大多数候选会被策略拒绝，取前缀会让允许项
  永远排不进来。只能分片并行（`POLICY_SHARDS`），任一分支片失败或超时必须整轮 fail-closed，
  不得接受部分结果。
- 清理器要求数据盘和别名身份正确，根及祖先为 root 所有且不对其他用户可写。
  完整扫描检查 cwd/exe/fd/maps、绝对 argv、各 mount namespace 和 inotify；
  证据未知则拒绝。内部有新文件、仓库、凭据、备份、数据库、socket、symlink、
  硬链接、挂载或外部可写文件的树保留。Mole 保护与用户 whitelist 同时约束
  原路径、/tmp 别名和删除入口，不允许隔离路径绕开保护。
- 每轮扫描最多 120 秒/单树 5 秒，按大小最多选 50 项，执行预算 300 秒。
  单实例锁覆盖整轮；隔离后再核对对象引用与完整指纹，Mole dry-run 和最终
  sink guard 均通过后删除；失败尝试恢复，原名已占用时保留原件并报告位置。
  遗留隔离项会阻止下一轮执行，只可人工核实，不能当过期缓存清理。
- 每日账本 `/data/metrics/devcloud-clean-tmp.log`：intent/deleted 事件与唯一
  summary 分开；错误日志为同名 `.stderr.log`。summary 记录两盘前后容量和
  部分失败，失败非零。logrotate 每日检查，保留 14 份/最长 30 天、5 MiB
  检查阈值，不截断活跃日志。回归：`tests/test_devcloud_cleanup.py`；本机
  挂载与真实 Mole 路由另以专属测试文件验收，结果入 `storage-audit.log`。
- 每周 BB 任务只读每日 summary、比较容量并解释增长，必要时预览同一执行器；
  没有第二套 14 天删除流程，也不执行迁移。系统盘实际使用率达到 70% 时
  突出报告；/tmp 清理只释放数据盘，不能声称达到系统盘降占用目标。
- `/data/tmp` 和其他测试产物需要结合 owner、持久数据及活动证据逐项审查；
  年龄和无进程引用不足以授权删除；目录叫 `tmp` 不构成可弃证据。
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
  的 `--dry-run --min-no-pr-age-hours 168 --write-approval-report …` 后接
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
  查询 `mr list <project> --state <merged|opened|closed> --source-branch <branch>`；
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
