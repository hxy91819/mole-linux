#!/usr/bin/env python3
"""Legacy clone helpers; CLI delegates to the full-disk git_tree_sweep.py.

Default is read-only. Shared process, Git proof, transport and archive helpers
come from the sibling worktree sweeper, including in isolated (-I) execution.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import time
from urllib.parse import unquote, urlparse

_spec = importlib.util.spec_from_file_location('tmp_clone_worktree', Path(__file__).with_name('worktree_sweep.py'))
ws = importlib.util.module_from_spec(_spec)
# Execute the hashed sibling source without writing or trusting a bytecode cache.
exec(compile(Path(_spec.origin).read_bytes(), _spec.origin, 'exec'), ws.__dict__)
ROOTS = ('/data/system-tmp', '/var/tmp')
IDLE_SECONDS = 86400


def usage_anchors():
    anchors = {}
    ws.process_anchors(anchors)
    ws.docker_anchors(anchors)
    # Unlike the worktree rule, ANY extant BB environment reserves a tmp clone.
    envs = json.loads(ws.run(['bb', 'environment', 'list', '--json'], timeout=120).stdout)
    if not isinstance(envs, list):
        raise ws.ScanIncomplete('BB environment scan incomplete')
    for env in envs:
        if env.get('status') != 'destroyed' and env.get('path'):
            ws.add_anchor(anchors, env['path'], f"BB environment {env['id']}")
    # Host bind mounts are also references (not necessarily Docker-owned).
    with open('/proc/self/mountinfo') as handle:
        for line in handle:
            fields = line.split()
            if len(fields) < 6:
                raise ws.ScanIncomplete('mount scan incomplete')
            mount = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), fields[4])
            ws.add_anchor(anchors, mount, 'mounted descendant')
    return anchors


def physical_clone(path, roots):
    path = os.path.abspath(path)
    if path != os.path.realpath(path) or not stat.S_ISDIR(os.lstat(path).st_mode):
        raise ws.Keep('symlink or non-directory clone')
    if os.path.dirname(path) not in {os.path.realpath(r) for r in roots}:
        raise ws.Keep('outside tmp roots or not top-level')
    dotgit = os.path.join(path, '.git')
    if not stat.S_ISDIR(os.lstat(dotgit).st_mode):
        raise ws.Keep('not an independent clone (.git is not a directory)')
    return path


def tree_evidence(path):
    """Latest mtime of every entry except .git, without following symlinks."""
    latest = os.lstat(path).st_mtime
    def failed(exc):
        raise exc
    for parent, dirs, files in os.walk(path, followlinks=False, onerror=failed):
        if parent == path:
            dirs[:] = [d for d in dirs if d != '.git']
            files = [f for f in files if f != '.git']
        if '.git' in dirs or '.git' in files:
            raise ws.Keep('nested .git')
        for name in dirs + files:
            latest = max(latest, os.lstat(os.path.join(parent, name)).st_mtime)
    return latest


def local_path(value, base):
    if value.startswith('file://'):
        parsed = urlparse(value)
        if parsed.netloc not in ('', 'localhost'):
            return None
        value = unquote(parsed.path)
    elif re.match(r'^[\w+.-]+://|^[^/]+@[^:]+:', value):
        return None
    elif ':' in value and not value.startswith('/'):
        return None
    return os.path.realpath(os.path.join(base, os.path.expanduser(value)))


def reference_index(roots, candidates):
    """All repositories, including nested/linked ones; errors deny this round.

    Read remote URLs directly through Git config (including multiple URLs and
    pushurls); resolve alternates relative to their object directory.
    """
    refs = {}
    candidates = {os.path.realpath(p) for p in candidates}
    def record(target, source, why):
        if not target:
            return
        for candidate in candidates:
            if source != candidate and any(t == candidate or t.startswith(candidate + os.sep) for t in ws.aliases(target)):
                refs[candidate] = f'{why}: {source}'
    def failed(exc):
        # Repositories may disappear during another maintenance session.
        if not isinstance(exc, FileNotFoundError):
            raise exc
    def scan(root):
        for parent, dirs, files in os.walk(root, followlinks=False, onerror=failed):
            bare = 'HEAD' in files and 'config' in files and 'objects' in dirs and 'refs' in dirs
            if '.git' not in dirs and '.git' not in files and not bare:
                continue
            dirs[:] = [d for d in dirs if d != '.git']
            marker = parent if bare else os.path.join(parent, '.git')
            if os.path.isfile(marker):
                with open(marker, errors='surrogateescape') as handle:
                    contents = handle.read()
                if not contents.startswith('gitdir: '):
                    # uv sdist caches and test fixtures use .git as a plain
                    # marker. Git rejects these; they are not repositories.
                    continue
                target = os.path.join(parent, contents[len('gitdir: '):].rstrip('\r\n'))
                try:
                    if not stat.S_ISDIR(os.stat(target).st_mode):
                        continue
                except FileNotFoundError:
                    # A dead linked-worktree gitfile has no readable Git
                    # metadata and cannot use a remote or object alternate.
                    continue
            probe = ws.git(parent, 'rev-parse', '--path-format=absolute', '--git-common-dir', timeout=30, check=False)
            if probe.returncode == 0:
                common = probe.stdout.strip()
                results = [ws.git(parent, 'config', '--get-regexp', r'^remote\..*\.(url|pushurl)$', check=False, timeout=30)]
            else:
                if 'not a git repository' not in probe.stderr:
                    raise ws.ScanIncomplete(f'reference scan incomplete: {parent}: {ws.failure_text(probe.stderr)}')
                # Some package/test fixtures have an empty .git directory.
                # Inspect any surviving config/alternates directly, so partial
                # metadata with a real dependency still protects the clone.
                gitdir = target if os.path.isfile(marker) else marker
                try:
                    with open(os.path.join(gitdir, 'commondir')) as handle:
                        common = os.path.realpath(os.path.join(gitdir, handle.read().rstrip('\r\n')))
                except FileNotFoundError:
                    common = gitdir
                results = []
                for config in dict.fromkeys([os.path.join(common, 'config'), os.path.join(gitdir, 'config.worktree')]):
                    try:
                        os.stat(config)
                    except FileNotFoundError:
                        continue
                    results.append(ws.git(parent, 'config', '--file', config, '--includes', '--get-regexp',
                                          r'^remote\..*\.(url|pushurl)$', check=False, timeout=30))
            for result in results:
                if result.returncode not in (0, 1):
                    raise ws.ScanIncomplete(f'remote reference scan incomplete: {parent}')
                for line in result.stdout.splitlines():
                    _, value = line.split(None, 1)
                    record(local_path(value, parent), os.path.realpath(parent), 'remote URL reference')
            alternate = os.path.join(common, 'objects/info/alternates')
            try:
                with open(alternate) as handle:
                    for line in handle:
                        value = line.strip()
                        if value.startswith(chr(34)):
                            value = json.loads(value)
                        record(local_path(value, os.path.join(common, 'objects')), os.path.realpath(parent), 'objects alternates reference')
            except FileNotFoundError:
                pass
    jobs = []
    for root in dict.fromkeys(os.path.realpath(r) for r in roots):
        if not os.path.isdir(root):
            continue
        with os.scandir(root) as entries:
            jobs.extend(e.path for e in entries if e.is_dir(follow_symlinks=False))
    with cf.ThreadPoolExecutor(8) as pool:
        list(pool.map(scan, jobs))
    return refs


def ignored_credential(path):
    # No main-checkout exemption for independent clones. Inspect all ignored
    # entries including dependency trees; never follow a symlink out of the repo.
    out = ws.git(path, 'ls-files', '-z', '--others', '--ignored', '--exclude-standard', timeout=600).stdout
    for rel in out.split('\0'):
        if rel and ws.SECRET_NAME.search(rel) and not ws.SECRET_TEMPLATE.search(rel):
            return rel
    return None


def local_state(path, roots, anchors, references):
    physical_clone(path, roots)
    holder = ws.anchored(anchors, path)
    if holder:
        raise ws.Keep(f'in use: {holder}')
    if path in references:
        raise ws.Keep(references[path])
    if time.time() - tree_evidence(path) < IDLE_SECONDS:
        raise ws.Keep('active within 24h')
    for name in ws.IN_PROGRESS:
        if os.path.lexists(os.path.join(path, '.git', name)):
            raise ws.Keep(f'git operation in progress: {name}')
    if ws.git(path, 'status', '--porcelain', '--untracked-files=all', '--ignore-submodules=none', timeout=600).stdout:
        raise ws.Keep('dirty working tree')
    if ws.git(path, 'stash', 'list').stdout:
        raise ws.Keep('stash exists')
    wts = ws.list_worktrees(path)
    if len(wts) != 1 or os.path.realpath(wts[0]['worktree']) != path:
        raise ws.Keep('linked worktrees exist')
    secret = ignored_credential(path)
    if secret:
        raise ws.Keep(f'ignored credential: {secret}')
    repo = ws.Repo(path, set(ws.DEFAULT_LOGINS))
    urls = [pair[0] for pair in repo.remotes.values()]
    head = ws.git(path, 'rev-parse', 'HEAD').stdout.strip()
    refs = ws.git(path, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads', 'refs/tags').stdout
    objects = dict(line.split(None, 1) for line in refs.splitlines())
    objects['HEAD'] = head
    return repo, {'urls': urls, 'head': head, 'objects': objects}


def plan_clone(path, roots, anchors, references, scan_error=None):
    item = {'path': path, 'action': 'keep', 'archives': []}
    try:
        if scan_error:
            raise ws.Keep(scan_error)
        repo, local = local_state(path, roots, anchors, references)
        item.update(local)
        # A local bare 'remote' inside the directory being removed disappears
        # with it, so ls-remote there cannot prove recoverability.
        for name, (fetch, _) in list(repo.remotes.items()):
            target = local_path(fetch, path)
            if target and any(p == path or p.startswith(path + os.sep) for p in ws.aliases(target)):
                repo.remotes.pop(name)
                if repo.dest == name:
                    repo.dest = None
        # Include exact tag objects as well as live branches. Remote-tracking
        # refs alone are never evidence that the server still has anything.
        repo.load_live(include_tags=True)
        objects = item['objects']
        missing = {ref: sha for ref, sha in objects.items()
                   if not (repo.by_sha.get(sha) if ref.startswith('refs/tags/') else repo.proof(sha))}
        item['objects'] = objects
        item['archive_objects'] = missing
        if missing:
            mono2 = any('mono2' in s.lower() for s in [path, *item['urls']])
            if mono2 and (not repo.dest or ws.parse_remote(repo.remotes[repo.dest][1])[0] == 'github.com'):
                raise ws.Keep('mono2 must never be archived to GitHub')
            if not repo.dest:
                raise ws.Keep('unpushed refs and no personal or internal remote')
            if repo.dest in repo.unreachable:
                raise ws.Keep(f'archive destination unreachable: {repo.dest}')
            item['archive_url'] = ws.transport_url(repo.remotes[repo.dest][1])
        item['action'] = 'archive+remove' if missing else 'remove'
    except ws.Keep as exc:
        item['reason'] = str(exc)
    except Exception as exc:
        item['reason'] = 'check failed: ' + ws.failure_text(exc)
    return item


def prepare_clone(item, roots, references, stamp):
    path = item['path']
    if 'bytes' not in item:
        ws.measure([item])
    if item['bytes'] is None:
        raise ws.Keep('size measurement incomplete')
    before = os.lstat(physical_clone(path, roots))
    fresh = plan_clone(path, roots, usage_anchors(), references)
    if fresh['action'] == 'keep':
        raise ws.Keep(fresh['reason'])
    if fresh['objects'] != item['objects']:
        raise ws.Keep('refs changed after plan')
    if fresh['archive_objects']:
        repo = ws.Repo(path, set(ws.DEFAULT_LOGINS))
        repo.load_live(include_tags=True)
        for source, sha in fresh['archive_objects'].items():
            suffix = re.sub(r'[^A-Za-z0-9-]+', '-', source)
            ref = f'refs/heads/archive/tmp-clone/{ws.archive_slug(path)}-{stamp}/{suffix}'
            # Tags (including annotated tag objects) retain their type and exact
            # bytes in a tag ref; branches/HEAD retain their entire history.
            if source.startswith('refs/tags/'):
                ref = ref.replace('refs/heads/', 'refs/tags/', 1)
            url = fresh['archive_url']
            got = ws.push_archive(repo, sha, ref, url, Path(path).name, stamp)
            item['archives'].append({'source': source, 'url': url, 'ref': ref, 'sha': got})
            if got != sha:
                raise ws.Keep('archive snapshot differs from exact local history; clone retained')
    # push_archive already proved each exact SHA at the push URL. Fetch may
    # point at a read-only upstream, so it need not contain the archive refs.
    _, checked = local_state(path, roots, usage_anchors(), references)
    if checked['objects'] != item['objects']:
        raise ws.Keep('refs changed after archive')
    item['_identity'] = [before.st_dev, before.st_ino]


def remove_prepared(item, roots, references):
    path = item['path']
    # All network IO and the complete reference refresh have ended. Occupancy
    # and every local condition are rebound here; nothing after this does IO to
    # a remote server.
    _, local = local_state(path, roots, usage_anchors(), references)
    if local['objects'] != item['objects']:
        raise ws.Keep('refs changed after archive')
    # fd-based rmtree never traverses symlinks. Bind the parent and compare the
    # same inode immediately before removing only this directory entry.
    parent = os.path.dirname(path)
    fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        current = os.stat(os.path.basename(path), dir_fd=fd, follow_symlinks=False)
        if tuple(item['_identity']) != (current.st_dev, current.st_ino):
            raise ws.Keep('clone identity changed')
        physical_clone(path, roots)
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ws.Keep('fd-based safe rmtree unavailable')
        shutil.rmtree(os.path.basename(path), dir_fd=fd)
    finally:
        os.close(fd)
    item['result'] = 'removed'


def summary(rows):
    counts, sizes = {}, {}
    for row in rows:
        key = row['action']
        if row.get('result') == 'removed':
            key = 'archive+removed' if row['archives'] else 'removed'
        elif row.get('result') in ('kept', 'failed') or key == 'keep':
            key = 'keep: ' + ws.reason_key(row.get('reason', 'unknown'))
        counts[key] = counts.get(key, 0) + 1
        sizes[key] = sizes.get(key, 0) + (row.get('bytes') or 0)
    return {'counts': counts, 'bytes': sizes, 'unmeasured': sum(r.get('bytes') is None for r in rows)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--quiet', action='store_true', help='silent unless a clone is removed or a command fails')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--state-dir', default=os.path.expanduser('~/.local/state/devcloud-storage-upkeep'))
    parser.add_argument('--budget-minutes', type=float, default=0)
    args = parser.parse_args(argv)
    started = time.time()
    stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime(started))
    os.makedirs(args.state_dir, exist_ok=True)
    log = os.path.join(args.state_dir, f'tmp-clone-sweep-{stamp}.jsonl')
    with open(os.path.join(args.state_dir, 'tmp-clone-sweep.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            if not args.quiet:
                print('tmp clone sweep busy; skipped')
            return 0
        try:
            candidates = []
            for root in ROOTS:
                with os.scandir(root) as entries:
                    candidates.extend(e.path for e in entries if e.is_dir(follow_symlinks=False)
                                      and os.path.isdir(os.path.join(e.path, '.git'))
                                      and not os.path.islink(os.path.join(e.path, '.git')))
            anchors, references, scan_error = {}, {}, None
            try:
                anchors = usage_anchors()
                references = reference_index(['/data/code', *ROOTS], candidates)
            except Exception as exc:
                scan_error = 'usage/reference scan incomplete: ' + ws.failure_text(exc)
            with cf.ThreadPoolExecutor(8) as pool:
                rows = list(pool.map(lambda p: plan_clone(p, ROOTS, anchors, references, scan_error), sorted(candidates)))
            ws.measure(rows)
            if args.execute:
                def persist(row):
                    with open(log, 'a') as handle:
                        handle.write(json.dumps(row, ensure_ascii=False) + '\n')
                        handle.flush()
                        os.fsync(handle.fileno())

                prepared = []
                for row in rows:
                    if row['action'] == 'keep':
                        continue
                    try:
                        if args.budget_minutes and time.time() - started > args.budget_minutes * 60:
                            raise ws.Keep('time budget reached')
                        prepare_clone(row, ROOTS, references, stamp)
                        prepared.append(row)
                    except (ws.Keep, ws.ScanIncomplete) as exc:
                        row.update(result='kept', reason=str(exc))
                        persist(row)
                    except Exception as exc:
                        row.update(result='failed', reason=ws.failure_text(exc))
                        persist(row)
                final_error = None
                if prepared:
                    try:
                        references = reference_index(['/data/code', *ROOTS], [r['path'] for r in prepared])
                    except Exception as exc:
                        final_error = 'final reference scan incomplete: ' + ws.failure_text(exc)
                        scan_error = final_error
                for row in prepared:
                    try:
                        if final_error:
                            raise ws.Keep(final_error)
                        remove_prepared(row, ROOTS, references)
                    except (ws.Keep, ws.ScanIncomplete) as exc:
                        row.update(result='kept', reason=str(exc))
                    except Exception as exc:
                        row.update(result='failed', reason=ws.failure_text(exc))
                    persist(row)
            # Preserve every refusal too; removed rows were fsynced immediately.
            with open(log, 'a') as handle:
                for row in rows:
                    if not args.execute or row['action'] == 'keep':
                        handle.write(json.dumps(row, ensure_ascii=False) + '\n')
            failed = any(r.get('result') == 'failed' for r in rows)
            report = {'mode': 'execute' if args.execute else 'preview', **summary(rows), 'log': log}
            if args.json and not args.quiet:
                print(json.dumps(rows, ensure_ascii=False))
            if not args.quiet or failed or any(r.get('result') == 'removed' for r in rows):
                print(json.dumps(report, ensure_ascii=False))
            if scan_error:
                print(scan_error, file=sys.stderr)
            return 1 if failed or scan_error else 0
        except Exception as exc:
            print('tmp clone sweep: ' + ws.failure_text(exc), file=sys.stderr)
            return 1


if __name__ == '__main__':
    entry = Path(__file__).with_name('git_tree_sweep.py')
    namespace = {'__name__': 'git_tree_sweep_cli', '__file__': str(entry)}
    exec(compile(entry.read_bytes(), str(entry), 'exec'), namespace)
    sys.exit(namespace['main']())
