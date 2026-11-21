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
import shutil
import shlex
import stat
import sys
import time

_spec = importlib.util.spec_from_file_location('pnpm_worktree', Path(__file__).with_name('worktree_sweep.py'))
ws = importlib.util.module_from_spec(_spec)
# Execute the hashed sibling source without writing or trusting a bytecode cache.
exec(compile(Path(_spec.origin).read_bytes(), _spec.origin, 'exec'), ws.__dict__)
_spec = importlib.util.spec_from_file_location('inventory', Path(__file__).with_name('local_storage_inventory.py'))
local = importlib.util.module_from_spec(_spec)
exec(compile(Path(_spec.origin).read_bytes(), _spec.origin, 'exec'), local.__dict__)


# Verified with explicit --store-dir probes on this host. Several pnpm majors
# share a format; use the newest verified major for each installed format.
STORE_MAJORS = {'v3': 9, 'v10': 10, 'v11': 12}
NEUTRAL_CWD = '/var/tmp'
# Docker owns image layers and containers; host pnpm must never prune them.
DOCKER_STORAGE = Path('/data/docker')
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


def available(path):
    vfs = os.statvfs(path)
    return vfs.f_bavail * vfs.f_frsize


def store_bytes(path):
    result = ws.run(['du', '-sx', '-B1', path], timeout=1800)
    return int(result.stdout.split()[0])


def installed_pnpm():
    home = Path.home()
    for directory in dict.fromkeys([
        *ws.ENV.get('PATH', '').split(os.pathsep),
        *sorted(map(str, home.glob('.nvm/versions/node/*/bin'))),
        *sorted(map(str, home.glob('.local/share/pnpm/.tools/pnpm/*/bin'))),
    ]):
        if directory and os.path.isabs(directory):
            binary = shutil.which('pnpm', path=directory)
            if binary:
                target = Path(binary).resolve()
                if target.parent.parent.name != 'corepack':
                    yield binary

    # A Corepack shim is a dispatcher, not another installed pnpm. Probe the
    # concrete releases already cached on this host without downloading one.
    corepack = Path(ws.ENV.get('COREPACK_HOME') or
                    (Path(ws.ENV.get('XDG_CACHE_HOME') or home / '.cache') / 'node/corepack'))
    for binary in sorted(corepack.glob('v1/pnpm/*/bin/pnpm.cjs')):
        if binary.is_file() and os.access(binary, os.X_OK):
            yield str(binary)


def discovery_paths():
    # Dependency/package cache configs are not project configuration.
    return local.inventory(['(', '-type', 'd', '(', '-name', 'node_modules',
                            '-o', '-name', '.git', ')', '-prune', '-false', '-o', '(',
                            '-name', '.npmrc', '-o', '-name', 'npmrc', '-o',
                            '(', '-type', 'd', '-name', 'v[0-9]*', ')', ')', ')'],
                           exclude={str(DOCKER_STORAGE)})


def configured_stores(config):
    if not config.is_file():
        return
    if config.stat().st_size > 1024 * 1024:
        raise RuntimeError(f'npmrc too large: {config}')
    for line in config.read_text(errors='surrogateescape').splitlines():
        key, sep, value = line.partition('=')
        if not sep or key.strip() != 'store-dir':
            continue
        value = value.strip()
        if value.startswith(('"', "'")):
            fields = shlex.split(value, comments=True)
            if len(fields) != 1:
                raise RuntimeError(f'unsupported store-dir syntax in {config}')
            value = fields[0]
        else:
            value = re.split(r'\s[;#]', value, 1)[0].rstrip()
        value = re.sub(r'\$\{([A-Za-z_][A-Za-z_0-9]*)\}',
                       lambda m: ws.ENV.get(m[1], m[0]), value)
        if '$' in value or not value:
            raise RuntimeError(f'unresolved store-dir in {config}')
        yield config.parent / Path(value).expanduser()


CAS_BUCKETS = {f'{i:02x}' for i in range(256)}
CAS_OBJECT = re.compile(r'[0-9a-f]{126}(?:-exec|-index\.json)?')


def has_cas_objects(files):
    with os.scandir(files) as entries:
        buckets = sorted(entry.path for entry in entries
                         if entry.name in CAS_BUCKETS and entry.is_dir(follow_symlinks=False))
    if len(buckets) != len(CAS_BUCKETS):
        return False
    for bucket in buckets:
        with os.scandir(bucket) as entries:
            if any(CAS_OBJECT.fullmatch(entry.name) and entry.is_file(follow_symlinks=False)
                   for entry in entries):
                return True
    return False


def discover_roots(selected=None):
    roots, errors = set(), []
    def add(path):
        try:
            resolved = Path(path).resolve(strict=True)
        except FileNotFoundError:
            return
        if resolved.is_dir():
            roots.add(str(resolved))

    add(Path.home() / '.local/share/pnpm/store')
    if ws.ENV.get('XDG_DATA_HOME'):
        add(Path(ws.ENV['XDG_DATA_HOME']) / 'pnpm/store')
    if selected is not None and selected in roots:
        return [selected], errors  # The explicitly selected default has independent discovery evidence.
    binaries = list(installed_pnpm())
    if not binaries:
        errors.append('no installed pnpm on PATH or in local version managers')
    for binary in binaries:
        for cwd in dict.fromkeys([os.getcwd(), NEUTRAL_CWD, str(Path.home())]):
            try:
                env = dict(ws.ENV, PATH=str(Path(binary).parent) + ':' + ws.ENV['PATH'],
                           npm_config_manage_package_manager_versions='false',
                           COREPACK_ENABLE_NETWORK='0')
                current = Path(ws.run([binary, 'store', 'path'], cwd=cwd, env=env,
                                      timeout=30).stdout.strip())
                if not current.is_absolute() or not re.fullmatch(r'v[0-9]+', current.name):
                    raise RuntimeError('pnpm store path is not an absolute version directory')
                add(current.parent)
            except Exception as exc:
                errors.append(f'{binary} in {cwd}: {ws.failure_text(exc)}')
    configs = []
    layout_roots = set()
    for path in discovery_paths():
        if path.name in ('.npmrc', 'npmrc'):
            configs.append(path)
        elif path.name in STORE_MAJORS:
            try:
                if (not re.fullmatch(r'v[0-9]+', path.parent.name)
                        and physical_store(path) and has_cas_objects(path / 'files')):
                    layout_roots.add(str(path.parent))
            except FileNotFoundError:
                continue
    configs.extend(Path(ws.ENV[key]) for key in ('NPM_CONFIG_USERCONFIG', 'NPM_CONFIG_GLOBALCONFIG',
                                                 'npm_config_userconfig', 'npm_config_globalconfig')
                   if ws.ENV.get(key))
    for config in dict.fromkeys(configs):
        try:
            for root in configured_stores(config):
                add(root)
        except (OSError, ValueError, RuntimeError) as exc:
            errors.append(f'npmrc {config}: {exc}')
    # Physical discovery cannot turn a malformed vN/vM inside another store
    # into an independently authorized root. Explicit config can identify it.
    for root in sorted(layout_roots, key=lambda path: (path.count('/'), path)):
        if not any(other in map(str, Path(root).parents) for other in roots):
            add(root)
    # realpath resolves symlinks but cannot collapse bind mounts such as
    # /tmp and /data/system-tmp. Keep one stable path per physical store.
    physical = {}
    for root in sorted(roots):
        try:
            st = os.stat(root)
        except FileNotFoundError:
            continue
        physical.setdefault((st.st_dev, st.st_ino), root)
    return list(physical.values()), errors


def version_identity(path):
    markers = [path.parent, path, path / 'files']
    if path.name == 'v10':
        markers.append(path / 'index')
    elif path.name == 'v11':
        markers.append(path / 'index.db')
    return tuple((st.st_dev, st.st_ino) for st in (entry.lstat() for entry in markers))


def physical_store(path):
    if path.is_relative_to(DOCKER_STORAGE) or path.resolve(strict=True) != path:
        return False
    markers = [path, path / 'files']
    if path.name == 'v10':
        markers.append(path / 'index')
    if not all(stat.S_ISDIR(p.lstat().st_mode) for p in markers):
        return False
    if path.name == 'v11':
        db = path / 'index.db'
        if not stat.S_ISREG(db.lstat().st_mode):
            return False
    mount_table = local.mounts()
    filesystem = next((mount_table[str(p)] for p in path.parents if str(p) in mount_table), None)
    return filesystem in local.LOCAL and not any(
        m == str(path) or m.startswith(str(path) + '/') for m in mount_table)



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
    parser.add_argument('--root', type=Path, help='limit the run to one discovered root')
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
            selected = str(args.root.resolve(strict=True)) if args.root else None
            roots, errors = discover_roots(selected)
            for reason in errors:
                print('pnpm discovery: ' + reason, file=sys.stderr)
                with open(log, 'a') as handle:
                    handle.write(json.dumps({'result': 'discovery-error', 'reason': reason}) + '\n')
            failed |= bool(errors)
            if selected:
                roots = [root for root in roots if os.path.samefile(root, selected)]
                if not roots:
                    raise RuntimeError('requested root is not a discovered pnpm store')
            versions = [p for root in roots for p in sorted(Path(root).iterdir())
                        if re.fullmatch(r'v[0-9]+', p.name)]
            try:
                native = ws.run(['pnpm', '--version'], cwd=NEUTRAL_CWD, timeout=120).stdout.strip()
                native_major = int(native.split('.', 1)[0])
            except Exception:
                native_major = None  # npx may still provide each required version
            for path in versions:
                root = str(path.parent)
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
                                if not physical_store(path):
                                    raise RuntimeError('unexpected store layout, symlink or mount')
                                identity = version_identity(path)
                                command = runner_for(row['pnpm_major'], native_major, root, str(path))
                            except Exception as exc:
                                row['reason'] = 'matching pnpm unavailable or mismatched: ' + ws.failure_text(exc)
                            else:
                                row['command'] = command
                                if not physical_store(path) or version_identity(path) != identity:
                                    raise RuntimeError('store identity changed')
                                state, reason = package_process_state()
                                if state != 1:
                                    row['reason'] = reason
                                elif args.execute:
                                    before_free = available(path)
                                    ws.run(command, cwd=NEUTRAL_CWD, timeout=720)
                                    row.update(result='pruned',
                                               filesystem_available_delta=available(path) - before_free)
                                else:
                                    row.update(result='would-prune', reclaimable_bytes=None)
                        row['after_bytes'] = store_bytes(str(path)) if args.execute else row['before_bytes']
                        if row['result'] == 'pruned':
                            row['allocated_delta_bytes'] = row['before_bytes'] - row['after_bytes']
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
                    print(json.dumps(row, ensure_ascii=False), flush=True)
        except Exception as exc:
            row = {'mode': 'execute' if args.execute else 'preview', 'result': 'failed', 'reason': ws.failure_text(exc)}
            with open(log, 'a') as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
            print('pnpm store prune: ' + row['reason'], file=sys.stderr)
            failed = True
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())
