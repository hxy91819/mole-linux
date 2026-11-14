#!/usr/bin/env python3
"""Bounded cleanup of this host's approved temporary roots; preview by default.

Used by devcloud.py clean-tmp. Parameters: --path /data/system-tmp (or its
/tmp bind alias) or /var/tmp, --age-days >= 3, --execute, --json, --du, --limit.
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
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from pathlib import Path

TMP_ROOT = Path('/data/system-tmp')
VAR_TMP_ROOT = Path('/var/tmp')
VAR_STATE = Path('/var/lib/devcloud-maintenance')
DATA_ROOT = Path('/data')
PROC_ROOT = Path('/proc')
STATE = Path('/data/metrics/devcloud-maintenance')
LOG = Path('/data/metrics/devcloud-clean-tmp.log')
REPO = Path(__file__).resolve().parent.parent
# Host measurement (2026-11-14): one guarded file 7.35s, dominated by repeated /proc scans.
# Sequential cron budgets: /var/tmp 300s + /data/system-tmp 1400s, within 1790s.
SCAN_SECONDS = 240
RUN_SECONDS = 1400
VAR_RUN_SECONDS = 300
# Leave time for the existing per-item probes, Mole calls and restoration.
ITEM_RUN_RESERVE_SECONDS = 120
ITEM_SECONDS = 5
CACHE_SECONDS = 60
BATCH_SIZE = 128
DELETE_WORKERS = 8
CACHE_AGE_SECONDS = 86400
CANCELLED = threading.Event()
GO_README = b"This directory holds cached build artifacts from the Go build system.\n"
# Mole 策略阶段逐个候选调用 validate_path_for_deletion（约 25ms）与
# should_protect_path（约 17ms），单项约 50ms。单进程顺序跑 ~6.7k 候选会撞上原先
# 硬编码的 30s 上限，整轮 fail-closed——这正是 /tmp 清理连续 4 天 deleted=0 的根因。
# 修法：同一脚本分片并行，逐项语义完全一致、候选集绝不预截断（绝大多数候选会被
# 策略拒绝，按大小取前缀会让允许项永远排不进来），只是把墙钟时间除以分片数。
POLICY_SHARDS = 32
POLICY_SECONDS = 180
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
    if CANCELLED.is_set():
        raise InterruptedError('cleanup cancelled; restore staged items')
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
        if v.f_blocks <= 0 or v.f_frsize <= 0:
            raise Refused('inconsistent filesystem capacity')
        total = v.f_blocks * v.f_frsize
        free = v.f_bfree * v.f_frsize
        available = v.f_bavail * v.f_frsize
        used = total - free
        reserved = free - available
        if (total <= 0 or free < 0 or available < 0 or used < 0 or reserved < 0
                or available > free or used > total):
            raise Refused('inconsistent filesystem capacity')
        rows[str(path)] = {
            'device': s,
            'total_bytes': total,
            'free_bytes': free,
            'used_bytes': used,
            'available_bytes': available,
            'reserved_bytes': reserved,
            'physical_used_percent': 100 * used / total,
            'unavailable_percent': 100 * (total - available) / total,
            'used_percent': 100 * used / total,
        }
    return rows


def state_for(root):
    return VAR_STATE if root == VAR_TMP_ROOT else STATE


def policy_alias(root):
    return root if root == VAR_TMP_ROOT else Path('/tmp')


def validate_data_mount():
    if not DATA_ROOT.is_mount() or DATA_ROOT.stat().st_dev == Path('/').stat().st_dev:
        raise Refused('/data is not a separate mounted filesystem')


def validate_root(requested):
    if requested == VAR_TMP_ROOT:
        validate_data_mount()
        trusted(requested.parent)
        # is_mount() misses same-device bind mounts; use kernel mount targets.
        mounts = parse_mounts((PROC_ROOT / 'self/mountinfo').read_text())
        if any(destination == str(requested) for _, _, destination in mounts):
            raise Refused('/var/tmp must not be a mount point')
        root = requested.lstat()
        if (not stat.S_ISDIR(root.st_mode) or root.st_uid != 0
                or (root.st_mode & 0o022 and not root.st_mode & stat.S_ISVTX)
                or root.st_dev != Path('/').stat().st_dev or requested.is_mount()):
            raise Refused('/var/tmp must be a root-owned system-disk directory (sticky if writable), not a mount')
        return requested
    if requested not in (TMP_ROOT, Path('/tmp')):
        raise Refused('only /var/tmp, /data/system-tmp and its verified /tmp bind alias are allowed')
    trusted(TMP_ROOT)
    validate_data_mount()
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


def process_snapshot(root, deadline, known=None):
    """Read live processes, skipping only PID/starttime pairs in a complete batch.

    New/reused PIDs get the full tri-state probe even at the final staged sink.
    Any unreadable identity or live evidence refuses; no partial snapshot escapes.
    """
    reserved, watches, processes = set(), set(), []
    identities = {}
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
        original_start = None
        for attempt in range(4):  # Initial probe plus at most three 100ms retries.
            checkpoint(deadline)
            try:
                before = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
                if known is not None and known.get(proc.name) == before[19]:
                    break
                if before[0] == 'Z' or int(before[6]) & 0x200000:
                    break
                if original_start is not None and before[19] != original_start:
                    break  # The original process exited and this PID was reused.
                original_start = before[19]
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
                    break  # The original process no longer holds references.
                identities[proc.name] = before[19]
                if via:
                    processes.append({'pid': int(proc.name), 'via': sorted(via),
                                      'cmdline': (proc / 'comm').read_text().strip()})
                break
            except (FileNotFoundError, ProcessLookupError, PermissionError):
                try:
                    current = (proc / 'stat').read_text().rsplit(')', 1)[1].split()
                    if current[0] == 'Z' or (original_start is not None and current[19] != original_start):
                        break
                    if attempt == 3:
                        raise Refused(f'incomplete evidence for live PID {proc.name}')
                except (FileNotFoundError, ProcessLookupError):
                    break
                # Exiting or execing processes can temporarily lose /proc access
                # (including EACCES while non-dumpable) before becoming zombies.
                # Retry the complete probe, bounded by both count and scan deadline.
                checkpoint(deadline)
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
                checkpoint(deadline)
            except (OSError, ValueError, KeyError, IndexError) as error:
                raise Refused(f'cannot inspect PID {proc.name}: {type(error).__name__}') from error
    return {'reserved': reserved, 'watches': watches, 'processes': processes, 'identities': identities}


def cache_kind(path):
    """Recognize owner layouts, never task-specific directory names. No symlinks."""
    if not stat.S_ISDIR(path.lstat().st_mode):
        return None
    with os.scandir(path) as children:
        entries = {child.name: child for child in children}
    dirs = {name for name, child in entries.items() if child.is_dir(follow_symlinks=False)}
    buckets = {f'{n:02x}' for n in range(256)}
    if dirs == buckets and set(entries) <= buckets | {'README', 'trim.txt'}:
        readme = entries.get('README')
        if readme and readme.is_file(follow_symlinks=False):
            with open(path / 'README', 'rb') as stream:
                if stream.readline(256) == GO_README:
                    return 'go-build'
    if entries and dirs == set(entries) and all(re.fullmatch(r'v[0-9]+', n) for n in dirs):
        # v3 keeps its index beside content as *-index.json; newer versions split it.
        valid = True
        for name in dirs:
            version = path / name
            files = version / 'files'
            if not files.is_dir() or files.is_symlink():
                valid = False
                break
            if not any((version / n).is_dir() and not (version / n).is_symlink()
                       for n in ('index', 'metadata')):
                embedded = name == 'v3' and any(files.glob('[0-9a-f][0-9a-f]/*-index.json'))
                database = version / 'index.db'
                db_metadata = False
                if name == 'v11' and database.is_file() and not database.is_symlink():
                    with database.open('rb') as stream:
                        db_metadata = stream.read(16) == b'SQLite format 3\0'
                if not (embedded or db_metadata):
                    valid = False
                    break
        if valid:
            return 'pnpm-store'
    if entries and dirs == set(entries) and all(
            re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+-[a-z0-9_]+-[0-9a-f]{8}-[0-9]+', n) for n in dirs):
        found = False
        for name in dirs:
            with os.scandir(path / name) as children:
                for child in children:
                    if not child.is_file(follow_symlinks=False) or not re.fullmatch('[0-9a-f]{8}', child.name):
                        return None
                    found = True
        if found:
            return 'node-compile'
    return None


def cache_member(kind, relative, mode):
    """Keep mixed/authored data out of a recognized Go/Node cache."""
    parts = Path(relative).parts
    if relative == '.':
        return True
    if kind == 'go-build':
        if len(parts) == 1:
            return (parts[0] in ('README', 'trim.txt') and stat.S_ISREG(mode)
                    or re.fullmatch('[0-9a-f]{2}', parts[0]) and stat.S_ISDIR(mode))
        return (len(parts) == 2 and stat.S_ISREG(mode)
                and re.fullmatch(parts[0] + r'[0-9a-f]{62}-[ad]', parts[1]))
    if kind == 'node-compile':
        return (len(parts) == 1 and stat.S_ISDIR(mode)
                or len(parts) == 2 and stat.S_ISREG(mode) and re.fullmatch('[0-9a-f]{8}', parts[1]))
    if kind == 'pnpm-store':
        if len(parts) == 1:
            return stat.S_ISDIR(mode)
        section = parts[1]
        if len(parts) == 2:
            if section in ('index.db', 'index.db-wal', 'index.db-shm'):
                return parts[0] == 'v11' and stat.S_ISREG(mode)
            return (stat.S_ISDIR(mode) and (section in ('files', 'index', 'metadata', 'projects', 'tmp', 'partial')
                    or re.fullmatch(r'(?:file|https?|git)\+[^/]+', section)))
        if section in ('files', 'index', 'metadata'):
            if len(parts) == 3:
                return bool(re.fullmatch('[0-9a-f]{2}', parts[2]) and stat.S_ISDIR(mode))
            if len(parts) != 4 or not stat.S_ISREG(mode):
                return False
            if section == 'files':
                return bool(re.fullmatch(r'[0-9a-f]{126}(?:-exec|-index\.json)?', parts[3]))
            return bool(re.fullmatch(r'[0-9a-f]{62}-.+\.json', parts[3]))
        if section == 'projects':
            return len(parts) == 3 and stat.S_ISREG(mode) and bool(re.fullmatch('[0-9a-f]{32}', parts[2]))
        # Unknown partial installs or unpacked source trees are retained.
        return (len(parts) == 3 and parts[2] == 'integrity.json' and stat.S_ISREG(mode)
                and bool(re.fullmatch(r'(?:file|https?|git)\+[^/]+', section)))
    return False


def tree_snapshot(path, cutoff, watches, deadline, cache_type=None):
    digest, size, entries, latest = hashlib.sha256(), 0, 0, 0.0
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
        if cache_type and not cache_member(cache_type, relative, s.st_mode):
            raise Refused('mixed cache layout')
        owner_database = (cache_type == 'pnpm-store' and len(Path(relative).parts) == 2
                          and item.name in ('index.db', 'index.db-wal', 'index.db-shm'))
        if (item.name in PERSISTENT_NAMES or item.name.endswith(PERSISTENT_SUFFIXES)) and not owner_database:
            raise Refused('repository or persistent state')
        if stat.S_ISREG(s.st_mode):
            latest = max(latest, s.st_mtime)
        if s.st_mtime > cutoff and (not cache_type or stat.S_ISREG(s.st_mode)):
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
    result = {'fingerprint': digest.hexdigest(), 'bytes': size, 'entries': entries}
    if cache_type:
        result.update(cache_type=cache_type, latest_file_mtime=latest)
    return result


def build_plan(root, cutoff, deadline):
    live = process_snapshot(root, deadline)
    plan, skipped = [], Counter()
    with os.scandir(root) as children:
        names = sorted(child.name for child in children)
    def inspect(name):
        checkpoint(deadline)
        if (name in PROTECTED_NAMES or name.startswith(PROTECTED_PREFIXES)
                or name.startswith('systemd-') or (name.startswith('.X') and name.endswith('-lock'))):
            return None, 'protected name'
        if name in live['reserved']:
            return None, 'live or mount reference'
        try:
            kind = cache_kind(root / name)
            snapshot = tree_snapshot(root / name, time.time() - CACHE_AGE_SECONDS if kind else cutoff,
                                     live['watches'], min(deadline, time.monotonic() +
                                                         (CACHE_SECONDS if kind else ITEM_SECONDS)), kind)
            return {'name': name, **snapshot}, None
        except Refused as error:
            return None, str(error)
        except TimeoutError:
            checkpoint(deadline)
            return None, 'slow tree retained'
        except FileNotFoundError:
            return None, 'disappeared'

    with ThreadPoolExecutor(max_workers=DELETE_WORKERS) as pool:
        # Bound queued work as well as active work; never materialize a partial scan.
        for offset in range(0, len(names), BATCH_SIZE):
            for row, reason in pool.map(inspect, names[offset:offset + BATCH_SIZE]):
                if row is not None:
                    plan.append(row)
                else:
                    skipped[reason] += 1
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
    result = subprocess.run(['/bin/bash', '-c', command, 'devcloud-policy', str(REPO), str(root), str(policy_alias(root))],
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
    if tmp_original_allowed "$2/$name" "$3/$name" > /dev/null 2>&1; then
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
    if (receipt.name != 'receipt.json' or receipt.parent.parent not in (STATE, VAR_STATE)
            or not receipt.parent.name.startswith('item-')):
        raise Refused('invalid cleanup receipt location')
    s = receipt.lstat()
    if not stat.S_ISREG(s.st_mode) or s.st_uid != 0 or s.st_mode & 0o022 or s.st_nlink != 1:
        raise Refused('unsafe cleanup receipt')
    record = json.loads(receipt.read_text())
    if record['name'] in ('', '.', '..', 'receipt.json') or Path(record['name']).name != record['name']:
        raise Refused('invalid staged name')
    root = VAR_TMP_ROOT if receipt.parent.parent == VAR_STATE else TMP_ROOT
    if (record.get('source') != str(root / record['name'])
            or not math.isfinite(record['cutoff'])
            or record['cutoff'] > time.time() - (CACHE_AGE_SECONDS if record.get('cache_type') else 3 * 86400)):
        raise Refused('receipt is outside the approved path or retention policy')
    validate_root(root)
    item = receipt.parent / record['name']
    if item.parent != receipt.parent or item.name != record['name']:
        raise Refused('invalid staged item')
    deadline = time.monotonic() + (CACHE_SECONDS if record.get("cache_type") else 20)
    live = process_snapshot(item, deadline, record.get('process_identities'))
    if live['reserved']:
        raise Refused('staged item acquired a process or mount reference')
    kind = record.get('cache_type')
    if kind and cache_kind(item) != kind:
        raise Refused('staged cache layout changed')
    snapshot = tree_snapshot(item, record['cutoff'], live['watches'], deadline, kind)
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
    root = VAR_TMP_ROOT if receipt.parent.parent == VAR_STATE else TMP_ROOT
    child = subprocess.Popen(['/bin/bash', '-c', command, 'devcloud-clean', str(REPO),
                              str(Path(__file__).resolve()), str(receipt), str(item),
                              str(root / item.name), str(policy_alias(root) / item.name)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=mole_environment(preview), start_new_session=True)
    try:
        child_deadline = time.monotonic() + seconds
        while True:
            checkpoint(child_deadline)
            try:
                stdout, stderr = child.communicate(timeout=min(0.2, child_deadline - time.monotonic()))
                break
            except subprocess.TimeoutExpired:
                continue
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


def execute_item(root, candidate, cutoff, deadline, batch_live=None):
    checkpoint(deadline)
    validate_root(root)
    live = batch_live if batch_live is not None else process_snapshot(root, deadline)
    name = candidate['name']
    if name in live['reserved']:
        raise Refused('candidate acquired a live reference')
    kind = candidate.get('cache_type')
    if kind:
        cutoff = time.time() - CACHE_AGE_SECONDS
        if cache_kind(root / name) != kind:
            raise Refused('cache layout changed since preview')
    current = tree_snapshot(root / name, cutoff, live['watches'],
                            min(deadline, time.monotonic() + (CACHE_SECONDS if kind else ITEM_SECONDS)), kind)
    if any(current[key] != candidate[key] for key in ('fingerprint', 'bytes', 'entries')):
        raise Refused('candidate changed since preview')
    stage = Path(tempfile.mkdtemp(prefix='item-', dir=state_for(root)))
    item, receipt = stage / name, stage / 'receipt.json'
    if name == receipt.name:
        stage.rmdir()
        raise Refused('reserved receipt name')
    record = {**candidate, 'cutoff': cutoff, 'source': str(root / name),
              'process_identities': live.get('identities')}
    try:
        write_receipt(receipt, record)
        append_log({'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': 'intent',
                    **{key: value for key, value in record.items() if key != 'process_identities'},
                    'path': str(root / name), 'staged': str(item)})
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
    parser.add_argument('--age-days', type=float, default=3, help='minimum idle age in days (>= 3; default: 3)')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--du', action='store_true', help='show measured candidate bytes (already included in JSON)')
    parser.add_argument('--limit', type=int, default=40, help='maximum candidate names displayed')
    options = parser.parse_args(args)
    summary = {'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': 'summary',
               'mode': 'execute' if options.execute else 'preview', 'path': str(options.path),
               'age_days': options.age_days if math.isfinite(options.age_days) else None,
               'deleted': 0, 'failed': 0}
    lock = None
    locked = False
    code = 0
    started = time.monotonic()
    try:
        summary['mounts_before'] = capacity()
        if not math.isfinite(options.age_days) or options.age_days < 3 or options.limit < 0:
            raise Refused('age must be finite and >= 3 days; display limit must be nonnegative')
        root = validate_root(options.path)
        summary['path'] = str(root)
        run_seconds = min(RUN_SECONDS, VAR_RUN_SECONDS) if root == VAR_TMP_ROOT else RUN_SECONDS
        state = state_for(root)
        if options.execute:
            trusted(state.parent)
            state.mkdir(mode=0o700, exist_ok=True)
            trusted(state)
            if state.stat().st_dev != root.stat().st_dev:
                raise Refused('staging must be on the temporary filesystem')
            lock = os.open(state / 'cleanup.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            metadata = os.fstat(lock)
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
                    or metadata.st_mode & 0o022 or metadata.st_nlink != 1):
                raise Refused('unsafe maintenance lock')
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            pending = [str(p) for p in state.glob('item-*')]
            if pending:
                raise Refused(f'unresolved staging receipt; review before continuing: {pending[0]}')
        cutoff = time.time() - options.age_days * 86400
        plan, skipped = build_plan(root, cutoff, started + SCAN_SECONDS)
        selected = plan
        summary.update(scan='complete', candidates=len(selected), eligible=len(plan),
                       deferred=len(plan) - len(selected), skipped=skipped,
                       candidate_bytes=sum(row['bytes'] for row in selected),
                       sample=[row['name'] for row in selected[:options.limit]],
                       cache_roots=[{'path': str(root / row['name']),
                                     **{key: row[key] for key in ('cache_type', 'bytes', 'latest_file_mtime')}}
                                    for row in selected if row.get('cache_type')])
        if options.execute:
            def remove(candidate, live):
                checkpoint(started + run_seconds)
                if started + run_seconds - time.monotonic() < ITEM_RUN_RESERVE_SECONDS:
                    return False
                execute_item(root, candidate, cutoff, started + run_seconds, live)
                append_log({'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'event': 'deleted',
                            'path': str(root / candidate['name']), 'bytes': candidate['bytes'],
                            'cache_type': candidate.get('cache_type')})
                return True

            with ThreadPoolExecutor(max_workers=DELETE_WORKERS) as pool:
                for offset in range(0, len(selected), BATCH_SIZE):
                    if started + run_seconds - time.monotonic() < ITEM_RUN_RESERVE_SECONDS:
                        break
                    # A complete, fresh scan per bounded batch. Never use a partial snapshot.
                    live = process_snapshot(root, started + run_seconds)
                    batch = selected[offset:offset + BATCH_SIZE]
                    futures = [pool.submit(remove, candidate, live) for candidate in batch]
                    cancellation = None
                    try:
                        wait(futures)
                    except InterruptedError as error:
                        CANCELLED.set()
                        cancellation = error
                        for future in futures:
                            future.cancel()
                        # Workers kill their Mole process group and restore isolated items.
                        wait(futures)
                    for future, candidate in zip(futures, batch):
                        if future.cancelled():
                            continue
                        try:
                            if future.result():
                                summary['deleted'] += 1
                                summary['deleted_bytes'] = summary.get('deleted_bytes', 0) + candidate['bytes']
                        except InterruptedError as error:
                            cancellation = error
                        except Exception as error:
                            summary['failed'] += 1
                            summary['error'] = str(error)
                            code = 1
                    if cancellation is not None:
                        summary['deferred'] = len(selected) - summary['deleted'] - summary['failed']
                        raise cancellation
                    if code:
                        break
            summary['deferred'] = len(selected) - summary['deleted'] - summary['failed']
            if summary['deferred'] and not code:
                summary['deferred_reason'] = 'execution budget'
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
            print(f"  {path}: {row['physical_used_percent']:.2f}% physical used; "
                  f"{row['unavailable_percent']:.2f}% unavailable; "
                  f"{row['available_bytes'] / 1024**3:.2f} GiB ordinary available")
        for name in summary.get('sample', []):
            print(f'  {name}')
        if 'error' in summary:
            print(summary['error'], file=sys.stderr)
    return code


def main(argv=None):
    CANCELLED.clear()

    def interrupted(signum, _frame):
        if CANCELLED.is_set():
            return  # A second TERM must not interrupt restoration or child reaping.
        CANCELLED.set()
        raise InterruptedError(f'cleanup cancelled by signal {signum}')

    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        return _main(argv)
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
        CANCELLED.clear()


if __name__ == '__main__':
    raise SystemExit(main())
