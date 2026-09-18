# Context Kit 1.0 Assessment 契约

Assessment 是 AI 对目标仓库语义的结构化评审。Context Kit 1.0 提供 Quick、Standard 和 Assurance 三档证据深度；它们共享同一套仓库责任映射，交付粒度逐档增加。Standard 使用 `schema_version: 5`；显式 Assurance 使用兼容的 `schema_version: 4` observation 契约。

Assessment、回执和报告默认写到仓库外本地目录或已忽略路径。

## 评审角色

- **AI 主审**：读取正文，建立仓库语义映射，确定 level、confidence、rationale、evidence 和 limitations。
- **发现脚本**：有界列出指令、Owner 源、文档、Catalog、服务候选、链接与 Git 事实。
- **算术/渲染脚本**：校验结构和引用，按固定权重计算、应用 P0 门禁并渲染报告。
- **独立复核者**：在 Assurance 中重新核对关键证据、判断理由和局限。
- **Owner**：确认责任、权威和验收决策。

## 仓库语义映射

Standard 和 Assurance 先映射下列责任面。每类记录候选、选中项、作用范围、权威性、理由和当前冲突。

| 责任面 | 可映射的现有实现 | AI 需回答 |
|---|---|---|
| 仓库与局部指令 | `AGENTS.md`、`CLAUDE.md`、路径规则 | 作用域、继承、安全边界和验证入口 |
| 组件/服务分母 | build/workspace/部署单元、Catalog | 哪些单元真正可独立负责、构建或运行 |
| Owner | CODEOWNERS、Catalog、组织制度 | 责任范围与当前有效性 |
| 正式知识 | `docs/`、handbook、ADR/RFC、文档站源文件 | 真源、时效和与代码/配置/测试的一致性 |
| AI 消费接口 | Context Pack、路由卡、索引、动态生成 | 何时加载、如何回链真源、如何退出 |
| 路由与控制面 | Router、Catalog、Schema、搜索/检索配置 | 选择算法、权威、排除和回归 |
| 可复用工作流 | Skill、命令、脚本、流程平台 | 触发、权限、停止条件和验证 |
| 生命周期与消费 | review_due、stale/retired、receipt/trace | 知识如何更新、退出和被观测 |
| 评测隔离 | Dataset/Gold/结果/报告与 Router 边界 | 协议如何复现，运行时如何保持隔离 |

没有对应资产也是有效发现，如实记录为待建设责任面。

## Quick 契约

Quick 仅审查 8 个 Context 控制面维度。每维输出：

```yaml
status: healthy|attention|critical|unknown
rationale: 一句与仓库事实直接相连的理由
evidence: [1–3 个 path+locator]
limitations: [最重要的证据边界]
```

Quick 最终摘要小于 80 行，最多保留 3 条优先建议。它使用定性状态，不生成数值 level 和百分制。

## Standard 契约

Standard 对 14 个维度各提交一个 AI 判断：

```json
{
  "level": 0,
  "confidence": "low",
  "rationale": "仓库事实为什么对应该等级",
  "evidence": [],
  "limitations": []
}
```

### Level

| level | 语义 |
|---:|---|
| 0 | 责任面尚未建立，或当前状态对任务形成明确风险 |
| 1 | 有少量真实资产，主要依赖个人经验或临时定位 |
| 2 | 主要路径可用，覆盖、来源、时效或执行性存在具体缺口 |
| 3 | 责任、入口、真源和验证形成可复用闭环，剩余局限已明确 |
| 4 | 闭环在声明范围内覆盖完整，具备生命周期、可执行验证和持续演进证据 |

### Confidence

| confidence | 证据覆盖 |
|---|---|
| `high` | 多个当前一手来源互相印证，关键冲突已核对 |
| `medium` | 核心结论有当前文件支持，部分覆盖、历史或消费情况待确认 |
| `low` | 只能完成有界抽样，关键来源不可访问或时效性待确认 |

Confidence 不修改 level 的算术值；报告将低 confidence 维度显示为优先复核点。

## Assurance 契约

Assurance 沿用相同的 14 个维度，并为每维展开 4 个 required observations，共 56 项。每项包含 `status/subject/rationale/evidence/limitations`，状态为 `pass | partial | fail | unknown`。Assurance level 由 observations 推导，不复制或采信 Standard 自报 level。

Assurance 还包含：

- 与 HEAD 和 worktree fingerprint 绑定的评审快照；
- 带 `assessor_id/run_id/completed_at` 的 Standard 与 Assurance provenance；
- Git `path + full commit`；
- 适用强证据槽的本地回执；
- 同一 Validator 入口的有效输入与 known-bad fixture；
- 独立 AI 复核与 Owner 可定位确认；
- 证据等级和 Assurance 附录。

Observations 是对同一维度重新形成高保证结论的细粒度依据。独立复核者调整 observation 时必须同步更新 rationale 和 limitations；脚本据此推导 level、算术与证据等级。

Grade A 使用两个本地 artifact 绑定：

```json
{
  "assessment_provenance": {
    "assessor_id": "assurance-agent",
    "run_id": "assurance-run-id",
    "completed_at": "ISO-8601"
  },
  "assurance_bindings": {
    "standard_assessment": {
      "artifact": "/local/path/standard-assessment.json",
      "artifact_sha256": "64-hex"
    },
    "independent_ai_review": {
      "artifact": "/local/path/independent-review.json",
      "artifact_sha256": "64-hex"
    }
  }
}
```

Standard artifact 必须是同一 HEAD 与 worktree fingerprint 下完整且达到 `provisional` 的 schema v5 Assessment，并填写自己的 provenance。独立复核 artifact 使用 `type: context-kit-independent-ai-review`、`schema_version: 1`，记录仓库快照、reviewer/run、与两个 assessor run 的分离声明、Standard SHA、当前 Assurance core SHA、14 维覆盖、`approved` 决定、时间与 limitations。任一 hash、快照、run 分离或维度覆盖不成立时，证据等级最高为 B，门禁最高为 `provisional`。

脚本能证明本地记录使用不同 run_id 并精确绑定 artifact；它不能证明现实身份或组织关系独立。高保证复核仍需要 Owner 根据实际执行关系判断。

## 证据契约

Standard 的每条文件证据至少包含 `kind`、仓库相对 `path`、`locator` 和 `statement`，并直接支持所属维度；`rationale` 与 `limitations` 说明能与不能外推什么。Assurance 再要求 observation 级 `proves`、`does_not_prove`、Git commit 或回执字段。

| kind | 定位 | 作用 |
|---|---|---|
| `file` | 仓库相对路径 + locator（行区间/标题/结构化键） | 证明当前内容 |
| `git` | `path` + 40 位 commit | 证明当前路径的可追溯历史 |
| `validator/test` | 入口、范围与本地回执 | 证明具体检查行为 |
| `owner_review` | 受控记录、reviewer、时间与决定 | 证明责任/权威结论已获确认 |
| `receipt/trace` | 本地 artifact + SHA-256 + 仓库快照 | 证明记录范围内的执行或消费 |
| `model_effect` | 隔离的效果 artifact | 作为独立效果附件 |

Standard 可使用文件、Git 和现有测试结果完成主审。Assurance 的 Validator/Test 回执额外记录 claim、正负输入 hashes、输出 hashes、selector、exit/time 和执行指纹。

## Finding、Action 与门禁

Finding 包含级别、维度、影响范围、直接证据和复核方式。Action 回链 Finding，给出顺序、验收和权限边界。

- P0 表示当前边界需要先处理，独立阻断门禁。
- P1 是当前建设周期的优先项。
- P2 是可进入后续演进的增强项。

Quick 只给定性优先级。Standard 在 14 维都有 level 时输出双量表；Assurance 对同一量表重新做 observation 级复核，并增加证据等级与 Owner 验收输入。

## Assurance 冻结顺序

1. 完成受评文件与 Skill 修改。
2. 记录 Skill 版本、HEAD 和 worktree fingerprint。
3. 一次性生成当前快照的回执。
4. 独立 AI 复核证据和 Standard 判断。
5. 渲染 Assurance 附录并提交 Owner 决策。

仓库或 Skill 进入新快照后，在新快照上生成对应证据集。
