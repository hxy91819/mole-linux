---
name: context-kit
description: Use when 用户需要快速了解代码仓库的 Context 建设状态，或者需要 AI 主审的标准评测、高保证复核与三轮建设。
---

# Context Kit 1.0

Context Kit 1.0 是一套面向任意代码仓库的 Context 工程评测与建设方法。AI 负责理解仓库语义、判断责任边界和给出理由；脚本负责有界发现、算术、契约校验和渲染。它不假定固定目录名，优先映射并复用仓库已有的指令、文档、Catalog、Owner 体系、Validator 和工作流。

当前已加载 `SKILL.md` 所在目录的绝对路径记为 `SKILL_ROOT`；安装位置由 Harness 决定。

## 选择模式

| 模式 | 何时使用 | AI 审查范围 | 交付 |
|---|---|---|---|
| **Quick（默认）** | 首次进入、日常健康检查、用户只想快速了解现状 | 8 个 Context 控制面维度，定性判断 | 小于 80 行的结论、风险、优先级和下一步 |
| **Standard** | 用户需要可比较的完整评测或准备建设 | 8 个 Context 维度 + 6 个 Documentation 维度 | 14 维 Assessment、双量表、P0/P1/P2 和建设建议 |
| **Assurance（显式可选）** | 高风险边界、重要验收、需要更强内部证据链 | 同 14 维的 56 observations + 回执 + known-bad fixture + 独立复核 | Assurance 报告、证据等级和 Owner 验收输入 |

用户未指定时使用 Quick。Quick 发现跨服务、权威冲突、P0 风险或明显过度建设时，建议升级为 Standard；Assurance 始终由用户或 Owner 明确选择。

## Quick：默认快速评测

1. 保护 dirty worktree，读取仓库和目标路径的指令、README 与构建入口。
2. 运行有界发现：

   ```bash
   python3 "$SKILL_ROOT/scripts/context_kit.py" --repo . --format summary --max-items 12
   ```

3. 对 [rubric.md](references/rubric.md) 的 8 个 Context 控制面维度逐项给出 `healthy | attention | critical | unknown`，每项引用 1–3 个可定位的仓库事实。
4. 输出不超过 80 行：仓库画像、8 维状态、P0/P1、三条以内建议、已读证据与局限。

Quick 不产生百分制分数，也不执行仓库 Validator。它的价值是快速建立可复核的现状地图。

## Standard：AI 主审的 14 维评测

Standard 必须完整读取 [assessment-contract.md](references/assessment-contract.md) 和 [rubric.md](references/rubric.md)。AI 为 14 个维度分别提交：

- `level: 0..4`；
- `confidence: high | medium | low`；
- `rationale`；
- `evidence[]`；
- `limitations[]`。

AI 的语义判断是 Standard 的主体。脚本只校验字段与证据引用、按权重完成算术、处理 P0 门禁并渲染 JSON/Markdown/HTML。每个维度的理由必须说明“仓库事实为什么对应这个 level”；confidence 反映证据覆盖和时效，不参与算术加分。

```bash
python3 "$SKILL_ROOT/scripts/context_kit.py" --repo . --mode standard --write-template <local-assessment.json>
# AI 读取仓库事实并填写 14 维 Assessment 后：
python3 "$SKILL_ROOT/scripts/context_kit.py" --repo . --mode standard \
  --assessment <local-assessment.json> --format json --output <local-result.json>
python3 "$SKILL_ROOT/scripts/render_context_report.py" --repo . \
  --assessment <local-assessment.json> --output <local-report.html>
```

Assessment 与报告写到仓库外本地目录或已忽略路径。部分维度无法判断时保留空 level，并展示 coverage 与 score range。

## Assurance：高保证复核

Assurance 沿用 Standard 的 14 个维度，但以每维 4 个 required observations 重新复核并推导 level，共 56 项；它不接收 Standard 自报 level。完整阅读 [behavioral-evaluation.md](references/behavioral-evaluation.md)，并执行：

1. 冻结 Skill 版本、HEAD 和 worktree fingerprint。
2. 完成同一快照的 Standard Assessment，填写 Standard 与 Assurance 的 `assessment_provenance`，并以 SHA-256 绑定 Standard artifact。
3. 对适用的强证据槽，使用同一仓库 Validator 分别运行有效输入和 known-bad fixture，保存 claim、输入/输出 hash、selector、exit/time 与执行指纹。
4. 由不同 run 的 AI 复核 14 维 Assessment，把复核 artifact 绑定到 Standard SHA 和当前 Assurance core SHA；Owner 对责任和权威结论做可定位确认。
5. 按 [assessment-contract.md](references/assessment-contract.md) 校验绑定，并依 [report-contract.md](references/report-contract.md) 生成 Assurance 附录。

Assurance 使用仓库文件、Git、本地检查和人工复核；模型效果数据可作独立附件，不是本模式的必需输入。

生成 Assurance 模板时显式运行：

```bash
python3 "$SKILL_ROOT/scripts/context_kit.py" --repo . --mode assurance --write-template <local-assurance.json>
```

## 用户确认后建设

用户明确选择建设后，创建 Goal，依 [remediation-playbook.md](references/remediation-playbook.md) 执行三轮：盘点与责任、权威与控制面、压缩与交付。目录和文件内容依 [repository-structure.md](references/repository-structure.md) 与 [document-contracts.md](references/document-contracts.md) 映射现有仓库。

建设不自动授权 commit、push、MR、发布、生产、外部写入、删除或归档。

## 证据与边界

- 文件、Git、Validator/Test、Trace 和 Owner Review 按 [assessment-contract.md](references/assessment-contract.md) 定位。
- 23 条通用原则及其就近业界依据见 [23-rules.md](references/23-rules.md)。
- 公开来源、适用边界与权重设计见 [industry-evidence.md](references/industry-evidence.md)。
- 所有模式都是 `agent_assisted_self_assessment / not_certified`；结论仅适用于声明的仓库范围和证据时点。

## 自检

维护 Context Kit 1.0 时运行：

```bash
python3 -m unittest discover -s "$SKILL_ROOT/tests" -v
```

读回 Quick 摘要、Standard Assessment 和 Assurance 附录，核对模式标识、证据定位、分数算术、敏感信息、本地保留与结论边界。
