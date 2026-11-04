# Worktree 巡检报告格式（自动化输出规范）

本文件仅用于阅读旧 `devcloud-patrol` 巡检报告；原 BB 自动化「Worktree 巡检与安全清理」已退役。
规则来源见 [storage-rules.md](storage-rules.md)；这里只定义**每轮必须产出的形状**，
不重复判定规则。目标：owner 一眼看到「清了什么、多大、干什么用的」和
「≥7 天为什么没清、涉及哪个 PR/MR、多大」，不必重读调查过程。

## 落盘位置

| 产物 | 路径 |
|---|---|
| 本轮目录 | `/data/metrics/ai-storage-maintenance/<YYYY-MM-DD>-worktree-patrol/` |
| 决策报告 | 该目录 `report.md`（脚本渲染）+ 回复正文 + `summary.json`（**不产出 HTML**，owner 2026-09-18 决定） |
| 同轮状态 | 该目录 `run-status.json`（含 mode/run_id/round_dir；只读预览不覆盖全局执行轮状态） |
| 原始证据 | 该目录 `evidence.json`（逐对象）、`summary.json`（一屏汇总） |
| 账本小节 | 该目录 `audit_section.md`（由脚本生成，可整体追加） |
| 采集原始件 | 该目录 `raw/`（owner 工具 JSON、who-uses、执行回报、state 快照） |
| 增量状态 | `/data/metrics/ai-storage-maintenance/worktree-state/state.json`（跨轮持久，不进报告目录） |
| 仓库镜像 | `/data/code/mole-linux/ai-storage-maintenance/<同名目录>/` |
| 账本 | `/data/code/mole-linux/storage-audit.log` 追加一节（见末节） |

`<YYYY-MM-DD>` 用采集当天（Asia/Shanghai）。同名目录已存在时**不要覆盖**：脚本会自动改写到
`<YYYY-MM-DD>-worktree-patrol-<HHMMSS-微秒>/`，并另起账本小节。

**所有自动化一律不产出 HTML**（owner 2026-09-18）；需要图文材料时由 owner 单独一次性要求，
不写回流程默认值。

## 执行入口（脚本优先）

采集、增量比对、判定、预聚合、记账全部由脚本完成；Agent **不要**自己重写这些步骤：

```bash
python3 -u /data/code/mole-linux/.claude/skills/devcloud-patrol/scripts/patrol_worktrees.py \
    --worktree-tool <已加载的 worktree-cleanup Skill 根>/scripts/cleanup_worktrees.py \
    [--execute] [--append-audit]
```

调用方先按名称加载 `worktree-cleanup` 并将解析出的入口显式传入；脚本不猜测其安装 scope。
脚本产出 `report.md` / `evidence.json` / `summary.json` / `run-status.json` / `audit_section.md` 与 `raw/`，
并输出紧凑 stdout 摘要。Agent 优先直接使用 `report.md`；需要核对时读取同轮 JSON。
`summary.projects[]`、`needs_decision_count` 和 `evidence.needs_decision[]` 已由脚本聚合，
Agent 不再按 host 或中文原因自行推导分类。

- 默认只读；`--execute` 才调用 owner 工具执行满足全部条件的项（常设授权）。
- `--execute` 仍受 owner 工具在 sink 前的指纹复验约束；脚本不提供 `--force`、`rm -rf`、`git clean`；分支删除仅由 owner 执行器按远端证明处理。
- 脚本被调用方截断（如管道接 `head`）不影响产物：账本与报告在打印前已落盘。

## 历史周报轮（已退役）

旧 BB 自动化「Worktree 巡检与安全清理」（`auto_jxt6wclgaxi`）曾只读本轮产物，按
[automation-report-format.md](automation-report-format.md) 渲染**完整表格**（正常轮也出全表，
owner 2026-09-20）；不重跑脚本、不调用清理执行器、不执行删除。为产出「按项目分类」的
项目明细表，该轮允许读 `summary.json` 与 `evidence.json` 的对象级字段做聚合；只读结构化字段，
不读原始 diff。
本轮通过仓库镜像根的 `.current_round` 定位；读取该目录 `summary.json` 的 `round_dir` 指向的原始目录。
报告、证据、同轮状态的 `run_id` 必须一致。不得按日期通配取最新目录再混用全局执行状态或旧 stdout。
`mode=dry-run` 标明「只读预览」，不能说静默清理刚完成；全局 `worktree-state/run-status.json`
仍专门反映最近执行轮的健康状态。
历史日志和旧轮错误计数不等于当前故障，归因需对应轮次的具体错误证据。
本机独立健康检查 cron 直接运行工作区文件，提交状态不是部署或生效依据。

## 增量状态账本

`/data/metrics/ai-storage-maintenance/worktree-state/state.json`（`--state` 可覆盖），version 1：

```json
{"version":1,"updated_at":"...","history":[{"date":"...","run_id":"...",
 "executed_bytes":0,"count":0,"items":[]}],
 "worktrees":{"<path>":{"repo":"...","fingerprint":"sha256:...","head":"...","branch":"...",
   "detached":false,"locked":false,"prunable":false,"dirty":false,"age_hours":0,
   "size_bytes":0,"artifact_bytes":0,"artifact_hits":[],"size_source":"cached|measured",
   "size_measured_at":"...","last_active_at":"...","who_uses":{"count":0,"at":"..."},
   "first_seen":"...","last_seen":"..."}}}
```

状态保留身份、历史回收和健康去重。Git 状态/ignored 根集合不能证明树内内容未变，
所以 `size_bytes`、`artifact_bytes`、`last_active_at` 每轮重新测量，失败记 `null`，不复用旧值冒充本轮测量。
`who_uses` 每轮重探，执行前再次刷新；非零和未知都保留。只读预览不更新持久状态、健康通知去重或全局执行状态。

远端查询（PR merged / 远端分支 tip / 含于主干）**每轮都做**，不进缓存：本地怎么变都测不出远端状态，
而它正是能否回收的决定量。同理，`age` 每轮按 `.git` mtime 重算——跨过 168h 是纯时钟事件。

## 回复结构（2026-09-20：减少重复）

正常报告只保留**一段汇总 + 一张按项目分组的明细表 + 证据链接**，由 `report.md` 直接渲染。

- 开头：采集时间、run_id、执行/预览模式；已清理 / 不清理 / 需决策计数只汇总一次。
  同段给 ≥7 天未清理空间和 <7 天保留数量，不在结尾再重复。
- 唯一明细表：`项目 | 工作树 | 结果 | 空间 | 年龄 | 依据 / 待办`。
  按项目升序、组内空间降序；相邻同项目用 `↳` 延续。工作树用短名称链接到完整路径。
  已清理、保留和需决策对象共用该表，每个对象只出现一次。
- 保留原因与远端核验合并到最后一列，相同事实不重复。已核验 PR/MR 保留可点击链接。
  需决策和执行失败的具体下一步直接写在该行；普通保留项不重复“保留、下轮重评”。
- 不再另列项目汇总、已清理空表、需决策表、下一步表、正常健康表、安全边界表。
  上次活跃、领先/落后、dirty 细目、累计回收、基础设施等完整字段留在 JSON，按需展开。
- 真实运行异常用表前短条目说明，正常项不逐项报平安。<7 天只给汇总；年龄不可得单独计数并在同表列项。
- ≥7 天明细通常最多 30 项，所有需决策项必须保留；已执行和年龄未知项也完整展示。
  被截断的项只给数量与完整证据链接，不再补一张表。

平台 `remote_host`、核验事实 `remote_status`、阻塞 `reasons[]` 与 `needs_decision` 保持分开。
“非 GitHub host”不是保留原因；本地条件导致远端未查时，不写“远端不可证”。
`needs_decision` 只计需要人选择处置方式的关联歧义、submodule；未找到 MR、HEAD 不匹配、查询失败按规则保留。
`retained_count + needs_decision_count = unreclaimed_over_7d_count`，计数按完整集合，不按展示行数。

### 工蜂（git.woa.com）核验契约

- 凭据只由脚本解析：优先已导出的 `GONGFENG_PRIVATE_TOKEN`，否则读 owner 维护的
  `/data/code/lighthouse/developer/.env`（要求 root 所有且组/其他不可读，否则拒绝并记
  `凭据缺失`，不降级、不猜测）。**token 只经 `env=` 传给子进程，不进 argv、不进报告/证据/账本/日志、
  不进 Agent 上下文**；Agent 不得自行 `cat`/`grep` 该文件。
- 查询仅路由 `git.woa.com`：`gongfengcli mr list <project> --state <opened|merged|closed> --source-branch <branch> --all`。
  先查 opened，防止复用分支的旧 merged MR 遮蔽在办 MR；merged 中优先与当前完整 HEAD 全等的记录。
  **`--state all` 在 0.1.0 上恒返回 0 条（实测），禁止使用**；必须逐状态查询全部分页并在本地校验
  `source_branch` 完全相等（避免同名前缀误配）。
- MR 链接用 `https://git.woa.com/<namespace>/<project>/-/merge_requests/<iid>`（实测 200；
  不带 `/-/` 的旧式路径是 302）。
- 判定沿用 owner 三条件：`state=merged` 且 `source_commit` 与本地完整 40 位 HEAD 全等 → 精确 head；
  仅 merged 但 head 不等 → 非精确 head，保留。
- **执行契约（2026-09-18 起）**：核验结果由脚本按 repo 写成
  `raw/owner_tool/<slug>.gongfeng-evidence.json`（version 1，逐项含
  `path/branch/head/source_commit/mr_iid/mr_url/target_branch` 与完整 40 位对象名），
  交给 `cleanup_worktrees.py --remote-evidence`。该模式**不调用 `gh`**，只接受与本地
  全等、`state=merged`、age ≥168h 的记录；`--min-no-pr-age-hours < 168` 直接拒绝。
  仍是 `--dry-run --write-approval-report` → `--apply --approve-report` 两跳，
  内部 `git worktree remove`（非 `--force`）、按远端证明删除分支、`.local` 备份、apply 前指纹复验。

## JSON 字段

`evidence.json` 顶层固定：`summary`、`objects[]`、`executed[]`、`unreclaimed[]`。
`objects[]` 每项至少含：`path`、`repo`、`age_hours`、`size_bytes`、`last_active_at`、`dirty`、
`dirty_summary`、`locked`、`prunable`、`head`、`branch`、`remote_proof`、`bb_environment`、
`who_uses`、`remote_host`、`remote_status`、`decision`、`reason`、`reasons`、`needs_decision`、`next_step`（未进入 ≥7 天判定的主 checkout / 未到期项允许省略判定字段）。`executed[]` / `unreclaimed[]` /
`verified_reclaimable[]` 同样带 `last_active_at`（不可得为 `null`）。
`dirty_summary` 是 `{tracked, untracked, files_changed, insertions, deletions}`，只统计数量与
diffstat、不读内容；dirty 项的 JSON 保留 `18F +421/-41 · 1 untracked` 口径的
摘要，供需要时查看，正文不逐项展开。

`summary.json` 至少含：`collected_at`、`scope`、`mode`、`registered_entries`、
`root_used_pct`、`data_used_pct`、`cleaner_status`、`min_age_hours`、
`eligible_count`、`executed_count`、`executed_bytes`（仅本轮新增执行）、
`unreclaimed_over_7d_count`、`unreclaimed_over_7d_bytes`、`unreclaimed_by_reason`、
`held_under7_count`、`held_under7_bytes`、`cumulative_reclaimed_bytes`、`cumulative_since`。

工蜂执行路径另加四个字段（2026-09-18 起）：`gongfeng_eligible_count`（精确 head 且年龄/工作区/BB/引用/submodule 检查通过的项数）、`gongfeng_executed_count` / `gongfeng_executed_bytes`（其中本轮经
`--remote-evidence` 实际执行的项数与表观字节）、`verified_reclaimable_remaining_count`
（核验后仍未执行的剩余项数，不要求另起表格）。

字段语义不许混用，这三组必须分开：

| 字段 | 含义 |
|---|---|
| `eligible_count` | 本轮通过远端证明和巡检共同条件的项数；排除未到期、未知状态、BB/进程引用、submodule；sink 仍需复验。原始 owner 工具候选另记 `owner_eligible_count` |
| `executed_count` / `executed_bytes` | **仅本轮新增执行**；同日早先轮次的执行不得计入，只在账本与 `history` 里 |
| `unreclaimed_over_7d_*` | ≥7 天未清理集合（**只含有磁盘实体的项**）；`prunable_registrations` 另计无实体登记项 |

字段名一经写入即视为契约：改字段要同时改本文件并在账本记一行。

## 账本

每轮向 `storage-audit.log` 追加一节，至少含：日期、触发来源与线程、采集时间窗、
规则版本（当前 7 天门槛）、证据命令、**已执行**（逐项路径/HEAD/PR/字节/失败数）、
**未执行**（按原因分组与合计）、基础设施健康、rule candidates、报告路径。
已执行与建议必须分开写，不允许用一句「清理若干」概括。
