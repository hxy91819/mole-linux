#!/usr/bin/env python3
"""Inventory local BB storage and apply only candidates proven safe by its API."""
import argparse
import importlib.util
import json
import subprocess
import time
from pathlib import Path

SCAN_SECONDS = 600


def bb_call(command, *args):
    result = subprocess.run([*command, *args], capture_output=True, text=True, timeout=30, check=True)
    return json.loads(result.stdout)


def local_host(call):
    matches = [host['id'] for host in call('machine', 'list', '--all', '--json')
               if host['type'] == 'persistent' and host['status'] == 'connected'
               and 'machineProviderId' in host and host['machineProviderId'] is None]
    if len(matches) != 1:
        raise ValueError('BB server host is missing or ambiguous')
    return matches[0]


def fresh_report(call, host, *, clock=time.monotonic, pause=time.sleep):
    policy = call('storage', 'retention')['policy']
    # The scan itself can otherwise trigger retention-owned developer cleanup.
    if policy.get('deleteDevDataOnCheckoutRemoval') is not False:
        raise ValueError('BB rescan may auto-remove development data; check the retention policy')
    args = ('storage', 'usage', '--machine', host)
    previous = call(*args).get('report') or {}
    generation = previous.get('scannedAt', 0)
    deadline = clock() + SCAN_SECONDS
    response = call(*args, '--rescan')
    while clock() < deadline:
        scan, report = response['scan'], response.get('report')
        if scan['state'] == 'failed':
            raise ValueError(f"BB storage rescan failed: {scan.get('message', 'unknown error')}")
        if scan['state'] == 'idle':
            if report and report['hostId'] == host and report['scannedAt'] > generation:
                return report
            raise ValueError('BB storage rescan produced no fresh local report')
        if scan['state'] != 'scanning':
            raise ValueError('unknown BB storage scan state')
        pause(3)
        response = call(*args)
    raise ValueError('BB storage rescan did not finish within 600 seconds')


def review(report):
    developer = report['developerStorage']
    missing = [entry for entry in developer['entries'] if not entry['current']
               and entry['sourcePathState'] == 'missing'] if developer else []
    unknown = [entry['name'] for entry in developer['entries'] if not entry['current']
               and entry['sourcePathState'] == 'unknown'] if developer else []
    leftovers = report['leftoverWorktrees']
    return {
        'remove-orphans': {
            'candidate_count': report['orphanCount'], 'candidate_bytes': report['orphanBytes'],
            'limitation': 'preview exposes totals only; discard can stop processes and cannot bind reviewed paths',
        },
        'remove-dev-instances': {
            'candidate_count': len(missing), 'candidate_bytes': sum(entry['sizeBytes'] for entry in missing),
            'paths': [str(Path(developer['path'], entry['name'])) for entry in missing],
            'unknown_entries': unknown,
            'limitation': 'bulk cleanup can stop processes; named mode can remove data even when its checkout exists',
        },
        'retry-worktree-cleanup': {
            'candidate_count': len(leftovers), 'candidate_bytes': report['leftoverWorktreeBytes'],
            'paths': [entry['path'] for entry in leftovers],
            'limitation': 'retry selects live retired environments, including unmeasured entries absent from usage',
        },
    }


def maintain(call, execute):
    host = local_host(call)
    report = fresh_report(call, host)
    actions = review(report)
    for action in actions.values():
        if not execute:
            action['result'] = 'preview'
        elif action['candidate_count']:
            action['result'] = 'deferred'
        else:
            # Do not invoke an unbound destructive method just to obtain a zero count.
            action['result'] = 'no measured candidates'
    return {'mode': 'execute' if execute else 'preview', 'host': host, 'scanned_at': report['scannedAt'],
            'actions': actions, 'removed_count': 0, 'removed_bytes': 0,
            'external_worktree_count': len(report['externalWorktrees'])}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true', help='Evaluate the owner cleanup gates after a fresh scan.')
    args = parser.parse_args(argv)
    try:
        path = Path(__file__).with_name('git_tree_sweep_bb_cli.py')
        spec = importlib.util.spec_from_file_location('binder', path)
        binder = importlib.util.module_from_spec(spec)
        exec(compile(path.read_bytes(), str(path), 'exec'), binder.__dict__)
        command = binder.command_for(binder.resolve(server_only=True))
        result = maintain(lambda *args: bb_call(command, *args), args.execute)
        print(json.dumps(result, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(json.dumps({'error': str(exc)}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
