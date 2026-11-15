#!/usr/bin/env python3
"""Bounded per-root measurements, comparison and registered-source Top5."""
import argparse
import concurrent.futures
import fnmatch
from decimal import Decimal
import re
import json
import os
from pathlib import Path
import subprocess
import stat
import time

OUT = Path(os.environ.get('MOLE_GROWTH_METRICS', '/var/lib/devcloud-storage-upkeep/metrics'))
RULES = Path(__file__).resolve().with_name('storage_growth_fingerprints.md')
PER_ROOT_SECONDS = 780
TOTAL_SECONDS = 840
WORKERS = 8
RETENTION_SECONDS = 30 * 86400
GIB = 1024 ** 3

BB_DATA = '/data/bb-data'
BB_STORAGE = BB_DATA + '/thread-storage'
GO_CACHE = BB_STORAGE + '/.shared-cache/go-build'
LINT_CACHE = BB_STORAGE + '/.shared-cache/golangci-lint'
BB_ROOTS = (BB_DATA, BB_STORAGE, GO_CACHE, LINT_CACHE, 'bb:threads', 'bb:non-thread')
CHILD_ROOTS = ('/data/code', '/root/.cache')
DOCKER_TYPES = {'Images': 'docker:images', 'Containers': 'docker:containers',
                'Local Volumes': 'docker:volumes', 'Build Cache': 'docker:build-cache'}


def unknown(root, error):
    return {'root': root, 'status': 'unknown', 'error': str(error)}


def alert_exclusion(root):
    if root in (BB_DATA, BB_STORAGE, '/root/.bb/thread-storage', '/data/system-tmp', *CHILD_ROOTS):
        return 'parent aggregate; alert on components'
    if root in (GO_CACHE, LINT_CACHE):
        return 'shared Go cache; daily check 2 owns size and age limits'
    return None


def discover():
    roots, errors = [], []
    try:
        data = [str(p) for p in sorted(Path('/data').iterdir()) if str(p) != '/data/docker']
        first = ('/data/code', '/data/system-tmp', BB_DATA)
        roots.extend([root for root in first if root in data])
        roots.extend(root for root in data if root not in first)
    except OSError as error:
        errors.append(unknown('/data', f'discovery: {error}'))
    roots.extend(('/var/tmp', '/root/.cache'))
    # The large /data roots get workers before secondary roots and tmp entries.
    for parent in ('/root/.bb', '/data/system-tmp'):
        try:
            roots.extend(str(p) for p in sorted(Path(parent).iterdir()))
        except OSError as error:
            errors.append(unknown(parent, f'discovery: {error}'))
    return list(dict.fromkeys(roots)), errors



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


def probe(root, command, deadline):
    budget = min(PER_ROOT_SECONDS, deadline - time.monotonic())
    if budget <= 0:
        return unknown(root, 'total budget exhausted')
    started = time.monotonic()
    try:
        result = subprocess.run(['ionice', '-c3', *command], capture_output=True,
                                text=True, timeout=budget)
        if result.returncode:
            row = unknown(root, result.stderr[-2000:] or f'exit {result.returncode}')
        else:
            row = {'root': root, 'status': 'ok', 'output': result.stdout}
    except (OSError, subprocess.SubprocessError) as error:
        row = unknown(root, error)
    row['elapsed_seconds'] = round(time.monotonic() - started, 3)
    return row


def measure(root, deadline):
    if deadline <= time.monotonic():
        return unknown(root, 'total budget exhausted')
    if root == 'docker system df':
        return probe(root, ['docker', 'system', 'df', '--format', '{{json .}}'], deadline)
    try:
        metadata = os.lstat(root)
        if not stat.S_ISDIR(metadata.st_mode):
            return {'root': root, 'status': 'ok', 'bytes': metadata.st_blocks * 512}
        row = probe(root, ['du', '-sx', '-B1', '--', root], deadline)
        if row['status'] == 'ok':
            size = int(row.pop('output').split()[0])
            if size < 0:
                raise ValueError('negative size')
            row['bytes'] = size
        return row
    except (OSError, ValueError, IndexError) as error:
        return unknown(root, error)


def du_sizes(output):
    sizes = {}
    for record in output.split('\0'):
        if not record:
            continue
        size, separator, path = record.partition('\t')
        if not separator or int(size) < 0:
            raise ValueError('invalid du record')
        sizes[path] = int(size)
    return sizes


def measure_children(root, deadline):
    # Reporting depth only: du still walks descendants once and deduplicates inodes.
    row = probe(root, ['du', '-ax', '-B1', '--max-depth=1', '--null', '--', root], deadline)
    if row['status'] != 'ok':
        return [unknown(root, row['error'])]
    try:
        sizes = du_sizes(row['output'])
        if root not in sizes:
            raise ValueError('du parent summary missing')
        return [{'root': path, 'status': 'ok', 'bytes': size,
                 'parent': root if path != root else None,
                 'elapsed_seconds': row['elapsed_seconds']}
                for path, size in sizes.items() if path == root or str(Path(path).parent) == root]
    except (ValueError, KeyError) as error:
        return [unknown(root, error)]


def split_bb_output(output, thread_dirs):
    sizes = du_sizes(output)
    total = sizes[BB_STORAGE]
    direct = {path: size for path, size in sizes.items() if str(Path(path).parent) == BB_STORAGE}
    shared = BB_STORAGE + '/.shared-cache'
    shared_children = {path: size for path, size in sizes.items() if str(Path(path).parent) == shared}
    threads = {path: size for path, size in direct.items() if Path(path).name in thread_dirs}
    other = sum(size for path, size in direct.items() if path not in threads and path != shared)
    other += sum(size for path, size in shared_children.items() if path not in (GO_CACHE, LINT_CACHE))
    # Include directory metadata so the four components account for the entire root.
    other += total - sum(direct.values())
    other += sizes.get(shared, 0) - sum(shared_children.values())
    values = {BB_DATA: sizes[BB_DATA], BB_STORAGE: total,
              GO_CACHE: sizes.get(GO_CACHE, 0), LINT_CACHE: sizes.get(LINT_CACHE, 0),
              'bb:threads': sum(threads.values()), 'bb:non-thread': other}
    if any(value < 0 for value in values.values()):
        raise ValueError('inconsistent thread-storage totals')
    rows = [{'root': root, 'status': 'ok', 'bytes': size} for root, size in values.items()]
    for row in rows:
        if row['root'] == 'bb:threads':
            row.update(path=BB_STORAGE, entries=len(threads))
        elif row['root'] == 'bb:non-thread':
            row.update(path=BB_STORAGE, entries=sum(path not in threads and path != shared for path in direct))
    return rows


def measure_bb_data(deadline):
    # One physical walk supplies both parents and all four components.
    row = probe(BB_DATA, ['du', '-ax', '-B1', '--max-depth=3', '--null', '--', BB_DATA], deadline)
    if row['status'] != 'ok':
        return [unknown(root, row['error']) for root in BB_ROOTS]
    try:
        with os.scandir(BB_STORAGE) as entries:
            threads = {entry.name for entry in entries
                       if entry.name.startswith('thr_') and entry.is_dir(follow_symlinks=False)}
        rows = split_bb_output(row['output'], threads)
        for item in rows:
            item['elapsed_seconds'] = row['elapsed_seconds']
        return rows
    except (OSError, ValueError, KeyError) as error:
        return [unknown(root, error) for root in BB_ROOTS]


def docker_bytes(value):
    match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*([kKMGTPE]?i?B)', value)
    if not match:
        raise ValueError(f'invalid Docker size: {value}')
    unit = match[2]
    powers = {'B': 0, 'KB': 1, 'MB': 2, 'GB': 3, 'TB': 4, 'PB': 5, 'EB': 6}
    power = powers[unit.replace('i', '').upper()]
    return int(Decimal(match[1]) * (1024 if 'i' in unit else 1000) ** power)


def split_docker_output(row):
    if row['status'] != 'ok':
        return [unknown(root, row['error']) for root in DOCKER_TYPES.values()]
    try:
        data = [json.loads(line) for line in row['output'].splitlines() if line.strip()]
        by_type = {item['Type']: item for item in data}
        rows = []
        for kind, root in DOCKER_TYPES.items():
            if kind not in by_type:
                rows.append(unknown(root, f'Docker category missing: {kind}'))
                continue
            item = by_type[kind]
            result = {'root': root, 'status': 'ok', 'bytes': docker_bytes(item['Size'])}
            for name in ('TotalCount', 'Active'):
                if name in item:
                    result[name] = int(item[name])
            rows.append(result)
        return rows
    except (ValueError, KeyError, TypeError) as error:
        return [unknown(root, error) for root in DOCKER_TYPES.values()]


def measure_job(root, deadline):
    if root in CHILD_ROOTS:
        return measure_children(root, deadline)
    if root == BB_DATA:
        return measure_bb_data(deadline)
    row = measure(root, deadline)
    return split_docker_output(row) if root == 'docker system df' else [row]


def collect(roots, deadline):
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as pool:
        return [row for group in pool.map(lambda root: measure_job(root, deadline), roots) for row in group]


def load_history(directory, before_ts):
    # Select by sample time rather than mtime/name; latest and its dated copy are one sample.
    newest = {}
    paths = sorted(directory.glob('*.json'), key=lambda p: p.name == 'latest.json')
    for path in paths:
        sample = json.loads(path.read_text())
        stamp = sample['ts']
        if stamp >= before_ts or stamp < before_ts - RETENTION_SECONDS:
            continue
        newest[stamp] = sample
        if len(newest) > 2:
            del newest[min(newest)]
    return [newest[stamp] for stamp in sorted(newest, reverse=True)]


def assess_growth(rows, history):
    prior = [{row['root']: row for row in sample.get('rows', [])} for sample in history[:2]]
    for row in rows:
        values = []
        for item in [row, *(sample.get(row['root'], {}) for sample in prior)]:
            if item.get('status') != 'ok' or 'bytes' not in item:
                break
            values.append(item['bytes'])
        row['history_samples'] = len(values)
        row['growth_total_bytes'] = values[0] - values[-1] if len(values) == 3 else None
        if row.get('alert_excluded'):
            result = 'excluded'
        elif not values:
            result = 'unmeasured'
        elif row['owner'] != 'unknown':
            result = 'daily_checked' if len(values) >= 2 else 'insufficient_samples'
        elif len(values) < 3:
            result = 'insufficient_samples'
        elif not values[2] < values[1] < values[0]:
            result = 'not_continuous_growth'
        elif row['growth_total_bytes'] < 3 * GIB:
            result = 'below_continuous_threshold'
        else:
            result = 'continuous_threshold_met'
        row['growth_status'] = result


def compare(rows, previous, now):
    before = {row['root']: row for row in previous.get('rows', [])}
    interval = now - previous.get('ts', now)
    compared = []
    for row in rows:
        if row['status'] != 'ok' or 'bytes' not in row:
            continue
        old = before.get(row['root'])
        new = old is None
        # An explicit failed measurement remains unknown, never a synthetic zero.
        if not new and (interval <= 0 or old.get('status') != 'ok' or 'bytes' not in old):
            continue
        old_bytes = 0 if new else old['bytes']
        delta = row['bytes'] - old_bytes
        compared.append({'root': row['root'], 'owner': row['owner'],
                         'alert_excluded': row.get('alert_excluded'),
                         'baseline': 'new' if new else 'measured', 'previous_bytes': old_bytes,
                         'history_samples': row.get('history_samples'),
                         'growth_status': row.get('growth_status'),
                         'growth_total_bytes': row.get('growth_total_bytes'),
                         'delta_bytes': delta,
                         'daily_bytes': round(delta * 86400 / interval) if interval > 0 else None})
    positive = sorted((r for r in compared if r['delta_bytes'] > 0),
                      key=lambda row: (-(row['daily_bytes'] if row['daily_bytes'] is not None
                                        else row['delta_bytes']), row['root']))
    return compared, positive[:5]


def is_alert(row):
    if row['alert_excluded'] or row['baseline'] != 'measured' or row['delta_bytes'] <= 0:
        return False
    if row['owner'] == 'unknown':
        return row['growth_status'] == 'continuous_threshold_met'
    return row['daily_bytes'] is not None and row['daily_bytes'] > 20 * GIB


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
    history = load_history(OUT, time.time())
    previous = history[0] if history else {}
    rows += collect(['docker system df', *roots], deadline)
    for row in rows:
        row['owner'] = owner(row['root'], rules)
        row['alert_excluded'] = alert_exclusion(row['root'])
    now = time.time()
    assess_growth(rows, history)
    comparisons, top5 = compare(rows, previous, now)
    alerts = [row for row in comparisons if is_alert(row)]
    top_unknown = [row for row in rows if row['status'] == 'unknown' and
                   (row['root'] == '/data' or
                    (row['root'].startswith('/data/') and row['root'].count('/') == 2))]
    docker_unknown = [row for row in rows if row['root'] in DOCKER_TYPES.values() and row['status'] == 'unknown']
    if docker_unknown:
        top_unknown.append({'root': '/data/docker', 'error': 'Docker category measurements incomplete',
                            'categories': docker_unknown})
    out = {'schema_version': 3, 'history_sample_count': 1 + len(history),
           'history_ts': [sample['ts'] for sample in history],
           'insufficient_history_count': sum(
               row['owner'] == 'unknown' and row['growth_status'] == 'insufficient_samples' for row in rows),
           'data_top_unknown': top_unknown, 'ts': now, 'previous_ts': previous.get('ts'),
           'interval_seconds': now - previous['ts'] if previous else None,
           'elapsed_seconds': round(time.monotonic() - started, 3),
           'rows': rows, 'comparisons': comparisons, 'top5': top5, 'alerts': alerts,
           'unknown_count': sum(row['status'] == 'unknown' for row in rows)}
    OUT.mkdir(parents=True, exist_ok=True)
    atomic_write(OUT / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(now)) + '.json'), out)
    atomic_write(OUT / 'latest.json', out)
    retain(OUT, now)
    print(f"growth: roots={len(rows)} unknown={out['unknown_count']} "
          f"data_top_unknown={len(top_unknown)} elapsed={out['elapsed_seconds']}s compared={len(comparisons)} top5={len(top5)} alerts={len(alerts)}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
