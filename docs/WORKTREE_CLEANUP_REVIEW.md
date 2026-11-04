# 本会话的空间清理监督与干预

> **状态（2026-11-04）**：本监督流程已停用。监督任务经 owner 同意暂停，监督会话 `thr_rd3ahpzrae` 已移交；现行存储维护入口是 `$devcloud-storage-upkeep`（阈值与授权见 storage-rules 的「owner 2026-11-04 存储维护策略」一节）。下文为历史记录，口径停留在停用前。

2026-11-03，用户要求定期使用当前「空间清理」会话检查前置清理自动化；效果不足或不符合预期时主动干预、优化策略；范围包括worktree、缓存、下载副本、镜像和开发测试产物；每轮报告补齐了什么规则。

本仓可用 `$devcloud-space-recovery` 查看相关文档入口，技能见 [SKILL.md](../.claude/skills/devcloud-space-recovery/SKILL.md)。已合并/已推送的worktree优先核验已有证明后直接回收，具体分流见快速手册第4节。

## 目标、执行与检查分工

用户2026-11-04最新要求“目标70%以上可用”。每轮目标改为 **普通可用空间 / 总容量 ≥70%**，对应计入保留块的使用率 **≤30%**；覆盖此前30%可用目标，旧报告保留当时口径。占用口径为`1-f_bavail/总容量`，同时报告物理已用比例；使用未取整statvfs，文件系统保留块不算普通可用。目标字节使用 `(total_bytes * 70 + 99) // 100` 向上取整；当前1,056,550,322,176 B总量对应739,585,225,524 B普通可用。候选大小、同盘搬移、调度succeeded都不能替代验收。

沿用原监督任务 `auto_cvheqhfkaf4`（project `proj_8d3wrcajxn`），改为投递到当前会话 `thr_rd3ahpzrae`，不另建复盘会话或worktree。前置任务 `auto_o3pwesztbpg`（project `proj_bdg8tywu4a`）仍每日09:00，真实参数/入口每轮核实。

| 阶段 | 北京时间 | 条件 |
| --- | --- | --- |
| recovery | 每2小时的15分 | 未达目标或效果不合格时主动干预；本次首先10:15，接在09:00清理后 |
| daily | 每日10:15 | 两次相隔至少2小时的独立容量测量均达70%，且效果证据合格 |
| three_per_week | 每周一三五10:15 | daily至少3个不同日期真实清理合格，首末至少48小时且维持70% |
| weekly | 每周一10:15 | 上阶段至少4次合格、跨14天，中间每日有清理终态且保持70% |

普通可用低于70%、漏跑/失败、证据未知、积压恶化或安全退化，恢复每2小时。原09:00清理不随监督降频，稳定后仍持续监督。

## 单执行端与独立检查

用户2026-11-03明确要求：实际清理通过自动化程序，或最多启动一个DSH；本会话仍是检查线程。

- 程序优先：已有受测程序完成可确定的回收，保留原09:00调度。不新建重复调度器。
- 需要人工推理、补规则或程序覆盖不足时，使用已安装provider `acp-dsh` 的一个执行线程，复用已有环境和线程，按[执行任务书](DEVCLOUD_CLEANUP_EXECUTOR_PROMPT.md)工作；每轮最多1个，执行线程不再派子Agent/审查线程/新worktree。程序和DSH实际删除共用原owner锁，串行修改。
- 检查线程 `thr_rd3ahpzrae` 取证、派单、独立复查代码/删除结果与容量、退回修正、维护规则和频率。长扫描/批量删除及执行器修复交给执行端，不在检查线程再次执行数十分钟批次。DSH自报成功仍需检查端验收。

**单实例跨轮协议**：持`review.lock`读取state.cleanup_worker，并核对BB中本检查线程的未归档DSH子线程和活跃执行进程。已有起动/运行/排队/停止中/有后台命令或状态未知的worker时复用或等待，不能另派；原程序在途也不能并行apply。无worker才先原子写入带唯一nonce的dispatching意图，再调用`bb thread spawn --project proj_8d3wrcajxn --environment env_enxy84wqn2 --provider acp-dsh --model <当前目录默认ID> --reasoning-level <该模型默认支持档位> --parent-thread thr_rd3ahpzrae --prompt-file <本轮任务书> --json`，将返回ID写回state。任务书要求读取技能和执行说明，并传入系统盘run目录。切换到DSH时显式传当前目录默认model ID及该模型支持的默认reasoning档位，避免继承其他provider的medium等不支持值；不改全局偏好。模型以当前provider目录/项目默认为准，不把旧模型ID当常量。

派发响应丢失或结果未知时保留dispatching/unknown及nonce，先按父线程/provider/nonce核对创建结果，未证实没有创建就不重派。跨轮优先复用相同thread_id；已有线程空闲且无排队/后台任务才发送下一轮任务。只有确认旧执行实例全部结束、无法恢复后才串行替换，记录原因；不自动换其他provider。检查端不将一个任务扇出成多个DSH。正常权限继承已有长期授权。

## 每轮必须自我迭代

对照上轮证据和[历史大额回收](DEVCLOUD_SPACE_RECOVERY_HISTORY.md)，覆盖worktree、工具缓存、Docker、测试制品、Git对象及生产者回长。按实测收益派单，不能只汇报worktree数量或重复复制待办。执行端完成已验证回收、阻塞修复或增长核验；检查端验收并把可复用判断固化到原owner。

新能力需精确目标、恢复契约、非目标、实测收益、行为/失败回归、部署/回滚和下轮效果。只把经过验证的规则晋升到唯一storage-rules.md；每轮保存rule-changes.json，0条时给出无需新增的依据。扩大安全能力不等于降低3天门槛、扩大名称删除或清未知数据。先验收已有收尾，再修泄漏和容量预算，保留在途任务。

## 每轮做什么

先读取前置调度和匹配的后台worker终态、逐token日志、回收/保留明细、恢复证明，再测容量及相关目录。全局扫描超时不能把未知当0；缩小范围重建完整独立计划后才执行。

未达目标、有到期对象却未清、清后回涨、漏扫或错误保留时，派执行端修复owner执行器、已验证规则及生产者收尾；已证明非近期且可恢复的对象按现有长期授权处理，不只重复建议。代码修改需复现、行为回归、适用检查/review、部署与回滚，保留他人现场。具体删除规则只维护在 [storage-rules.md](../.claude/skills/devcloud-patrol/references/storage-rules.md)。

每轮报告空间/70%普通可用是否达标及其字节缺口、前置真实效果、本轮干预、**规则变更清单**（旧问题、新条件/排除、证据/测试、部署、收益）、未完成项和下次时间。无规则变更写0条及原因，不编造规则或稳定结论。

历史残留继续满72小时并核恢复性和引用。主checkout、活跃任务、会话、凭据、唯一ignored、数据库、retained及保留备份保护；容量目标不改变这些边界。

## 记录位置

- `/root/.local/state/worktree-cleanup-review/state.json`：version 2，目标、绑定会话、阶段、容量及待办；保留旧历史。
- 同目录 `runs/<唯一轮次>/`：执行端写execution.json、executor-report.json/md、rule-changes.json和raw；检查端独立写report.json/md及验收结论；系统盘、0700。
- state.cleanup_worker：单DSH的thread_id/provider、派发nonce/状态/当前轮次/任务书摘要/最后验收；只由检查端持review.lock更新。
- `/root/.local/share/fork-maintenance/state/`：前置owner最新报告、恢复索引、job-history/idle-cleanup/<日期>/<token>/，不能只看latest。
- 本仓storage-audit.log：每轮追加、Git忽略；不生成HTML。
- [WORKTREE_CLEANUP_REVIEW_PROMPT.md](WORKTREE_CLEANUP_REVIEW_PROMPT.md)：完整提示词；BB保存文本副本，改文件后必须显式更新任务。

用 `bb automation show/runs <ID> --project <projectID>` 查任务和运行；`bb automation list --target-thread thr_rd3ahpzrae` 查投递到本会话的任务，该list不再附project。会话被归档/删除时BB会停用绑定任务，恢复前核对实配。

## 历史及待验收项

旧执行器10-28至11-01连续五次移除0，最后200项因ignored保留；11-01新版采用30天，owner在11-02明确恢复3天。当前3天代码与边界测试已上线，09:00真实效果待验。逐token日志已补；fd/未知状态/挂载别名/sink契约、/tmp覆盖、每对象字节及生产者收敛为优先待办。

11-03手工移除50个生成目录及8镜像，期间有并发写入/收尾，不能据此判自动化稳定。详见 `/root/.local/state/space-recovery-20261103/清理清单与长期治理.md` 及同目录CSV。

手动应急操作见 [快速释放空间手册](DEVCLOUD_SPACE_RECOVERY.md)。
