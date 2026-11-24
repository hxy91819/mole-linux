#!/usr/bin/env python3
"""Sync the canonical daily storage prompt to its BB automation."""
import argparse
import difflib
import json
from pathlib import Path
import subprocess
import sys

SOURCE = Path(__file__).resolve().parents[1] / 'docs/DEVCLOUD_DAILY_CHECK_PROMPT.md'
AUTOMATION = 'auto_5gacevqssoe'
PROJECT = 'proj_8d3wrcajxn'


def bb(operation, *args):
    command = ['bb', 'automation', operation, AUTOMATION, '--project', PROJECT, '--json', *args]
    return json.loads(subprocess.run(command, check=True, capture_output=True, text=True, timeout=30).stdout)


def read_prompt():
    record = bb('show')
    if record['id'] != AUTOMATION or record['projectId'] != PROJECT or record['execution']['mode'] != 'agent':
        raise ValueError('unexpected BB automation record')
    prompt = record['execution']['prompt']
    if not isinstance(prompt, str):
        raise ValueError('BB prompt is not text')
    return prompt


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true')
    mode.add_argument('--apply', action='store_true')
    parser.add_argument('--staged', action='store_true', help='Check the prompt in the Git index.')
    args = parser.parse_args(argv)
    if args.staged and not args.check:
        parser.error('--staged requires --check')
    try:
        if args.staged:
            root = SOURCE.parents[1]
            command = ['git', 'show', ':' + SOURCE.relative_to(root).as_posix()]
            desired = subprocess.run(command, cwd=root, check=True, capture_output=True, timeout=10).stdout.decode('utf-8')
        else:
            desired = SOURCE.read_bytes().decode('utf-8')
        actual = read_prompt()
        if args.apply and desired != actual:
            bb('update', '--prompt', desired)
            actual = read_prompt()
        if desired == actual:
            print(f'{AUTOMATION}: prompt matches {SOURCE.name} exactly')
            return 0
        for line in difflib.unified_diff(actual.splitlines(keepends=True), desired.splitlines(keepends=True),
                                         fromfile=AUTOMATION, tofile=SOURCE.name):
            sys.stdout.write(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n')
        return 1
    except subprocess.CalledProcessError as exc:
        # Do not include the whole prompt from the failed command's argv.
        detail = exc.stderr.decode('utf-8', 'replace') if isinstance(exc.stderr, bytes) else exc.stderr
        print(f'{exc.cmd[0]} {exc.cmd[1]} failed ({exc.returncode}): {detail.strip()}', file=sys.stderr)
    except subprocess.TimeoutExpired:
        print(f'{exc.cmd[0]} command timed out', file=sys.stderr)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'Cannot verify daily prompt: {exc}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
