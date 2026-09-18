# Context Kit 1.0 报告契约

TMR（Context Readiness & Remediation Report）把仓库画像、逐维判断、证据、建设规划和复核结果放在同一交付中。报告固定标记 `agent_assisted_self_assessment / not_certified`，用于团队自查和 Owner 验收输入。

JSON 用于机器处理，Markdown 用于评审，单文件 HTML 用于分享。Assessment、报告与运行记录默认写到仓库外本地目录或仓库已忽略路径。

## 三种报告深度

| 模式 | 输出 | 数值 | 适用场景 |
|---|---|---|---|
| Quick | 小于 80 行的 Markdown | 无 | 首次进入、日常健康检查 |
| Standard | 14 维 JSON/Markdown/HTML TMR | 两套 100 分、coverage、score range | 完整评测与三轮建设 |
| Assurance | 同 14 维的 56 observations 与证据报告 | observations 推导分数 + Assurance 证据等级 | 用户明确要求的高保证复核 |

## Quick 必须包含

1. 仓库路径、扫描文件数、Git 可用性、dirty 数量与链接候选；
2. 文档、指令、Owner、服务、控制面、Skill、评测隔离和重复候选的数量与最多 3 个路径；
3. 8 个 Context 控制面维度的 AI 核查提示；
4. “候选不自动等于成熟度”的边界，以及 Standard/Assurance 下一步。

Quick 不打印完整 discovery JSON，不展示 0 分，不执行 Validator。

## Standard 必须包含

1. `product_version: 1.0`、`schema_version: 5`、`assessment_mode: standard`；
2. 仓库画像 `empty/underbuilt/overbuilt/managed` 与理由；
3. 8 个 Context 维度和 6 个 Documentation 维度；
4. 每维 `level/confidence/rationale/evidence/limitations`；
5. 每维考察问题、权重、为什么这样加权、能与不能证明什么；
6. 两套分数、已评覆盖率、置信度分布和部分评测的分数区间；
7. P0/P1/P2、三轮建设建议、验收条件和权限边界；
8. 当前快照、Git 可用性、证据定位与未完成能力。

Standard 核心形状：

```json
{
  "product_version": "1.0",
  "schema_version": 5,
  "assessment_mode": "standard",
  "measurement_type": "agent_assisted_self_assessment",
  "certification_status": "not_certified",
  "repository": {
    "head": "40-hex-or-null",
    "assessment_scope": "repository scope",
    "worktree_fingerprint": "64-hex"
  },
  "repository_profile": {
    "class": "managed",
    "rationale": "仓库事实为什么对应这一画像",
    "tags": []
  },
  "assessment_provenance": {
    "assessor_id": "agent-id",
    "run_id": "run-id",
    "completed_at": "ISO-8601"
  },
  "score": 75,
  "score_range": {"min": 75, "max": 75},
  "coverage": {"assessed_dimensions": 14, "total_dimensions": 14, "percent": 100},
  "confidence_summary": {"high": 8, "medium": 5, "low": 1},
  "dimensions": {},
  "documentation": {"score": 75, "score_range": {"min": 75, "max": 75}},
  "dimension_assessments": {
    "context_control_plane": {
      "entry_routing": {
        "level": 3,
        "confidence": "high",
        "rationale": "仓库事实为什么对应 level 3",
        "evidence": [{"kind": "file", "path": "AGENTS.md", "locator": "L1-L30", "statement": "最短入口与作用域"}],
        "limitations": ["尚未覆盖归档服务"]
      }
    },
    "repository_documentation": {}
  },
  "findings": [],
  "actions": []
}
```

部分维度未评时，点估计只基于已评权重，同时显示 coverage 和理论 `score_range`：下界把未评维度按 0 计，上界按满分计。报告不得把低覆盖点估计包装成完整结论。

Standard 的 `complete` 要求 14 维和仓库画像同时完整；画像缺失时可以保留分数与 coverage 供补录，但状态仍为 `partial/not_assessed`。

## Assurance 附录

Assurance 使用 `schema_version: 4` 的 observation 契约，对相同 14 维重新推导成熟度。报告增加：

- 每维 4 个 observations 的 status、subject、rationale、evidence 与 limitations；
- Git `path + full commit`、HEAD 和 worktree fingerprint；
- 适用 Validator/Test 的本地回执与 known-bad fixture；
- Standard Assessment artifact 的 SHA-256 绑定；
- 绑定当前 Assurance core 的独立 AI 复核 artifact；
- Owner 可定位确认和 A/B/C/Unknown 证据等级。

Standard 页面不展示这些专用附录；只有显式 Assurance 才加载和呈现。

## 门禁表述

```text
Context 控制面 >= 85 且 Context P0 = 0
AND
Repository Documentation >= 85 且 Documentation P0 = 0
```

- `not_assessed`：仍是 Quick，或 Standard 尚无有效 AI 判断；
- `blocked`：存在 P0；
- `needs_improvement`：Standard 14 维和画像已完整，任一量表低于 85；
- `provisional`：Standard 14 维和画像完整、两套量表达到 85 且 P0=0，可进入 Owner 验收讨论；
- `ready_for_owner_acceptance`：显式 Assurance 达到 A 级证据组合，可提交 Owner 决策。

状态只描述声明范围和证据时点。除非另有隔离实验，不外推模型准确率、Token、时延或费用改善。

## 最终三轮检查

1. **契约与行为**：Quick 预算、Standard schema/算术/覆盖率、Assurance observation 兼容、四类仓型与安装路径独立性；
2. **证据与边界**：AI 理由、文件定位、Git、P0、目录职责、评测隔离；Assurance 再检查回执与独立复核；
3. **发行读回**：从 ZIP 解压运行测试，读回 Markdown/JSON/HTML，扫描敏感信息、本机路径、实验结果、生成报告和版本标识。
