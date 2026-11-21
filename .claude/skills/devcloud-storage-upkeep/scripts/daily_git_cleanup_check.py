#!/usr/bin/env python3
"""Read-only daily check of Git cleanup effectiveness and the BB CLI."""
import argparse
from collections import Counter
from datetime import datetime, time, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

LOCAL = timezone(timedelta(hours=8))  # Both schedules use Asia/Shanghai.
STATE = Path.home() / '.local/state/devcloud-storage-upkeep'
METRICS = Path('/var/lib/devcloud-storage-upkeep/metrics')
STAGES = {'bb-environments': 'bb-environment-sweep', 'linked': 'linked-worktree-sweep',
          'clones': 'git-tree-sweep'}
THRESHOLD = 20 * 2**30
CLEAN_PATH = '/root/.local/bin:/usr/local/bin:/usr/bin:/bin'
PROJECT = 'proj_8d3wrcajxn'


def read_json(path):
    return json.loads(path.read_bytes())


def stamp_datetime(stamp):
    return datetime.strptime(stamp, '%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc).astimezone(LOCAL)


def collect_reports(state, days):
    reports = {}
    for stage, prefix in STAGES.items():
        for path in state.glob(f'{prefix}-????????T??????Z.json'):
            if stamp_datetime(path.stem.removeprefix(prefix + '-')).date() not in days:
                continue
            report = read_json(path)
            invocation = report.get('invocation_id')
            if not invocation or report.get('policy') != stage or report.get('mode') != 'execute':
                continue
            key = (stage, invocation)
            # Ambiguous records cannot prove a completed zero-delete day.
            reports[key] = None if key in reports else report
    return reports


def scheduled_invocations(runs, days):
    ids = {}
    seen = set()
    for run in runs:
        if run['trigger'] != 'schedule':
            continue
        slot = datetime.fromtimestamp(run['scheduledFor'] / 1000, LOCAL)
        day = slot.date()
        if day not in days or slot.time() != time(6, 30):
            continue
        match = re.search(r'\binvocation ID: ([0-9a-f]{32})\b', run.get('output') or '')
        ids[day] = match[1] if day not in seen and run['status'] == 'succeeded' and match else None
        seen.add(day)
    return ids


def collect_sizes(directory, days):
    samples = {}
    for path in directory.glob('????????T??????Z.json'):
        stamp = stamp_datetime(path.stem)
        if stamp.date() not in days or not time(7) <= stamp.time() <= time(7, 30):
            continue
        sample = read_json(path)
        if datetime.fromtimestamp(sample['ts'], LOCAL).replace(microsecond=0) != stamp:
            raise ValueError(f'growth sample timestamp disagrees with {path}')
        day = stamp.date()
        samples[day] = None if day in samples else sample
    return [
        {row['root']: row['bytes'] if row['status'] == 'ok' else None for row in samples[day]['rows']
         if row['root'].startswith('/')} if samples.get(day) else {}
        for day in days
    ]


def affected_root_sizes(rows, samples):
    affected = {}
    current = samples[-1]
    for row in rows:
        path = Path(row['path'])
        if path.is_relative_to('/tmp'):
            path = Path('/data/system-tmp') / path.relative_to('/tmp')
        root = next((str(parent) for parent in (path, *path.parents) if str(parent) in current), None)
        if root:
            affected.setdefault(root, Counter())[row.get('reason', 'unknown')] += 1
    return [{'path': root, 'bytes_by_day': [sample.get(root) for sample in samples],
             'retention_reasons': [reason for reason, _ in sorted(
                 affected[root].items(), key=lambda pair: (-pair[1], pair[0]))[:5]]}
            for root in sorted(affected)]


def assess(days, invocations, reports, samples):
    alerts, stages = [], {}
    for stage in STAGES:
        history = [reports.get((stage, invocations.get(day))) for day in days]
        usable = [r is not None and r.get('complete') is True and r.get('error') is None for r in history]
        latest = history[-1]
        ranking = latest.get('summary', {}).get('retention') if latest else None
        stages[stage] = status = {'report': latest.get('output') if latest else None,
                                  'complete': usable[-1], 'retention': ranking}
        # A measured lower bound can still prove a blocker in a partial report.
        for bucket in ranking['by_bytes'] if ranking else []:
            if bucket['bytes'] is not None and bucket['bytes'] > THRESHOLD:
                alerts.append(dict(stage=stage, kind='retention_over_20_gib', **bucket))
        if stage == 'bb-environments':
            continue
        removed = [sum(row.get('result') == 'removed' for row in report['items']) if ok else None
                   for report, ok in zip(history, usable)]
        status['removed_by_day'] = removed
        status['growth_sample_days'] = sum(bool(sample) for sample in samples)
        if removed == [0, 0, 0]:
            kept = [row for row in latest['items'] if row.get('result', row['action']) in ('keep', 'kept')]
            status['affected_roots'] = affected_root_sizes(kept, samples)
            for growth in status['affected_roots']:
                sizes = growth['bytes_by_day']
                if not all(type(size) is int for size in sizes) or not sizes[0] < sizes[1] < sizes[2]:
                    continue
                alerts.append(dict(stage=stage, kind='zero_deletions_for_3_days_with_growth',
                                   reason='no removals in three complete scheduled executions', **growth))
    return {'days': [day.isoformat() for day in days], 'alerts': alerts, 'stages': stages}


def run(command, *, env=None):
    out = subprocess.run(command, env=env, capture_output=True, text=True, timeout=30, check=True)
    return out.stdout.strip()


def version_check(server_command, clean_path=CLEAN_PATH):
    server_version = run([*server_command, '--version'])
    clean_env = {'HOME': str(Path.home()), 'PATH': clean_path, 'LANG': 'C'}
    path = shutil.which('bb', path=clean_path)
    try:
        clean_version = run(['bb', '--version'], env=clean_env)
        error = None
    except (OSError, subprocess.SubprocessError) as exc:
        clean_version, error = None, str(exc)
    return {'match': bool(server_version and clean_version == server_version),
            'clean_path': clean_path, 'cli': path, 'cli_version': clean_version,
            'server_cli': server_command[-1], 'server_version': server_version, 'error': error}


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    try:
        spec = importlib.util.spec_from_file_location('binder', Path(__file__).with_name('git_tree_sweep_bb_cli.py'))
        binder = importlib.util.module_from_spec(spec)
        # This check also runs inside a read-only snapshot; don't create pycache.
        exec(compile(Path(spec.origin).read_bytes(), spec.origin, 'exec'), binder.__dict__)
        server = binder.command_for(binder.resolve(server_only=True))
        version = version_check(server)
        runs = json.loads(run([*server, 'automation', 'runs', 'auto_hukidoavox0',
                               '--project', PROJECT, '--limit', '30', '--json']))['runs']
        today = datetime.now(LOCAL).date()
        days = [today - timedelta(days=offset) for offset in (2, 1, 0)]
        results = assess(days, scheduled_invocations(runs, days), collect_reports(STATE, days),
                         collect_sizes(METRICS, days))
        print(json.dumps(dict(bb_version=version, **results), indent=2))
        return 1 if not version['match'] or results['alerts'] else 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({'error': str(exc)}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
