---
name: devcloud-patrol
description: 巡检这台 DevCloud 服务器：磁盘空间 + 开发期残留的对外暴露服务（Vite 等 dev server、PostgreSQL 等数据库、临时容器）。只读取证与分类；worktree 由独立的每日清理任务处理，其余处置逐项确认后执行。
disable-model-invocation: true
---

# DevCloud 服务器巡检

磁盘布局、设计取舍和踩坑背景见 [DEVCLOUD_STORAGE_DESIGN](../../../docs/DEVCLOUD_STORAGE_DESIGN.md)；
现行存储任务和部署见 [DEVCLOUD_AUTOMATION](../../../docs/DEVCLOUD_AUTOMATION.md)。
本文只写巡检和逐项处置流程。

这台开发机的磁盘经常被 Docker、worktree、日志和缓存填满；开发过程还会留下绑定在非回环地址上的临时服务——安全扫描已两次点名本机：Vite dev server（5173，CVE-2025-30208 任意文件读取）和 PostgreSQL（5432，弱口令）。这类残留没法靠固定脚本判定生死：同一个端口上，可能是三天前 `nohup` 忘关的孤儿进程，也可能是同事此刻正在用的预览服务。巡检的价值在取证和分类；处置永远逐项过人。

一条铁律贯穿全程：**巡检只读取证；worktree 的自动归档清理由独立每日任务负责，现役入口见存储规则，其余处置逐项过人。**

## 流程

### 1. 发现（只读）

磁盘与服务两条线并行采集：

```bash
# 磁盘：已有工具承担发现与初分类，不重复造轮子
df -hT /data /
./mo devcloud --json          # 盘面紧张或做全面巡检时加 --deep

# 服务：从监听面反查进程；0.0.0.0 / :: / 内网 IP 都是对外暴露
ss -tlnp
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
```

完成条件：

- `df` 账面占用与可见目录对得上；对不上的差额作为独立调查项记录（通常是 overlay/挂载层），不允许把 du 超时或遍历失败报成"低占用"。
- 每个非回环监听端口都有一行归属记录：端口 → PID/容器 → 命令行 → 属主 → 启动时间 → 工作目录。取证清单见 [references/service-patrol.md](references/service-patrol.md)。
- 上一轮 `storage-audit.log` 里未闭环的暴露项逐一复核是否仍在（当前未闭环：5173 Vite、5432 PostgreSQL，以最新扫描为准）。

### 2. 分类

每个发现归入三类。类别跟证据走，不跟名字走：

| 类别 | 判定证据 |
|---|---|
| 长期服务 | 白名单条目（见下）；`mole.protect=true` / `devcloud.protect=true` 标签；或 owner 已确认的长期容器（以当下 `docker ps` 为准，不背缓存名单） |
| 开发残留候选 | 监听非回环 + 启动时间久/父进程已死 + cwd 指向某个项目目录；test-like 命名的临时容器。逐项证据见 references |
| 未知 | 证据不足。一律问 owner，不猜 |

**白名单是唯一豁免源**：分类前先读 `devcloud-exposure/whitelist.json`。豁免条件是**端口和身份证据同时匹配**（如 8080 上跑的必须是 lighttools-frontend-1）；只对上端口、身份变了的一律按未知处理——端口被顶替是劫持信号，不是白名单失效。

磁盘侧候选沿用 `./mo devcloud` 输出的分类，逐条对照 [references/storage-rules.md](references/storage-rules.md) 的持久规则（binlog 走 retention、包缓存走 owner prune、worktree 走当前保留期/远端持久规则等），那里是唯一规则源。

完成条件：没有"未分类"条目；每个候选的证据链能在一行话里讲给 owner 听。

### 3. 报告与逐项决策

需要回顾旧巡检数据时，可用 [scripts/patrol_worktrees.py](scripts/patrol_worktrees.py)
只读采集与渲染；调用方先按名称加载 `worktree-cleanup`，把宿主解析的脚本入口显式传入。
旧报告形状见 [references/worktree-report-format.md](references/worktree-report-format.md)：

```bash
python3 -u <本 Skill 根>/scripts/patrol_worktrees.py \
  --worktree-tool <已加载的 worktree-cleanup Skill 根>/scripts/cleanup_worktrees.py
```

脚本把「按项目分组的已清理」（年龄、功能推断、释放空间、PR/MR 地址、可恢复性）与
「≥7 天未清理」（原因、PR/MR 地址、大概空间）连同紧凑摘要直接打到 stdout；Agent 只读摘要并补
需要判断的部分（主 checkout/ignored 依据、真实待决事项的完整清单、异常说明），不要重写脚本已产出的章节。
新的 worktree 清理结果以 `/root/.local/share/fork-maintenance/state/idle-cleanup.json` 及对应 worker 终态为准；其余走下面的逐项决策。

一屏汇总，每行：资产（端口/路径）｜证据｜风险｜建议动作（owner 命令优先）｜状态。然后让用户对每一项表态：**保留 / 处置 / 暂缓**。用户没逐项点头的条目不动——"都清了吧"不算对具体条目的授权，要回放清单拿到确认。

巡检报告的回复格式见 [references/automation-report-format.md](references/automation-report-format.md)。

### 4. 处置与记账

服务处置按 [references/service-patrol.md](references/service-patrol.md) 的命令模板执行，每步自带复核：

- kill 前当场重读 `/proc/<pid>/cmdline` 和启动时间（PID 会复用），先 TERM 后 KILL；只按精确 PID 杀，不用模式匹配批量杀。
- 容器先查 protect 标签和 compose 归属再 stop/down；数据卷默认保留。
- 处置后回查 `ss -tlnp` / `df`：端口消失、空间入账，才算这项闭环。

用户判断后同步维护白名单（结构化写入，不是散文）：新保留项加入 `devcloud-exposure/whitelist.json` 的 `entries`（端口 + 身份证据 + 复核方法三样缺一不可），被处置项移入 `disposed` 并附恢复命令，更新 `updated` / `authority` 字段。

收尾必须记账：向 `storage-audit.log` 追加一节——日期、证据命令、每个处置项的证据与结果、回收字节、遗留项。巡检中沉淀出的新规则先写进本轮 log，等跨运行成立后按 Ledger 五问提升进 [references/storage-rules.md](references/storage-rules.md)。

## 周期化

完整巡检流程由人或 agent 显式调用（`$devcloud-patrol`）。Worktree 每日清理由独立 BB script automation 执行；临时目录清理和每周 BB 容量复盘按各自任务运行。

临时目录的范围与失败恢复仍见 [references/storage-rules.md](references/storage-rules.md)；worktree 清理规则与恢复证据见存储规则所指的现役执行器 README 和状态报告。旧巡检报告格式只用于阅读历史产物。
