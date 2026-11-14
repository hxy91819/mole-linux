#!/usr/bin/env python3
"""One locked, bounded and accounted clean-tmp entry for cron and watermark."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

EXECUTOR = '/data/tools/devcloud-maintenance/releases/cbb5caf329a61b57/bin/devcloud.py'
LOCK = Path('/var/lib/devcloud-storage-upkeep/clean-tmp.lock')
LOG = Path('/data/metrics/devcloud-clean-tmp.log')
STDERR = Path('/data/metrics/devcloud-clean-tmp.stderr.log')
ROOT_TIMEOUTS = {'/var/tmp': 325, '/data/system-tmp': 1435}
TOTAL_SECONDS = 1790


def command(root):
    # Executor owns the internal 300/1400s budgets, staging lock and JSONL events.
    # It imports devcloud_tmp from its own directory; preserve its existing -B contract.
    return ['/usr/bin/timeout', '--signal=TERM', '--kill-after=10s',
            str(ROOT_TIMEOUTS[root]), '/usr/bin/python3', '-B', EXECUTOR,
            'clean-tmp', '--path', root, '--execute', '--age-days', '3', '--json']


def record(**fields):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open('a') as stream:
        stream.write(json.dumps({'ts': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                                 **fields}) + '\n')


def execute(roots, fd):
    deadline = time.monotonic() + TOTAL_SECONDS
    code = 0
    record(event='runner_start', paths=roots)
    with STDERR.open('a') as errors:
        for root in roots:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                record(event='runner_finish', path=root, returncode=124, error='total budget')
                return 124
            try:
                result = subprocess.run(command(root), text=True, stdout=subprocess.PIPE,
                                        stderr=errors, timeout=remaining, pass_fds=(fd,))
                rc, output = result.returncode, result.stdout
            except subprocess.TimeoutExpired:
                rc, output = 124, ''
            record(event='runner_finish', path=root, returncode=rc, output=output)
            if rc:
                code = rc
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--path', choices=ROOT_TIMEOUTS, help='omit to run both cron roots')
    parser.add_argument('--launch', action='store_true', help='detach with lock inherited by worker')
    parser.add_argument('--lock-fd', type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    roots = [args.path] if args.path else list(ROOT_TIMEOUTS)
    if args.lock_fd is None:
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(LOCK, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            print(json.dumps({'status': 'lock'}))
            return 0
    else:
        fd = args.lock_fd
    try:
        if args.launch:
            LOG.parent.mkdir(parents=True, exist_ok=True)
            cmd = ['/usr/bin/timeout', '--signal=TERM', '--kill-after=10s',
                   str(TOTAL_SECONDS), '/usr/bin/python3', '-I', str(Path(__file__).resolve()),
                   '--lock-fd', str(fd)]
            if args.path:
                cmd += ['--path', args.path]
            with STDERR.open('a') as errors:
                child = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=errors,
                                         stderr=errors, pass_fds=(fd,), start_new_session=True)
            # Closing our copy keeps the child's inherited open-file-description locked.
            print(json.dumps({'status': 'triggered', 'pid': child.pid}))
            return 0
        # Keep the descriptor across timeout/exec, even if the wrapper dies early.
        # The executor also takes its native per-root staging mutex.
        return execute(roots, fd)
    finally:
        os.close(fd)


if __name__ == '__main__':
    sys.exit(main())
