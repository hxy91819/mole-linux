# Context Kit 1.0 评分与定性标准

Context Kit 1.0 用同一套维度支持三种深度：Quick 对 8 个 Context 控制面维度做定性判断；Standard 由 AI 主审 14 维并输入 level/confidence；Assurance 展开 56 observations 和高保证证据链。

公开行业资料支持“有限 Context、分层指令、权威与责任、文档分工、可验证流程”这些方向；具体权重是 Context Kit 1.0 按仓库失败影响制定的风险配额，便于团队比较和追踪，不代表自然定律。

## Quick：8 维定性状态

| 状态 | 判断 |
|---|---|
| `healthy` | 主要责任闭环可用，当前证据未显示优先风险 |
| `attention` | 已有真实能力，覆盖、权威、时效或执行性需要补强 |
| `critical` | 当前事实对正确路由、安全边界、权威或评测隔离形成 P0 影响 |
| `unknown` | 当前有界读取无法支持可靠判断 |

Quick 按下表 8 维输出状态、1–3 个可定位证据和一条局限，不做百分制运算。

## Standard：AI 主审与算术

AI 为每维提交 `level: 0..4`、`confidence: high|medium|low`、`rationale`、`evidence[]` 和 `limitations[]`。Level 的语义见 [assessment-contract.md](assessment-contract.md)。

- 维度得分 = `weight × level / 4`。
- Context 与 Repository Documentation 各 100 分，分别呈现。
- confidence 反映证据覆盖与时效，不改变算术得分。
- P0 独立阻断门禁。
- 维度缺失或无法判断时，报告已评权重的点估计、coverage 和理论 score range；coverage 未满时不作完整结论。

脚本读取 AI 判断后只完成结构校验、算术、门禁和渲染。

## Context 控制面 100 分

| 维度 | 权重 | AI 主审问题 | 为什么这样设计（就近依据） |
|---|---:|---|---|
| 入口与分层路由 | 15 | Agent 能否从最短入口按作用域到达局部知识 | 所有任务都经过入口，因此是直接风险项。[OpenAI AGENTS.md](https://developers.openai.com/codex/guides/agents-md) 与 [GitHub 路径指令](https://docs.github.com/en/copilot/customizing-copilot/adding-repository-custom-instructions-for-github-copilot) 都强调分层、就近的仓库指令。 |
| 真实任务路由覆盖 | 15 | 代表性任务是否命中必要 Context，排除项是否生效 | 路由决定 Agent 实际获得的信息，影响与入口同级。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) 建议维持最小高信号 Context。 |
| 权威性与知识完整度 | 15 | 关键事实能否回到当前真源、Owner 和版本 | 错误真源会直接导致错误行动。[Backstage System Model](https://backstage.io/docs/features/software-catalog/system-model/) 用 Component/System/Domain/Owner 管理责任与关系；[ADR](https://adr.github.io/) 提供可追溯决策记录。 |
| 规则可执行性 | 15 | 关键规则是否有检查、测试、人工门禁和明确结果 | 它决定制度能否成为可重复行为，因此与前三项共同构成 60 分核心层。[Anthropic Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) 强调简单可组合流程与可验证反馈。 |
| 指令链 Token 预算 | 10 | 默认注入、按需读取和例外是否有可解释预算 | Token 是效率约束，对正确性的影响通过 Context 竞争间接体现，因此为 10。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) 将 Context 视为有限资源。 |
| 任务状态与反馈闭环 | 10 | 长任务能否恢复，验收和失败能否进入下一轮 | 闭环决定持续改进能力，为长期稳定性项。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) 讨论压缩、外部记忆与长任务管理。 |
| Skill 与工作流治理 | 10 | 可复用流程的触发、资源、权限和验证是否清晰 | 工作流改善复用效率，不替代仓库事实，因此为 10。[Agent Skills Specification](https://agentskills.io/specification) 定义入口与资源渐进加载。 |
| 生命周期、消费观测与评测隔离 | 10 | 知识如何创建、复查、退出并被观测，评测资产如何隔离 | 它是长期治理与可信评测的基础层。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) 支持外部记忆与结构化管理；[Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) 支持可验证反馈。 |

## Repository Documentation 100 分

| 维度 | 权重 | AI 主审问题 | 为什么这样设计（就近依据） |
|---|---:|---|---|
| 文档盘点与分类 | 20 | 文档母集、责任分类、重复与生命周期是否清晰 | 母集是覆盖率和治理的分母，因此为 20。[Diátaxis](https://diataxis.fr/) 说明文档应按用户任务分工。 |
| 服务责任与入口 | 25 | 真实服务分母、Owner、代码与最短入口是否共同演进 | 服务是日常代码、运行、责任和局部知识的主要边界，影响面最大，因此为 25。[Backstage System Model](https://backstage.io/docs/features/software-catalog/system-model/) 直接建模 Component、System、Domain 和 Owner。 |
| 跨服务工程知识层级 | 20 | 架构、Domain、Journey、决策和运行知识是否按职责分层 | 跨服务真源影响多个边界，与母集同为 20。[C4 Model](https://c4model.com/) 支持分层架构视图；[ADR](https://adr.github.io/) 支持决策可追溯。 |
| 链接与入口完整性 | 15 | 文件、图片、锨点和真源链接是否可解析 | 链接是人与 Agent 沿证据链导航的直接依赖，为 15。[Agent Skills Specification](https://agentskills.io/specification) 使用入口与可组合资源支持渐进加载。 |
| Context 派生接口 | 10 | AI 使用的紧凑内容是否回链正式真源并标明消费边界 | 它是正式知识进入运行时的接口，为 10。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) 支持最小高信号 Context 与按需管理。 |
| 内容质量与时效 | 10 | 抽样正文能否支持任务，事实、Owner 和复查时间是否有效 | 它用于校准结构与真实内容，但只能有界抽样，因此为 10。[Diátaxis](https://diataxis.fr/) 强调内容应服务明确用户任务；[C4](https://c4model.com/) 与 [ADR](https://adr.github.io/) 都需要持续回到当前系统事实。 |

## 仓库画像

- `empty`：可用入口、正式知识与控制面尚未建立。
- `underbuilt`：已有部分真实资产，责任闭环存在明确缺口。
- `overbuilt`：默认输入或文档体量超出可用路由、权威和生命周期能力。
- `managed`：入口、真源、AI 消费接口、控制面和工作流形成可演进闭环。

画像需要 rationale 和 signals，用于选择建设策略，不直接改变分数。

## Assurance：56 observations 与证据等级

Assurance 为同一组 14 维各展开 4 个 observations：基础存在、边界/覆盖、质量/风险、可复现证据。具体 ID 以当前 `--mode assurance --write-template` 为准；脚本从 observations 重新推导 level 与证据等级。

证据等级：

- **A**：完整且达标的 Standard Assessment、当前文件、完整 Git `path+commit`、快照、56 observations、适用回执、独立 AI 复核和已确认 Owner/权威记录可共同复现；Standard 与独立复核均以 artifact SHA 绑定当前快照和不同 run。
- **B**：文件与 Git 组合成立，部分可执行、消费或 Owner 复核仍在补充。
- **C**：主要依据当前文件与有界抽样。
- **Unknown**：关键证据源尚不可用。

## 门禁

```text
Context 控制面 >= 85 且 Context P0 = 0
AND
Repository Documentation >= 85 且 Documentation P0 = 0
```

- `not_assessed`：Quick，或 Standard 尚无有效维度判断。
- `blocked`：存在 P0。
- `needs_improvement`：Standard 已完整，任一量表低于 85。
- `provisional`：Standard 已完整、两套量表达到 85 且 P0=0，可进入建设验收讨论。
- `ready_for_owner_acceptance`：Assurance 达到 A 级证据组合，可提交 Owner 决策。

以上状态都是 `agent_assisted_self_assessment / not_certified`，只适用于声明范围和证据时点。
