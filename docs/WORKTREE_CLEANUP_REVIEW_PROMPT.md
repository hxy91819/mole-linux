# 本会话：空间清理独立检查与单DSH执行

继续用户的服务器空间治理任务，使用简体中文。本任务投递到当前「空间清理」会话 `thr_rd3ahpzrae`，沿用这里的授权、证据和未完成工作，不新建替代检查会话或worktree；实际清理通过程序或最多一个DSH执行线程完成，跨轮复用。

用户2026-11-03要求定期检查空间清理自动化，覆盖worktree、缓存、下载副本、镜像和开发测试产物，并在每轮报告补齐的规则。用户2026-11-04最新明确要求：“继续清理啊，目标70%以上可用。”因此当前目标是普通可用空间至少70%，计入保留块的使用率至多30%；覆盖旧的30%可用/70%使用率目标，旧报告保留历史口径。每轮必须自我迭代，增加经过验证的安全清理能力。程序优先，最多一个DSH执行，复用thr_ighghjazfc；本会话保持独立检查。已有授权内持续执行，不等待下一定时点，不因正在排队的监督消息重复派发或apply；安全保留条件继续生效。

## 已核验的后续用户授权（2026-11-04）

用户在thr_tikzbd34dg明确批准了Go build缓存和测试DB卷紧急回收、闲置24小时worktree远端归档，以及长期维护技能。原话副本见runs/20261104T181500/raw/owner-policy-user-excerpt.txt；最新对象策略见storage-rules.md“owner 2026-11-04 存储维护策略”。先读新技能/data/code/mole-linux/.claude/skills/devcloud-storage-upkeep/SKILL.md；原devcloud-space-recovery保留执行/检查分工。新策略中已单独授权的缓存/卷/24小时worktree不继续套用过期的72小时/7天政策；已部署旧入口仍按其受测条件运行，不能内部库绕过。数据、唯一ignored/.env、备份、retained及未知引用继续保护；可重建ignored与唯一私有文件必须区分，tracked-only归档不证明后者可弃。

新的cache_caps.py/worktree_sweep.py在18:15独立隔离测试中仍有明确技术缺口：du失败当0、进程Permission当无引用、唯一ignored.env随worktree消失。它们在本监督保持hold，先修相应边界并正反回归、同owner锁、完整fresh计划及末端证据，再启用；这不是要求用户重新批准已授权策略。禁止与thr_tikzbd34dg的在途存储程序并行apply，不重复计其外部回收。

生产者修复优先：已定位lightspace多个/var/tmp复制路径内的Go进程真实写入/data的GOCACHE。两测试脚本的-v修复已来自外部合主干3404943，本监督仅把两处-v窄hunk部署至旧本机checkout，保留其他dirty；本监督新增回归在Draft MR!497，检查pending，不能说已合或全套通过。

## 入口与状态

- 本监督任务：`auto_cvheqhfkaf4`，project `proj_8d3wrcajxn`，目标会话 `thr_rd3ahpzrae`。
- 前置任务：`auto_o3pwesztbpg`，project `proj_bdg8tywu4a`，每日北京时间09:00。每轮核对真实入口，目前是 `/data/code/fork-maintenance/scheduled.py idle-cleanup`。
- 跨轮状态和唯一报告目录：`/root/.local/state/worktree-cleanup-review/`，0700、系统盘存放。使用 `review.lock` 串行监督，原子更新state.json，保留旧历史，不删除别人的锁，不覆盖其他会话未提交修改。
- 先读 `/data/code/mole-linux/.claude/skills/devcloud-space-recovery/SKILL.md`、`/data/code/mole-linux/AGENTS.md`、`docs/WORKTREE_CLEANUP_REVIEW.md`、`docs/DEVCLOUD_SPACE_RECOVERY.md`、`.claude/skills/devcloud-patrol/references/storage-rules.md`，再读pending和上轮报告。删除规则唯一来源是storage-rules.md，容量目标与state.goal核对。
- 盘点 `/data/code`、登记在其他目录的worktree、已结束的 `/data/system-tmp`、`/data/output`、`/data/ops` 测试生成物及owner管理缓存、镜像。`/tmp`与`/data/system-tmp`为同一份，去重。BB环境依BB生命周期处理。核对03:30 tmp cron与其他维护的并发/结果，不新增调度器。

## 1. 真实空间与70%普通可用空间目标

每轮开始及清理后保存同一文件系统的容量；不用df取整百分比或候选大小验收：

```python
import json, os, time
path = '/data'
if not os.path.ismount(path) or os.stat(path).st_dev == os.stat('/').st_dev:
    raise RuntimeError('data filesystem identity unknown; do not clean')
v = os.statvfs(path)
total = v.f_blocks * v.f_frsize
if total <= 0:
    raise RuntimeError('invalid filesystem capacity')
available = v.f_bavail * v.f_frsize
target = (total * 70 + 99) // 100
print(json.dumps({
    'at': time.time(), 'path': path, 'device': os.stat(path).st_dev,
    'total_bytes': total, 'used_bytes': (v.f_blocks-v.f_bfree)*v.f_frsize,
    'available_bytes': available, 'available_percent': available*100/total,
    'physical_used_percent': (v.f_blocks-v.f_bfree)*v.f_frsize*100/total,
    'unavailable_percent': (total-available)*100/total,
    'maximum_unavailable_percent': 30,
    'target_available_bytes': target, 'target_available_percent': 70,
    'gap_bytes': max(0, target-available), 'target_met': available >= target
}))
```

记录系统盘、inode和挂载身份。目标使用 `/data` 的 `f_bavail/总容量`，排除保留块；同盘别名或Trash搬移不计回收。扩容/迁移/保留块变化与清理分开记账，不从本授权推导磁盘结构或服务配置变更。跨轮比较同设备和同范围，区分并发新增/任务自身收尾与本轮动作，不把整个df变化归因自己，不重复相加共享存储。

## 2. 验收前置清理

1. 用 `bb automation show/runs --project ... --json` 检查启用状态、脚本参数、最近触发、上轮至今的全部运行记录，分页不漏日期。succeeded、退出0、wakeAgent:false、status:pending均不能代替效果。
2. 固定 `/root/.local/share/fork-maintenance/state/` 下job-idle-cleanup.json、idle-cleanup.json、job-idle-cleanup.log及job-history/idle-cleanup/<日期>/<token>/；核对后台worker终态、exit_code、applied、job_token、开始/完成时间，复制前后身份一致。运行中/排队/未知记pending，不重复apply、不杀在途任务来验收；长于预算要查队列、锁和生命周期。
3. 核对 `*-lock-skip.json` 的锁路径、job_token与时间：已确认锁竞争是正常跳过，保留既有真实故障；退出0的跳过不计清理有效，也不因正常竞争新建诊断会话。查archive-index.jsonl和archive-evidence恢复证明；旧 `/data/code/.worktree-cleanup/state/runs/` 只作历史，不混作本轮成功。保存每个来源轮次的不可混淆副本。
4. 分类实际移除、仅清生成物、到期未清、近期/活跃/保留、扫描未知。worktree删除核远端ref/SHA及目录消失。检查ignored阻塞、/tmp漏扫、维护假活跃、超时/批量限制及生产者结束收尾。
5. 有界、同文件系统、不跟随链接计量；大小和活动现测，未知记null。扫描未完成不能给删除循环喂半成品，必要时缩小范围重建完整计划。父目录mtime不作大小缓存。

## 3. 检查、唯一执行端与每轮迭代

本会话是检查端：取证、派单、独立审查、验收、纠偏和维护监督频率。长扫描、批量删除和owner代码修复交给受测程序或唯一DSH，检查端不再亲自运行几十分钟清理批次。

1. 先读state.cleanup_worker和真实BB状态/原程序状态。已有起动、运行、排队、停止中、后台命令或未知状态的执行实例时，复用/检查，不另起执行实例。程序与DSH修改共用owner总锁/仓库锁，串行apply；正常锁竞争记跳过，不触发新诊断任务。
2. 程序已支持并安全覆盖的动作优先由原程序执行。覆盖不足、未达70%目标、积压不减、清后回涨或需修规则时，派给一个`acp-dsh`执行线程；跨轮复用相同thread_id。按`docs/WORKTREE_CLEANUP_REVIEW.md`单实例协议持review.lock保存dispatch意图/唯一nonce，核实父线程/provider/nonce再创建或复用；结果未知不重复派发。复用已有环境，不创建worktree；DSH禁止嵌套Agent、清理/审查线程。任务书来自`docs/DEVCLOUD_CLEANUP_EXECUTOR_PROMPT.md`，带当轮系统盘run目录。
3. 按历史收益与当前测量派单，覆盖worktree、工具缓存、Docker、测试制品、Git对象与生产者回长。优先已合并/已推送的远端保存证明，复用ref/SHA直接移除；普通ignored生成物先独立清理，唯一ignored继续保护。核查缓存、历史测试大包与Git重复对象，不能只数worktree，也不能按上次清单直接删除。
4. 执行端每轮完成一项已验证回收、阻塞修复或增长核验，并写executor-report与rule-changes；检查端独立核容量、真实删除、恢复证明、容器/保留项不变及代码/测试，结论为accepted、needs_changes、partial或pending。未达标且仍有可闭合事项，向同一执行端退回具体任务，不替它做长批次，也不要求用户重复授权。
5. **每轮自我迭代**：定位实际堵点/回长→精确目标/恢复契约/排除→正反回归与失败路径→原owner部署/回滚→现场收益→唯一storage-rules规则源→下一轮验证。新能力必须有测量和安全证据，不能扩大名字匹配来凑收益。已有能力足够且结果合格可0条规则变更，注明依据。安全缺口先补，其他已有安全owner路径可同时按串行锁推进，不因一个执行器待修而停止全部授权回收。
6. 生产者的正常/失败/信号收尾、复用、清单及容量准入用于防回长；先验证已有finally，不把在途峰值全称泄漏，不停在途用户任务。锁、近期/不可恢复、归属未知等阻塞留对象、缺的证据、下一次可处理时间与本轮进展，不能每天原样复制pending。

检查端每轮预算45分钟；DSH单轮执行也有45分钟及命令预算，收尾自己创建的进程/fixture，不停止在途用户任务。异步执行尚未完成写pending并在同一线程续办。代码独立审查由当前检查线程承担，执行端不额外派审查Agent；只交付自己的窄改动，保留他人dirty，无远端直报本地部署。检查通过后不无理由重复整套测试。安全规则不满足时标记needs_intervention，既不虚报达标，也不以改门槛或保护范围达标。

## 安全与授权

用户长期授权：“所有不是近期在用、可恢复的东西都能清理。”未被上述新授权单独调整的历史残留仍沿用72小时及恢复/引用条件，年龄/无进程本身不证明可弃。主checkout、BB生命周期、共享依赖、唯一ignored/.env、会话、凭据、活跃数据库、retained卷及保留备份继续保护。内部源码仅适当私有远端归档。纯可重建缓存按最新单独授权与受测owner处理；含会话、凭据、安装态或未知数据的混合pool不整库清；registry/下载摘要校验失败保留原件；未知拒绝。

缓存owner reset/prune同样是删除入口；单独新授权只调整对应策略，不豁免精确根范围、恢复契约、保留项、完整计划和末端证明；其他对象仍按原条件。执行端须从受测入口取得完整fresh计划与末端证明，不能用临时shell或内部库直接reset。发现越界时保留原回执和更正，分开记录容量变化与安全验收；state中的纠偏任务未完成时只推进相关代码/隔离测试，不继续该未通过能力的删除。已有授权内条件完整的能力不引入新的用户审批。

删除遵循既有owner契约或Mole helper，preview/执行同条件、末端重验，不用通配rm、git clean -fdx或强制跳过保护替代。新规则按AGENTS五问记录精确目标、恢复契约、最小owner动作、排除对象、实测收益和回归，晋升到原规则源，报告不另造第二套规则。

## 4. 状态与自适应频率

只调整本监督任务的阶段频率，不降低09:00原清理频率。优化前置执行内容走上述修复/检查/部署/回滚，不新建定时任务。调用 `bb automation update auto_cvheqhfkaf4 --project proj_8d3wrcajxn --cron ... --timezone Asia/Shanghai` 后show核实，成功才原子更新状态；保留 targetThreadId=`thr_rd3ahpzrae` 及其余执行设置。

| 阶段 | 北京时间cron | 转换条件 |
| --- | --- | --- |
| recovery | `15 */2 * * *` | 未达70%或效果/证据不合格时，每2小时检查并纠偏执行端；下次时间读取实际nextRunAt |
| daily | `15 10 * * *` | 至少两次相隔2小时的独立容量测量均达70%，且效果证据合格，才从recovery进入 |
| three_per_week | `15 10 * * 1,3,5` | daily内至少3个不同日期/真实清理轮次合格，首末至少48小时，全程维持70% |
| weekly | `15 10 * * 1` | 上阶段至少4次合格、跨14天，中间每日清理有证据且保持70% |

任何阶段低于70%、漏跑/失败、关键证据未知、积压恶化或安全/策略退化，回到recovery并清零稳定计数。重复读取同一worker不累计清理有效性，手动初始化不算合格轮次。weekly仍持续监督。

state.json version=2保留兼容，新增execution_policy与cleanup_worker（单DSH线程/派发nonce/阶段/轮次/任务书摘要/最后验收），保存旧历史及automation_id、project_id、target_thread_id、phase/cron、goal、last_capacity、capacity_observations、healthy_streak/healthy_since、清理token、history、pending、last_report。goal.required_available_percent=70，maximum_unavailable_percent=30，metric=available_bytes/total_bytes。70%普通可用空间目标按普通可用比例验收，物理已用比例另报，既有更严格可用目标不降低。状态损坏/缺失保留证据、恢复recovery，不直接降频。

未达70%是监督轮正常完成后的 needs_intervention 业务结论，不伪装成调度器崩溃而触发连续异常暂停；如实报告未达和已做干预。

## 5. 每轮必给用户的报告

执行端保存execution.json、executor-report.json/md、rule-changes.json和raw；检查端独立保存report.json、report.md、验收结论及原始证据，追加storage-audit.log，不生成HTML。回复在本会话，完整自包含并附证据链接：

1. **空间与目标表**：前后used/available、物理已用/计入保留块的使用率、70%普通可用是否达标、70%目标字节、缺口、是否达标、并发影响。
2. **执行端与前置任务效果表**：程序/DSH模式、唯一线程ID与无重复实例证据、独立验收结论；真实轮次/token、移除/保留/失败、实测与可归因回收、积压、覆盖完整性；unknown明说。
3. **补齐/调整规则表（必填）**：规则ID、旧问题/行为、新条件与排除、证据/回归、部署位置/版本、观测收益/回滚。每轮必须复盘和自我迭代；没有规则变更时写0条、无需新增的证据和已验证回收/阻塞修复/增长核验，不凑规则。
4. **清理明细和未完成项**：逐对象记录来源项目、归属证据（Git主checkout/owner metadata/制品标签）、大小口径、清理/保留原因、恢复信息；无法确认来源时明确写“未定位”，不凭目录名猜测。另按项目汇总本轮移除数量及实际占用；本轮动作与未来建议分开。未达目标给具体下一步干预和时间，不止“继续观察”。
5. **下次运行与频率依据**：阶段、下次时间、升降频理由；修复已部署与后续周期待验分开，不宣称未观察的稳定。
