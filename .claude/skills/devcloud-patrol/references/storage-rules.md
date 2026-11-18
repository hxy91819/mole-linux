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
- 仍保护活跃/排队任务、共享引用、会话、凭据、唯一数据、受保护主 checkout、数据库及明确 retained/备份。
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
- **2026-11-18最新方向：linked 与独立clone分开判定**：每天06:30（Asia/Shanghai）的同一launcher先运行 `worktree_sweep.py` 的Git登记路径（15分钟），再运行 `git_tree_sweep.py` 的全盘独立clone路径（预演45分钟/执行150分钟）；systemd整体硬限为预演63分钟/执行168分钟。`tmp_clone_sweep.py` CLI转发独立clone路径，旧两个文件的helper仅供兼容。新规则预演后按owner授权直接执行一次，改动留工作区，不提交。
  - **linked worktree专用规则**：已知主仓库取BB项目source、BB环境所属仓库及最近一次完整两盘发现的Git路径（报告中的完整发现库存）；每轮重新向Git执行 `git worktree list --porcelain -z`，不在全盘通用判定里处理linked。完整clone扫描继续登记新发现的主仓库/linked marker，供下一次快速路径使用。
    仅当未locked、不是任何未销毁BB环境（含环境路径落在该树内）、没有活进程cwd落在该树内、当前分支/ detached HEAD已有实时远端分支证明或已按原SHA归档核验、闲置满24小时，才从主仓库执行 `git worktree remove -- <登记路径>`，不加 `--force`。预演用Git status报告dirty/untracked，最终以Git remove是否接受为准；Git拒绝即保留并记录原因，无原始目录删除fallback。
    24小时活动证据只含工作树文件、它自己的gitdir及所在分支的ref/reflog；主仓库fetch或其他worktree的共同元数据活动不重置本树年龄。只证明当前分支/HEAD，不要求其他共享分支、tag或stash满足clone规则。归档仍使用允许的同主机个人/内部origin、`archive/git-tree/*`原SHA并远端核验，拒绝不改写历史；mono2不归档到GitHub。
    BB环境/cwd/登记/HEAD/24h活动在每项网络IO后、Git remove前复核，未知保留。名称引用、项目兜底、嵌套、alternates、HOME/XDG、配置/容器等全盘clone保护不用于linked；Git对ignored条目和子模块按自身remove语义处理。理由：linked的所有权和状态由主仓库登记及上述实时证据决定，不让clone的通用范围保护阻止Git管理自己的副本。
    每个主仓库随后执行 `git worktree prune --verbose`（预演加 `--dry-run`），仅由Git清理失效的登记；不直接删除残缺gitfile目录。无法解析的Git路径列为unresolved_mains/保留，主仓库自身仍走下面原clone保护。首次人工执行绑定专用完整预演 `--reviewed-preview`，报告schema_version=3、policy=linked。
  - **以下规则仅适用于独立clone，保持原严格策略不变**：全盘入口仍发现所有Git标记并把linked作为引用来源/已知主仓索引，但只把独立clone送入通用计划与删除路径；不套用旧131项路径白名单，自然闲置到期按原规则处理。
  - **发现**：物理遍历 `/` 和 `/data`，每根 `-xdev`，按 device/inode 去重，不跟随软链，
    跳过 `/proc /sys /dev /run`；没有深度、隐藏目录、项目路径或 BB thread-storage 排除。
    `.git` 为目录是独立 clone，为文件是 linked/gitfile；发现仓库后仍扫描其后代，包含 ignored 下的仓库。
    bare 仓库只作为引用来源。损坏 gitfile、gitdir 丢失、未登记 gitfile（含 submodule）均列出保留。
  - **仅排除 owner 存储**：Docker 实时 data root、containerd、Nix/Guix store、npm `_cacache`、
    pnpm store、pip/uv 缓存、Cargo registry/git、Go module/VCS 与 Go/lint build 缓存；解析迁移软链的真实根。
    依赖容器 `node_modules/vendor/.venv/venv/__pypackages__` 仅在 package.json、Go/Composer/Bundler/Cargo 元数据、pyvenv.cfg 或 PEP 582 布局等 owner 证据存在时裁剪，pnpm 按版本/files 布局确认；仅同名目录仍遍历。
    理由：其中的 Git 结构是安装器/缓存的实现状态，生命周期归 owner 命令；整库删除会破坏依赖和工具复用。
    `.pyenv`、`.nvm` 等工具安装本身不按名称排除，依赖实际使用引用保护。排除路径及理由写入报告。
  - **占用保护**：BB 项目全部 source 路径、任何未 destroyed 环境路径、原有忙碌/近期 BB 线程、
    进程 cwd/exe/fd/maps 与 PATH、`*_HOME/*_ROOT/*_DIR`，容器（含 stopped）挂载、挂载点、外部软链，
    shell 初始化文件、systemd 系统/用户单元、crontab、BB 自动化保存的命令引用，均保护包含该引用的工作树。
    引用父容器本身不等于引用其中每个子 clone；不借引用保护恢复路径白名单。未成功物化、无 path/instance 的
    error 环境不表示磁盘上有环境；有 provider input path 时仍保护该路径。其他 BB 路径未知整轮保留。
    shell 不执行；保留引号内空格，展开 HOME、普通变量赋值、默认/必填/替代参数（:-、:=、:?、:+）和已有 glob 匹配，动态路径赋值传播 unknown；for 序列同样先展开，命令替换保留原单词字面前缀。
    先替换已读取真实进程环境的全部可能值，以及自动化显式 env、确定的 BB 项目/环境 cwd；不臆造 BB 注入变量。
    系统 profile 的固定 `$(/绝对路径/go env GOPATH)` / GOROOT 仅以同一 Go 可执行文件的只读 `go env -json` 元数据替换，其他命令替换保持未知。
    **引用未知按可能范围保留**（暂停/过期一次性任务同样适用）：每个动态操作数分别取第一个未解析变量、glob 或命令替换之前的绝对字面前缀（纯 glob 无论有无匹配都建范围，无匹配 for 仍分析 body），
    保留前缀下及包含前缀的所有 Git 树；目录名中途截断的前缀按字节前缀匹配，软链和 `/tmp` bind alias 先归一父目录；未完成的最后组件保留拼写，并补充已有物理目标的子树范围。
    无前缀时，保留该自动化所属项目的 source checkout、未销毁环境 worktree，以及与Git源或项目元数据具有相同有效 remote URL 的 clone树；项目source为非Git容器时，只保护source点及项目登记环境的包含树，不保护容器后代，不从环境remote扩展其他项目。无source的remote-only项目仍按已知remote归属；source/环境/remote均无法定位才整体保留。
    URL 比较（含项目元数据）应用 Git fetch/push 重写，并统一常见 SSH/HTTPS 表示及网络 `.git` 后缀；本地只按物理路径及 `/.git` 地址归一，不把不同目录 foo/foo.git 合并，不使用已缓存远端 ref 代替恢复证明。
    只有既无前缀又无可定位项目时才因引用表达式未知整轮保留；完整库存/权限/进程扫描失败仍整轮保留。
    缺 HEAD 等残缺树自身保留；其可读 remote/URL rewrite 配置可在自建临时 bare Git 上下文解析，不写原仓库。条件 include、不可读配置或仍无法解析均整轮保留，不静默丢弃引用。报告逐条记录 source、expression、scope、prefixes、project_id 和 protected_paths，
    即使路径已被其他规则保护也列全；global_unresolved 点名全局阻断来源。scoped unknown 不使 complete=false，删除前所有复核复用相同范围策略。
    Python3 自动化按声明的解释器用 AST 只读分析，不把 Python 数据循环当 shell 未知路径；跟踪字面 Path、文件调用、有限 glob 循环、已观测环境及 BB 实际 cwd。对标准json.load/json.loads从可证明路径读取的当前≤1MiB普通JSON文件，只读解析字段及get值；校验读取前后文件身份，缺失/字段未知/解码定制/值被不可证明修改时回到unknown，不执行脚本或导入。已知helper的*args逐参数传播；不透明展开仍unknown。动态文件操作数没有可证明前缀时仍按项目兜底；不得通过执行 Python/shell 求值。原子写入的 with_suffix/with_name 保留已知目录，datetime.replace 不当作文件替换。每条未知包含自动化ID、名称、启停、最后运行时间/状态和实际保护路径；暂停任务同样适用。
  - **2026-11-17追加证据规则**：remote 的 owner/repo，或长度≥4且非通用词/纯版本号的目录名，出现在配置中即保留；
    匹配大小写归一的完整名称/路径组件，网络 .git 后缀归一，同remote的副本中，若有树已因非名称证据保留，名称引用归属该树，其余副本继续全部条件判定；不以名称证据循环满足另一名称引用。没有非名称保留副本时仍全部按歧义保留，不设置路径豁免。报告记录satisfied_by、other_reason、protected_by_name。
    配置范围为 passwd HOME 下一层 dotfiles、~/.config/**、/etc/** 的≤1MiB普通文本；跟随配置目录的软链并按物理目录去重，
    二进制、大文件、shell/editor历史及历史临时副本不是此处配置来源。读取失败不消费部分结果。
    活进程（含清理器自身）的cwd只保护包含cwd或根等于cwd的Git树，不保护cwd下面的树；HOME、XDG_*_HOME保护完整子树，若值为 /、挂载点（含物理别名）、或该进程真实 UID 的 passwd home，
    则仅保护该目录及其包含树，不把保护扩散到后代。普通 home 的子目录（如 ~/.config、~/.cache）仍保护子树；UID无passwd记录时保留较大的子树范围。
    清理器worker的cwd设为/；cwd=/data/code不再使该容器下全部仓库受保护，cwd位于某仓库内部仍保护该仓库。
  - **本轮执行授权条件**：旧034c预演131项的子集核验已于11-17完成，用于验证当次规则变更没有扩大范围；不作为日常候选白名单。闲置自然到期不需审批；tmux-resurrect/continuum、实例HOME下codebuddy、异源dmca/upstream和evidence仍受保护。本轮11-18三项引用规则收窄按本节开头的owner追加授权判定。
    满足本次规则来源核验即视为owner已审核通过：发布执行版、开启06:30并手动执行一次；发现其他规则被意外放宽时保留并回报。首批执行用 `--reviewed-preview <新完整预演>` 在任何归档/删除之前比较，
    有新候选先整批不执行；自然到期可自主重做完整预演后继续，意外规则扩大才恢复预演并回报。此比较是本批授权核验，不成为日后按路径白名单清理；后续06:30按同一全盘证据规则运行。
    owner核验的 account-limits/v0.3.2、devcloudtool/vim、bats-core-1.13.0 及 lighthouse-bak 不加路径例外；名称碰撞按上述同源归属规则处理；没有其他保留副本时才保留并说明。
  - **本地全条件**：所有非 `.git` 条目及 Git 元数据（含 FETCH_HEAD、ORIG_HEAD、refs 与 reflog）最新 mtime 满 7 天（168h），所有位置统一；
    status 含全部 untracked 为空、无 stash、无进行中的 Git 操作/锁、无已填充子模块，
    ignored 中无凭据（无主 checkout 同内容豁免，也不豁免依赖目录内凭据）。包含嵌套仓库的父树保留；
    位于另一Git树内的子树（含ignored目录）若是父仓库的linked worktree，或其有效remote与父仓库任一remote规范化后相同，就不适用嵌套保护，继续判定其他全部条件；否则（异源、无remote或关系未知）保留，原因写“嵌套于 <父仓库>，由该项目自行保留”。每次本地复核重读当前common gitdir及有效fetch/push URL，应用Git重写/物理本地路径归一，不沿用旧库存中的同源结论；多层嵌套逐层判断，异源外层仍保护取证快照。
    主仓库还有登记 linked worktree 时保留；被其他发现仓库（含 bare）的 remote URL /
    pushurl（含 Git insteadOf/pushInsteadOf 展开后的全部有效 URL）/ alternates 引用时保留。借用对象的一方自身存在 `objects/info/alternates` 不构成保留理由；删除仅作用于借用方自己的目录，不会删除外部对象库。借用方仍须满足干净、7天、所有refs实时远端证明或先归档核SHA等全部条件；借用配置文件的近期mtime仍计为Git活动。主 checkout 仅在这些保护下判定，不再按路径或身份一律排除。
  - **恢复证明**：每个本地 branch、HEAD 和 tag 都要在实时远端上证明；tag 验 exact object SHA，
    branch/HEAD 可验位于实时远端分支历史。远端查询失败保留；指向待删树自身/内部的 local/file remote 不作独立恢复证明；外部本地 remote 的包含仓库由完整 URL 引用索引保护，bare 库只作引用源不进入删除。缺失 ref 只能向同主机个人命名空间或内部 origin 的 `archive/git-tree/*`
    推原始对象，逐 ref `ls-remote` 核 exact SHA；tag 用 tag ref，不能用压缩快照替代原历史，不 force push，
    mono2 不推 GitHub，无目的地/归档失败均保留。预演只查询，不推送。
  - **删除与复核**：全批远端查询/归档结束后重扫两盘及全部引用，再在每个 sink 前复核本地状态与占用；
    unknown 或预算不足即保留。linked 必须从主 checkout 调 `git worktree remove -- <path>`，不加 `--force`；
    文件系统根和挂载点只列出保留；clone 仅 fd-based 删除该目录，逐级打开 ancestor 时 O_NOFOLLOW，不跟随任何软链。预演与执行共用判定。
    不删除 BB 环境。每对象 JSON/JSONL 包含路径、类型、实占（失败为 null）、
    决定/原因、refs/SHA、归档证据和耗时；嵌套占用不相加冒充可回收量。完整发现或引用扫描失败整轮保留。
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

执行入口：`$devcloud-storage-upkeep` 的 `scripts/git_tree_sweep.py`、`scripts/docker_leftovers.py`、
`scripts/go_cache_trim.py`、`scripts/pnpm_store_prune.py`，默认预演；全盘 Git 按本轮子集条件核验后开启执行。全盘回归 `tests/test_git_tree_sweep.py`；旧 helper 回归：`tests/test_devcloud_storage_upkeep.py`（已合并删除、仅本地提交归档、WIP 快照、
凭据文件保留、进程占用保留、近期活动保留、凭据类 ignored 文件与主 checkout 不同内容则整树保留、
node_modules/虚拟环境内凭据不阻塞、ls-remote 失败保留、/proc 非退出错误整轮保留、docker ps/inspect
失败整轮保留、删除后同步处理精确匹配 BB 环境与拒绝/故障 fail-open、busy 锁跳过、retained_volumes 排除；go_cache_trim：`test_old_go_cache_entries_are_deleted`、
`test_recent_go_cache_entries_are_kept`、`test_non_matching_go_cache_names_are_kept`、
`test_go_cache_symlink_is_kept_and_target_is_untouched`、
`test_missing_go_cache_readme_deletes_nothing_and_exits_1`、`test_go_cache_dry_run_deletes_nothing`、
`test_go_cache_quiet_is_silent_when_nothing_is_deleted`）。定时任务现状以 `bb automation list` 为准。

## 盘点的计量与只读要求

### 水位刹车与增长归因（2026-11-14）

每 15 分钟读取 `/` 与 `/data` 的 `statvfs` 可用字节；低于 150 GiB 预警、低于 50 GiB 危险。首次越线、warning→danger 升级或两小时冷却结束后再次触发才通知；冷却期静默，恢复正常后重新越线视为首次。运行中或查询未知的任务跳过并在触发简报注明；clean-tmp 与 cron 共用 `clean_tmp_runner.py`，非阻塞锁由后台执行链持有到结束，两盘同时越线合并为同一 worker 顺序执行两根；新入口用 `-I`，旧执行器仍用 `-B`（同目录依赖）。路径、timeout、预算与 JSONL 记账只有一个入口。派发后先保存冷却再通知，通知失败仍非零退出。`--dry-run` 仅打印计划，不写状态、不调用 BB 或包装器。真实触发 stdout 输出一行摘要，正常/冷却完全静默。每日07:00按/data顶层优先、system-tmp逐项最后，以8并发、ionice空闲级计量；单根最多780秒、全轮840秒，BB超时900秒，须在07:30检查前结束。/data/docker不du，改Docker system df分类数值比较（镜像、容器、卷、构建缓存）；/data顶层未测根单列data_top_unknown。超时/失败/缺类不当0比较，历史保留30天。

thread-storage由一次物理du遍历拆成共享go-build、共享golangci-lint、thr_*真实目录合计（bb:threads）、非线程条目合计（bb:non-thread）；后者排除两共享Go缓存，包含共享缓存其他项与目录元数据。各项按下表登记归属。/data/bb-data、thread-storage、/data/system-tmp、/data/code与/root/.cache父根只进Top5、不重复报警；/data/code和/root/.cache单次du遍历输出直接子项，增加观察粒度但不重复遍历；两共享Go缓存也只观察，由每日检查第2项核≤100GiB与无超过1520分钟的哈希条目。unknown分项只在本次与前两次三样本有效、严格递增、累计≥3GiB时告警；缺测/缺项中断连续性，不足三样本明示insufficient_samples。已登记分项仍按单日>20GiB告警。前次无指标的新子项以0作合成基线进Top5并标baseline=new，告警只用真实历史，不把前次unknown改成0。

计量du固定LC_ALL=C；非零退出仅在stdout有本根合法总量、stderr每行全为条目消失（No such file or directory）时记ok，附vanished诊断行数与最多5条vanished_examples，不作为每日检查不符合。分项遍历只在扫描父根附备注，不重复计数。混有权限/I/O等错误、无本根合法总量、信号退出或超时仍记unknown；此例外只用于只读计量，不用于清理候选扫描。

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
  「经验教训」；现行 worktree 规则见上文 owner 2026-11-17 策略和 `git_tree_sweep.py`。
  当前保护以 2026-11-17 规则为准；旧的全量主 checkout 豁免与删除 BB 环境已被替代。

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

## 已知来源指纹

用内容、运行状态和路径证据确认归属。本机例子见 `.local/devcloud/publication-backup/.claude/skills/devcloud-patrol/references/storage-rules.md`。

### 增长计量的路径指纹

以下为上述已知来源中可仅凭路径确认归属的部分。发布时将表复制为 `storage_growth_fingerprints.md`，
与 `storage_growth_measure.py` 同目录入 manifest；运行仅读该快照内副本，规则修改须重新发布。
BB/Codex 会话根与 pnpm 根按本机布局登记；混合根（如 `/data/code`、`/root/.cache`）、
匿名数据库卷和泛称 Go 缓存仍需内容/进程证据，不凭目录名套用项目归属，未匹配为 `unknown`。

<!-- growth-fingerprints -->
| 路径指纹 | 归属 |
|---|---|
| `/data/itfs` | itfs |
| `/data/.pnpm-store` | pnpm store |
| `/data/codex` | Codex sessions |
| `/data/bb-data/thread-storage/.shared-cache/go-build` | shared Go build cache |
| `/data/bb-data/thread-storage/.shared-cache/golangci-lint` | shared golangci-lint cache |
| `bb:threads` | BB thread directories |
| `bb:non-thread` | BB root non-thread entries |
| `docker:images` | Docker images |
| `docker:containers` | Docker containers |
| `docker:volumes` | Docker volumes |
| `docker:build-cache` | Docker build cache |
| `/data/bb-data` | BB |
| `/data/bb-data/thread-storage` | BB thread storage |
| `/root/.bb/*` | BB |
| `/data/system-tmp/oc-local-smoke-*` | opencraft |
| `/data/system-tmp/saiens_bundle*` | SaiensCreditSystemRails |
| `/data/system-tmp/saienscreditsystemrails_*` | SaiensCreditSystemRails |
| `/data/system-tmp/fast-feedback-rules-followup_*` | SaiensCreditSystemRails |
| `/data/system-tmp/scan-preview-drag-experiment_*` | SaiensCreditSystemRails |
<!-- /growth-fingerprints -->

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
  每日北京时间 03:30 cron 与水位刹车共用不可变快照内 `clean_tmp_runner.py`（crontab `# devcloud-clean-tmp` 行）。包装器持 `/var/lib/devcloud-storage-upkeep/clean-tmp.lock`，子进程继承 fd，锁忙不等待；外层 timeout 1790 秒，
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
- Git 工作树统一由每日 06:30 的 `git_tree_sweep.py`（模式按本轮授权条件），旧 06:45 任务停用；
  不再补删 BB 环境，pnpm 每周日 05:30 逐版本 owner prune。
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
「决策记录」和「经验教训」。现行入口是 `git_tree_sweep.py`，规则见上文 owner 2026-11-17
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

linked年龄补充：计算工作树文件/目录和自身gitdir的实际文件（index、HEAD、FETCH_HEAD、reflog等）mtime，不以Git管理目录自身mtime作为活动。实测3棵mono2树仅管理目录被临时锁/探针刷新，实际文件均旧于24h；目录mtime不应使它们永久刷活。此修正只用于linked，clone保持原7天全部元数据规则。
