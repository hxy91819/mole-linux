#!/usr/bin/env python3
"""Run OpenCraft's owner retention tool; default to preview."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

REPO = Path('/data/code/opencraft')
NODE_BIN = '/root/.nvm/versions/node/v24.22.1/bin'


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    env = dict(os.environ)
    env['PATH'] = f'{NODE_BIN}:/usr/local/bin:/usr/bin:/bin'
    env['OPENCRAFT_LOCAL_RUNTIME_ROOTS'] = f'{REPO}/.local,{REPO}/.runtime'
    env['OPENCRAFT_LOCAL_RUNTIME_PROC_ROOT'] = '/proc'
    # Retained recovery state (2026-11-18) and live Compose deployment (2026-11-28).
    # Container mount paths need explicit protection even when host /proc is visible.
    protected = [REPO / '.runtime', REPO / '.local/personal-relay',
                 REPO / '.local/execution-separation']
    extra = env.get('OPENCRAFT_LOCAL_RUNTIME_PROTECTED_ROOTS', '')
    env['OPENCRAFT_LOCAL_RUNTIME_PROTECTED_ROOTS'] = ','.join(map(str, protected)) + (
        f',{extra}' if extra else '')
    command = ['npm', 'run', 'maintenance:local-runtime']
    if args.apply:
        command += ['--', '--apply']
    try:
        return subprocess.run(command, cwd=REPO, env=env, timeout=840).returncode
    except subprocess.TimeoutExpired:
        print('OpenCraft owner retention exceeded 840 seconds', file=sys.stderr)
        return 124
    except OSError as exc:
        print(f'Cannot run OpenCraft owner retention: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
