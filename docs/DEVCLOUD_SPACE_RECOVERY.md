# 当前 DevCloud 服务器：快速释放空间手册

适用主机：`/data` 挂载于 `/dev/vdb`、`/tmp` 是 `/data/system-tmp` 的 bind 别名的这台 Linux 开发机。首次核实于 2026-10-29；换机器或路径变化后先重新确认。

**2026-11-04 起**：阈值与授权以 [存储规则](../.claude/skills/devcloud-patrol/references/storage-rules.md) 的「owner 2026-11-04 存储维护策略」一节为准（worktree、/tmp、工具缓存、Docker 残留的当前门槛都在那里，本手册不复述数字）；自动化现状见 [本机存储维护落地方案](DEVCLOUD_AUTOMATION.md) 的「当前任务」表。

这是**手工操作入口和故障排查手册**，不增加自动删除权限。对象规则以 [存储规则](../.claude/skills/devcloud-patrol/references/storage-rules.md) 为准；有单独保留声明的数据库、卷、备份继续保护。已授权范围按现有规则执行，不重复索取许可；未知对象先保留并查归属。

比较“上次为什么释放很多”或汇总当天成果时，先看 [历次回收与当天对账](DEVCLOUD_SPACE_RECOVERY_HISTORY.md)：分别核对生成物、worktree、镜像共享层和df净变化；不能把最后一个批次当作当天总量。

## 1. 紧急时先做这几步

按段操作，执行前看对应前提；不要把整篇直接作为清理脚本运行。

```bash
cleanup_run="/root/.local/state/devcloud-space-recovery/$(date +%Y%m%dT%H%M%S)"
DEVCLOUD_TOOL=/data/tools/devcloud-maintenance/releases/df3e2b353d8e1c72
mkdir -p "$cleanup_run"
chmod 700 "$cleanup_run"
df -hT /data / | tee "$cleanup_run/df-before.txt"
findmnt -T /tmp -o TARGET,SOURCE,FSTYPE,OPTIONS
```

后续命令共用这里的变量，跳到后面的章节前先完成本节准备。证据目录放系统盘，避免数据盘写满后连报告都写不下；先用 `findmnt -T /root/.local/state` 确认它没有迁到满盘。`/tmp` 与 `/data/system-tmp` 只算一份。ext4 保留块也会导致 used + available 小于总量，不把它当成可直接删除的文件。

| 优先级 | 做什么 | 适用前提与收益 |
| --- | --- | --- |
| 1 | 查看 Docker owner 的缓存报告，按到期条件回收 | 不递归扫描 overlay2；构建缓存可能回收数 GiB，也可能正常返回 0B |
| 2 | 清理已结束测试的专属生成物，再跑 worktree 归档工具 | 满足当前门槛（见文首 2026-11-04 指针）；源码、私有数据和远端证明必须分别检查 |
| 3 | 检查 Go、npm、pnpm、Cargo 的专属缓存 | 公共开发缓存不自动清；确需手动清理，只能在确认没有进程在用时进行（见第 6 节） |
| 4 | 对确认重复 pack 的闲置测试 Git 库做保留对象的压缩 | 历史上曾减少约 34.6 GiB；不会凭目录名删除整个 Git 库，当前未必还有收益 |
| 5 | 运行 tmp 清理器预演 | 每日 03:30 任务已覆盖执行（见 DEVCLOUD_AUTOMATION.md「当前任务」）；手动多用于预演 |

执行一个动作后马上重新 `df -h /data`，空间够用就停止扩大范围。不能保证每次都有几十 GiB 到期可回收；满 3 天或运行状态检查不通过时，保留对象，不改成 `--days 0`。

## 2. 常见大目录：先查谁，保护谁

2026-10-29 的目录测量仅作查找顺序：code 约 259 GiB、system-tmp 约 121 GiB、cache-migrated 约 76 GiB、Docker 约 54 GiB。会持续变化，不能用这些旧数字计算本轮回收量；分开 du 的硬链接、并发增删也会影响合计。

| 路径 | 内容 | 处理方式 |
| --- | --- | --- |
| `/data/code/mono2-worktrees`、各项目 `.worktrees` | 源码、依赖、生成缓存及可能唯一的本地状态 | 使用归档工具；不能整棵按日期删 |
| `/data/system-tmp` | 手动 worktree、每轮测试目录、缓存、日志 | 先定位具体 run；共享父目录持续有新运行，不代表每个旧子项都在用 |
| `/data/cache-migrated`、`/data/.pnpm-store`、`/data/root-dev-cache` | 下载和构建缓存，也可能有混合工具状态 | 确认实际 owner 路径，再按工具支持的方式回收 |
| `/data/cache-migrated/.cache/claude-ollama-bb-task/backup` | BB 发布切换的数据库回滚备份，含 `bb.db`、WAL/SHM 与旧配置 | 按备份保护整族；owner metadata 位于同任务 `state.json`，不按 cache 目录名或年龄清空 |
| `/data/output`、`/data/ops` | 历史交付、源码副本、测试输出和证据 | 区分可重建大产物、仍在使用的制品及保留证据 |
| `/data/docker/lib` | 镜像层、容器、卷、构建缓存 | 仅使用 Docker owner 命令；不用文件系统删除 |
| `/data/codex`、`/data/bb-data`、`/data/root-migrated` | 会话、工具状态和备份 | 保护；不能整棵当缓存清理 |
| `/data/root-storage-20260912` | 迁移后的正式后端存储 | **不是旧备份**；原入口依赖它，保护 |
| `/data/backups`、`bb-backups`、`bb-archives`、`bb-cutover-backups`、`.local-test/backups/**` | 保留备份或审计证据 | 不按年龄、名字或无进程引用自动删除 |
| `/data/itfs`、`/data/swapfile` | 保留基础设施/系统状态 | 不作为本手册的回收目标 |

不要先花二十分钟全盘 du。先看近期证据，再测一个确切大目录：

```bash
timeout 180 stdbuf -oL du -x -B1 --max-depth=1 /data/cache-migrated/.cache \
  > "$cleanup_run/cache-sizes.txt" 2> "$cleanup_run/cache-sizes.err"
```

退出 124 或未输出目标根目录总计时，记为扫描未完成，不能把缺失部分报成 0。测量结果只用于调查，不能直接变成删除清单。

## 3. 年龄与占用：删除前必须重新查

年龄取**整棵候选树最新 mtime/ctime**，不是父目录时间、Git commit 日期或镜像创建时间。镜像不提供可靠 last-used，不能把 Created 称为闲置时间。Git gc 等维护能刷新 reflog 文件时间，也不能据此断言任务刚使用过。

下面只做年龄检查，不授权删除，不写候选目录。把变量改成已经归属明确的专属生成目录；45 秒内未扫完会拒绝。`HOURS` 按对象类型查 [存储规则](../.claude/skills/devcloud-patrol/references/storage-rules.md)「owner 2026-11-04 存储维护策略」一节，不在本手册内嵌默认值：

```bash
cleanup_candidate='/data/已确认的项目/专属生成目录'
HOURS=<按对象类型查存储规则后填入>
python3 - "$cleanup_candidate" "$HOURS" <<'PY'
import json, os, sys, time
from pathlib import Path
root = Path(sys.argv[1])
try:
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError('root must be an existing physical directory')
    hours = float(sys.argv[2])
    cutoff, deadline = time.time() - hours * 3600, time.monotonic() + 45
    device, latest, count = root.stat().st_dev, 0, 0
    def failed(error):
        raise error
    for directory, dirs, files in os.walk(root, followlinks=False, onerror=failed):
        for name in ['.'] + dirs + files:
            if time.monotonic() >= deadline:
                raise RuntimeError('incomplete age scan')
            p = Path(directory) if name == '.' else Path(directory) / name
            s = p.lstat()
            if s.st_dev != device:
                raise RuntimeError('nested filesystem')
            latest = max(latest, s.st_mtime, s.st_ctime)
            count += 1
            if latest > cutoff:
                print(json.dumps({'age_check': 'less_than_cutoff', 'entries_checked': count}))
                sys.exit(1)
    print(json.dumps({'age_check': 'at_least_cutoff', 'latest_timestamp': latest, 'entries_checked': count}))
except Exception as error:
    print(json.dumps({'age_check': 'unknown', 'reason': str(error)}))
    sys.exit(2)
PY
```

再查引用，使用部署版本的统一探针，覆盖 cwd/exe/fd/maps、挂载别名和 inotify：

```bash
/usr/bin/python3 -B "$DEVCLOUD_TOOL/bin/devcloud.py" who-uses "$cleanup_candidate" --json
```

该命令退出 0/1/2 分别代表未发现引用/有引用/未知；1、2 均保留。还要查关联 BB 线程、Git 操作和该资源的消费者。年龄检查、无打开文件都不能证明某个数据库、备份或私有文件可删除。实际执行应在最后边界复查身份、年龄及引用；不要隔几小时复用检查结果。

## 4. Worktree：走原归档工具

大量历史worktree已经合并或推送，优先核对已有保存证明：

| 实际内容状态 | 处理路径 |
| --- | --- |
| 主干或其他实时远端ref已包含需保存的HEAD，且本地无未保存工作 | 满足当前门槛（见文首 2026-11-04 指针）且引用/保护检查通过后，复用远端ref/SHA，走owner直接移除，无需再创建WIP归档 |
| 已推送但尚未合并，需保存内容已在远端 | 同样可复用实时远端证明；“尚未合并”本身不要求继续占用本地worktree |
| 仍有需保存的本地改动或无有效远端证明 | 原工具先归档、推送并核验；失败保留 |
| 只被可再生ignored依赖/构建缓存阻塞 | 先按现有规则清理这些生成物，再重新盘点，不把整树无限保留 |
| 活跃/共享引用、唯一ignored数据或子模块独有工作未保存 | 按保护原因保留，不由“已合并”覆盖这些事实 |

需核实际HEAD、暂存/工作区内容及远端当前ref，不只看分支名、旧MR状态或过期本地tracking ref。每个成功项仍记录恢复位置。

历史归档工具入口 `/data/code/worktree-cleanup` 当前只用于盘点；其 `core.py` 仍使用 force 移除且引用检查范围未覆盖现役要求，不作为本机当前受测删除入口。实际清理入口已切换到 `$devcloud-storage-upkeep` 的 `worktree_sweep.py`（每日定时任务，门槛见 storage-rules 2026-11-04 策略），沿用原授权，无需新增用户审批。历史工具说明见该项目 `README.md`。2026-10-29 每日任务实际传 `--days 3`；11-01 已由 `/data/code/fork-maintenance/scheduled.py idle-cleanup` 接管，11-01 曾采用 30 天，owner 11-02 明确要求恢复，当日门槛为连续闲置 3 天（72 小时）。实时状态与保留期修正见 [清理效果复盘](WORKTREE_CLEANUP_REVIEW.md)；以下命令是原授权的手工入口，不是当前调度命令，不能在现役执行器并发运行时混用。项目 CLI 默认仍为 7，参数须显式核对。扫描也会更新盘点状态，但不提交、推送或删除源码：

```bash
cd /data/code/worktree-cleanup
python3 -m worktree_cleanup scan \
  --root /data/code --state-dir /data/code/.worktree-cleanup/state --days 3 --bb
```

检查报告中的主 checkout、保留原因、真实线程/进程状态和归档目的地。工蜂私有项目只允许归档到适合的私有远端；同时配置工蜂与公开 GitHub mirror 的仓库，必须核对执行器的远端选择，不能默认加 `--owner` 后直接批量推公开 fork。输出 remote URL 前脱敏，不把 HTTPS URL 内的 token 写报告。

09:00 原 owner 入口（`auto_o3pwesztbpg`，已于 2026-11-05 退役）的保护修复已本地提交`fork-maintenance@8d87f7a`：未验证依赖显式保留，worktree使用非force移除，并在最后引用检查后重新核ignored/源状态与根身份。源码仍dirty或留有唯一数据时保留。新依赖原型和缓存reset未启用；本机没有该owner仓库远端，提交未push；真实周期回收效果待后续验证。

维护命令的进程收尾另有本机提交`d7aa3eb`：正常结束及中断后检查自有session，按身份与期限收尾残留写入者。当前部署和适用范围仍以监督state、验收回执及唯一存储规则源为准；该控制修复的真实周期容量收益尚待验证。

扫描卡住时先看原任务日志和匹配 token 的归档，不再仅凭 launcher 返回 pending/0 判断清理有效：

```bash
tail -n 80 /root/.local/share/fork-maintenance/state/job-idle-cleanup.log
```

`fork-maintenance@c1db93e` 已补立即写入的阶段 JSONL（stderr，原单 JSON stdout 保留），可区分 discover、共享依赖、逐仓活动及 apply；具体规则与验收见唯一存储规则源。旧09:00日志仍为0B，新事件需等真实新轮次观察。原扫描仍先遍历所有 Git 登记的 peer；传 `--root` 不能当作已有分仓预算，阶段正常结束也不等于对象可删除或空间已回收。

共享引用扫描的重复路径计算已由受测提交 `934f1ad` 优化，部署后列出的555个peer完成只读扫描。恢复删除前仍须重新执行完整计划与末端检查；性能基准、引用图完成和实际回收分别验收。当前部署与后续真实日任务结果见监督state及唯一规则源。

执行前从监督state和当轮部署回执核对现役入口、已验收源码版本及完整fresh计划；由同一owner锁下的受测程序执行，不调用旧工具apply或复制旧批次计划。代码修补中、恢复/年龄/引用证据缺失的能力保留；条件完整的已授权能力自主执行，不增加人审步骤。

`--root /data/code` 不覆盖登记在 `/tmp` 下的工作区。需要补盘点 `/tmp` 时，先核对是否有另一管家正在处理，采用独立且固定复用的历史盘点状态目录：

```bash
python3 -m worktree_cleanup scan \
  --root /tmp --state-dir /data/code/.worktree-cleanup/state-tmp --days 3 --bb
```

这里的scan结果只作线索；实际动作仍由覆盖该物理root、经过验证的现役owner程序重建fresh计划后处理。

同一范围始终使用同一状态目录；遇锁占用就停止本轮，不删除锁文件。即使进程检查无引用，也要等另一个清理执行器完成，避免交叉删除。

遇 `unarchived_local_data` 时，先列出 ignored 文件：

```bash
git --no-optional-locks -C "$cleanup_candidate" ls-files \
  --others --ignored --exclude-standard --directory --no-empty-directory
```

它可能只是 16 KiB 的 Ruff 缓存，也可能是唯一的 `.env`、会话或子模块数据。只对归属明确、满 3 天的生成缓存建立**本次**删除计划，经 Mole dry-run、白名单和最终引用检查回收，再由现役owner重新盘点。目前这一步尚未集成到长期执行器；不能用 `git clean -fdx`、手动强删或复用历史 apply 计划代替。该宿主运维工具的授权不改变 Mole 产品“不自动删除 worktree”的边界。

成功报告在 `state/runs/`，含 `recovery.url/ref/sha`。恢复时从报告抄录值，使用适当私有 SSH 远端，不抄令牌：

```bash
git -C "${main_checkout:?填写主 checkout}" fetch "${recovery_remote:?填写恢复远端}" "${recovery_ref:?填写恢复 ref}"
git -C "${main_checkout:?}" cat-file -t "${recovery_sha:?填写报告中的 SHA}"
git -C "${main_checkout:?}" worktree add -b "${recovered_branch:?填写新分支}" "${recovered_path:?填写新路径}" "${recovery_sha:?}"
```

## 5. Docker：优先 owner 缓存，精确对象删除

Docker 残留（匿名测试 DB 卷、悬空镜像、BuildKit 缓存）当前由 `docker_leftovers.py`（每小时定时任务）按
storage-rules 2026-11-04 策略的门槛清理；以下手动命令是补充排查入口，先看该处授权再用。

先取一次状态并落盘，不循环调用慢速 `docker system df`：

```bash
timeout 60 docker ps -a --no-trunc > "$cleanup_run/docker-ps-before.txt"
timeout 60 docker buildx du --verbose > "$cleanup_run/buildx-before.txt"
```

构建缓存按 BuildKit 最后使用时间筛选。自动化门槛以 storage-rules 2026-11-04 策略为准（本行写作时为 14 天）：

```bash
timeout 180 docker buildx prune -f --filter until=336h
```

本次明确授权的测试缓存可按 3 天条件使用 `until=72h`；这不自动降低其他 owner 的保留策略。返回 0B 是有效结果，不能改成无过滤全清。

测试镜像/容器先按存储规则核对登记、保护声明、全部容器引用（包括 stopped/created）、精确 ID/tag、年龄与最新保留项。确认符合**当前授权**后，只对计划中的精确对象执行：

```bash
docker image rm -- "${reviewed_test_image_tag:?填写已复核的精确 tag}"
docker container rm -- "${reviewed_stopped_test_container_id:?填写已复核的停止容器 ID}"
```

不加 force，不带删除卷的选项；容器必须确认为结束且符合保留期。标签可能移动，执行前重新核对 tag→ID。执行后比较运行容器 ID、StartedAt、镜像和挂载。Docker CLI 超时表示证据未知，不转而删除 `/data/docker/lib`。

**卷不属于快速释放入口**。retained、命名数据库、itfs、未知卷和审计备份继续保护；数据库按自己的 owner/退役登记处理。禁止用 `docker system prune -a --volumes`、`docker volume prune`、删除 overlay2 目录来抢空间。

## 6. 工具缓存：公共开发缓存不自动清

**公共开发缓存（go-build、npm `_cacache`、pnpm store）不自动清**（owner 2026-11-04）：这些缓存本身经常在
用，按大小整体重置会打断正在编译的任务；Go 自身会删除 5 天没用过的条目。阈值、授权与是否允许清理以
[存储规则](../.claude/skills/devcloud-patrol/references/storage-rules.md)「owner 2026-11-04 存储维护策略」
一节为准，本节不复述门槛。

先解析真实路径，确认是专属可重建缓存，完成第 3 节检查并排除相关安装/构建任务。**手动清理也只能在确认没有进程在用时进行**（探针未知即保留）。下表是 owner 命令，如今属例外操作，**不是可整段复制执行的清理清单**。

| 目标 | 查真实位置/预览 | 满足前提后的 owner 动作 |
| --- | --- | --- |
| Go 编译缓存 | `go env GOCACHE` | `go clean -cache` |
| npm 下载缓存 | `npm config get cache` | `npm cache clean --force --cache "${reviewed_npm_cache:?填写已审查缓存路径}"`；force 是 npm 的清缓存参数，不是绕过前置检查 |
| pnpm store | `pnpm store path` | `pnpm store prune`；先确认当前项目/配置指向已审查的 store，没有其他安装任务 |
| Cargo 专属 target | `cargo clean --offline --dry-run --manifest-path "${manifest:?填写 Cargo.toml 路径}" --target-dir "${target:?填写专属 target 路径}"` | 同一参数去掉 `--dry-run`；执行前再次检查，保留源码和安装中的版本 |

`go env`、`npm config get cache`和`pnpm store path`只定位路径，不是删除预览。表中动作仍须有完整候选计划、满足 storage-rules 的当前授权与引用条件，并在owner真正突变前复查；不能因为命令在本手册中而直接reset。

这些命令可能清整个范围，不能用于“树里有新文件，但有些旧文件看起来能删”的情况。没有 owner 支持的年龄选择就保留，不能手删工具索引。`GOMODCACHE` 在本机有迁移符号链接及并发下载的历史问题，`go clean -modcache` 不列为快速默认动作；不能把链接被移除误报成缓存已经释放。

## 7. 重复 Git pack：保留所有对象的维护

只适用于已经证实有大量重复 pack、无人写入的具体测试库。历史 `opencraft-replay-*` 曾有上千 pack，保留所有对象的压缩减少约 34.6 GiB；2026-10-29 已未发现该类大 pack 积压。不要按前缀批量删目录。

完成引用检查、确认没有 Git 操作并预留重打包的临时空间后，对**一个已审查库**运行：

```bash
git -C "${reviewed_test_repo:?填写已审查的测试库路径}" count-objects -v
git -C "${reviewed_test_repo:?}" for-each-ref --format='%(refname) %(objectname)' \
  > "$cleanup_run/refs-before.txt"
timeout 600 git -C "${reviewed_test_repo:?}" -c pack.threads=2 \
  -c gc.reflogExpire=never -c gc.reflogExpireUnreachable=never gc --prune=never
git -C "${reviewed_test_repo:?}" for-each-ref --format='%(refname) %(objectname)' \
  > "$cleanup_run/refs-after.txt"
diff -u "$cleanup_run/refs-before.txt" "$cleanup_run/refs-after.txt"
git -C "${reviewed_test_repo:?}" fsck --connectivity-only --no-dangling
```

中间任一命令失败就停止，保留输出查原因；不删 `.git`、不可达对象或 reflog，不改用 `--prune=now`。重打包会临时占空间，磁盘极满时先选择别的到期对象。

## 8. 通用 tmp 清理：默认预览，失败不执行

日常执行已由每天 03:30 的定时任务覆盖（参数与部署版本见 [DEVCLOUD_AUTOMATION.md](DEVCLOUD_AUTOMATION.md)「当前任务」表）；以下命令用于手动预演与补救，`--age-days` 的当前值以部署任务为准：

```bash
/usr/bin/python3 -B "$DEVCLOUD_TOOL/bin/devcloud.py" clean-tmp --json \
  > "$cleanup_run/tmp-preview.json" 2> "$cleanup_run/tmp-preview.err"
```

只有退出 0、JSON `status=ok` 且 `scan=complete`，并且处于既有授权范围，才运行同一入口加 `--execute`；它会重新扫描：

```bash
/usr/bin/python3 -B "$DEVCLOUD_TOOL/bin/devcloud.py" clean-tmp --execute --json \
  > "$cleanup_run/tmp-apply.json" 2> "$cleanup_run/tmp-apply.err"
```

**历史**：2026-10-29 默认策略预算 90 秒仍曾超时，一次性延长预算得到完整预览并执行 50 项，也不代表定时版本已经修好。不要执行本手册证据中的旧计划，也不要手改只读 release、去掉保护或提高 force 级别。日志：`/data/metrics/devcloud-clean-tmp.log`；stderr 同目录；锁和隔离原件：`/data/metrics/devcloud-maintenance/`。

发现 `item-*` 隔离原件时，先读它的 receipt 并核对原路径、身份和状态；原名被占用时不覆盖。保留原件及记录，按 receipt 的来源逐项恢复或处理，不能清空 staging 后继续跑。

## 9. 收尾和长期防复发

```bash
df -hT /data / | tee "$cleanup_run/df-after.txt"
```

把精确对象、owner、年龄、引用检查、执行命令、结果、恢复 ref/SHA 和保留原因追加到本机 `storage-audit.log`。并发任务会写入/删除，df 差值不能全算作自己的回收；镜像共享层不能按 Size 相加。正常回收为 0 也如实记录。

预部署日常入口不要无必要地使用 `--keep-worktree`；保留现场要登记 owner 和最后使用时间。生成目录、下载缓存、制品和小型证据分开登记；历史目录按当前门槛（见文首 2026-11-04 指针）及当前/回退引用检查回收。巡检只读，限制 lint 范围并在合适的只读检查中禁用写缓存。原回收器需改进批处理、真实活动判定和失败可见性，这些改进尚未全部部署。

完整历史证据：`/root/.bb/thread-storage/cleanup-20261027/清理与修复最终回执.md`、`/root/.bb/thread-storage/cleanup-20261029/清理回执.md`、该目录 `root-cause/根因与追加清理.md` 和 `long-term/长期治理诊断与方案.md`。它们用于追溯，不是下一次的删除授权或执行清单。长期调度与固定版本部署见 [本机存储维护落地方案](DEVCLOUD_AUTOMATION.md)。
