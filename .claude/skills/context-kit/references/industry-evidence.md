# Context Kit 1.0 行业依据注册表

Context Kit 1.0 把来源放在其实际支持的原则和权重旁边：[23-rules.md](23-rules.md) 每条规则带就近依据，[rubric.md](rubric.md) 每个维度解释权重与来源。本页统一记录来源的可依靠范围，便于复查。

| 公开来源 | 支持的行业方向 | Context Kit 1.0 中的用法 | 适用边界 |
|---|---|---|---|
| Anthropic, [Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | Context 是有限资源；长任务需要选择、压缩、外部记忆和结构化管理 | 短入口、按需路由、Token 预算、Checkpoint、Receipt 和生命周期 | 具体预算、阈值和得分需由目标仓库和 Rubric 说明 |
| Anthropic, [Building effective agents](https://www.anthropic.com/research/building-effective-agents) | 从简单可组合流程开始，以工具边界和可验证反馈控制复杂度 | 规则可执行性、反馈闭环、工具契约和模型路由 | 具体工作流仍由仓库风险和验收性决定 |
| OpenAI Codex, [AGENTS.md guide](https://developers.openai.com/codex/guides/agents-md) | 仓库与子目录指令可分层发现 | 根/局部入口、作用域、继承与最近规则 | 文档分类、Catalog 与权重由 Context Kit 另行定义 |
| GitHub Copilot, [Repository custom instructions](https://docs.github.com/en/copilot/customizing-copilot/adding-repository-custom-instructions-for-github-copilot) | 仓库级与路径级指令应简短、明确且相关 | 指令预算、局部增量规则和路径证据 | 各 Harness 的加载细节按目标环境核对 |
| Agent Skills, [Specification](https://agentskills.io/specification) | Skill 使用明确入口和渐进加载的资源组织工作流 | Skill 治理、`SKILL.md`、references/scripts/tests 分工 | 业务有效性仍需要目标仓库证据 |
| Backstage, [System model / Software Catalog](https://backstage.io/docs/features/software-catalog/system-model/) | Component、System、Domain、Owner 和关系适合机器可读 Catalog | 服务分母、Owner、Domain、上下游与文档入口 | 可使用自有 Catalog 或等价管理面 |
| Diátaxis, [Documentation framework](https://diataxis.fr/) | 不同文档服务不同用户任务，应按用途分工 | 正式文档分类、Runbook/Reference/Architecture 职责分离 | 服务、Domain、Journey 是代码仓库的工程映射 |
| C4 Model, [C4 model](https://c4model.com/) | 架构描述需要清晰层级与不同缩放尺度 | System overview、service topology 与服务局部 architecture | 当前性通过 Owner、复查日期和代码/配置抽样确认 |
| ADR community, [Architectural Decision Records](https://adr.github.io/) | 重要决策应记录背景、决定与后果并与版本共同演进 | 跨服务与服务局部 ADR、Git 可追溯证据 | ADR 记录决策语境，当前运行步骤由 Runbook 承载 |
| CodeBuddy IDE, [4.11.3 发布说明](https://www.codebuddy.cn/docs/ide/release-notes/release-notes#_4-11-3-2026-08-28) | Coding Agent 产品持续治理长对话 Context 压缩 | 作为规则 5 的产品实践信号 | 具体压缩策略和效果按可复核实现证据确认 |

## 权重设计记录

Context 量表的 60/40 分配表达两层风险：入口、路由、权威和可执行性直接决定 Agent 获得什么以及能否稳定行动；预算、任务闭环、Skill 和生命周期决定系统的长期效率与治理。

Documentation 量表将服务责任设为 25，因为服务是代码、Owner、运行和局部知识共同演进的主边界；母集和跨服务知识各 20，分别承担治理分母和系统真源；链接 15 承担证据导航；派生接口和内容时效各 10 承担运行时接入与抽样校准。每个维度的就近来源见 [rubric.md](rubric.md)。

团队如根据本地风险调整子项，应保留两套量表各 100 分、P0 独立门禁，并记录调整理由、映射和 Owner 决定。

## 证据链层级

1. 行业来源解释设计原则和风险分配。
2. 当前文件解释仓库现状。
3. Git 解释路径与变更历史。
4. Validator/Test 解释契约在声明范围内的可执行性。
5. Receipt/Trace 解释执行或消费记录。
6. 独立 AI 与 Owner Review 为 Assurance 提供分离复核。

隔离的模型效果结果可作为独立附件；Context Kit 1.0 的 Quick、Standard 和 Assurance 都可仅基于仓库事实与内部证据链完成。
