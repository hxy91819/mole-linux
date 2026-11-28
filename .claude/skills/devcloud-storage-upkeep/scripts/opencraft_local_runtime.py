#!/usr/bin/env python3
"""Run OpenCraft's owner retention tool; default to preview."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

REPO = Path('/data/code/opencraft')
NODE_BIN = '/root/.nvm/versions/node/v26.7.0/bin'
# Owner handoff for PR #670; keep recovery/config, allow granular release pruning.
PROTECTED_PATHS = (
    '.runtime',
    '.local/execution-separation',
    '.local/personal-relay/__pycache__',
    '.local/personal-relay/deployment',
    '.local/personal-relay/releases',
    '.local/personal-relay/reports',
    '.local/personal-relay/private',
    '.local/personal-relay/current',
    '.local/personal-relay/artifacts',
    '.local/personal-relay/backups',
    '.local/personal-relay/compose/known_hosts',
    '.local/personal-relay/compose/mason-env-new.json',
    '.local/personal-relay/compose/site.env',
    '.local/personal-relay/compose/compose.sh',
    '.local/personal-relay/compose/migration-receipt.json',
    '.local/personal-relay/compose/mason-smoke-expected.json',
    '.local/personal-relay/compose/sync-codex-auth.py',
    '.local/personal-relay/compose/compose.mason.yaml',
    '.local/personal-relay/compose/migrate-mason-state.py',
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    env = dict(os.environ)
    env['PATH'] = f'{NODE_BIN}:/usr/local/bin:/usr/bin:/bin'
    env['OPENCRAFT_LOCAL_RUNTIME_ROOTS'] = f'{REPO}/.local,{REPO}/.runtime'
    env['OPENCRAFT_LOCAL_RUNTIME_PROC_ROOT'] = '/proc'
    protected = [REPO / path for path in PROTECTED_PATHS]
    extra = env.get('OPENCRAFT_LOCAL_RUNTIME_PROTECTED_ROOTS', '')
    env['OPENCRAFT_LOCAL_RUNTIME_PROTECTED_ROOTS'] = ','.join(map(str, protected)) + (
        f',{extra}' if extra else '')
    command = ['/usr/bin/timeout', '--signal=TERM', '840',
               'npm', 'run', 'maintenance:local-runtime']
    if args.apply:
        command += ['--', '--apply']
    try:
        code = subprocess.run(command, cwd=REPO, env=env).returncode
        if code == 124:
            print('OpenCraft owner retention exceeded 840 seconds', file=sys.stderr)
        return code
    except OSError as exc:
        print(f'Cannot run OpenCraft owner retention: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
