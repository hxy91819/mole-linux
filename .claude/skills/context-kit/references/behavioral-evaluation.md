# Context Kit 1.0 行为复核

维护本 Skill 时在临时仓库执行，不调用模型 API，不把结果写入目标仓库。脚本测试确定结构、预算和算术；AI 语义质量由前向任务复核。

## Quick

- 默认 `context_kit.py --repo .` 输出 Markdown，而非完整 JSON；
- 不超过 80 行，目标小于 8 KiB；
- 只呈现 8 个 Context 控制面核查提示、有限候选和边界；
- 不从目录名、数量或扫描命中自动给成熟度分。

## Standard

- 模板为 schema v5，每维只有一个 AI 判断：`level/confidence/rationale/evidence/limitations`；
- 14 维权重分别归入两套 100 分量表；
- 脚本校验仓库相对路径、locator 和当前快照，只做算术、coverage、score range、P0 门禁与渲染；
- AI 必须读取正文、Git/Owner/现有检查等仓库事实后判断，不能用固定路径存在性代替语义；
- Standard HTML 不展示 Assurance 的回执与执行指纹版块。

## Assurance

- 仅显式选择时生成 schema v4 的 56 observations；
- 适用强证据槽继续校验 HEAD/worktree、claim、同入口正负输入、本地回执和独立复核；
- `ready_for_owner_acceptance` 仍不是 Owner 已接受或外部认证。

## 四类前向仓库

1. **陌生成熟仓**：使用 `CLAUDE.md`、handbook、Backstage 或自有 Router；应按职责复用，不因目录名不同扣分。
2. **空仓/欠建设仓**：给出 `empty/underbuilt` 画像与最小建设顺序，不创建空目录换分。
3. **过度建设仓**：通过正文、链接和 Git 识别平行真源、重复和入口过载；未获授权只给迁移清单。
4. **成熟受管仓**：Standard 应能基于现有文件、Owner、Git 和测试完成 14 维判断；是否追加 Assurance 由用户决定。

## 三轮最终检查

1. **契约检查**：frontmatter、Quick 预算、Standard schema/算术、Assurance 兼容、Renderer 和安装路径独立性。
2. **语义前向检查**：让独立 AI 仅拿 Skill 与代表性仓库完成 Quick/Standard，核对理由、证据定位、边界和建设建议。
3. **发行检查**：从 ZIP 解压运行全量测试，读回 Markdown/JSON/HTML，扫描本机路径、凭据、本地实验、模型答案、模拟结果与生成报告。

发现稳定问题时先添加能复现预期行为的测试，再修改实现或文档。
