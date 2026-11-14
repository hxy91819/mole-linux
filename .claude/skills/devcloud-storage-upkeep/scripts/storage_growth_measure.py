#!/usr/bin/env python3
"""Bounded per-root measurements, comparison and registered-source Top5."""
import argparse
import concurrent.futures
import fnmatch
import json
import os
from pathlib import Path
import subprocess
import stat
import time

OUT = Path(os.environ.get('MOLE_GROWTH_METRICS', '/var/lib/devcloud-storage-upkeep/metrics'))
RULES = Path(__file__).resolve().with_name('storage_growth_fingerprints.md')
PER_ROOT_SECONDS = 60
TOTAL_SECONDS = 180
WORKERS = 8
RETENTION_SECONDS = 30 * 86400
GIB = 1024 ** 3


def discover():
    # Schedule core roots before the potentially tens of thousands of tmp entries.
    roots = ['/var/tmp', '/root/.cache', '/data/bb-data/thread-storage']
    unknown = []
    for parent in ('/data', '/root/.bb', '/data/system-tmp'):
        try:
            roots.extend(str(p) for p in sorted(Path(parent).iterdir()))
        except OSError as error:
            unknown.append({'root': parent, 'status': 'unknown', 'error': f'discovery: {error}'})
    # Parents and descendants are independently useful; never sum overlapping rows.
    return list(dict.fromkeys(roots)), unknown



def fingerprints(path=RULES):
    """Read the table packaged from storage-rules alongside this script."""
    text = path.read_text()
    section = text.split('<!-- growth-fingerprints -->', 1)[1].split('<!-- /growth-fingerprints -->', 1)[0]
    rows = []
    for line in section.splitlines():
        if line.startswith('| `'):
            pattern, owner = [p.strip().strip('`') for p in line.strip('|').split('|')]
            rows.append((pattern, owner))
    if not rows:
        raise ValueError('storage-rules has no growth fingerprints')
    return rows


def owner(root, rules):
    return next((name for pattern, name in rules if fnmatch.fnmatchcase(root, pattern)), 'unknown')


def measure(root, deadline):
    budget = min(PER_ROOT_SECONDS, deadline - time.monotonic())
    if budget <= 0:
        return {'root': root, 'status': 'unknown', 'error': 'total budget exhausted'}
    cmd = ['docker', 'system', 'df'] if root == 'docker system df' else ['du', '-sx', '-B1', '--', root]
    try:
        if root != 'docker system df':
            metadata = os.lstat(root)
            if not stat.S_ISDIR(metadata.st_mode):
                # du's allocated-byte contract for one file/link, without a fork.
                return {'root': root, 'status': 'ok', 'bytes': metadata.st_blocks * 512}
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=budget)
        if result.returncode:
            return {'root': root, 'status': 'unknown', 'error': result.stderr[-2000:] or f'exit {result.returncode}'}
        if root == 'docker system df':
            return {'root': root, 'status': 'ok', 'output': result.stdout[-20000:]}
        size = int(result.stdout.split()[0])
        if size < 0:
            raise ValueError('negative size')
        return {'root': root, 'status': 'ok', 'bytes': size}
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as error:
        return {'root': root, 'status': 'unknown', 'error': str(error)}


def compare(rows, previous, now):
    before = {row['root']: row for row in previous.get('rows', [])}
    interval = now - previous.get('ts', now)
    compared = []
    for row in rows:
        old = before.get(row['root'], {})
        if (interval <= 0 or row['status'] != 'ok' or old.get('status') != 'ok'
                or 'bytes' not in row or 'bytes' not in old):
            continue
        compared.append({'root': row['root'], 'owner': row['owner'],
                         'delta_bytes': row['bytes'] - old['bytes'],
                         'daily_bytes': round((row['bytes'] - old['bytes']) * 86400 / interval)})
    positive = sorted((r for r in compared if r['delta_bytes'] > 0),
                      key=lambda row: (-row['daily_bytes'], row['root']))
    return compared, positive[:5]


def atomic_write(path, payload):
    scratch = path.with_suffix('.tmp')
    try:
        scratch.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
        scratch.replace(path)
    finally:
        scratch.unlink(missing_ok=True)


def retain(directory, now):
    for path in directory.glob('*.json'):
        if path.name == 'latest.json':
            continue
        # Age belongs to the sample, independent of copy/restore mtime.
        data = json.loads(path.read_text())
        if data['ts'] < now - RETENTION_SECONDS:
            path.unlink()


def main(argv=None):
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    started = time.monotonic()
    deadline = started + TOTAL_SECONDS
    roots, rows = discover()
    rules = fingerprints()
    previous = json.loads((OUT / 'latest.json').read_text()) if (OUT / 'latest.json').exists() else {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        rows += list(pool.map(lambda root: measure(root, deadline), ['docker system df', *roots]))
    for row in rows:
        row['owner'] = owner(row['root'], rules)
    now = time.time()
    comparisons, top5 = compare(rows, previous, now)
    alerts = [r for r in comparisons if r['delta_bytes'] > 0 and
              (r['daily_bytes'] >= GIB if r['owner'] == 'unknown' else r['daily_bytes'] > 20 * GIB)]
    out = {'ts': now, 'previous_ts': previous.get('ts'),
           'interval_seconds': now - previous['ts'] if previous else None,
           'elapsed_seconds': round(time.monotonic() - started, 3),
           'rows': rows, 'comparisons': comparisons, 'top5': top5, 'alerts': alerts,
           'unknown_count': sum(row['status'] == 'unknown' for row in rows)}
    OUT.mkdir(parents=True, exist_ok=True)
    atomic_write(OUT / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now)) + '.json'), out)
    atomic_write(OUT / 'latest.json', out)
    retain(OUT, now)
    print(f"growth: roots={len(rows)} unknown={out['unknown_count']} "
          f"elapsed={out['elapsed_seconds']}s compared={len(comparisons)} top5={len(top5)} alerts={len(alerts)}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
