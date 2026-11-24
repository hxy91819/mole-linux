#!/usr/bin/env python3
"""Trigger existing storage cleanups once per low-watermark cooldown."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

GIB = 1024 ** 3
COOLDOWN = 7200
PROJECT = 'proj_8d3wrcajxn'
STATE = Path(os.environ.get('MOLE_WATERMARK_STATE', '/var/lib/devcloud-storage-upkeep/watermark.json'))
TASKS = {
    '/data': [('docker', 'auto_1ih1erjpbqo'),
              ('worktree', 'auto_hukidoavox0'),
              ('pnpm', 'auto_yfr5p14dvnc'), ('clean-tmp /data/system-tmp', '/data/system-tmp')],
    '/': [('go', 'auto_vad6yvkpdeq'), ('clean-tmp /var/tmp', '/var/tmp')],
}


def level(avail):
    if avail < 50 * GIB:
        return 'danger'
    if avail < 150 * GIB:
        return 'warning'
    return None


def available(path):
    probe = os.statvfs(path)
    return probe.f_bavail * probe.f_frsize


def bb(*args):
    return subprocess.run([os.environ.get('BB_CLI', 'bb'), *args], text=True,
                          capture_output=True, timeout=8, check=True).stdout


def running(aid):
    """True/False/None: unknown must never start another cleanup."""
    try:
        data = json.loads(bb('automation', 'runs', aid, '--project', PROJECT, '--limit', '10', '--json'))
        rows = data if isinstance(data, list) else data['runs']
        if not isinstance(rows, list) or any(not isinstance(row, dict) or
                row.get('status') not in ('running', 'queued', 'pending', 'succeeded',
                                          'skipped', 'failed', 'cancelled', 'canceled') for row in rows):
            return None
        return any(row['status'] in ('running', 'queued', 'pending') for row in rows)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        return None


def trigger(aid):
    if aid.startswith('/') or aid == 'clean-tmp-both':
        command = ['/usr/bin/python3', '-I',
                   str(Path(__file__).with_name('clean_tmp_runner.py')), '--launch']
        if aid != 'clean-tmp-both':
            command += ['--path', aid]
        result = subprocess.run(command, text=True, capture_output=True, timeout=8, check=True)
        status = json.loads(result.stdout)['status']
        if status not in ('triggered', 'lock'):
            raise ValueError('unknown clean-tmp launcher response')
        return status
    active = running(aid)
    if active is None:
        return 'unknown (查询失败，跳过)'
    if active:
        return 'running'
    bb('automation', 'run', aid, '--project', PROJECT, '--json')
    return 'triggered'


def save(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    scratch = STATE.with_suffix('.tmp')
    try:
        scratch.write_text(json.dumps(state, sort_keys=True) + '\n')
        scratch.replace(STATE)
    finally:
        scratch.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    now = time.time()
    reports, changed, failed = [], False, False
    plans = []
    for disk in ('/', '/data'):
        avail = available(disk)
        severity = level(avail)
        old = state.get(disk)
        if severity is None:
            if old:
                del state[disk]
                changed = True
            continue
        escalated = old and old['level'] == 'warning' and severity == 'danger'
        if old and now - old['ts'] < COOLDOWN and not escalated:
            continue
        plans.append((disk, avail, severity))
    clean_result = None
    for disk, avail, severity in plans:
        outcomes = []
        for label, aid in TASKS[disk]:
            if args.dry_run:
                result = 'would trigger'
            else:
                try:
                    if aid.startswith('/') and len(plans) == 2:
                        if clean_result is None:
                            clean_result = trigger('clean-tmp-both')
                        result = clean_result
                    else:
                        result = trigger(aid)
                except (OSError, subprocess.SubprocessError, ValueError, KeyError) as error:
                    result = f'failed ({error})'
                    if aid.startswith('/') and len(plans) == 2:
                        clean_result = result
                    failed = True
            outcomes.append(f'{label}={result}')
        reports.append(f'{disk} {severity} {avail}B ({avail / GIB:.2f} GiB): ' + ', '.join(outcomes))
        state[disk] = {'level': severity, 'ts': now}
        changed = True
    # Persist completed dispatches even when the independent notification fails.
    if changed and not args.dry_run:
        save(state)
    if reports:
        message = ('[水位预演] ' if args.dry_run else '[水位刹车] ') + '；'.join(reports)
        print(message, flush=True)
        if not args.dry_run:
            bb('thread', 'tell', 'thr_tikzbd34dg', message)
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
