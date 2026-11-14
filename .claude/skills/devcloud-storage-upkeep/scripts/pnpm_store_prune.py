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
import shlex
import stat
import sys
import time

_spec = importlib.util.spec_from_file_location('pnpm_worktree', Path(__file__).with_name('worktree_sweep.py'))
ws = importlib.util.module_from_spec(_spec)
# Execute the hashed sibling source without writing or trusting a bytecode cache.
exec(compile(Path(_spec.origin).read_bytes(), _spec.origin, 'exec'), ws.__dict__)


# Verified with explicit --store-dir probes on this host. Several pnpm majors
# share a format; use the newest verified major for each installed format.
STORE_MAJORS = {'v3': 9, 'v10': 10, 'v11': 12}
NEUTRAL_CWD = '/var/tmp'
PNPM_NAMES = {'pnpm', 'pnpm.cjs', 'pnpm.js', 'pnpm.mjs'}
DLX_NAMES = {'pnpx', 'pnpx.cjs', 'pnpx.js', 'pnpx.mjs', 'pnx'}
NODE_VALUE_FLAGS = {'--require', '-r', '--import', '--loader', '--experimental-loader',
                    '--conditions', '-C', '--inspect-port', '--max-old-space-size',
                    '--env-file', '--env-file-if-exists', '--title', '--icu-data-dir',
                    '--openssl-config', '--redirect-warnings', '--diagnostic-dir',
                    '--cpu-prof-dir', '--cpu-prof-name', '--heap-prof-dir', '--heap-prof-name',
                    '--trace-event-categories', '--trace-event-file-pattern',
                    '--report-directory', '--report-filename', '--heapsnapshot-signal'}
PNPM_VALUE_FLAGS = {'--dir', '-C', '--prefix', '--filter', '--filter-prod', '-F',
                    '--store-dir', '--cache-dir', '--state-dir', '--registry', '--userconfig',
                    '--global-dir', '--global-bin-dir', '--virtual-store-dir', '--modules-dir',
                    '--reporter', '--loglevel', '--workspace-concurrency', '--network-concurrency',
                    '--child-concurrency', '--use-node-version', '--package-import-method',
                    '--npmrc-auth-file', '--https-proxy', '--http-proxy', '--no-proxy',
                    '--workspace-packages', '--test-pattern', '--changed-files-ignore-pattern',
                    '--node-linker', '--lockfile-dir', '--cpu', '--os', '--libc',
                    '--fetch-timeout', '--fetch-warn-timeout-ms', '--fetch-min-speed-ki-bps',
                    '--user-agent', '--pnpr-server', '--allow-build',
                    '--merge-git-branch-lockfiles-branch-pattern'}
STORE_WRITERS = {'install', 'i', 'add', 'update', 'up', 'upgrade', 'remove', 'rm', 'uninstall',
                 'un', 'fetch', 'import', 'dlx', 'rebuild', 'dedupe', 'link', 'unlink',
                 'patch', 'patch-commit', 'patch-remove', 'deploy', 'server',
                 'ci', 'clean-install', 'install-clean', 'ic', 'install-test', 'it',
                 'pipeline', 'create', 'pnpx', 'pnx', 'rb', 'uni', 'ln', 'dislink', 'prune'}


def _command(args, value_flags):
    """First positional command/script, respecting option operands."""
    i = 0
    while i < len(args):
        token = args[i]
        if token == '--':
            return (args[i + 1], args[i + 2:]) if i + 1 < len(args) else (None, [])
        if not token.startswith('-'):
            return token, args[i + 1:]
        i += 2 if token in value_flags else 1
    return None, []


def installing(args):
    """Only a pnpm command that can write its store blocks pruning.

    npm/npx/yarn/vite and pnpm script runners are consumers, not store writers.
    A pnpm child of npx is inspected separately through its own /proc entry.
    """
    if not args:
        return False
    base = os.path.basename(args[0])
    if base in PNPM_NAMES | DLX_NAMES | {'node', 'nodejs', 'corepack', 'corepack.js', 'corepack.cjs'}:
        title = [args[0]]
    elif args[0].startswith(('pnpm ', 'pnpx ', 'pnx ', 'corepack ')):
        title = shlex.split(args[0])
    else:
        return False
    executable = os.path.basename(title[0]) if title else ''
    rest = title[1:] + args[1:]
    if executable in ('node', 'nodejs'):
        script, rest = _command(rest, NODE_VALUE_FLAGS)
        executable = os.path.basename(script or '')
    if executable in ('corepack', 'corepack.js', 'corepack.cjs'):
        manager, rest = _command(rest, set())
        executable = os.path.basename(manager or '')
    if executable in DLX_NAMES:
        return bool(rest)
    if executable not in PNPM_NAMES:
        return False
    command, rest = _command(rest, PNPM_VALUE_FLAGS)
    while command in {'recursive', 'multi', 'm', 'pm'}:
        command, rest = _command(rest, PNPM_VALUE_FLAGS)
    if command == 'store':
        action, _ = _command(rest, PNPM_VALUE_FLAGS)
        return action is not None and action not in {'path', 'status', 'help'}
    if command == 'audit':
        return '--fix' in rest
    return command in STORE_WRITERS


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
    except (OSError, ValueError) as exc:
        return 2, 'package process scan incomplete: ' + str(exc)


def store_bytes(path):
    result = ws.run(['du', '-sx', '-B1', path], timeout=1800)
    return int(result.stdout.split()[0])


def store_root():
    current = ws.run(['pnpm', 'store', 'path'], timeout=120).stdout.strip()
    if not os.path.isabs(current) or not re.fullmatch(r'v[0-9]+', Path(current).name):
        raise RuntimeError('pnpm store path is not an absolute version directory')
    root = os.path.realpath(Path(current).parent)
    if not os.path.isdir(root):
        raise RuntimeError('pnpm store root does not exist')
    return root


def runner_for(major, native_major, root, target):
    prefix = ['pnpm'] if major == native_major else ['npx', '--yes', f'pnpm@{major}']
    actual = ws.run([*prefix, '--version'], cwd=NEUTRAL_CWD, timeout=180).stdout.strip()
    if not re.fullmatch(rf'{major}\.[0-9]+\.[0-9]+(?:[-+].*)?', actual):
        raise RuntimeError(f'expected pnpm {major}, got {actual!r}')
    command = [*prefix, '--store-dir', root, 'store']
    resolved = ws.run([*command, 'path'], cwd=NEUTRAL_CWD, timeout=180).stdout.strip()
    if not os.path.isabs(resolved) or os.path.realpath(resolved) != target:
        raise RuntimeError(f'pnpm {major} resolves a different store: {resolved}')
    return [*command, 'prune']


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--quiet', action='store_true', help='silent unless prune executes or a command fails')
    parser.add_argument('--state-dir', default=os.path.expanduser('~/.local/state/devcloud-storage-upkeep'))
    args = parser.parse_args(argv)
    os.makedirs(args.state_dir, exist_ok=True)
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    log = os.path.join(args.state_dir, f'pnpm-store-prune-{stamp}.jsonl')
    failed = False
    with open(os.path.join(args.state_dir, 'pnpm-store-prune.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not args.quiet:
                print('pnpm store prune busy; skipped')
            return 0
        try:
            root = store_root()
            versions = sorted((p for p in Path(root).iterdir() if re.fullmatch(r'v[0-9]+', p.name)),
                              key=lambda p: int(p.name[1:]))
            try:
                native = ws.run(['pnpm', '--version'], cwd=NEUTRAL_CWD, timeout=120).stdout.strip()
                native_major = int(native.split('.', 1)[0])
            except Exception:
                native_major = None  # npx may still provide each required version
            for path in versions:
                row = {'mode': 'execute' if args.execute else 'preview', 'result': 'skipped',
                       'version': path.name, 'store_root': root, 'store': str(path),
                       'pnpm_major': STORE_MAJORS.get(path.name), 'before_bytes': None, 'after_bytes': None}
                try:
                    if not stat.S_ISDIR(os.lstat(path).st_mode):
                        row['reason'] = 'version is not a physical directory'
                    else:
                        row['before_bytes'] = store_bytes(str(path))
                        state, reason = package_process_state()
                        if state != 1:
                            row['reason'] = reason
                        elif row['pnpm_major'] is None:
                            row['reason'] = 'no verified pnpm major for store format'
                        else:
                            try:
                                command = runner_for(row['pnpm_major'], native_major, root, str(path))
                            except Exception as exc:
                                row['reason'] = 'matching pnpm unavailable or mismatched: ' + ws.failure_text(exc)
                            else:
                                row['command'] = command
                                state, reason = package_process_state()
                                if state != 1:
                                    row['reason'] = reason
                                elif args.execute:
                                    ws.run(command, cwd=NEUTRAL_CWD, timeout=720)
                                    row['result'] = 'pruned'
                                else:
                                    row.update(result='would-prune', reclaimable_bytes=None)
                        row['after_bytes'] = store_bytes(str(path)) if args.execute else row['before_bytes']
                except Exception as exc:
                    row.update(result='failed', reason=ws.failure_text(exc))
                    failed = True
                    try:
                        row['after_bytes'] = store_bytes(str(path))
                    except Exception as size_error:
                        row['size_error'] = ws.failure_text(size_error)
                    print('pnpm store prune: ' + row['reason'], file=sys.stderr)
                with open(log, 'a') as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + '\n')
                    handle.flush()
                    os.fsync(handle.fileno())
                if not args.quiet or row['result'] == 'pruned':
                    print(json.dumps(row, ensure_ascii=False))
        except Exception as exc:
            row = {'mode': 'execute' if args.execute else 'preview', 'result': 'failed', 'reason': ws.failure_text(exc)}
            with open(log, 'a') as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
            print('pnpm store prune: ' + row['reason'], file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
