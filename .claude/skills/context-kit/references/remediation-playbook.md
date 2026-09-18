# Context Kit 1.0 三轮建设手册

首次默认只读。Quick 建立有界现状地图；需要完整规划时由 AI 完成 Standard 14 维评测。用户确认建设后创建 Goal，再修改仓库。Assurance 只在用户明确要求高保证复核时加入。

## 仓库画像与策略

- `empty`：从真实代码/部署边界建立最小入口、服务清单、Owner 和必要文档，不预建空目录。
- `underbuilt`：保留有价值正文，先归类、回链真源，再补责任和控制面。
- `overbuilt`：先确定权威与迁移影响，缩短默认入口，合并重复；删除和归档需单独授权。
- `managed`：只修 P0/P1 和证据空白，不为追求满分继续堆文档。

## Round 1：盘点、分类与责任

目标：建立可信文档母集和真实服务边界。

1. 读取仓库指令、构建/部署入口、`git status --short` 与文档候选，保护 dirty worktree。
2. 将现有内容映射为 canonical、product、work、generated、evaluation、archive、vendor/upstream、context 等责任类别；复用仓库已有命名。
3. 对重复或冲突内容确定唯一真源；其余列入合并、转链或归档清单。
4. 从 workspace、构建/部署单元、Catalog、CODEOWNERS 和团队制度确定服务分母与 Owner。
5. 为每个真实服务补齐最短代码入口、局部规则、README、Owner/Catalog 和服务文档索引。

验收：文档母集可重扫；服务分母可解释；Owner 有来源；链接无新增失效；未授权内容未删除。

## Round 2：正式知识、AI 派生层与控制面

目标：把完整工程知识与紧凑 AI 消费接口分开。

1. 按真实需要组织 Architecture、Domain、Journey、Platform、Operations、Governance 和 ADR。
2. 服务目录共置代码、Owner、README、局部规则与服务文档；跨服务知识放正式工程层。
3. 从正式知识派生 Domain/Service/Flow/Runbook Context，并保留 source、authority、revision、review_due 和生命周期。
4. 建立或复用 Schema、Catalog、Router、Authority、Lifecycle、External Sources 与 Tool Contracts。
5. 为 Router 覆盖单域、跨域、精确路径、服务路径、无匹配和评测禁入场景。
6. 将 Dataset/Gold/模型答案/评分/报告与运行时 Context 隔离；版本化协议，运行产物留本地或受控制品系统。

验收：active Context 可回到真源；路由按任务渐进加载；评测资产不进入运行时；23 条原则状态如实可定位。

## Round 3：压缩、复核与交付

目标：收紧默认入口，完成 Standard 复评和可持续交付。

1. 测量根入口与继承链，删除重复表述并改为按需链接。
2. 抽样核对高频/高风险文档的 Owner、事实、时效、来源和任务可用性。
3. 使用当前文件与 Git 确认迁移差异和关键来源；缺失历史时降低 confidence 并写明局限。
4. 只按真实风险补 Validator、测试、Schema 或链接检查，不复制一套只服务评分的路径。
5. 由 AI 重新评审 14 维，更新 P0/P1/P2、coverage、confidence、score range 和三轮变化。
6. 生成本地 JSON/Markdown/HTML TMR，并执行发行包读回检查。

验收：Standard 两套内部自评均达到 85、P0=0、coverage=100%，或明确记录未达项与后续计划；默认入口有界；目录职责、真源、生命周期和评测隔离可复核。

如用户选择 Assurance，再冻结 Skill 与仓库快照，一次性生成 observations/回执，由独立 AI 和 Owner 复核；这不是 Standard 建设的默认成本。

## Goal 模板

```text
完成目标仓库 Context 工程 1.0：基于首次 Quick/Standard 评测，按三轮修复 P0/P1，
建立可追溯的正式知识、AI 派生层、控制面、可复用工作流与评测隔离；
以 14 维 Assessment 可复核、两套内部自评均达到 85、P0 为 0、
默认入口有界且 TMR 可重现为完成条件。
```

Goal 不自动授权 commit、push、MR、发布、生产变更、外部写入、删除或归档。

## 停止条件

- 任一 P0：先修污染、权威冲突、路由失败、安全边界或预算超限。
- 门禁已满足且只剩 P2：提交 Owner 决定是否验收 1.0。
- 文档增加但默认入口、重复率或未分类量上升：回到真源和分层设计。
- 证据不可访问：降低 confidence、缩小结论范围，不用目录名或文件数量补齐判断。
