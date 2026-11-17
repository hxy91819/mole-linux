#!/usr/bin/env python3
"""Inventory all local Git working trees; dry-run unless --execute is supplied.

Owner policy 2026-11-17. Discovery is physical, unbounded in depth and -xdev
per disk. Legacy entrypoints supply helpers only; they are not scheduled.
"""
from __future__ import annotations

import argparse
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


def live_query(path, url, symref=False):
    """One live server query per effective URL in a planning generation."""
    key = (url, symref)
    with LIVE_LOCK:
        entry = LIVE.setdefault(key, {'lock': threading.Lock()})
    with entry['lock']:
        if 'result' not in entry:
            try:
                entry['result'] = git(path, 'ls-remote', *(['--symref'] if symref else ['--heads', '--tags']),
                                      url, *(['HEAD'] if symref else []), timeout=90).stdout
            except Exception as exc:
                entry['result'] = exc
        if isinstance(entry['result'], Exception):
            raise entry['result']
        return entry['result']


class LiveRepo(ws.Repo):
    def load_live(self, include_tags=True):
        for name, (url, _) in self.remotes.items():
            try:
                out = live_query(self.main, ws.transport_url(url))
            except Exception:
                self.unreachable.add(name)
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
                out = live_query(self.main, ws.transport_url(self.remotes[name][0]), symref=True)
                match = re.match(r'ref: refs/heads/(\S+)\tHEAD', out)
                branch = match[1] if match else None
            self._default[name] = branch
        return self._default[name]


def checkpoint():
    if time.monotonic() >= DEADLINE:
        raise ws.ScanIncomplete('time budget reached')


def command(args, cwd=None, timeout=120, check=True, env=None):
    checkpoint()
    return _run(args, cwd=cwd, timeout=max(.01, min(timeout, DEADLINE - time.monotonic())), check=check, env=env)


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
        elif record.get('project_id') and (record.get('project_paths') or record.get('project_remote_ids')):
            record['scope'] = 'project'
            paths = record.get('project_paths', [])
            identities = set(record.get('project_remote_ids', []))
            source_trees = {tree for tree in trees if any(inside(path, tree) for path in paths)}
            for tree in source_trees:
                identities.update(urls.get(tree, []))
            protected.update(source_trees)
            protected.update(tree for tree, remotes in urls.items() if identities.intersection(remotes) and tree in trees)
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
    for root in roots:
        root = canonical(root)
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
            'excluded': skipped, 'directories': len(visited)}


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
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
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


def process_anchors(anchors, proc='/proc'):
    environments = ProcessEnvironments()
    mounts = process_mounts()
    for pid in os.listdir(proc):
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
                        process_directory(anchors, environments.scopes, target, login_home, mounts, f'process {pid} cwd')
                    anchor(anchors, target, f'process {pid}')
                except FileNotFoundError:
                    pass
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


def configuration_name_anchors(anchors, inventory, sources=None):
    names = {}
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
                    collision = len(names[name]) > 1
                    label = 'collision' if collision else 'reference'
                    anchor(anchors, tree, f'configuration name {label}: {name} in {path}')
                    evidence.append({'path': tree, 'name': name, 'config': path, 'collision': collision})
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
    for project in projects:
        if not isinstance(project.get('sources'), list):
            raise ws.ScanIncomplete('BB project sources unknown')
        for source in project['sources']:
            if source.get('path'):
                anchor(anchors, source['path'], f'BB project source: {project["id"]}')
    for env in environments:
        if env.get('status') != 'destroyed' and (env.get('lifecycle') or {}).get('phase') != 'destroyed':
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
                     e.get('status') != 'destroyed' and (e.get('lifecycle') or {}).get('phase') != 'destroyed' and
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
            selected = directory.get('environmentId') or execution.get('environmentId')
            selected_paths = [canonical(e['path']) for e in environments if e.get('id') == selected and e.get('path')]
            if selected_paths:
                metadata['PWD'] = selected_paths
            records = environment_references(anchors, text, (execution.get('env') or {}).get('HOME', '/root'),
                                              reason, environments_values, metadata)
            if records and project_identities is None:
                project_identities = project_url_identities(project['gitRemoteUrl'], project_paths) if project.get('gitRemoteUrl') else []
            for record in records:
                record.update(project_id=project['id'], automation_id=task['id'],
                              automation_enabled=task.get('enabled'), project_paths=sorted(set(project_paths + env_paths)),
                              project_remote_ids=project_identities)
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


def usage(inventory):
    anchors = {}
    environments = process_anchors(anchors)
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
    unknown = bb_anchors(anchors, environments) + config_anchors(anchors, environments=environments, inventory=inventory)
    symlink_anchors(anchors, inventory)
    with open('/proc/self/mountinfo') as handle:
        for line in handle:
            fields = line.split()
            if len(fields) < 6:
                raise ws.ScanIncomplete('mount inventory incomplete')
            mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4])
            anchor(anchors, mount, 'mounted descendant')
    return anchors, scope_unknown(anchors, unknown, inventory)


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


def local_state(path, kind, inventory, anchors, refs):
    checkpoint()
    if path != canonical(path) or not stat.S_ISDIR(os.lstat(path).st_mode):
        raise ws.Keep('symlink or nonphysical directory')
    if os.path.ismount(path):
        raise ws.Keep('filesystem root or mount point')
    parent = os.path.dirname(path)
    while parent != path:
        if parent in inventory['trees']:
            raise ws.Keep(f'嵌套于 {parent}，由该项目自行保留')
        if parent == '/': break
        parent = os.path.dirname(parent)
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
        if os.path.exists(os.path.join(directory, 'objects/info/alternates')):
            raise ws.Keep('repository uses alternates')
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
        for name in dirs + files:
            latest = max(latest, os.lstat(os.path.join(parent, name)).st_mtime)
    for directory in dict.fromkeys([common, gitdir]):
        # Ref/reflog/FETCH_HEAD changes are use too, even if checkout files and
        # HEAD/index did not change. Object payload and sibling metadata are
        # not traversed; their shared refs and this worktree's metadata are.
        for parent, dirs, files in os.walk(directory, followlinks=False, onerror=failed):
            checkpoint()
            dirs[:] = [d for d in dirs if d not in ('objects', 'worktrees', 'modules')]
            for name in [None, *dirs, *files]:
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
    # No dependency or main-checkout exemption for ignored credentials.
    out = git(path, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard', timeout=600).stdout
    for rel in out.split('\0'):
        if rel and ws.SECRET_NAME.search(rel) and not ws.SECRET_TEMPLATE.search(rel):
            raise ws.Keep(f'ignored credential: {rel}')
    head = git(path, 'rev-parse', 'HEAD').stdout.strip()
    objects = dict(line.split(None, 1) for line in git(path, 'for-each-ref',
                   '--format=%(refname) %(objectname)', 'refs/heads', 'refs/tags').stdout.splitlines())
    objects['HEAD'] = head
    info = os.lstat(path)
    return {'head': head, 'objects': objects, 'main': primary, 'gitdir': gitdir,
            'identity': [info.st_dev, info.st_ino], 'idle_hours': (time.time() - latest) / 3600}


def plan(path, kind, inventory, anchors, refs, error=None):
    item = {'path': path, 'type': kind, 'action': 'keep', 'archives': []}
    try:
        item.update(local_state(path, kind, inventory, anchors, refs))
        if error:
            raise ws.Keep(error)
        repo = LiveRepo(path, set(ws.DEFAULT_LOGINS))
        # A remote inside this tree vanishes with it. External local remotes
        # participate in the full reference index, which keeps their containing
        # checkout; bare stores are reference sources, never deletion targets.
        for name, (url, _) in list(repo.remotes.items()):
            target = tc.local_path(url, path)
            if target and inside(canonical(target), path):
                repo.remotes.pop(name)
                if repo.dest == name:
                    repo.dest = None
        repo.load_live(include_tags=True)
        if repo.unreachable:
            raise ws.Keep('remote query incomplete: ' + ', '.join(sorted(repo.unreachable)))
        if not repo.live:
            raise ws.Keep('no independently recoverable remote')
        item['remote_proofs'] = {ref: (repo.by_sha.get(sha) if ref.startswith('refs/tags/') else repo.proof(sha))
                                 for ref, sha in item['objects'].items()}
        missing = {ref: sha for ref, sha in item['objects'].items() if not item['remote_proofs'][ref]}
        item['archive_objects'] = missing
        item['urls'] = [pair[0] for pair in repo.remotes.values()]
        if missing:
            if not repo.dest:
                raise ws.Keep('unpushed refs and no personal or internal remote')
            url = ws.transport_url(repo.remotes[repo.dest][1])
            if any('mono2' in s.lower() for s in [path, *item['urls']]) and ws.parse_remote(url)[0] == 'github.com':
                raise ws.Keep('mono2 must never be archived to GitHub')
            item['archive_url'] = url
        item['action'] = 'archive+remove' if missing else 'remove'
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


def remove(item, inventory, anchors, refs):
    path = item['path']
    local = local_state(path, item['type'], inventory, anchors, refs)
    for key in ('identity', 'objects', 'gitdir', 'main'):
        if local[key] != item[key]:
            raise ws.Keep(f'{key} changed after plan')
    with physical_parent(path) as (fd, name):
        current = os.stat(name, dir_fd=fd, follow_symlinks=False)
        if [current.st_dev, current.st_ino] != item['identity']:
            raise ws.Keep('directory identity changed')
        if item['type'] == 'linked':
            # Git refuses dirty/locked/submodule worktrees; never add --force.
            git(item['main'], 'worktree', 'remove', '--', path, timeout=600)
        else:
            if not shutil.rmtree.avoids_symlink_attacks:
                raise ws.Keep('fd-based deletion unavailable')
            shutil.rmtree(name, dir_fd=fd)
    item['result'] = 'removed'


def persist(path, value):
    with open(path, 'a', opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
        handle.write(json.dumps(value, ensure_ascii=True) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def execute(rows, inventory, anchors, refs, roots, excluded, stamp, log):
    prepared = []
    for row in rows:
        if row['action'] == 'keep':
            continue
        try:
            if row.get('bytes') is None:
                raise ws.Keep('size measurement incomplete')
            current_anchors, unknown = usage(inventory)
            if global_unknown(unknown):
                raise ws.Keep('unresolved global config references: ' + '; '.join(global_unknown(unknown)))
            LIVE.clear()  # New remote-proof generation for this execution preparation.
            fresh = plan(row['path'], row['type'], inventory, current_anchors, refs)
            if fresh['action'] == 'keep':
                raise ws.Keep(fresh['reason'])
            if fresh['objects'] != row['objects'] or fresh['identity'] != row['identity']:
                raise ws.Keep('local evidence changed')
            for source, sha in fresh['archive_objects'].items():
                prefix = 'refs/tags' if source.startswith('refs/tags/') else 'refs/heads'
                ref = f'{prefix}/archive/git-tree/{ws.archive_slug(row["path"])}-{stamp}/' + re.sub(r'[^A-Za-z0-9-]', '-', source)
                url = fresh['archive_url']
                # Exact objects only: deliberately bypass the old squash fallback.
                git(row['path'], 'push', url, f'{sha}:{ref}', timeout=900)
                proof = git(row['path'], 'ls-remote', url, ref).stdout.split()
                if not proof or proof[0] != sha:
                    raise ws.Keep('archive SHA verification failed')
                row['archives'].append({'source': source, 'url': url, 'ref': ref, 'sha': sha})
                persist(log, dict(row, event='archive_verified'))
            prepared.append(row)
        except Exception as exc:
            row.update(result='kept', reason=ws.failure_text(exc))
            persist(log, row)
    # All remote IO has finished. Rewalk the disks and refresh EVERY consumer.
    if not prepared:
        return
    fresh_inventory = discover(roots, excluded)
    error = '; '.join(fresh_inventory['errors'])
    try:
        if error:
            raise ws.ScanIncomplete(error)
        final_refs = references(fresh_inventory)
        final_anchors, unknown = usage(fresh_inventory)
        if global_unknown(unknown):
            raise ws.ScanIncomplete('unresolved global config references: ' + '; '.join(global_unknown(unknown)))
    except Exception as exc:
        error = ws.failure_text(exc)
    for row in prepared:
        try:
            if error:
                raise ws.Keep('final inventory incomplete: ' + error)
            # Recheck process/BB/config at each sink; no network Git calls here.
            current_anchors, unknown = usage(fresh_inventory)
            if global_unknown(unknown):
                raise ws.Keep('unresolved global config references: ' + '; '.join(global_unknown(unknown)))
            current_anchors.update(final_anchors)
            persist(log, dict(row, event='remove_intent'))
            remove(row, fresh_inventory, current_anchors, final_refs)
        except Exception as exc:
            row.update(result='kept', reason=ws.failure_text(exc))
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
    # Nested rows overlap: totals are inventory sizes, not additive freed space.
    return result


def reviewed_comparison(rows, preview_path):
    preview = json.loads(Path(preview_path).read_text())
    if preview.get('mode') != 'dry-run' or preview.get('complete') is not True or preview.get('error'):
        raise ws.Keep('reviewed preview is not a complete dry-run')
    allowed = {(item['path'], item['type']) for item in preview['items'] if item['action'] in ('remove', 'archive+remove')}
    added = [{'path': item['path'], 'type': item['type']} for item in rows
             if item['action'] != 'keep' and (item['path'], item['type']) not in allowed]
    return {'preview': str(preview_path), 'allowed': len(allowed), 'added': added}


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
        persist(log, {'event': 'start', 'timestamp': stamp, 'mode': 'execute' if args.execute else 'dry-run'})
        try:
            excluded = managed_roots()
        except Exception as exc:
            error = 'managed storage unknown: ' + ws.failure_text(exc)
        inventory = discover(roots, excluded)
        timings = {'discovery_seconds': round(time.monotonic() - started, 3)}
        persist(log, {'event': 'discovered', 'seconds': timings['discovery_seconds'],
                      'trees': len(inventory['trees']), 'directories': inventory['directories'], 'errors': inventory['errors']})
        anchors, refs, unknown = {}, {}, []
        try:
            if inventory['errors']:
                raise ws.ScanIncomplete('; '.join(inventory['errors'][:10]))
            refs = references(inventory)
            anchors, unknown = usage(inventory)
        except Exception as exc:
            error = 'usage/reference inventory incomplete: ' + ws.failure_text(exc)
        if global_unknown(unknown):
            error = 'unresolved global config references: ' + '; '.join(global_unknown(unknown))
        timings['inventory_seconds'] = round(time.monotonic() - started, 3)
        persist(log, {'event': 'references_checked', 'seconds': timings['inventory_seconds'], 'error': error})
        with cf.ThreadPoolExecutor(8) as pool:
            rows = list(pool.map(lambda p: plan(p, inventory['trees'][p], inventory, anchors, refs, error), sorted(inventory['trees'])))
        timings['plan_seconds'] = round(time.monotonic() - started, 3)
        persist(log, {'event': 'planned', 'seconds': timings['plan_seconds'], 'summary': summarize(rows)})
        # Measurement is bounded too; failures remain null, never zero.
        def measure(row):
            try:
                out = command(['du', '-sx', '-B1', '--', row['path']], timeout=180, check=False)
                row['bytes'] = int(out.stdout.split()[0]) if out.returncode == 0 else None
            except Exception:
                row['bytes'] = None
        with cf.ThreadPoolExecutor(8) as pool:
            list(pool.map(measure, rows))
        comparison = None
        if args.reviewed_preview:
            try:
                comparison = reviewed_comparison(rows, args.reviewed_preview)
                persist(log, dict(comparison, event='reviewed_comparison'))
                if comparison['added']:
                    error = 'new candidates outside reviewed preview; no archive or removal performed'
            except Exception as exc:
                error = 'reviewed preview unknown: ' + ws.failure_text(exc)
        if args.execute and not error:
            execute(rows, inventory, anchors, refs, roots, excluded, stamp, log)
        if time.monotonic() >= DEADLINE:
            error = error or 'time budget reached; partial plan retained'
        report = {'schema_version': 2, 'timestamp': stamp, 'mode': 'execute' if args.execute else 'dry-run',
                  'roots': roots, 'complete': not bool(error or inventory['errors']), 'error': error,
                  'unresolved': unknown, 'global_unresolved': global_unknown(unknown),
                  'reviewed_comparison': comparison,
                  'discovery': {k: v for k, v in inventory.items() if k not in ('trees', 'links', 'remote_urls')},
                  'timings': dict(timings, total_seconds=round(time.monotonic() - started, 3)),
                  'summary': summarize(rows), 'items': rows, 'output': str(output)}
        with open(output, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
            json.dump(report, handle, ensure_ascii=True, indent=2)
        for row in rows:
            persist(log, row)
        print(json.dumps(report if args.json else {k: report[k] for k in ('mode', 'complete', 'error', 'timings', 'summary', 'output')}, ensure_ascii=True))
        return 1 if not report['complete'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
