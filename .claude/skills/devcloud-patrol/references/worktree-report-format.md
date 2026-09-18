# Worktree 巡检报告格式（自动化输出规范）

本文件是 `devcloud-patrol` 与 BB 自动化「Worktree 巡检与安全清理」的输出契约。
规则来源见 [storage-rules.md](storage-rules.md)；这里只定义**每轮必须产出的形状**，
不重复判定规则。目标：owner 一眼看到「清了什么、多大、干什么用的」和
「≥7 天为什么没清、涉及哪个 PR/MR、多大」，不必重读调查过程。

## 落盘位置

| 产物 | 路径 |
|---|---|
| 本轮目录 | `/data/metrics/ai-storage-maintenance/<YYYY-MM-DD>-worktree-patrol/` |
| 决策报告 | 回复正文 + 该目录 `summary.json`（**不产出 HTML**，owner 2026-09-18 决定） |
| 原始证据 | 该目录 `evidence.json`（逐对象）、`summary.json`（一屏汇总） |
| 账本小节 | 该目录 `audit_section.md`（由脚本生成，可整体追加） |
| 采集原始件 | 该目录 `raw/`（owner 工具 JSON、who-uses、执行回报、state 快照） |
| 增量状态 | `/data/metrics/ai-storage-maintenance/worktree-state/state.json`（跨轮持久，不进报告目录） |
| 仓库镜像 | `/data/code/mole-linux/ai-storage-maintenance/<同名目录>/` |
| 账本 | `/data/code/mole-linux/storage-audit.log` 追加一节（见末节） |

`<YYYY-MM-DD>` 用采集当天（Asia/Shanghai）。同名目录已存在时**不要覆盖**：脚本会自动改写到
`<YYYY-MM-DD>-worktree-patrol-<HHMM>/`，并另起账本小节。

**所有自动化一律不产出 HTML**（owner 2026-09-18）；需要图文材料时由 owner 单独一次性要求，
不写回流程默认值。

## 执行入口（脚本优先）

采集、增量比对、判定、预聚合、记账全部由脚本完成；Agent **不要**自己重写这些步骤：

```bash
python3 -u /data/code/mole-linux/.claude/skills/devcloud-patrol/scripts/patrol_worktrees.py \
    [--execute] [--append-audit]
```

脚本产出 `evidence.json` / `summary.json` / `audit_section.md` 与 `raw/`，
并把一段**紧凑摘要**打到 stdout（登记数、本轮执行、≥7 天按原因分组、前 30 项（含所属项目/
上次活跃时间/dirty 摘要）、需 owner 查 MR 的完整清单、基础设施健康）。**Agent 只读这段 stdout 与
`summary.json`**，不要读 `evidence.json` 的对象级明细，除非要追某个具体对象。

- 默认只读；`--execute` 才调用 owner 工具执行满足全部条件的项（常设授权）。
- `--execute` 仍受 owner 工具在 sink 前的指纹复验约束；脚本不提供 `--force`、`rm -rf`、`git clean`、删分支的路径。
- 脚本被调用方截断（如管道接 `head`）不影响产物：账本与报告在打印前已落盘。

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

指纹覆盖 `path/head/branch/detached/locked/prunable/dirty/ignored 根集合/.local 指纹`。
指纹不变则复用上次的 `size_bytes`/`artifact_bytes`/`last_active_at`/`who_uses`，报告标注来源为缓存。

三条硬约束：

1. **缓存不参与删除授权**：`--execute` 前仍由 owner 工具按指纹复验，缓存只决定"要不要重测"。
2. **指纹盲区**：`ignored` 只哈希根路径集合，不哈希内容——`node_modules` 变大指纹不变，
   所以复用值是**估计**，实际回收字节只在执行时实测；不要把它写成"实际入账"。
3. **状态损坏或缺失时全量重扫**（`--force-fresh` 可强制），缓存不能成为单点。

远端查询（PR merged / 远端分支 tip / 含于主干）**每轮都做**，不进缓存：本地怎么变都测不出远端状态，
而它正是能否回收的决定量。同理，`age` 每轮按 `.git` mtime 重算——跨过 168h 是纯时钟事件。

## 固定章节

章节顺序与标题固定，无内容的章节写「无」而不是省略——空章节本身是结论。
标 **[脚本]** 的由 `patrol_worktrees.py` 的 stdout 摘要与账本小节直接渲染（**不产出 HTML**），
Agent 不要重写；标 **[Agent]** 的需要判断。

1. **[脚本] 一屏汇总**：卡片式。两盘水位（`/` 超 70% 标红）、登记工作树条目、
   本轮释放（项数 + 字节）、累计释放、待决项（项数 + 字节）、增量命中数。
2. **[脚本] 按项目分组的已清理**：见下表 A。
3. **[脚本] ≥7 天未清理**：见下表 B。这是每轮信息量最大的一节。
4. **[脚本] 需 owner 查 MR/PR 的完整清单**：`非 GitHub host` 与 `工具判 ambiguous` 两类，
   不受 30 行截断限制——它们是唯一需要 owner 去查 MR 的部分。
5. **[脚本] 工蜂已核验可回收**：`MR merged(工蜂核验·精确 head)` 且 clean/无引用/无 BB 归属的项，
   逐项给 MR 号、目标分支、链接与空间。`--execute` 时这些项经 owner 工具 `--remote-evidence`
   （见 storage-rules.md「非 GitHub host」）走同一 sink 自动执行；本节只列**本轮未执行**的
   剩余项，并给出本轮执行计数。
6. **[脚本] 原因分布**：原因 × 项数 × 合计空间。
7. **[脚本] <7 天保留**：只给计数与合计字节，不逐项——它们是「在办工作」，列出来是噪音。
8. **[脚本] 增量与执行器健康**：测量/复用数、who-uses 探测数、eligible/failed、
   `clean-tmp` 连续失败天数、Docker 可回收（review-only）、非白名单监听端口。
9. **[Agent] 主 checkout 与 ignored 私有状态**：只列判断依据（`git status` 行、ignored 命中），
   绝不清理；含本仓自身工作区。
10. **[Agent] 需人决定的最小事项**：≤5 条，每条一句决策 + 一条最小命令。
11. **[Agent] 采集命令与异常**：可复现命令；本轮与预期不符之处（脚本报错、口径变化、判断分歧）。

### 表 A：按项目分组的已清理

按 `项目` 升序，组内按释放空间降序。

| 列 | 内容 |
|---|---|
| 项目 | 仓库路径（`git rev-parse --show-toplevel`） |
| 路径 | 被回收的 worktree 精确路径 |
| 年龄 | `xx.x d`（执行器 `.git` mtime 口径） |
| 上次活跃 | 树内最新 mtime（`du -sbx --time` 口径，绝对时间戳）；取不到写「不可得」 |
| 功能 | 该 worktree 在做什么，**标注「推断」**；规则见下 |
| 释放 | 表观字节（回收前 `du -sbx`），GiB/MiB 两位 |
| 远端证明 | PR/MR 链接（merged/closed）或远端分支名；纯文本 URL 也要可点 |
| 分支 | 默认已删（owner 2026-09-19 政策，远端证明为据）；未删写分支名 + 原因（如 `--keep-branches`） |
| 可恢复性 | HEAD 短 SHA；提交在远端默认分支/远端分支/MR 中可达 |
| 备份 | 备份根路径（无 `.local` 时写「无 .local」） |

### 表 B：≥7 天未清理

按 `大概空间` 降序，最多 30 行；其余行在 `evidence.json` 中完整保留并在表尾注明。

| 列 | 内容 |
|---|---|
| 项目 | 仓库路径 |
| 路径 | worktree 精确路径 |
| 大概空间 | 表观字节 |
| 年龄 | `xx.x d` |
| 上次活跃 | 树内最新 mtime（`du -sbx --time` 口径，绝对时间戳）；取不到写「不可得」 |
| 原因 | 用下面的固定词表，可叠加 |
| 领先/落后 | 相对远端默认分支的 `a<ahead>/b<behind>`（借鉴 gmc `--diff-base`，仅作价值信号）；`a0` 即无独有提交，纯残留；取不到基线写「—」 |
| PR/MR | 链接或「无」；非 GitHub host 写 `<host>/<project>!<iid> (未验证)` |
| 下一步 | owner 可执行的最小动作，或「保留」 |

**原因词表**（固定，不得自造近义词）：

`dirty(未提交)`、`PR-open`、`远端不可证`、`非 GitHub host`、`prunable`、`locked`、
`活跃 BB 环境`、`工具判 ambiguous`、`age<7d`、`其他(注明)`、
`MR merged(工蜂核验·精确 head)`、`MR merged(工蜂核验·非精确 head)`、`MR-open`、
`MR closed(未合并)`、`非 GitHub host(无 MR)`、`MR 查询失败`、`凭据缺失`。

`远端不可证` 与 `非 GitHub host` 必须分开：前者是查了但没有证据，后者是根本无法查。
非 GitHub host 的项必须继续用 `gongfengcli` 核验 MR，得到上表后四种状态之一；
`非 GitHub host` 只保留为 host 事实，与 MR 状态并列，不再作为「无法判定」的终点。

### 工蜂（git.example.invalid）核验契约

- 凭据只由脚本解析：优先已导出的 `GONGFENG_PRIVATE_TOKEN`，否则读 owner 维护的
  `/data/code/lighthouse/developer/.env`（要求 root 所有且组/其他不可读，否则拒绝并记
  `凭据缺失`，不降级、不猜测）。**token 只经 `env=` 传给子进程，不进 argv、不进报告/证据/账本/日志、
  不进 Agent 上下文**；Agent 不得自行 `cat`/`grep` 该文件。
- 查询 `gongfengcli mr list <project> --state <merged|opened|closed> --source-branch <branch>`。
  **`--state all` 在 0.1.0 上恒返回 0 条（实测），禁止使用**；必须逐状态查询并在本地校验
  `source_branch` 完全相等（避免同名前缀误配）。
- MR 链接用 `https://git.example.invalid/<namespace>/<project>/-/merge_requests/<iid>`（实测 200；
  不带 `/-/` 的旧式路径是 302）。
- 判定沿用 owner 三条件：`state=merged` 且 `source_commit` 与本地 HEAD 相等 → 精确 head；
  仅 merged 但 head 不等 → 非精确 head，保留。
- **执行契约（2026-09-18 起）**：核验结果由脚本按 repo 写成
  `raw/owner_tool/<slug>.gongfeng-evidence.json`（version 1，逐项含
  `path/branch/head/source_commit/mr_iid/mr_url/target_branch` 与完整 40 位对象名），
  交给 `cleanup_worktrees.py --remote-evidence`。该模式**不调用 `gh`**，只接受与本地
  全等、`state=merged`、age ≥168h 的记录；`--min-no-pr-age-hours < 168` 直接拒绝。
  仍是 `--dry-run --write-approval-report` → `--apply --approve-report` 两跳，
  内部 `git worktree remove`（非 `--force`）、不删分支、`.local` 备份、apply 前指纹复验。

**截断规则**：表 B 按大概空间降序最多 30 行；但 `非 GitHub host` 与 `工具判 ambiguous`
两类**必须完整列出、不受 30 行限制**——它们是 owner 唯一需要去查 MR/PR 的两类，
被大小排序截掉就等于没给可执行信息。其余行在 `evidence.json` 的 `unreclaimed[]` 中完整保留。

## 内容规则

- **功能推断**：优先级 `PR 标题 > 分支名 > 最近一次 commit subject`。三者都没有时写
  「无描述」。每条都要带「推断」字样，不要写成事实。
- **PR/MR 地址**：GitHub 给出完整 `https://github.com/<owner>/<repo>/pull/<n>`；其他 host
  给出 host、项目、分支名，并标 `(未验证)`。查不到证据就写「无」，不要猜编号。
- **数字口径**：表格里的空间统一用「表观字节（回收前 `du -sbx`）」并在表头或脚注说明；
  实际入账另给一段 `df` 前后对比，并注明 `df` 受并行写入干扰、仅作参考。
- **本轮释放 vs 累计释放**：累计值从 `storage-audit.log` 历史小节求和，写明统计起始日期。
- **对比上一轮**：每节开头一句「上一轮 X → 本轮 Y」，没有上一轮时写「首轮」。
- **无操作轮次**：不空转。仍输出全部章节；表 A 写「无」，表 B 正常列出，
  一屏汇总明确「本轮释放 0 字节，原因：…」。
- **不重复调查**：同一对象若上一轮已有结论，本轮只写「结论未变」或「已变化：…」。

## JSON 字段

`evidence.json` 顶层固定：`summary`、`objects[]`、`executed[]`、`unreclaimed[]`。
`objects[]` 每项至少含：`path`、`repo`、`age_hours`、`size_bytes`、`last_active_at`、`dirty`、
`dirty_summary`、`locked`、`prunable`、`head`、`branch`、`remote_proof`、`bb_environment`、
`who_uses`、`decision`、`reason`、`next_step`。`executed[]` / `unreclaimed[]` /
`verified_reclaimable[]` 同样带 `last_active_at`（不可得为 `null`）。
`dirty_summary` 是 `{tracked, untracked, files_changed, insertions, deletions}`，只统计数量与
diffstat、不读内容；表 B 的 dirty 项在「原因」列后附一行 `18F +421/-41 · 1 untracked` 口径的
摘要，让 owner 不必逐项跑 `git status`。

`summary.json` 至少含：`collected_at`、`scope`、`mode`、`registered_entries`、
`root_used_pct`、`data_used_pct`、`cleaner_status`、`min_age_hours`、
`eligible_count`、`executed_count`、`executed_bytes`（仅本轮新增执行）、
`unreclaimed_over_7d_count`、`unreclaimed_over_7d_bytes`、`unreclaimed_by_reason`、
`held_under7_count`、`held_under7_bytes`、`cumulative_reclaimed_bytes`、`cumulative_since`。

工蜂执行路径另加四个字段（2026-09-18 起）：`gongfeng_eligible_count`（本轮核验为精确
head 的项数）、`gongfeng_executed_count` / `gongfeng_executed_bytes`（其中本轮经
`--remote-evidence` 实际执行的项数与表观字节）、`verified_reclaimable_remaining_count`
（核验后仍未执行的剩余项数，等于本节表格行数）。

字段语义不许混用，这三组必须分开：

| 字段 | 含义 |
|---|---|
| `eligible_count` | 本轮执行器判 eligible 的项数（= GitHub owner 工具 dry-run 判可执行 + 工蜂精确 head 项；= 本轮应当执行的集合） |
| `executed_count` / `executed_bytes` | **仅本轮新增执行**；同日早先轮次的执行不得计入，只在账本与 `history` 里 |
| `unreclaimed_over_7d_*` | 表 B 的 ≥7 天未清理集合（**只含有磁盘实体的项**）；`prunable_registrations` 另计无实体登记项 |

字段名一经写入即视为契约：改字段要同时改本文件并在账本记一行。

## 账本

每轮向 `storage-audit.log` 追加一节，至少含：日期、触发来源与线程、采集时间窗、
规则版本（当前 7 天门槛）、证据命令、**已执行**（逐项路径/HEAD/PR/字节/失败数）、
**未执行**（按原因分组与合计）、基础设施健康、rule candidates、报告路径。
已执行与建议必须分开写，不允许用一句「清理若干」概括。
