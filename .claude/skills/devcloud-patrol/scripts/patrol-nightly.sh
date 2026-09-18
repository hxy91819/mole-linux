#!/usr/bin/env bash
# Worktree 巡检静默轮（script automation 入口，owner 授权见 storage-rules.md）
# 正常：exit 0 且无输出（BB 记录为静默跳过）；异常：打印可行动摘要。
# patrol 逻辑单一真实源在本仓工作区脚本，本文件只做启动与结果协议评估。
set -uo pipefail
cd /data/code/mole-linux

PATROL=".claude/skills/devcloud-patrol/scripts/patrol_worktrees.py"
STATUS=/data/metrics/ai-storage-maintenance/worktree-state/run-status.json
STDOUT_LOG=/data/metrics/ai-storage-maintenance/patrol-stdout.log

python3 -u "$PATROL" --execute --append-audit > "$STDOUT_LOG" 2>&1
rc=$?

python3 - "$STATUS" "$rc" "$STDOUT_LOG" <<'PY'
import json, os, sys

status_path, rc, out_log = sys.argv[1], int(sys.argv[2]), sys.argv[3]
try:
    s = json.load(open(status_path, encoding="utf-8"))
except (OSError, json.JSONDecodeError) as exc:
    tail = ""
    if os.path.exists(out_log):
        with open(out_log, errors="replace") as fh:
            tail = fh.read()[-500:]
    print(f"PATROL FAILED: run-status 不可读（{exc}）；stdout 尾部：\n{tail}")
    sys.exit(1)

if rc != 0 or s.get("status") == "failed":
    print(f"PATROL FAILED rc={rc} status={s.get('status')} run={s.get('run_id')}")
    for e in s.get("exceptions") or []:
        print(f"  [{e.get('code')}] {str(e.get('detail',''))[:160]}")
    sys.exit(1)

ex = s.get("executed") or {}
head = (f"本轮: worktree {ex.get('worktrees',0)} · 分支 {ex.get('branches_worktree',0)}+"
        f"{ex.get('branches_orphan',0)} · prunable {ex.get('prunable_pruned',0)}")
notes = [f"[新异常] {e.get('code')}: {str(e.get('detail',''))[:160]}"
         for e in s.get("notifiable") or []]
notes += [f"[已恢复] {e.get('code')}" for e in s.get("recovered") or []]
if notes:
    print(head)
    print("\n".join(notes))
# 正常且无新异常 → 无输出，BB 记为静默跳过
PY
exit $?