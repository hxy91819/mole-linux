#!/usr/bin/env python3
"""独立健康检查：不依赖 BB automation 自身报告巡检健康。

检查 run-status.json 的陈旧度与终态；由系统 crontab 驱动（非 BB），
BB 停机时仍能发现巡检漏跑。整机失联属机外哨兵范畴，本脚本不覆盖。
"""

import json
import sys
from datetime import datetime, timezone

STATUS = "/data/metrics/ai-storage-maintenance/worktree-state/run-status.json"
MAX_AGE_H = 36.0  # 巡检计划每日 04:10；>36h 无完成记录即告警


def main() -> int:
    try:
        with open(STATUS, encoding="utf-8") as fh:
            s = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ALERT: run-status.json 不可读（{exc}）——巡检从未运行或产物丢失")
        return 1

    finished = s.get("finished_at") or ""
    try:
        finished_dt = datetime.fromisoformat(finished)
    except ValueError:
        print(f"ALERT: run-status finished_at 不可解析：{finished!r}")
        return 1
    age_h = (datetime.now().astimezone() - finished).total_seconds() / 3600
    if age_h > MAX_AGE_H:
        print(f"ALERT: 巡检已 {age_h:.0f}h 无完成记录（计划每日 04:10；"
              f"上次完成 {finished}，状态 {s.get('status')}）")
        return 1
    if s.get("status") == "failed":
        print(f"ALERT: 最近一轮巡检失败（run={s.get('run_id')}，"
              f"{finished}）；详情见 run-status.json exceptions")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())