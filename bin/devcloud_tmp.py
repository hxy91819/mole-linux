#!/usr/bin/env python3
"""Bounded cleanup of this host's approved temporary root; preview by default.

Used by devcloud.py clean-tmp. Parameters: --path /data/system-tmp (or its
/tmp bind alias), --age-days >= 7, --execute, --json, --du, --limit.
Outputs: one summary, including filesystem capacity; execute also writes a
JSONL intent/result log. Exit 0 = completed/preview, 1 = partial/failed,
2 = refused/unknown. No databases, migrations, or package-manager operations.
Examples: python3 bin/devcloud.py clean-tmp --json
          python3 bin/devcloud.py clean-tmp --execute --json
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import hashlib
import json
import math
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

TMP_ROOT = Path('/data/system-tmp')
DATA_ROOT = Path('/data')
PROC_ROOT = Path('/proc')
STATE = Path('/data/metrics/devcloud-maintenance')
LOG = Path('/data/metrics/devcloud-clean-tmp.log')
REPO = Path(__file__).resolve().parent.parent
SCAN_SECONDS = 120
RUN_SECONDS = 300
ITEM_SECONDS = 5
MAX_ITEMS = 50
# Mole 策略阶段逐个候选调用 validate_path_for_deletion（约 25ms）与
# should_protect_path（约 17ms），单项约 50ms。单进程顺序跑 ~6.7k 候选会撞上原先
# 硬编码的 30s 上限，整轮 fail-closed——这正是 /tmp 清理连续 4 天 deleted=0 的根因。
# 修法：同一脚本分片并行，逐项语义完全一致、候选集绝不预截断（绝大多数候选会被
# 策略拒绝，按大小取前缀会让允许项永远排不进来），只是把墙钟时间除以分片数。
POLICY_SHARDS = 16
POLICY_SECONDS = 90
PROTECTED_PREFIXES = ('dsh-s04-', 'cc-daemon', 'playwright_', 'node-addon-')
PROTECTED_NAMES = {'.X11-unix', '.ICE-unix', '.XIM-unix', '.font-unix', '.Test-unix', 'receipt.json'}
PERSISTENT_NAMES = {'.git', '.env', '.ssh', '.aws', '.gnupg', 'credentials.json', 'auth.json'}
PERSISTENT_SUFFIXES = ('.pem', '.key', '.bak', '.patch', '.diff', '.db', '.sqlite', '.sqlite3', '.sql')
MOLE_POLICY = '''set -euo pipefail
source "$1/lib/core/app_protection.sh"
source "$1/lib/core/file_ops.sh"
load_mole_whitelist
if [[ ${#WHITELIST_WARNINGS[@]} -gt 0 ]]; then
    printf 'invalid Mole whitelist; review ~/.config/mole/whitelist before cleanup\n' >&2
    exit 2
fi
tmp_original_allowed() {
    validate_path_for_deletion "$1" && validate_path_for_deletion "$2" &&
        ! is_path_whitelisted "$1" && ! is_path_whitelisted "$2"
}
'''


class Refused(RuntimeError):
    pass


def checkpoint(deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError('time budget exhausted; incomplete scan is not executable')


def beneath(path, root):
    return path == root or path.startswith(root.rstrip('/') + '/')


def trusted(path):
    for item in (path, *path.parents):
        s = item.lstat()
        if not stat.S_ISDIR(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022:
            raise Refused(f'untrusted directory: {item}')


def capacity():
    rows = {}
    for path in (Path('/'), DATA_ROOT):
        s = path.stat().st_dev
        v = os.statvfs(path)
        used = (v.f_blocks - v.f_bfree) * v.f_frsize
        free = v.f_bavail * v.f_frsize
        rows[str(path)] = {'device': s, 'used_bytes': used, 'available_bytes': free,
                           'used_percent': 100 * used / (used + free)}
    return rows


def validate_root(requested):
    if requested not in (TMP_ROOT, Path('/tmp')):
        raise Refused('only /data/system-tmp and its verified /tmp bind alias are allowed')
    trusted(TMP_ROOT)
    if not DATA_ROOT.is_mount() or DATA_ROOT.stat().st_dev == Path('/').stat().st_dev:
        raise Refused('/data is not a separate mounted filesystem')
    root = TMP_ROOT.stat()
    if root.st_dev != DATA_ROOT.stat().st_dev:
        raise Refused('temporary root is not on the data filesystem')
    alias = requested.lstat()
    if not stat.S_ISDIR(alias.st_mode) or (alias.st_dev, alias.st_ino) != (root.st_dev, root.st_ino):
        raise Refused('/tmp alias identity does not match the approved temporary root')
    return TMP_ROOT


def unescape_mount(value):
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def parse_mounts(text):
    rows = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 10 or '-' not in fields:
            raise Refused('incomplete mount information')
        rows.append((fields[2], unescape_mount(fields[3]), unescape_mount(fields[4])))
    if not rows:
        raise Refused('empty mount information')
    return rows


def mount_aliases(root, mounts):
    s = root.stat()
    device = f'{os.major(s.st_dev)}:{os.minor(s.st_dev)}'
    if beneath(str(root), str(DATA_ROOT)):
        physical = '/' + str(root.relative_to(DATA_ROOT))
    elif s.st_dev == Path('/').stat().st_dev:
        physical = str(root)
    else:
        raise Refused('cannot resolve the probed filesystem mount')
    aliases = [(str(root), '')]
    mounted_children = set()
    for dev, origin, destination in mounts:
        if dev != device:
            continue
        if beneath(physical, origin):
            relative = physical[len(origin):].lstrip('/')
            aliases.append((os.path.join(destination, relative).rstrip('/') or '/', ''))
            if physical == origin and root != TMP_ROOT:
                mounted_children.add('.')
        elif beneath(origin, physical):
            relative = origin[len(physical):].lstrip('/')
            aliases.append((destination, relative))
            mounted_children.add(relative.split('/')[0])
    for _, _, destination in mounts:
        for alias, prefix in aliases:
            if beneath(destination, alias) and destination != alias:
                relative = '/'.join(filter(None, (prefix, destination[len(alias):].lstrip('/'))))
                mounted_children.add(relative.split('/')[0])
    return aliases, mounted_children


def process_snapshot(root, deadline):
    """Read every live process; unreadable evidence refuses the entire scan."""
    reserved, watches, processes = set(), set(), []
    namespaces = {}
    ancestors = {os.getpid()}
    pid = os.getppid()
    while pid > 1 and pid not in ancestors:
        ancestors.add(pid)
        try:
            pid = int((PROC_ROOT / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()[1])
        except FileNotFoundError:
            break
    for proc in PROC_ROOT.iterdir():
        checkpoint(deadline)
        if not proc.name.isdigit() or int(proc.name) == os.getpid():
            continue
        try:
            before = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            if before[0] == 'Z' or int(before[6]) & 0x200000:
                continue
            namespace = os.readlink(proc / 'ns/mnt')
            if namespace not in namespaces:
                namespaces[namespace] = mount_aliases(root, parse_mounts((proc / 'mountinfo').read_text()))
            aliases, mounts = namespaces[namespace]
            reserved.update(mounts)
            paths = [(part, os.readlink(proc / part)) for part in ('cwd', 'exe')]
            for fd in (proc / 'fd').iterdir():
                checkpoint(deadline)
                try:
                    value = os.readlink(fd)
                    paths.append(('fd', value))
                    if value == 'anon_inode:inotify':
                        for line in (proc / 'fdinfo' / fd.name).read_text().splitlines():
                            checkpoint(deadline)
                            if line.startswith('inotify '):
                                fields = dict(word.split(':', 1) for word in line.split()[1:])
                                watches.add((int(fields['sdev'], 16), int(fields['ino'], 16)))
                except FileNotFoundError:
                    continue  # A descriptor closed while enumerating, not an unreadable process.
            for line in (proc / 'maps').read_text().splitlines():
                checkpoint(deadline)
                parts = line.split(None, 5)
                if len(parts) == 6:
                    paths.append(('maps', parts[5]))
            if int(proc.name) not in ancestors:
                for arg in (proc / 'cmdline').read_bytes().decode(errors='replace').split('\0'):
                    value = arg.split('=', 1)[-1] if arg.startswith('--') else arg
                    if value.startswith('/'):
                        paths.append(('argv', value))
            via = set()
            for kind, value in paths:
                for alias, prefix in aliases:
                    if beneath(value, alias):
                        relative = '/'.join(filter(None, (prefix, value[len(alias):].lstrip('/'))))
                        reserved.add(relative.split('/')[0] if relative else '.')
                        via.add(kind)
            after = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
            if before[19] != after[19]:
                raise Refused('process identity changed during scan')
            if via:
                processes.append({'pid': int(proc.name), 'via': sorted(via),
                                  'cmdline': (proc / 'comm').read_text().strip()})
        except (FileNotFoundError, ProcessLookupError):
            try:
                if (proc / 'stat').read_text().rsplit(')', 1)[1].split()[0] != 'Z':
                    raise Refused(f'incomplete evidence for live PID {proc.name}')
            except (FileNotFoundError, ProcessLookupError):
                pass
        except (OSError, ValueError, KeyError, IndexError) as error:
            raise Refused(f'cannot inspect PID {proc.name}: {type(error).__name__}') from error
    return {'reserved': reserved, 'watches': watches, 'processes': processes}


def tree_snapshot(path, cutoff, watches, deadline):
    digest, size, entries = hashlib.sha256(), 0, 0
    device = path.lstat().st_dev
    stack = [path]
    while stack:
        checkpoint(deadline)
        item = stack.pop()
        s = item.lstat()
        relative = str(item.relative_to(path))
        if s.st_dev != device or item.is_mount():
            raise Refused('mounted descendant')
        if not (stat.S_ISREG(s.st_mode) or stat.S_ISDIR(s.st_mode)):
            raise Refused('symlink, socket or special file')
        if s.st_uid != 0 or s.st_mode & 0o022 or (stat.S_ISREG(s.st_mode) and s.st_nlink != 1):
            raise Refused('foreign owner, writable tree or shared file')
        if item.name in PERSISTENT_NAMES or item.name.endswith(PERSISTENT_SUFFIXES):
            raise Refused('repository or persistent state')
        if s.st_mtime > cutoff:
            raise Refused('recent descendant')
        key = (os.major(s.st_dev) << 20 | os.minor(s.st_dev), s.st_ino)
        if key in watches:
            raise Refused('inotify reference')
        digest.update(json.dumps([relative, s.st_dev, s.st_ino, s.st_mode, s.st_uid,
                                  s.st_gid, s.st_nlink, s.st_size, s.st_mtime_ns,
                                  s.st_ctime_ns if relative != '.' else 0]).encode())
        size += s.st_blocks * 512
        entries += 1
        if stat.S_ISDIR(s.st_mode):
            with os.scandir(item) as children:
                stack.extend(sorted((Path(child.path) for child in children), reverse=True))
    return {'fingerprint': digest.hexdigest(), 'bytes': size, 'entries': entries}


def build_plan(root, cutoff, deadline):
    live = process_snapshot(root, deadline)
    plan, skipped = [], Counter()
    with os.scandir(root) as children:
        names = sorted(child.name for child in children)
    for name in names:
        checkpoint(deadline)
        if (name in PROTECTED_NAMES or name.startswith(PROTECTED_PREFIXES)
                or name.startswith('systemd-') or (name.startswith('.X') and name.endswith('-lock'))):
            skipped['protected name'] += 1
            continue
        if name in live['reserved']:
            skipped['live or mount reference'] += 1
            continue
        try:
            snapshot = tree_snapshot(root / name, cutoff, live['watches'],
                                     min(deadline, time.monotonic() + ITEM_SECONDS))
            plan.append({'name': name, **snapshot})
        except Refused as error:
            skipped[str(error)] += 1
        except TimeoutError:
            checkpoint(deadline)
            skipped['slow tree retained'] += 1
        except FileNotFoundError:
            skipped['disappeared'] += 1
    checkpoint(deadline)
    allowed = policy_candidates(root, [row['name'] for row in plan], deadline)
    skipped['Mole protection or whitelist'] += len(plan) - len(allowed)
    plan = [row for row in plan if row['name'] in allowed]
    plan.sort(key=lambda row: (-row['bytes'], row['name']))
    return plan, dict(skipped)


def mole_environment(preview):
    env = os.environ.copy()
    env.update(MOLE_TEST_NO_AUTH='1', MOLE_DRY_RUN='1' if preview else '0',
               MOLE_DELETE_MODE='permanent', MOLE_CURRENT_COMMAND='devcloud-clean-tmp')
    return env


def _policy_shard(command, root, chunk, timeout):
    """Run the unchanged policy script over one shard; fail closed on any error."""
    result = subprocess.run(['/bin/bash', '-c', command, 'devcloud-policy', str(REPO), str(root)],
                            input=''.join(name + '\0' for name in chunk).encode(),
                            capture_output=True, env=mole_environment(True), timeout=timeout)
    if result.returncode:
        raise Refused('Mole policy scan failed; no executable plan: '
                      + result.stderr.decode(errors='replace')[-300:])
    return set(os.fsdecode(name) for name in result.stdout.split(b'\0') if name)


def policy_candidates(root, names, deadline):
    if not names:
        return set()
    checkpoint(deadline)
    command = MOLE_POLICY + '''while IFS= read -r -d '' name; do
    if tmp_original_allowed "$2/$name" "/tmp/$name" > /dev/null 2>&1; then
        printf '%s\\0' "$name"
    fi
done
'''
    shards = max(1, min(POLICY_SHARDS, len(names)))
    # Interleaved slices keep shard sizes even and the union order-independent.
    chunks = [names[index::shards] for index in range(shards)]
    budget = min(POLICY_SECONDS, max(1.0, deadline - time.monotonic()))
    if shards == 1:
        allowed = _policy_shard(command, root, chunks[0], budget)
    else:
        allowed = set()
        with ThreadPoolExecutor(max_workers=shards) as pool:
            futures = [pool.submit(_policy_shard, command, root, chunk, budget)
                       for chunk in chunks]
            for future in futures:
                # Any shard error (refusal or timeout) propagates: a partially
                # checked candidate set must never become an executable plan.
                allowed |= future.result()
    checkpoint(deadline)
    if not allowed.issubset(names):
        raise Refused('invalid Mole policy scan output')
    return allowed


def append_log(record):
    trusted(LOG.parent)
    fd = os.open(LOG, os.O_APPEND | os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        s = os.fstat(fd)
        if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022 or s.st_nlink != 1:
            raise Refused('unsafe audit log')
        line = (json.dumps(record, ensure_ascii=True) + '\n').encode()
        with os.fdopen(fd, 'ab', closefd=False) as out:
            out.write(line)
            out.flush()
            os.fsync(fd)
    finally:
        os.close(fd)


def write_receipt(path, record):
    with path.open('x') as out:
        json.dump(record, out)
        out.flush()
        os.fsync(out.fileno())


def final_guard(receipt):
    trusted(receipt.parent)
    if (receipt.name != 'receipt.json' or receipt.parent.parent != STATE
            or not receipt.parent.name.startswith('item-')):
        raise Refused('invalid cleanup receipt location')
    s = receipt.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022 or s.st_nlink != 1:
        raise Refused('unsafe cleanup receipt')
    record = json.loads(receipt.read_text())
    if record['name'] in ('', '.', '..', 'receipt.json') or Path(record['name']).name != record['name']:
        raise Refused('invalid staged name')
    if (record.get('source') != str(TMP_ROOT / record['name'])
            or not math.isfinite(record['cutoff']) or record['cutoff'] > time.time() - 7 * 86400):
        raise Refused('receipt is outside the approved path or retention policy')
    validate_root(TMP_ROOT)
    item = receipt.parent / record['name']
    if item.parent != receipt.parent or item.name != record['name']:
        raise Refused('invalid staged item')
    deadline = time.monotonic() + 20
    live = process_snapshot(item, deadline)
    if live['reserved']:
        raise Refused('staged item acquired a process or mount reference')
    snapshot = tree_snapshot(item, record['cutoff'], live['watches'], deadline)
    if snapshot['fingerprint'] != record['fingerprint']:
        raise Refused('staged contents changed')


def owner_delete(receipt, item, preview, deadline=None):
    seconds = min(45, deadline - time.monotonic()) if deadline is not None else 45
    if seconds <= 0:
        raise TimeoutError('cleanup budget exhausted before Mole')
    command = MOLE_POLICY + '''if ! tmp_original_allowed "$5" "$6"; then
    printf 'Mole protection or whitelist refused the original path\n' >&2
    exit 1
fi
TMP_CLEAN_SCRIPT="$2"
TMP_CLEAN_RECEIPT="$3"
tmp_clean_final_guard() { /usr/bin/python3 -B "$TMP_CLEAN_SCRIPT" _guard "$TMP_CLEAN_RECEIPT"; }
_MOLE_SAFE_REMOVE_FINAL_GUARD=tmp_clean_final_guard
mole_delete "$4" false
'''
    child = subprocess.Popen(['/bin/bash', '-c', command, 'devcloud-clean', str(REPO),
                              str(Path(__file__).resolve()), str(receipt), str(item),
                              str(TMP_ROOT / item.name), str(Path('/tmp') / item.name)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=mole_environment(preview), start_new_session=True)
    try:
        stdout, stderr = child.communicate(timeout=seconds)
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            child.communicate(timeout=3)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.communicate()
        raise
    if child.returncode:
        raise Refused(f'Mole refused or failed ({child.returncode}): {(stdout + stderr)[-600:]}')
    if not preview and os.path.lexists(item):
        raise Refused('Mole reported success but staged item remains')


def rename_exclusive(source, destination):
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(destination), 1):
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def execute_item(root, candidate, cutoff, deadline):
    checkpoint(deadline)
    validate_root(root)
    live = process_snapshot(root, deadline)
    name = candidate['name']
    if name in live['reserved']:
        raise Refused('candidate acquired a live reference')
    current = tree_snapshot(root / name, cutoff, live['watches'],
                            min(deadline, time.monotonic() + ITEM_SECONDS))
    if current != {key: candidate[key] for key in ('fingerprint', 'bytes', 'entries')}:
        raise Refused('candidate changed since preview')
    stage = Path(tempfile.mkdtemp(prefix='item-', dir=STATE))
    item, receipt = stage / name, stage / 'receipt.json'
    if name == receipt.name:
        stage.rmdir()
        raise Refused('reserved receipt name')
    record = {**candidate, 'cutoff': cutoff, 'source': str(root / name)}
    try:
        write_receipt(receipt, record)
        append_log({'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': 'intent', **record, 'staged': str(item)})
        # 根目录 cwd 不代表每个子项在用；隔离移除旧入口，再检查旧对象的实际引用。
        rename_exclusive(root / name, item)
        final_guard(receipt)
        owner_delete(receipt, item, preview=True, deadline=deadline)
        checkpoint(deadline)
        owner_delete(receipt, item, preview=False, deadline=deadline)
    except BaseException:
        if os.path.lexists(item):
            try:
                rename_exclusive(item, root / name)
            except OSError:
                raise Refused(f'original name is occupied; retained item needs review: {item}')
        raise
    finally:
        if not os.path.lexists(item):
            receipt.unlink(missing_ok=True)  # This invocation's receipt; no user file is removed here.
            stage.rmdir()


def _main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == '_guard':
        try:
            final_guard(Path(args[1]))
            return 0
        except Exception as error:
            print(str(error), file=sys.stderr)
            return 2
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--path', type=Path, default=TMP_ROOT)
    parser.add_argument('--age-days', type=float, default=7)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--du', action='store_true', help='show measured candidate bytes (already included in JSON)')
    parser.add_argument('--limit', type=int, default=40, help='maximum candidate names displayed')
    options = parser.parse_args(args)
    summary = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': 'summary',
               'mode': 'execute' if options.execute else 'preview', 'path': str(TMP_ROOT),
               'age_days': options.age_days if math.isfinite(options.age_days) else None,
               'deleted': 0, 'failed': 0}
    lock = None
    locked = False
    code = 0
    started = time.monotonic()
    try:
        summary['mounts_before'] = capacity()
        if not math.isfinite(options.age_days) or options.age_days < 7 or options.limit < 0:
            raise Refused('age must be finite and >= 7 days; display limit must be nonnegative')
        root = validate_root(options.path)
        if options.execute:
            trusted(STATE.parent)
            STATE.mkdir(mode=0o700, exist_ok=True)
            trusted(STATE)
            if STATE.stat().st_dev != root.stat().st_dev:
                raise Refused('staging must be on the temporary filesystem')
            lock = os.open(STATE / 'cleanup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            metadata = os.fstat(lock)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                    or metadata.st_mode & 0o022 or metadata.st_nlink != 1):
                raise Refused('unsafe maintenance lock')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            pending = [str(p) for p in STATE.glob('item-*')]
            if pending:
                raise Refused(f'unresolved staging receipt; review before continuing: {pending[0]}')
        cutoff = time.time() - options.age_days * 86400
        plan, skipped = build_plan(root, cutoff, started + SCAN_SECONDS)
        selected = plan[:MAX_ITEMS]
        summary.update(scan='complete', candidates=len(selected), eligible=len(plan),
                       deferred=len(plan) - len(selected), skipped=skipped,
                       candidate_bytes=sum(row['bytes'] for row in selected),
                       sample=[row['name'] for row in selected[:options.limit]])
        if options.execute:
            for candidate in selected:
                try:
                    execute_item(root, candidate, cutoff, started + RUN_SECONDS)
                    summary['deleted'] += 1
                    append_log({'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': 'deleted',
                                'path': str(root / candidate['name']), 'bytes': candidate['bytes']})
                except Exception as error:
                    summary['failed'] += 1
                    summary['error'] = str(error)
                    code = 1
                    break
    except Exception as error:
        code = 2
        summary.update(error=str(error), scan=summary.get('scan', 'incomplete'))
    finally:
        try:
            summary['mounts_after'] = capacity()
            summary['root_over_70_percent'] = summary['mounts_after']['/']['used_percent'] >= 70
            summary['elapsed_seconds'] = round(time.monotonic() - started, 2)
            summary['status'] = 'ok' if code == 0 else 'failed'
            if options.execute and locked:
                append_log(summary)
        except Exception as error:
            code = 2
            summary.update(status='failed', audit_error=str(error))
        if lock is not None:
            os.close(lock)
    if options.json:
        print(json.dumps(summary, ensure_ascii=True))
        if code:
            print(summary.get('error', summary.get('audit_error', 'cleanup failed')), file=sys.stderr)
    else:
        print(f"clean-tmp {summary['mode']}: {summary['status']}; "
              f"candidates={summary.get('candidates', 'unknown')} deleted={summary['deleted']} failed={summary['failed']}")
        for path, row in summary.get('mounts_after', {}).items():
            print(f"  {path}: {row['used_percent']:.2f}% used, {row['available_bytes'] / 1024**3:.2f} GiB available")
        for name in summary.get('sample', []):
            print(f'  {name}')
        if 'error' in summary:
            print(summary['error'], file=sys.stderr)
    return code


def main(argv=None):
    def interrupted(signum, _frame):
        raise InterruptedError(f'cleanup cancelled by signal {signum}')

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        return _main(argv)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


if __name__ == '__main__':
    raise SystemExit(main())
