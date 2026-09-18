# Mole Linux / DevCloud Storage Vision

## 目标

这是一台经常因为开发环境、Docker、worktree、日志、数据库和包管理器缓存而满盘的 Linux DevCloud。工具要先找出真正占用空间的来源，再给出可验证的清理建议；默认只读，不自动删除（唯一例外：/tmp 每日自动清理，见下文 2026-09-01 节）。

落地状态见 [本机存储维护](docs/DEVCLOUD_AUTOMATION.md)：写入位置一次性配置并验收，现有每日清理已改用固定部署快照，每周复盘读取每日记录并解释增长。迁移不进定时任务，不引入通用策略框架；运行验收与日志位置以文档为准。

## 设计原则

- 借鉴 Mole：分析优先、候选可解释、风险分级、执行前预览、拒绝不确定的删除。
- Linux 优先：识别 ext4/xfs 挂载、Docker overlay2/volume、开发机缓存、临时目录、worktree 和日志。
- 数据库 binlog、PostgreSQL/MySQL 数据、Qdrant/MinIO、活动容器和源码依赖默认高风险，只报告不直接清理。
- 可重建产物（旧 worktree 的构建目录、过期测试输出、包缓存）只作为 reviewable candidate，并给出 owner 命令或人工确认要求。
- 不扫描或删除 `/proc`、`/sys`、`/dev`、活动容器挂载层，不把 Docker overlay2 的重复计数当成真实可回收空间。

## 当前最重要的调查方向（2026-09-01 更新）

上一轮（2026-08-25）列的方向已全部闭环：MySQL binlog 已按 retention 收敛、story42 等测试容器/卷已清、dangling images 与 BuildKit 已 prune、trpc.log.bk-* 已按轮转策略清理、/tmp 改为工具化自动清理。当前未决候选（数字为 2026-09-01 实测，处置前需 owner 逐项批准）：

1. `/data/cache-migrated` 约 32G（迁移来的旧 home 缓存：.cache 23G、.npm 7.3G），需拆解后逐项走 owner 命令。
2. `/data/.pnpm-store` 约 16G，确认无活跃包任务后 `pnpm store prune`。
3. `/data/root-dev-cache` 约 12G（go-pkg-mod 8.6G），owner 命令 `go clean -modcache`。
4. `/data/codex` 约 29G：codex-home 15G 为活跃状态不动；asm-p3-snapshot 3.1G、db-backups 1.1G 待 owner 确认。
5. `saienscreditsystemrails_postgres_data` 数据卷 4.8G：数据库卷高风险，仅 owner 决策项目死活。
6. `/root` 的 AI 工具堆约 8G（.acpx 1.3G、.kiro-server 1G、.tokenjuice 981M、.codebuddy 890M、.pulumi 761M、.zcode 736M、.copilot 717M、.chromium-snapshots 661M、.codexl 574M）：多数陈旧但属 AI 工具状态，卸载前跑 `./mo devcloud who-uses` 并逐项确认；.pulumi 可能含 stack state/凭据。
7. pyenv 3.12 的 CUDA 栈约 4.6G：lighthouse 的 kb_vector_service.py 常驻依赖（见 storage-rules.md「Python 站点包」），保留，除非该服务下线。

## 2026-09-01 盘面与策略变化

- `/tmp` 即 `/data/system-tmp` 的 bind mount；每日 03:30 cron 自动清理 7 天前条目（`clean-tmp` 工具，自带活进程扫描与静态保护名单），账本在 `/data/metrics/devcloud-clean-tmp.log`。首轮释放 106.3G。
- Docker 同日 prune：builder 22.9G、无引用镜像 9.0G、story42 全套容器与卷；`/data` 83% → 62%（可用 201G）。
- `/` 91% → 76%（可用 25G）：删除 node v22（claude 已迁 v24）、opencode 全套、pyenv 3.6.8、旧 IDE server 构建、android-sdk、opentofu、.antigravity-server。
- 新工具：`./mo devcloud who-uses <路径>` 删除/卸载前的活进程依赖探针（exit 1=有引用必须停）；CUDA 栈因该探针发现 kb_vector_service 依赖而保留。

## 2026-08-25 新增证据与规则

- E2E MySQL binlog 已通过 `PURGE BINARY LOGS` 清理，volume 从约 72GB 降至约 739MB，`/data` 从 99.8% 降至约 87%。
- E2E 容器的 binlog 保留期已通过 `SET PERSIST` 固定为 86400 秒；后续 `run-e2e.sh` 和 `run-gate.sh` 会在精确容器名匹配时自动复核并清理过期 binlog。
- 当前 Docker volume、数据库和活动容器继续保持高风险；只允许 owner 命令，不允许路径级递归删除。
- `/data/.pnpm-store`、`/data/tmp`、`/data/anydev`、worktree 和 `/var/log` 是下一轮重点候选，但需要分别验证 active process、缓存 owner、文件年龄和日志轮转策略。
- 新增候选：Docker 有大量 `<none>:<none>` dangling images，单层虚拟大小约 1.2–1.3GB；这些大小不能相加，必须使用 Docker image prune 的 owner 语义和 age filter。
- `/data/anydev/agent/logs` 约 1.6GB，其中多个 `trpc.log.bk-*` 约 200MB 且创建于 2026-03/04；当前 `trpc.log` 约 198MB，旧备份可作为日志轮转候选，但需确认进程与 AnyDev 日志策略。
- 复核发现 `/data` 的 `df` 已用约 480,980,152,320 字节（约 448GiB），而普通可见目录仅约 20GB；`/data/docker/lib/overlay2` 的递归 `du` 在 60 秒内超时。工具必须把“账面-可见差额”单独列为 Docker/挂载层调查项，不能把超时误报成低占用。
- dangling image 复核：共 72 个，4 个仍被容器引用（`platform-platform-1`、两个 clawsweeper identity debug 容器、`itfs`），68 个未发现容器引用。前者不能纳入统一清理；后者也只能按创建时间和构建/回滚 owner 复核后使用 Docker prune，72 个的虚拟 size 不能相加为可回收空间。
- 容器治理新增显式保护契约：`mole.protect=true` 或 `devcloud.protect=true` 表示长期服务禁止清理；未标记容器只进入候选盘点，不自动等同于安全删除。现有容器不能原地添加 Docker label，必须修改所属 Compose 文件并经 owner 同意后重建。
- 当前盘点的长期服务候选包括 `developer-reports`、`lighttools`、`itfs`、`kb-agent-access`、`platform`；测试/临时候选包括四组 `beta-data-clone-test-*`、三个 `immutable-evidence-harness-*`、`lighthouse-ailab-e2e-mysql`、`agent-ux-preview-pg-*`、`clawsweeper` debug/serialization、已退出的 migrate/init/import 容器。以上是证据分类，不是删除授权；`platform` 与 `kb-agent-access` 共用 Compose 文件且可能是新旧并行部署，必须先确认保留哪一套。
- 用户决策已确认：保留 `kb-agent-access`；`platform`、`saiens_postgres`、`kb-vector-qdrant`、`echo-web` 不属于长期服务。共享 KB Compose 已加入 `MOLE_PROTECT` 标签开关，只有以 `MOLE_PROTECT=true` 重建保留项目才会写入 `mole.protect=true`；当前运行中的容器尚未重建。
- 2026-08-25 已执行清理：删除旧 `platform`、测试/预览/E2E、clawsweeper debug/state、已停止一次性容器，共保留 13 个长期服务容器；删除对应测试 volume、3.06GB 未引用 pnpm cache volume，并移除确认无当前容器引用的 E2E/测试镜像。
- 清理前 `docker system df` 显示 BuildKit cache 40.75GB 且全部可回收；执行 `docker builder prune -af` 后 `/data` 从 440GiB used 降至 428GiB used，使用率从 85% 降至 83%。最终容器 13 个、镜像 39 个、dangling image 1 个、volume 24 个；`saiens_postgres` 数据卷和未确认的持久化数据保留。
- 全量一级目录扫描发现主要空间并不在当前容器：`/data/cache-migrated` 30G（其中旧 Git/cache pack）、`/data/codex` 29G（`codex-home` 18G、backups 9.7G）、`/data/itfs` 11G（活动 ITFS cache）、`/data/root-dev-cache` 11G、`/data/DescribeInstances-Logs` 5G（单个 2025-12 ndjson）、`/data/kb-vector` 3G（Qdrant snapshots 2.8G）、`/data/code/openclaw` 60G、`/data/code/DeepSeek-TUI` 19G。
- 深拆出的高优先级候选：`/data/cache-migrated/.cache` 23G、`.npm` 7.3G；`openclaw/openclaw/.pnpm-store` 12G、`.worktrees` 7.4G；`discord-store/.git` 14G；`DeepSeek-TUI/lane1/target` 16G；`itfs/cache` 11G；`root-dev-cache/go-pkg-mod` 8.6G。缓存和 Rust target 可按 owner 命令重建，Git 历史、ITFS cache、Qdrant snapshots、Codex backups 不能按目录名直接删除。
- 深度扫描新增：`/data/.Trash-0` 5.6G，包含旧 worktree、测试产物和安装包；`/data/system-tmp` 超过 1,038,000 个文件、约 2.45G apparent data。Trash 是下一步最高置信度候选，system-tmp 先按年龄和进程归属分层，避免因文件数量大而误删。
- 新增 deleted-open 检查：发现 12 个约运行 24 天的 Chrome/Cuprite 进程，仍持有约 12MB 已删除文件；这会造成小额泄漏，已纳入工具输出，但不是当前百 GB 级差额的主因。
- 用户已确认删除 `/data/code/DeepSeek-TUI` 和 `/data/.Trash-0`；执行前确认二者均为 `/data` 上的普通目录且不是挂载点或符号链接。删除后 `/data` 从 393G used / 76% 降至 371G used / 72%，实际释放约 22G。

## 命令契约

```bash
./mo devcloud
./mo devcloud --json
./mo status --json
./mo analyze --json /data
```

上述命令只读。任何清理命令必须单独设计 dry-run、owner/process 检查、精确路径证明和可回滚/可恢复策略。
