#!/usr/bin/env python3
"""Weekly owner-managed pnpm store prune; read-only unless --execute."""
from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time

_spec = importlib.util.spec_from_file_location('pnpm_worktree', Path(__file__).with_name('worktree_sweep.py'))
ws = importlib.util.module_from_spec(_spec)
# Execute the hashed sibling source without writing or trusting a bytecode cache.
exec(compile(Path(_spec.origin).read_bytes(), _spec.origin, 'exec'), ws.__dict__)


def installing(args):
    managers = {'pnpm', 'pnpm.cjs', 'pnpm.js', 'npm', 'npm-cli.js', 'yarn', 'yarn.js', 'yarn.cjs', 'yarnpkg', 'npx', 'npx-cli.js'}
    commands = {'install', 'i', 'ci', 'add', 'update', 'up', 'upgrade', 'rebuild', 'fetch', 'import', 'link', 'unlink', 'dedupe', 'dlx', 'exec', 'prune', 'remove', 'rm', 'uninstall'}
    # Recognize executable and Node/Corepack tool paths, including runtime
    # flags with operands; never search arbitrary shell command text.
    positions = [0]
    if args and os.path.basename(args[0]) in ('node', 'nodejs', 'corepack'):
        positions += list(range(1, len(args)))
    for i in positions:
        if i >= len(args):
            continue
        manager = os.path.basename(args[i])
        if manager not in managers and not re.fullmatch(r'yarn-\d[^/]*\.(?:cjs|js)', manager):
            continue
        rest = args[i + 1:]
        # pnpm/npm may overwrite argv[0] with a process title like "npm install".
        rest = rest[:rest.index('--')] if '--' in rest else rest
        return not rest or any(x in commands for x in rest)
    title = args[0].split() if args else []
    return len(title) > 1 and title[0] in managers and title[1] in commands


def package_process_state():
    """0 installing, 1 none, 2 unknown. A disappearing process is normal."""
    try:
        for pid in os.listdir('/proc'):
            if not pid.isdigit() or int(pid) == os.getpid():
                continue
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as handle:
                    args = [x.decode(errors='replace') for x in handle.read().split(b'\0') if x]
                if installing(args):
                    return 0, f'package installation process {pid}'
            except (FileNotFoundError, ProcessLookupError):
                continue
        return 1, 'no installation processes'
    except OSError as exc:
        return 2, 'package process scan incomplete: ' + str(exc)


def store_bytes(path):
    result = ws.run(['du', '-sx', '-B1', path], timeout=1800)
    return int(result.stdout.split()[0])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--quiet', action='store_true', help='silent unless prune executes or a command fails')
    parser.add_argument('--state-dir', default=os.path.expanduser('~/.local/state/devcloud-storage-upkeep'))
    args = parser.parse_args(argv)
    os.makedirs(args.state_dir, exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    log = os.path.join(args.state_dir, f'pnpm-store-prune-{stamp}.jsonl')
    row = {'mode': 'execute' if args.execute else 'preview', 'result': 'skipped'}
    failed = False
    with open(os.path.join(args.state_dir, 'pnpm-store-prune.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not args.quiet:
                print('pnpm store prune busy; skipped')
            return 0
        try:
            store = ws.run(['pnpm', 'store', 'path'], timeout=120).stdout.strip()
            if not os.path.isabs(store) or not os.path.isdir(store):
                raise RuntimeError('pnpm store path is not an existing absolute directory')
            row['store'] = os.path.realpath(store)
            row['before_bytes'] = store_bytes(store)
            state, reason = package_process_state()
            if state != 1:
                row['reason'] = reason
            elif args.execute:
                # Recheck after sizing; only the owner command mutates the store.
                state, reason = package_process_state()
                if state != 1:
                    row['reason'] = reason
                else:
                    ws.run(['pnpm', 'store', 'prune'], timeout=720)
                    row['result'] = 'pruned'
            else:
                row['result'] = 'would-prune'
                row['reclaimable_bytes'] = None  # pnpm has no read-only prune plan
            row['after_bytes'] = store_bytes(store) if args.execute else row['before_bytes']
        except Exception as exc:
            row.update(result='failed', reason=ws.failure_text(exc))
            if 'store' in row:
                try:
                    row['after_bytes'] = store_bytes(row['store'])
                except Exception as size_error:
                    row['after_bytes'] = None
                    row['size_error'] = ws.failure_text(size_error)
            failed = True
            print('pnpm store prune: ' + row['reason'], file=sys.stderr)
        with open(log, 'a') as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + '\n')
        if not args.quiet or row['result'] == 'pruned':
            print(json.dumps(row, ensure_ascii=False))
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
