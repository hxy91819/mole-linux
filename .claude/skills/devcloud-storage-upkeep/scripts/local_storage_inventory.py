"""Bounded inventory of host-visible filesystems without following links."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import subprocess
import time

LOCAL = {'xfs', 'ext2', 'ext3', 'ext4', 'btrfs', 'tmpfs', 'devtmpfs',
         'zfs', 'f2fs', 'vfat', 'exfat', 'ntfs3'}
# Split large trees into disjoint subtrees so a cold data disk does not
# serialize the whole scan. Every entry is still passed to find exactly once.
SPLIT = {'/', '/root', '/data', '/data/code', '/data/system-tmp', '/data/codex',
         '/data/bb-data'}


def mounts():
    result = {}
    for line in Path('/proc/self/mountinfo').read_text().split('\n'):
        if line:
            left, right = line.split(' - ', 1)
            path = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), left.split(' ')[4])
            result[path] = right.split(' ')[0]
    return result


def vanished_only(stderr):
    lines = stderr.splitlines()
    return bool(lines) and all(
        re.fullmatch(rb"/usr/bin/find: '.+': No such file or directory", line)
        for line in lines
    )


def inventory(predicate, timeout=650, *, exclude=()):
    started = time.monotonic()
    before = mounts()
    if before.get('/') not in LOCAL:
        raise RuntimeError('unrecognized host root filesystem')
    excluded = {path for path, fs in before.items() if fs not in LOCAL} | set(exclude)
    pruned = [part for path in sorted(excluded)
              for part in ('-path', re.sub(r'([\\*?\[\]])', r'\\\1', path), '-o')]
    expr = ['(', *pruned[:-1], ')', '-prune', '-o'] if pruned else []
    shallow, recursive = [], []

    def partition(path):
        if path in excluded:
            return
        shallow.append(path)
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.path in excluded:
                    continue
                if entry.path in SPLIT and entry.is_dir(follow_symlinks=False):
                    partition(entry.path)
                else:
                    recursive.append(entry.path)

    partition('/')

    def scan(job):
        paths, flags = job
        if not paths:
            return []
        remaining = started + timeout - time.monotonic()
        if remaining <= 0:
            raise RuntimeError('filesystem inventory exceeded its time budget')
        cmd = ['/usr/bin/find', '-P', *paths, '-ignore_readdir_race',
               *flags, *expr, *predicate, '-print0']
        run = subprocess.run(cmd, capture_output=True, timeout=remaining,
                             env=dict(os.environ, LC_ALL='C', QUOTING_STYLE='shell-escape-always'))
        if (run.returncode != 0 or run.stderr) and not (
            run.returncode == 1 and vanished_only(run.stderr)
        ):
            raise RuntimeError(f'filesystem inventory incomplete: {os.fsdecode(run.stderr)[-1000:]}')
        return [Path(os.fsdecode(p)) for p in run.stdout.split(b'\0') if p]

    # Start known large shards before batching ordinary top-level entries.
    heavy = [p for p in recursive if p.startswith(('/data/', '/root/')) or p in {'/tmp', '/var', '/opt'}]
    heavy_set = set(heavy)
    light = [p for p in recursive if p not in heavy_set]
    jobs = [(shallow, ['-maxdepth', '0']), *(([p], []) for p in heavy)]
    jobs.extend((light[i:i+48], []) for i in range(0, len(light), 48))
    with ThreadPoolExecutor(max_workers=8) as pool:
        result = [path for group in pool.map(scan, jobs) for path in group]
    if before != mounts():
        raise RuntimeError('mount inventory changed during scan')
    return result
