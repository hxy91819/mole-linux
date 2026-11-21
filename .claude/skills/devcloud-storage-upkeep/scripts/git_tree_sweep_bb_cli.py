#!/usr/bin/env python3
"""Bind the sweep to BB's injected CLI or the running server's package."""
import os
from pathlib import Path
import subprocess
import sys


def command_for(cli):
    """The JS entrypoint's env-node shebang must not select a PATH runtime."""
    with open(cli, 'rb') as stream:
        first = stream.readline(256).decode('utf-8', 'replace').strip()
    if first.startswith('#!') and any(word == 'node' or word.endswith('/node') for word in first[2:].split()):
        for parent in Path(cli).parents:
            if parent.name == 'node_modules':
                node = parent.parent / 'bin/node'
                if node.is_file() and os.access(node, os.X_OK):
                    return [str(node), cli]
                break
        raise RuntimeError('Packaged Node unavailable for BB_CLI: ' + cli +
                           '; inspect bb-dev-server.service MainPID installation and restart BB')
    return [cli]


def resolve(*, server_only=False):
    value = None if server_only else os.environ.get('BB_CLI')
    if not value:
        try:
            out = subprocess.run(['/usr/bin/systemctl', 'show', 'bb-dev-server.service',
                                  '--property=MainPID', '--value'], capture_output=True, text=True,
                                 timeout=10, check=True)
            pid = out.stdout.strip()
            if not pid.isdigit() or int(pid) <= 0:
                raise ValueError('service MainPID is not running')
            executable = Path(os.readlink('/proc/' + pid + '/exe'))
            value = str(executable.parent.parent / 'node_modules/bb-app/host-daemon/dist/bb')
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            raise RuntimeError('No BB_CLI and cannot locate bb-dev-server.service MainPID: ' + str(exc) +
                               '; inspect systemctl status bb-dev-server.service and restart BB before retrying') from exc
    if not os.path.isabs(value) or not os.path.isfile(value) or not os.access(value, os.X_OK):
        raise RuntimeError('BB_CLI is not an executable absolute file: ' + value +
                           '; start BB to refresh its CLI, then rerun this launcher with the injected BB_CLI')
    value = str(Path(value).resolve(strict=True))
    for args, required in [(['thread', 'list', '--help'], ['--include-archived', '--include-hidden', '--json']),
                           (['environment', 'cleanup', '--help'], ['--json']),
                           (['machine', 'list', '--help'], ['--all', '--json'])]:
        env = dict(os.environ, BB_CLI=value)
        try:
            out = subprocess.run([*command_for(value), *args], env=env, capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError('BB_CLI capability probe failed: ' + value + ' ' + ' '.join(args) +
                               '; inspect this CLI and the running BB installation') from exc
        missing = [option for option in required if option not in out.stdout]
        if out.returncode:
            detail = (out.stderr or out.stdout).strip().splitlines()
            raise RuntimeError('BB_CLI probe exited ' + str(out.returncode) + ': ' + value + ' ' + ' '.join(args) +
                               ('; ' + detail[0][:300] if detail else '') +
                               '; inspect this CLI --version and the running BB installation')
        if missing:
            raise RuntimeError('BB_CLI lacks required command/options: ' + value + ' ' + ' '.join(args) +
                               (' missing ' + ', '.join(missing) if missing else '') +
                               '; start the current BB server and retry with its CLI; PATH bb is never used')
    return value


if __name__ == '__main__':
    try:
        print(resolve())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
