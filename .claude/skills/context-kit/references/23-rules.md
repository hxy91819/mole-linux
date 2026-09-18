# Context Kit 1.0 的 23 条通用原则

这 23 条原则用于评审和建设，不要求仓库采用固定目录。每条的公开依据紧跟设计理由，证据的可外推边界见 [industry-evidence.md](industry-evidence.md)。

| # | 原则 | 为什么与就近依据 | 最低实现证据 |
|---:|---|---|---|
| 1 | 入口是路由 | 默认输入为所有任务共享，应保留最小高信号集合。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | 入口预算、硬边界、加载算法、专题链接 |
| 2 | 规则分层、作用域明确 | 仓库级与局部路径需要不同粒度的指令。[OpenAI AGENTS.md](https://developers.openai.com/codex/guides/agents-md) | scope/precedence，根到局部继承核对 |
| 3 | 默认动态发现 | 任务应获得相关资源，资源渐进加载利于维持信号密度。[Agent Skills Specification](https://agentskills.io/specification) | 路径/符号/关键词/显式文件通道，代表性路由核对 |
| 4 | 一个窗口承载一个主要目标 | 清晰目标让验收、状态和恢复共享同一边界。[Anthropic Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) | goal、non_goals、acceptance 与当前阶段 |
| 5 | 在窗口压力下主动 compact | 长任务需要可控压缩与恢复策略。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | 利用率、触发阈值、最小回收收益、compact 事件与恢复检查 |
| 6 | 先管理可重取工具结果 | 日志和扫描结果可以通过工具再获取，任务事实需要稳定保留。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | tool-clear、近期保护、重取定位、部分截断边界 |
| 7 | compact 必须可恢复 | 压缩的目标是保持任务可继续性。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | verified/unverified、next action、摘要 hash、重载回归 |
| 8 | 大规模检索使用隔离任务 | 隔离扫描可以只返回主窗口所需的证据摘要。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | scan_scope、scan_output、证据摘要契约 |
| 9 | 使用结构化交接 | 目标、事实、风险、验证和下一步是跨会话恢复的核心状态。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | 交接 Schema 与跨会话恢复抽检 |
| 10 | 多路检索 | 精确路径、符号、文本、历史和关系服务不同查询。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | 路由通道与任务类型的组合记录 |
| 11 | 精确命中保持高优先级 | 用户指定的文件、路径和符号是高置信度信号。[OpenAI AGENTS.md](https://developers.openai.com/codex/guides/agents-md) | 显式路径保护和排序/包含检查 |
| 12 | Context 带来源、版本和权威 | 机器可读 Catalog 能连接组件、Domain、Owner 和关系。[Backstage System Model](https://backstage.io/docs/features/software-catalog/system-model/) | owner、authority、revision、review date、source_of_truth |
| 13 | Catalog/Wiki 作为可追溯视图 | 派生视图应指向实体和真源，并可以重建。[Backstage System Model](https://backstage.io/docs/features/software-catalog/system-model/) | source hash、真源回链、可重建流程 |
| 14 | 记忆写入经过验证门禁 | 长期知识需要作用域、Owner 和生命周期。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | candidate→pilot→active、Owner 记录与过期策略 |
| 15 | 保留有效失败信息 | 可验证反馈使工作流能够逐步改进。[Anthropic Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) | 触发、影响、证据、根因、修复与回归 |
| 16 | 稳定提示前缀与工具契约 | 简单可组合的工具接口更容易理解和验证。[Anthropic Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) | 版本化输入/输出/错误与兼容策略 |
| 17 | 状态外部化 | 外部记忆和结构化状态支持长任务恢复。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | Task、Checkpoint、Todo、Feedback 与恢复边界 |
| 18 | 检索优先与有界回退 | 按需检索同时保留精确搜索通道，适配新问题。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | route→must_read→bounded exact search→记录查询→扩大范围 |
| 19 | 观测取得、注入与使用 | 可观测记录能连接“资产存在”和“任务实际使用”。[Anthropic Context Engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | receipt；进一步是 retrieve/rerank/assemble/utilization trace |
| 20 | 失败回流到评测、索引或规则 | 可验证反馈是持续改进 Agent 系统的核心。[Anthropic Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) | 关闭案例回链 route/rule/content/evaluation 修复与回归 |
| 21 | 模型按难度与可验证性路由 | 简单工作流与更强自主性应按任务复杂度选择。[Anthropic Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) | requested/actual model、原因、预算与结果 trace |
| 22 | 工具集少而精 | 清晰、可组合的工具边界有利于 Agent 正确选择。[Anthropic Building Effective Agents](https://www.anthropic.com/research/building-effective-agents) | 不重叠的输入、输出、失败策略和使用指标 |
| 23 | 经验经 Owner 确认后增量沉淀 | 持续知识需要责任、范围和退出机制。[Backstage System Model](https://backstage.io/docs/features/software-catalog/system-model/) | 来源、owner、范围、有效期、决定与复用记录 |

## 原则与责任面

- 1–3、10–11、18：仓库入口、Router 和路由核对。
- 4–9、17：Task、Checkpoint、compact 与交接。
- 12–14、23：正式知识、Catalog、Authority 和 Lifecycle。
- 15、19–20：Feedback、Receipt、Trace 和回归。
- 16、21–22：Tool Contract、工具集与模型路由。

## 实现状态

- `implemented`：机制存在，并有当前文件、命令、测试或消费记录可复核。
- `partial`：已有真实文件或人工流程，覆盖、自动化或长期证据待完善。
- `design-only`：目标、责任和边界已定义，实现进入建设规划。
