---
name: devcloud-space-recovery
disable-model-invocation: true
description: 已于 2026-11-04 停用：空间清理监督任务经 owner 同意暂停，现行维护入口是 $devcloud-storage-upkeep，本技能只保留停用原因和历史资料入口。
---

# 已停用（2026-11-04）

- 停用原因：owner 同意暂停监督任务 `auto_cvheqhfkaf4`（2026-11-05 已删除），之后的清理不再走单 DSH 执行端，监督会话已移交。
- 现行入口：`$devcloud-storage-upkeep`。规则与授权只读 [storage-rules](../devcloud-patrol/references/storage-rules.md) 的「owner 2026-11-04 存储维护策略」一节。
- 历史资料（供追溯）：[监督说明](../../../docs/WORKTREE_CLEANUP_REVIEW.md)、[快速释放空间手册](../../../docs/DEVCLOUD_SPACE_RECOVERY.md) 与 [历次回收对账](../../../docs/DEVCLOUD_SPACE_RECOVERY_HISTORY.md)，保留停用前的目标、分工与门槛口径。