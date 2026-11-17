"""Host-wide Git sweep contract. Real disposable Git repositories; no real remotes."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / '.claude/skills/devcloud-storage-upkeep/scripts/git_tree_sweep.py'
OLD = time.time() - 10 * 86400


def shell(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def age(path):
    for parent, dirs, files in os.walk(path):
        for name in dirs + files:
            os.utime(Path(parent, name), (OLD, OLD), follow_symlinks=False)
        os.utime(parent, (OLD, OLD))


@pytest.fixture
def case(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location('git_tree_sweep_test', SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    temporary = tempfile.TemporaryDirectory(prefix='tmp-git-tree-', dir=SCRIPT.parents[4] / 'tests')
    tmp_path = Path(temporary.name)
    repo = tmp_path / 'deep' / '.hidden' / 'a' / 'b' / 'repo'
    bare = tmp_path / 'remote.git'
    shell('git', 'init', '-q', '--bare', '-b', 'main', str(bare))
    shell('git', 'init', '-q', '-b', 'main', str(repo))
    for key, value in [('user.name', 'Test'), ('user.email', 'test@example.com'), ('core.excludesFile', '/dev/null')]:
        shell('git', 'config', key, value, cwd=repo)
    (repo / 'a').write_text('a')
    (repo / '.gitignore').write_text('.worktrees/\nnode_modules/\n.env\n*.pem\n')
    shell('git', 'add', '.', cwd=repo)
    shell('git', 'commit', '-qm', 'initial', cwd=repo)
    shell('git', 'remote', 'add', 'origin', 'https://git.woa.com/group/repo.git', cwd=repo)
    raw = m._run
    calls = []
    def transport(args, **kw):
        calls.append(list(args))
        args = [str(bare) if a == 'https://git.woa.com/group/repo.git' else a for a in args]
        return raw(args, **kw)
    monkeypatch.setattr(m, '_run', transport)
    shell('git', 'push', '-q', str(bare), 'main', cwd=repo)
    age(repo)
    yield SimpleNamespace(m=m, repo=repo, bare=bare, root=tmp_path, calls=calls)
    temporary.cleanup()


def inv(c):
    return c.m.discover([str(c.root)], set())


def plan(c, repo=None, kind='clone', anchors=None, refs=None):
    repo = repo or c.repo
    return c.m.plan(str(repo), kind, inv(c), anchors or {}, refs or {})


def linked(c):
    target = c.repo / '.worktrees' / 'other project'
    shell('git', 'worktree', 'add', '-q', '-b', 'work', str(target), cwd=c.repo)
    age(c.repo)
    return target


def test_discovers_deep_hidden_nested_clones_and_foreign_linked(case):
    c = case
    inner = c.repo / '.worktrees' / 'independent'
    shell('git', 'clone', '-q', str(c.bare), str(inner))
    foreign = linked(c)
    orphan = c.root / 'thread-storage' / 'clone'
    (orphan / '.git').mkdir(parents=True)
    found = inv(c)
    assert found['trees'][str(c.repo)] == 'clone'
    assert found['trees'][str(inner)] == 'clone'
    assert found['trees'][str(foreign)] == 'linked'
    assert found['trees'][str(orphan)] == 'clone'
    assert str(c.bare) in found['bare']
    assert not found['errors']


@pytest.mark.parametrize('name', ['node_modules', 'vendor', '.venv', 'venv', '__pypackages__', '.pnpm-store'])
def test_dependency_storage_is_not_a_worktree_candidate(case, name):
    target = case.root / name / 'dependency' / '.git'
    target.mkdir(parents=True)
    container = target.parents[1]
    if name in ('.venv', 'venv'): (container / 'pyvenv.cfg').touch()
    if name == 'node_modules': (case.root / 'package.json').write_text('{}')
    if name == 'vendor':
        (case.root / 'go.mod').write_text('module fixture')
        (container / 'modules.txt').touch()
    if name == '__pypackages__':
        (case.root / 'pyproject.toml').touch()
        (container / '3.12/lib').mkdir(parents=True)
    if name == '.pnpm-store': (container / 'v11/files').mkdir(parents=True)
    assert str(target.parent) not in inv(case)['trees']
    assert 'directory' in inv(case)['excluded'][str(case.root / name)] or name == '.pnpm-store'


def test_owner_roots_are_resolved_and_excluded_but_tool_installs_are_discovered(case):
    c = case
    managed = c.root / 'docker'
    (managed / 'repo' / '.git').mkdir(parents=True)
    for tool in ('.pyenv', '.nvm'):
        (c.root / tool / '.git').mkdir(parents=True)
    found = c.m.discover([str(c.root)], {str(managed)})
    assert str(managed / 'repo') not in found['trees']
    assert all(str(c.root / tool) in found['trees'] for tool in ('.pyenv', '.nvm'))


def test_discovery_does_not_follow_symlinks_and_deduplicates_roots(case):
    c = case
    (c.root / 'alias').symlink_to(c.repo, target_is_directory=True)
    found = c.m.discover([str(c.root), str(c.root)], set())
    assert len([p for p in found['trees'] if p.endswith('/repo')]) == 1
    assert (str(c.root / 'alias'), str(c.repo)) in found['links']


def test_discovery_obeys_xdev(case, monkeypatch):
    c = case
    raw = c.m.os.stat
    def other_device(path, **kw):
        value = raw(path, **kw)
        return SimpleNamespace(st_dev=value.st_dev + 1, st_ino=value.st_ino) if str(path) == str(c.repo) else value
    monkeypatch.setattr(c.m.os, 'stat', other_device)
    assert str(c.repo) not in inv(c)['trees']


def test_discovery_denial_is_incomplete_and_plan_retains(case, monkeypatch):
    c = case
    raw = c.m.os.scandir
    def denied(path):
        if str(path) == str(c.repo): raise PermissionError('denied')
        return raw(path)
    monkeypatch.setattr(c.m.os, 'scandir', denied)
    found = inv(c)
    assert found['errors']
    row = c.m.plan(str(c.repo), 'clone', found, {}, {}, 'discovery incomplete')
    assert row['action'] == 'keep'


def test_clean_old_published_clone_is_candidate_and_preview_does_not_mutate(case):
    c = case
    before = shell('git', 'show-ref', cwd=c.bare)
    row = plan(c)
    assert row['action'] == 'remove', row
    assert row['objects']['HEAD'] and row['archives'] == []
    assert c.repo.exists() and before == shell('git', 'show-ref', cwd=c.bare)
    assert not any('push' in call for call in c.calls)


@pytest.mark.parametrize('guard', ['untracked', 'dirty', 'stash', 'recent_file', 'recent_metadata',
                                  'ignored_secret', 'dependency_secret', 'operation', 'alternates'])
def test_local_refusals(case, guard):
    c = case
    if guard == 'untracked': (c.repo / 'untracked').write_text('unique')
    if guard == 'dirty': (c.repo / 'a').write_text('changed')
    if guard == 'stash':
        # Construct the observable stash reflog without a side-effecting stash command.
        head = shell('git', 'rev-parse', 'HEAD', cwd=c.repo)
        shell('git', 'update-ref', '--create-reflog', '-m', 'stash fixture', 'refs/stash', head, cwd=c.repo)
    if guard == 'recent_file': (c.repo / 'a').touch()
    if guard == 'recent_metadata': (c.repo / '.git/HEAD').touch()
    if guard == 'ignored_secret': (c.repo / '.env').write_text('secret')
    if guard == 'dependency_secret':
        (c.repo / 'node_modules').mkdir()
        (c.repo / 'node_modules/key.pem').write_text('private')
    if guard == 'operation': (c.repo / '.git/MERGE_HEAD').write_text('x')
    if guard == 'alternates': (c.repo / '.git/objects/info/alternates').write_text(str(c.bare / 'objects'))
    if guard not in ('recent_file', 'recent_metadata'): age(c.repo)
    row = plan(c)
    assert row['action'] == 'keep', row
    expect = {'untracked': 'dirty', 'dirty': 'dirty', 'stash': 'stash', 'recent_file': '168h',
              'recent_metadata': '168h', 'ignored_secret': 'credential', 'dependency_secret': 'credential',
              'operation': 'progress', 'alternates': 'alternates'}
    assert expect[guard] in row['reason'], row


def test_main_checkout_with_live_linked_tree_is_protected(case):
    target = case.root / 'linked'
    shell('git', 'worktree', 'add', '-q', '-b', 'work', str(target), cwd=case.repo)
    age(case.root)
    assert 'linked worktrees' in plan(case)['reason']


def test_other_location_linked_worktree_is_candidate(case):
    c = case
    target = c.root / 'unrelated' / '.hidden' / 'linked'
    shell('git', 'worktree', 'add', '-q', '-b', 'work', str(target), cwd=c.repo)
    age(c.root)
    assert plan(c, target, 'linked')['action'] == 'remove'
    assert 'linked worktrees' in plan(c)['reason']


@pytest.mark.parametrize('kind', ['missing', 'invalid', 'locked', 'submodule'])
def test_undecidable_gitfile_or_locked_worktree_stays(case, kind):
    c = case
    target = linked(c)
    if kind == 'missing':
        gitdir = shell('git', 'rev-parse', '--absolute-git-dir', cwd=target)
        import shutil
        shutil.rmtree(gitdir)
    if kind == 'invalid': (target / '.git').write_text('not gitdir')
    if kind == 'locked': shell('git', 'worktree', 'lock', str(target), cwd=c.repo)
    if kind == 'submodule':
        # A separate gitdir reached through a gitfile is not a linked registration.
        target = c.root / 'submodule-shaped'
        shell('git', 'clone', '-q', '--separate-git-dir', str(c.root / 'separate'), str(c.bare), str(target))
    age(c.root)
    row = plan(c, target, 'linked')
    assert row['action'] == 'keep', row


@pytest.mark.parametrize('ref', ['branch', 'HEAD', 'tag', 'annotated_tag'])
def test_every_ref_requires_live_remote_proof(case, ref):
    c = case
    if ref in ('HEAD', 'branch'):
        (c.repo / 'a').write_text('new')
        shell('git', 'commit', '-qam', 'local', cwd=c.repo)
        if ref == 'branch':
            shell('git', 'branch', 'unpublished', cwd=c.repo)
            shell('git', 'reset', '--hard', 'HEAD~', cwd=c.repo)
    elif ref == 'tag': shell('git', '-c', 'tag.gpgSign=false', 'tag', 'local-tag', cwd=c.repo)
    else: shell('git', 'tag', '-am', 'annotated', 'local-tag', cwd=c.repo)
    age(c.repo)
    row = plan(c)
    if ref == 'tag':
        # Lightweight tag points at the already-published exact object.
        assert row['action'] == 'remove'
    else:
        assert row['action'] == 'archive+remove', row
        assert row['archive_objects']


@pytest.mark.parametrize('reason', ['process 1', 'container db', 'BB project source: p', 'BB environment: e',
                                    'symlink reference: /bin/tool', 'process 1 environment PATH',
                                    'config reference: .bashrc', 'config reference: unit',
                                    'config reference: cron', 'BB automation reference: a',
                                    'remote URL reference: other', 'alternates reference: other'])
def test_each_consumer_reserves_its_containing_checkout(case, reason):
    c = case
    anchors = {}
    c.m.anchor(anchors, str(c.repo / 'bin' / 'tool'), reason)
    row = plan(c, anchors=anchors)
    assert row['action'] == 'keep' and row['reason'] == reason
    assert not c.m.ws.anchored(anchors, str(c.repo / '.worktrees/nested'))


@pytest.mark.parametrize('key', ['PATH', 'PYENV_ROOT', 'NVM_DIR', 'TOOL_HOME'])
def test_process_environment_paths_are_protected(case, key):
    anchors = {}
    value = ('/usr/bin:' if key == 'PATH' else '') + str(case.repo / 'bin')
    case.m.add_environment(anchors, {key: value}, 'process 42 environment')
    assert case.m.ws.anchored(anchors, str(case.repo)) == f'process 42 environment {key}'


@pytest.mark.parametrize('probe', ['cwd', 'exe', 'fd', 'maps', 'environment'])
def test_process_probe_reads_cwd_maps_fd_and_environment(case, tmp_path, probe):
    c = case
    proc = tmp_path / 'proc'
    pid = proc / '987654'
    (pid / 'fd').mkdir(parents=True)
    (pid / 'cwd').symlink_to(c.repo if probe == 'cwd' else '/unrelated')
    (pid / 'exe').symlink_to(c.repo / 'a' if probe == 'exe' else '/usr/bin/bash')
    (pid / 'status').write_text('Uid: 0 0 0 0\n')
    (pid / 'stat').write_text('987654 (test process) ' + ' '.join(['1'] * 25))
    (pid / 'maps').write_text('a b c d e ' + (str(c.repo / 'lib.so') if probe == 'maps' else '/unrelated/lib.so') + '\n')
    (pid / 'environ').write_text('NVM_DIR=' + (str(c.repo) if probe == 'environment' else '/unrelated') + '\0')
    (pid / 'fd/3').symlink_to(c.repo / 'a' if probe == 'fd' else '/unrelated/file')
    anchors = {}
    c.m.process_anchors(anchors, str(proc))
    assert c.m.ws.anchored(anchors, str(c.repo))
    assert plan(c, anchors=anchors)['action'] == 'keep'


def test_process_probe_permission_failure_is_unknown(case, monkeypatch):
    monkeypatch.setattr(case.m.os, 'listdir', lambda _: (_ for _ in ()).throw(PermissionError('denied')))
    with pytest.raises(PermissionError): case.m.process_anchors({})


@pytest.mark.parametrize('text', ['export PYENV_ROOT="$HOME/.pyenv"\nexport PATH="$PYENV_ROOT/bin:$PATH"',
                                  'ExecStart={repo}/bin/tool', '30 6 * * * {repo}/bin/tool',
                                  'source {repo}/init.sh'])
def test_shell_unit_and_cron_literals_reserve_tool_clones(case, text):
    c = case
    anchors = {}
    home = str(c.repo.parent)
    text = text.format(repo=c.repo)
    c.m.text_anchors(anchors, text, home, 'config reference', {'PATH': '/usr/bin'})
    target = str(c.repo.parent / '.pyenv') if 'PYENV' in text else str(c.repo)
    assert c.m.ws.anchored(anchors, target)


def test_bb_project_and_live_environment_anchors(case, monkeypatch):
    c = case
    projects = [{'id': 'p', 'sources': [{'path': str(c.repo)}]}]
    env = c.root / 'env'
    dead = c.root / 'dead'
    responses = {'project': projects, 'environment': [{'id': 'e', 'status': 'ready', 'path': str(env)},
                                                     {'id': 'dead', 'status': 'destroyed', 'path': str(dead)}], 'automation': []}
    monkeypatch.setattr(c.m, 'command', lambda cmd, **kw: SimpleNamespace(stdout=json.dumps(responses[cmd[1]])))
    monkeypatch.setattr(c.m.ws, 'bb_anchors', lambda *a: None)
    anchors = {}
    c.m.bb_anchors(anchors)
    assert 'BB project source' in c.m.ws.anchored(anchors, str(c.repo))
    assert 'BB environment' in c.m.ws.anchored(anchors, str(env))
    assert not c.m.ws.anchored(anchors, str(dead))


def test_bb_automation_stored_command_is_a_consumer(case, monkeypatch):
    c = case
    script = c.root / 'automation.sh'
    script.write_text('exec ' + str(c.repo / 'bin/tool'))
    def response(cmd, **kw):
        data = {'project': [{'id': 'p', 'sources': []}], 'environment': [],
                'automation': [{'id': 'a', 'execution': {'storedScriptPath': str(script)}}]}[cmd[1]]
        return SimpleNamespace(stdout=json.dumps(data))
    monkeypatch.setattr(c.m, 'command', response)
    monkeypatch.setattr(c.m.ws, 'bb_anchors', lambda *a: None)
    anchors = {}
    c.m.bb_anchors(anchors)
    assert 'BB automation' in c.m.ws.anchored(anchors, str(c.repo))


@pytest.mark.parametrize('source', ['remote', 'pushurl', 'alternates', 'bare'])
def test_reference_index_includes_all_discovered_repos(case, source):
    c = case
    other = c.root / 'other'
    shell('git', 'init', '-q', '--bare' if source == 'bare' else '--quiet', str(other))
    metadata = other if source == 'bare' else other / '.git'
    if source in ('alternates', 'bare'):
        (metadata / 'objects/info/alternates').write_text(str(c.repo / '.git/objects'))
    else:
        shell('git', 'config', 'remote.local.' + ('pushurl' if source == 'pushurl' else 'url'),
              'file://' + str(c.repo), cwd=other)
    refs = c.m.references(inv(c))
    assert c.m.ws.anchored(refs, str(c.repo))
    assert plan(c, refs=refs)['action'] == 'keep'


def test_deleted_gitdir_is_listed_but_does_not_break_other_reference_reads(case):
    c = case
    missing = c.root / 'dead'
    missing.mkdir()
    (missing / '.git').write_text('gitdir: /does/not/exist')
    assert c.m.references(inv(c)) == {}
    assert 'gitdir missing' in plan(c, missing, 'linked')['reason']


def test_clone_delete_unlinks_internal_symlinks_only(case, monkeypatch):
    c = case
    outside = c.root / 'valuable'
    outside.mkdir()
    (outside / 'keep').write_text('keep')
    (c.repo / 'node_modules').mkdir()
    (c.repo / 'node_modules/link').symlink_to(outside, target_is_directory=True)
    age(c.repo)
    row = plan(c)
    assert row['action'] == 'remove', row
    c.m.remove(row, inv(c), {}, {})
    assert not c.repo.exists() and (outside / 'keep').read_text() == 'keep'


def test_linked_delete_uses_primary_git_without_force(case):
    c = case
    target = c.root / 'linked'
    shell('git', 'worktree', 'add', '-q', '-b', 'work', str(target), cwd=c.repo)
    age(c.root)
    row = plan(c, target, 'linked')
    assert row['action'] == 'remove', row
    c.m.remove(row, inv(c), {}, {})
    assert not target.exists() and c.repo.exists()
    commands = [x for x in c.calls if 'remove' in x]
    assert commands and commands[-1][2] == str(c.repo)
    assert '--force' not in commands[-1] and '-f' not in commands[-1]


@pytest.mark.parametrize('change', ['dirty', 'anchor', 'identity', 'secret'])
def test_sink_rechecks_local_state(case, change):
    c = case
    row = plan(c)
    anchors = {}
    if change == 'dirty': (c.repo / 'a').write_text('new')
    if change == 'anchor': c.m.anchor(anchors, str(c.repo), 'new process')
    if change == 'identity': row['identity'][1] += 1
    if change == 'secret': (c.repo / '.env').write_text('private')
    age(c.repo)
    with pytest.raises(Exception): c.m.remove(row, inv(c), anchors, {})
    assert c.repo.exists()


def test_overall_budget_marks_partial_discovery_unknown(case):
    c = case
    c.m.DEADLINE = 0
    found = inv(c)
    assert found['errors']
    with pytest.raises(c.m.ws.ScanIncomplete): c.m.command(['true'])


def test_external_symlinks_protect_target_but_internal_symlinks_do_not(case):
    c = case
    (c.repo / 'internal').symlink_to(c.repo / 'a')
    (c.root / 'external').symlink_to(c.repo / 'a')
    anchors = {}
    c.m.symlink_anchors(anchors, inv(c))
    assert 'external' in c.m.ws.anchored(anchors, str(c.repo))
    (c.root / 'external').unlink()
    anchors = {}
    c.m.symlink_anchors(anchors, inv(c))
    assert not c.m.ws.anchored(anchors, str(c.repo))


@pytest.mark.parametrize('mode', ['unreachable', 'no_destination', 'mono2'])
def test_remote_or_archive_uncertainty_retains_clone(case, monkeypatch, mode):
    c = case
    (c.repo / 'a').write_text('local')
    shell('git', 'commit', '-qam', 'local', cwd=c.repo)
    age(c.repo)
    if mode == 'unreachable':
        raw = c.m._run
        def unreachable(cmd, **kw):
            if 'ls-remote' in cmd: raise RuntimeError('offline')
            return raw(cmd, **kw)
        monkeypatch.setattr(c.m, '_run', unreachable)
    if mode == 'no_destination': monkeypatch.setattr(c.m.ws.Repo, '_archive_destination', lambda *a: None)
    if mode == 'mono2':
        shell('git', 'remote', 'set-url', 'origin', 'https://github.com/hxy91819/mono2.git', cwd=c.repo)
        raw = c.m._run
        monkeypatch.setattr(c.m, '_run', lambda cmd, **kw: raw([str(c.bare) if a == 'https://github.com/hxy91819/mono2.git' else a for a in cmd], **kw))
        age(c.repo)
    assert plan(c)['action'] == 'keep'


def execute_case(c, monkeypatch):
    row = plan(c)
    row['bytes'] = 4096
    def usage(inventory):
        anchors = {}
        c.m.symlink_anchors(anchors, inventory)
        return anchors, []
    monkeypatch.setattr(c.m, 'usage', usage)
    log = c.root / 'ledger.jsonl'
    c.m.execute([row], inv(c), {}, {}, [str(c.root)], set(), 'test', log)
    return row, log


def test_execution_archives_exact_history_before_clone_removal(case, monkeypatch):
    c = case
    (c.repo / 'a').write_text('unpublished')
    shell('git', 'commit', '-qam', 'local', cwd=c.repo)
    shell('git', '-c', 'tag.gpgSign=false', 'tag', '-am', 'tag', 'annotated', cwd=c.repo)
    original = shell('git', 'rev-parse', 'HEAD', cwd=c.repo)
    age(c.repo)
    row, log = execute_case(c, monkeypatch)
    assert row.get('result') == 'removed', row
    assert not c.repo.exists()
    assert row['archives'] and row['archives'][0]['sha'] == original
    for archive in row['archives']:
        assert shell('git', 'rev-parse', archive['ref'], cwd=c.bare) == archive['sha']
    events = [json.loads(line) for line in log.read_text().splitlines()]
    assert any(x.get('event') == 'archive_verified' for x in events)
    assert events[-1]['result'] == 'removed'
    assert all('--force' not in cmd and '-f' not in cmd for cmd in c.calls if 'push' in cmd)


@pytest.mark.parametrize('failure', ['push_rejected', 'wrong_sha', 'new_secret', 'new_reference', 'new_symlink', 'discovery_unknown', 'config_unknown'])
def test_archive_and_last_network_window_failures_keep_clone(case, monkeypatch, failure):
    c = case
    (c.repo / 'a').write_text('unpublished')
    shell('git', 'commit', '-qam', 'local', cwd=c.repo)
    age(c.repo)
    raw = c.m._run
    touched = False
    def altered(cmd, **kw):
        nonlocal touched
        if 'push' in cmd and failure == 'push_rejected':
            raise RuntimeError('committer-check rejected original history')
        if 'ls-remote' in cmd and any('/archive/git-tree/' in a for a in cmd):
            if failure == 'wrong_sha': return SimpleNamespace(stdout='0'*40+'\tref\n', returncode=0)
            if not touched:
                touched = True
                if failure == 'new_secret': (c.repo / '.env').write_text('secret')
                if failure == 'new_symlink': (c.root / 'consumer').symlink_to(c.repo / 'a')
                if failure == 'new_reference':
                    other = c.root / 'consumer'
                    shell('git', 'init', '-q', str(other))
                    shell('git', 'config', 'remote.local.url', str(c.repo), cwd=other)
        return raw(cmd, **kw)
    monkeypatch.setattr(c.m, '_run', altered)
    original_discover = c.m.discover
    if failure == 'discovery_unknown':
        count = 0
        def incomplete(*a):
            nonlocal count
            result = original_discover(*a)
            count += 1
            if count >= 3: result['errors'].append('denied')
            return result
        monkeypatch.setattr(c.m, 'discover', incomplete)
    if failure == 'config_unknown':
        row = plan(c)
        row['bytes'] = 4096
        monkeypatch.setattr(c.m, 'usage', lambda _: ({}, ['unknown path variable']))
        c.m.execute([row], inv(c), {}, {}, [str(c.root)], set(), 'test', c.root / 'ledger')
    else:
        row, _ = execute_case(c, monkeypatch)
    assert row.get('result') == 'kept', row
    assert c.repo.exists()


def test_unknown_path_expression_is_reported_but_nonpath_shell_state_is_ignored(case):
    c = case
    anchors = {}
    unknown = c.m.text_anchors(anchors, 'source "$UNKNOWN_HOME/init.sh"\ncase "$TERM" in x) ;; esac', '/root', 'shell')
    assert unknown and 'UNKNOWN_HOME' in unknown[0]['expression']
    assert c.m.text_anchors({}, 'test "$TERM" = x', '/root', 'shell') == []


def test_config_file_read_failure_is_unknown(case, monkeypatch):
    path = case.root / '.bashrc'
    path.write_text('source /tool/init.sh')
    original = Path.read_text
    def denied(self, **kw):
        if self == path: raise PermissionError('denied')
        return original(self, **kw)
    monkeypatch.setattr(Path, 'read_text', denied)
    with pytest.raises(PermissionError): case.m.config_anchors({}, [(str(path), '/root')])


def test_summary_keeps_unknown_size_distinct_from_zero(case):
    result = case.m.summarize([{'action':'keep','reason':'unknown: denied','bytes':None},
                               {'action':'remove','bytes':0}])
    assert result['unmeasured'] == 1 and 'keep: unknown' not in result['bytes']


def test_git_metadata_symlink_is_also_a_consumer(case):
    c = case
    other = c.root / 'other'
    shell('git', 'init', '-q', str(other))
    (other / '.git' / 'tool-link').symlink_to(c.repo / 'a')
    anchors = {}
    c.m.symlink_anchors(anchors, inv(c))
    assert c.m.ws.anchored(anchors, str(c.repo))


def test_virtual_proc_path_variables_do_not_hide_real_unresolved_paths(case):
    c = case
    assert c.m.text_anchors({}, 'echo -100 > /proc/$pid/oom_score_adj', '/root', 'unit') == []
    assert c.m.text_anchors({}, 'source $tool/init.sh', '/root', 'shell')


def test_main_reports_budget_exhaustion_and_never_executes_partial_inventory(case, monkeypatch, capsys):
    c = case
    inventory = inv(c)
    monkeypatch.setattr(c.m, 'managed_roots', lambda: set())
    monkeypatch.setattr(c.m, 'discover', lambda *a: inventory)
    monkeypatch.setattr(c.m, 'usage', lambda _: ({}, []))
    monkeypatch.setattr(c.m, 'references', lambda _: {})
    state = c.root / 'state'
    assert c.m.main(['--budget-minutes', '0', '--state-dir', str(state)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['complete'] is False and 'budget' in result['error']
    assert c.repo.exists() and not any('push' in cmd for cmd in c.calls)


@pytest.mark.parametrize('text', ['source "{repo}/init.sh"', 'ExecStart="{repo}/bin/tool"',
                                  'TOOL_HOME="{repo}"\nexport PATH="$TOOL_HOME/bin:$PATH"'])
def test_quoted_space_paths_reserve_the_entire_clone(case, text):
    c = case
    path = c.root / 'tool clone'
    anchors = {}
    unknown = c.m.text_anchors(anchors, text.format(repo=path), '/root', 'config', {'PATH': '/usr/bin'})
    assert not unknown
    assert c.m.ws.anchored(anchors, str(path)) == 'config'


@pytest.mark.parametrize('text', ['export TOOL_HOME="$(tool locate)"\nexport PATH="$TOOL_HOME/bin:$PATH"',
                                  'TOOL_ROOT=`tool locate`\nsource "$TOOL_ROOT/init.sh"',
                                  'source "$(tool locate)/init.sh"'])
def test_command_substitution_path_state_remains_unknown(case, text):
    assert case.m.text_anchors({}, text, '/root', 'config', {'PATH': '/usr/bin'})


@pytest.mark.parametrize('push', [False, True])
def test_effective_remote_rewrites_protect_local_clone(case, push):
    c = case
    other = c.root / 'other'
    shell('git', 'init', '-q', str(other))
    shell('git', 'config', 'remote.origin.url', 'local:repo', cwd=other)
    # Rewrite to an existing candidate, for both fetch and implicit push URLs.
    suffix = 'pushInsteadOf' if push else 'insteadOf'
    shell('git', 'config', 'url.' + str(c.repo.parent) + '/.' + suffix, 'local:', cwd=other)
    refs = c.m.references(inv(c))
    assert c.m.ws.anchored(refs, str(c.repo))


@pytest.mark.parametrize('rel', ['FETCH_HEAD', 'ORIG_HEAD', 'refs/heads/new-branch',
                                  'refs/remotes/origin/main', 'logs/refs/heads/main',
                                  'logs/refs/remotes/origin/main'])
def test_recent_git_activity_protects_even_without_checkout_changes(case, rel):
    c = case
    target = c.repo / '.git' / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.touch()
    row = plan(c)
    assert row['action'] == 'keep' and '168h' in row['reason'], row


def test_wildcard_shell_reference_expands_complete_quoted_paths(case):
    c = case
    target = c.root / 'tool clone'
    target.mkdir()
    (target / 'init.sh').touch()
    anchors = {}
    records = c.m.text_anchors(anchors, 'source "' + str(c.root / 'tool*' / 'init.sh') + '"', '/root', 'config')
    assert records and all(r["prefixes"] for r in records)
    assert c.m.ws.anchored(anchors, str(target))


def test_metadata_link_walk_obeys_device_boundary(case, monkeypatch):
    c = case
    raw = c.m.os.stat
    root = c.repo / '.git'
    def foreign(path, **kw):
        info = raw(path, **kw)
        return SimpleNamespace(st_dev=info.st_dev + 1) if str(path) == str(root) else info
    monkeypatch.setattr(c.m.os, 'stat', foreign)
    assert c.m.metadata_links(str(root), raw(root).st_dev) == []


def test_shell_function_path_parameters_are_bound_only_when_called(case):
    c = case
    script = 'pathmunge () {\n PATH="$1:$PATH"\n}\npathmunge "' + str(c.repo) + '"\n'
    anchors = {}
    assert not c.m.text_anchors(anchors, script, '/root', 'shell', {'PATH':'/usr/bin'})
    assert c.m.ws.anchored(anchors, str(c.repo))
    dynamic = 'tool () {\n export TOOL_HOME="$(tool locate)"\n}\n'
    assert c.m.text_anchors({}, dynamic, '/root', 'shell') == []
    assert c.m.text_anchors({}, dynamic + 'tool\n', '/root', 'shell')


def test_display_and_virtual_process_substitutions_are_not_filesystem_paths(case):
    script = 'PS1="$(hostname) ${PWD/#$HOME/xx}/"\npid=$(pgrep tmux); echo -1000 > /proc/$pid/oom_score_adj'
    assert not case.m.text_anchors({}, script, '/root', 'shell')


def test_relative_process_environment_is_resolved_from_its_own_cwd(case):
    c = case
    anchors = {}
    c.m.add_environment(anchors, {'PATH':str(c.repo.relative_to(c.root)) + '/bin'}, 'process 9', str(c.root))
    assert c.m.ws.anchored(anchors, str(c.repo))
    with pytest.raises(c.m.ws.ScanIncomplete): c.m.add_environment({}, {'TOOL_ROOT':'relative'}, 'process 9')


@pytest.mark.parametrize('text', ['source "$opaque"', 'cd ./tool', 'source ../tool/init.sh'])
def test_unresolvable_config_path_operands_are_unknown(case, text):
    assert case.m.text_anchors({}, text, '/root', 'config')


def test_environment_root_with_a_colon_is_one_path(case):
    c = case
    anchors = {}
    path = c.root / 'tool:clone'
    c.m.add_environment(anchors, {'TOOL_ROOT':str(path)}, 'process 9')
    assert c.m.ws.anchored(anchors, str(path))


@pytest.mark.parametrize('inline', [False, True])
def test_for_source_binds_literal_and_glob_paths(case, inline):
    c = case
    source = c.repo / 'init.sh'
    source.touch()
    script = 'for file in "' + str(c.repo) + '"/*.sh; do\n source "$file"\ndone'
    if inline: script = script.replace('; do\n', '; do ').replace('\n', '; ')
    anchors = {}
    records = c.m.text_anchors(anchors, script, '/root', 'shell')
    c.m.scope_unknown(anchors, records, inv(c))
    assert records and all(r['scope'] == 'prefix' for r in records)
    assert c.m.ws.anchored(anchors, str(c.repo))


@pytest.mark.parametrize('prefix', ['else ', 'echo | ', '! ', '{ ', '( '])
def test_function_path_references_in_all_call_positions(case, prefix):
    c = case
    script = 'tool() { export TOOL_ROOT="' + str(c.repo) + '"; }\n' + prefix + 'tool'
    anchors = {}
    c.m.text_anchors(anchors, script, '/root', 'shell')
    assert c.m.ws.anchored(anchors, str(c.repo))


@pytest.mark.parametrize('text', ['source tool/init.sh', 'source .venv/bin/activate', 'cd tool'])
def test_bare_relative_shell_paths_are_unknown(case, text):
    assert case.m.text_anchors({}, text, '/root', 'shell')


@pytest.mark.parametrize('quoted', [False, True])
def test_systemd_environment_defines_and_reserves_paths(case, quoted):
    c = case
    binding = 'WORKTREE=' + str(c.repo)
    script = 'Environment=' + ('"' + binding + '"' if quoted else binding) + '\nWorkingDirectory=$WORKTREE'
    anchors = {}
    assert not c.m.text_anchors(anchors, script, '/root', 'unit')
    assert c.m.ws.anchored(anchors, str(c.repo))


def test_single_line_function_preserves_trailing_reference(case):
    c = case
    script = 'foo() { echo "}"; }; source "' + str(c.repo / 'init.sh') + '"'
    anchors = {}
    assert not c.m.text_anchors(anchors, script, '/root', 'shell')
    assert c.m.ws.anchored(anchors, str(c.repo))


def test_prompt_command_is_code_and_pwd_rendering_is_read_only(case):
    c = case
    assert c.m.text_anchors({}, "PROMPT_COMMAND='cd $(tool locate)'", '/root', 'shell')
    assert not c.m.text_anchors({}, '''PROMPT_COMMAND='printf "%s" "${PWD/#$HOME/~}"' ''', '/root', 'shell')


@pytest.mark.parametrize('execute', [False, True])
@pytest.mark.parametrize('state,success,launched', [('LoadState=not-found\nActiveState=inactive',True,True),
    ('LoadState=loaded\nActiveState=active',True,False), ('LoadState=loaded\nActiveState=deactivating',True,False),
    ('LoadState=unknown\nActiveState=inactive',False,False), ('ERROR',False,False)])
def test_launcher_starts_only_one_bounded_dry_run(case, state, success, launched, execute):
    c = case
    bindir = c.root / 'bin'
    bindir.mkdir()
    capture = c.root / 'argv.json'
    ctl = bindir / 'systemctl'
    ctl.write_text('#!/bin/bash\n[[ "$PROBE_STATE" != ERROR ]] || exit 1\nprintf "%s\\n" "$PROBE_STATE"\n')
    ctl.chmod(0o755)
    run = bindir / 'systemd-run'
    run.write_text('#!' + os.sys.executable + '\nimport os,sys,json,pathlib\npathlib.Path(os.environ["PROBE_CAPTURE"]).write_text(json.dumps(sys.argv[1:]))\n')
    run.chmod(0o755)
    launcher = SCRIPT.with_name('git_tree_sweep_run.sh')
    result = subprocess.run(['bash', str(launcher), *(['--execute'] if execute else [])], env=dict(os.environ, PATH=str(bindir) + ':' + os.environ['PATH'],
                            PROBE_STATE=state, PROBE_CAPTURE=str(capture)), capture_output=True, text=True)
    assert (result.returncode == 0) == success, result.stderr
    assert capture.exists() == launched
    if launched:
        args = json.loads(capture.read_text())
        assert ('--property=RuntimeMaxSec=9180' if execute else '--property=RuntimeMaxSec=1980') in args
        assert '--property=WorkingDirectory=/' in args
        assert args[args.index('--budget-minutes') + 1] == ('150' if execute else '30')
        assert ('--execute' in args) is execute
        assert str(SCRIPT) in args


def test_launcher_rejects_unknown_execute_arguments(case):
    result = subprocess.run(['bash', str(SCRIPT.with_name('git_tree_sweep_run.sh')), '--execute', '--unknown'], capture_output=True, text=True)
    assert result.returncode == 2


def test_systemd_percent_home_uses_the_unit_user(case, monkeypatch):
    c = case
    unit = c.root / 'tool.service'
    unit.write_text('User=toolowner\nExecStart=%h/bin/tool\n')
    monkeypatch.setattr(c.m.pwd, 'getpwnam', lambda _: SimpleNamespace(pw_dir=str(c.repo)))
    anchors = {}
    assert not c.m.config_anchors(anchors, [(str(unit),'/root')])
    assert c.m.ws.anchored(anchors, str(c.repo))


def test_shell_configuration_root_with_colon_is_one_path(case):
    c = case
    anchors = {}
    path = c.root / 'tool:clone'
    assert not c.m.text_anchors(anchors, 'TOOL_ROOT="' + str(path) + '"', '/root', 'shell')
    assert c.m.ws.anchored(anchors, str(path))


def test_global_unknown_denies_an_otherwise_eligible_clone(case):
    c = case
    row = c.m.plan(str(c.repo), 'clone', inv(c), {}, {}, 'process scan incomplete')
    assert row['action'] == 'keep' and row['reason'] == 'process scan incomplete'


def test_clone_requires_fd_safe_deletion_support(case, monkeypatch):
    c = case
    row = plan(c)
    monkeypatch.setattr(c.m.shutil.rmtree, 'avoids_symlink_attacks', False)
    with pytest.raises(c.m.ws.Keep, match='fd-based'): c.m.remove(row, inv(c), {}, {})
    assert c.repo.exists()


@pytest.mark.parametrize('consumer', ['container', 'thread'])
def test_legacy_consumers_resolve_symlinked_parent_paths(case, monkeypatch, consumer):
    c = case
    alias = c.root / 'container-alias'
    alias.symlink_to(c.repo.parent, target_is_directory=True)
    source = str(alias / c.repo.name / 'bin')
    def reserve(anchors, *args): c.m.ws.add_anchor(anchors, source, consumer)
    if consumer == 'container':
        monkeypatch.setattr(c.m, 'process_anchors', lambda _: None)
        monkeypatch.setattr(c.m.ws, 'docker_anchors', reserve)
        monkeypatch.setattr(c.m, 'bb_anchors', lambda *a, **kw: [])
        monkeypatch.setattr(c.m, 'config_anchors', lambda *a, **kw: [])
        anchors, unknown = c.m.usage(inv(c))
    else:
        monkeypatch.setattr(c.m.ws, 'bb_anchors', reserve)
        monkeypatch.setattr(c.m, 'command', lambda cmd, **kw: SimpleNamespace(stdout='[]'))
        anchors = {}
        unknown = c.m.bb_anchors(anchors)
    assert not unknown
    assert plan(c, anchors=anchors)['action'] == 'keep'


@pytest.mark.parametrize('name', ['worktree_sweep.py','tmp_clone_sweep.py'])
def test_legacy_clis_delegate_to_unfiltered_entry(name):
    path = SCRIPT.with_name(name)
    result = subprocess.run([os.sys.executable,'-I',str(path),'--help'],capture_output=True,text=True)
    assert result.returncode == 0
    assert 'Inventory all local Git working trees' in result.stdout
    assert '--root' not in result.stdout
    blocked = subprocess.run([os.sys.executable,'-I',str(path),'--root','/some/path'],capture_output=True,text=True)
    assert blocked.returncode == 2 and 'unrecognized arguments' in blocked.stderr


@pytest.mark.parametrize('name', ['node_modules','vendor','.venv','venv','__pypackages__','.pnpm-store'])
def test_dependency_like_name_without_owner_evidence_is_still_discovered(case, name):
    target = case.root / name / 'authored-clone'
    (target / '.git').mkdir(parents=True)
    assert str(target) in inv(case)['trees']


def test_external_local_bare_remote_proves_recovery_without_a_network_mock(case):
    c = case
    shell('git','remote','set-url','origin',str(c.bare),cwd=c.repo)
    age(c.repo)
    assert plan(c)['action'] == 'remove'
    assert c.bare.exists()


def test_referenced_local_checkout_is_retained_as_the_recovery_copy(case):
    c = case
    backup = c.root / 'backup'
    shell('git','clone','-q',str(c.bare),str(backup))
    shell('git','remote','set-url','origin',str(backup),cwd=c.repo)
    age(c.root)
    refs=c.m.references(inv(c))
    assert plan(c,refs=refs)['action'] == 'remove'
    assert plan(c,backup,refs=refs)['action'] == 'keep'


def test_self_remote_is_not_an_independent_recovery_copy(case):
    c = case
    shell('git','remote','set-url','origin',str(c.repo),cwd=c.repo)
    age(c.repo)
    assert plan(c)['action'] == 'keep'


def test_filesystem_root_is_listed_but_cannot_become_a_delete_candidate(case):
    c = case
    row = c.m.plan('/', 'clone', {'trees':{},'bare':[]}, {}, {})
    assert row['action'] == 'keep' and 'filesystem root' in row['reason']
    with pytest.raises(c.m.ws.Keep,match='directory entry'):
        with c.m.physical_parent('/'): pass


def test_mount_point_is_retained_at_the_local_boundary(case, monkeypatch):
    c = case
    monkeypatch.setattr(c.m.os.path,'ismount',lambda path: path==str(c.repo))
    assert 'mount point' in plan(c)['reason']


def scoped(case, text, trees=None, **owner):
    c = case
    inventory = {'trees': trees or {str(c.repo): 'clone'}, 'remote_urls': {}}
    anchors = {}
    records = c.m.text_anchors(anchors, text, '/root', 'test source', {'PATH': '/usr/bin'})
    for record in records:
        record.update(owner)
    c.m.scope_unknown(anchors, records, inventory)
    return anchors, records


@pytest.mark.parametrize('operand', ['{root}/$name/bin', '{root}/*/$name/bin', '{root}/${name}/$other', '{root}/pre$name/bin'])
def test_unknown_prefix_protects_descendants_and_containing_tree_only(case, operand):
    c = case
    prefix = str(c.repo / 'ignored with spaces')
    expression = operand.replace("{root}", prefix)
    child = prefix + ('/prefix' if '/pre$' in expression else '/child')
    sibling = str(c.root / 'unrelated')
    anchors, records = scoped(c, 'source "' + expression + '"',
                              {str(c.repo): 'clone', child: 'clone', sibling: 'linked'})
    assert records and all(r['scope'] == 'prefix' for r in records)
    assert set().union(*(set(r['protected_paths']) for r in records)) == {str(c.repo), child}
    assert not c.m.global_unknown(records)
    assert not c.m.ws.anchored(anchors, sibling)


def test_loop_variable_keeps_its_absolute_prefix(case):
    c = case
    _, records = scoped(c, f'for checkout in "{c.repo}/$name"; do source "$checkout/init.sh"; done')
    assert records and all(r['scope'] == 'prefix' for r in records)
    assert all(str(c.repo) in r['protected_paths'] for r in records)


def test_unrelated_literal_on_same_line_cannot_bound_unknown(case):
    _, records = scoped(case, 'echo /usr/bin; source "$UNBOUND_HOME/init.sh"')
    assert records and any(r['scope'] == 'global' for r in records)
    assert case.m.global_unknown(records)


@pytest.mark.parametrize('expression', ['source "$UNKNOWN_HOME/init.sh"', 'source relative/tool', 'export TOOL_ROOT="$(locate-tool)"'])
def test_unowned_unknown_names_source_and_blocks_global_generation(case, expression):
    anchors, records = scoped(case, expression)
    assert records and all(r['scope'] == 'global' for r in records)
    assert case.m.global_unknown(records)[0].startswith('test source: ')
    assert all(r['protected_paths'] == [str(case.repo)] for r in records)
    assert plan(case, anchors=anchors)['action'] == 'keep'


def test_unknown_prefix_canonicalizes_symlink_parent(case):
    c = case
    alias = c.root / 'alias'
    alias.symlink_to(c.repo, target_is_directory=True)
    _, records = scoped(c, f'source "{alias}/$name/init.sh"')
    assert records[0]['prefixes'][0]['path'] == str(c.repo)
    assert records[0]['protected_paths'] == [str(c.repo)]


@pytest.mark.parametrize('path,expected', [('/tmp/$name/bin', '/data/system-tmp'), ('/$name/init', '/')])
def test_unknown_prefix_handles_bind_alias_and_root(case, monkeypatch, path, expected):
    monkeypatch.setattr(case.m.os.path, 'samefile', lambda *a: True)
    records = case.m.text_anchors({}, 'source "' + path + '"', '/root', 'shell')
    assert records[0]['prefixes'][0]['path'] == expected
    inventory = {'trees': {expected + '/tree': 'clone'}}
    case.m.scope_unknown({}, records, inventory)
    assert records[0]['protected_paths'] == [expected + '/tree']


@pytest.mark.parametrize('values', [1, 2])
def test_observed_process_values_are_all_substituted(case, monkeypatch, values):
    c = case
    monkeypatch.setattr(c.m.os, 'environ', {})
    paths = {str(c.repo), str(c.root / 'second')} if values == 2 else {str(c.repo)}
    anchors = {}
    records = c.m.environment_references(anchors, 'source "$TOOL_ROOT/init.sh"', '/root', 'shell', {'TOOL_ROOT': paths})
    assert records == []
    assert all(c.m.ws.anchored(anchors, path) for path in paths)


def test_process_snapshot_returns_real_values_for_later_substitution(case, tmp_path):
    p = tmp_path / '123'
    (p / 'fd').mkdir(parents=True)
    (p / 'cwd').symlink_to(case.repo)
    (p / 'status').write_text('Uid: 0 0 0 0\n')
    (p / 'stat').write_text('123 (proc) ' + ' '.join(['0'] * 22))
    (p / 'maps').write_text('')
    (p / 'environ').write_text('TERM=dumb\0TOOL_ROOT=' + str(case.repo) + '\0')
    env = case.m.process_anchors({}, str(tmp_path))
    assert env['TERM'] == {'dumb'} and env['TOOL_ROOT'] == {str(case.repo)}


def test_project_fallback_matches_effective_fetch_push_urls_and_linked_trees(case):
    c = case
    work = linked(c)
    other = c.root / 'same remote'
    shell('git', 'clone', '-q', str(c.bare), str(other))
    shell('git', 'remote', 'set-url', 'origin', 'git@git.woa.com:group/repo.git', cwd=other)
    different = c.root / 'different'
    shell('git', 'init', '-q', str(different))
    shell('git', 'config', 'remote.origin.url', 'https://git.woa.com/group/different.git', cwd=different)
    inventory = inv(c)
    c.m.references(inventory)
    record = c.m.unresolved('paused automation a', 'source "$checkout/init.sh"')
    record.update(project_id='p', project_paths=[str(c.repo)], project_remote_ids=[])
    c.m.scope_unknown({}, [record], inventory)
    assert record['scope'] == 'project'
    assert set(record['protected_paths']) == {str(c.repo), str(work), str(other)}
    assert str(different) not in record['protected_paths']


@pytest.mark.parametrize('enabled', [False, True])
def test_bb_unknown_keeps_project_env_metadata_and_disabled_tasks(case, monkeypatch, enabled):
    c = case
    script = c.root / 'automation.sh'
    script.write_text('source "$checkout/init.sh"\nsource "$PWD/tools.sh"')
    env = str(c.root / 'environment')
    responses = {'project': [{'id': 'p', 'sources': [{'path': str(c.repo)}]}],
                 'environment': [{'id': 'e', 'projectId': 'p', 'status': 'ready', 'path': env}],
                 'automation': [{'id': 'a', 'enabled': enabled, 'execution': {'storedScriptPath': str(script), 'workingDirectory': {'type': 'project'}}}]}
    monkeypatch.setattr(c.m, 'command', lambda cmd, **kw: SimpleNamespace(stdout=json.dumps([] if cmd[1] == 'automation' and 'proj_personal' in cmd else responses[cmd[1]])))
    monkeypatch.setattr(c.m.ws, 'bb_anchors', lambda *a: None)
    anchors = {}
    records = c.m.bb_anchors(anchors, {})
    c.m.scope_unknown(anchors, records, {'trees': {str(c.repo): 'clone', env: 'linked'}})
    assert records and all(r['scope'] == 'project' for r in records)
    assert all(r['automation_enabled'] is enabled for r in records)
    assert all(set(r['protected_paths']) == {str(c.repo), env} for r in records)
    assert all('PWD' not in r['expression'] for r in records)


def test_unknown_report_lists_paths_even_if_already_protected(case):
    c = case
    anchors = {str(c.repo): 'BB project source'}
    r = c.m.unresolved('config', f'source "{c.repo}/$x/init"', [f'{c.repo}/$x/init'])
    c.m.scope_unknown(anchors, [r], {'trees': {str(c.repo): 'clone'}})
    assert r['protected_paths'] == [str(c.repo)]
    assert json.loads(json.dumps(r))['scope'] == 'prefix'


@pytest.mark.parametrize('expression', ['source "$HOME/tools/$name/init.sh"', 'source /etc/profile.d/$name.sh'])
def test_scoped_unknown_allows_unrelated_clone_to_reach_remote_proof(case, expression):
    c = case
    anchors, records = scoped(c, expression)
    assert not c.m.global_unknown(records)
    row = plan(c, anchors=anchors)
    assert row['action'] == 'remove'
    assert row['remote_proofs'] and all(row['remote_proofs'].values())


@pytest.mark.parametrize('protect', [False, True])
def test_execution_rechecks_scoped_unknown_without_global_veto(case, monkeypatch, protect):
    c = case
    row = plan(c)
    row['bytes'] = 4096
    def usage(inventory):
        root = c.repo if protect else c.root / 'elsewhere'
        anchors = {}
        records = c.m.text_anchors(anchors, f'source "{root}/$name/init"', '/root', 'new config')
        return anchors, c.m.scope_unknown(anchors, records, inventory)
    monkeypatch.setattr(c.m, 'usage', usage)
    c.m.execute([row], inv(c), {}, {}, [str(c.root)], set(), 'scoped', c.root / 'events')
    assert c.repo.exists() is protect
    assert row['result'] == ('kept' if protect else 'removed')


def test_system_unit_description_is_prose_and_path_fields_are_still_consumers(case):
    c = case
    unit = c.root / 'tool.service'
    unit.write_text('[Unit]\nDescription=Read-only source image timer\n[Service]\nEnvironment=TOOL_ROOT=' + str(c.repo) + '\nExecStart=/usr/bin/python3 ' + str(c.repo / 'tool.py') + '\n')
    anchors = {}
    assert not c.m.config_anchors(anchors, sources=[(str(unit), '/root')])
    assert c.m.ws.anchored(anchors, str(c.repo))


def test_profile_loop_uses_observed_term_and_go_metadata_without_shell(case, monkeypatch):
    c = case
    monkeypatch.setattr(c.m.os, 'environ', {})
    text = 'for file in "$HOME/.colors.$TERM"; do test -f "$file"; done\nexport PATH="$PATH:$(/usr/local/go/bin/go env GOPATH)/bin"'
    anchors = {}
    assert not c.m.environment_references(anchors, text, '/root', 'profile',
        {'TERM': {'dumb'}, 'PATH': {'/usr/bin'}, '__GO_EXE': {'/usr/local/go/bin/go'}, '__GO_GOPATH': {str(c.repo)}})
    assert c.m.ws.anchored(anchors, str(c.repo))
    assert not c.calls


def test_process_environment_contexts_preserve_observed_correlations(case, monkeypatch):
    c = case
    monkeypatch.setattr(c.m.os, 'environ', {})
    env = c.m.ProcessEnvironments()
    env.contexts = [{'TOOL_ROOT': str(c.root / 'first'), 'EDITION': 'one'},
                    {'TOOL_ROOT': str(c.root / 'second'), 'EDITION': 'two'}]
    for context in env.contexts:
        for key, value in context.items():
            env.setdefault(key, set()).add(value)
    anchors = {}
    assert not c.m.environment_references(anchors, 'source "$TOOL_ROOT/$EDITION/init"', '/root', 'profile', env)
    assert str(c.root / 'first/one/init') in anchors
    assert str(c.root / 'second/two/init') in anchors
    assert str(c.root / 'first/two/init') not in anchors
    assert str(c.root / 'second/one/init') not in anchors


def test_dynamic_systemd_environment_value_has_scoped_protection(case):
    c = case
    unit = c.root / 'tool.service'
    unit.write_text('[Service]\nEnvironment="TOOL_ROOT=' + str(c.repo) + '/$flavour"\n')
    anchors = {}
    records = c.m.config_anchors(anchors, sources=[(str(unit), '/root')])
    c.m.scope_unknown(anchors, records, inv(c))
    assert records and all(r['scope'] == 'prefix' for r in records)
    assert all(str(c.repo) in r['protected_paths'] for r in records)


def test_missing_process_value_preserves_shell_default(case, monkeypatch):
    c = case
    monkeypatch.setattr(c.m.os, 'environ', {'PATH': '/usr/bin'})
    env = c.m.ProcessEnvironments()
    env.contexts = [{'XDG_CONFIG_HOME': str(c.repo)}, {'PATH': '/usr/bin'}]
    env.update({'XDG_CONFIG_HOME': {str(c.repo)}, 'PATH': {'/usr/bin'}})
    anchors = {}
    assert not c.m.environment_references(anchors, 'source "${XDG_CONFIG_HOME:-$HOME/.config}/init"', '/root', 'profile', env)
    assert c.m.ws.anchored(anchors, str(c.repo))


def test_systemd_multiline_command_is_parsed_as_one_statement(case):
    c = case
    unit = c.root / 'multiline.service'
    unit.write_text('[Service]\nExecStart=/bin/bash -c \'read args <&3; \\\n exec ' + str(c.repo / 'tool') + '; exit 0\'\n')
    anchors = {}
    assert not c.m.config_anchors(anchors, sources=[(str(unit), '/root')])
    assert c.m.ws.anchored(anchors, str(c.repo))


def test_default_assignment_carries_path_to_following_statement(case):
    c = case
    anchors = {}
    assert not c.m.text_anchors(anchors, ': "${TOOL_ROOT:=' + str(c.repo) + '}"\nsource "$TOOL_ROOT/init"', '/root', 'profile')
    assert c.m.ws.anchored(anchors, str(c.repo))


def test_dynamic_absolute_scratch_assignment_keeps_its_prefix(case):
    anchors, records = scoped(case, 'tool="' + str(case.repo) + '/$version"\n"$tool"')
    assert records and all(r['scope'] == 'prefix' for r in records)
    assert str(case.repo) in records[0]['protected_paths']
    assert plan(case, anchors=anchors)['action'] == 'keep'


def test_project_remote_metadata_scopes_unknown_even_without_local_checkout(case):
    c = case
    inventory = inv(c)
    c.m.references(inventory)
    record = c.m.unresolved('automation', 'source "$checkout/init"')
    record.update(project_id='remote-only', project_paths=[],
                  project_remote_ids=[c.m.remote_identity('git@git.woa.com:group/repo.git', '/')])
    c.m.scope_unknown({}, [record], inventory)
    assert record['scope'] == 'project'
    assert record['protected_paths'] == [str(c.repo)]
    assert not c.m.global_unknown([record])


@pytest.mark.parametrize('push', [False, True])
def test_broken_git_stays_keep_but_effective_urls_still_protect_consumers(case, push):
    c = case
    broken = c.root / 'lost HEAD'
    shell('git', 'init', '-q', str(broken))
    shell('git', 'config', 'remote.origin.url', 'https://example.com/read-only', cwd=broken)
    if not push:
        shell('git', 'config', 'remote.origin.url', 'local:repo', cwd=broken)
        shell('git', 'config', 'url.' + str(c.repo) + '/.insteadOf', 'local:', cwd=broken)
    else:
        shell('git', 'config', 'url.' + str(c.repo) + '/.pushInsteadOf', 'https://example.com/', cwd=broken)
    (broken / '.git/HEAD').unlink()
    inventory = inv(c)
    refs = c.m.references(inventory)
    assert c.m.ws.anchored(refs, str(c.repo))
    assert c.m.plan(str(broken), 'clone', inventory, {}, refs)['action'] == 'keep'
    assert not (broken / '.git/HEAD').exists()
    assert c.m.remote_identity(str(c.repo / ('read-only' if push else 'repo')), str(broken)) in inventory['remote_urls'][str(broken)]


def test_broken_git_conditional_config_remains_global_unknown(case):
    c = case
    broken = c.root / 'lost HEAD'
    shell('git', 'init', '-q', str(broken))
    shell('git', 'config', 'remote.origin.url', 'https://example.com/repo', cwd=broken)
    shell('git', 'config', 'includeIf.gitdir:/something/.path', str(c.root / 'external.conf'), cwd=broken)
    (broken / '.git/HEAD').unlink()
    with pytest.raises(c.m.ws.ScanIncomplete, match='conditional config unknown'):
        c.m.references(inv(c))


def test_broken_git_global_conditional_config_stays_unknown(case, monkeypatch):
    c = case
    broken = c.root / 'lost HEAD'
    shell('git', 'init', '-q', str(broken))
    shell('git', 'config', 'remote.origin.url', 'https://example.com/repo', cwd=broken)
    (broken / '.git/HEAD').unlink()
    config = c.root / 'global-config'
    config.write_text('[includeIf "gitdir:/something/"]\npath=/nonexistent/config\n')
    monkeypatch.setattr(c.m.ws, 'ENV', dict(c.m.ws.ENV, GIT_CONFIG_GLOBAL=str(config)))
    with pytest.raises(c.m.ws.ScanIncomplete, match='conditional config unknown'):
        c.m.references(inv(c))


@pytest.mark.parametrize('kind', ['source', 'loop', 'environment'])
def test_pure_unmatched_glob_protects_all_git_trees_below_prefix(case, kind):
    c = case
    pattern = str(c.root / '*' / 'no-matching-init-file')
    scripts = {'source': f'source "{pattern}"',
               'loop': f'for file in "{pattern}"; do source "$file"; done',
               'environment': f'Environment="TOOL_ROOT={pattern}"'}
    anchors, records = scoped(c, scripts[kind])
    assert records and all(r['scope'] == 'prefix' for r in records)
    assert not c.m.global_unknown(records)
    assert all(str(c.repo) in r['protected_paths'] for r in records)
    assert plan(c, anchors=anchors)['action'] == 'keep'


@pytest.mark.parametrize('substitution', ['$(tool locate)', '`tool locate`', '$(tool $(nested $(another nested)))'])
def test_loop_command_substitution_retains_literal_absolute_prefix(case, substitution):
    c = case
    text = f'for file in "{c.repo}/{substitution}"; do source "$file/init"; done'
    _, records = scoped(c, text)
    assert records and all(r['scope'] == 'prefix' for r in records)
    assert all(r['protected_paths'] == [str(c.repo)] for r in records)
    assert not c.m.global_unknown(records)


@pytest.mark.parametrize('operator', [':?', ':?message', ':+' ])
def test_bb_explicit_env_modifiers_protect_paths_outside_project(case, monkeypatch, operator):
    c = case
    outside = c.root / 'external-tool'
    script = c.root / 'automation.sh'
    expression = '${TOOL_HOME' + operator + (str(outside) if operator == ':+' else '') + '}'
    script.write_text('source "' + expression + '/init.sh"')
    responses = {'project': [{'id': 'p', 'sources': [{'path': str(c.repo)}]}], 'environment': [],
                 'automation': [{'id': 'a', 'execution': {'storedScriptPath': str(script), 'env': {'TOOL_HOME': str(outside)}}}]}
    monkeypatch.setattr(c.m, 'command', lambda cmd, **kw: SimpleNamespace(stdout=json.dumps([] if cmd[1] == 'automation' and 'proj_personal' in cmd else responses[cmd[1]])))
    monkeypatch.setattr(c.m.ws, 'bb_anchors', lambda *a: None)
    anchors = {}
    assert not c.m.bb_anchors(anchors)
    assert c.m.ws.anchored(anchors, str(outside))


@pytest.mark.parametrize('operator', [':?', ':+' ])
def test_unknown_required_or_alternative_parameter_remains_unknown(case, operator):
    assert case.m.text_anchors({}, 'source "${MISSING_HOME' + operator + '}/init"', '/root', 'config')


def test_alternative_parameter_expands_nested_known_value(case):
    c = case
    anchors = {}
    assert not c.m.text_anchors(anchors, 'source "${ENABLED:+${TOOL_HOME}}/init"', '/root', 'shell',
                                {'ENABLED': '1', 'TOOL_HOME': str(c.repo)})
    assert c.m.ws.anchored(anchors, str(c.repo))


def test_broken_gitfile_readable_config_still_protects_remote_target(case):
    c = case
    broken, metadata = c.root / 'broken linked', c.root / 'metadata'
    shell('git', 'init', '-q', '--separate-git-dir', str(metadata), str(broken))
    shell('git', 'config', 'remote.origin.url', str(c.repo), cwd=broken)
    (metadata / 'HEAD').unlink()
    inventory = inv(c)
    refs = c.m.references(inventory)
    assert c.m.ws.anchored(refs, str(c.repo))
    assert c.m.plan(str(broken), 'linked', inventory, {}, refs)['action'] == 'keep'
    assert not (metadata / 'HEAD').exists()


@pytest.mark.parametrize('push', [False, True])
def test_project_metadata_urls_apply_git_rewrites_before_scope_matching(case, monkeypatch, push):
    c = case
    config = c.root / 'global-url-config'
    if push:
        value = 'https://read.example/group/repo.git'
        config.write_text('[url "ssh://git@write.example/"]\n pushInsteadOf=https://read.example/\n')
        shell('git', 'remote', 'set-url', 'origin', 'https://different.example/repo.git', cwd=c.repo)
        shell('git', 'config', 'remote.origin.pushurl', 'ssh://git@write.example/group/repo.git', cwd=c.repo)
        expected = 'write.example/group/repo'
    else:
        value = 'short:group/repo.git'
        config.write_text('[url "https://rewritten.example/"]\n insteadOf=short:\n')
        shell('git', 'remote', 'set-url', 'origin', 'https://rewritten.example/group/repo.git', cwd=c.repo)
        expected = 'rewritten.example/group/repo'
    monkeypatch.setattr(c.m.ws, 'ENV', dict(c.m.ws.ENV, GIT_CONFIG_GLOBAL=str(config)))
    identities = c.m.project_url_identities(value, [])
    assert expected in identities
    inventory = inv(c); c.m.references(inventory)
    record = c.m.unresolved('automation', 'source "$checkout/init"')
    record.update(project_id='p', project_paths=[], project_remote_ids=identities)
    c.m.scope_unknown({}, [record], inventory)
    assert record['scope'] == 'project' and record['protected_paths'] == [str(c.repo)]


def test_local_url_suffix_preserves_distinct_physical_stores(case):
    c = case
    distinct = Path(str(c.repo) + '.git')
    shell('git', 'init', '--bare', '-q', str(distinct))
    other = c.root / 'other-consumer'
    shell('git', 'init', '-q', str(other))
    shell('git', 'config', 'remote.origin.url', str(distinct), cwd=other)
    assert c.m.remote_identity(str(c.repo), '/') == c.m.remote_identity(str(c.repo / '.git'), '/')
    assert c.m.remote_identity(str(c.repo), '/') != c.m.remote_identity(str(distinct), '/')
    inventory = inv(c); c.m.references(inventory)
    record = c.m.unresolved('project', 'source "$checkout/init"')
    record.update(project_id='p', project_paths=[str(c.repo)], project_remote_ids=[c.m.remote_identity(str(c.repo), '/')])
    c.m.scope_unknown({}, [record], inventory)
    assert str(c.repo) in record['protected_paths'] and str(other) not in record['protected_paths']


def test_nonpath_systemd_environment_glob_does_not_reserve_entire_generation(case):
    assert not case.m.text_anchors({}, 'Environment="URL=https://example.com?q=a"', '/root', 'unit')


def test_for_sequence_resolves_required_parameter_before_binding(case):
    c = case
    records = c.m.text_anchors({}, 'for file in "${TOOL_HOME:?}/$(tool locate)"; do source "$file/init"; done',
                               '/root', 'config', {'TOOL_HOME': str(c.repo)})
    c.m.scope_unknown({}, records, {'trees': {str(c.repo): 'clone'}})
    assert records and all(r['scope'] == 'prefix' for r in records)


def test_bb_project_metadata_rewrite_protects_same_remote_clone_end_to_end(case, monkeypatch):
    c = case
    config = c.root / 'global-url-config'
    config.write_text('[url "https://rewritten.example/"]\n insteadOf=short:\n')
    monkeypatch.setattr(c.m.ws, 'ENV', dict(c.m.ws.ENV, GIT_CONFIG_GLOBAL=str(config)))
    shell('git', 'remote', 'set-url', 'origin', 'https://rewritten.example/group/repo.git', cwd=c.repo)
    inventory = inv(c); c.m.references(inventory)
    script = c.root / 'unknown-consumer.sh';script.write_text('source "$checkout/init.sh"')
    raw = c.m.command
    def command(cmd, **kw):
        if cmd[0] != 'bb': return raw(cmd, **kw)
        data = {'project': [{'id': 'p', 'gitRemoteUrl': 'short:group/repo.git', 'sources': []}], 'environment': [],
                'automation': [] if 'proj_personal' in cmd else [{'id': 'a', 'execution': {'storedScriptPath': str(script)}}]}
        return SimpleNamespace(stdout=json.dumps(data[cmd[1]]))
    monkeypatch.setattr(c.m, 'command', command)
    monkeypatch.setattr(c.m.ws, 'bb_anchors', lambda *a: None)
    anchors = {};records = c.m.bb_anchors(anchors)
    c.m.scope_unknown(anchors, records, inventory)
    assert records and all(r['protected_paths'] == [str(c.repo)] for r in records)
    assert c.m.ws.anchored(anchors, str(c.repo))


def test_project_url_scratch_cannot_write_inherited_git_directory(case, monkeypatch):
    c = case
    before = (c.repo / '.git/config').read_bytes()
    monkeypatch.setattr(c.m.ws, 'ENV', dict(c.m.ws.ENV, GIT_DIR=str(c.repo / '.git'), GIT_WORK_TREE=str(c.repo)))
    assert 'example.com/group/repo' in c.m.project_url_identities('https://example.com/group/repo.git', [])
    assert (c.repo / '.git/config').read_bytes() == before
    assert not (c.repo / '.git/refs/heads/project').exists()


def test_project_url_scratch_uses_source_rules_without_temp_conditional_rules(case, monkeypatch):
    c = case
    config = c.root / 'global-url-config'
    extra = c.root / 'temp-rules'
    extra.write_text('[url "https://wrong.example/"]\n insteadOf=short:group/\n')
    config.write_text('[url "https://correct.example/"]\n insteadOf=short:\n'
                      '[includeIf "gitdir:/tmp/git-project-url-*/"]\n path=' + str(extra) + '\n')
    monkeypatch.setattr(c.m.ws, 'ENV', dict(c.m.ws.ENV, GIT_CONFIG_GLOBAL=str(config)))
    assert c.m.project_url_identities('short:group/repo.git', []) == ['correct.example/group/repo']


def test_bb_explicit_home_is_used_for_tool_reference(case, monkeypatch):
    c = case
    outside = c.root / 'external-home'
    script = c.root / 'automation.sh';script.write_text('source "$HOME/tool/init.sh"')
    responses = {'project': [{'id': 'p', 'sources': [{'path': str(c.repo)}]}], 'environment': [],
                 'automation': [{'id': 'a', 'execution': {'storedScriptPath': str(script), 'env': {'HOME': str(outside)}}}]}
    monkeypatch.setattr(c.m, 'command', lambda cmd, **kw: SimpleNamespace(stdout=json.dumps([] if cmd[1] == 'automation' and 'proj_personal' in cmd else responses[cmd[1]])))
    monkeypatch.setattr(c.m.ws, 'bb_anchors', lambda *a: None)
    anchors = {}; assert not c.m.bb_anchors(anchors)
    assert c.m.ws.anchored(anchors, str(outside / 'tool'))


@pytest.mark.parametrize('field', ['WorkingDirectory', 'ExecStart'])
@pytest.mark.parametrize('known', [False, True])
def test_systemd_environment_dynamic_bare_path_is_resolved_or_unknown(case, field, known):
    c = case
    variables = {'OBSERVED': str(c.repo)} if known else {}
    anchors = {}
    records = c.m.text_anchors(anchors, f'Environment=WORKTREE=$OBSERVED\n{field}=$WORKTREE', '/root', 'unit', variables)
    if known:
        assert not records and c.m.ws.anchored(anchors, str(c.repo))
    else:
        c.m.scope_unknown(anchors, records, inv(c))
        assert records and c.m.global_unknown(records)
        assert all(str(c.repo) in r['protected_paths'] for r in records)


@pytest.mark.parametrize('injection', ['directory', 'config_count'])
def test_project_source_url_rules_ignore_inherited_repository_and_injection(case, monkeypatch, injection):
    c = case
    config = c.root / 'global-rules'
    config.write_text('[url "https://right.example/"]\n insteadOf=short:\n')
    shell('git', 'config', 'url.https://wrong.example/.insteadOf', 'short:group/', cwd=c.repo)
    values = dict(c.m.ws.ENV, GIT_CONFIG_GLOBAL=str(config))
    if injection == 'directory':
        values.update(GIT_DIR=str(c.repo / '.git'), GIT_WORK_TREE=str(c.repo))
    else:
        values.update(GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='url.https://wrong.example/.insteadOf', GIT_CONFIG_VALUE_0='short:group/')
    monkeypatch.setattr(c.m.ws, 'ENV', values)
    assert c.m.project_url_identities('short:group/repo.git', []) == ['right.example/group/repo']


@pytest.mark.parametrize('gitfile', [False, True])
@pytest.mark.parametrize('push', [False, True])
def test_broken_git_imports_global_fetch_and_push_rewrites(case, monkeypatch, gitfile, push):
    c = case
    broken, metadata = c.root / 'broken', c.root / 'metadata'
    if gitfile:
        shell('git', 'init', '-q', '--separate-git-dir', str(metadata), str(broken))
    else:
        shell('git', 'init', '-q', str(broken)); metadata = broken / '.git'
    value = 'https://fetch.example/repo' if push else 'meta:repo'
    shell('git', 'config', 'remote.origin.url', value, cwd=broken)
    (metadata / 'HEAD').unlink()
    config = c.root / 'global-rewrite'
    key, match = ('pushInsteadOf', 'https://fetch.example/') if push else ('insteadOf', 'meta:')
    config.write_text('[url "' + str(c.repo) + '/"]\n ' + key + '=' + match + '\n')
    monkeypatch.setattr(c.m.ws, 'ENV', dict(c.m.ws.ENV, GIT_CONFIG_GLOBAL=str(config)))
    inventory = inv(c); refs = c.m.references(inventory)
    expected = c.m.remote_identity(str(c.repo / 'repo'), str(broken))
    assert expected in inventory['remote_urls'][str(broken)]
    assert c.m.ws.anchored(refs, str(c.repo))
    assert not (metadata / 'HEAD').exists()


def test_broken_git_unreadable_global_rules_deny_inventory(case, monkeypatch):
    c = case
    broken = c.root / 'broken'
    shell('git', 'init', '-q', str(broken))
    shell('git', 'config', 'remote.origin.url', 'meta:repo', cwd=broken)
    (broken / '.git/HEAD').unlink()
    config = c.root / 'unreadable-global'
    config.write_text('[url "https://example.com/"]\n insteadOf=meta:\n')
    monkeypatch.setattr(c.m.ws, 'ENV', dict(c.m.ws.ENV, GIT_CONFIG_GLOBAL=str(config)))
    raw = c.m._run
    def denied(args, **kw):
        # The test runs as root: emulate Git's real permission-error reply.
        if '--global' in args and any(arg.startswith('^url') for arg in args):
            return SimpleNamespace(returncode=128, stdout='', stderr='permission denied')
        return raw(args, **kw)
    monkeypatch.setattr(c.m, '_run', denied)
    with pytest.raises(c.m.ws.ScanIncomplete, match='global URL rules unknown'): c.m.references(inv(c))


def test_partial_component_prefix_preserves_symlink_spelling_and_target(case):
    c = case
    alias = c.root / 'tool';alias.symlink_to(c.repo, target_is_directory=True)
    sibling, unrelated = str(alias) + '-old', str(c.repo) + '-unrelated'
    _, records = scoped(c, f'source "{alias}$edition/init"',
                        {str(c.repo): 'clone', sibling: 'clone', unrelated: 'clone'})
    assert records and all(r['scope'] == 'prefix' for r in records)
    assert set(records[0]['protected_paths']) == {str(c.repo), sibling}


def test_partial_tmp_prefix_covers_bind_descendants_and_root_siblings(case, monkeypatch):
    c = case
    monkeypatch.setattr(c.m.os.path, 'samefile', lambda *a: True)
    _, records = scoped(c, 'source "/tmp$variant/init"',
                        {'/tmp-tools': 'clone', '/data/system-tmp/tool': 'clone', '/data/system-tmp-other': 'clone'})
    assert records and set(records[0]['protected_paths']) == {'/tmp-tools', '/data/system-tmp/tool'}


@pytest.mark.parametrize('kind', ['cwd', 'HOME', 'XDG_CACHE_HOME'])
@pytest.mark.parametrize('location', ['root', 'mount', 'login', 'isolated', 'xdg_child'])
def test_process_directory_scope_exceptions_and_isolated_homes(case, monkeypatch, kind, location):
    c = case
    login = str(c.root / 'login-home')
    root = {'root':'/', 'mount':str(c.root / 'volume'), 'login':login,
            'isolated':str(c.root / 'instance/home'), 'xdg_child':login + '/.cache'}[location]
    target = root.rstrip('/') + '/clone'
    anchors, scopes = {}, {}
    c.m.process_directory(anchors, scopes, root, login, {str(c.root / 'volume')}, f'process 42 {kind}')
    c.m.scope_directories(anchors, scopes, {'trees':{target:'clone'}})
    if location in ('root','mount','login'):
        assert not c.m.ws.anchored(anchors, target)
        assert not scopes
        if root != '/': assert c.m.ws.anchored(anchors, root)
    else:
        assert c.m.ws.anchored(anchors, target) == f'process 42 {kind} subtree'


@pytest.mark.parametrize('key', ['HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'cwd'])
def test_process_home_from_proc_protects_nested_checkout(case, tmp_path, monkeypatch, key):
    c = case
    proc = tmp_path / 'proc'; p = proc / '987654'
    (p/'fd').mkdir(parents=True)
    instance = c.root / 'server-instance/home'
    (p/'cwd').symlink_to(instance if key == 'cwd' else '/')
    (p/'stat').write_text('987654 (server) ' + ' '.join(['1'] * 25))
    (p/'status').write_text('Uid: 0 0 0 0\n')
    (p/'maps').write_text('')
    (p/'environ').write_text(key + '=' + str(instance) + '\0' if key != 'cwd' else '')
    monkeypatch.setattr(c.m.pwd, 'getpwuid', lambda uid: SimpleNamespace(pw_dir='/root'))
    anchors = {}; env = c.m.process_anchors(anchors, str(proc))
    target = str(instance / '.codebuddy/extensions/tool')
    c.m.scope_directories(anchors, env.scopes, {'trees':{target:'clone'}})
    assert key + ' subtree' in c.m.ws.anchored(anchors, target)


def test_passwd_home_comes_from_process_uid(case, tmp_path, monkeypatch):
    c = case;proc = tmp_path / 'proc';p=proc/'987654';(p/'fd').mkdir(parents=True)
    home = str(c.root/'service-login')
    (p/'cwd').symlink_to(home)
    (p/'stat').write_text('987654 (server) ' + ' '.join(['1'] * 25))
    (p/'status').write_text('Uid: 2345 2345 2345 2345\n');(p/'maps').write_text('')
    (p/'environ').write_text('HOME='+home+'\0')
    observed=[]
    def entry(uid): observed.append(uid);return SimpleNamespace(pw_dir=home)
    monkeypatch.setattr(c.m.pwd,'getpwuid',entry)
    anchors={};env=c.m.process_anchors(anchors,str(proc))
    c.m.scope_directories(anchors,env.scopes,{'trees':{home+'/unreferenced':'clone'}})
    assert observed == [2345] and not c.m.ws.anchored(anchors,home+'/unreferenced')


@pytest.mark.parametrize('text', ["set -g @plugin 'tmux-plugins/tmux-resurrect'", 'plugins=["https://github.com/tmux-plugins/tmux-resurrect.git"]'])
def test_remote_namespace_in_configuration_reserves_tool_by_name(case, text):
    c=case;config=c.root/'.tmux.conf';config.write_text(text)
    inventory={'trees':{str(c.repo):'clone'},'remote_urls':{str(c.repo):['github.com/tmux-plugins/tmux-resurrect']}}
    anchors={};c.m.configuration_name_anchors(anchors,inventory,[str(config)])
    assert 'configuration name reference' in c.m.ws.anchored(anchors,str(c.repo))
    assert inventory['configuration_name_references'][0]['config']==str(config)


@pytest.mark.parametrize('name,protect', [('special-tool',True),('tool with spaces',True),('repo',False),('src',False),('upstream',False),('v0.3.2',False),('abc',False)])
def test_directory_name_reference_requires_distinctive_name(case, name, protect):
    c=case;path=str(c.root/name);config=c.root/'settings';config.write_text('enabled='+name+'\n')
    inventory={'trees':{path:'clone'},'remote_urls':{}};anchors={}
    c.m.configuration_name_anchors(anchors,inventory,[str(config)])
    assert bool(c.m.ws.anchored(anchors,path)) is protect


def test_tool_name_boundary_does_not_match_a_prefix(case):
    c=case;path=str(c.root/'tmux-resurrect');cfg=c.root/'settings';cfg.write_text('tmux-resurrect-other')
    anchors={};c.m.configuration_name_anchors(anchors,{'trees':{path:'clone'}},[str(cfg)])
    assert not c.m.ws.anchored(anchors,path)


def test_config_name_scan_uses_small_text_only_and_denies_read_failure(case, monkeypatch):
    c=case;path=str(c.root/'special-tool');root=c.root/'configs';root.mkdir()
    binary=root/'binary';binary.write_bytes(b'\0special-tool')
    big=root/'large';big.write_text('special-tool'+' '*c.m.CONFIG_TEXT_LIMIT)
    anchors={};c.m.configuration_name_anchors(anchors,{'trees':{path:'clone'}},[str(binary),str(big)])
    assert not anchors
    import builtins
    raw=builtins.open
    def denied(file,*a,**kw):
        if str(file)==str(binary):raise PermissionError('denied')
        return raw(file,*a,**kw)
    monkeypatch.setattr(builtins,'open',denied)
    with pytest.raises(PermissionError):c.m.configuration_name_anchors({}, {'trees':{path:'clone'}},[str(binary)])


def test_nested_independent_clone_is_project_owned_even_if_clean_published(case):
    c=case;target=c.repo/'.local/evidence/upstream'
    shell('git','clone','-q',str(c.bare),str(target));age(c.repo)
    row=plan(c,target)
    assert row['action']=='keep' and row['reason']==f'嵌套于 {c.repo}，由该项目自行保留'


def test_nested_linked_worktree_is_project_owned(case):
    c=case;target=linked(c);age(c.repo)
    row=plan(c,target,'linked')
    assert row['action']=='keep' and row['reason']==f'嵌套于 {c.repo}，由该项目自行保留'


@pytest.mark.parametrize('hours,eligible', [(68,False),(167,False),(169,True),(240,True)])
def test_every_location_requires_seven_days_idle(case, hours, eligible):
    c=case;stamp=time.time()-hours*3600
    for parent,dirs,files in os.walk(c.repo):
        for name in dirs+files:os.utime(Path(parent,name),(stamp,stamp),follow_symlinks=False)
        os.utime(parent,(stamp,stamp))
    row=plan(c)
    assert (row['action']=='remove') is eligible,row
    if not eligible:assert '168h' in row['reason']


def test_name_configuration_sources_exclude_history_and_follow_config_aliases(case, monkeypatch):
    c=case;home=c.root/'login';home.mkdir();config=home/'.config';config.mkdir()
    dot=home/'.tmux.conf';dot.write_text('plugin')
    history=home/'.bash_history-45871.tmp';history.write_text('not config')
    nested=config/'deep';nested.mkdir();(nested/'settings').write_text('plugin')
    external=c.root/'external-config';external.mkdir();(external/'tool.conf').write_text('plugin')
    (config/'linked').symlink_to(external,target_is_directory=True)
    empty=c.root/'empty-etc';empty.mkdir();raw=c.m.os.scandir
    monkeypatch.setattr(c.m.pwd,'getpwall',lambda:[SimpleNamespace(pw_dir=str(home))])
    monkeypatch.setattr(c.m.os,'scandir',lambda p:raw(empty if str(p)=='/etc' else p))
    paths=set(c.m.name_config_sources())
    assert str(dot) in paths and str(nested/'settings') in paths and str(config/'linked/tool.conf') in paths
    assert str(history) not in paths


def test_configuration_name_collision_keeps_all_matching_clones(case):
    c=case;other=str(c.root/'backup');cfg=c.root/'settings';cfg.write_text('group/repo')
    inventory={'trees':{str(c.repo):'clone',other:'clone'},
               'remote_urls':{str(c.repo):['git.example/group/repo'],other:['git.example/group/repo']}}
    anchors={};c.m.configuration_name_anchors(anchors,inventory,[str(cfg)])
    assert all('collision' in c.m.ws.anchored(anchors,p) for p in inventory['trees'])
    assert all(x['collision'] for x in inventory['configuration_name_references'])


def test_reviewed_preview_detects_additional_candidates_and_types(case):
    c=case;p=c.root/'reviewed.json'
    p.write_text(json.dumps({'mode':'dry-run','complete':True,'error':None,
        'items':[{'path':str(c.repo),'type':'clone','action':'remove'}]}))
    allowed={'path':str(c.repo),'type':'clone','action':'archive+remove'}
    assert c.m.reviewed_comparison([allowed],p)['added']==[]
    added=dict(allowed,path=str(c.root/'new'))
    assert c.m.reviewed_comparison([allowed,added],p)['added']==[{'path':added['path'],'type':'clone'}]
    assert c.m.reviewed_comparison([dict(allowed,type='linked')],p)['added']
    p.write_text(json.dumps({'mode':'execute','complete':True}))
    with pytest.raises(c.m.ws.Keep):c.m.reviewed_comparison([allowed],p)


def test_new_candidate_stops_execution_before_archive_or_remove(case, monkeypatch):
    c=case;old=c.root/'reviewed.json';old.write_text(json.dumps({'mode':'dry-run','complete':True,'error':None,
      'items':[{'path':str(c.repo),'type':'clone','action':'remove'}]}))
    other=c.root/'new-candidate';other.mkdir()
    inventory={'trees':{str(c.repo):'clone',str(other):'clone'},'bare':[],'links':[],'errors':[],'excluded':{},'directories':2}
    monkeypatch.setattr(c.m,'managed_roots',lambda:set())
    monkeypatch.setattr(c.m,'discover',lambda *a:inventory)
    monkeypatch.setattr(c.m,'references',lambda _: {})
    monkeypatch.setattr(c.m,'usage',lambda _: ({},[]))
    monkeypatch.setattr(c.m,'plan',lambda path,kind,*a: {'path':path,'type':kind,'action':'remove','archives':[]})
    def forbidden(*a,**kw):raise AssertionError('execution must not start')
    monkeypatch.setattr(c.m,'execute',forbidden)
    state=c.root/'state'
    assert c.m.main(['--execute','--reviewed-preview',str(old),'--state-dir',str(state)])==1
    report=json.loads(next(state.glob('*.json')).read_text())
    assert not report['complete'] and report['reviewed_comparison']['added']==[{'path':str(other),'type':'clone'}]
    assert c.repo.exists() and other.exists()


def test_failure_of_one_project_task_inventory_is_not_ignored(case, monkeypatch):
    c=case
    def command(cmd,**kw):
        if cmd[1]=='project':data=[{'id':'p','sources':[]}]
        elif cmd[1]=='environment':data=[]
        elif cmd[cmd.index('--project')+1]=='proj_personal':raise RuntimeError('personal unreadable')
        else:data=[]
        return SimpleNamespace(stdout=json.dumps(data))
    monkeypatch.setattr(c.m,'command',command)
    with pytest.raises(RuntimeError,match='personal unreadable'):c.m.bb_anchors({})
