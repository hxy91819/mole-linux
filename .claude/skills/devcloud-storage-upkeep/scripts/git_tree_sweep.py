#!/usr/bin/env python3
"""Full-disk independent clones; worktree_sweep.py handles registered linked trees.

Owner policy 2026-11-17. Discovery is physical, unbounded in depth and -xdev
per disk. Legacy entrypoints supply helpers only; they are not scheduled.
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures as cf
import contextlib
import fcntl
import glob
import importlib.util
import itertools
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit, unquote


def sibling(name):
    path = Path(__file__).with_name(name + '.py')
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    exec(compile(path.read_bytes(), str(path), 'exec'), module.__dict__)
    return module


ws = sibling('worktree_sweep')
tc = sibling('tmp_clone_sweep')
_run = ws.run
_bb_cli_helper = None
IDLE = 7 * 86400
CONFIG_TEXT_LIMIT = 1024 * 1024
GENERIC_NAMES = set('app apps assets backup backend bin build cache client code config configuration contents data debug default dependency dependencies directory dist doc docs documentation example examples frontend git home include install lib local log logs main master module modules node_modules package packages plugin plugins private project projects public release repo repository root runtime sample scripts server service services share site software source src static storage target temp templates test tests tmp tool tools upstream vendor version versions website work worktree worktrees'.split())
NON_CONFIG_DOTFILES = {'.bash_history', '.zsh_history', '.python_history', '.lesshst', '.viminfo', '.wget-hsts'}
VIRTUAL = {'/proc', '/sys', '/dev', '/run'}
DEPENDENCIES = {'node_modules', 'vendor', '.venv', 'venv', '__pypackages__', '.pnpm-store'}
SHELL_NAMES = {'.bashrc', '.bash_profile', '.bash_login', '.profile', '.zshrc', '.zprofile',
               '.zshenv', '.zlogin', '.pam_environment'}
ENV_NAME = re.compile(r'^(PATH|.*_(HOME|ROOT|DIR))$')
DEADLINE = float('inf')
LIVE = {}
LIVE_LOCK = threading.Lock()
EXECUTION = threading.local()
CLONE_RECHECK_SECONDS = 10


@contextlib.contextmanager
def execution_step(name):
    row = getattr(EXECUTION, 'row', None)
    started = time.monotonic()
    try:
        yield
    finally:
        if row is not None:
            values = row.setdefault('execution_timings', {})
            values[name] = round(values.get(name, 0) + time.monotonic() - started, 6)


def remote_failure(exc):
    if isinstance(exc, subprocess.TimeoutExpired):
        return 'remote timeout'
    message = str(getattr(exc, 'stderr', None) or exc).lower()
    for reason, patterns in (
        ('remote timeout', ('timed out', 'operation too slow')),
        ('remote authentication failed', ('authentication failed', 'permission denied (publickey',
                                          'could not read username', 'http 401', 'http 403', 'returned error: 401',
                                          'returned error: 403', 'host key verification failed')),
        ('remote network failure', ('could not resolve', 'connection refused', 'network is unreachable',
                                    'connection reset', 'failed to connect', 'no route to host')),
        ('remote repository missing or access denied', ('repository not found', 'does not appear to be a git repository')),
    ):
        if any(pattern in message for pattern in patterns):
            return reason
    return 'remote query failed'


def live_query(path, url, symref=False):
    """One live server query per effective URL in a planning generation."""
    target = tc.local_path(url, path) if url else None
    key = (('local', target) if target else ('remote', url), symref)
    with LIVE_LOCK:
        entry = LIVE.setdefault(key, {'lock': threading.Lock()})
    with entry['lock']:
        if 'result' not in entry:
            until = min(time.monotonic() + 90, DEADLINE)
            for attempt in range(2):
                left = until - time.monotonic()
                if left <= 0:
                    entry.setdefault('result', ws.ScanIncomplete('remote query budget exhausted'))
                    break
                try:
                    entry['result'] = git(path, 'ls-remote', *(['--symref'] if symref else ['--heads', '--tags']),
                                          '--', url, *(['HEAD'] if symref else []), timeout=left / (2 - attempt)).stdout
                    break
                except Exception as exc:
                    entry['result'] = exc
                    if remote_failure(exc) != 'remote timeout':
                        break
        if isinstance(entry['result'], Exception):
            raise entry['result']
        return entry['result']


class LiveRepo(ws.Repo):
    def load_live(self, include_tags=True):
        self.remote_errors = {}
        for name, (url, _) in self.remotes.items():
            try:
                out = live_query(self.main, ws.transport_url(url))
            except Exception as exc:
                self.unreachable.add(name)
                self.remote_errors[name] = remote_failure(exc)
                continue
            refs = {ref: sha for sha, ref in (line.split('\t') for line in out.splitlines() if '\t' in line)}
            self.live[name] = refs
            for ref, sha in refs.items():
                self.by_sha.setdefault(sha, f'{name}:{ref}')

    def default_branch(self, name):
        if name not in self._default:
            sym = git(self.main, 'symbolic-ref', '-q', f'refs/remotes/{name}/HEAD', check=False).stdout.strip()
            branch = sym.split('/', 3)[3] if sym.count('/') >= 3 else None
            if not branch:
                try:
                    out = live_query(self.main, ws.transport_url(self.remotes[name][0]), symref=True)
                except Exception as exc:
                    raise ws.Keep(f'{remote_failure(exc)}: {name} (default branch)') from exc
                match = re.match(r'ref: refs/heads/(\S+)\tHEAD', out)
                branch = match[1] if match else None
            self._default[name] = branch
        return self._default[name]


def checkpoint():
    if time.monotonic() >= DEADLINE:
        raise ws.ScanIncomplete('time budget reached')


def command(args, cwd=None, timeout=120, check=True, env=None):
    global _bb_cli_helper
    checkpoint()
    category = ('bb' if args[0] == 'bb' else 'du' if args[0] == 'du' else
                'ls_remote' if args[0] == 'git' and 'ls-remote' in args else
                'archive_push' if args[0] == 'git' and 'push' in args else
                'git_status' if args[0] == 'git' and 'status' in args else 'other')
    if args and args[0] == 'bb':
        cli = os.environ.get('BB_CLI', '')
        if not os.path.isabs(cli) or not os.path.isfile(cli) or not os.access(cli, os.X_OK):
            raise ws.ScanIncomplete('BB_CLI missing or not executable; run git_tree_sweep_run.sh to bind the running server CLI; PATH bb is never used')
        if _bb_cli_helper is None:
            _bb_cli_helper = sibling('git_tree_sweep_bb_cli')
        args = [*_bb_cli_helper.command_for(cli), *args[1:]]
        env = dict(ws.ENV if env is None else env, BB_CLI=cli)
    started = time.monotonic()
    try:
        return _run(args, cwd=cwd, timeout=max(.01, min(timeout, DEADLINE - time.monotonic())), check=check, env=env)
    finally:
        row = getattr(EXECUTION, 'row', None)
        if row is not None:
            values = row.setdefault('execution_subprocess_seconds', {})
            values[category] = round(values.get(category, 0) + time.monotonic() - started, 6)


# The shared Repo proof helper must observe the same overall deadline.
ws.run = command


def git(path, *args, timeout=120, check=True):
    return command(['git', '-C', str(path), *args], timeout=timeout, check=check)


def inside(path, root):
    return path == root or path.startswith(root.rstrip('/') + '/')


def canonical(path):
    path = os.path.realpath(os.path.abspath(path))
    if inside(path, '/tmp'):
        # /tmp is a bind mount, not a symlink, on this machine.
        try:
            if os.path.samefile('/tmp', '/data/system-tmp'):
                path = '/data/system-tmp' + path[4:]
        except OSError:
            pass
    return path


def anchor(anchors, path, reason):
    if path.startswith('/'):
        ws.add_anchor(anchors, canonical(path), reason)


def unresolved(reason, expression, values=()):
    """Keep evidence separate from its eventual prefix/project/global scope."""
    prefixes = []
    for value in values:
        if not value.startswith('/'):
            continue
        match = re.search(r'[$`*?\[]', value)
        if match:
            literal = value[:match.start()]
            if literal:
                partial = not literal.endswith('/')
                if partial:
                    parent, name = os.path.split(literal)
                    prefix = os.path.join(canonical(parent), name)
                    prefixes.append({'path': prefix, 'partial': True})
                    resolved = canonical(literal)
                    if resolved != prefix:
                        prefixes.append({'path': resolved, 'partial': False})
                else:
                    prefixes.append({'path': canonical(literal), 'partial': False})
    return {'source': reason, 'expression': expression, 'prefixes': prefixes}


def unique_unknown(records):
    return list({json.dumps(r, sort_keys=True): r for r in records}.values())


def shell_words(value):
    """Keep command substitutions inside their word; never evaluate them."""
    substitutions, pieces, cursor = {}, [], 0
    while cursor < len(value):
        if value[cursor] != '`' and not value.startswith('$(', cursor):
            pieces.append(value[cursor])
            cursor += 1
            continue
        start, backtick = cursor, value[cursor] == '`'
        cursor += 1 if backtick else 2
        depth, quote, escaped = 1, None, False
        while cursor < len(value):
            char = value[cursor]
            cursor += 1
            if escaped:
                escaped = False
                continue
            if char == '\\':
                escaped = True
            elif backtick and char == '`':
                break
            elif not backtick:
                if quote:
                    if char == quote: quote = None
                elif char in ('\x22', "'"): quote = char
                elif char == '(': depth += 1
                elif char == ')':
                    depth -= 1
                    if not depth: break
        key = f'__GIT_SWEEP_SUB_{len(substitutions)}__'
        substitutions[key] = value[start:cursor]
        pieces.append(key)
    masked = ''.join(pieces)
    result = shlex.split(masked)
    for key, original in substitutions.items():
        result = [part.replace(key, original) for part in result]
    return result


def remote_identity(value, base):
    """Compare repository identities without persisting URL credentials."""
    local = tc.local_path(value, base)
    if local:
        # Local foo and foo.git may be distinct stores; only /.git is an
        # address of the same checkout's metadata. Physical aliases normalize.
        return 'file:' + canonical(local).removesuffix('/.git').rstrip('/')
    if '://' in value:
        parsed = urlsplit(value)
        host, path = parsed.hostname, unquote(parsed.path)
        if parsed.port and not (parsed.scheme == 'ssh' and parsed.port == 22 or
                                parsed.scheme == 'https' and parsed.port == 443 or
                                parsed.scheme == 'http' and parsed.port == 80):
            host = f'{host}:{parsed.port}'
    else:
        match = re.match(r'(?:[^/@:]+@)?([^/:]+):(.+)$', value)
        if not match:
            return 'opaque:' + value
        host, path = match.groups()
    return (host or '').lower() + '/' + path.strip('/').removesuffix('.git')


def scratch_git(path, *args):
    """Only our bare directory may be written; no inherited Git context."""
    env = {key: value for key, value in ws.ENV.items() if not key.startswith('GIT_')}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0',
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
    return command(['git', '--git-dir', str(path), '-C', str(path), *args], env=env)


def source_environment():
    """Read the selected repository, with the host's selected config files."""
    retained = {'GIT_CONFIG_GLOBAL', 'GIT_CONFIG_SYSTEM', 'GIT_CONFIG_NOSYSTEM'}
    env = {key: value for key, value in ws.ENV.items() if not key.startswith('GIT_') or key in retained}
    env.update(GIT_OPTIONAL_LOCKS='0', GIT_TERMINAL_PROMPT='0')
    return env


def source_git(path, *args, check=True):
    return command(['git', '-C', str(path), *args], env=source_environment(), check=check)


def scope_unknown(anchors, records, inventory):
    """Only unowned, unbounded expressions invalidate the entire generation."""
    trees = inventory['trees']
    urls = inventory.get('remote_urls', {})
    for record in records:
        if not isinstance(record, dict):
            continue  # Legacy/mock failures remain global.
        protected = set()
        prefixes = record.get('prefixes', [])
        if prefixes:
            record['scope'] = 'prefix'
            for prefix in prefixes:
                path = prefix['path']
                protected.update(tree for tree in trees if inside(path, tree) or
                                 (tree.startswith(path) if prefix['partial'] else inside(tree, path)))
        elif record.get('project_id') and (record.get('project_paths') or record.get('project_source_paths') or
                                           record.get('project_environment_paths') or record.get('project_remote_ids')):
            record['scope'] = 'project'
            paths = record.get('project_source_paths', record.get('project_paths', []))
            environment_paths = record.get('project_environment_paths', [])
            identities = set(record.get('project_remote_ids', []))
            source_trees = {tree for tree in trees if any(inside(path, tree) for path in paths)}
            if paths and not source_trees:
                # A non-Git project container (e.g. /data/code) does not own
                # every repository below it, or its environments' remotes.
                identities.clear()
                record['project_scope_kind'] = 'source-points-and-environments'
            else:
                record['project_scope_kind'] = 'git-source-and-matching-remotes'
            for tree in source_trees:
                identities.update(urls.get(tree, []))
            protected.update(source_trees)
            protected.update(tree for tree in trees if any(inside(path, tree) for path in environment_paths))
            protected.update(tree for tree, remotes in urls.items() if identities.intersection(remotes) and tree in trees)
            for point in paths + environment_paths:
                anchor(anchors, point, f'unresolved project reference: {record["source"]}')
        else:
            record['scope'] = 'global'
            protected.update(trees)
        record['protected_paths'] = sorted(protected)
        for path in protected:
            anchor(anchors, path, f'unresolved {record["scope"]} reference: {record["source"]}')
    return records


def global_unknown(records):
    return [r if isinstance(r, str) else r['source'] + ': ' + r['expression']
            for r in records if not isinstance(r, dict) or r.get('scope') == 'global']


def managed_roots():
    """Resolve owner storage, including symlinked/migrated caches, not project roots."""
    roots = {'/nix/store', '/gnu/store', '/var/lib/docker', '/var/lib/containerd'}
    homes = {p.pw_dir for p in pwd.getpwall() if p.pw_dir.startswith('/')}
    for home in homes:
        for suffix in ('.npm/_cacache', '.cache/pip', '.cache/uv', '.cache/pnpm',
                       '.local/share/pnpm/store', '.cargo/registry', '.cargo/git',
                       'go/pkg/mod', '.cache/go-build', '.cache/golangci-lint'):
            roots.add(os.path.join(home, suffix))
    for key in ('GOMODCACHE', 'GOCACHE', 'PNPM_STORE_DIR', 'UV_CACHE_DIR', 'PIP_CACHE_DIR'):
        if os.environ.get(key, '').startswith('/'):
            roots.add(os.environ[key])
    if shutil.which('go'):
        data = json.loads(command(['go', 'env', '-json', 'GOMODCACHE', 'GOCACHE'], timeout=30).stdout)
        roots.update(v for v in data.values() if v.startswith('/'))
    if shutil.which('docker'):
        root = command(['docker', 'info', '--format', '{{.DockerRootDir}}'], timeout=60).stdout.strip()
        if not root.startswith('/') or root == '/':
            raise ws.ScanIncomplete('Docker data root unknown')
        roots.add(root)
    return {canonical(p) for p in roots if p != '/'}


def metadata_links(root, device):
    """Find metadata symlinks using dirent types, with the same -xdev boundary."""
    pending, links = [root], []
    while pending:
        checkpoint()
        parent = pending.pop()
        try:
            if os.stat(parent, follow_symlinks=False).st_dev != device:
                continue
            with os.scandir(parent) as entries:
                for entry in entries:
                    if entry.is_symlink():
                        links.append((entry.path, canonical(entry.path)))
                    elif entry.is_dir(follow_symlinks=False):
                        pending.append(entry.path)
        except FileNotFoundError:
            continue
    return links


def dependency_storage(path, name, parent_names):
    """Names are hints; only owner metadata authorizes pruning discovery."""
    if name not in DEPENDENCIES:
        return None
    root = Path(path)
    if name in ('.venv', 'venv') and (root / 'pyvenv.cfg').is_file():
        return 'dependency directory: pyvenv.cfg'
    if name == 'node_modules':
        if 'package.json' in parent_names or any((root / p).exists() for p in ('.package-lock.json', '.modules.yaml', '.pnpm')):
            return 'dependency directory: npm/pnpm metadata'
    if name == 'vendor':
        if 'go.mod' in parent_names and (root / 'modules.txt').is_file():
            return 'dependency directory: go.mod and modules.txt'
        if 'composer.json' in parent_names and (root / 'composer').is_dir():
            return 'dependency directory: Composer metadata'
        if 'Gemfile' in parent_names and (root / 'bundle').is_dir():
            return 'dependency directory: Bundler layout'
        if 'Cargo.toml' in parent_names:
            with os.scandir(root) as entries:
                if any(e.is_dir(follow_symlinks=False) and Path(e.path, '.cargo-checksum.json').is_file() for e in entries):
                    return 'dependency directory: Cargo checksums'
    if name == '__pypackages__' and ('pyproject.toml' in parent_names or 'pdm.lock' in parent_names):
        with os.scandir(root) as entries:
            if any(re.fullmatch(r'\d+\.\d+', e.name) and Path(e.path, 'lib').is_dir() for e in entries):
                return 'dependency directory: PEP 582 layout'
    if name == '.pnpm-store':
        with os.scandir(root) as entries:
            if any(re.fullmatch(r'v\d+', e.name) and Path(e.path, 'files').is_dir() for e in entries):
                return 'owner-managed storage: pnpm format'
    return None


def discover(roots, excluded):
    """Continue below worktrees (including ignored containers); never follow links.

    Bare repositories are reference sources, not deletion candidates. Inode
    de-duplication handles bind aliases; a different device is scanned only as
    an explicit disk root. A failed walk invalidates the complete deletion plan.
    """
    trees, bare, links, errors, skipped = {}, set(), [], [], {}
    visited = set()
    scan_roots = [canonical(root) for root in roots]
    for root in scan_roots:
        try:
            device = os.stat(root).st_dev
            pending = [root]
            while pending:
                checkpoint()
                parent = pending.pop()
                try:
                    info = os.stat(parent, follow_symlinks=False)
                except FileNotFoundError:
                    continue  # A vanished subtree cannot still reference a checkout.
                if info.st_dev != device or (info.st_dev, info.st_ino) in visited:
                    continue
                visited.add((info.st_dev, info.st_ino))
                try:
                    entries = list(os.scandir(parent))
                except FileNotFoundError:
                    continue
                names = {e.name for e in entries}
                if {'HEAD', 'config', 'objects', 'refs'} <= names:
                    bare.add(parent)
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            links.append((entry.path, canonical(entry.path)))
                            if entry.name == '.git':
                                trees[parent] = 'unknown'
                            continue
                        if entry.name == '.git':
                            trees[parent] = ('clone' if entry.is_dir(follow_symlinks=False) else
                                             'linked' if entry.is_file(follow_symlinks=False) else 'unknown')
                            if entry.is_dir(follow_symlinks=False):
                                # Metadata can itself contain symlinks into another
                                # checkout. Scan it for references, not candidates.
                                links.extend(metadata_links(entry.path, device))
                            continue
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                        reason = ('virtual filesystem' if entry.path in VIRTUAL else
                                  'owner-managed storage' if entry.path in excluded else
                                  dependency_storage(entry.path, entry.name, names))
                        if reason:
                            skipped[entry.path] = reason
                        elif parent not in bare or entry.name not in ('objects', 'refs'):
                            pending.append(entry.path)
                    except FileNotFoundError:
                        continue
        except Exception as exc:
            errors.append(f'{parent if "parent" in locals() else root}: {ws.failure_text(exc)}')
    return {'trees': trees, 'bare': sorted(bare), 'links': links, 'errors': errors,
            'excluded': skipped, 'directories': len(visited), 'scan_roots': scan_roots}


def add_environment(anchors, values, reason, base=None):
    for key, value in values.items():
        if ENV_NAME.fullmatch(key):
            for path in (value.split(':') if key == 'PATH' else [value]):
                if path and not path.startswith('/'):
                    if not base:
                        raise ws.ScanIncomplete(f'relative process environment path unknown: {key}')
                    path = os.path.join(base, path)
                anchor(anchors, path, f'{reason} {key}')


class ProcessEnvironments(dict):
    def __init__(self):
        super().__init__()
        self.contexts = []
        self.scopes = {}


def process_mounts():
    mounts = set()
    # Kernel mountinfo escapes ASCII space/tab/newline/backslash only. Other
    # whitespace (including NBSP and Unicode line separators) is path data.
    for line in Path('/proc/self/mountinfo').read_text().split('\n'):
        if not line:
            continue
        fields = [field for field in line.split(' ') if field]
        if len(fields) < 6:
            raise ws.ScanIncomplete('mount inventory incomplete')
        path = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4])
        mounts.update((path, os.path.realpath(path), canonical(path)))
    return mounts


def process_directory(anchors, scopes, path, login_home, mounts, reason):
    if not path.startswith('/'):
        raise ws.ScanIncomplete('process directory is not absolute: ' + reason)
    physical = canonical(path)
    anchor(anchors, path, reason)
    # Generic roots say nothing about ownership of their children.
    if (physical == '/' or physical in mounts or os.path.realpath(path) in mounts or os.path.ismount(path) or
            login_home and physical == canonical(login_home)):
        return
    scopes.setdefault(physical, reason + ' subtree')


def scope_directories(anchors, scopes, inventory):
    for root, reason in scopes.items():
        for tree in inventory['trees']:
            if inside(tree, root):
                anchor(anchors, tree, reason)


def argument_paths(argv, cwd):
    """Literal process operands only; never evaluate a shell or its variables."""
    words = list(argv)
    index = None
    if argv and os.path.basename(argv[0]) in ('sh', 'bash', 'dash', 'zsh', 'ksh'):
        cursor = 1
        while cursor < len(argv):
            option = argv[cursor]
            if option == '--' or not option.startswith(('-', '+')) or option in ('-', '+'):
                break
            if re.fullmatch(r'-[A-Za-z]+', option) and 'c' in option:
                index = cursor + 1
                # -o/-O consume an option name before the pending command.
                if any(flag in option[1:] for flag in ('o', 'O')):
                    index += 1
                break
            cursor += 2 if option in ('-o', '+o', '-O', '+O', '--rcfile', '--init-file') else 1
    if index is not None:
        if index < len(argv):
            words = list(argv[:index]) + list(argv[index + 1:])
            try:
                lexer = shlex.shlex(argv[index], posix=True, punctuation_chars=';&|()<>')
                lexer.whitespace_split = True
                lexer.commenters = ''
                words.extend(lexer)
            except ValueError:
                # Here-doc content is not necessarily shell quoting. Preserve
                # its literal absolute operands without running the command.
                for quoted, plain in re.findall(r'''["'](/[^"'\n]+)["']|(?:^|[=\s])(/[^\s;|&<>()]+)''', argv[index]):
                    words.append(quoted or plain)
    operands = []
    for word in words:
        # The original may itself be a filename containing '='. Also inspect
        # option/assignment values; neither spelling is discarded.
        operands.append(word)
        if '=' in word:
            operands.append(word.split('=', 1)[1])
        if word.startswith('-C') and len(word) > 2:
            operands.append(word[2:])
    for value in operands:
        if value.startswith('file:'):
            path = tc.local_path(value, cwd or '/')
            if path:
                yield path
        elif value.startswith('/'):
            yield value
        elif cwd and value:
            target = os.path.join(cwd, value)
            if os.path.lexists(target) or (not value.startswith('-') and '/' in value and '\n' not in value and '\r' not in value and not re.match(r'^[^/]+:', value)):
                yield target


def process_anchors(anchors, proc='/proc'):
    environments = ProcessEnvironments()
    mounts = process_mounts()
    for pid in os.listdir(proc):
        checkpoint()
        if not pid.isdigit():
            continue
        base = Path(proc, pid)
        try:
            # Read identity before and after so a recycled PID cannot hide a process.
            before = (base / 'stat').read_text()
            uid = re.search(r'^Uid:\s+(\d+)', (base / 'status').read_text(), re.M)
            if not uid:
                raise ws.ScanIncomplete(f'process uid unknown: {pid}')
            try:
                login_home = pwd.getpwuid(int(uid[1])).pw_dir
            except KeyError:
                login_home = None  # Unknown login home keeps the larger scope.
            cwd = None
            for link in [base / 'cwd', base / 'exe', *list((base / 'fd').iterdir())]:
                try:
                    target = os.readlink(link).removesuffix(' (deleted)')
                    if link == base / 'cwd':
                        cwd = target
                        anchor(anchors, target, f'process {pid} cwd')
                    anchor(anchors, target, f'process {pid}')
                except FileNotFoundError:
                    pass
            argv = [os.fsdecode(part) for part in (base / 'cmdline').read_bytes().split(b'\0') if part]
            for path in argument_paths(argv, cwd):
                anchor(anchors, path, f'process {pid} argv')
            for line in (base / 'maps').read_text().splitlines():
                fields = line.split(None, 5)
                if len(fields) == 6:
                    anchor(anchors, fields[5].removesuffix(' (deleted)'), f'process {pid} maps')
            values = dict(part.split('=', 1) for part in (base / 'environ').read_text().split('\0') if '=' in part)
            add_environment(anchors, values, f'process {pid} environment', cwd)
            for key, value in values.items():
                if value and (key == 'HOME' or key.startswith('XDG_') and key.endswith('_HOME')):
                    path = value if value.startswith('/') else os.path.join(cwd or '', value)
                    process_directory(anchors, environments.scopes, path, login_home, mounts, f'process {pid} {key}')
            for key, value in values.items():
                environments.setdefault(key, set()).add(value)
            if values:
                environments.contexts.append(values)
            after = (base / 'stat').read_text()
            if before.rsplit(')', 1)[1].split()[19] != after.rsplit(')', 1)[1].split()[19]:
                raise ws.ScanIncomplete('process identity changed')
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError) as exc:
            raise ws.ScanIncomplete(f'process scan incomplete: {pid}') from exc
    return environments


def text_anchors(anchors, text, home, reason, variables=None, strict=True, _depth=0, _functions=None):
    """Read shell quoting without evaluating code; propagate unresolved values."""
    variables = dict(variables or {}, HOME=home)
    unknown = []
    if _depth > 8:
        return [unresolved(reason, 'function expansion limit')]
    functions = dict(_functions or {})
    def braces(line):
        quote, escaped, parameter = None, False, 0
        for index, char in enumerate(line):
            if escaped:
                escaped = False
                continue
            if char == '\\' and quote != "'":
                escaped = True
                continue
            if quote:
                if char == quote:
                    quote = None
                continue
            if char in ("'", '"'):
                quote = char
            elif char == '{' and (parameter or index and line[index - 1] == '$'):
                parameter += 1
            elif char == '}' and parameter:
                parameter -= 1
            elif not parameter and char in '{}':
                yield index, 1 if char == '{' else -1
    statements, body, function, balance = [], [], None, 0
    for line in text.replace('\\\n', '').splitlines():
        declaration = re.match(r'^\s*(?:function\s+)?([A-Za-z_][A-Za-z_0-9]*)\s*\(\s*\)\s*\{(.*)$', line)
        if not function and declaration:
            function, line, body, balance = declaration[1], declaration[2], [], 1
        if function:
            for index, delta in braces(line):
                balance += delta
                if balance == 0:
                    body.append(line[:index])
                    functions[function] = '\n'.join(body)
                    function = None
                    if line[index + 1:].strip():
                        statements.append(line[index + 1:])
                    break
            if function:
                body.append(line)
            continue
        statements.append(line)
    if function and strict:
        unknown.append(unresolved(reason, 'function body unresolved: ' + '\n'.join(body)))

    def tokens(value, depth=0):
        lexer = shlex.shlex(value, posix=True, punctuation_chars=';&|()<>')
        lexer.whitespace_split = True
        lexer.commenters = '#'
        result = list(lexer)
        if depth < 3:
            for token in list(result):
                if (any(c.isspace() for c in token) and not token.startswith('/') and
                        not re.match(r'^[A-Za-z_][A-Za-z_0-9]*=', token)):
                    try:
                        result.extend(tokens(token, depth + 1))
                    except ValueError:
                        pass
        return result

    cursor = 0
    while cursor < len(statements):
        line = statements[cursor]
        cursor += 1
        if line.lstrip().startswith('#'):
            continue
        line = line.replace('~/', home.rstrip('/') + '/').replace('%h/', home.rstrip('/') + '/')
        for _ in range(4):
            def default_value(match):
                key, colon, operation, fallback = match.groups()
                value = variables.get(key)
                present = bool(value) if colon else key in variables
                if operation in ('?', '+'):
                    if value is not None and re.search(r'[$`]', value):
                        return match[0]
                    if operation == '?':
                        return value if present else match[0]
                    if key not in variables:
                        return match[0]
                    return fallback if present else ''
                value = value if present else fallback
                value = re.sub(r'\$(?:\{([A-Za-z_][A-Za-z_0-9]*)\}|([A-Za-z_][A-Za-z_0-9]*))',
                               lambda m: variables.get(m[1] or m[2], m[0]), value)
                if operation == '=':
                    variables[key] = value
                return value
            line = re.sub(r'\$\{([A-Za-z_][A-Za-z_0-9]*)(:?)([-=+?])([^{}]*)\}', default_value, line)
            line = re.sub(r'\$\{([A-Za-z_][A-Za-z_0-9]*)\}',
                          lambda m: variables[m[1]] if m[1] in variables and not re.search(r'[$`]', variables[m[1]]) else m[0], line)
        loop = re.match(r'^\s*for\s+([A-Za-z_][A-Za-z_0-9]*)\s+in\s+(.+)$', line)
        if loop:
            sequence = loop[2]
            inline = re.split(r';\s*do(?:\s|$)', sequence, 1)
            sequence = inline[0].rstrip('; ')
            loop_body = [inline[1]] if len(inline) == 2 else []
            nesting = 1
            if loop_body and re.search(r';\s*done\b', loop_body[0]):
                loop_body[0], tail = re.split(r';\s*done\b', loop_body[0], 1)
                nesting = 0
                if tail.strip(): statements.insert(cursor, tail)
            while nesting and cursor < len(statements):
                statement = statements[cursor]
                cursor += 1
                if re.match(r'^\s*(for|while|until)\b', statement): nesting += 1
                if re.match(r'^\s*done\b', statement):
                    nesting -= 1
                    if not nesting:
                        tail = re.sub(r'^\s*done\b', '', statement)
                        if tail.strip(): statements.insert(cursor, tail)
                        break
                if statement.strip() != 'do': loop_body.append(statement)
            sequence = re.sub(r'\$(?:\{([A-Za-z_][A-Za-z_0-9]*)\}|([A-Za-z_][A-Za-z_0-9]*))',
                              lambda m: variables.get(m[1] or m[2], m[0]), sequence)
            if nesting or '$' in sequence or '`' in sequence:
                try:
                    loop_values = shell_words(sequence)
                except ValueError:
                    loop_values = []
                if not loop_values:
                    loop_values = ['$' + loop[1]]
                for value in loop_values:
                    if strict and (nesting or re.search(r'[$`*?\[]', value)):
                        unknown.append(unresolved(reason, 'for ' + loop[1] + ' in ' + sequence, [value]))
                    anchor(anchors, value, reason)
                    unknown.extend(text_anchors(anchors, '\n'.join(loop_body), home, reason,
                                                dict(variables, **{loop[1]: value}), strict, _depth + 1, functions))
                continue
            try:
                values = shlex.split(sequence)
            except ValueError:
                if strict: unknown.append(unresolved(reason, 'loop quoting unresolved: ' + sequence))
                continue
            expanded = []
            for value in values:
                if re.search(r'[*?\[]', value):
                    anchor(anchors, re.split(r'[*?\[]', value, 1)[0], reason)
                    if strict:
                        unknown.append(unresolved(reason, 'for ' + loop[1] + ' in ' + sequence, [value]))
                    expanded.extend(glob.glob(value) or [value])
                else:
                    expanded.append(value)
            for value in expanded:
                checkpoint()
                anchor(anchors, value, reason)
                unknown.extend(text_anchors(anchors, '\n'.join(loop_body), home, reason,
                                            dict(variables, **{loop[1]: value}), strict, _depth + 1, functions))
            continue
        line = line.replace('~/', home.rstrip('/') + '/').replace('%h/', home.rstrip('/') + '/')
        # This prompt rendering operation returns PWD; it does not construct
        # another filesystem path. PROMPT_COMMAND itself is executable code.
        line = re.sub(r'\$\{PWD/[^}]*\}', lambda _: variables.get('PWD', ''), line)
        # Substitute only this known read-only metadata query, never evaluate
        # shell commands or infer the output from the executable's directory.
        def go_value(match):
            executable = match[1]
            if canonical(executable) == variables.get('__GO_EXE'):
                return variables.get('__GO_' + match[2], match[0])
            return match[0]
        line = re.sub(r'\$\((/[^\s()]+/go)\s+env\s+(GOPATH|GOROOT)\)', go_value, line)
        if line.lstrip().startswith('Environment='):
            try:
                bindings = shlex.split(line.split('=', 1)[1])
            except ValueError:
                if strict: unknown.append(unresolved(reason, 'unit environment unresolved: ' + line))
                continue
            for binding in bindings:
                key, sep, value = binding.partition('=')
                if not sep:
                    continue
                for _ in range(4):
                    value = re.sub(r'\$(?:\{([A-Za-z_][A-Za-z_0-9]*)\}|([A-Za-z_][A-Za-z_0-9]*))',
                                   lambda m: variables.get(m[1] or m[2], m[0]), value)
                variables[key] = value
                for path in (value.split(':') if key == 'PATH' else [value]):
                    path = path.replace('%h/', home.rstrip('/') + '/')
                    if re.search(r'[$`*?\[]', path):
                        if strict and (re.search(r'[$`]', path) or path.startswith('/') or ENV_NAME.fullmatch(key)):
                            unknown.append(unresolved(reason, line, [path]))
                    else:
                        anchor(anchors, path, reason)
            continue
        def substitute(match):
            key = match[1] or match[2]
            value = variables.get(key, match[0])
            # Keep dynamic scratch values symbolic outside a path operand.
            # Textual insertion into backticks would invent nested shell code.
            if ('$' in value or '`' in value) and not ENV_NAME.fullmatch(key):
                if not (line[match.end():].startswith('/') or
                        re.search(r'(?:source|\.|cd|exec)\s+[\x22\x27]?$', line[:match.start()])):
                    return match[0]
            return value
        line = re.sub(r'\$(?:\{([A-Za-z_][A-Za-z_0-9]*)\}|([A-Za-z_][A-Za-z_0-9]*))', substitute, line)
        try:
            parts = tokens(line)
        except ValueError:
            if strict and ('/' in line or '$' in line):
                unknown.append(unresolved(reason, 'shell quoting unresolved: ' + line))
            continue
        for index, token in enumerate(parts):
            if token not in functions or (index and parts[index - 1] not in
                                         (';', '&&', '||', '|', '|&', '!', '{', '(', 'then', 'else', 'elif', 'do', 'if')):
                continue
            arguments = []
            for part in parts[index + 1:]:
                if part in (';', '&&', '||'):
                    break
                arguments.append(part)
            function_text = re.sub(r'\$(?:\{([1-9])\}|([1-9]))',
                                   lambda m: arguments[int(m[1] or m[2]) - 1]
                                   if int(m[1] or m[2]) <= len(arguments) else '', functions[token])
            unknown.extend(text_anchors(anchors, function_text, home, reason, variables, strict,
                                        _depth + 1, functions))
        dynamic = False
        dynamic_values = []
        display_assignment = bool(re.match(r'\s*(?:export\s+)?(?:PS1|PS2)=', line))
        for index, token in enumerate(parts):
            assignment = re.match(r'^([A-Za-z_][A-Za-z_0-9]*)=(.*)$', token, re.S)
            value = assignment[2] if assignment else token
            path_operand = index > 0 and parts[index - 1] in ('source', '.', 'cd', 'exec')
            if path_operand and ('$' in value or
                                 (parts[index - 1] != 'exec' and not value.startswith(('/', '-'))) or
                                 value.startswith(('./', '../'))):
                dynamic = True
                dynamic_values.append(value)
            if assignment:
                key = assignment[1]
                if key == 'PROMPT_COMMAND' and value.strip():
                    unknown.extend(text_anchors(anchors, value, home, reason, variables, strict,
                                                _depth + 1, functions))
                if '$(' in value or '`' in value or '$' in value:
                    variables[key] = value
                    dynamic |= bool(ENV_NAME.fullmatch(key))
                    if ENV_NAME.fullmatch(key):
                        dynamic_values.extend(value.split(':') if key == 'PATH' else [value])
                else:
                    variables[key] = value
            if not assignment and re.search(r'(?:\$\(.*\)|`[^`]*`)/', value):
                dynamic = True
                dynamic_values.append(value)
            for part in (value.split(':') if assignment and assignment[1] == 'PATH' else [value]):
                part = part.removeprefix('-')  # systemd EnvironmentFile=-/path
                if part.startswith('/'):
                    if '$' in part or '`' in part:
                        if re.match(r'^/(proc|sys|dev|run)/', part):
                            continue
                        dynamic = True
                        dynamic_values.append(part)
                        continue
                    if re.search(r'[*?\[]', part):
                        dynamic = True
                        dynamic_values.append(part)
                        matched = False
                        for expanded in glob.iglob(part):
                            checkpoint()
                            matched = True
                            anchor(anchors, expanded, reason)
                        # No current match is not an unknown consumer; retain
                        # the fixed prefix too, in case it is inside a checkout.
                        part = re.split(r'[*?\[]', part, 1)[0]
                    anchor(anchors, part, reason)
        if strict:
            path_line = re.sub(r'/(?:proc|sys|dev|run)/[^\s\x22\x27;]*', '', line)
            unresolved_names = re.findall(r'\$\{?([A-Za-z_][A-Za-z_0-9]*)', path_line)
            relevant = [key for key in unresolved_names if ENV_NAME.fullmatch(key) or
                        re.search(r'\$\{?' + re.escape(key) + r'\}?/', path_line)]
            if (relevant or dynamic) and not display_assignment:
                # Each dynamic operand has its own bound: a literal operand on
                # the same line must never constrain an unrelated $variable.
                for value in dynamic_values or ['']:
                    if re.search(r'[$`*?\[]', value) or not value.startswith('/'):
                        unknown.append(unresolved(reason, line, [value]))
    return unique_unknown(unknown)


def config_sources():
    homes = {p.pw_dir for p in pwd.getpwall() if p.pw_dir.startswith('/') and os.path.isdir(p.pw_dir)}
    files = set()
    for home in homes:
        files.update((str(Path(home, name)), home) for name in SHELL_NAMES if Path(home, name).exists())
        for root in (Path(home, '.config/fish'), Path(home, '.config/systemd/user')):
            if root.exists():
                files.update((str(p), home) for p in root.rglob('*') if p.is_file())
    for root in ('/etc/profile.d', '/etc/systemd/system', '/usr/lib/systemd/system', '/lib/systemd/system',
                 '/run/systemd/system', '/etc/cron.d', '/var/spool/cron'):
        if os.path.isdir(root):
            def failed(exc):
                raise exc
            for parent, _, names in os.walk(root, onerror=failed):
                files.update((os.path.join(parent, n), '/root') for n in names)
    files.update((p, '/root') for p in ('/etc/profile', '/etc/bash.bashrc', '/etc/bashrc', '/etc/zsh/zshenv',
                                      '/etc/zsh/zprofile', '/etc/zsh/zshrc', '/etc/crontab', '/etc/environment') if os.path.exists(p))
    return files


def environment_references(anchors, text, home, reason, environments=None, metadata=None, strict=True):
    """Substitute all observed values; no arbitrary choice between processes."""
    pools = {key: set(values) for key, values in (environments or {}).items()}
    for key, value in os.environ.items():
        pools.setdefault(key, set()).add(value)
    for key, values in (metadata or {}).items():
        pools[key] = set(values)
    names = sorted(set(re.findall(r'\$(?:\{)?([A-Za-z_][A-Za-z_0-9]*)', text)) - {'HOME'})
    names = [key for key in names if pools.get(key)]
    records = []
    contexts = getattr(environments, 'contexts', None)
    if contexts is not None:
        # Real environments are correlated records, not a Cartesian product.
        # Missing keys can use the actual worker environment or a unique
        # observed value; otherwise leave them symbolic for scoped protection.
        variants = []
        for context in [dict(os.environ), *contexts]:
            if names and not any(key in context or key in (metadata or {}) for key in names):
                continue
            merged = dict(os.environ, **context)
            for key in names:
                if key in (metadata or {}):
                    continue
                if key not in merged and len(pools[key]) == 1:
                    merged[key] = next(iter(pools[key]))
            meta_names = [key for key in names if key in (metadata or {})]
            for meta_values in itertools.product(*(sorted(pools[key]) for key in meta_names)):
                selected = dict(merged, **dict(zip(meta_names, meta_values)))
                variants.append(tuple(selected.get(key) for key in names))
        variants = sorted(set(variants), key=repr)
        if not variants and not names:
            variants = [()]
    else:
        variants = itertools.product(*(sorted(pools[key]) for key in names))
    for values in variants:
        checkpoint()
        variables = {key: value for key, value in zip(names, values) if value is not None}
        variables.update({key: next(iter(values)) for key, values in pools.items()
                          if key.startswith('__GO_') and len(values) == 1})
        records.extend(text_anchors(anchors, text, home, reason, variables, strict))
    return unique_unknown(records)


def read_json_state(path):
    """Read one current regular JSON file; never call script decoder hooks."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size > CONFIG_TEXT_LIMIT:
            raise ValueError('JSON state is not bounded regular text')
        data = bytearray()
        while len(data) <= CONFIG_TEXT_LIMIT:
            checkpoint()
            block = os.read(fd, min(65536, CONFIG_TEXT_LIMIT + 1 - len(data)))
            if not block: break
            data.extend(block)
        def identity(info):
            return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns
        if len(data) > CONFIG_TEXT_LIMIT or identity(before) != identity(os.fstat(fd)) or identity(before) != identity(os.stat(path)):
            raise ValueError('JSON state changed while reading')
        return json.loads(data.decode('utf-8'))
    finally:
        os.close(fd)


def python_anchors(anchors, text, home, reason, environments=None, metadata=None, _depth=0):
    """Read declared Python scripts as syntax, without importing or executing them.

    Data loops are not shell path expressions. Track literal path construction,
    bounded glob iterations and observed environment values; an unbounded file
    operand still falls back to its BB project, just like an unknown shell path.
    """
    if _depth > 8:
        return [unresolved(reason, 'Python expansion limit')]
    try:
        module = ast.parse(text)
    except SyntaxError:
        text_anchors(anchors, text, home, reason, strict=False)
        return [unresolved(reason, 'Python syntax unresolved')]
    pools = {key: set(values) for key, values in (environments or {}).items()}
    for key, value in os.environ.items():
        pools.setdefault(key, set()).add(value)
    for key, values in (metadata or {}).items():
        pools[key] = set(values)
    pools['HOME'] = {home}
    if any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and
           isinstance(node.func.value, ast.Name) and node.func.value.id == 'os' and node.func.attr == 'chdir'
           for node in ast.walk(module)):
        pools.setdefault('PWD', set()).add('$PWD')  # May differ from its launch cwd.
    unknown = []
    mutable_globals = {key for node in ast.walk(module) if isinstance(node, (ast.Global, ast.Nonlocal)) for key in node.names}
    definitions, visited_functions, bounded_callbacks = [], set(), set()
    call_depth = 0
    container_names, container_tainted = set(), False
    mutators = {'append', 'extend', 'insert', 'pop', 'remove', 'clear', 'sort', 'reverse', 'add', 'update',
                'discard', 'setdefault', 'popitem', 'difference_update', 'intersection_update', 'symmetric_difference_update', '__setitem__', '__delitem__'}
    def bindings_from(target, value):
        if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)) and len(target.elts) == len(value.elts):
            return [pair for left, right in zip(target.elts, value.elts) for pair in bindings_from(left, right)]
        return [(target, value)]
    assignments = [pair for node in ast.walk(module) if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
                   for target in (node.targets if isinstance(node, ast.Assign) else [node.target]) for pair in bindings_from(target, node.value)]
    for _ in range(len(assignments) + 1):
        before = len(container_names)
        for target, value in assignments:
            container = isinstance(value, (ast.List, ast.Tuple, ast.Set, ast.Dict, ast.ListComp, ast.SetComp, ast.DictComp)) or (
                isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id in ('list', 'set', 'dict', 'tuple', 'sorted'))
            if container or isinstance(value, ast.Name) and value.id in container_names:
                container_names.update(node.id for node in ast.walk(target) if isinstance(node, ast.Name))
        if before == len(container_names): break

    def mutation(node):
        return any(isinstance(part, ast.Subscript) and isinstance(part.ctx, (ast.Store, ast.Del)) or
                   isinstance(part, ast.Attribute) and part.attr in mutators or isinstance(part, ast.AugAssign)
                   for part in ast.walk(node))
    # Alias/callback order is not evaluated. Once a mutator escapes or a
    # container can change anywhere, no old finite container values may prove
    # a path bound, including earlier definitions invoked by a later callback.
    container_tainted = mutation(module)
    constructors = set()
    path_classes = {'Path', 'PurePath', 'PosixPath', 'PurePosixPath'}
    shadowed = {node.id for node in ast.walk(module) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    shadowed.update(node.name for node in ast.walk(module) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    shadowed.update(node.arg for node in ast.walk(module) if isinstance(node, ast.arg))
    assigned_attributes = {ast.unparse(node) for node in ast.walk(module) if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)}
    imports = {}
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            for alias in node.names: imports[alias.asname or alias.name.split('.')[0]] = alias.name
        if isinstance(node, ast.ImportFrom):
            for alias in node.names: imports[alias.asname or alias.name] = (node.module or '') + '.' + alias.name
        if isinstance(node, ast.ImportFrom) and node.module == 'pathlib':
            for alias in node.names:
                constructors.update(path_classes if alias.name == '*' else {alias.asname or alias.name} if alias.name in path_classes else set())
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == 'pathlib': constructors.update((alias.asname or alias.name) + '.' + name for name in path_classes)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            anchor(anchors, node.value, reason)
            if '/' in node.value:
                text_anchors(anchors, node.value, home, reason, strict=False)

    path_methods = {'open', 'read_text', 'read_bytes', 'write_text', 'write_bytes', 'exists', 'is_file', 'is_dir',
                    'stat', 'lstat', 'glob', 'rglob', 'iterdir', 'mkdir', 'unlink', 'rmdir', 'touch', 'readlink',
                    'rename', 'replace', 'chmod', 'chown', 'symlink_to', 'hardlink_to'}
    file_calls = {'open', 'builtins.open', 'io.open', 'os.open', 'os.stat', 'os.lstat', 'os.scandir', 'os.listdir', 'os.unlink', 'os.chdir',
                  'os.remove', 'os.mkdir', 'os.makedirs', 'os.path.exists', 'os.path.isfile', 'os.path.isdir',
                  'os.rename', 'os.replace', 'shutil.copy', 'shutil.copy2', 'shutil.copyfile', 'shutil.copytree',
                  'shutil.move', 'shutil.rmtree'}
    destinations = {'os.rename', 'os.replace', 'shutil.copy', 'shutil.copy2', 'shutil.copyfile', 'shutil.copytree', 'shutil.move'}
    file_keywords = {'open':{'file'}, 'builtins.open':{'file'}, 'io.open':{'file'}, 'os.makedirs':{'name'}}
    path_flags = {'-C', '--cwd', '--chdir', '--directory', '--git-dir', '--work-tree', '--environment', '--env-path',
                  '--config', '--file', '--path', '--root', '--script', '--script-file', '--prompt-file', '--state-dir', '--output', '--output-dir'}

    def bounded(items):
        result = set()
        for item in items:
            result.add(item)
            if len(result) > 256:
                return {'$python'}  # No partial set may narrow an unknown operand.
        return result

    def name(node):
        return ast.unparse(node) if isinstance(node, (ast.Name, ast.Attribute)) else ''

    def joined(left, right):
        # Path(base) / absolute drops base. A wholly unknown RHS therefore
        # has both a bounded relative case and an unbounded absolute case.
        return bounded(value for a in left for b in right
                       for value in ([os.path.join(a, b), '$python'] if b.startswith('$') else [os.path.join(a, b)]))

    def canonical_call(node):
        called = name(node)
        root = called.split('.')[0]
        return imports.get(root, root) + called[len(root):]

    json_objects, argv_objects, json_cache, file_objects = {}, {}, {}, {}
    written_json_paths, unknown_json_write = set(), False
    json_keys_changed, json_all_changed = set(), False
    json_names = set()
    function_nodes = {node.name: node for node in ast.walk(module) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def json_expression(node):
        return (isinstance(node, ast.Name) and node.id in json_names or
                isinstance(node, ast.Subscript) and json_expression(node.value) or
                isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict, ast.Starred, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp, ast.comprehension)) and
                    any(json_expression(child) for child in ast.iter_child_nodes(node)) or
                isinstance(node, ast.Call) and (canonical_call(node.func) in ('json.load', 'json.loads') or
                    isinstance(node.func, ast.Attribute) and node.func.attr == 'get' and json_expression(node.func.value)))

    for _ in range(len(assignments) + 1):
        before = len(json_names)
        for target, value in assignments:
            if json_expression(value):
                json_names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        for call in ast.walk(module):
            if not isinstance(call, ast.Call) or name(call.func) not in function_nodes: continue
            function = function_nodes[name(call.func)]
            parameters = [*function.args.posonlyargs, *function.args.args]
            for index, argument in enumerate(call.args):
                if json_expression(argument):
                    if isinstance(argument, ast.Starred):
                        json_names.update(arg.arg for arg in [*parameters, *function.args.kwonlyargs])
                        if function.args.vararg: json_names.add(function.args.vararg.arg)
                    elif index < len(parameters): json_names.add(parameters[index].arg)
                    elif function.args.vararg: json_names.add(function.args.vararg.arg)
            for keyword in call.keywords:
                if keyword.arg and json_expression(keyword.value): json_names.add(keyword.arg)
        if len(json_names) == before: break
    parent_nodes = {id(child): node for node in ast.walk(module) for child in ast.iter_child_nodes(node)}
    file_names = {node.id for target, value in assignments if isinstance(value, ast.Call) and canonical_call(value.func) in ('open', 'builtins.open', 'io.open')
                  for node in ast.walk(target) if isinstance(node, ast.Name)}
    file_names.update(node.id for part in ast.walk(module) if isinstance(part, ast.withitem) and part.optional_vars is not None and
                      isinstance(part.context_expr, ast.Call) and canonical_call(part.context_expr.func) in ('open', 'builtins.open', 'io.open')
                      for node in ast.walk(part.optional_vars) if isinstance(node, ast.Name))
    tainted_files = set()
    for node in ast.walk(module):
        if not isinstance(node, ast.Name) or not isinstance(node.ctx, ast.Load) or node.id not in file_names: continue
        parent = parent_nodes.get(id(node))
        if isinstance(parent, ast.keyword) and parent.arg == 'fp': parent = parent_nodes.get(id(parent))
        safe = isinstance(parent, ast.Call) and canonical_call(parent.func) == 'json.load'
        if isinstance(parent, ast.Attribute) and parent.attr == 'read':
            call = parent_nodes.get(id(parent));outer = parent_nodes.get(id(call))
            safe = (isinstance(call, ast.Call) and not call.args and not call.keywords and
                    isinstance(outer, ast.Call) and canonical_call(outer.func) == 'json.loads')
        if not safe: tainted_files.add(node.id)
    known_functions = {node.name for node in ast.walk(module) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    file_writers = {'os.write', 'os.pwrite', 'os.ftruncate', 'os.truncate', 'os.unlink', 'os.remove', 'os.rename', 'os.replace', 'shutil.copy', 'shutil.copy2',
                    'shutil.copyfile', 'shutil.copytree', 'shutil.move', 'shutil.rmtree'}
    writer_methods = {'write_text', 'write_bytes', 'touch', 'unlink', 'rename', 'replace', 'symlink_to', 'hardlink_to'}

    def is_file_write(call):
        called = canonical_call(call.func)
        if called == 'os.open':
            flags = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == 'flags'), None)
            return not (isinstance(flags, ast.Constant) and flags.value == 0 or flags is not None and canonical_call(flags) == 'os.O_RDONLY')
        if called in ('open', 'builtins.open', 'io.open'):
            mode = call.args[1] if len(call.args) > 1 else next((k.value for k in call.keywords if k.arg == 'mode'), None)
            return mode is not None and not (isinstance(mode, ast.Constant) and mode.value in ('r', 'rt', 'rb'))
        if isinstance(call.func, ast.Attribute) and call.func.attr == 'replace':
            if not call.args and not any(k.arg == 'target' for k in call.keywords): return False
            if isinstance(call.func.value, ast.JoinedStr) or isinstance(call.func.value, ast.Constant) and isinstance(call.func.value.value, str): return False
        return called in file_writers or isinstance(call.func, ast.Attribute) and call.func.attr in writer_methods

    readonly_calls = {'len', 'str', 'bool', 'int', 'float', 'isinstance', 'json.dumps', 'json.dump', 'json.load', 'json.loads',
                      'open', 'builtins.open', 'io.open', 'subprocess.run', 'subprocess.call', 'subprocess.check_call',
                      'subprocess.check_output', 'subprocess.Popen', *file_calls, *constructors,
                      *('pathlib.' + cls for cls in path_classes)}
    reader_names = {key for key, value in imports.items() if value in ('json', 'pathlib', 'io', 'builtins') or value.startswith('pathlib.')}
    for _ in range(len(assignments) + 1):
        before = len(reader_names)
        for target, value in assignments:
            if isinstance(value, ast.Name) and value.id in reader_names:
                reader_names.update(n.id for n in ast.walk(target) if isinstance(n, ast.Name))
        if len(reader_names) == before: break
    # Mutation order, aliases and callbacks are not executed. Known dictionary
    # key writes taint that key throughout; unknown writes/escapes taint all.
    for part in ast.walk(module):
        if isinstance(part, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and any(isinstance(c, ast.Call) and is_file_write(c) for c in ast.walk(part)):
            json_all_changed = True  # Callback order may change the file before a load.
        if isinstance(part, ast.Subscript) and isinstance(part.ctx, (ast.Store, ast.Del)):
            if isinstance(part.slice, ast.Constant) and isinstance(part.slice.value, (str, int)):
                json_keys_changed.add(part.slice.value)
            else: json_all_changed = True
        if isinstance(part, ast.AugAssign): json_all_changed = True
        if isinstance(part, ast.Attribute) and isinstance(part.ctx, ast.Store) and (part.attr in ('load', 'loads', 'read_text', 'read_bytes', 'open') or part.attr.startswith('__')):
            json_all_changed = True
        if isinstance(part, ast.Call):
            called = canonical_call(part.func)
            if called in ('exec', 'eval', 'setattr', 'delattr', 'getattr', 'vars', 'compile', '__import__', 'globals', 'locals'):
                json_all_changed = True
            if any(isinstance(arg, ast.Name) and arg.id in reader_names for arg in [*part.args, *(k.value for k in part.keywords)]):
                json_all_changed = True
            if called not in readonly_calls and name(part.func) not in known_functions and not isinstance(part.func, ast.Attribute):
                if any(json_expression(arg) for arg in [*part.args, *(k.value for k in part.keywords)]):
                    json_all_changed = True
        if isinstance(part, ast.Attribute) and part.attr in mutators:
            parent = parent_nodes.get(id(part))
            if not isinstance(parent, ast.Call) or parent.func is not part:
                json_all_changed = True
            elif part.attr == 'update':
                for arg in parent.args:
                    if isinstance(arg, ast.Dict) and all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in arg.keys):
                        json_keys_changed.update(k.value for k in arg.keys)
                    else: json_all_changed = True
                for keyword in parent.keywords:
                    if keyword.arg is None: json_all_changed = True
                    else: json_keys_changed.add(keyword.arg)
            elif part.attr in ('pop', 'setdefault', '__setitem__', '__delitem__') and parent.args and isinstance(parent.args[0], ast.Constant):
                json_keys_changed.add(parent.args[0].value)
            elif isinstance(part.value, ast.Subscript) and isinstance(part.value.slice, ast.Constant):
                json_keys_changed.add(part.value.slice.value)
            else: json_all_changed = True
        if isinstance(part, ast.Call) and isinstance(part.func, ast.Attribute) and json_expression(part.func.value):
            if part.func.attr not in ('get', *mutators): json_all_changed = True
        if isinstance(part, ast.Call) and isinstance(part.func, ast.Attribute) and canonical_call(part.func) not in readonly_calls:
            if part.func.attr not in mutators and any(json_expression(arg) for arg in [*part.args, *(k.value for k in part.keywords)]):
                json_all_changed = True
    if json_keys_changed.intersection(('load', 'loads', 'read_text', 'read_bytes', 'open')):
        json_all_changed = True

    def json_value(value):
        if isinstance(value, str): return {value}
        if isinstance(value, (dict, list)):
            token = f'$json:{len(json_objects)}'
            json_objects[token] = value
            return {token}
        return {'$python'}

    def selected_json(node, key_node, env, default=None, depth=0):
        if json_all_changed: return {'$python'}
        if isinstance(key_node, ast.Constant) and isinstance(key_node.value, (str, int)):
            keys = {key_node.value}
        else: keys = values(key_node, env, depth + 1)
        result = set()
        for token in values(node, env, depth + 1):
            if token not in json_objects:
                result.add('$python');continue
            obj = json_objects.get(token)
            for key in keys:
                if key in json_keys_changed or isinstance(key, str) and key.startswith('$'):
                    result.add('$python');continue
                try: result.update(json_value(obj[key]))
                except (KeyError, IndexError, TypeError):
                    result.update(values(default, env, depth + 1) if default is not None else {'$python'})
        return bounded(result) or {'$python'}

    def open_paths(call, env, depth):
        called = canonical_call(call.func)
        if called not in ('open', 'builtins.open', 'io.open') or name(call.func).split('.')[0] in shadowed:
            return {'$python'}
        if len(call.args) > 2 or any(k.arg not in ('file', 'mode', 'encoding') for k in call.keywords): return {'$python'}
        modes = values(call.args[1], env, depth + 1) if len(call.args) > 1 else {'r'}
        for kw in call.keywords:
            if kw.arg == 'mode': modes = values(kw.value, env, depth + 1)
            if kw.arg == 'encoding' and values(kw.value, env, depth + 1) - {'utf-8', 'utf8'}: return {'$python'}
        if not modes <= {'r', 'rt', 'rb'}: return {'$python'}
        path_node = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == 'file'), None)
        return values(path_node, env, depth + 1) if path_node is not None else {'$python'}

    def file_paths(node, env, depth):
        if isinstance(node, ast.Call) and canonical_call(node.func) in ('open', 'builtins.open', 'io.open'):
            return open_paths(node, env, depth)
        return bounded(path for token in values(node, env, depth + 1) for path in file_objects.get(token, {'$python'}))

    def load_json(call, env, depth):
        root = name(call.func).split('.')[0]
        if imports.get(root) not in ('json', 'json.load', 'json.loads') or root in shadowed or any(name(call.func) == p or name(call.func).startswith(p + '.') for p in assigned_attributes):
            return {'$python'}
        called = canonical_call(call.func)
        operand = None
        if len(call.args) == 1 and not call.keywords:
            operand = call.args[0]
        elif not call.args and len(call.keywords) == 1 and call.keywords[0].arg == ('s' if called == 'json.loads' else 'fp'):
            operand = call.keywords[0].value
        if operand is None: return {'$python'}  # Decoder hooks can change paths.
        paths = None
        if called == 'json.loads' and isinstance(operand, ast.Call) and isinstance(operand.func, ast.Attribute) and operand.func.attr in ('read_text', 'read_bytes'):
            if operand.args or any(k.arg != 'encoding' or values(k.value, env) - {'utf-8', 'utf8'} for k in operand.keywords): return {'$python'}
            paths = values(operand.func.value, env, depth + 1)
        elif called == 'json.loads' and isinstance(operand, ast.Call) and isinstance(operand.func, ast.Attribute) and operand.func.attr == 'read' and not operand.args and not operand.keywords:
            paths = file_paths(operand.func.value, env, depth)
        elif called == 'json.load':
            paths = file_paths(operand, env, depth)
        if paths is None: return {'$python'}
        result = set()
        for path in paths:
            if not path.startswith('/') or re.search(r'[$`*?\[]', path):
                result.add('$python');continue
            if unknown_json_write or any(inside(canonical(path), modified) or inside(modified, canonical(path)) for modified in written_json_paths):
                result.add('$python');continue
            if path not in json_cache:
                try: json_cache[path] = read_json_state(path)
                except (OSError, ValueError, RecursionError): json_cache[path] = None
            result.update(json_value(json_cache[path]))
        return bounded(result) or {'$python'}

    def argv_sequence(node, env):
        if isinstance(node, (ast.List, ast.Tuple)):
            sequence = []
            for item in node.elts:
                if isinstance(item, ast.Starred):
                    expanded = argv_sequence(item.value, env)
                    if expanded is None: return None
                    sequence.extend(expanded)
                else: sequence.append(values(item, env))
            return sequence
        tokens = values(node, env)
        variants = [argv_objects[t] for t in tokens if t in argv_objects]
        if len(variants) != len(tokens) or not variants or len({len(v) for v in variants}) != 1: return None
        return [set().union(*(v[i] for v in variants)) for i in range(len(variants[0]))]

    def values(node, env, depth=0):
        if depth > 12:
            return {'$python'}
        if isinstance(node, ast.Constant):
            return {node.value} if isinstance(node.value, str) else {''} if node.value is None else {'$python'}
        if isinstance(node, ast.Name):
            result = env.get(node.id, {'$' + node.id})
            return result | {'$' + node.id} if node.id in mutable_globals or node.id in tainted_files or container_tainted and node.id in container_names else result
        if isinstance(node, ast.Attribute) and node.attr in ('parent', 'name', 'stem', 'suffix'):
            paths = values(node.value, env, depth + 1)
            if node.attr == 'parent': return {os.path.dirname(x) or '$python' for x in paths}
            if node.attr == 'name': return {os.path.basename(x) for x in paths}
            return {os.path.splitext(os.path.basename(x))[0 if node.attr == 'stem' else 1] for x in paths}
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return set().union(*(values(x, env, depth + 1) for x in node.elts))
        if isinstance(node, ast.JoinedStr):
            result = {''}
            for part in node.values:
                result = bounded(a + b for a in result for b in values(part.value if isinstance(part, ast.FormattedValue) else part, env, depth + 1))
            return result
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Add)):
            left, right = values(node.left, env, depth + 1), values(node.right, env, depth + 1)
            return joined(left, right) if isinstance(node.op, ast.Div) else bounded(a + b for a in left for b in right)
        if isinstance(node, ast.BoolOp):
            return set().union(*(values(x, env, depth + 1) for x in node.values)) - {''}
        if isinstance(node, ast.IfExp):
            return values(node.body, env, depth + 1) | values(node.orelse, env, depth + 1)
        if isinstance(node, ast.Subscript) and name(node.value) == 'os.environ':
            keys = values(node.slice, env, depth + 1)
            return bounded(value for key in keys for value in pools.get(key, {'$' + key}))
        if isinstance(node, ast.Subscript):
            return selected_json(node.value, node.slice, env, depth=depth)
        if isinstance(node, ast.Call):
            called = name(node.func)
            if canonical_call(node.func) in ('json.load', 'json.loads'):
                return load_json(node, env, depth)
            if canonical_call(node.func) in ('open', 'builtins.open', 'io.open'):
                token = f'$file:{len(file_objects)}'
                file_objects[token] = open_paths(node, env, depth)
                return {token}
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'get' and node.args and any(t in json_objects for t in values(node.func.value, env, depth + 1)):
                return selected_json(node.func.value, node.args[0], env, node.args[1] if len(node.args) > 1 else None, depth)
            root_name = called.split('.')[0]
            wrappers = {'sorted', 'list', 'tuple', 'set', 'iter'}
            modules = {'os':'os', 'shutil':'shutil', 'datetime':'datetime', 'concurrent':'concurrent',
                       'ThreadPoolExecutor':'concurrent.futures.ThreadPoolExecutor', 'ProcessPoolExecutor':'concurrent.futures.ProcessPoolExecutor'}
            special = any(called == item or called.startswith(item + '.') for item in constructors) or root_name in modules or called in wrappers
            foreign = root_name in modules and not (imports.get(root_name) == modules[root_name] or imports.get(root_name, '').startswith(modules[root_name] + '.'))
            if (special and root_name in shadowed) or foreign or (called in wrappers and called in imports) or any(called == key or called.startswith(key + '.') for key in assigned_attributes):
                return {'$python'}
            if called in ('datetime.datetime.now', 'datetime.datetime.utcnow', 'datetime.datetime.fromtimestamp',
                          'datetime.datetime.fromisoformat', 'datetime.now', 'datetime.utcnow', 'datetime.fromtimestamp', 'datetime.fromisoformat'):
                return {'$datetime'}
            if called in ('concurrent.futures.ThreadPoolExecutor', 'concurrent.futures.ProcessPoolExecutor', 'ThreadPoolExecutor', 'ProcessPoolExecutor'):
                return {'$executor'}
            if called in ('os.getenv', 'os.environ.get') and node.args:
                result = set()
                for key in values(node.args[0], env, depth + 1):
                    result.update(pools.get(key, values(node.args[1], env, depth + 1) if len(node.args) > 1 else {''}))
                return bounded(result)
            if called == 'shutil.which' and node.args:
                return bounded(found for item in values(node.args[0], env, depth + 1)
                               for path in pools.get('PATH', {os.defpath})
                               if (found := shutil.which(item, path=path))) or {'$python'}
            if called in constructors or called == 'os.path.join':
                result = values(node.args[0], env, depth + 1) if node.args else pools.get('PWD', {'$PWD'})
                for arg in node.args[1:]:
                    result = joined(result, values(arg, env, depth + 1))
                return result
            if called in {p + '.home' for p in constructors}:
                return pools['HOME']
            if called == 'os.getcwd' or called in {p + '.cwd' for p in constructors}:
                return pools.get('PWD', {'$PWD'})
            if called in ('sorted', 'list', 'tuple', 'set', 'iter') and node.args:
                return values(node.args[0], env, depth + 1)
            if called == 'str' and called not in shadowed and called not in imports and len(node.args) == 1 and not node.keywords:
                return values(node.args[0], env, depth + 1)
            if isinstance(node.func, ast.Attribute):
                root = values(node.func.value, env, depth + 1)
                if node.func.attr == 'strftime' and root == {'$datetime'} and node.args:
                    formats = values(node.args[0], env, depth + 1)
                    if all(re.fullmatch(r'(?:[^%/]|%[YymdHMSfjz%])*', x) for x in formats):
                        return {x if '%' not in x else '*' for x in formats}
                if node.func.attr in ('glob', 'rglob', 'iterdir'):
                    pattern = values(node.args[0], env, depth + 1) if node.args else {'*'}
                    return joined(root, pattern if node.func.attr != 'rglob' else {'**/' + x for x in pattern})
                if node.func.attr == 'expanduser':
                    return bounded(directory + x[1:] if x == '~' or x.startswith('~/') else '$python' if x.startswith('~') else x
                                   for x in root for directory in pools['HOME'])
                if node.func.attr in ('resolve', 'absolute'):
                    return root
                if node.func.attr == 'with_name' and node.args:
                    return bounded(os.path.join(os.path.dirname(x), y) for x in root for y in values(node.args[0], env, depth + 1))
                if node.func.attr == 'with_suffix' and node.args:
                    return bounded(os.path.splitext(x)[0] + y for x in root for y in values(node.args[0], env, depth + 1))
        return {'$python'}

    def reserve(node, operands, cwd=None):
        expression = ast.get_source_segment(text, node) or ast.unparse(node)
        for path in operands:
            if path and not path.startswith(('/', '$')) and (cwd is not None or pools.get('PWD')):
                unknown.extend(reserve_relative(node, path, cwd))
                continue
            anchor(anchors, path, reason)
            if not path.startswith('/') or re.search(r'[$`*?\[]', path):
                unknown.append(unresolved(reason, expression, [path]))

    def reserve_relative(node, path, cwd=None):
        records = []
        for directory in (cwd if cwd is not None else pools['PWD']):
            value = os.path.join(directory, path)
            anchor(anchors, value, reason)
            if re.search(r'[$`*?\[]', value):
                records.append(unresolved(reason, ast.get_source_segment(text, node) or ast.unparse(node), [value]))
        return records

    def invoke(entry, arguments, keywords, caller_env):
        nonlocal call_depth
        function, defining_env, defining_functions = entry
        if call_depth >= 8:
            unknown.append(unresolved(reason, 'Python function expansion limit: ' + function.name))
            return
        nested = dict(defining_env)
        parameters = [*function.args.posonlyargs, *function.args.args]
        for arg in (*parameters, *function.args.kwonlyargs): nested[arg.arg] = {'$' + arg.arg}
        for parameter, argument in zip(parameters, arguments or []): nested[parameter.arg] = argument
        if function.args.vararg:
            if arguments is None:
                nested[function.args.vararg.arg] = {'$python'}
            else:
                token = f'$argv:{len(argv_objects)}'
                argv_objects[token] = arguments[len(parameters):]
                nested[function.args.vararg.arg] = {token}
        for key, value in keywords.items(): nested[key] = value
        visited_functions.add(id(function))
        call_depth += 1
        try:
            statements(function.body, nested, {key: list(entries) for key, entries in defining_functions.items()})
        finally:
            call_depth -= 1

    def expressions(node, env, functions):
        nonlocal container_tainted, unknown_json_write
        container_tainted |= mutation(node)
        # Comprehension/lambda names shadow outer bindings. An unsupported
        # local binding may broaden to project scope, never borrow an unrelated
        # outer path and silently narrow the actual operand.
        env = dict(env)
        for child in ast.walk(node):
            if isinstance(child, ast.comprehension):
                for target in ast.walk(child.target):
                    if isinstance(target, ast.Name): env[target.id] = {'$' + target.id}
            if isinstance(child, ast.Lambda):
                for arg in (*child.args.posonlyargs, *child.args.args, *child.args.kwonlyargs): env[arg.arg] = {'$' + arg.arg}
        def python_code(value):
            for source in values(value, env):
                if source.startswith('$'):
                    unknown.append(unresolved(reason, ast.get_source_segment(text, value) or ast.unparse(value)))
                else:
                    for directory in pools['HOME']:
                        unknown.extend(python_anchors(anchors, source, directory, reason, pools, pools, _depth + 1))
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            called = name(call.func)
            if is_file_write(call):
                operands = list(call.args[:2]) if canonical_call(call.func) in file_writers else []
                if isinstance(call.func, ast.Attribute) and call.func.attr in writer_methods:
                    operands.append(call.func.value)
                    if call.func.attr in ('rename', 'replace', 'symlink_to', 'hardlink_to'):
                        operands.extend(call.args[:1])
                        operands.extend(k.value for k in call.keywords if k.arg == 'target')
                if canonical_call(call.func) in ('open', 'builtins.open', 'io.open', 'os.open'):
                    operands.extend(call.args[:1])
                    operands.extend(k.value for k in call.keywords if k.arg == 'file')
                if not operands: unknown_json_write = True
                for operand in operands:
                    for value in values(operand, env):
                        if not value.startswith('/') or re.search(r'[$`*?\[]', value): unknown_json_write = True
                        else: written_json_paths.add(canonical(value))
            root = called.split('.')[0]
            filesystem_call = imports.get(root, root) + called[len(root):]
            if filesystem_call not in file_calls and called in file_calls: filesystem_call = called
            for entry in functions.get(called, []):
                invoke(entry, argv_sequence(ast.Tuple(elts=call.args), env),
                       {arg.arg: values(arg.value, env) for arg in call.keywords if arg.arg}, env)
            mapped = called == 'map' and 'map' not in shadowed and 'map' not in imports or (
                isinstance(call.func, ast.Attribute) and call.func.attr == 'map' and values(call.func.value, env) == {'$executor'})
            if mapped and len(call.args) > 1 and isinstance(call.args[0], ast.Name):
                entries = functions.get(call.args[0].id, [])
                if entries:
                    bounded_callbacks.add(id(call.args[0]))
                    for entry in entries: invoke(entry, [values(arg, env) for arg in call.args[1:]], {}, env)
            if (called in ('exec', 'eval') or filesystem_call in ('builtins.exec', 'builtins.eval')) and call.args:
                python_code(call.args[0])
            if called in constructors:
                reserve(call, values(call, env))
            elif filesystem_call in file_calls:
                if call.args: reserve(call.args[0], values(call.args[0], env))
                if filesystem_call in destinations and len(call.args) > 1: reserve(call.args[1], values(call.args[1], env))
                roles = {'src', 'dst'} if filesystem_call in destinations else file_keywords.get(filesystem_call, {'path'})
                for keyword in call.keywords:
                    if keyword.arg in roles: reserve(keyword.value, values(keyword.value, env))
                    elif keyword.arg is None:
                        if isinstance(keyword.value, ast.Dict) and all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in keyword.value.keys):
                            for key, value in zip(keyword.value.keys, keyword.value.values):
                                if key.value in roles: reserve(value, values(value, env))
                                elif key.value in ('dir_fd', 'src_dir_fd', 'dst_dir_fd') and not (isinstance(value, ast.Constant) and value.value is None):
                                    unknown.append(unresolved(reason, ast.get_source_segment(text, call) or ast.unparse(call)))
                        else:
                            unknown.append(unresolved(reason, ast.get_source_segment(text, keyword.value) or ast.unparse(keyword.value)))
                    elif keyword.arg in ('dir_fd', 'src_dir_fd', 'dst_dir_fd') and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None):
                        unknown.append(unresolved(reason, ast.get_source_segment(text, call) or ast.unparse(call)))
                if filesystem_call == 'os.chdir':
                    operands = [call.args[0]] if call.args else [k.value for k in call.keywords if k.arg == 'path']
                    for operand in operands:
                        paths = values(operand, env)
                        pools['PWD'] = bounded(path if path.startswith(('/', '$')) else os.path.join(base, path)
                                               for path in paths for base in pools.get('PWD', {'$PWD'})) | pools.get('PWD', set())
            elif isinstance(call.func, ast.Attribute) and call.func.attr in path_methods:
                target_keywords = [k.value for k in call.keywords if k.arg == 'target']
                expanded_keywords = [k.value for k in call.keywords if k.arg is None]
                if call.func.attr == 'replace' and not call.args and not target_keywords and not expanded_keywords:
                    continue  # datetime.replace(tzinfo=...) is not Path.replace(target).
                if call.func.attr == 'replace' and (isinstance(call.func.value, ast.JoinedStr) or isinstance(call.func.value, ast.Constant) and isinstance(call.func.value.value, str)):
                    continue  # Literal strings cannot be Path instances.
                operands = values(call if call.func.attr in ('glob', 'rglob', 'iterdir') else call.func.value, env)
                reserve(call, operands)
                if call.func.attr in ('rename', 'replace', 'symlink_to', 'hardlink_to'):
                    if call.args: reserve(call.args[0], values(call.args[0], env))
                    for target in target_keywords: reserve(target, values(target, env))
                    for keyword in expanded_keywords:
                        if isinstance(keyword, ast.Dict) and all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in keyword.keys):
                            for key, value in zip(keyword.keys, keyword.values):
                                if key.value == 'target': reserve(value, values(value, env))
                        else:
                            unknown.append(unresolved(reason, ast.get_source_segment(text, keyword) or ast.unparse(keyword)))
            if called in ('os.environ.update', 'os.putenv'):
                unknown.append(unresolved(reason, ast.get_source_segment(text, call) or ast.unparse(call)))
            if filesystem_call in ('subprocess.run', 'subprocess.call', 'subprocess.check_call', 'subprocess.check_output', 'subprocess.Popen'):
                process_keywords = []
                for keyword in call.keywords:
                    if keyword.arg is not None:
                        process_keywords.append(keyword)
                    elif isinstance(keyword.value, ast.Dict) and all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in keyword.value.keys):
                        process_keywords.extend(ast.keyword(arg=key.value, value=value) for key, value in zip(keyword.value.keys, keyword.value.values))
                    else:
                        unknown.append(unresolved(reason, ast.get_source_segment(text, keyword.value) or ast.unparse(keyword.value)))
                cwd = None
                for keyword in process_keywords:
                    if keyword.arg == 'cwd' and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is None):
                        cwd = bounded(path if path.startswith(('/', '$')) else os.path.join(base, path)
                                      for path in values(keyword.value, env) for base in pools.get('PWD', {'$PWD'}))
                        reserve(keyword.value, cwd)
                    if keyword.arg == 'env':
                        unknown.append(unresolved(reason, 'subprocess environment mapping not evaluated'))
                        if isinstance(keyword.value, ast.Dict):
                            for key, value in zip(keyword.value.keys, keyword.value.values):
                                if not isinstance(key, ast.Constant) or not isinstance(key.value, str) or not (
                                    key.value in ('HOME', 'PATH', 'PYTHONPATH', 'ROOT', 'DIR', 'WORKTREE', 'CHECKOUT', 'CWD') or
                                    ENV_NAME.fullmatch(key.value) or key.value.endswith('_PATH')): continue
                                for path in values(value, env):
                                    for entry in (path.split(':') if isinstance(key, ast.Constant) and key.value == 'PATH' else [path]):
                                        if entry.startswith('/'):
                                            unknown.append(unresolved(reason, 'subprocess environment path', [entry.rstrip('/') + '/$python']))
                arg = call.args[0] if call.args else next((k.value for k in process_keywords if k.arg == 'args'), None)
                if arg is not None:
                    sequence = argv_sequence(arg, env)
                    executable = arg.elts[0] if isinstance(arg, (ast.List, ast.Tuple)) and arg.elts else arg
                    for value in (sequence[0] if sequence else values(executable, env)):
                        if value.startswith(('/', '$', '~')): reserve(executable, {value})
                    if not isinstance(arg, (ast.List, ast.Tuple)):
                        possible = values(arg, env)
                        if possible & path_flags:
                            reserve(arg, possible - path_flags, cwd)
                    for value in (set().union(*sequence) if sequence is not None else values(arg, env)):
                        if '/' in value and not re.search(r'\s|://', value): reserve(arg, {value}, cwd)
                    if sequence is not None:
                        if isinstance(arg, (ast.List, ast.Tuple)) and any(isinstance(item, ast.Starred) for item in arg.elts) and any(value.startswith('$') for items in sequence for value in items):
                            unknown.append(unresolved(reason, ast.get_source_segment(text, call) or ast.unparse(call)))
                        pending_path = False
                        for items in sequence:
                            if pending_path: reserve(arg, items, cwd)
                            pending_path = bool(items & path_flags)
                            for value in items:
                                flag, sep, path = value.partition('=')
                                if sep and flag in path_flags: reserve(arg, {path}, cwd)
                    elif isinstance(arg, (ast.List, ast.Tuple)) and any(isinstance(item, ast.Starred) for item in arg.elts):
                        unknown.append(unresolved(reason, ast.get_source_segment(text, call) or ast.unparse(call)))
                    if isinstance(arg, (ast.List, ast.Tuple)) and any(re.fullmatch(r'python(?:[23](?:\.\d+)*)?', os.path.basename(x)) for x in values(executable, env)):
                        for index, part in enumerate(arg.elts[1:], 1):
                            if values(part, env) == {'-c'} and index + 1 < len(arg.elts):
                                python_code(arg.elts[index + 1])
                            if values(part, env) == {'-'}:
                                for keyword in process_keywords:
                                    if keyword.arg == 'input': python_code(keyword.value)
                    for keyword in process_keywords:
                        if keyword.arg == 'shell' and isinstance(keyword.value, ast.Constant) and keyword.value.value is True:
                            for source in values(arg, env):
                                for directory in pools['HOME']:
                                    unknown.extend(environment_references(anchors, source, directory, reason, pools, pools))

    def assign(target, value, env):
        nonlocal container_tainted
        if isinstance(target, ast.Name):
            env[target.id] = value
        elif isinstance(target, (ast.Tuple, ast.List)):
            for part in target.elts: assign(part, value, env)
        elif isinstance(target, ast.Subscript) and name(target.value) == 'os.environ':
            for key in values(target.slice, env):
                pools.setdefault(key, set()).update(value)
        elif isinstance(target, ast.Subscript):
            container_tainted = True

    def merge(env, variants):
        for key in set().union(*(set(v) for v in variants)):
            env[key] = set().union(*(v.get(key, {'$' + key}) for v in variants))

    def loop_bindings(nodes, env, target=None):
        nonlocal container_tainted
        container_tainted |= any(mutation(node) for node in nodes)
        nested = dict(env)
        reset = {n.id for n in ast.walk(target) if isinstance(n, ast.Name)} if target is not None else set()
        for statement in nodes:
            for part in ast.walk(statement):
                if isinstance(part, ast.Name) and isinstance(part.ctx, ast.Store) and part.id not in reset:
                    nested[part.id] = nested.get(part.id, set()) | {'$' + part.id}
        return nested

    def statements(nodes, env, functions, bindings=None):
        bindings = dict(env) if bindings is None else bindings
        for node in nodes:
            checkpoint()
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if node.value is None: continue
                expressions(node.value, env, functions)
                for target in targets:
                    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(node.value, (ast.Tuple, ast.List)) and len(target.elts) == len(node.value.elts):
                        for part, value in zip(target.elts, node.value.elts): assign(part, values(value, env), env)
                    else: assign(target, values(node.value, env), env)
            elif isinstance(node, ast.AugAssign):
                expressions(node.value, env, functions)
                assign(node.target, {'$python'}, env)
            elif isinstance(node, (ast.For, ast.AsyncFor)):
                expressions(node.iter, env, functions)
                nested = loop_bindings(node.body, env, node.target); assign(node.target, values(node.iter, env), nested)
                statements(node.body + node.orelse, nested, functions, bindings)
                merge(env, [env.copy(), nested])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for value in [*node.decorator_list, *node.args.defaults, *(x for x in node.args.kw_defaults if x is not None)]:
                    expressions(value, env, functions)
                entry = (node, bindings, functions)
                functions.setdefault(node.name, []).append(entry)
                definitions.append(entry)
            elif isinstance(node, ast.If):
                expressions(node.test, env, functions)
                left, right = dict(env), dict(env)
                statements(node.body, left, functions, bindings); statements(node.orelse, right, functions, bindings); merge(env, [left, right])
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    expressions(item.context_expr, env, functions)
                    if item.optional_vars: assign(item.optional_vars, values(item.context_expr, env), env)
                statements(node.body, env, functions, bindings)
            elif isinstance(node, ast.Try):
                variants = []
                for body in [node.body + node.orelse, *(h.body for h in node.handlers)]:
                    nested = dict(env); statements(body, nested, functions, bindings); variants.append(nested)
                merge(env, variants); statements(node.finalbody, env, functions, bindings)
            elif isinstance(node, ast.While):
                nested = loop_bindings(node.body, env)
                expressions(node.test, nested, functions)
                statements(node.body + node.orelse, nested, functions, bindings); merge(env, [env.copy(), nested])
            else:
                expressions(node, env, functions)
            for key, value in env.items(): bindings[key] = bindings.get(key, set()) | value
    statements(module.body, {}, {})
    parents = {id(child): parent for parent in ast.walk(module) for child in ast.iter_child_nodes(parent)}
    escaped = {node.id for node in ast.walk(module) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
               and id(node) not in bounded_callbacks and not (
                   isinstance(parents.get(id(node)), ast.Call) and parents[id(node)].func is node)}
    # Unknown callback/alias invocations must not hide a function's file uses.
    for entry in list(definitions):
        if id(entry[0]) not in visited_functions or entry[0].name in escaped:
            invoke(entry, [], {}, {})
    return unique_unknown(unknown)


def name_config_sources():
    """Small configuration text, including directory symlinks and dotfiles."""
    homes = {p.pw_dir for p in pwd.getpwall() if p.pw_dir.startswith('/') and os.path.isdir(p.pw_dir)}
    pending, seen = ['/etc'], set()
    for home in homes:
        with os.scandir(home) as entries:
            for entry in entries:
                history = any(entry.name == name or entry.name.startswith((name + '-', name + '.')) for name in NON_CONFIG_DOTFILES)
                if entry.name.startswith('.') and not history and entry.is_file():
                    yield entry.path
        if Path(home, '.config').exists():
            pending.append(str(Path(home, '.config')))
    while pending:
        checkpoint()
        directory = pending.pop()
        try:
            info = os.stat(directory)
            identity = (info.st_dev, info.st_ino)
            if identity in seen:
                continue
            seen.add(identity)
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(): pending.append(entry.path)
                        elif entry.is_file(): yield entry.path
                    except FileNotFoundError: pass
        except FileNotFoundError:
            continue


def configuration_name_anchors(anchors, inventory, sources=None, refs=None):
    names = {}
    identities = inventory.get('remote_urls', {})
    by_identity = {}
    for tree in inventory['trees']:
        for identity in identities.get(tree, []):
            by_identity.setdefault(identity, set()).add(tree)
    # Name protection never supplies its own evidence, including a different
    # name resolved earlier in this same scan.
    base = {p: why for p, why in anchors.items() if not why.startswith('configuration name ')}
    refs = refs or {}
    keepers = {}

    def other_protection(tree):
        if tree in keepers:
            return keepers[tree]
        try:
            before = os.lstat(tree)
            if not stat.S_ISDIR(before.st_mode) or tree != canonical(tree):
                return None
            reason = ws.anchored(base, tree) or ws.anchored(refs, tree)
            if not reason:
                row = plan(tree, inventory['trees'][tree], dict(inventory, bare=inventory.get('bare', [])), base, refs)
                reason = row.get('reason') if row['action'] == 'keep' else None
            after = os.lstat(tree)
            if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                reason = None
            keepers[tree] = reason
            return reason
        except OSError:
            return None  # A vanished directory cannot satisfy a name.

    for tree in inventory['trees']:
        name = os.path.basename(tree).casefold()
        if (len(name) >= 4 and name.lstrip('.') not in GENERIC_NAMES and
                not re.fullmatch(r'v?\d+(?:[._-]\d+)+(?:[-+][\w.]+)?', name)):
            names.setdefault(name, set()).add(tree)
        for identity in inventory.get('remote_urls', {}).get(tree, []):
            if identity.startswith(('file:', 'opaque:')):
                continue
            pieces = identity.split('/')[1:]
            if len(pieces) >= 2:
                names.setdefault('/'.join(pieces[-2:]).casefold(), set()).add(tree)
    complex_names = [name for name in names if not re.fullmatch(r'[\w.+-]+(?:/[\w.+-]+)*', name)]
    evidence = []
    for path in sorted(set(name_config_sources() if sources is None else sources)):
        checkpoint()
        try:
            info = os.stat(path)
            if not stat.S_ISREG(info.st_mode) or info.st_size > CONFIG_TEXT_LIMIT:
                continue
            with open(path, 'rb') as handle:
                data = handle.read(CONFIG_TEXT_LIMIT + 1)
            if len(data) > CONFIG_TEXT_LIMIT or b'\0' in data:
                continue
            text = data.decode('utf-8', errors='surrogateescape').casefold()
            found = set()
            for match in re.finditer(r'[\w.+-]+(?:/[\w.+-]+)*', text):
                parts = match[0].split('/')
                found.update(part for part in parts if part in names)
                for left, right in zip(parts, parts[1:]):
                    pair = left + '/' + right.removesuffix('.git')
                    if pair in names: found.add(pair)
            found.update(name for name in complex_names if re.search(r'(?<![\w.-])' + re.escape(name) + r'(?![\w.-])', text))
            for name in sorted(found):
                for tree in sorted(names[name]):
                    keys = set(identities.get(tree, []))
                    if '/' in name:
                        keys = {key for key in keys if not key.startswith(('file:', 'opaque:')) and
                                '/'.join(key.split('/')[-2:]).casefold() == name}
                    copies = {tree} | set().union(*(by_identity.get(key, set()) for key in keys))
                    ordered = sorted(copies, key=lambda p: (not bool(ws.anchored(base, p) or ws.anchored(refs, p)), p))
                    holder = next((p for p in ordered if other_protection(p)), None) if len(copies) > 1 else None
                    collision = len(names[name]) > 1
                    label = 'collision' if collision else 'reference'
                    if holder:
                        # Bind the selected copy for this guard generation, even
                        # if its other reason expires before the following plan.
                        anchor(anchors, holder, f'configuration name assigned: {name} in {path}')
                    else:
                        anchor(anchors, tree, f'configuration name {label}: {name} in {path}')
                    evidence.append({'path': tree, 'name': name, 'config': path, 'collision': collision,
                                     'satisfied_by': holder, 'other_reason': keepers.get(holder),
                                     'protected_by_name': not holder or holder == tree})
        except FileNotFoundError:
            continue
    inventory['configuration_name_references'] = evidence


def config_anchors(anchors, sources=None, environments=None, inventory=None):
    """Read shell init, system and per-user units, cron and BB stored commands."""
    unknown = []
    for path, home in sorted(config_sources() if sources is None else sources):
        # Masked units are symlinks to /dev/null. Other symlink targets are also
        # in the global link inventory; read their text only if regular.
        try:
            info = os.stat(path)
        except FileNotFoundError:
            continue  # Includes distro-shipped dangling unit links.
        if not stat.S_ISREG(info.st_mode):
            continue
        text = Path(path).read_text(errors='surrogateescape')
        unit_user = re.search(r'^User=([^\n]+)$', text, re.M)
        if unit_user and '%h' in text:
            try:
                home = pwd.getpwnam(unit_user[1].strip()).pw_dir
            except KeyError:
                unknown.append(unresolved(f'config reference: {path}', 'unit user home unknown'))
                continue
        # Shell scratch variables are often dynamic. Absolute/expanded references
        # are useful even then; retain on unresolved *path-bearing* variables.
        if re.search(r'^\[(Unit|Service|Timer|Socket|Mount|Path)\]$', text, re.M):
            text = text.replace('\\\n', '')
            # Description and other prose are not executable shell syntax.
            text_anchors(anchors, text, home, f'config reference: {path}', strict=False)
            text = '\n'.join(line for line in text.splitlines() if re.match(
                r'^(Environment|EnvironmentFile|Exec\w+|WorkingDirectory|RootDirectory|RootImage|Bind\w*Paths|Read\w*Paths)=', line))
        unknown.extend(environment_references(anchors, text, home, f'config reference: {path}', environments))
    if inventory is not None:
        configuration_name_anchors(anchors, inventory)
    return unique_unknown(unknown)


def project_url_identities(value, sources):
    """Resolve metadata URLs with Git's fetch/push rewrites, without network IO."""
    identities = set()
    for source in sources or ['/']:
        settings = source_git(source, 'config', '--includes', '--get-regexp', r'^url\..*\.(insteadof|pushinsteadof)$', check=False)
        if settings.returncode not in (0, 1):
            settings = command(['git', 'config', '--includes', '--get-regexp',
                                r'^url\..*\.(insteadof|pushinsteadof)$'], cwd='/', env=source_environment(), check=False)
        if settings.returncode not in (0, 1):
            raise ws.ScanIncomplete('project URL rewrites unknown')
        with tempfile.TemporaryDirectory(prefix='git-project-url-') as scratch:
            scratch_git(scratch, 'init', '--bare', '--template=', '-q')
            scratch_git(scratch, 'config', 'remote.project.url', value)
            for line in settings.stdout.splitlines():
                key, setting = line.split(None, 1)
                scratch_git(scratch, 'config', '--add', key, setting)
            for push in (False, True):
                out = scratch_git(scratch, 'remote', 'get-url', '--all', *(['--push'] if push else []), 'project')
                identities.update(remote_identity(url, source) for url in out.stdout.splitlines())
    return sorted(identities)


def bb_anchors(anchors, environments_values=None):
    projects = json.loads(command(['bb', 'project', 'list', '--json']).stdout)
    environments = json.loads(command(['bb', 'environment', 'list', '--json']).stdout)
    if not isinstance(projects, list) or not isinstance(environments, list):
        raise ws.ScanIncomplete('BB inventory incomplete')
    environment_rows = bb_environment_inventory(environments)
    protected_ids = {row['id'] for row in environment_rows if row['protects']}
    for project in projects:
        if not isinstance(project.get('sources'), list):
            raise ws.ScanIncomplete('BB project sources unknown')
        for source in project['sources']:
            if source.get('path'):
                anchor(anchors, source['path'], f'BB project source: {project["id"]}')
    for env in environments:
        if env['id'] in protected_ids:
            path = env.get('path') or ((env.get('environmentProviderSelection') or {}).get('inputs') or {}).get('path')
            if not path and env.get('status') == 'error' and not env.get('environmentProviderInstanceKey'):
                continue  # Provisioning failed before any checkout was materialized.
            if not path:
                raise ws.ScanIncomplete('BB environment path unknown')
            anchor(anchors, path, f'BB environment: {env["id"]}')
    unknown = []
    # Include disabled tasks: they remain configured consumers of these paths.
    all_projects = [*projects, {'id': 'proj_personal'}]
    def read_tasks(project):
        tasks = json.loads(command(['bb', 'automation', 'list', '--project', project['id'], '--json']).stdout)
        if not isinstance(tasks, list):
            raise ws.ScanIncomplete('BB automation inventory incomplete')
        return tasks
    # Every query is required; only read-only acquisition is parallel. Rebind
    # this complete inventory on every execution guard, without a stale cache.
    with cf.ThreadPoolExecutor(8) as pool:
        all_tasks = list(pool.map(read_tasks, all_projects))
    for project, tasks in zip(all_projects, all_tasks):
        project_identities = None
        project_paths = [canonical(s['path']) for s in project.get('sources', []) if s.get('path')]
        env_paths = [canonical(e.get('path') or ((e.get('environmentProviderSelection') or {}).get('inputs') or {}).get('path'))
                     for e in environments if e.get('projectId') == project['id'] and
                     e['id'] in protected_ids and
                     (e.get('path') or ((e.get('environmentProviderSelection') or {}).get('inputs') or {}).get('path'))]
        for task in tasks:
            if task.get('problem'):
                raise ws.ScanIncomplete('BB automation record unreadable')
            execution = task.get('execution') or {}
            reason = f'BB automation reference: {task["id"]}'
            directory = execution.get('workingDirectory') or {}
            if directory.get('path'):
                anchor(anchors, directory['path'], reason)
            text_anchors(anchors, execution.get('prompt', ''), '/root', reason, os.environ, strict=False)
            text = ''
            path = execution.get('storedScriptPath')
            if path:
                anchor(anchors, path, reason)
                text += '\n' + Path(path).read_text()
            metadata = {'BB_PROJECT_ID': [project['id']], 'BB_AUTOMATION_ID': [task['id']]}
            metadata.update({key: [value] for key, value in (execution.get('env') or {}).items()
                             if isinstance(value, str)})
            # The cwd can be fixed, a project checkout, or a specific environment.
            # Use only metadata explicitly selected by the stored task.
            cwd = directory.get('path')
            if cwd:
                metadata['PWD'] = [canonical(cwd)]
            elif directory.get('type') == 'project' and project_paths:
                metadata['PWD'] = project_paths
            elif directory.get('type') in (None, 'automation-storage') and path and Path(path).parent.name == task['id']:
                # BB's declared policy uses the common scripts directory, not
                # the per-task stored-script directory (runtime contract).
                metadata['PWD'] = [canonical(str(Path(path).parent.parent))]
            selected = directory.get('environmentId') or execution.get('environmentId')
            selected_paths = [canonical(e['path']) for e in environments if e.get('id') == selected and e.get('path')]
            if selected_paths:
                metadata['PWD'] = selected_paths
            reader = python_anchors if execution.get('interpreter') == 'python3' else environment_references
            records = reader(anchors, text, (execution.get('env') or {}).get('HOME', '/root'),
                             reason, environments_values, metadata)
            if records and project_identities is None:
                project_identities = project_url_identities(project['gitRemoteUrl'], project_paths) if project.get('gitRemoteUrl') else []
            for record in records:
                record.update(project_id=project['id'], automation_id=task['id'],
                              automation_enabled=task.get('enabled'), project_paths=sorted(set(project_paths + env_paths)),
                              project_source_paths=project_paths, project_environment_paths=env_paths,
                              project_remote_ids=project_identities, automation_name=task.get('name'),
                              automation_last_run_at=task.get('lastRunAt'), automation_last_run_status=task.get('lastRunStatus'))
            unknown.extend(records)
    # Thread checks add the original active/recent-thread rule to environment protection.
    thread_anchors = {}
    ws.bb_anchors(thread_anchors, IDLE)
    for path, reason in thread_anchors.items():
        anchor(anchors, path, reason)
    return unique_unknown(unknown)


def symlink_anchors(anchors, inventory):
    for source, target in inventory['links']:
        # Internal symlinks do not reserve their own checkout. External ones do.
        path = target
        while path != '/':
            if path in inventory['trees'] and not inside(source, path):
                anchor(anchors, target, f'symlink reference: {source}')
            path = os.path.dirname(path)


def usage(inventory, refs=None, *, process_snapshot=None):
    if process_snapshot is None:
        anchors = {}
        environments = process_anchors(anchors)
    else:
        recorded_anchors, recorded_environment = process_snapshot
        anchors = dict(recorded_anchors)
        environments = ProcessEnvironments()
        environments.update({key: set(values) for key, values in (recorded_environment or {}).items()})
        environments.scopes = dict(getattr(recorded_environment, 'scopes', {}))
        environments.contexts = [dict(context) for context in getattr(recorded_environment, 'contexts', [])]
    if environments is None:
        environments = {}
    scope_directories(anchors, getattr(environments, 'scopes', {}), inventory)
    executable = shutil.which('go')
    if executable:
        values = json.loads(command([executable, 'env', '-json', 'GOPATH', 'GOROOT'], timeout=30).stdout)
        environments.update({'__GO_EXE': {canonical(executable)},
                             **{'__GO_' + key: {value} for key, value in values.items()}})
    containers = {}
    ws.docker_anchors(containers)
    for path, reason in containers.items():
        anchor(anchors, path, reason)
    unknown = bb_anchors(anchors, environments) + config_anchors(anchors, environments=environments)
    symlink_anchors(anchors, inventory)
    for mount in process_mounts():
        anchor(anchors, mount, 'mounted descendant')
    unknown = scope_unknown(anchors, unknown, inventory)
    configuration_name_anchors(anchors, inventory, refs=refs)
    return anchors, unknown


def metadata(path, kind):
    if kind == 'unknown':
        raise ws.Keep('symlink or invalid .git marker')
    marker = Path(path, '.git')
    if marker.is_symlink():
        raise ws.Keep('symlink .git marker')
    if kind == 'linked':
        value = marker.read_text().strip()
        if not value.startswith('gitdir: '):
            raise ws.Keep('invalid gitfile')
        target = canonical(os.path.join(path, value[8:]))
        if not os.path.isdir(target):
            raise ws.Keep('gitdir missing; state unknown')
    top = canonical(git(path, 'rev-parse', '--show-toplevel').stdout.strip())
    if top != path:
        raise ws.Keep('Git root mismatch')
    common = canonical(git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip())
    gitdir = canonical(git(path, 'rev-parse', '--path-format=absolute', '--git-dir').stdout.strip())
    out = git(path, 'worktree', 'list', '--porcelain', '-z').stdout
    worktrees = []
    for block in out.split('\0\0'):
        fields = dict(field.partition(' ')[::2] for field in block.split('\0') if field)
        if fields:
            worktrees.append(fields)
    if not worktrees or not worktrees[0].get('worktree'):
        raise ws.Keep('worktree registration unknown')
    primary = canonical(worktrees[0]['worktree'])
    current = next((w for w in worktrees if canonical(w.get('worktree', '/')) == path), None)
    if not current:
        raise ws.Keep('gitfile is not a registered linked worktree (possibly submodule)')
    if 'locked' in current or 'prunable' in current:
        raise ws.Keep('locked or prunable worktree')
    if kind == 'clone' and (len(worktrees) != 1 or primary != path):
        raise ws.Keep('main repository has linked worktrees')
    if kind == 'linked' and (primary == path or not os.path.isdir(primary)):
        raise ws.Keep('main checkout missing or unknown')
    return common, gitdir, primary


def references(inventory):
    """Every discovered repository and bare repo participates, regardless of age."""
    anchors = {}
    inventory['remote_urls'] = {}
    sources = [(p, k) for p, k in inventory['trees'].items()] + [(p, 'bare') for p in inventory['bare']]
    def inspect(pair):
        path, kind = pair
        marker = Path(path) if kind == 'bare' else Path(path, '.git')
        if marker.is_file():
            value = marker.read_text().strip()
            if not value.startswith('gitdir: '):
                return path, [], []
            marker = Path(canonical(os.path.join(path, value[8:])))
            if not marker.exists():
                return path, [], []  # No surviving metadata; state remains unknown.
        if marker.is_symlink():
            raise ws.ScanIncomplete(f'symlink Git metadata: {path}')
        common = marker
        if (marker / 'commondir').exists():
            common = Path(canonical(str(marker / (marker / 'commondir').read_text().strip())))
        def read_config(config, *args):
            out = source_git(path, 'config', '--file', str(config), '--includes', *args, check=False)
            if out.returncode not in (0, 1) and source_git(path, '--git-dir', str(marker), 'rev-parse', '--git-dir', check=False).returncode:
                out = command(['git', 'config', '--file', str(config), '--includes', *args], cwd='/', env=source_environment(), check=False)
            return out
        found, identities = [], set()
        configs = list(dict.fromkeys([common / 'config', marker / 'config.worktree']))
        names = set()
        for config in configs:
            if not config.exists():
                continue
            out = read_config(config, '--get-regexp', r'^remote\..*\.(url|pushurl)$')
            if out.returncode not in (0, 1):
                raise ws.ScanIncomplete(f'remote reference scan incomplete: {path}')
            for line in out.stdout.splitlines():
                key, value = line.split(None, 1)
                name = re.fullmatch(r'remote\.(.+)\.(url|pushurl)', key)
                if name:
                    names.add(name[1])
                target = tc.local_path(value, path)
                if target and not inside(target, path):
                    found.append((target, f'remote URL reference: {path}'))
        for name in names:
            for push in (False, True):
                out = source_git(path, '--git-dir', str(marker), 'remote', 'get-url', '--all', *(['--push'] if push else []), name, check=False)
                if out.returncode:
                    if source_git(path, '--git-dir', str(marker), 'rev-parse', '--git-dir', check=False).returncode == 0:
                        raise ws.ScanIncomplete(f'effective remote URL unknown: {path}')
                    # A broken .git directory (e.g. lost HEAD) stays a keep row.
                    # Its readable configuration is still a reference source.
                    # Ask Git to apply URL rewrites in our disposable context;
                    # never repair or write the discovered repository.
                    options = []
                    for scope in ('--system', '--global'):
                        conditional = command(['git', 'config', scope, '--includes', '--get-regexp', r'^includeIf\.'], cwd='/', env=source_environment(), check=False)
                        if conditional.returncode != 1:
                            raise ws.ScanIncomplete(f'broken Git conditional config unknown ({scope}): {path}')
                        settings = command(['git', 'config', scope, '--includes', '--get-regexp',
                                            r'^url\..*\.(insteadof|pushinsteadof)$'], cwd='/', env=source_environment(), check=False)
                        if settings.returncode not in (0, 1):
                            raise ws.ScanIncomplete(f'broken Git global URL rules unknown: {path}')
                        for line in settings.stdout.splitlines():
                            options.append(tuple(line.split(None, 1)))
                    for config in configs:
                        if not config.exists():
                            continue
                        conditional = read_config(config, '--get-regexp', r'^includeIf\.')
                        if conditional.returncode != 1:
                            raise ws.ScanIncomplete(f'broken Git conditional config unknown: {path}')
                        settings = read_config(config, '--get-regexp', r'^(remote\..*\.(url|pushurl)|url\..*\.(insteadof|pushinsteadof))$')
                        if settings.returncode not in (0, 1):
                            raise ws.ScanIncomplete(f'broken Git config unreadable: {path}')
                        for line in settings.stdout.splitlines():
                            key, value = line.split(None, 1)
                            options.append((key, value))
                    with tempfile.TemporaryDirectory(prefix='git-reference-context-') as scratch:
                        scratch_git(scratch, 'init', '--bare', '--template=', '-q')
                        for key, value in options:
                            scratch_git(scratch, 'config', '--add', key, value)
                        out = scratch_git(scratch, 'remote', 'get-url', '--all', *(['--push'] if push else []), name)
                for value in out.stdout.splitlines():
                    identities.add(remote_identity(value, path))
                    target = tc.local_path(value, path)
                    if target and not inside(target, path):
                        found.append((target, f'remote URL reference: {path}'))
        for objects in dict.fromkeys([common / 'objects', marker / 'objects']):
            alternate = objects / 'info/alternates'
            if alternate.exists():
                for line in alternate.read_text().splitlines():
                    value = json.loads(line) if line.startswith('"') else line
                    target = tc.local_path(value, str(objects))
                    if not target:
                        raise ws.ScanIncomplete(f'alternates reference unknown: {path}')
                    found.append((target, f'alternates reference: {path}'))
        return path, found, sorted(identities)
    with cf.ThreadPoolExecutor(8) as pool:
        for path, found, identities in pool.map(inspect, sources):
            inventory['remote_urls'][path] = identities
            for target, reason in found:
                anchor(anchors, target, reason)
    return anchors


def same_origin_nested(path, kind, parent, parent_kind):
    """Rebind origin evidence at every guard; foreign/unknown snapshots stay."""
    if kind == 'unknown' or parent_kind == 'unknown':
        return False
    try:
        if kind == 'linked':
            common = source_git(path, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
            parent_common = source_git(parent, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()
            if common and parent_common and canonical(common) == canonical(parent_common):
                return True  # Registration/locks/main checkout are checked below.
        # Reuse effective fetch/push URL resolution, including Git rewrites and
        # physical local identities. Do not rely on the earlier inventory here:
        # a parent remote may have changed without touching the child.
        current = {'trees': {path: kind, parent: parent_kind}, 'bare': []}
        references(current)
        return bool(set(current['remote_urls'][path]) & set(current['remote_urls'][parent]))
    except Exception:
        return False  # An unreadable relationship cannot exempt a nested tree.


def parent_git_state(path):
    """A marker alone does not give a directory Git ownership of descendants."""
    marker = os.path.join(path, '.git')
    try:
        result = source_git(path, 'rev-parse', '--show-toplevel', check=False)
        if result.returncode == 0:
            if result.stdout.rstrip('\n') and canonical(result.stdout.rstrip('\n')) == path:
                return 'valid', None
            return 'invalid', 'Git top-level is another directory'
        if 'not a git repository' in result.stderr or 'invalid gitfile format' in result.stderr:
            return 'invalid', 'Git does not recognize this .git marker'
        if 'this operation must be run in a work tree' in result.stderr:
            return 'invalid', 'Git does not recognize a working tree here'
        return 'unknown', 'Git top-level probe failed'
    except (OSError, subprocess.SubprocessError, ws.ScanIncomplete):
        return 'unknown', f'could not verify {marker}'


def parent_git_inventory(trees):
    parents = {str(parent) for path in trees for parent in Path(path).parents if str(parent) in trees}
    ordered = sorted(parents)
    with cf.ThreadPoolExecutor(8) as pool:
        return dict(zip(ordered, pool.map(parent_git_state, ordered)))


def ignored_dependency_file(rel):
    parts = Path(rel).parts[:-1]  # Directory components only, never the leaf name.
    if any(part in {'node_modules', 'vendor', '.venv', 'site-packages'} for part in parts):
        return True
    for i, part in enumerate(parts[:-2]):
        store = part == '.pnpm-store' or (part == 'store' and i > 0 and parts[i - 1] == 'pnpm')
        if store and re.fullmatch(r'v[0-9]+', parts[i + 1]) and parts[i + 2] in ('files', 'index'):
            return True
    return False


def local_state(path, kind, inventory, anchors, refs, live_parents=False):
    checkpoint()
    if path != canonical(path) or not stat.S_ISDIR(os.lstat(path).st_mode):
        raise ws.Keep('symlink or nonphysical directory')
    if os.path.ismount(path):
        raise ws.Keep('filesystem root or mount point')
    parent_states = {} if live_parents else inventory.get('parent_git_states', {})
    nested_child, nested_kind = path, kind
    for ancestor in Path(path).parents:
        parent = str(ancestor)
        marker = ancestor / '.git'
        parent_kind = inventory['trees'].get(parent)
        if live_parents and (parent_kind is not None or
                             any(inside(parent, root) for root in inventory.get('scan_roots', []))):
            try:
                marker_info = marker.lstat()
            except FileNotFoundError:
                continue
            parent_kind = ('clone' if stat.S_ISDIR(marker_info.st_mode) else
                           'linked' if stat.S_ISREG(marker_info.st_mode) else 'unknown')
        if parent_kind is None:
            continue
        state, detail = parent_states[parent] if parent in parent_states else parent_git_state(parent)
        if state == 'invalid':
            continue
        if state == 'unknown':
            raise ws.Keep(f'parent Git worktree unknown: {parent}: {detail}')
        if not same_origin_nested(nested_child, nested_kind, parent, parent_kind):
            raise ws.Keep(f'嵌套于 {parent}，由该项目自行保留')
        # A descendant with remotes A+B must not bridge foreign B under A.
        nested_child, nested_kind = parent, parent_kind
    for index in (anchors, refs):
        holder = ws.anchored(index, path)
        if holder:
            raise ws.Keep(holder)
    if any(other != path and inside(other, path) for other in [*inventory['trees'], *inventory['bare']]):
        raise ws.Keep('contains nested Git repository')
    common, gitdir, primary = metadata(path, kind)
    for directory in dict.fromkeys([common, gitdir]):
        for name in (*ws.IN_PROGRESS, 'index.lock', 'HEAD.lock', 'packed-refs.lock', 'config.lock'):
            if os.path.lexists(os.path.join(directory, name)):
                raise ws.Keep(f'git operation in progress: {name}')
    latest = os.lstat(path).st_mtime
    def failed(exc):
        raise exc
    for parent, dirs, files in os.walk(path, followlinks=False, onerror=failed):
        checkpoint()
        if parent == path:
            dirs[:] = [d for d in dirs if d != '.git']
            files = [f for f in files if f != '.git']
        elif '.git' in dirs or '.git' in files:
            raise ws.Keep('contains nested Git repository')
        for index, name in enumerate(dirs + files):
            if index % 128 == 0:
                checkpoint()
            latest = max(latest, os.lstat(os.path.join(parent, name)).st_mtime)
    for directory in dict.fromkeys([common, gitdir]):
        # Borrowers may be removed. The lender is protected by references();
        # changing the borrowing configuration is still recent Git activity.
        for name in ('objects/info/alternates', 'objects/info/http-alternates'):
            target = os.path.join(directory, name)
            if os.path.lexists(target):
                latest = max(latest, os.lstat(target).st_mtime, os.stat(target).st_mtime)
        # Ref/reflog/FETCH_HEAD changes are use too, even if checkout files and
        # HEAD/index did not change. Object payload and sibling metadata are
        # not traversed; their shared refs and this worktree's metadata are.
        for parent, dirs, files in os.walk(directory, followlinks=False, onerror=failed):
            checkpoint()
            dirs[:] = [d for d in dirs if d not in ('objects', 'worktrees', 'modules')]
            for index, name in enumerate([None, *dirs, *files]):
                if index % 128 == 0:
                    checkpoint()
                target = os.path.join(parent, name) if name else parent
                latest = max(latest, os.lstat(target).st_mtime)
    if time.time() - latest < IDLE:
        raise ws.Keep('active within 7d (168h)')
    if git(path, 'status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none', timeout=600).stdout:
        raise ws.Keep('dirty working tree (including untracked)')
    if git(path, 'stash', 'list').stdout:
        raise ws.Keep('stash exists')
    if ws.populated_submodule(path):
        raise ws.Keep('populated submodule')
    # Owner-rebuildable installed dependency files are not repository secrets.
    out = git(path, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard', timeout=600).stdout
    for rel in out.split('\0'):
        if (rel and ws.SECRET_NAME.search(rel) and not ws.SECRET_TEMPLATE.search(rel)
                and not ignored_dependency_file(rel)):
            raise ws.Keep(f'ignored credential: {rel}')
    head = git(path, 'rev-parse', 'HEAD').stdout.strip()
    objects = dict(line.split(None, 1) for line in git(path, 'for-each-ref',
                   '--format=%(refname) %(objectname)', 'refs/heads', 'refs/tags').stdout.splitlines())
    objects['HEAD'] = head
    info = os.lstat(path)
    return {'head': head, 'objects': objects, 'main': primary, 'gitdir': gitdir,
            'identity': [info.st_dev, info.st_ino], 'idle_hours': (time.time() - latest) / 3600}


def recovery_state(path, objects):
    item = {'objects': objects}
    repo = LiveRepo(path, set(ws.DEFAULT_LOGINS))
    # A remote inside this tree vanishes with it. External local remotes
    # participate in the full reference index, which keeps their containing
    # checkout; bare stores are reference sources, never deletion targets.
    missing_local = {}
    for name, (url, _) in list(repo.remotes.items()):
        target = tc.local_path(url, path) if url else None
        if not target:
            continue
        if inside(canonical(target), path):
            repo.remotes.pop(name)
            if repo.dest == name:
                repo.dest = None
            continue
        try:
            os.stat(target)
        except (FileNotFoundError, NotADirectoryError):
            missing_local[name] = target
            repo.remotes.pop(name)
        except OSError as exc:
            raise ws.Keep(f'local remote could not be checked: {name} ({target}); {exc.strerror}') from exc
    if missing_local:
        repo.dest = repo._archive_destination(set(ws.DEFAULT_LOGINS))
    repo.load_live(include_tags=True)
    if repo.unreachable:
        first = sorted(repo.unreachable)[0]
        failures = ', '.join(f'{name} ({repo.remote_errors[name]})' for name in sorted(repo.unreachable))
        # Network/authentication failure does not prove refs are absent.
        cache = ' [AgentFlow knowledge-cache]' if inside(path, str(Path.home() / '.agentflow/knowledge-cache')) else ''
        raise ws.Keep(f'{repo.remote_errors[first]}: {failures}{cache}')
    if not repo.live and not missing_local:
        raise ws.Keep('no independently recoverable remote')
    item['remote_proofs'] = {ref: (repo.by_sha.get(sha) if ref.startswith('refs/tags/') else repo.proof(sha))
                             for ref, sha in item['objects'].items()}
    missing = {ref: sha for ref, sha in item['objects'].items() if not item['remote_proofs'][ref]}
    item['archive_objects'] = missing
    item['urls'] = [pair[0] for pair in repo.remotes.values()]
    item['missing_local_remotes'] = missing_local
    if missing:
        if not repo.dest:
            if missing_local:
                lost = ', '.join(f'{name} ({target})' for name, target in sorted(missing_local.items()))
                raise ws.Keep(f'unpushed refs; local remote missing and no archive destination: {lost}')
            raise ws.Keep('unpushed refs and no personal or internal remote')
        url = ws.transport_url(repo.remotes[repo.dest][1])
        if any('mono2' in s.lower() for s in [path, *item['urls']]) and ws.parse_remote(url)[0] == 'github.com':
            raise ws.Keep('mono2 must never be archived to GitHub')
        item['archive_url'] = url
    item.pop('objects')
    return item


def plan(path, kind, inventory, anchors, refs, error=None, *, live_parents=False):
    item = {'path': path, 'type': kind, 'action': 'keep', 'archives': []}
    try:
        with execution_step('local_state'):
            item.update(local_state(path, kind, inventory, anchors, refs, live_parents))
        if error:
            raise ws.Keep(error)
        with execution_step('remote_proof'):
            item.update(recovery_state(path, item['objects']))
        item['action'] = 'archive+remove' if item['archive_objects'] else 'remove'
    except Exception as exc:
        item['reason'] = str(exc) if isinstance(exc, ws.Keep) else 'check unknown: ' + ws.failure_text(exc)
    return item


@contextlib.contextmanager
def physical_parent(path):
    """Bind every ancestor by fd with O_NOFOLLOW, then operate on one basename."""
    if path == '/' or not os.path.basename(path):
        raise ws.Keep('not a removable directory entry')
    parts = Path(path).parts
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd, parts[-1]
    finally:
        os.close(fd)


def clone_environment_anchors():
    anchors = {}
    for row in bb_environment_inventory():
        if not row['protects'] or row.get('unmaterialized'):
            continue
        path = row.get('path')
        if not path or not path.startswith('/'):
            raise ws.ScanIncomplete('BB environment path unknown: ' + row['id'])
        anchor(anchors, path, 'BB environment: ' + row['id'])
    return anchors


def verify_clone_recovery(item):
    """A fresh server generation for this sink, including prior exact archives."""
    LIVE.clear()
    current = recovery_state(item['path'], item['objects'])
    missing = current['archive_objects']
    if missing:
        expected = [a for a in item['archives'] if missing.get(a['source']) == a['sha']]
        if set(missing) != {a['source'] for a in expected}:
            raise ws.Keep('remote proof changed after plan')
        verify_archives(item['path'], expected)
        for archive in expected:
            current['remote_proofs'][archive['source']] = archive['url'] + ':' + archive['ref']
    item['remote_proofs'] = current['remote_proofs']
    item['urls'] = current['urls']
    item['missing_local_remotes'] = current['missing_local_remotes']


def remove(item, inventory, anchors, refs, *, live=False):
    path = item['path']
    if live:
        with execution_step('remote_proof'):
            verify_clone_recovery(item)
        with execution_step('bb_environment'):
            anchors = {**anchors, **clone_environment_anchors()}
    with execution_step('local_state'):
        local = local_state(path, item['type'], inventory, anchors, refs, live)
    for key in ('identity', 'objects', 'gitdir', 'main'):
        if local[key] != item[key]:
            raise ws.Keep(f'{key} changed after plan')
    if live:
        with execution_step('process_scan'):
            processes = {}
            environments = process_anchors(processes)
            scope_directories(processes, getattr(environments, 'scopes', {}), {'trees': {path: item['type']}})
            holder = ws.anchored(processes, path)
            if holder:
                raise ws.Keep(holder)
    with execution_step('mount_scan'):
        for mount in process_mounts():
            if inside(mount, path):
                raise ws.Keep('mounted path inside candidate: ' + mount)
    checkpoint()
    with physical_parent(path) as (fd, name):
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if [current.st_dev, current.st_ino] != item['identity']:
            raise ws.Keep('directory identity changed')
        # Opening ancestors and the final stat are IO too. A crossed deadline
        # must stop before entering either destructive owner operation.
        checkpoint()
        if item['type'] == 'linked':
            # Git refuses dirty/locked/submodule worktrees; never add --force.
            git(item['main'], 'worktree', 'remove', '--', path, timeout=600)
        else:
            if not shutil.rmtree.avoids_symlink_attacks:
                raise ws.Keep('fd-based deletion unavailable')
            with execution_step('delete'):
                shutil.rmtree(name, dir_fd=fd)
    item['result'] = 'removed'


def persist(path, value):
    with open(path, 'a', opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
        handle.write(json.dumps(value, ensure_ascii=True) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def verify_archives(path, archives, *, require_all=True):
    groups = {}
    for archive in archives:
        groups.setdefault(archive['url'], []).append(archive)
    verified = []
    for url, entries in groups.items():
        out = git(path, 'ls-remote', url, *(a['ref'] for a in entries)).stdout
        observed = {ref: sha for sha, ref in (line.split() for line in out.splitlines())}
        verified.extend(a for a in entries if observed.get(a['ref']) == a['sha'])
    if require_all and len(verified) != len(archives):
        raise ws.Keep('archive SHA verification failed')
    return verified


def execute(rows, inventory, anchors, refs, roots, excluded, stamp, log):
    global DEADLINE
    round_deadline = DEADLINE
    for row in rows:
        if row['action'] == 'keep':
            continue
        started = time.monotonic()
        DEADLINE = min(round_deadline, started + CLONE_RECHECK_SECONDS)
        EXECUTION.row = row
        row['execution_started_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        row['global_inventory_at'] = inventory.get('snapshot_at')
        persist(log, dict(row, event='candidate_started'))
        try:
            if row.get('bytes') is None:
                raise ws.Keep('size measurement incomplete')
            LIVE.clear()
            fresh = plan(row['path'], row['type'], inventory, anchors, refs, live_parents=True)
            if fresh['action'] == 'keep':
                raise ws.Keep(fresh['reason'])
            if fresh['objects'] != row['objects'] or fresh['identity'] != row['identity']:
                raise ws.Keep('local evidence changed')
            archives = []
            for source, sha in fresh['archive_objects'].items():
                prefix = 'refs/tags' if source.startswith('refs/tags/') else 'refs/heads'
                ref = f'{prefix}/archive/git-tree/{ws.archive_slug(row["path"])}-{stamp}/' + re.sub(r'[^A-Za-z0-9-]', '-', source)
                url = fresh['archive_url']
                archives.append({'source': source, 'url': url, 'ref': ref, 'sha': sha})
            if archives:
                persist(log, dict(row, event='archive_intent', requested_archives=archives))
                with execution_step('archive'):
                    # One push preserves all original objects; no squash or force.
                    push_error = None
                    try:
                        pushed = git(row['path'], 'push', fresh['archive_url'], *(f'{a["sha"]}:{a["ref"]}' for a in archives), timeout=900, check=False)
                        if pushed.returncode:
                            push_error = ws.failure_text(pushed.stderr or pushed.stdout)
                    except Exception as exc:
                        push_error = ws.failure_text(exc)
                    verified = verify_archives(row['path'], archives, require_all=False)
                    row['archives'].extend(verified)
                    if verified:
                        persist(log, dict(row, event='archive_verified'))
                    if push_error:
                        raise ws.Keep('archive push failed: ' + push_error)
                    if len(verified) != len(archives):
                        raise ws.Keep('archive SHA verification failed')
            persist(log, dict(row, event='remove_intent'))
            remove(row, inventory, anchors, refs, live=True)
        except Exception as exc:
            timed_out = time.monotonic() >= DEADLINE and DEADLINE < round_deadline
            row.update(result='kept', reason=('clone live-check exceeded 10s; retry next round: ' if timed_out else '') + ws.failure_text(exc))
        finally:
            row.setdefault('execution_timings', {})['total'] = round(time.monotonic() - started, 6)
            DEADLINE = round_deadline
            EXECUTION.row = None
        persist(log, row)


def summarize(rows):
    result = {'counts': {}, 'bytes': {}, 'unmeasured': 0}
    for row in rows:
        key = row.get('result', row['action'])
        if key in ('keep', 'kept'):
            key = 'keep: ' + ws.reason_key(row.get('reason', 'unknown'))
        result['counts'][key] = result['counts'].get(key, 0) + 1
        if row.get('bytes') is None:
            result['unmeasured'] += 1
        else:
            result['bytes'][key] = result['bytes'].get(key, 0) + row['bytes']
    result['retention'] = retention_ranking(rows)
    # Nested rows and shared blocks can overlap: inventory size is not freed space.
    return result


def retention_ranking(rows):
    groups = {}
    for row in rows:
        if row.get('result', row['action']) not in ('keep', 'kept'):
            continue
        reason = ws.reason_key(row.get('reason', 'unknown'))
        groups.setdefault(reason, []).append(row)
    buckets = []
    for reason, items in groups.items():
        measured = {}
        for row in items:
            if row.get('bytes') is not None and row.get('path'):
                path = os.path.normpath(row['path'])
                measured[path] = max(measured.get(path, 0), row['bytes'])
        # Within a bucket, count a measured subtree only once per lexical
        # root. Per-path du still cannot deduplicate hardlinks across roots.
        largest_roots = {
            path: size for path, size in measured.items()
            if not any(str(parent) in measured for parent in Path(path).parents)
        }
        buckets.append(dict(
            reason=reason, count=len(items),
            unmeasured=sum(row.get('bytes') is None for row in items),
            bytes=sum(largest_roots.values()) if largest_roots else None,
            largest_paths=[dict(path=path, bytes=size) for path, size in
                           sorted(largest_roots.items(), key=lambda pair: (-pair[1], pair[0]))[:5]],
        ))
    by_bytes = sorted(buckets, key=lambda row: (-(row['bytes'] if row['bytes'] is not None else -1), row['reason']))
    by_count = sorted(buckets, key=lambda row: (-row['count'], row['reason']))
    return {'by_bytes': by_bytes,
            'by_count': [{'reason': row['reason'], 'count': row['count']} for row in by_count]}


def environment_retention(rows, host):
    # This is a reporting view only: a release/refusal hands the path to Git.
    measured = []
    until = min(time.monotonic() + 20, DEADLINE - 5)

    def size(path):
        try:
            left = until - time.monotonic()
            if left > 0 and os.path.isdir(path) and not os.path.islink(path):
                out = command(['du', '-sx', '-B1', '--', path], timeout=left, check=False)
                if out.returncode == 0:
                    return int(out.stdout.split()[0])
        except (OSError, ValueError, IndexError, subprocess.SubprocessError, ws.ScanIncomplete):
            pass
        return None

    for row in rows:
        outcome = row.get('result', row['action'])
        if row['reason'] == 'environment destroyed' or outcome not in ('keep', 'kept', 'cleanup requested; pending'):
            continue
        reason = row.get('cleanup_error', outcome if outcome == 'cleanup requested; pending' else row['reason'])
        measured.append(dict(row, action='keep', result='kept', reason=reason, bytes=None))

    def measure(row):
        path = row['path']
        if row['host_id'] == host and path and os.path.isabs(path) and os.path.normpath(path) != '/':
            row['bytes'] = size(path)
    with cf.ThreadPoolExecutor(8) as pool:
        list(pool.map(measure, measured))
    return retention_ranking(measured)


def reviewed_comparison(rows, preview_path):
    preview = json.loads(Path(preview_path).read_text())
    if preview.get('mode') != 'dry-run' or preview.get('complete') is not True or preview.get('error'):
        raise ws.Keep('reviewed preview is not a complete dry-run')
    allowed = {(item['path'], item['type']) for item in preview['items'] if item['action'] in ('remove', 'archive+remove')}
    added = [{'path': item['path'], 'type': item['type']} for item in rows
             if item['action'] != 'keep' and (item['path'], item['type']) not in allowed]
    return {'preview': str(preview_path), 'allowed': len(allowed), 'added': added}


def bb_environment_classify(env, threads, now=None):
    """Archived is a lifecycle fact, never inferred from an idle runtime."""
    now = time.time() if now is None else now
    path = env.get('path') or ((env.get('environmentProviderSelection') or {}).get('inputs') or {}).get('path')
    row = dict(id=env['id'], path=path, host_id=env.get('hostId'),
               provider=env.get('environmentProviderId'), managed=env.get('managed') is True,
               unmaterialized=not path and env.get('status') == 'error' and not env.get('environmentProviderInstanceKey'),
               abandoned=False, protects=True, thread_ids=[t['id'] for t in threads])
    if env.get('status') == 'destroyed' or (env.get('lifecycle') or {}).get('phase') == 'destroyed':
        return dict(row, protects=False, reason='environment destroyed')
    unarchived = [t['id'] for t in threads if t.get('archivedAt') is None]
    if unarchived:
        return dict(row, reason='unarchived threads', unarchived_thread_ids=unarchived)
    values = []
    for record in [env, *threads]:
        for key in ('createdAt', 'updatedAt'):
            value = record.get(key)
            if type(value) not in (int, float) or not 0 < value <= now * 1000:
                return dict(row, reason='environment/thread activity timestamp unknown')
            values.append(value)
        for key in ('archivedAt', 'latestAttentionAt', 'lastReadAt'):
            value = record.get(key)
            if value is not None:
                if type(value) not in (int, float) or not 0 < value <= now * 1000:
                    return dict(row, reason='environment/thread activity timestamp unknown')
                values.append(value)
        activity = record.get('activity') or {}
        if (record.get('status') in ws.BB_BUSY or
                record.get('queuedWork', 'none') != 'none' or record.get('hasPendingInteraction') or
                any(value for value in activity.values() if isinstance(value, int))):
            return dict(row, reason='environment/thread runtime still active')
    latest = max(values) / 1000
    row.update(last_activity=latest, idle_hours=(now - latest) / 3600)
    if now - latest < 86400:
        return dict(row, reason='BB activity within 24h')
    teardown = (env.get('lifecycle') or {}).get('teardown') or {}
    # A successful asynchronous request is not a refusal: let its owner finish.
    running = teardown.get('status') == 'running' or (env.get('lifecycle') or {}).get('phase') in ('removing', 'destroying')
    return dict(row, abandoned=True, protects=running,
                reason='BB provider cleanup running' if running else 'all threads archived or absent; BB idle >=24h')


def bb_environment_inventory(environments=None, threads=None):
    if environments is None:
        environments = json.loads(command(['bb', 'environment', 'list', '--json']).stdout)
    if not isinstance(environments, list) or any(not isinstance(e, dict) or not e.get('id') for e in environments):
        raise ws.ScanIncomplete('BB environment inventory incomplete')
    # Older/incomplete records already remain protected, without inventing age.
    if threads is None:
        threads = (json.loads(command(['bb', 'thread', 'list', '--include-archived', '--include-hidden', '--json']).stdout)
                   if any(e.get('createdAt') and e.get('updatedAt') for e in environments) else [])
    if not isinstance(threads, list) or any(not isinstance(t, dict) or not t.get('id') or 'environmentId' not in t for t in threads):
        raise ws.ScanIncomplete('BB thread inventory incomplete')
    grouped = {}
    for thread in threads:
        grouped.setdefault(thread['environmentId'], []).append(thread)
    return [bb_environment_classify(env, grouped.get(env['id'], [])) for env in environments]


def bb_provider_workspace_idle(row, context):
    """BB removal may force/remove and stop cwd users: establish idle first."""
    path = canonical(row['path'])
    for points in ('cwds', 'environments'):
        for point, reason in context[points].items():
            if inside(point, path):
                raise ws.Keep(reason)
    if not os.path.lexists(path):
        return  # BB also owns cleanup of an already-missing managed workspace.
    roots = [path]
    if os.path.isfile(os.path.join(path, '.git')):
        roots.append(metadata(path, 'linked')[1])
    def failed(exc): raise exc
    for root in roots:
        for parent, dirs, files in os.walk(root, followlinks=False, onerror=failed):
            checkpoint()
            if root == path and parent == path and len(roots) > 1:
                dirs[:] = [d for d in dirs if d != '.git']
            names = [None, *dirs, *files] if root == path else files
            for name in names:
                info = os.lstat(os.path.join(parent, name) if name else parent)
                if context.get('cwd_identities', {}).get(f'{info.st_dev}:{info.st_ino}'):
                    raise ws.Keep('live process cwd in provider workspace')
                if time.time() - info.st_mtime < 86400:
                    raise ws.Keep('provider workspace active within 24h')


def bb_local_host():
    # BB's server host has no machine provider; enrolled/remote machines do.
    # Unlike `bb status.thread`, this inventory also exists under systemd.
    machines = json.loads(command(['bb','machine','list','--all','--json']).stdout)
    if not isinstance(machines, list):
        raise ws.ScanIncomplete('BB machine inventory unknown')
    local = [m for m in machines if isinstance(m, dict) and
             'machineProviderId' in m and m['machineProviderId'] is None and
             m.get('type') == 'persistent' and m.get('status') == 'connected' and m.get('id')]
    if len(local) != 1:
        raise ws.ScanIncomplete('local BB host identity ambiguous/unavailable')
    return local[0]['id']


def bb_environment_main(argv=None):
    global DEADLINE
    parser = argparse.ArgumentParser(description='Retire BB environments: all threads archived/absent and idle >=24h.')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--budget-minutes', type=float, default=5)
    parser.add_argument('--state-dir', default=os.path.expanduser('~/.local/state/devcloud-storage-upkeep'))
    args = parser.parse_args(argv)
    started = time.monotonic(); DEADLINE = started + args.budget_minutes * 60
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    os.makedirs(args.state_dir, mode=0o700, exist_ok=True)
    output = Path(args.state_dir, f'bb-environment-sweep-{stamp}.json'); log = output.with_suffix('.jsonl')
    rows, error, failures, host = [], None, [], None
    with open(Path(args.state_dir, 'git-tree-sweep.lock'), 'a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Git sweep busy; skipped'); return 0
        persist(log, {'event':'start','timestamp':stamp,'mode':'execute' if args.execute else 'dry-run','bb_cli':os.environ.get('BB_CLI')})
        try:
            host = bb_local_host()
            environments = json.loads(command(['bb','environment','list','--json']).stdout)
            rows = [dict(row, action='keep') for row in bb_environment_inventory(environments)]
            for row in rows:
                checkpoint()
                row['action'] = 'keep'
                if not row['abandoned'] or row['protects']: continue
                if row['host_id'] != host:
                    row['reason'] = 'different/unknown host; no local cleanup'; continue
                if not row['managed'] or not row['provider']:
                    row['action'] = 'release-protection'; continue
                row['action'] = 'provider-cleanup'
                try:
                    if not row['path'] or not row['path'].startswith('/'):
                        raise ws.Keep('provider path unknown')
                    bb_provider_workspace_idle(row, linked_context())
                    if not args.execute: continue
                    env = json.loads(command(['bb','environment','show',row['id'],'--json']).stdout)
                    threads = json.loads(command(['bb','thread','list','--environment',row['id'],'--include-archived','--include-hidden','--json']).stdout)
                    fresh = bb_environment_inventory([env], threads)[0]
                    if not fresh['abandoned'] or fresh['protects'] or any(fresh[key] != row[key] for key in ('path','host_id','provider','managed')):
                        raise ws.Keep('BB environment evidence changed')
                    bb_provider_workspace_idle(fresh, linked_context())
                    persist(log, dict(row,event='cleanup_intent'))
                    out = command(['bb','environment','cleanup',row['id'],'--json'],check=False)
                    reply = json.loads(out.stdout)
                    if out.returncode:
                        problem = reply.get('error') or {}
                        if problem.get('code') not in ('invalid_request', 'conflict', 'environment_in_use') or not problem.get('message'):
                            raise ws.ScanIncomplete('BB cleanup failed: ' + ws.failure_text(out.stderr or out.stdout))
                        row.update(result='refused; Git fallback',cleanup_error=problem['message'])
                    else:
                        if reply.get('ok') is not True:
                            raise ws.ScanIncomplete('BB cleanup response unknown')
                        observed = json.loads(command(['bb','environment','show',row['id'],'--json']).stdout)
                        if observed.get('id') != row['id'] or observed.get('status') not in ('provisioning','ready','error','destroyed'):
                            raise ws.ScanIncomplete('BB cleanup observation unknown')
                        row.update(result='destroyed' if observed.get('status')=='destroyed' or (observed.get('lifecycle') or {}).get('phase')=='destroyed' else 'cleanup requested; pending', observed=observed)
                except ws.Keep as exc:
                    row.update(result='kept', cleanup_error=ws.failure_text(exc))
                except Exception as exc:
                    row.update(result='failed', cleanup_error=ws.failure_text(exc))
                    failures.append(row['id'] + ': ' + row['cleanup_error'])
                persist(log,row)
        except Exception as exc: error = ws.failure_text(exc)
        error = error or ('; '.join(failures) if failures else None)
        if time.monotonic() >= DEADLINE: error = error or 'time budget reached'
        retention = environment_retention(rows, host) if host is not None else retention_ranking([])
        report = dict(schema_version=1, policy='bb-environments', bb_cli=os.environ.get('BB_CLI'), mode='execute' if args.execute else 'dry-run',
                      timestamp=stamp,invocation_id=os.environ.get('INVOCATION_ID'),complete=not bool(error),error=error,failures=failures,items=rows,
                      summary={'retention': retention},
                      timings={'total_seconds':round(time.monotonic()-started,3)},output=str(output))
        with open(output,'w',opener=lambda p,flags:os.open(p,flags,0o600)) as handle: json.dump(report,handle,ensure_ascii=True,indent=2)
        for row in rows: persist(log,row)
        persist(log, {'event': 'finish', 'complete': report['complete'], 'summary': report['summary']})
        print(json.dumps(report if args.json else {key:report[key] for key in ('policy','mode','complete','error','timings','summary','output')}))
        return 1 if error else 0


def linked_context(proc='/proc'):
    """Only BB environments and live process cwd belong to the linked policy."""
    projects = json.loads(command(['bb', 'project', 'list', '--json']).stdout)
    environments = json.loads(command(['bb', 'environment', 'list', '--json']).stdout)
    if not isinstance(projects, list) or not isinstance(environments, list):
        raise ws.ScanIncomplete('BB linked inventory incomplete')
    protected_ids = {row['id'] for row in bb_environment_inventory(environments) if row['protects']}
    sources, reserved, cwds, cwd_identities = set(), {}, {}, {}
    for project in projects:
        if not isinstance(project.get('sources'), list):
            raise ws.ScanIncomplete('BB project sources unknown')
        sources.update(s['path'] for s in project['sources'] if s.get('path'))
    for env in environments:
        path = env.get('path') or ((env.get('environmentProviderSelection') or {}).get('inputs') or {}).get('path')
        if path:
            sources.add(path)
        if env['id'] not in protected_ids:
            continue
        if not path and env.get('status') == 'error' and not env.get('environmentProviderInstanceKey'):
            continue
        if not path:
            raise ws.ScanIncomplete('BB environment path unknown')
        reserved[canonical(path)] = 'BB environment: ' + env['id']
    for pid in os.listdir(proc):
        if not pid.isdigit():
            continue
        base = Path(proc, pid)
        try:
            before = (base / 'stat').read_text().rsplit(')', 1)[1].split()[19]
            try:
                path = os.readlink(base / 'cwd').removesuffix(' (deleted)')
            except FileNotFoundError:
                path = None  # Kernel/zombie process or an exited process.
            ancestors = {}
            if path:
                pointer = str(base / 'cwd')
                seen = set()
                for _ in range(256):
                    checkpoint()
                    info = os.stat(pointer)
                    identity = f'{info.st_dev}:{info.st_ino}'
                    if identity in seen: break
                    seen.add(identity)
                    ancestors[identity] = f'process {pid} cwd (inode ancestry: {path})'
                    pointer += '/..'
                else: raise ws.ScanIncomplete('process cwd ancestry unknown')
            after = (base / 'stat').read_text().rsplit(')', 1)[1].split()[19]
            if before != after:
                raise ws.ScanIncomplete('process identity changed')
            if path:
                if not path.startswith('/'):
                    raise ws.ScanIncomplete('process cwd unknown')
                cwds[canonical(path)] = f'process {pid} cwd'
                cwd_identities.update(ancestors)
        except (FileNotFoundError, ProcessLookupError):
            continue
        except (OSError, ValueError, IndexError) as exc:
            raise ws.ScanIncomplete(f'linked cwd scan incomplete: {pid}') from exc
    return {'sources': sorted(sources), 'environments': reserved, 'cwds': cwds, 'cwd_identities': cwd_identities}


def linked_known_sources(state_dir, context):
    """The last complete disk discovery supplements live BB repository paths.

    A new main will be indexed by the unchanged full-disk clone pass. Reading
    the existing inventory avoids waiting for another disk walk before linked
    worktrees can be processed.
    """
    sources = set(context['sources'])
    indexed = None
    for path in sorted(Path(state_dir).glob('git-tree-sweep-*.json'), reverse=True):
        checkpoint()
        try:
            report = json.loads(path.read_text())
            if not isinstance(report, dict):
                continue
            if report.get('roots') != ['/', '/data'] or report.get('discovery', {}).get('errors'):
                continue
            if not isinstance(report.get('items'), list):
                continue
            if not all(isinstance(x, dict) and isinstance(x.get('path'), str) and x['path'].startswith('/') for x in report['items']):
                continue
            sources.update(x['path'] for x in report['items'])
            sources.update(report['discovery'].get('bare', []))
            sources.update(report['discovery'].get('linked_markers', []))
            indexed = str(path)
            break
        except (OSError, ValueError, KeyError):
            continue
    return sorted(sources), indexed


def linked_registrations(main):
    out = source_git(main, 'worktree', 'list', '--porcelain', '-z').stdout
    entries = [dict(field.partition(' ')[::2] for field in block.split('\0') if field)
               for block in out.split('\0\0') if block.strip('\0')]
    if not entries or not entries[0].get('worktree'):
        raise ws.Keep('Git worktree registration unknown')
    primary = canonical(entries[0]['worktree'])
    rows = []
    for entry in entries[1:]:
        if not entry.get('worktree'):
            raise ws.Keep('Git worktree path unknown')
        rows.append(dict(entry, path=canonical(entry['worktree']), registered_path=entry['worktree'],
                         main=primary, type='linked'))
    return primary, rows


def linked_inventory(sources):
    mains, failures = {}, []
    def inspect(path):
        try:
            try:
                info = os.stat(path)
            except (FileNotFoundError, NotADirectoryError):
                return None
            if not stat.S_ISDIR(info.st_mode):
                return None
            return linked_registrations(path)
        except Exception as exc:
            # A container that is not a Git repo is not a failed main. Broken
            # Git markers remain visible; no deletion can originate from them.
            if isinstance(exc, OSError) or os.path.lexists(os.path.join(path, '.git')) or os.path.isfile(os.path.join(path, 'HEAD')):
                return {'path': path, 'reason': ws.failure_text(exc)}
            return None
    with cf.ThreadPoolExecutor(8) as pool:
        for result in pool.map(inspect, sources):
            if isinstance(result, dict):
                failures.append(result)
            elif result:
                main, entries = result
                mains[main] = entries
    # A copied gitdir may claim the same primary path with stale registrations.
    # Enumerate each actual primary again instead of letting the last copy win.
    primaries = sorted(mains)
    refreshed = {}
    with cf.ThreadPoolExecutor(8) as pool:
        for primary, result in zip(primaries, pool.map(inspect, primaries)):
            if isinstance(result, tuple) and result[0] == primary:
                refreshed[primary] = result[1]
            else:
                failures.append({'path': primary, 'reason': 'primary Git registration unavailable or changed'})
    mains = refreshed
    registered = {row['path'] for entries in mains.values() for row in entries}
    failed_paths = {canonical(row['path']) for row in failures}
    for path in sources:
        if os.path.isfile(os.path.join(path, '.git')) and canonical(path) not in registered and canonical(path) not in failed_paths:
            failures.append({'path': canonical(path), 'reason': 'gitfile is not a registered linked worktree (submodule/separate gitdir)'})
    return mains, failures


def linked_state(registration, context):
    path, main = registration['path'], registration['main']
    _, current = linked_registrations(main)
    live = next((r for r in current if r['path'] == path), None)
    if not live:
        raise ws.Keep('linked registration disappeared')
    if 'locked' in live:
        raise ws.Keep('locked worktree: ' + live['locked'])
    for name in ('environments', 'cwds'):
        for point, reason in context[name].items():
            if inside(point, path):
                raise ws.Keep(reason)
    if 'prunable' in live or not os.path.isdir(path):
        raise ws.Keep('missing/prunable worktree; Git prune decides registration cleanup')
    before = os.lstat(path)
    identity_reason = context.get('cwd_identities', {}).get(f'{before.st_dev}:{before.st_ino}')
    if identity_reason:
        raise ws.Keep(identity_reason)
    if not stat.S_ISDIR(before.st_mode) or path != canonical(path):
        raise ws.Keep('worktree directory identity unknown')
    common, gitdir, primary = metadata(path, 'linked')
    if primary != main:
        raise ws.Keep('worktree main changed')
    head = source_git(path, 'rev-parse', 'HEAD').stdout.strip()
    if head != live.get('HEAD'):
        raise ws.Keep('registered HEAD changed')
    latest = before.st_mtime
    def failed(exc):
        raise exc
    # Shared common-dir fetches/other worktrees do not refresh this worktree.
    for root in (path, gitdir):
        for parent, dirs, files in os.walk(root, followlinks=False, onerror=failed):
            checkpoint()
            if parent == path:
                dirs[:] = [d for d in dirs if d != '.git']
            # Git's read probes and transient lockfiles can refresh an admin
            # directory without changing HEAD/index/reflog or source files.
            names = ([*files, *(name for name in dirs if os.path.islink(os.path.join(parent, name)))]
                     if root == gitdir else [None, *dirs, *files])
            for name in names:
                checkpoint()
                info = os.lstat(os.path.join(parent, name) if name else parent)
                if root == path and stat.S_ISDIR(info.st_mode):
                    reason = context.get('cwd_identities', {}).get(f'{info.st_dev}:{info.st_ino}')
                    if reason:
                        raise ws.Keep(reason)
                latest = max(latest, info.st_mtime)
                if time.time() - latest < 86400:
                    raise ws.Keep('active within 24h')
    branch = live.get('branch')
    if branch:
        for name in (branch, 'logs/' + branch):
            target = os.path.join(common, name)
            if os.path.lexists(target):
                latest = max(latest, os.lstat(target).st_mtime)
    if time.time() - latest < 86400:
        raise ws.Keep('active within 24h')
    # Preview the same clean state Git requires. Git remove is still the final
    # judge, including submodules and ignored entries; never pass --force.
    if source_git(path, 'status', '--porcelain', '--untracked-files=all').stdout:
        raise ws.Keep('Git working tree is dirty (including untracked)')
    after = os.lstat(path)
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise ws.Keep('worktree directory changed')
    objects = {branch or 'HEAD': head}
    return dict(live, identity=[after.st_dev, after.st_ino], gitdir=gitdir, head=head, objects=objects,
                idle_hours=(time.time() - latest) / 3600)


def linked_plan(registration, context):
    row = dict(registration, action='keep', archives=[])
    try:
        row.update(linked_state(registration, context))
        repo = LiveRepo(row['main'], set(ws.DEFAULT_LOGINS))
        rejected = {}
        for name, (url, _) in list(repo.remotes.items()):
            target = tc.local_path(url, row['main'])
            if target:
                try:
                    target = canonical(target)
                    target_common = canonical(source_git(target, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip())
                    independent = not inside(target, row['path']) and target_common != canonical(repo.common)
                except Exception:
                    independent = False
                if not independent:
                    rejected[name] = 'local remote is self/shared storage or independence is unknown'
                    repo.remotes.pop(name)
        repo.dest = repo._archive_destination(set(ws.DEFAULT_LOGINS))
        row['urls'] = [pair[0] for pair in repo.remotes.values()]
        row['rejected_local_remotes'] = rejected
        row['remote_errors'] = {}
        proof = None
        # One live branch proof suffices. Do not wait for unrelated servers
        # after origin has already proved the checked-out commit.
        for name in sorted(repo.remotes, key=lambda value: (value != 'origin', value)):
            try:
                out = live_query(row['main'], ws.transport_url(repo.remotes[name][0]))
                refs = {ref: sha for sha, ref in (line.split('\t') for line in out.splitlines() if '\t' in line)
                        if ref.startswith('refs/heads/')}
                repo.live[name] = refs
                repo.by_sha.update({sha: f'{name}:{ref}' for ref, sha in refs.items()})
                proof = repo.proof(row['head'])
                if proof: break
            except Exception as exc:
                repo.live.pop(name, None)
                row['remote_errors'][name] = ws.failure_text(exc)
        row['remote_proofs'] = {key: proof for key in row['objects']}
        row['archive_objects'] = {} if proof else dict(row['objects'])
        if not proof:
            if not repo.dest:
                raise ws.Keep('unpushed branch and no personal or internal remote')
            url = ws.transport_url(repo.remotes[repo.dest][1])
            if any('mono2' in value.lower() for value in (row['path'], *[v[0] for v in repo.remotes.values()])) and ws.parse_remote(url)[0] == 'github.com':
                raise ws.Keep('mono2 must never be archived to GitHub')
            row['archive_url'] = url
        row['action'] = 'remove' if proof else 'archive+remove'
    except Exception as exc:
        row['reason'] = str(exc) if isinstance(exc, ws.Keep) else 'check unknown: ' + ws.failure_text(exc)
    return row


def linked_execute(rows, log, stamp):
    for row in rows:
        if row['action'] == 'keep':
            continue
        try:
            LIVE.clear()
            context = linked_context()
            fresh = linked_plan(row, context)
            if fresh['action'] == 'keep':
                raise ws.Keep(fresh['reason'])
            for key in ('identity', 'head', 'gitdir', 'main', 'objects'):
                if fresh[key] != row[key]:
                    raise ws.Keep(key + ' changed after preview')
            for key in ('remote_proofs', 'archive_objects', 'urls', 'rejected_local_remotes', 'remote_errors'):
                row[key] = fresh[key]
            for source, sha in fresh['archive_objects'].items():
                ref = f'refs/heads/archive/git-tree/{ws.archive_slug(row["path"])}-{stamp}/' + re.sub(r'[^A-Za-z0-9-]', '-', source)
                url = fresh['archive_url']
                source_git(row['main'], 'push', url, f'{sha}:{ref}')
                proof = source_git(row['main'], 'ls-remote', url, ref).stdout.split()
                if not proof or proof[0] != sha:
                    raise ws.Keep('archive SHA verification failed')
                archive = {'source': source, 'sha': sha, 'url': url, 'ref': ref}
                row['archives'].append(archive)
                persist(log, dict(row, event='archive_verified'))
            # BB/cwd/registration/age/cleanliness are live after network IO.
            current = linked_state(row, linked_context())
            for key in ('identity', 'head', 'gitdir', 'main', 'objects'):
                if current[key] != row[key]:
                    raise ws.Keep(key + ' changed before Git remove')
            persist(log, dict(row, event='remove_intent'))
            with physical_parent(row['path']) as (fd, basename):
                actual = os.stat(basename, dir_fd=fd, follow_symlinks=False)
                if [actual.st_dev, actual.st_ino] != row['identity']:
                    raise ws.Keep('directory identity changed')
                source_git(row['main'], 'worktree', 'remove', '--', current['registered_path'])
            row['result'] = 'removed'
        except Exception as exc:
            row.update(result='kept', reason=ws.failure_text(exc))
        persist(log, row)


def linked_main(argv=None):
    global DEADLINE
    parser = argparse.ArgumentParser(description='Registered linked worktrees: Git remove, 24h idle; independent clones use git_tree_sweep.py.')
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--reviewed-preview')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--budget-minutes', type=float, default=15)
    parser.add_argument('--state-dir', default=os.path.expanduser('~/.local/state/devcloud-storage-upkeep'))
    args = parser.parse_args(argv)
    started = time.monotonic(); DEADLINE = started + args.budget_minutes * 60; LIVE.clear()
    # The linked entry is a separate process; retain host config but discard
    # inherited repository/config injection, just like source_git.
    ws.ENV = source_environment()
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    os.makedirs(args.state_dir, mode=0o700, exist_ok=True)
    output = Path(args.state_dir, f'linked-worktree-sweep-{stamp}.json'); log = output.with_suffix('.jsonl')
    with open(Path(args.state_dir, 'git-tree-sweep.lock'), 'a') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Git sweep busy; skipped'); return 0
        persist(log, {'event':'start','timestamp':stamp,'mode':'execute' if args.execute else 'dry-run','policy':'linked','bb_cli':os.environ.get('BB_CLI')})
        rows, mains, failures, error, indexed, comparison, prunes = [], {}, [], None, None, None, []
        try:
            context = linked_context(); sources, indexed = linked_known_sources(args.state_dir, context)
            mains, failures = linked_inventory(sources)
            registrations = [row for entries in mains.values() for row in entries]
            with cf.ThreadPoolExecutor(8) as pool:
                rows = list(pool.map(lambda row: linked_plan(row, context), registrations))
            registered_paths = {row['path'] for row in rows}
            rows.extend(dict(failure, type='linked', action='keep', archives=[], reason='Git registration unavailable: ' + failure['reason'])
                        for failure in failures if failure['path'] not in registered_paths and os.path.isfile(os.path.join(failure['path'], '.git')))
            def measure(row):
                try:
                    out = command(['du','-sx','-B1','--',row['path']],timeout=60,check=False)
                    row['bytes'] = int(out.stdout.split()[0]) if out.returncode == 0 else None
                except Exception: row['bytes'] = None
            with cf.ThreadPoolExecutor(8) as pool: list(pool.map(measure, rows))
            persist(log, {'event':'planned','seconds':time.monotonic()-started,'summary':summarize(rows)})
            if args.reviewed_preview:
                prior = json.loads(Path(args.reviewed_preview).read_text())
                if prior.get('policy') != 'linked': raise ws.Keep('reviewed preview is not a linked preview')
                comparison = reviewed_comparison(rows,args.reviewed_preview)
                if comparison['added']: raise ws.Keep('new linked candidates outside reviewed preview; no actions performed')
            if args.execute: linked_execute(rows,log,stamp)
            for main in mains:
                try:
                    flags = [] if args.execute else ['--dry-run']
                    out = source_git(main,'worktree','prune','--verbose',*flags)
                    entry = {'main':main,'mode':'execute' if args.execute else 'dry-run','result':'ok','output':out.stdout+out.stderr}
                except Exception as exc: entry = {'main':main,'result':'kept','reason':ws.failure_text(exc)}
                prunes.append(entry); persist(log,dict(entry,event='prune'))
        except Exception as exc: error = ws.failure_text(exc)
        if time.monotonic() >= DEADLINE: error = error or 'time budget reached'
        report = {'schema_version':3,'policy':'linked','mode':'execute' if args.execute else 'dry-run','timestamp':stamp,'bb_cli':os.environ.get('BB_CLI'),
                  'invocation_id':os.environ.get('INVOCATION_ID'),'complete':not bool(error),'error':error,'indexed_discovery':indexed,'mains':sorted(mains),
                  'unresolved_mains':failures,'items':rows,'prunes':prunes,'reviewed_comparison':comparison,
                  'timings':{'total_seconds':round(time.monotonic()-started,3)},'summary':summarize(rows),'output':str(output)}
        with open(output,'w',opener=lambda p,flags:os.open(p,flags,0o600)) as handle: json.dump(report,handle,ensure_ascii=True,indent=2)
        for row in rows: persist(log,row)
        persist(log, {'event': 'finish', 'complete': report['complete'], 'summary': report['summary']})
        print(json.dumps(report if args.json else {key:report[key] for key in ('policy','mode','complete','error','timings','summary','output')}))
        return 1 if error else 0


def main(argv=None):
    global DEADLINE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--reviewed-preview', help='First execution batch: stop before archive/removal if new candidates appear.')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--budget-minutes', type=float, default=30)
    parser.add_argument('--state-dir', default=os.path.expanduser('~/.local/state/devcloud-storage-upkeep'))
    args = parser.parse_args(argv)
    started = time.monotonic()
    started_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    LIVE.clear()
    DEADLINE = started + args.budget_minutes * 60
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
    os.makedirs(args.state_dir, mode=0o700, exist_ok=True)
    output = Path(args.state_dir, f'git-tree-sweep-{stamp}.json')
    log = Path(args.state_dir, f'git-tree-sweep-{stamp}.jsonl')
    with open(Path(args.state_dir, 'git-tree-sweep.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('git tree sweep busy; skipped')
            return 0
        roots = ['/', '/data']
        excluded, error = set(), None
        persist(log, {'event': 'start', 'timestamp': stamp, 'mode': 'execute' if args.execute else 'dry-run', 'bb_cli': os.environ.get('BB_CLI')})
        try:
            excluded = managed_roots()
        except Exception as exc:
            error = 'managed storage unknown: ' + ws.failure_text(exc)
        inventory = discover(roots, excluded)
        inventory['parent_git_states'] = parent_git_inventory(inventory['trees'])
        inventory['invalid_parent_markers'] = [
            {'path': os.path.join(parent, '.git'), 'reason': detail,
             'recommendation': 'inspect and remove separately only after confirming it is unused'}
            for parent, (state, detail) in inventory['parent_git_states'].items() if state == 'invalid'
        ]
        inventory['linked_markers'] = [path for path, kind in inventory['trees'].items() if kind == 'linked']
        inventory['marker_counts'] = {kind: sum(value == kind for value in inventory['trees'].values()) for kind in set(inventory['trees'].values())}
        timings = {'discovery_seconds': round(time.monotonic() - started, 3)}
        persist(log, {'event': 'discovered', 'seconds': timings['discovery_seconds'],
                      'trees': len(inventory['trees']), 'directories': inventory['directories'], 'errors': inventory['errors']})
        global_inventory = {'started_at': started_at,
                            'discovered_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'scope': 'round'}
        invalid_parents = {path for path, (state, _) in inventory['parent_git_states'].items()
                           if state == 'invalid'}
        # Diagnostic markers are not Git trees. Never charge a container's
        # entire footprint to its invalid marker or send it to a deletion plan.
        paths = sorted(path for path, kind in inventory['trees'].items()
                       if kind != 'linked' and path not in invalid_parents)
        # Capture processes before starting our own du workers: their argv/fds
        # must not become long-lived business anchors in this round snapshot.
        process_started = time.monotonic()
        process_error, process_snapshot = None, None
        try:
            process_points = {}
            process_snapshot = (process_points, process_anchors(process_points))
        except Exception as exc:
            process_error = ws.failure_text(exc)
        timings['process_snapshot_seconds'] = round(time.monotonic() - process_started, 3)
        global_inventory['processes_checked_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        measurement_started = time.monotonic()
        def measure(path):
            try:
                out = command(['du', '-sx', '-B1', '--', path], timeout=180, check=False)
                size = int(out.stdout.split()[0]) if out.returncode == 0 else None
            except Exception:
                size = None
            return size, time.monotonic()
        # du does not depend on reference/config parsing or on the plan. Its
        # bounded workers overlap that work; all results join before execution.
        with cf.ThreadPoolExecutor(8) as measurements:
            sizes = {path: measurements.submit(measure, path) for path in paths}
            anchors, refs, unknown = {}, {}, []
            try:
                if inventory['errors']:
                    raise ws.ScanIncomplete('; '.join(inventory['errors'][:10]))
                if process_error:
                    raise ws.ScanIncomplete('process snapshot incomplete: ' + process_error)
                phase_started = time.monotonic()
                refs = references(inventory)
                timings['references_seconds'] = round(time.monotonic() - phase_started, 3)
                phase_started = time.monotonic()
                anchors, unknown = usage(inventory, refs, process_snapshot=process_snapshot)
                timings['usage_seconds'] = round(time.monotonic() - phase_started, 3)
            except Exception as exc:
                error = 'usage/reference inventory incomplete: ' + ws.failure_text(exc)
            if global_unknown(unknown):
                error = 'unresolved global config references: ' + '; '.join(global_unknown(unknown))
            global_inventory['references_checked_at'] = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
            inventory['snapshot_at'] = global_inventory['references_checked_at']
            timings['inventory_seconds'] = round(time.monotonic() - started, 3)
            persist(log, {'event': 'references_checked', 'seconds': timings['inventory_seconds'], 'error': error,
                          'global_inventory': global_inventory})
            phase_started = time.monotonic()
            with cf.ThreadPoolExecutor(8) as pool:
                rows = list(pool.map(lambda path: plan(path, inventory['trees'][path], inventory, anchors, refs, error), paths))
            timings['planning_seconds'] = round(time.monotonic() - phase_started, 3)
            timings['plan_seconds'] = round(time.monotonic() - started, 3)
            persist(log, {'event': 'planned', 'seconds': timings['plan_seconds'], 'summary': summarize(rows)})
            wait_started = time.monotonic()
            measured = {path: future.result() for path, future in sizes.items()}
            for row in rows:
                row['bytes'] = measured[row['path']][0]
            timings['measurement_wait_seconds'] = round(time.monotonic() - wait_started, 3)
            timings['measurement_seconds'] = round(max((finished for _, finished in measured.values()), default=measurement_started) - measurement_started, 3)
        comparison = None
        if args.reviewed_preview:
            try:
                comparison = reviewed_comparison(rows, args.reviewed_preview)
                persist(log, dict(comparison, event='reviewed_comparison'))
                if comparison['added']:
                    error = 'new candidates outside reviewed preview; no archive or removal performed'
            except Exception as exc:
                error = 'reviewed preview unknown: ' + ws.failure_text(exc)
        phase_started = time.monotonic()
        if args.execute and not error:
            execute(rows, inventory, anchors, refs, roots, excluded, stamp, log)
        timings['execution_seconds'] = round(time.monotonic() - phase_started, 3)
        if time.monotonic() >= DEADLINE:
            error = error or 'time budget reached; partial plan retained'
        report = {'schema_version': 2, 'policy': 'clones', 'timestamp': stamp, 'mode': 'execute' if args.execute else 'dry-run', 'bb_cli': os.environ.get('BB_CLI'),
                  'invocation_id': os.environ.get('INVOCATION_ID'), 'roots': roots, 'complete': not bool(error or inventory['errors']), 'error': error,
                  'global_inventory': global_inventory,
                  'unresolved': unknown, 'global_unresolved': global_unknown(unknown),
                  'reviewed_comparison': comparison,
                  'discovery': {k: v for k, v in inventory.items() if k not in ('trees', 'links', 'remote_urls', 'parent_git_states')},
                  'timings': dict(timings, total_seconds=round(time.monotonic() - started, 3)),
                  'summary': summarize(rows), 'items': rows, 'output': str(output)}
        with open(output, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
            json.dump(report, handle, ensure_ascii=True, indent=2)
        for row in rows:
            persist(log, row)
        persist(log, {'event': 'finish', 'complete': report['complete'], 'summary': report['summary']})
        print(json.dumps(report if args.json else {k: report[k] for k in ('mode', 'complete', 'error', 'timings', 'summary', 'output')}, ensure_ascii=True))
        return 1 if not report['complete'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
