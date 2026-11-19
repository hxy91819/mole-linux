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
    shell('git', 'remote', 'add', 'origin', 'https://git.example.invalid/group/repo.git', cwd=repo)
    raw = m._run
    calls = []
    def transport(args, **kw):
        calls.append(list(args))
        args = [str(bare) if a == 'https://git.example.invalid/group/repo.git' else a for a in args]
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
                                   'ignored_secret', 'dependency_secret', 'operation'])
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
    if guard not in ('recent_file', 'recent_metadata'): age(c.repo)
    row = plan(c)
    assert row['action'] == 'keep', row
    expect = {'untracked': 'dirty', 'dirty': 'dirty', 'stash': 'stash', 'recent_file': '168h',
              'recent_metadata': '168h', 'ignored_secret': 'credential', 'dependency_secret': 'credential',
              'operation': 'progress'}
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
    (pid / 'fd').mkdir(parents=True);(pid/'cmdline').write_bytes(b'')
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
    def usage(inventory, refs=None, **kw):
        anchors = {}
        c.m.symlink_anchors(anchors, inventory)
        return anchors, []
    inventory=inv(c);anchors,_=usage(inventory)
    monkeypatch.setattr(c.m,'process_anchors',lambda anchors:{})
    monkeypatch.setattr(c.m,'bb_environment_inventory',lambda:[])
    log = c.root / 'ledger.jsonl'
    c.m.execute([row], inventory, anchors, {}, [str(c.root)], set(), 'test', log)
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


@pytest.mark.parametrize('failure', ['push_rejected', 'wrong_sha', 'new_secret'])
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
        return raw(cmd, **kw)
    monkeypatch.setattr(c.m, '_run', altered)
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
    monkeypatch.setattr(c.m, 'usage', lambda _, refs=None, **kw: ({}, []))
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
                            PROBE_STATE=state, PROBE_CAPTURE=str(capture),BB_CLI=str(bound_test_cli(c))), capture_output=True, text=True)
    assert (result.returncode == 0) == success, result.stderr
    assert capture.exists() == launched
    if launched:
        args = json.loads(capture.read_text())
        assert '--property=RuntimeMaxSec=3180' in args
        assert '--property=WorkingDirectory=/' in args
        assert any(value.startswith('--setenv=BB_CLI=') and value.endswith('/server package/bb') for value in args)
        assert str(SCRIPT.with_name('git_tree_sweep_worker.sh')) in args
        assert ('--execute' in args) is execute
        assert '/root/.pyenv/versions/3.12.12/bin/python3' in args


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
        monkeypatch.setattr(c.m, 'name_config_sources', lambda: [])
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
    expected = 'Registered linked worktrees' if name == 'worktree_sweep.py' else 'Full-disk independent clones'
    assert expected in result.stdout
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
    (p / 'fd').mkdir(parents=True);(p/'cmdline').write_bytes(b'')
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
    shell('git', 'remote', 'set-url', 'origin', 'git@git.example.invalid:group/repo.git', cwd=other)
    different = c.root / 'different'
    shell('git', 'init', '-q', str(different))
    shell('git', 'config', 'remote.origin.url', 'https://git.example.invalid/group/different.git', cwd=different)
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
def test_execution_preserves_scoped_unknown_from_round_snapshot(case, monkeypatch, protect):
    c = case
    row = plan(c)
    row['bytes'] = 4096
    def usage(inventory, refs=None, **kw):
        root = c.repo if protect else c.root / 'elsewhere'
        anchors = {}
        records = c.m.text_anchors(anchors, f'source "{root}/$name/init"', '/root', 'new config')
        return anchors, c.m.scope_unknown(anchors, records, inventory)
    inventory=inv(c);anchors,records=usage(inventory)
    assert records and not c.m.global_unknown(records)
    monkeypatch.setattr(c.m,'process_anchors',lambda anchors:{})
    monkeypatch.setattr(c.m,'bb_environment_inventory',lambda:[])
    c.m.execute([row], inventory, anchors, {}, [str(c.root)], set(), 'scoped', c.root / 'events')
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
                  project_remote_ids=[c.m.remote_identity('git@git.example.invalid:group/repo.git', '/')])
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


@pytest.mark.parametrize('kind', ['HOME', 'XDG_CACHE_HOME'])
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


@pytest.mark.parametrize('key', ['HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME'])
def test_process_home_from_proc_protects_nested_checkout(case, tmp_path, monkeypatch, key):
    c = case
    proc = tmp_path / 'proc'; p = proc / '987654'
    (p/'fd').mkdir(parents=True);(p/'cmdline').write_bytes(b'')
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
    c = case;proc = tmp_path / 'proc';p=proc/'987654';(p/'fd').mkdir(parents=True);(p/'cmdline').write_bytes(b'')
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


def test_foreign_nested_dmca_clone_is_project_owned_even_if_clean_published(case):
    c=case;target=c.repo/'.local/evidence/upstream'
    shell('git','clone','-q',str(c.bare),str(target));age(c.repo)
    row=plan(c,target)
    assert row['action']=='keep' and row['reason']==f'嵌套于 {c.repo}，由该项目自行保留'


def test_same_origin_nested_linked_worktree_is_a_candidate(case):
    c=case;target=linked(c);age(c.repo)
    row=plan(c,target,'linked')
    assert row['action']=='remove',row


def test_same_origin_nested_clone_is_a_candidate(case):
    c=case;target=c.repo/'.worktrees/copy'
    shell('git','clone','-q',str(c.bare),str(target))
    shell('git','remote','set-url','origin','https://git.example.invalid/group/repo.git',cwd=target)
    age(c.repo)
    row=plan(c,target)
    assert row['action']=='remove',row


def test_nested_clone_without_remote_stays_project_owned(case):
    c=case;target=c.repo/'.local/dmca-20261118/upstream'
    shell('git','clone','-q',str(c.bare),str(target));shell('git','remote','remove','origin',cwd=target);age(c.repo)
    row=plan(c,target)
    assert row['action']=='keep' and row['reason']==f'嵌套于 {c.repo}，由该项目自行保留'


def test_same_origin_linked_without_remote_reaches_recovery_check(case):
    c=case;target=linked(c);shell('git','remote','remove','origin',cwd=c.repo);age(c.repo)
    row=plan(c,target,'linked')
    assert row['action']=='keep' and row['reason']=='no independently recoverable remote'


def test_nested_same_origin_still_keeps_dirty_and_referenced_work(case):
    c=case;target=linked(c);(target/'new').write_text('private');age(c.repo)
    assert 'dirty working tree' in plan(c,target,'linked')['reason']
    (target/'new').unlink();age(c.repo)
    assert plan(c,target,'linked',anchors={str(target):'BB environment: live'})['reason']=='BB environment: live'


def test_nested_parent_remote_change_rebinds_relationship(case):
    c=case;target=c.repo/'.worktrees/copy'
    shell('git','clone','-q',str(c.bare),str(target))
    shell('git','remote','set-url','origin','https://git.example.invalid/group/repo.git',cwd=target);age(c.repo)
    inventory=inv(c);c.m.references(inventory)
    row=c.m.plan(str(target),'clone',inventory,{},{});assert row['action']=='remove',row
    shell('git','remote','set-url','origin','https://git.example.invalid/foreign/evidence.git',cwd=c.repo)
    with pytest.raises(c.m.ws.Keep,match='嵌套于'):
        c.m.remove(row,inventory,{},{} )
    assert target.exists()


def test_nested_same_origin_matches_any_effective_parent_remote(case,monkeypatch):
    c=case;target=c.repo/'.worktrees/copy'
    shell('git','clone','-q',str(c.bare),str(target))
    shell('git','remote','add','second','ssh://git@git.example.invalid/other/shared.git',cwd=c.repo)
    shell('git','remote','set-url','origin','short:other/shared.git',cwd=target)
    shell('git','config','url.https://git.example.invalid/.insteadOf','short:',cwd=target);age(c.repo)
    # The local-state boundary performs no remote IO; only recovery proof is mocked.
    raw=c.m._run
    def transport(args,**kw):
        return raw([str(c.bare) if a in ('short:other/shared.git','https://git.example.invalid/other/shared.git') else a for a in args],**kw)
    monkeypatch.setattr(c.m,'_run',transport)
    row=plan(c,target)
    assert row['action']=='remove',row


def test_same_origin_inner_clone_does_not_bypass_foreign_outer_owner(case):
    c=case;middle=c.repo/'.local/dmca/upstream';inner=middle/'.worktrees/copy'
    shell('git','clone','-q',str(c.bare),str(middle));shell('git','clone','-q',str(c.bare),str(inner));age(c.repo)
    row=plan(c,inner)
    assert row['action']=='keep' and row['reason']==f'嵌套于 {c.repo}，由该项目自行保留'


def test_multiple_remotes_cannot_bridge_foreign_nested_snapshot(case):
    c=case;middle=c.repo/'.local/dmca/upstream';inner=middle/'.worktrees/copy'
    shell('git','clone','-q',str(c.bare),str(middle));shell('git','clone','-q',str(c.bare),str(inner))
    shell('git','remote','add','outer','https://git.example.invalid/group/repo.git',cwd=inner);age(c.repo)
    row=plan(c,inner)
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
    monkeypatch.setattr(c.m,'usage',lambda _, refs=None, **kw: ({},[]))
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



def shared_borrower(c, source=None, name='borrower'):
    target=c.root/name
    shell('git','clone','--shared','-q',str(source or c.repo),str(target))
    shell('git','remote','set-url','origin','https://git.example.invalid/group/repo.git',cwd=target)
    for key,value in [('user.name','Test'),('user.email','test@example.com')]:
        shell('git','config',key,value,cwd=target)
    return target


def test_shared_borrower_can_be_deleted_without_harming_its_lender(case):
    c=case;borrower=shared_borrower(c);age(c.root)
    inventory=inv(c);refs=c.m.references(inventory)
    assert 'alternates reference' in c.m.ws.anchored(refs,str(c.repo))
    assert 'alternates reference' in plan(c,refs=refs)['reason']
    row=plan(c,borrower,refs=refs)
    assert row['action']=='remove',row
    head=row['objects']['HEAD'];c.m.remove(row,inventory,{},refs)
    assert not borrower.exists() and c.repo.exists()
    assert shell('git','rev-parse','HEAD',cwd=c.repo)==head
    shell('git','fsck','--full',cwd=c.repo)


def test_shared_borrower_unpublished_history_still_requires_verified_archive(case,monkeypatch):
    c=case;borrower=shared_borrower(c)
    (borrower/'a').write_text('borrower-only history')
    shell('git','commit','-qam','local borrower work',cwd=borrower);age(c.root)
    inventory=inv(c);refs=c.m.references(inventory)
    row=plan(c,borrower,refs=refs)
    assert row['action']=='archive+remove' and row['archive_objects'],row
    row['bytes']=1024
    monkeypatch.setattr(c.m,'usage',lambda _, refs=None, **kw:({},[]))
    monkeypatch.setattr(c.m,'process_anchors',lambda anchors:{})
    monkeypatch.setattr(c.m,'bb_environment_inventory',lambda:[])
    c.m.execute([row],inventory,{},refs,[str(c.root)],set(),'borrower-test',c.root/'execute.jsonl')
    assert row['result']=='removed' and c.repo.exists()
    assert row['archives']
    for archive in row['archives']:
        assert shell('git','rev-parse',archive['ref'],cwd=c.bare)==archive['sha']
    shell('git','fsck','--full',cwd=c.repo)


def test_a_borrower_that_also_lends_objects_is_still_protected(case):
    c=case;middle=shared_borrower(c);leaf=shared_borrower(c,middle,'leaf');age(c.root)
    refs=c.m.references(inv(c))
    assert 'alternates reference' in plan(c,refs=refs)['reason']
    assert 'alternates reference' in plan(c,middle,refs=refs)['reason']
    assert plan(c,leaf,refs=refs)['action']=='remove'


@pytest.mark.parametrize('kind',['alternates','http-alternates'])
def test_recent_borrowing_configuration_is_recent_git_activity(case,kind):
    c=case;borrower=shared_borrower(c);age(c.root)
    (borrower/'.git/objects/info'/kind).write_text(str(c.repo/'.git/objects')+'\n')
    assert '168h' in plan(c,borrower)['reason']


@pytest.mark.parametrize('interpreter',['python3','bash'])
def test_bb_uses_the_declared_language_and_reports_automation_ownership(case,monkeypatch,interpreter):
    c=case;script=c.root/'watcher';script.write_text('for row in rows:\n    print(row)\n')
    task={'id':'a','name':'data-only watcher','enabled':False,'lastRunAt':123456,'lastRunStatus':'skipped',
          'execution':{'interpreter':interpreter,'storedScriptPath':str(script)}}
    def command(cmd,**kw):
        if cmd[1]=='project':data=[{'id':'p','sources':[{'path':str(c.repo)}]}]
        elif cmd[1]=='environment':data=[]
        else:data=[task] if cmd[cmd.index('--project')+1]=='p' else []
        return SimpleNamespace(stdout=json.dumps(data))
    monkeypatch.setattr(c.m,'command',command);monkeypatch.setattr(c.m.ws,'bb_anchors',lambda *a:None)
    records=c.m.bb_anchors({})
    if interpreter=='python3':assert records==[]
    else:
        assert records and records[0]['automation_name']=='data-only watcher'
        assert records[0]['automation_last_run_at']==123456 and records[0]['automation_enabled'] is False


def test_python_data_loops_are_not_unknown_filesystem_paths(case):
    c=case;anchors={}
    text="""import json
from pathlib import Path
ROOT=Path('/data/ops/watch-only')
rows=json.loads((ROOT/'data.json').read_text())
for row in rows:
    print(row['value'])
"""
    assert c.m.python_anchors(anchors,text,'/root','python')==[]
    assert not c.m.ws.anchored(anchors,str(c.repo))
    assert c.m.ws.anchored(anchors,'/data/ops/watch-only/data.json')


def test_python_glob_loop_retains_its_absolute_prefix(case):
    c=case;root=str(c.root/'snapshots');target=root+'/new-clone'
    text=f"from pathlib import Path\nroot=Path({root!r})\nfor p in sorted(root.glob('*/config.json')):\n    p.read_text()\n"
    anchors={};records=c.m.python_anchors(anchors,text,'/root','python')
    scoped=c.m.scope_unknown(anchors,records,{'trees':{target:'clone',str(c.repo):'clone'}})
    assert scoped and all(x['scope']=='prefix' for x in scoped)
    assert c.m.ws.anchored(anchors,target) and not c.m.ws.anchored(anchors,str(c.repo))


@pytest.mark.parametrize('operand',["Path(os.environ['UNOBSERVED_ROOT'])",'Path(sys.argv[1])','open(dynamic_path)', 'unknown_path.read_text()'])
def test_python_unbounded_file_operand_keeps_project_scope(case,operand):
    c=case;anchors={};records=c.m.python_anchors(anchors,'import os,sys\nfrom pathlib import Path\n'+operand,'/root','python')
    for record in records:record.update(project_id='p',project_paths=[str(c.repo)])
    scoped=c.m.scope_unknown(anchors,records,{'trees':{str(c.repo):'clone'}})
    assert any(x['scope']=='project' and str(c.repo) in x['protected_paths'] for x in scoped)


@pytest.mark.parametrize('style',['env','fstring','join','tuple','home'])
def test_python_literal_and_observed_environment_paths_are_reserved(case,style):
    c=case;root=str(c.repo)
    text={'env':"Path(os.environ['CHECKOUT_ROOT']).read_text()",
          'fstring':f"Path(f'{root}/config-{{unknown}}.json').read_text()",
          'join':f"Path(os.path.join({root!r},'config.json')).read_text()",
          'tuple':f"first,second=Path('/other/place'),Path({root!r})\nsecond.read_text()",
          'home':"Path.home().joinpath('config.json').read_text()"}[style]
    if style=='home':text="(Path.home()/'config.json').read_text()"
    anchors={};records=c.m.python_anchors(anchors,'import os\nfrom pathlib import Path\n'+text,root,'python',metadata={'CHECKOUT_ROOT':[root]})
    c.m.scope_unknown(anchors,records,{'trees':{root:'clone'}})
    assert c.m.ws.anchored(anchors,root)


@pytest.mark.parametrize('source',["Path('/data/ops/' + unknown).read_text()","(Path('/data/ops') / ('file-' + unknown)).read_text()"])
def test_python_dynamic_path_has_a_prefix_instead_of_project_fallback(case,source):
    c=case;records=c.m.python_anchors({},'from pathlib import Path\n'+source,'/root','python')
    assert records and all(x['prefixes'] for x in records)


@pytest.mark.parametrize('command',['exec(code)',"subprocess.run(['python3','-c',code])","subprocess.run(['python3','-'],input=code)"])
def test_python_embedded_local_code_keeps_its_file_references(case,command):
    c=case;code=f"from pathlib import Path; Path({str(c.repo)!r}).read_text()"
    text='import subprocess\ncode='+repr(code)+'\n'+command
    anchors={};c.m.python_anchors(anchors,text,'/root','python')
    assert c.m.ws.anchored(anchors,str(c.repo))


@pytest.mark.parametrize('command',['exec(code)',"subprocess.run(['python3','-c',code])"])
def test_python_unknown_local_code_stays_unknown(case,command):
    records=case.m.python_anchors({},'import subprocess\n'+command,'/root','python')
    assert any(not x['prefixes'] for x in records)


def test_python_syntax_failure_stays_unknown_and_never_executes(case):
    c=case;sentinel=c.root/'must-not-exist'
    text=f"from pathlib import Path\nPath({str(sentinel)!r}).touch()\nfor ???"
    records=c.m.python_anchors({},text,'/root','python')
    assert records and not sentinel.exists()


def test_python_possible_value_limit_does_not_use_a_partial_prefix(case):
    c=case;records=c.m.python_anchors({},"import os\nfrom pathlib import Path\nPath(os.environ['ROOT_DIR']).read_text()",'/root','python',{'ROOT_DIR':{f'/volume-{i}' for i in range(300)}})
    assert any(not x['prefixes'] for x in records)


@pytest.mark.parametrize('source',["[p.read_text() for p in dynamic_paths]", "lambda p: p.read_text()", "def f(p=open(dynamic_path)):\n    return p", "Path('/known').rename(dynamic_path)"])
def test_python_nested_bindings_and_file_destinations_remain_unknown(case,source):
    c=case;text="from pathlib import Path\np=Path('/unrelated/static-root')\n"+source
    records=c.m.python_anchors({},text,'/root','python')
    assert any(not x['prefixes'] for x in records)



def test_python_unknown_join_operand_cannot_discard_the_base_silently(case):
    records=case.m.python_anchors({},"from pathlib import Path\n(Path('/data/ops') / unknown).read_text()",'/root','python')
    assert any(x['prefixes'] for x in records) and any(not x['prefixes'] for x in records)



def test_python_atomic_write_suffix_preserves_known_parent(case):
    c=case;text=f"from pathlib import Path\nroot=Path({str(c.repo)!r})\np=root/'state.json'\ntemporary=p.with_suffix('.new')\ntemporary.write_text('data')\ntemporary.replace(p)\n"
    anchors={};assert c.m.python_anchors(anchors,text,'/root','python')==[]
    assert c.m.ws.anchored(anchors,str(c.repo))


def test_python_datetime_replace_is_not_a_file_operation(case):
    records=case.m.python_anchors({},"import datetime\nend=datetime.datetime.fromisoformat(cfg['end']).replace(tzinfo=datetime.timezone.utc)",'/root','python')
    assert records==[]


def test_python_cwd_mutation_never_reuses_launch_cwd(case):
    text="import os\nfrom pathlib import Path\nos.chdir(dynamic_path)\n(Path.cwd()/'state').read_text()"
    records=case.m.python_anchors({},text,'/root','python',metadata={'PWD':['/unrelated/launch-cwd']})
    assert any(not x['prefixes'] for x in records)


def test_python_environment_assignment_is_a_possible_path_value(case):
    c=case;text=f"import os\nfrom pathlib import Path\nos.environ['CHECKOUT_ROOT']={str(c.repo)!r}\nPath(os.environ['CHECKOUT_ROOT']).read_text()"
    anchors={};case.m.python_anchors(anchors,text,'/root','python',{'CHECKOUT_ROOT':{'/unrelated/root'}})
    assert c.m.ws.anchored(anchors,str(c.repo))


def test_python_keyword_file_replacement_keeps_unknown_destination(case):
    records=case.m.python_anchors({},"from pathlib import Path\nPath('/known').replace(target=dynamic_path)",'/root','python')
    assert any(not x['prefixes'] for x in records)


def test_bb_python_cwd_uses_automation_storage_policy(case,monkeypatch):
    c=case;storage=c.root/'automation-storage';script=storage/'a'/'watch.py';script.parent.mkdir(parents=True)
    script.write_text("from pathlib import Path\n(Path.cwd()/'state.json').read_text()")
    task={'id':'a','execution':{'interpreter':'python3','workingDirectory':{'type':'automation-storage'},'storedScriptPath':str(script)}}
    def command(cmd,**kw):
        if cmd[1]=='project':data=[{'id':'p','sources':[{'path':str(c.repo)}]}]
        elif cmd[1]=='environment':data=[]
        else:data=[task] if cmd[cmd.index('--project')+1]=='p' else []
        return SimpleNamespace(stdout=json.dumps(data))
    monkeypatch.setattr(c.m,'command',command);monkeypatch.setattr(c.m.ws,'bb_anchors',lambda *a:None)
    anchors={};assert c.m.bb_anchors(anchors)==[]
    assert c.m.ws.anchored(anchors,str(storage/'state.json'))



@pytest.mark.parametrize('loop',["for i in [1,2]",'while active'])
def test_python_loop_cannot_reuse_only_the_first_iteration_path(case,loop):
    text=f"from pathlib import Path\np=Path('/first/path')\n{loop}:\n    p.glob('*')\n    p=unknown_path\n"
    records=case.m.python_anchors({},text,'/root','python')
    assert any(not x['prefixes'] for x in records)


def test_python_function_mutated_global_path_remains_unknown(case):
    text="from pathlib import Path\np=Path('/first/path')\ndef change():\n    global p\n    p=dynamic_path\nchange()\np.glob('*')\n"
    records=case.m.python_anchors({},text,'/root','python')
    assert any(not x['prefixes'] for x in records)



def test_python_known_helper_arguments_narrow_to_its_path_prefix(case):
    text="from pathlib import Path\ndef write(n,v):\n    (ROOT/n).write_text(v)\nROOT=Path('/data/ops/only-this-directory')\nwrite('state.json','value')"
    anchors={};records=case.m.python_anchors(anchors,text,'/root','python')
    assert records==[]
    assert case.m.ws.anchored(anchors,'/data/ops/only-this-directory/state.json')


def test_python_datetime_filename_is_bounded_without_evaluating_time(case):
    text="import datetime\nfrom pathlib import Path\nstamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')\nD=Path('/data/ops/samples')/stamp\n(D/'result.json').write_text('x')"
    records=case.m.python_anchors({},text,'/root','python')
    assert records and all(x['prefixes'] for x in records)


def test_python_executor_map_binds_literal_callback_inputs(case):
    text="import concurrent.futures\nfrom pathlib import Path\nROOT=Path('/data/ops/collect')\ndef collect(kind):\n    (ROOT/(kind+'.json')).read_text()\nwith concurrent.futures.ThreadPoolExecutor() as ex:\n    results=list(ex.map(collect,['db','images']))"
    anchors={};records=case.m.python_anchors(anchors,text,'/root','python')
    assert records==[]
    assert case.m.ws.anchored(anchors,'/data/ops/collect/db.json')
    assert case.m.ws.anchored(anchors,'/data/ops/collect/images.json')


def test_python_escaped_callback_keeps_unbounded_path_inputs(case):
    text="from pathlib import Path\ndef read(p):\n    Path(p).read_text()\nread('/known')\nregister_callback(read)"
    records=case.m.python_anchors({},text,'/root','python')
    assert any(not x['prefixes'] for x in records)


def test_python_callee_uses_lexical_globals_not_caller_locals(case):
    text="from pathlib import Path\nROOT=Path('/lexical')\ndef read():\n    ROOT.glob('*')\ndef caller():\n    ROOT=Path('/caller')\n    read()\ncaller()"
    records=case.m.python_anchors({},text,'/root','python')
    assert any(any(x['path'].startswith('/lexical') for x in r['prefixes']) for r in records)


def test_python_branch_definition_sees_later_module_bindings(case):
    text="from pathlib import Path\nROOT=Path('/first')\nif condition:\n    def read():\n        ROOT.glob('*')\nROOT=Path('/later')\nread()"
    records=case.m.python_anchors({},text,'/root','python')
    assert any(any(x['path'].startswith('/later') for x in r['prefixes']) for r in records)



@pytest.mark.parametrize('setup',["def Path(value):\n    return custom_factory(value)", 'from custom_library import Path'])
def test_python_unknown_path_factory_is_not_assumed_to_be_pathlib(case,setup):
    records=case.m.python_anchors({},setup+"\nPath('/unrelated/root').read_text()",'/root','python')
    assert any(not x['prefixes'] for x in records)



@pytest.mark.parametrize('text',["from custom_library import os\nfrom pathlib import Path\nPath(os.getenv('ROOT')).read_text()","from pathlib import Path\ndef sorted(items):\n    return dynamic_paths\nfor p in sorted([Path('/known')]):\n    p.read_text()"])
def test_python_foreign_helpers_do_not_invent_known_path_values(case,text):
    records=case.m.python_anchors({},text,'/root','python',{'ROOT':{'/unrelated/root'}})
    assert any(not x['prefixes'] for x in records)



@pytest.mark.parametrize('change',["roots[0]=dynamic_path", "roots.append(dynamic_path)", "alias=roots; alias.append(dynamic_path)", "roots += [dynamic_path]", "del roots[0]"])
def test_python_container_mutations_invalidate_old_finite_paths(case,change):
    text="from pathlib import Path\nroots=['/unrelated/static']\n"+change+"\nfor p in roots:\n    Path(p).read_text()"
    records=case.m.python_anchors({},text,'/root','python')
    assert any(not x['prefixes'] for x in records)


@pytest.mark.parametrize('loop',["for i in [1,2]",'while running'])
def test_python_loop_container_mutation_cannot_keep_first_iteration_values(case,loop):
    text=f"from pathlib import Path\nroots=['/unrelated/static']\n{loop}:\n    for p in roots:\n        Path(p).read_text()\n    roots.append(dynamic_path)"
    records=case.m.python_anchors({},text,'/root','python')
    assert any(not x['prefixes'] for x in records)


def test_python_container_alias_mutation_in_a_helper_remains_unknown(case):
    text="from pathlib import Path\nroots=['/unrelated/static']\nalias=roots\ndef change():\n    alias.append(dynamic_path)\nchange()\nfor p in roots:\n    Path(p).read_text()"
    records=case.m.python_anchors({},text,'/root','python')
    assert any(not x['prefixes'] for x in records)


@pytest.mark.parametrize('call',["open(file=dynamic_path).read()", "shutil.copy('/known/source', dst=dynamic_path)", "os.chdir(path=dynamic_path)", "open(**options)", "shutil.copy('/known', **options)", "open(**{'file':dynamic_path})", "from_io(file=dynamic_path)"])
def test_python_keyword_file_operands_never_disappear(case,call):
    text="import os,shutil\nfrom io import open as from_io\n"+call
    records=case.m.python_anchors({},text,'/root','python',metadata={'PWD':['/known/cwd']})
    assert any(not x['prefixes'] for x in records)


@pytest.mark.parametrize('call',["Path('state.new').replace(dynamic_path)", "p=Path('state.new'); p.replace(target=dynamic_path)", "Path('state.new').replace(**options)", "Path('state.new').replace(**{'target':dynamic_path})"])
def test_python_relative_replacement_checks_destination(case,call):
    records=case.m.python_anchors({},'from pathlib import Path\n'+call,'/root','python',metadata={'PWD':['/known/cwd']})
    assert any(not x['prefixes'] for x in records)


def test_python_known_relative_replacement_resolves_source_and_target(case):
    anchors={};text="from pathlib import Path\nPath('state.new').replace(**{'target':'state.json'})"
    records=case.m.python_anchors(anchors,text,'/root','python',metadata={'PWD':['/known/cwd']})
    assert records==[] and case.m.ws.anchored(anchors,'/known/cwd/state.new') and case.m.ws.anchored(anchors,'/known/cwd/state.json')


def test_python_dynamic_chdir_itself_reserves_project_scope(case):
    records=case.m.python_anchors({},'import os\nos.chdir(dynamic_path)','/root','python')
    for record in records:record.update(project_id='p',project_paths=[str(case.repo)])
    scoped=case.m.scope_unknown({},records,{'trees':{str(case.repo):'clone'}})
    assert any(x['scope']=='project' and str(case.repo) in x['protected_paths'] for x in scoped)



@pytest.mark.parametrize('argv',["['bb','thread','spawn','--environment',state['environmentPath']]", "['git','-C',dynamic_path,'status']", "['tool','--config='+dynamic_path]", "['bb',*dynamic_args]"])
def test_python_cli_path_parameters_remain_unknown(case,argv):
    records=case.m.python_anchors({},'import subprocess\nsubprocess.run('+argv+')','/root','python')
    assert any(not x['prefixes'] for x in records)


def test_python_variadic_cli_wrapper_cannot_hide_environment_path(case):
    text="import subprocess\ndef call(*args):\n    subprocess.check_output(['bb',*args,'--json'])\ncall('thread','spawn','--environment',state['environmentPath'])"
    records=case.m.python_anchors({},text,'/root','python')
    assert any(not x['prefixes'] for x in records)


def test_python_cli_relative_path_uses_the_subprocess_cwd(case):
    text="import subprocess\nsubprocess.run(['git','-C','child','status'],cwd='/actual/cwd')"
    anchors={};records=case.m.python_anchors(anchors,text,'/root','python',metadata={'PWD':['/launch/cwd']})
    assert records==[]
    assert case.m.ws.anchored(anchors,'/actual/cwd/child') and not case.m.ws.anchored(anchors,'/launch/cwd/child')



@pytest.mark.parametrize('source',["from subprocess import run as r\nr(['git','-C',dynamic_path,'status'])", "import subprocess as p\np.run(args=['bb','thread','spawn','--environment',dynamic_path])", "import subprocess\nsubprocess.run(**options)", "import subprocess\nsubprocess.run(**{'args':['git','-C',dynamic_path]})"])
def test_python_cli_aliases_and_keyword_argv_keep_unknown_paths(case,source):
    records=case.m.python_anchors({},source,'/root','python')
    assert any(not x['prefixes'] for x in records)



@pytest.mark.parametrize('change',["alias=roots; alias += [dynamic_path]", "alias=roots\nif condition:\n    alias += [dynamic_path]", "def change(r):\n    r += [dynamic_path]\nchange(roots)", "add=roots.append; add(dynamic_path)", "register(roots.append)"])
def test_python_augmented_or_escaped_container_aliases_stay_unknown(case,change):
    text="from pathlib import Path\nroots=['/unrelated/static']\n"+change+"\nfor p in roots:\n    Path(p).read_text()"
    records=case.m.python_anchors({},text,'/root','python')
    assert any(not x['prefixes'] for x in records)


def test_python_escaped_callback_order_cannot_preserve_finite_paths(case):
    text="from pathlib import Path\nroots=['/unrelated/static']\ndef read_roots():\n    for p in roots: Path(p).read_text()\ndef change():\n    roots.append(dynamic_path)\nregister(change)\nregister(read_roots)"
    records=case.m.python_anchors({},text,'/root','python')
    assert any(not x['prefixes'] for x in records)


@pytest.mark.parametrize('call',["os.stat('state', **{'dir_fd': fd})", "os.rename('state', dst='state2', **{'src_dir_fd': fd})", "os.replace('state','state2', **{'dst_dir_fd':fd})"])
@pytest.mark.parametrize('dynamic',[True,False])
def test_python_literal_kwargs_fd_context_is_unknown_unless_none(case,call,dynamic):
    text='import os\n'+(call if dynamic else call.replace(': fd', ': None').replace(':fd', ':None'))
    records=case.m.python_anchors({},text,'/root','python',metadata={'PWD':['/known/cwd']})
    assert bool(records) is dynamic
    if dynamic:assert any(not x['prefixes'] for x in records)


@pytest.mark.parametrize('cwd,expected',[('worktree','/known/cwd/worktree/child'),('.','/known/cwd/child'),('..','/known/child'),('~/worktree','/known/cwd/~/worktree/child')])
def test_python_subprocess_relative_cwd_keeps_its_child_path(case,cwd,expected):
    text=f"import subprocess\nsubprocess.run(['git','-C','child','status'],cwd={cwd!r})"
    anchors={};records=case.m.python_anchors(anchors,text,'/root','python',metadata={'PWD':['/known/cwd']})
    assert case.m.ws.anchored(anchors,expected),records


def test_python_subprocess_relative_cwd_without_launch_context_is_unknown(case,monkeypatch):
    monkeypatch.delenv('PWD',raising=False)
    records=case.m.python_anchors({},"import subprocess\nsubprocess.run(['git','-C','child'],cwd='worktree')",'/root','python')
    assert any(not x['prefixes'] for x in records)


@pytest.mark.parametrize('style',['positional','keyword'])
def test_python_noninline_argv_keeps_relative_environment_path(case,style):
    invocation='subprocess.run(argv)' if style=='positional' else "subprocess.run(args=argv,cwd='/actual/cwd')"
    text="import subprocess\nargv=['bb','thread','spawn','--environment','env-path']\n"+invocation
    anchors={};records=case.m.python_anchors(anchors,text,'/root','python',metadata={'PWD':['/launch/cwd']})
    expected=('/launch/cwd' if style=='positional' else '/actual/cwd')+'/env-path'
    assert case.m.ws.anchored(anchors,expected) or any(not x['prefixes'] for x in records)


def test_python_changed_home_is_not_replaced_with_launch_home(case):
    text="import os\nfrom pathlib import Path\nos.environ['HOME']='/new/home'\n(Path.home()/'child').read_text()"
    anchors={};case.m.python_anchors(anchors,text,'/old/home','python')
    assert case.m.ws.anchored(anchors,'/new/home/child')


def test_python_tilde_is_literal_until_expanduser_is_called(case):
    text="from pathlib import Path\nPath('~/child').read_text()\nPath('~').expanduser().glob('other')"
    anchors={};case.m.python_anchors(anchors,text,'/actual/home','python',metadata={'PWD':['/launch']})
    assert case.m.ws.anchored(anchors,'/launch/~/child')
    assert case.m.ws.anchored(anchors,'/actual/home/other')



def test_python_shell_child_uses_script_modified_home(case):
    text="import os,subprocess\nos.environ['HOME']='/new/home'\nsubprocess.run('cd \"$HOME/child\"',shell=True)"
    anchors={};case.m.python_anchors(anchors,text,'/launch/home','python')
    assert case.m.ws.anchored(anchors,'/new/home/child')


def test_python_subprocess_custom_environment_keeps_known_home_prefix(case):
    text="import subprocess\nsubprocess.run(['python3','-c',\"from pathlib import Path; (Path.home()/'child').read_text()\"],env={'HOME':'/new/home','TOKEN':'not-a-path'})"
    records=case.m.python_anchors({},text,'/old/home','python')
    assert any(any(x['path']=='/new/home' for x in r['prefixes']) for r in records)
    assert any(not r['prefixes'] for r in records)


@pytest.mark.parametrize('location,protected', [('inside',True),('root',True),('ancestor',False)])
def test_cwd_only_protects_worktrees_containing_the_directory(case,tmp_path,monkeypatch,location,protected):
    c=case;proc=tmp_path/'proc';p=proc/'123456';(p/'fd').mkdir(parents=True);(p/'cmdline').write_bytes(b'')
    directory={'inside':c.repo/'src','root':c.repo,'ancestor':c.repo.parent}[location]
    directory.mkdir(exist_ok=True)
    (p/'cwd').symlink_to(directory);(p/'stat').write_text('123456 (worker) '+' '.join(['1']*25))
    (p/'status').write_text('Uid: 0 0 0 0\n');(p/'maps').write_text('');(p/'environ').write_text('')
    monkeypatch.setattr(c.m.pwd,'getpwuid',lambda _:SimpleNamespace(pw_dir='/root'))
    anchors={};env=c.m.process_anchors(anchors,str(proc));child=str(c.repo/'.worktrees/copy')
    c.m.scope_directories(anchors,env.scopes,{'trees':{str(c.repo):'clone',child:'clone'}})
    assert bool(c.m.ws.anchored(anchors,str(c.repo))) is protected
    assert not c.m.ws.anchored(anchors,child)


def same_name_pair(c):
    backup=c.root/'backup';shell('git','clone','-q',str(c.bare),str(backup))
    shell('git','remote','set-url','origin','https://git.example.invalid/group/repo.git',cwd=backup)
    age(c.root);inventory=inv(c);refs=c.m.references(inventory)
    config=c.root/'name.conf';config.write_text('repository=group/repo\n')
    return backup,inventory,refs,config


@pytest.mark.parametrize('reason',['BB project source: p','process 12 cwd','BB environment: e'])
def test_name_reference_is_satisfied_by_an_independently_protected_copy(case,reason):
    c=case;backup,inventory,refs,config=same_name_pair(c);anchors={str(c.repo):reason}
    c.m.configuration_name_anchors(anchors,inventory,[str(config)],refs)
    assert c.m.plan(str(backup),'clone',inventory,anchors,refs)['action']=='remove'
    assert c.m.plan(str(c.repo),'clone',inventory,anchors,refs)['action']=='keep'
    assert any(x['path']==str(backup) and x['satisfied_by']==str(c.repo) for x in inventory['configuration_name_references'])


def test_name_without_other_protection_keeps_every_ambiguous_copy(case):
    c=case;backup,inventory,refs,config=same_name_pair(c);anchors={}
    c.m.configuration_name_anchors(anchors,inventory,[str(config)],refs)
    for path in (c.repo,backup):
        row=c.m.plan(str(path),'clone',inventory,anchors,refs)
        assert row['action']=='keep' and 'configuration name collision' in row['reason']


def test_name_rule_cannot_use_another_name_rule_as_its_evidence(case):
    c=case;backup,inventory,refs,config=same_name_pair(c)
    anchors={str(c.repo):'configuration name reference: old-name'}
    c.m.configuration_name_anchors(anchors,inventory,[str(config)],refs)
    assert c.m.plan(str(backup),'clone',inventory,anchors,refs)['action']=='keep'


def test_other_local_retention_can_satisfy_a_name_without_an_anchor(case):
    c=case;backup,inventory,refs,config=same_name_pair(c)
    (c.repo/'uncommitted').write_text('keep');age(c.repo);anchors={}
    c.m.configuration_name_anchors(anchors,inventory,[str(config)],refs)
    assert c.m.plan(str(backup),'clone',inventory,anchors,refs)['action']=='remove'
    assert c.m.plan(str(c.repo),'clone',inventory,anchors,refs)['action']=='keep'


def test_unrelated_remote_with_the_same_namespace_does_not_satisfy_a_name(case):
    c=case;backup,inventory,refs,config=same_name_pair(c)
    inventory['remote_urls'][str(backup)]=['other.example/group/repo']
    anchors={str(c.repo):'BB project source: p'}
    c.m.configuration_name_anchors(anchors,inventory,[str(config)],refs)
    assert c.m.plan(str(backup),'clone',inventory,anchors,refs)['action']=='keep'


def test_registered_environment_remotes_do_not_expand_a_non_git_container_project(case):
    c=case;other=c.root/'other-project';env=c.root/'envs/registered';source=c.root/'container';source.mkdir()
    trees={str(c.repo):'clone',str(other):'clone',str(env):'linked'}
    inventory={'trees':trees,'remote_urls':{p:['host/mono2'] for p in trees}}
    record=c.m.unresolved('automation','unknown path')
    record.update(project_id='code',project_paths=[str(source),str(env)],project_source_paths=[str(source)],project_environment_paths=[str(env)],project_remote_ids=['host/mono2'])
    anchors={};c.m.scope_unknown(anchors,[record],inventory)
    assert record['scope']=='project' and record['protected_paths']==[str(env)]
    assert c.m.ws.anchored(anchors,str(source)) and not c.m.ws.anchored(anchors,str(other))


def test_real_git_project_fallback_still_protects_source_copies_and_own_environments(case):
    c=case;copy=str(c.root/'same-remote');env=str(c.root/'env');unrelated=str(c.root/'different-project')
    inventory={'trees':{str(c.repo):'clone',copy:'clone',env:'linked',unrelated:'clone'},
               'remote_urls':{str(c.repo):['host/project'],copy:['host/project'],env:['host/other'],unrelated:['host/other']}}
    record=c.m.unresolved('automation','unknown')
    record.update(project_id='p',project_paths=[str(c.repo),env],project_source_paths=[str(c.repo)],project_environment_paths=[env],project_remote_ids=[])
    c.m.scope_unknown({},[record],inventory)
    assert set(record['protected_paths'])=={str(c.repo),copy,env}


def test_unowned_json_fallback_remains_global(case):
    record=case.m.unresolved('automation','unknown JSON path');record['project_id']='unknown'
    case.m.scope_unknown({},[record],{'trees':{str(case.repo):'clone'}})
    assert record['scope']=='global'


@pytest.mark.parametrize('style',['read_text','read_bytes','load_open','alias'])
def test_python_reads_current_literal_json_path_fields_without_running_the_script(case,style):
    c=case;state=c.root/'state.json';target=str(c.root/'checkout');state.write_text(json.dumps({'environmentPath':target}))
    loaders={'read_text':f'json.loads(Path({str(state)!r}).read_text())',
             'read_bytes':f'json.loads(Path({str(state)!r}).read_bytes())',
             'load_open':f'json.load(open({str(state)!r}))',
             'alias':f'decode(Path({str(state)!r}).read_text())'}
    text=f"import json,subprocess\nfrom pathlib import Path\nfrom json import loads as decode\nstate={loaders[style]}\nraise RuntimeError('never execute')\nsubprocess.run(['bb','thread','spawn','--environment',state['environmentPath']])"
    anchors={};records=c.m.python_anchors(anchors,text,'/root','python')
    assert records==[] and c.m.ws.anchored(anchors,target)


def test_python_known_json_field_flows_through_variadic_bb_wrapper(case):
    c=case;state=c.root/'state.json';target=str(c.root/'checkout');state.write_text(json.dumps({'environmentPath':target}))
    text=f"import json,subprocess\nfrom pathlib import Path\nstate=json.loads(Path({str(state)!r}).read_text())\ndef call(*args):\n subprocess.check_output(['bb',*args,'--json'])\ncall('thread','spawn','--environment',state['environmentPath'],'--prompt-file',str(Path('/fixed/prompt.txt')))\nstate.update(status='done',threadId='id')"
    anchors={};records=c.m.python_anchors(anchors,text,'/root','python')
    assert records==[] and c.m.ws.anchored(anchors,target) and c.m.ws.anchored(anchors,'/fixed/prompt.txt')


def test_python_json_fields_are_reloaded_on_each_protection_generation(case):
    c=case;state=c.root/'state.json';one=str(c.root/'one');two=str(c.root/'two')
    text=f"import json\nfrom pathlib import Path\nstate=json.loads(Path({str(state)!r}).read_text())\nPath(state['path']).read_text()"
    for target,absent in [(one,two),(two,one)]:
        state.write_text(json.dumps({'path':target}));anchors={}
        assert c.m.python_anchors(anchors,text,'/root','python')==[]
        assert c.m.ws.anchored(anchors,target) and not c.m.ws.anchored(anchors,absent)


@pytest.mark.parametrize('failure',['missing_file','missing_key','malformed','nonstring','large'])
def test_missing_or_unreadable_json_path_fields_remain_unknown(case,failure):
    c=case;state=c.root/'state.json'
    if failure=='missing_key':state.write_text('{}')
    elif failure=='malformed':state.write_text('{')
    elif failure=='nonstring':state.write_text('{"path":42}')
    elif failure=='large':state.write_text(' '*c.m.CONFIG_TEXT_LIMIT+'{}')
    text=f"import json\nfrom pathlib import Path\nstate=json.loads(Path({str(state)!r}).read_text())\nPath(state['path']).read_text()"
    assert c.m.python_anchors({},text,'/root','python')


@pytest.mark.parametrize('change',[
    "state['path']=dynamic", "alias=state\nalias.update(path=dynamic)", 'unknown_function(state)',
    'def change(s):\n unknown_function(s)\nchange(state)', 'method=state.update\nregister(method)',
    "import json as j\nj.loads=foreign_decoder", "external.configure(state)",
])
def test_json_paths_do_not_reuse_values_after_unknown_mutation_or_escape(case,change):
    c=case;state=c.root/'state.json';target=str(c.root/'old');state.write_text(json.dumps({'path':target}))
    text=f"import json\nfrom pathlib import Path\nstate=json.loads(Path({str(state)!r}).read_text())\n{change}\nPath(state['path']).read_text()"
    anchors={};records=c.m.python_anchors(anchors,text,'/root','python')
    assert records and not c.m.ws.anchored(anchors,target)


def test_opaque_variadic_arguments_are_still_unknown(case):
    text="import subprocess\ndef call(*args):\n subprocess.check_output(['bb',*args])\ncall(*unknown_arguments)"
    assert case.m.python_anchors({},text,'/root','python')


def test_json_decoder_hooks_are_never_executed_or_assumed_pure(case):
    c=case;state=c.root/'state.json';sentinel=c.root/'should-not-exist';state.write_text('{"path":"/old/value"}')
    text=f"import json\nfrom pathlib import Path\ndef hook(obj):\n Path({str(sentinel)!r}).write_text('executed')\n return obj\nstate=json.loads(Path({str(state)!r}).read_text(),object_hook=hook)\nPath(state['path']).read_text()"
    assert c.m.python_anchors({},text,'/root','python') and not sentinel.exists()


def test_json_fifo_is_unknown_and_does_not_wait_for_a_writer(case):
    import signal
    c=case;state=c.root/'state.json';os.mkfifo(state)
    old=signal.signal(signal.SIGALRM,lambda *_: (_ for _ in ()).throw(TimeoutError('FIFO blocked')))
    signal.alarm(3)
    try:
        text=f"import json\nfrom pathlib import Path\nstate=json.loads(Path({str(state)!r}).read_text())\nPath(state['path']).read_text()"
        assert c.m.python_anchors({},text,'/root','python')
    finally:signal.alarm(0);signal.signal(signal.SIGALRM,old)


def test_file_written_before_json_load_is_not_assumed_to_keep_old_contents(case):
    c=case;state=c.root/'state.json';target=str(c.root/'old');state.write_text(json.dumps({'path':target}))
    text=f"import json\nfrom pathlib import Path\np=Path({str(state)!r})\np.write_text(dynamic_payload)\nstate=json.loads(p.read_text())\nPath(state['path']).read_text()"
    anchors={};assert c.m.python_anchors(anchors,text,'/root','python')
    assert not c.m.ws.anchored(anchors,target)


def test_writing_back_after_read_does_not_change_the_already_loaded_json_object(case):
    c=case;state=c.root/'state.json';target=str(c.root/'kept');state.write_text(json.dumps({'path':target}))
    text=f"import json\nfrom pathlib import Path\np=Path({str(state)!r})\nstate=json.loads(p.read_text())\np.write_text(dynamic_payload)\nPath(state['path']).read_text()"
    anchors={};assert c.m.python_anchors(anchors,text,'/root','python')==[]
    assert c.m.ws.anchored(anchors,target)


def test_json_replaced_during_read_is_unknown(case,monkeypatch):
    c=case;state=c.root/'state.json';replacement=c.root/'new.json'
    state.write_text('{"path":"/old"}');replacement.write_text('{"path":"/new"}')
    inode=state.stat().st_ino;read=c.m.os.read;changed=False
    def racing(fd,count):
        nonlocal changed
        data=read(fd,count)
        if not changed and os.fstat(fd).st_ino==inode:
            changed=True;replacement.replace(state)
        return data
    monkeypatch.setattr(c.m.os,'read',racing)
    text=f"import json\nfrom pathlib import Path\nstate=json.loads(Path({str(state)!r}).read_text())\nPath(state['path']).read_text()"
    assert c.m.python_anchors({},text,'/root','python') and changed


@pytest.mark.parametrize('reader',['json.load(stream)','json.load(fp=stream)','json.loads(stream.read())'])
def test_literal_json_open_context_is_resolved_without_executing_it(case,reader):
    c=case;state=c.root/'state.json';target=str(c.root/'checkout');state.write_text(json.dumps({'path':target}))
    text=f"import json\nfrom pathlib import Path\nwith open({str(state)!r}) as stream:\n state={reader}\nPath(state['path']).read_text()"
    anchors={};assert c.m.python_anchors(anchors,text,'/root','python')==[]
    assert c.m.ws.anchored(anchors,target)


def test_json_open_handle_escaping_an_unknown_helper_does_not_narrow_paths(case):
    c=case;state=c.root/'state.json';state.write_text('{"path":"/old/path"}')
    text=f"import json\nfrom pathlib import Path\nwith open({str(state)!r}) as stream:\n unknown_helper(stream)\n state=json.load(stream)\nPath(state['path']).read_text()"
    assert c.m.python_anchors({},text,'/root','python')


def test_json_open_with_write_mode_is_not_executed(case):
    c=case;state=c.root/'state.json';original='{"path":"/old/path"}';state.write_text(original)
    text=f"import json\nfrom pathlib import Path\nstate=json.load(open({str(state)!r},'w'))\nPath(state['path']).read_text()"
    assert c.m.python_anchors({},text,'/root','python') and state.read_text()==original


@pytest.mark.parametrize('consumer', ['remote','alternates','symlink'])
def test_new_inactive_consumer_is_detected_next_round(case,monkeypatch,consumer):
    # Owner 2026-11-19: global references are a complete per-round snapshot;
    # consumers created later belong to the next round, active readers stay live.
    c=case;inventory=inv(c);refs=c.m.references(inventory);row=c.m.plan(str(c.repo),'clone',inventory,{},refs);row['bytes']=4096
    assert row['action']=='remove'
    target=c.root/'new-consumer'
    if consumer=='symlink':target.symlink_to(c.repo/'a')
    else:
        shell('git','init','-q',str(target))
        if consumer=='remote':shell('git','config','remote.local.url',str(c.repo),cwd=target)
        else:(target/'.git/objects/info/alternates').write_text(str(c.repo/'.git/objects'))
    monkeypatch.setattr(c.m,'process_anchors',lambda anchors:{});monkeypatch.setattr(c.m,'bb_environment_inventory',lambda:[])
    def no_delete(*a,**kw):pass
    no_delete.avoids_symlink_attacks=True
    with monkeypatch.context() as patch:
        patch.setattr(c.m.shutil,'rmtree',no_delete)
        c.m.execute([row],inventory,{},refs,[str(c.root)],set(),'snapshot',c.root/'events')
    assert row.get('result')=='removed' and c.repo.exists(),row  # Only the sink is stubbed.
    next_inventory=inv(c);next_refs=c.m.references(next_inventory);anchors={};c.m.symlink_anchors(anchors,next_inventory)
    assert c.m.plan(str(c.repo),'clone',next_inventory,anchors,next_refs)['action']=='keep'


@pytest.mark.parametrize('escape',[
    'external([state])', "external({'settings':state})", 'external(*[state])',
    "external(**{'settings':state})", 'bucket=[state]\nexternal(bucket)',
    'external.configure((state,))',
    'def forward(a,b):\n external(b)\nforward(*[None,state])',
])
def test_json_container_alias_escape_returns_unknown(case, escape):
    c=case;path=c.root/'state.json';target=str(c.root/'previous-target')
    path.write_text(json.dumps({'path':target}))
    text=f"import json\nfrom pathlib import Path\nstate=json.loads(Path({str(path)!r}).read_text())\n{escape}\nPath(state['path']).read_text()"
    anchors={};assert c.m.python_anchors(anchors,text,'/root','python')
    assert not c.m.ws.anchored(anchors,target)


@pytest.mark.parametrize('write',[
    'fd=os.open(p,os.O_WRONLY|os.O_TRUNC)\nos.write(fd,dynamic_payload)\nos.close(fd)',
    'fd=os.open(p,flags=dynamic_flags)',
    'os.write(dynamic_fd,dynamic_payload)',
    'def write():\n fd=os.open(p,os.O_RDWR)\n os.write(fd,dynamic_payload)\nregister(write)',
])
def test_low_level_writes_invalidate_current_json_proof(case,write):
    c=case;path=c.root/'state.json';target=str(c.root/'previous-target')
    path.write_text(json.dumps({'path':target}));before=path.read_bytes()
    text=f"import os,json\nfrom pathlib import Path\np={str(path)!r}\n{write}\nstate=json.loads(Path(p).read_text())\nPath(state['path']).read_text()"
    anchors={};assert c.m.python_anchors(anchors,text,'/root','python')
    assert not c.m.ws.anchored(anchors,target) and path.read_bytes()==before


def test_readonly_low_level_open_does_not_invalidate_json_content(case):
    c=case;path=c.root/'state.json';target=str(c.root/'target');path.write_text(json.dumps({'path':target}))
    text=f"import os,json\nfrom pathlib import Path\np={str(path)!r}\nfd=os.open(p,os.O_RDONLY)\nos.close(fd)\nstate=json.loads(Path(p).read_text())\nPath(state['path']).read_text()"
    anchors={};assert c.m.python_anchors(anchors,text,'/root','python')==[]
    assert c.m.ws.anchored(anchors,target)


def test_json_open_non_utf8_encoding_does_not_prove_utf8_path(case):
    c=case;path=c.root/'state.json';target=str(c.root/'café')
    path.write_text(json.dumps({'path':target},ensure_ascii=False),encoding='utf-8')
    text=f"import json\nfrom pathlib import Path\nstate=json.load(open({str(path)!r},encoding='latin1'))\nPath(state['path']).read_text()"
    anchors={};assert c.m.python_anchors(anchors,text,'/root','python')
    assert not c.m.ws.anchored(anchors,target)


def linked_row(c, target):
    _, rows=c.m.linked_registrations(str(c.repo))
    return next(row for row in rows if row['path']==str(target))


def linked_empty_context(c):
    return {'sources':[str(c.repo)],'environments':{},'cwds':{}}


def test_registered_linked_uses_24_hours_and_clone_still_uses_7_days(case):
    c=case;target=linked(c)
    for root in (target,Path(shell('git','rev-parse','--absolute-git-dir',cwd=target))):
        for parent,dirs,files in os.walk(root):
            for name in dirs+files:os.utime(Path(parent,name),(time.time()-48*3600,)*2,follow_symlinks=False)
            os.utime(parent,(time.time()-48*3600,)*2)
    row=c.m.linked_plan(linked_row(c,target),linked_empty_context(c))
    assert row['action']=='remove' and 47 < row['idle_hours'] < 49, row
    clone=c.root/'independent';shell('git','clone','-q',str(c.bare),str(clone))
    for parent,dirs,files in os.walk(clone):
        for name in dirs+files:os.utime(Path(parent,name),(time.time()-48*3600,)*2,follow_symlinks=False)
        os.utime(parent,(time.time()-48*3600,)*2)
    assert '168h' in plan(c,clone)['reason']


@pytest.mark.parametrize('guard',['locked','environment','cwd_inside','recent_file','recent_metadata','dirty','untracked'])
def test_linked_only_realtime_conditions_keep_worktree(case,guard):
    c=case;target=linked(c);ctx=linked_empty_context(c)
    if guard=='locked':shell('git','worktree','lock',str(target),cwd=c.repo)
    if guard=='environment':ctx['environments'][str(target)]='BB environment: live'
    if guard=='cwd_inside':ctx['cwds'][str(target/'sub')]='process 1 cwd'
    if guard=='dirty':(target/'a').write_text('changed')
    if guard=='untracked':(target/'unique').write_text('private')
    if guard in ('dirty','untracked','locked'):age(c.root)
    if guard=='recent_file':(target/'a').touch()
    if guard=='recent_metadata':Path(shell('git','rev-parse','--absolute-git-dir',cwd=target),'index').touch()
    row=c.m.linked_plan(linked_row(c,target),ctx)
    assert row['action']=='keep',row
    assert any(token in row['reason'] for token in ('locked','BB environment','cwd','24h','dirty')),row


def test_linked_cwd_ancestor_and_shared_common_activity_do_not_keep_children(case):
    c=case;target=linked(c);ctx=linked_empty_context(c);ctx['cwds'][str(c.repo)]='process 1 cwd'
    (c.repo/'.git/FETCH_HEAD').touch();(c.repo/'.git/config').touch()
    assert c.m.linked_plan(linked_row(c,target),ctx)['action']=='remove'


def test_linked_checks_only_its_branch_not_unrelated_heads_tags_or_stash(case):
    c=case;target=linked(c);head=shell('git','rev-parse','HEAD',cwd=c.repo)
    (c.repo/'a').write_text('other branch');shell('git','commit','-qam','other',cwd=c.repo)
    shell('git','-c','tag.gpgSign=false','tag','unpublished',cwd=c.repo)
    shell('git','update-ref','refs/stash',shell('git','rev-parse','HEAD',cwd=c.repo),cwd=c.repo)
    age(c.root)
    row=c.m.linked_plan(linked_row(c,target),linked_empty_context(c))
    assert row['action']=='remove' and set(row['objects'].values())=={head},row


def test_linked_ignores_generic_usage_and_foreign_nesting(case,monkeypatch):
    c=case;other=c.root/'other-main';shell('git','clone','-q',str(c.bare),str(other))
    target=c.repo/'.local/evidence/foreign-linked';shell('git','worktree','add','-q','-b','other',str(target),cwd=other);age(c.root)
    for name in ('usage','references','scope_unknown','configuration_name_anchors','same_origin_nested'):
        monkeypatch.setattr(c.m,name,lambda *a,**kw:(_ for _ in ()).throw(AssertionError('generic guard was used')))
    _,rows=c.m.linked_registrations(str(other))
    assert c.m.linked_plan(rows[0],linked_empty_context(c))['action']=='remove'


def test_linked_detached_head_has_remote_proof(case):
    c=case;target=c.root/'detached';shell('git','worktree','add','-q','--detach',str(target),cwd=c.repo);age(c.root)
    row=c.m.linked_plan(linked_row(c,target),linked_empty_context(c))
    assert row['action']=='remove' and 'HEAD' in row['objects'],row


def test_linked_archives_original_sha_then_git_removes_without_force(case,monkeypatch):
    c=case;target=linked(c);(target/'a').write_text('branch commit')
    shell('git','commit','-qam','local',cwd=target);age(c.root)
    ctx=linked_empty_context(c);row=c.m.linked_plan(linked_row(c,target),ctx);sha=row['head']
    assert row['action']=='archive+remove',row
    monkeypatch.setattr(c.m,'linked_context',lambda:ctx)
    c.m.linked_execute([row],c.root/'linked-events','linked-test')
    assert row['result']=='removed' and not target.exists() and c.repo.exists(),row
    assert row['archives'] and all(a['sha']==sha and shell('git','rev-parse',a['ref'],cwd=c.bare)==sha for a in row['archives'])
    removes=[x for x in c.calls if 'remove' in x]
    assert removes[-1][2]==str(c.repo) and '--force' not in removes[-1] and '-f' not in removes[-1]


@pytest.mark.parametrize('change',['cwd','environment','lock','dirty','head','archive_rejected','archive_wrong_sha'])
def test_linked_execution_rechecks_realtime_evidence_and_keeps_on_git_refusal(case,monkeypatch,change):
    c=case;target=linked(c)
    if change.startswith('archive_'):
        (target/'a').write_text('local');shell('git','commit','-qam','local',cwd=target);age(c.root)
    ctx=linked_empty_context(c);row=c.m.linked_plan(linked_row(c,target),ctx)
    assert row['action']!='keep',row
    if change=='cwd':ctx['cwds'][str(target)]='process 2 cwd'
    if change=='environment':ctx['environments'][str(target)]='BB environment: new'
    if change=='lock':shell('git','worktree','lock',str(target),cwd=c.repo)
    if change=='dirty':(target/'a').write_text('unique');age(c.root)
    if change=='head':(target/'a').write_text('unique');shell('git','commit','-qam','new',cwd=target);age(c.root)
    raw=c.m._run
    def altered(args,**kwargs):
        if change=='archive_rejected' and 'push' in args:raise RuntimeError('archive rejected')
        if change=='archive_wrong_sha' and 'ls-remote' in args and any('/archive/git-tree/' in x for x in args):
            return SimpleNamespace(stdout='0'*40+'\tref\n',returncode=0)
        return raw(args,**kwargs)
    monkeypatch.setattr(c.m,'_run',altered);monkeypatch.setattr(c.m,'linked_context',lambda:ctx)
    c.m.linked_execute([row],c.root/'events','test')
    assert row['result']=='kept' and target.exists(),row


def test_linked_git_remove_refusal_is_reported_without_raw_delete_fallback(case,monkeypatch):
    c=case;target=linked(c);ctx=linked_empty_context(c);row=c.m.linked_plan(linked_row(c,target),ctx)
    raw=c.m._run
    def refused(args,**kwargs):
        if 'worktree' in args and 'remove' in args:raise RuntimeError('Git refuses populated submodule')
        return raw(args,**kwargs)
    monkeypatch.setattr(c.m,'_run',refused);monkeypatch.setattr(c.m,'linked_context',lambda:ctx)
    c.m.linked_execute([row],c.root/'events','test')
    assert row['result']=='kept' and 'submodule' in row['reason'] and target.exists()


def test_linked_discovery_uses_bb_and_complete_disk_inventory_without_disk_walk(case,monkeypatch,capsys):
    c=case;target=linked(c);ctx=linked_empty_context(c);state=c.root/'state';state.mkdir()
    (state/'git-tree-sweep-test.json').write_text(json.dumps({'roots':['/','/data'],'items':[{'path':str(c.repo)}],'discovery':{'errors':[]}}))
    monkeypatch.setattr(c.m,'linked_context',lambda:dict(ctx,sources=[]))
    monkeypatch.setattr(c.m,'discover',lambda *a:(_ for _ in ()).throw(AssertionError('full disk walk')))
    assert c.m.linked_main(['--state-dir',str(state),'--json'])==0
    report=json.loads(capsys.readouterr().out)
    assert report['policy']=='linked' and report['indexed_discovery']
    assert report['items'][0]['path']==str(target) and report['items'][0]['action']=='remove' and target.exists()


def test_linked_prune_only_clears_missing_registration_and_preview_does_not_prune(case,monkeypatch,capsys):
    import shutil
    c=case;target=linked(c);ctx=linked_empty_context(c);sha=shell('git','rev-parse','work',cwd=c.repo)
    shell('git','config','gc.worktreePruneExpire','now',cwd=c.repo)
    shutil.rmtree(target)
    monkeypatch.setattr(c.m,'linked_context',lambda:ctx)
    state=c.root/'state'
    assert c.m.linked_main(['--state-dir',str(state),'--json'])==0
    preview=json.loads(capsys.readouterr().out)
    assert str(target) in shell('git','worktree','list','--porcelain',cwd=c.repo)
    prior=state/'preview.json';prior.write_text(json.dumps(preview))
    assert c.m.linked_main(['--state-dir',str(state),'--execute','--reviewed-preview',str(prior),'--json'])==0
    result=json.loads(capsys.readouterr().out)
    assert str(target) not in shell('git','worktree','list','--porcelain',cwd=c.repo)
    assert shell('git','rev-parse','work',cwd=c.repo)==sha and result['prunes'][0]['result']=='ok'


def test_linked_unknown_cwd_or_bb_inventory_never_removes(case,monkeypatch,capsys):
    c=case;target=linked(c)
    monkeypatch.setattr(c.m,'linked_context',lambda:(_ for _ in ()).throw(PermissionError('unknown process cwd')))
    assert c.m.linked_main(['--execute','--state-dir',str(c.root/'state'),'--json'])==1
    report=json.loads(capsys.readouterr().out)
    assert not report['complete'] and target.exists()


@pytest.mark.parametrize('mode',[[],['--execute']])
def test_worker_runs_linked_before_clones_and_preserves_each_budget(case,mode):
    c=case;probe=c.root/'python';log=c.root/'calls.jsonl'
    probe.write_text('#!'+os.sys.executable+'\nimport json,sys,pathlib\nwith pathlib.Path('+repr(str(log))+').open("a") as f:f.write(json.dumps(sys.argv[1:])+"\\n")\n')
    probe.chmod(0o755)
    result=subprocess.run(['bash',str(SCRIPT.with_name('git_tree_sweep_worker.sh')),str(probe),*mode],env=dict(os.environ,BB_CLI=str(bound_test_cli(c))),capture_output=True,text=True)
    calls=[json.loads(x) for x in log.read_text().splitlines()]
    assert result.returncode==0 and len(calls)==3
    assert [Path(call[1]).name for call in calls]==['bb_environment_sweep.py','worktree_sweep.py','git_tree_sweep.py']
    assert [call[call.index('--budget-minutes')+1] for call in calls]==['5','15','30']
    assert all(('--execute' in call)==bool(mode) for call in calls)


def test_linked_head_only_on_remote_tag_needs_branch_archive(case):
    c=case;target=linked(c)
    shell('git','-c','tag.gpgSign=false','tag','only-tag',cwd=c.repo)
    shell('git','push','-q',str(c.bare),'refs/tags/only-tag',cwd=c.repo)
    shell('git','update-ref','-d','refs/heads/main',cwd=c.bare);age(c.root)
    assert c.m.linked_plan(linked_row(c,target),linked_empty_context(c))['action']=='archive+remove'


def test_clone_main_does_not_feed_linked_trees_to_generic_plan(case,monkeypatch,capsys):
    c=case;target=linked(c);inventory=inv(c);seen=[]
    monkeypatch.setattr(c.m,'discover',lambda *a:inventory)
    monkeypatch.setattr(c.m,'managed_roots',lambda:set())
    monkeypatch.setattr(c.m,'references',lambda *a:{})
    monkeypatch.setattr(c.m,'usage',lambda *a,**kw:({},[]))
    def guard(path,kind,*args):
        assert kind!='linked';seen.append(path)
        return {'path':path,'type':kind,'action':'keep','archives':[],'reason':'fixture'}
    monkeypatch.setattr(c.m,'plan',guard)
    assert c.m.main(['--state-dir',str(c.root/'state'),'--json'])==0
    report=json.loads(capsys.readouterr().out)
    assert str(target) not in seen and str(target) in report['discovery']['linked_markers']
    assert all(row['type']!='linked' for row in report['items'])


@pytest.mark.parametrize('failed_phase',[0,1,2])
def test_worker_attempts_both_phases_and_reports_failures(case,failed_phase):
    c=case;probe=c.root/'python';log=c.root/'calls'
    probe.write_text('#!'+os.sys.executable+'\nimport sys,pathlib\np=pathlib.Path('+repr(str(log))+')\nn=len(p.read_text().splitlines()) if p.exists() else 0\nwith p.open("a") as f:f.write("called\\n")\nsys.exit(7 if n=='+str(failed_phase)+' else 0)\n')
    probe.chmod(0o755)
    result=subprocess.run(['bash',str(SCRIPT.with_name('git_tree_sweep_worker.sh')),str(probe)],env=dict(os.environ,BB_CLI=str(bound_test_cli(c))),capture_output=True,text=True)
    assert result.returncode==7 and len(log.read_text().splitlines())==3


@pytest.mark.parametrize('status,phase,protected', [('active',None,True),('paused',None,True),('destroyed',None,False),('active','destroyed',False)])
def test_linked_context_reads_real_bb_environment_lifecycle_and_only_cwd(case,tmp_path,monkeypatch,status,phase,protected):
    c=case;proc=tmp_path/'proc';p=proc/'123';p.mkdir(parents=True)
    (p/'stat').write_text('123 (worker) '+' '.join(['1']*25));(p/'cwd').symlink_to(c.repo)
    (p/'environ').write_text('HOME=/isolated/home\0PATH=/other/tool/bin')
    project=[{'sources':[{'path':str(c.repo)}]}]
    env=[{'id':'e','path':str(c.repo),'status':status,'lifecycle':{'phase':phase}}]
    def command(args,**kw):return SimpleNamespace(stdout=json.dumps(project if args[1]=='project' else env))
    monkeypatch.setattr(c.m,'command',command)
    context=c.m.linked_context(str(proc))
    assert (str(c.repo) in context['environments']) is protected
    assert context['cwds']=={str(c.repo):'process 123 cwd'} and '/isolated/home' not in context['cwds']


@pytest.mark.parametrize('fault',['missing_environment_path','cwd_denied','identity_reused'])
def test_linked_context_unknown_observations_fail_closed(case,tmp_path,monkeypatch,fault):
    c=case;proc=tmp_path/'proc';p=proc/'123';p.mkdir(parents=True)
    (p/'stat').write_text('123 (worker) '+' '.join(['1']*25));(p/'cwd').symlink_to(c.repo)
    env=[{'id':'e','status':'active'}] if fault=='missing_environment_path' else []
    monkeypatch.setattr(c.m,'command',lambda args,**kw:SimpleNamespace(stdout=json.dumps([{'sources':[]}] if args[1]=='project' else env)))
    raw=c.m.os.readlink
    def link(path,*args,**kw):
        if str(path)==str(p/'cwd'):
            if fault=='cwd_denied':raise PermissionError('denied')
            if fault=='identity_reused':(p/'stat').write_text('123 (worker) '+' '.join(['2']*25))
        return raw(path,*args,**kw)
    monkeypatch.setattr(c.m.os,'readlink',link)
    with pytest.raises(c.m.ws.ScanIncomplete):c.m.linked_context(str(proc))


def test_linked_unpushed_branch_without_archive_destination_stays(case):
    c=case;target=linked(c);(target/'a').write_text('local');shell('git','commit','-qam','local',cwd=target)
    shell('git','remote','set-url','--push','origin','https://github.com/unrelated/repo.git',cwd=c.repo);age(c.root)
    row=c.m.linked_plan(linked_row(c,target),linked_empty_context(c))
    assert row['action']=='keep' and 'no personal or internal remote' in row['reason']


def test_linked_git_prune_preserves_locked_missing_worktree(case,monkeypatch,capsys):
    import shutil
    c=case;target=linked(c);shell('git','worktree','lock',str(target),cwd=c.repo);shutil.rmtree(target)
    monkeypatch.setattr(c.m,'linked_context',lambda:linked_empty_context(c))
    assert c.m.linked_main(['--execute','--state-dir',str(c.root/'state'),'--json'])==0
    report=json.loads(capsys.readouterr().out)
    assert 'locked' in report['items'][0]['reason'] and str(target) in shell('git','worktree','list','--porcelain',cwd=c.repo)


@pytest.mark.parametrize('location,protected',[('root',True),('child',True),('ancestor',False)])
def test_linked_cwd_mount_alias_uses_inode_ancestry(case,tmp_path,monkeypatch,location,protected):
    c=case;target=linked(c);child=target/'child';child.mkdir();age(c.root)
    proc=tmp_path/'proc';p=proc/'123';p.mkdir(parents=True)
    (p/'stat').write_text('123 (worker) '+' '.join(['1']*25))
    directory={'root':target,'child':child,'ancestor':c.repo}[location]
    (p/'cwd').symlink_to(directory)
    raw=c.m.os.readlink
    def alias(path,*args,**kw):
        if str(path)==str(p/'cwd'):return '/container/mount/cwd'
        return raw(path,*args,**kw)
    monkeypatch.setattr(c.m.os,'readlink',alias)
    monkeypatch.setattr(c.m,'command',lambda args,**kw:SimpleNamespace(stdout=json.dumps([{'sources':[]}] if args[1]=='project' else [])) if args[0]=='bb' else original(args,**kw))
    original=c.m._run
    context=c.m.linked_context(str(proc))
    row=c.m.linked_plan(linked_row(c,target),context)
    assert (row['action']=='keep') is protected,row
    if protected:assert 'cwd' in row['reason']


def test_unregistered_separate_gitfile_is_listed_and_kept(case,monkeypatch,capsys):
    c=case;target=c.root/'gitfile-primary'
    shell('git','clone','-q','--separate-git-dir',str(c.root/'metadata'),str(c.bare),str(target));age(c.root)
    monkeypatch.setattr(c.m,'linked_context',lambda:dict(linked_empty_context(c),sources=[str(target)]))
    assert c.m.linked_main(['--execute','--state-dir',str(c.root/'state'),'--json'])==0
    report=json.loads(capsys.readouterr().out)
    row=next(row for row in report['items'] if row['path']==str(target))
    assert row['action']=='keep' and 'not a registered' in row['reason'] and target.exists()


def test_missing_gitdir_worktree_data_is_kept_while_registration_inventory_is_read(case,monkeypatch,capsys):
    import shutil
    c=case;target=linked(c);gitdir=Path(shell('git','rev-parse','--absolute-git-dir',cwd=target));shutil.rmtree(gitdir)
    monkeypatch.setattr(c.m,'linked_context',lambda:dict(linked_empty_context(c),sources=[str(c.repo),str(target)]))
    assert c.m.linked_main(['--execute','--state-dir',str(c.root/'state'),'--json'])==0
    report=json.loads(capsys.readouterr().out)
    row=next(row for row in report['items'] if row['path']==str(target))
    assert row['action']=='keep' and target.exists() and (target/'a').read_text()=='a'


def test_unreadable_known_git_source_is_reported_without_blocking_other_mains(case,monkeypatch):
    c=case;target=linked(c);denied=str(c.root/'denied')
    raw=c.m.os.stat
    def stat_path(path,*args,**kwargs):
        if str(path)==denied:raise PermissionError('cannot read this main')
        return raw(path,*args,**kwargs)
    monkeypatch.setattr(c.m.os,'stat',stat_path)
    mains,failures=c.m.linked_inventory([str(c.repo),denied])
    assert str(c.repo) in mains and any(row['path']==denied for row in failures)
    assert any(row['path']==str(target) for row in mains[str(c.repo)])


@pytest.mark.parametrize('style',['worktree','file_worktree','alias','main','gitdir'])
def test_linked_self_or_shared_store_remote_is_not_recovery_proof(case,style):
    c=case;target=linked(c);alias=c.root/'alias';alias.symlink_to(target)
    url={'worktree':str(target),'file_worktree':target.as_uri(),'alias':str(alias),'main':str(c.repo),'gitdir':(c.repo/'.git').as_uri()}[style]
    shell('git','remote','set-url','origin',url,cwd=c.repo);age(c.root)
    row=c.m.linked_plan(linked_row(c,target),linked_empty_context(c))
    assert row['action']=='keep',row
    assert not any('ls-remote' in call for call in c.calls)


def test_linked_discovery_requeries_primary_instead_of_trusting_a_backup_copy(case,monkeypatch):
    c=case;target=linked(c);backup=c.root/'backup';backup.mkdir()
    original=c.m.linked_registrations
    def registration(path):
        if str(path)==str(backup):return str(c.repo),[]
        return original(path)
    monkeypatch.setattr(c.m,'linked_registrations',registration)
    mains,_=c.m.linked_inventory([str(c.repo),str(backup)])
    assert any(row['path']==str(target) for row in mains[str(c.repo)])


def test_linked_uses_a_proven_origin_without_waiting_for_unrelated_remote(case,monkeypatch):
    c=case;target=linked(c);dead='https://unused.invalid/other.git'
    shell('git','remote','add','offline',dead,cwd=c.repo);age(c.root)
    raw=c.m._run
    def command(args,**kw):
        if 'ls-remote' in args and dead in args:raise RuntimeError('unused remote unavailable')
        return raw(args,**kw)
    monkeypatch.setattr(c.m,'_run',command)
    row=c.m.linked_plan(linked_row(c,target),linked_empty_context(c))
    assert row['action']=='remove',row
    assert not any('ls-remote' in call and dead in call for call in c.calls)


def test_linked_fetch_failure_can_plan_original_sha_archive_but_never_delete_without_verification(case,monkeypatch):
    c=case;target=linked(c);ctx=linked_empty_context(c);raw=c.m._run
    def unavailable(args,**kw):
        if 'ls-remote' in args:raise RuntimeError('remote query unavailable')
        return raw(args,**kw)
    monkeypatch.setattr(c.m,'_run',unavailable)
    row=c.m.linked_plan(linked_row(c,target),ctx)
    assert row['action']=='archive+remove',row
    monkeypatch.setattr(c.m,'linked_context',lambda:ctx)
    c.m.linked_execute([row],c.root/'events','verify-failure')
    assert row['result']=='kept' and target.exists() and not row['archives'],row


def test_linked_entry_clears_inherited_git_redirect_before_git_removal(case,monkeypatch,capsys):
    c=case;target=linked(c);other=c.root/'other.git';shell('git','init','-q','--bare',str(other))
    (other/'sentinel').write_text('unchanged')
    c.m.ws.ENV.update(GIT_DIR=str(other),GIT_WORK_TREE=str(c.root/'wrong-tree'),GIT_CONFIG_COUNT='1',GIT_CONFIG_KEY_0='remote.injected.url',GIT_CONFIG_VALUE_0='/wrong')
    monkeypatch.setattr(c.m,'linked_context',lambda:linked_empty_context(c))
    assert c.m.linked_main(['--execute','--state-dir',str(c.root/'state'),'--json'])==0
    report=json.loads(capsys.readouterr().out)
    assert any(x.get('result')=='removed' and x['path']==str(target) for x in report['items'])
    assert not target.exists() and (other/'sentinel').read_text()=='unchanged'


def test_linked_report_retains_new_remote_proof_when_another_row_already_archived_sha(case,monkeypatch):
    c=case;first=linked(c);(first/'a').write_text('unpublished');shell('git','commit','-qam','local',cwd=first)
    second=c.root/'second';sha=shell('git','rev-parse','HEAD',cwd=first)
    shell('git','worktree','add','-q','-b','second',str(second),sha,cwd=c.repo);age(c.root)
    ctx=linked_empty_context(c);rows=[c.m.linked_plan(linked_row(c,path),ctx) for path in (first,second)]
    assert all(row['action']=='archive+remove' for row in rows)
    monkeypatch.setattr(c.m,'linked_context',lambda:ctx)
    c.m.linked_execute(rows,c.root/'events','shared-sha')
    assert all(row['result']=='removed' for row in rows),rows
    assert rows[0]['archives'] and not rows[1]['archives'] and all(rows[1]['remote_proofs'].values())


def test_explicit_linked_prune_uses_git_command_semantics_when_gc_pruning_is_disabled(case,monkeypatch,capsys):
    import shutil
    c=case;target=linked(c);shell('git','config','gc.worktreePruneExpire','never',cwd=c.repo)
    shutil.rmtree(target);monkeypatch.setattr(c.m,'linked_context',lambda:linked_empty_context(c))
    assert c.m.linked_main(['--execute','--state-dir',str(c.root/'state'),'--json'])==0
    report=json.loads(capsys.readouterr().out)
    assert report['prunes'][0]['result']=='ok'
    # gc.worktreePruneExpire configures git gc, not an explicit worktree prune.
    assert str(target) not in shell('git','worktree','list','--porcelain',cwd=c.repo)


@pytest.mark.parametrize('admin_directory',['.','logs'])
def test_linked_administrative_directory_mtime_does_not_self_refresh_idle_age(case,admin_directory):
    c=case;target=linked(c);gitdir=Path(shell('git','rev-parse','--absolute-git-dir',cwd=target))
    os.utime(gitdir/admin_directory,None)
    row=c.m.linked_plan(linked_row(c,target),linked_empty_context(c))
    assert row['action']=='remove' and row['idle_hours']>=24,row


@pytest.mark.parametrize('git_file',['HEAD','FETCH_HEAD','logs/HEAD'])
def test_linked_actual_recent_git_files_still_count_as_activity(case,git_file):
    c=case;target=linked(c);gitdir=Path(shell('git','rev-parse','--absolute-git-dir',cwd=target))
    (gitdir/git_file).touch()
    row=c.m.linked_plan(linked_row(c,target),linked_empty_context(c))
    assert row['action']=='keep' and '24h' in row['reason'],row


def test_linked_recent_source_directory_still_counts_as_activity(case):
    c=case;target=linked(c);(target/'empty-directory').mkdir()
    row=c.m.linked_plan(linked_row(c,target),linked_empty_context(c))
    assert row['action']=='keep' and '24h' in row['reason'],row


def test_linked_bind_mount_of_child_directory_is_detected_without_host_parent_ancestry(case):
    c=case;target=linked(c);child=target/'served';child.mkdir();age(c.root)
    info=child.stat();ctx=linked_empty_context(c)
    ctx['cwds']={'/container/workdir':'process 456 cwd'}
    # A container bind rooted at this child walks to the container parent,
    # not the host worktree root. Its cwd inode still identifies the directory.
    ctx['cwd_identities']={f'{info.st_dev}:{info.st_ino}':'process 456 cwd (bound child)'}
    row=c.m.linked_plan(linked_row(c,target),ctx)
    assert row['action']=='keep' and 'cwd' in row['reason'],row


def bb_old_environment(c, managed=False):
    return dict(id='env-old',path=str(c.repo),hostId='host-local',status='ready',
                managed=managed,environmentProviderId='git-worktree' if managed else None,
                createdAt=OLD*1000,updatedAt=OLD*1000,lifecycle={'phase':'active'})


def bb_archived_thread(env='env-old'):
    return dict(id='thread-old',environmentId=env,archivedAt=OLD*1000,createdAt=OLD*1000,
                updatedAt=OLD*1000,status='idle',activity={},queuedWork='none')


@pytest.mark.parametrize('threads,age_hours,abandoned',[
    ([],25,True),([True],25,True),([False],500,False),([True,False],500,False),
    ([True],23.99,False),([],24,True),([True],24,True)])
def test_bb_environment_requires_archival_and_24h(case,threads,age_hours,abandoned):
    c=case;now=time.time();env=bb_old_environment(c)
    env['updatedAt']=env['createdAt']=(now-age_hours*3600)*1000
    ts=[]
    for index,archived in enumerate(threads):
        t=bb_archived_thread();t.update(id=f't-{index}',updatedAt=env['updatedAt'],createdAt=env['createdAt'],archivedAt=env['updatedAt'] if archived else None);ts.append(t)
    row=c.m.bb_environment_classify(env,ts,now)
    assert row['abandoned'] is abandoned and row['protects'] is not abandoned,row


@pytest.mark.parametrize('change',['env_timestamp_missing','thread_timestamp_missing','future_timestamp','recent_archive','recent_read','busy_archived','queued_archived'])
def test_bb_environment_unknown_or_recent_activity_keeps(case,change):
    c=case;env=bb_old_environment(c);t=bb_archived_thread()
    if change=='env_timestamp_missing':env.pop('updatedAt')
    if change=='thread_timestamp_missing':t.pop('createdAt')
    if change=='future_timestamp':t['updatedAt']=(time.time()+3600)*1000
    if change=='recent_archive':t['archivedAt']=time.time()*1000
    if change=='recent_read':t['lastReadAt']=time.time()*1000
    if change=='busy_archived':t['activity']={'activeBackgroundCommandCount':1}
    if change=='queued_archived':t['queuedWork']='queued'
    row=c.m.bb_environment_classify(env,[t])
    assert row['protects'] and not row['abandoned'],row


@pytest.mark.parametrize('status,phase,teardown,protected',[
    ('destroyed','destroyed',None,False),('ready','removing',None,True),
    ('ready','active',{'status':'running'},True),('error','active',{'status':'failed'},False)])
def test_bb_environment_destroyed_running_and_refused_lifecycle(case,status,phase,teardown,protected):
    env=bb_old_environment(case,True);env.update(status=status,lifecycle={'phase':phase,'teardown':teardown})
    assert case.m.bb_environment_classify(env,[])['protects'] is protected


@pytest.mark.parametrize('data',[{},[{}],[{'id':'thread','archivedAt':None}]])
def test_bb_environment_malformed_thread_inventory_is_unknown(case,data):
    with pytest.raises(case.m.ws.ScanIncomplete):case.m.bb_environment_inventory([bb_old_environment(case)],data)


@pytest.mark.parametrize('archived',[True,False])
def test_abandoned_environment_releases_both_linked_and_clone_protection(case,monkeypatch,archived):
    c=case;target=linked(c);env=bb_old_environment(c);env['path']=str(target);t=bb_archived_thread();t['archivedAt']=OLD*1000 if archived else None
    raw=c.m.command
    def respond(args,**kw):
        if args[0]!='bb':return raw(args,**kw)
        value={'project':[{'id':'p','sources':[]}],'environment':[env],'thread':[t],'automation':[]}[args[1]]
        return SimpleNamespace(stdout=json.dumps(value),returncode=0)
    monkeypatch.setattr(c.m,'command',respond);monkeypatch.setattr(c.m.ws,'bb_anchors',lambda *a:None)
    proc=c.root/'empty-proc';proc.mkdir();context=c.m.linked_context(str(proc))
    assert (c.m.linked_plan(linked_row(c,target),context)['action']!='keep') is archived
    anchors={};c.m.bb_anchors(anchors)
    assert bool(c.m.ws.anchored(anchors,str(target))) is not archived
    assert plan(c,target,'linked',anchors=anchors)['action']==('remove' if archived else 'keep')


def bb_environment_fake_cli(c,monkeypatch,env,threads=None,fresh_threads=None,response=None,observed=None):
    threads=[] if threads is None else threads;calls=[];raw=c.m.command
    def run(args,**kw):
        if args[0]!='bb':return raw(args,**kw)
        calls.append(args)
        if args[1]=='status':value={'thread':None}
        elif args[1]=='machine':value=[{'id':'host-local','machineProviderId':None,'status':'connected','type':'persistent'}]
        elif args[1:3]==['environment','list']:value=[env]
        elif args[1:3]==['thread','list']:value=fresh_threads if '--environment' in args and fresh_threads is not None else threads
        elif args[1:3]==['environment','show']:value=observed if observed is not None and any(x[1:3]==['environment','cleanup'] for x in calls) else env
        elif args[1:3]==['environment','cleanup']:return response or SimpleNamespace(stdout='{"ok":true}',stderr='',returncode=0)
        else:raise AssertionError(args)
        return SimpleNamespace(stdout=json.dumps(value),stderr='',returncode=0)
    monkeypatch.setattr(c.m,'command',run)
    monkeypatch.setattr(c.m,'linked_context',lambda:dict(linked_empty_context(c),sources=[]))
    return calls


@pytest.mark.parametrize('managed,execute',[(False,False),(False,True),(True,False),(True,True)])
def test_environment_sweep_previews_and_uses_bb_owner_command_only(case,monkeypatch,capsys,managed,execute):
    c=case;env=bb_old_environment(c,managed);observed=dict(env,status='destroyed',lifecycle={'phase':'destroyed'})
    calls=bb_environment_fake_cli(c,monkeypatch,env,observed=observed)
    assert c.m.bb_environment_main(['--state-dir',str(c.root/'state'),'--json',*(['--execute'] if execute else [])])==0
    report=json.loads(capsys.readouterr().out);row=report['items'][0]
    assert row['action']==('provider-cleanup' if managed else 'release-protection')
    assert bool([x for x in calls if x[1:3]==['environment','cleanup']]) is (managed and execute)
    assert not any(x[1:3]==['environment','delete'] or '--recover' in x for x in calls)
    assert c.repo.exists()  # BB is mocked; sweeper has no direct directory sink.
    if managed and execute:assert row['result']=='destroyed'


@pytest.mark.parametrize('fault',['unarchived','recent','process_cwd','another_environment','new_file','cleanup_refused','async_pending','bad_response'])
def test_environment_cleanup_rebinds_and_reports_owner_refusal(case,monkeypatch,capsys,fault):
    c=case;env=bb_old_environment(c,True);fresh=[];ctx=linked_empty_context(c)
    if fault=='unarchived':fresh=[dict(bb_archived_thread(),archivedAt=None)]
    if fault=='recent':env['updatedAt']=time.time()*1000
    if fault=='process_cwd':ctx['cwds'][str(c.repo/'child')]='live cwd'
    if fault=='another_environment':ctx['environments'][str(c.repo)]='BB environment: live'
    if fault=='new_file':(c.repo/'just-created').touch()
    response=SimpleNamespace(stdout='{"ok":false,"error":{"code":"invalid_request","message":"BB refuses cleanup"}}',stderr='',returncode=1) if fault=='cleanup_refused' else SimpleNamespace(stdout='{}',stderr='',returncode=0) if fault=='bad_response' else None
    calls=bb_environment_fake_cli(c,monkeypatch,env,fresh_threads=fresh,response=response)
    monkeypatch.setattr(c.m,'linked_context',lambda:ctx)
    assert c.m.bb_environment_main(['--execute','--json','--state-dir',str(c.root/'state')])==(1 if fault=='bad_response' else 0)
    row=json.loads(capsys.readouterr().out)['items'][0];mutations=[x for x in calls if x[1:3]==['environment','cleanup']]
    if fault in ('cleanup_refused','async_pending','bad_response'):
        assert len(mutations)==1
        if fault=='cleanup_refused':assert row['result']=='refused; Git fallback' and not c.m.bb_environment_classify(env,[])['protects']
        if fault=='async_pending':assert row['result']=='cleanup requested; pending'
        if fault=='bad_response':assert row['result']=='failed' and 'response unknown' in row['cleanup_error']
    else:assert not mutations
    assert c.repo.exists()


def test_environment_sweep_incomplete_inventory_does_not_mutate(case,monkeypatch,capsys):
    c=case;calls=bb_environment_fake_cli(c,monkeypatch,bb_old_environment(c,True),threads={})
    assert c.m.bb_environment_main(['--execute','--json','--state-dir',str(c.root/'state')])==1
    report=json.loads(capsys.readouterr().out);assert not report['complete']
    assert not any(x[1:3]==['environment','cleanup'] for x in calls)


def test_environment_sweep_does_not_require_thread_context(case,monkeypatch,capsys):
    c=case;env=bb_old_environment(c);calls=bb_environment_fake_cli(c,monkeypatch,env);raw=c.m.command
    def run(args,**kw):
        if args[:2]==['bb','status']:return SimpleNamespace(stdout='{"thread":null}',returncode=0)
        if args[:2]==['bb','machine']:return SimpleNamespace(stdout=json.dumps([{'id':'host-local','machineProviderId':None,'status':'connected','type':'persistent'}]),returncode=0)
        return raw(args,**kw)
    monkeypatch.setattr(c.m,'command',run)
    assert c.m.bb_environment_main(['--execute','--json','--state-dir',str(c.root/'state')])==0
    report=json.loads(capsys.readouterr().out)
    assert report['items'][0]['action']=='release-protection' and report['complete']
    assert not any(x[1]=='status' for x in calls)


@pytest.mark.parametrize('fault',['show_query','cleanup_bad_json','cleanup_bad_ack','cleanup_transport','show_after_cleanup'])
def test_environment_query_and_protocol_errors_make_stage_fail(case,monkeypatch,capsys,fault):
    c=case;env=bb_old_environment(c,True);calls=bb_environment_fake_cli(c,monkeypatch,env);raw=c.m.command
    def run(args,**kw):
        if args[1:3]==['environment','show'] and (fault=='show_query' or fault=='show_after_cleanup' and any(x[1:3]==['environment','cleanup'] for x in calls)):
            raise RuntimeError('BB query unreachable')
        if args[1:3]==['environment','cleanup'] and fault.startswith('cleanup_'):
            if fault=='cleanup_bad_json':return SimpleNamespace(stdout='not JSON',stderr='',returncode=0)
            if fault=='cleanup_bad_ack':return SimpleNamespace(stdout='{}',stderr='',returncode=0)
            return SimpleNamespace(stdout='{"ok":false,"error":{"code":"network_error","message":"connection failed"}}',stderr='',returncode=1)
        return raw(args,**kw)
    monkeypatch.setattr(c.m,'command',run)
    assert c.m.bb_environment_main(['--execute','--json','--state-dir',str(c.root/'state')])==1
    report=json.loads(capsys.readouterr().out)
    assert not report['complete'] and report['error'] and report['items'][0]['result']=='failed'
    assert c.repo.exists()


@pytest.mark.parametrize('machines',[[],[{'id':'host-local','status':'connected','type':'persistent'}],
    [{'id':'a','machineProviderId':None,'status':'connected','type':'persistent'},{'id':'b','machineProviderId':None,'status':'connected','type':'persistent'}],
    [{'id':'remote','machineProviderId':'manual','status':'connected','type':'persistent'}]])
def test_environment_host_identity_unknown_prevents_cleanup(case,monkeypatch,capsys,machines):
    c=case;calls=bb_environment_fake_cli(c,monkeypatch,bb_old_environment(c,True));raw=c.m.command
    def run(args,**kw):
        if args[1]=='machine':return SimpleNamespace(stdout=json.dumps(machines),returncode=0)
        return raw(args,**kw)
    monkeypatch.setattr(c.m,'command',run)
    assert c.m.bb_environment_main(['--execute','--json','--state-dir',str(c.root/'state')])==1
    assert not json.loads(capsys.readouterr().out)['complete']
    assert not any(x[1:3]==['environment','cleanup'] for x in calls)


def test_environment_stage_continues_other_environments_after_query_failure(case,monkeypatch,capsys):
    c=case;env=bb_old_environment(c,True);other=dict(bb_old_environment(c),id='other-env');calls=bb_environment_fake_cli(c,monkeypatch,env);raw=c.m.command
    def run(args,**kw):
        if args[1:3]==['environment','list']:return SimpleNamespace(stdout=json.dumps([env,other]),returncode=0)
        if args[1:3]==['environment','show']:raise RuntimeError('query failure')
        return raw(args,**kw)
    monkeypatch.setattr(c.m,'command',run)
    assert c.m.bb_environment_main(['--execute','--json','--state-dir',str(c.root/'state')])==1
    report=json.loads(capsys.readouterr().out)
    assert report['items'][0]['result']=='failed' and report['items'][1]['action']=='release-protection'
    assert len(report['failures'])==1


def bound_test_cli(c,packaged_node=False):
    package=c.root/'server package'
    cli=package/('node_modules/bb-app/host-daemon/dist/bb' if packaged_node else 'bb');cli.parent.mkdir(parents=True)
    implementation='#!'+os.sys.executable+'\nimport json,os,sys\nfrom pathlib import Path\na=sys.argv[1:]\ncli=Path(__file__).resolve()\n'
    if packaged_node:
        cli.write_text('#!/usr/bin/env node\n// Executed only by the package runtime.\n')
        runtime=package/'bin/node';runtime.parent.mkdir(parents=True)
        implementation+='cli=Path(a.pop(0)).resolve()\n'
    else:runtime=cli
    implementation+='if os.environ.get("CLI_TEST_LOG"):\n with open(os.environ["CLI_TEST_LOG"],"a") as f:f.write(json.dumps({"cli":str(cli),"args":a})+"\\n")\nif "--help" in a:print("--include-archived --include-hidden --json --all")\nelif a[:2]==["machine","list"]:print(json.dumps([{"id":"local","machineProviderId":None,"status":"connected","type":"persistent"}]))\nelif a[:2]==["environment","list"]:print(json.dumps([{"id":"old","status":"destroyed","createdAt":1,"updatedAt":1}]))\nelse:print("[]")\n'
    runtime.write_text(implementation);runtime.chmod(0o755);cli.chmod(0o755);return cli


@pytest.mark.parametrize('packaged_node',[False,True])
def test_worker_uses_bound_server_cli_even_with_old_bb_on_path(case,monkeypatch,packaged_node):
    c=case;cli=bound_test_cli(c,packaged_node);bindir=c.root/'bin';bindir.mkdir();old_called=c.root/'old-called';old=bindir/'bb'
    old.write_text('#!'+os.sys.executable+'\nfrom pathlib import Path\nPath('+repr(str(old_called))+').touch()\nraise SystemExit(9)\n');old.chmod(0o755)
    probe=c.root/'python';probe.write_text('#!'+os.sys.executable+'''\nfrom pathlib import Path
import os,sys,types
p=Path(os.environ["CLI_TEST_CORE"])
m=types.ModuleType("cli_test");m.__file__=str(p)
exec(compile(p.read_bytes(),str(p),"exec"),m.__dict__)
# Replace host discovery only; execute the real stage entry and BB query paths,
# including clone usage -> bb_anchors -> ws.bb_anchors -> ws.run.
m.managed_roots=lambda:set()
m.discover=lambda *a:{"trees":{},"bare":[],"links":[],"errors":[],"directories":0,"excluded":{}}
m.process_anchors=lambda *a:{}
m.config_anchors=lambda *a,**kw:[]
m.name_config_sources=lambda:[]
m.shutil.which=lambda name:None
listdir=m.os.listdir
m.os.listdir=lambda path:[] if str(path)=="/proc" else listdir(path)
entry={"bb_environment_sweep.py":m.bb_environment_main,"worktree_sweep.py":m.linked_main,"git_tree_sweep.py":m.main}[Path(sys.argv[2]).name]
raise SystemExit(entry([*sys.argv[3:],"--state-dir",os.environ["CLI_TEST_STATE"]]))
''');probe.chmod(0o755)
    state=c.root/'state';log=c.root/'cli.jsonl';env=dict(os.environ,PATH=str(bindir)+':/usr/bin:/bin',BB_CLI=str(cli),CLI_TEST_LOG=str(log),CLI_TEST_CORE=str(SCRIPT),CLI_TEST_STATE=str(state))
    after=subprocess.run(['/bin/bash',str(SCRIPT.with_name('git_tree_sweep_worker.sh')),str(probe)],env=env,capture_output=True,text=True)
    assert after.returncode==0 and not old_called.exists(),after.stderr
    queries=[x for x in map(json.loads,log.read_text().splitlines()) if '--help' not in x['args']]
    assert all(x['cli']==str(cli) for x in queries)
    threads=[x for x in queries if x['args'][:2]==['thread','list']]
    assert len(threads)==4 and len([x for x in threads if '--include-archived' in x['args']])==3
    reports=[json.loads(p.read_text()) for p in state.glob('*.json')]
    assert len(reports)==3 and all(p['complete'] and p['error'] is None for p in reports)


@pytest.mark.parametrize('bad',['missing','relative','not-executable'])
def test_bb_command_refuses_unbound_cli_without_path_fallback(case,monkeypatch,bad):
    c=case;p=c.root/'bad-cli';p.write_text('no exec')
    value={'missing':'','relative':'bb','not-executable':str(p)}[bad];monkeypatch.setenv('BB_CLI',value)
    monkeypatch.setattr(c.m,'_run',lambda *a,**k:(_ for _ in ()).throw(AssertionError('PATH must not be invoked')))
    with pytest.raises(c.m.ws.ScanIncomplete,match='run git_tree_sweep_run.sh'):c.m.command(['bb','thread','list'])


def test_bb_command_overrides_stale_child_environment(case,monkeypatch):
    c=case;cli=bound_test_cli(c);monkeypatch.setenv('BB_CLI',str(cli));seen=[]
    monkeypatch.setattr(c.m,'_run',lambda args,**kw:seen.append((args,kw)) or SimpleNamespace(stdout='[]'))
    c.m.command(['bb','thread','list'],env={'BB_CLI':'/stale/shim','OTHER':'preserved'})
    assert seen[0][0][0]==str(cli) and seen[0][1]['env']=={'BB_CLI':str(cli),'OTHER':'preserved'}


@pytest.mark.parametrize('packaged_node',[False,True])
def test_cli_resolver_uses_service_package_when_not_injected(case,monkeypatch,packaged_node):
    c=case;m=c.m.sibling('git_tree_sweep_bb_cli');cli=bound_test_cli(c,packaged_node)
    package=c.root/'install';target=package/'node_modules/bb-app/host-daemon/dist/bb';target.parent.mkdir(parents=True);target.symlink_to(cli)
    monkeypatch.delenv('BB_CLI',raising=False);monkeypatch.setenv('PATH','/usr/bin:/bin');raw=m.subprocess.run;seen=[]
    def run(args,**kw):
        seen.append(args)
        if args[0]=='/usr/bin/systemctl':return SimpleNamespace(stdout='123\n',returncode=0)
        return raw(args,**kw)
    raw_link=m.os.readlink
    monkeypatch.setattr(m.subprocess,'run',run);monkeypatch.setattr(m.os,'readlink',lambda value,**kw:str(package/'bin/node') if value=='/proc/123/exe' else raw_link(value,**kw))
    assert m.resolve()==str(cli)
    assert seen[0][1:]==['show','bb-dev-server.service','--property=MainPID','--value']


def test_cli_node_entry_refuses_path_runtime_when_package_runtime_missing(case,monkeypatch):
    c=case;m=c.m.sibling('git_tree_sweep_bb_cli');cli=bound_test_cli(c,True)
    (c.root/'server package/bin/node').unlink();monkeypatch.setenv('BB_CLI',str(cli))
    monkeypatch.setattr(m.subprocess,'run',lambda *a,**k:(_ for _ in ()).throw(AssertionError('PATH runtime must not run')))
    with pytest.raises(RuntimeError,match='Packaged Node unavailable.*inspect bb-dev-server.service'):m.resolve()


def test_cli_probe_reports_nonzero_stderr_and_next_step(case,monkeypatch):
    c=case;m=c.m.sibling('git_tree_sweep_bb_cli');cli=c.root/'broken-cli'
    cli.write_text('#!/bin/sh\necho interpreter-sentinel >&2\nexit 17\n');cli.chmod(0o755);monkeypatch.setenv('BB_CLI',str(cli))
    with pytest.raises(RuntimeError,match='probe exited 17.*interpreter-sentinel.*inspect this CLI --version'):m.resolve()


@pytest.mark.parametrize('fault',['inactive','missing-package','old-cli'])
def test_cli_resolver_failure_names_cause_and_next_step(case,monkeypatch,fault):
    c=case;m=c.m.sibling('git_tree_sweep_bb_cli');monkeypatch.delenv('BB_CLI',raising=False)
    if fault=='old-cli':
        cli=c.root/'old';cli.write_text('#!/bin/sh\necho "--json"\n');cli.chmod(0o755);monkeypatch.setenv('BB_CLI',str(cli))
    else:
        monkeypatch.setattr(m.subprocess,'run',lambda *a,**kw:SimpleNamespace(stdout='0' if fault=='inactive' else '123',returncode=0))
        monkeypatch.setattr(m.os,'readlink',lambda _:str(c.root/'absent/bin/node'))
    with pytest.raises(RuntimeError) as error:m.resolve()
    message=str(error.value)
    assert ('start' in message.lower() or 'inspect' in message.lower()) and ('BB_CLI' in message or 'MainPID' in message)
    if fault=='old-cli':assert 'missing --include-archived' in message and 'PATH bb is never used' in message


@pytest.mark.parametrize('operand', ['absolute', 'flag', 'file-url', 'relative', 'bare', 'short-directory', 'shell', 'spaces', 'equals', 'file-url-equals', 'bare-equals', 'shell-semicolon', 'shell-flag-semicolon', 'shell-pipe', 'quoted-punctuation', 'bare-dash', 'shell-login', 'shell-trace', 'shell-option-argument'])
def test_live_process_argv_protects_source_checkout(case, tmp_path, operand):
    c=case;proc=tmp_path/'proc';p=proc/'987654';(p/'fd').mkdir(parents=True);(p/'cmdline').write_bytes(b'')
    target=c.root/'-source' if operand=='bare-dash' else c.root/'tool; clone&' if operand=='quoted-punctuation' else c.root/'local-source' if operand=='bare' else c.root/'tool=clone' if operand in ('equals','file-url-equals','bare-equals') else c.repo if operand!='spaces' else c.root/'tool clone';target.mkdir(exist_ok=True)
    (p/'cwd').symlink_to(c.root)
    (p/'stat').write_text('987654 (git) '+' '.join(['1']*25));(p/'status').write_text('Uid: 0 0 0 0\n')
    (p/'maps').write_text('');(p/'environ').write_bytes(b'')
    args={'absolute':['git','clone','--shared',str(target)],'flag':['git','clone','--reference='+str(target)],
          'file-url':['git','clone','file://'+str(target)],'relative':['git','clone',os.path.relpath(target,c.root)],
          'bare':['git','clone','--shared','local-source'],'short-directory':['git','-C'+str(target),'status'],
          'shell':['bash','-c','git clone --shared '+str(target)],'spaces':['git','clone','--reference',str(target)],
          'equals':['git','clone','--shared',str(target)],'file-url-equals':['git','clone','file://'+str(target)],'bare-equals':['git','clone','tool=clone'],
          'shell-semicolon':['bash','-c',f'git clone --shared "{target}"; echo done'],
          'shell-flag-semicolon':['bash','-c',f'git clone --reference="{target}"&& echo done'],
          'shell-pipe':['bash','-c',f'git clone --shared "{target}"|cat'],
          'quoted-punctuation':['bash','-c',f'git clone --shared "{target}"'],
          'shell-login':['bash','-lc',f'git clone --shared "{target}"; echo done'],
          'shell-trace':['bash','-xc',f'git clone --shared "{target}"; echo done'],
          'shell-option-argument':['bash','--rcfile','other-config','-o','errexit','-lc',f'git clone --shared "{target}"; echo done'],
          'bare-dash':['git','clone','--shared','--','-source']}[operand]
    (p/'cmdline').write_bytes(b'\0'.join(os.fsencode(arg) for arg in args)+b'\0')
    anchors={};c.m.process_anchors(anchors,str(proc))
    assert 'argv' in (c.m.ws.anchored(anchors,str(target)) or '')


def test_execute_reuses_round_inventory_and_does_not_repeat_global_usage(case,monkeypatch):
    c=case;second=c.root/'second';shell('git','clone','-q',str(c.bare),str(second));age(second)
    inventory=inv(c);refs=c.m.references(inventory)
    rows=[c.m.plan(str(path),'clone',inventory,{},refs) for path in (c.repo,second)]
    assert all(row['action']=='remove' for row in rows)
    for row in rows:row['bytes']=4096
    def forbidden(*a,**k):raise AssertionError('global inventory must be reused within the round')
    monkeypatch.setattr(c.m,'discover',forbidden);monkeypatch.setattr(c.m,'references',forbidden);monkeypatch.setattr(c.m,'usage',forbidden)
    monkeypatch.setattr(c.m,'process_anchors',lambda anchors:{});monkeypatch.setattr(c.m,'bb_environment_inventory',lambda:[])
    c.m.execute(rows,inventory,{},refs,[str(c.root)],set(),'reuse',c.root/'events')
    assert [row.get('result') for row in rows]==['removed','removed'],rows


@pytest.mark.parametrize('change',['dirty','stash','lock','refs','credential','bb_environment','bb_unknown','proc_unknown','remote_lost','remote_unreachable','cwd','fd','argv'])
def test_clone_sink_checks_live_evidence_after_preparation(case,monkeypatch,change):
    c=case;inventory=inv(c);row=c.m.plan(str(c.repo),'clone',inventory,{},{});row['bytes']=4096
    assert row['action']=='remove'
    environments=[];monkeypatch.setattr(c.m,'bb_environment_inventory',lambda:environments)
    monkeypatch.setattr(c.m,'usage',lambda *a,**k:({},[]))
    original=c.m.remove;raw=c.m._run;child=None
    if change not in ('cwd','fd','argv','proc_unknown'):
        monkeypatch.setattr(c.m,'process_anchors',lambda anchors:{})
    def remove(item,*args,**kw):
        nonlocal child
        if change=='dirty':(c.repo/'a').write_text('changed');age(c.repo)
        if change=='stash':
            (c.repo/'a').write_text('stashed');shell('git','stash','push','-qm','keep',cwd=c.repo);age(c.repo)
        if change=='lock':(c.repo/'.git/index.lock').touch();age(c.repo)
        if change=='refs':
            (c.repo/'a').write_text('new commit');shell('git','commit','-qam','new',cwd=c.repo);age(c.repo)
        if change=='credential':(c.repo/'.env').write_text('private');age(c.repo)
        if change=='bb_environment':environments.append({'id':'new-env','path':str(c.repo),'protects':True})
        if change=='bb_unknown':monkeypatch.setattr(c.m,'bb_environment_inventory',lambda:(_ for _ in ()).throw(c.m.ws.ScanIncomplete('BB inventory unknown')))
        if change=='proc_unknown':monkeypatch.setattr(c.m,'process_anchors',lambda a:(_ for _ in ()).throw(PermissionError('proc denied')))
        if change=='remote_lost':shell('git','update-ref','-d','refs/heads/main',cwd=c.bare)
        if change=='remote_unreachable':monkeypatch.setattr(c.m,'_run',lambda argv,**kwargs:(_ for _ in ()).throw(RuntimeError('remote unreachable')) if 'ls-remote' in argv else raw(argv,**kwargs))
        if change in ('cwd','fd','argv'):
            code='import os,sys\np=sys.stdin.readline().strip()\n'
            code+= 'os.chdir(p)\n' if change=='cwd' else 'held=open(p+"/a")\n' if change=='fd' else ''
            code+='print("ready",flush=True)\nsys.stdin.read()\n'
            child=subprocess.Popen([os.sys.executable,'-c',code,*([str(c.repo)] if change=='argv' else [])],cwd=c.root,stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
            child.stdin.write(str(c.repo)+'\n');child.stdin.flush();assert child.stdout.readline().strip()=='ready'
        return original(item,*args,**kw)
    monkeypatch.setattr(c.m,'remove',remove)
    try:c.m.execute([row],inventory,{},{},[str(c.root)],set(),'live',c.root/'events')
    finally:
        if child:
            child.stdin.close();child.wait(timeout=5);child.stdout.close()
    assert row.get('result')=='kept',row
    assert c.repo.exists()
    if child:assert 'process '+str(child.pid) in row.get('reason',''),row
    if change=='argv':assert 'argv' in row['reason']


def test_archive_refspecs_are_batched_and_verified_exactly(case,monkeypatch):
    c=case;(c.repo/'a').write_text('unpublished');shell('git','commit','-qam','new',cwd=c.repo)
    for tag in ('one','two','three'):shell('git','-c','tag.gpgSign=false','tag',tag,cwd=c.repo)
    age(c.repo);row,_=execute_case(c,monkeypatch)
    assert row['result']=='removed' and len(row['archives'])>=5
    pushes=[argv for argv in c.calls if 'push' in argv]
    assert len(pushes)==1
    assert all(any(arg==a['sha']+':'+a['ref'] for arg in pushes[0]) for a in row['archives'])


def test_unmaterialized_error_environment_does_not_reserve_a_directory(case,monkeypatch):
    c=case;env={'id':'unmaterialized','status':'error','path':None}
    row=c.m.bb_environment_classify(env,[])
    assert row['protects'] and row['unmaterialized']
    monkeypatch.setattr(c.m,'bb_environment_inventory',lambda:[row])
    assert c.m.clone_environment_anchors()=={}


def test_main_overlaps_size_measurement_with_reference_inventory(case,monkeypatch,capsys):
    import threading
    c=case;inventory=inv(c);du_started=threading.Event();raw=c.m.command
    monkeypatch.setattr(c.m,'managed_roots',lambda:set());monkeypatch.setattr(c.m,'discover',lambda *a:inventory)
    def references(_):
        assert du_started.wait(3),'size IO must overlap independent reference work'
        return {}
    def command(argv,**kw):
        if argv[0]=='du':du_started.set();return SimpleNamespace(stdout='4096\t'+argv[-1],returncode=0)
        return raw(argv,**kw)
    monkeypatch.setattr(c.m,'references',references);monkeypatch.setattr(c.m,'command',command);monkeypatch.setattr(c.m,'usage',lambda *a,**kw:({},[]))
    assert c.m.main(['--json','--state-dir',str(c.root/'state')])==0
    report=json.loads(capsys.readouterr().out)
    assert report['global_inventory']['scope']=='round' and report['global_inventory']['references_checked_at']
    assert report['complete'] and all(r['bytes']==4096 for r in report['items'])


def test_clone_live_commands_are_bounded_without_leaking_budget_to_next_phase(case,monkeypatch):
    c=case;inventory=inv(c);row=c.m.plan(str(c.repo),'clone',inventory,{},{});row['bytes']=4096
    assert row['action']=='remove'
    monkeypatch.setattr(c.m,'process_anchors',lambda anchors:{});monkeypatch.setattr(c.m,'bb_environment_inventory',lambda:[])
    seen=[];raw=c.m._run
    def run(argv,**kw):seen.append((list(argv),kw['timeout']));return raw(argv,**kw)
    monkeypatch.setattr(c.m,'_run',run);c.m.DEADLINE=time.monotonic()+120
    c.m.execute([row],inventory,{},{},[str(c.root)],set(),'bounded',c.root/'events')
    assert row['result']=='removed' and seen and all(timeout<=10 for _,timeout in seen)
    c.m.command(['/bin/true'],timeout=60)
    assert seen[-1][1]==60
    assert row['execution_timings']['total']>0 and row['execution_subprocess_seconds']['ls_remote']>0


def test_final_recovery_records_the_live_effective_remote_url(case,monkeypatch):
    c=case;inventory=inv(c);row=c.m.plan(str(c.repo),'clone',inventory,{},{});row['bytes']=4096
    monkeypatch.setattr(c.m,'process_anchors',lambda anchors:{});monkeypatch.setattr(c.m,'bb_environment_inventory',lambda:[])
    original=c.m.remove
    def remove(item,*args,**kw):
        shell('git','remote','set-url','origin',str(c.bare),cwd=c.repo);age(c.repo)
        return original(item,*args,**kw)
    monkeypatch.setattr(c.m,'remove',remove)
    c.m.execute([row],inventory,{},{},[str(c.root)],set(),'live-url',c.root/'events')
    assert row['result']=='removed' and row['urls']==[str(c.bare)]


def test_partial_batched_push_keeps_clone_and_records_only_verified_refs(case,monkeypatch):
    c=case;(c.repo/'a').write_text('new');shell('git','commit','-qam','new',cwd=c.repo);age(c.repo)
    raw=c.m._run
    def partial(argv,**kw):
        if 'push' in argv:
            index=argv.index('push');raw(argv[:index+3],**kw)
            return SimpleNamespace(returncode=1,stdout='',stderr='one ref rejected')
        return raw(argv,**kw)
    monkeypatch.setattr(c.m,'_run',partial)
    row,_=execute_case(c,monkeypatch)
    assert row['result']=='kept' and c.repo.exists() and len(row['archives'])==1,row
    assert 'one ref rejected' in row['reason']
    assert shell('git','rev-parse',row['archives'][0]['ref'],cwd=c.bare)==row['archives'][0]['sha']


@pytest.mark.parametrize('fault',['discovery','references','usage'])
def test_incomplete_round_snapshot_never_enters_execution(case,monkeypatch,capsys,fault):
    c=case;inventory=inv(c)
    if fault=='discovery':inventory['errors'].append('inventory denied')
    monkeypatch.setattr(c.m,'managed_roots',lambda:set());monkeypatch.setattr(c.m,'discover',lambda *a:inventory)
    if fault=='references':monkeypatch.setattr(c.m,'references',lambda *a:(_ for _ in ()).throw(RuntimeError('reference denied')))
    monkeypatch.setattr(c.m,'usage',lambda *a,**kw:({},['unresolved global path'] if fault=='usage' else []))
    monkeypatch.setattr(c.m,'execute',lambda *a:(_ for _ in ()).throw(AssertionError('incomplete snapshot must not execute')))
    assert c.m.main(['--execute','--json','--state-dir',str(c.root/'state')])==1
    report=json.loads(capsys.readouterr().out)
    assert not report['complete'] and report['error'] and c.repo.exists()


@pytest.mark.parametrize('placement',['root','same-device-child','file','quoted-space'])
def test_live_mount_added_after_inventory_never_reaches_delete(case,monkeypatch,placement):
    c=case;payload=c.repo/'node_modules/payload';payload.mkdir(parents=True)
    mounted={'root':c.repo,'same-device-child':payload,'file':payload/'mounted-file','quoted-space':payload/'mounted folder'}[placement]
    if placement=='file':mounted.touch()
    elif placement=='quoted-space':mounted.mkdir()
    age(c.repo);inventory=inv(c);row=c.m.plan(str(c.repo),'clone',inventory,{},{});row['bytes']=4096
    assert row['action']=='remove'
    monkeypatch.setattr(c.m,'process_anchors',lambda anchors:{});monkeypatch.setattr(c.m,'bb_environment_inventory',lambda:[])
    actual_read=Path.read_text;original=c.m.remove;calls=[]
    device=c.repo.stat().st_dev
    text=f'999 1 {os.major(device)}:{os.minor(device)} / '+str(mounted).replace(' ','\\040')+' rw - ext4 /dev/same-device rw\n'
    def remove(item,*a,**kw):
        monkeypatch.setattr(Path,'read_text',lambda p,*args,**kwargs:text if str(p)=='/proc/self/mountinfo' else actual_read(p,*args,**kwargs))
        return original(item,*a,**kw)
    def never_delete(*a,**kw):calls.append(a);raise AssertionError('mounted data must stay')
    never_delete.avoids_symlink_attacks=True;monkeypatch.setattr(c.m,'remove',remove)
    with monkeypatch.context() as patch:
        patch.setattr(c.m.shutil,'rmtree',never_delete)
        c.m.execute([row],inventory,{},{},[str(c.root)],set(),'new-mount',c.root/'events')
    assert not calls and row.get('result')=='kept' and 'mount' in row.get('reason','').lower(),row
    assert c.repo.exists()


def test_du_in_progress_does_not_enter_round_process_protection(case,monkeypatch,capsys):
    import threading
    c=case;inventory=inv(c);du_started=threading.Event();usage_finished=threading.Event();raw_command=c.m.command;original_usage=c.m.usage
    monkeypatch.setattr(c.m,'managed_roots',lambda:set());monkeypatch.setattr(c.m,'discover',lambda *a:inventory)
    def process(anchors):
        if du_started.is_set() and not usage_finished.is_set():c.m.anchor(anchors,str(c.repo),'measurement du source')
        return c.m.ProcessEnvironments()
    def references(_):assert du_started.wait(3);return {}
    def usage(*a,**kw):
        try:return original_usage(*a,**kw)
        finally:usage_finished.set()
    def command(argv,**kw):
        if argv[0]=='du':
            du_started.set();assert usage_finished.wait(3);return SimpleNamespace(stdout='4096\t'+argv[-1],returncode=0)
        return raw_command(argv,**kw)
    monkeypatch.setattr(c.m,'process_anchors',process);monkeypatch.setattr(c.m,'references',references);monkeypatch.setattr(c.m,'usage',usage);monkeypatch.setattr(c.m,'command',command)
    monkeypatch.setattr(c.m,'bb_anchors',lambda *a:[]);monkeypatch.setattr(c.m,'config_anchors',lambda *a,**kw:[]);monkeypatch.setattr(c.m,'name_config_sources',lambda:[]);monkeypatch.setattr(c.m.shutil,'which',lambda _:None)
    assert c.m.main(['--json','--state-dir',str(c.root/'state')])==0
    report=json.loads(capsys.readouterr().out)
    assert report['complete'] and report['items'][0]['action']=='remove',report['items']


def test_deadline_crossed_by_final_stat_does_not_start_recursive_delete(case,monkeypatch):
    c=case;row=plan(c);clock={'now':9.9};calls=[];original_stat=c.m.os.stat
    def stat_call(*a,**kw):
        result=original_stat(*a,**kw)
        if 'dir_fd' in kw:clock['now']=10.1
        return result
    def never_delete(*a,**kw):calls.append(a)
    never_delete.avoids_symlink_attacks=True
    with monkeypatch.context() as patch:
        patch.setattr(c.m,'DEADLINE',10.0);patch.setattr(c.m.time,'monotonic',lambda:clock['now']);patch.setattr(c.m.os,'stat',stat_call)
        patch.setattr(c.m,'local_state',lambda *a:{key:row[key] for key in ('identity','objects','gitdir','main')});patch.setattr(c.m.shutil,'rmtree',never_delete)
        with pytest.raises(c.m.ws.ScanIncomplete,match='budget'):c.m.remove(row,inv(c),{},{})
    assert not calls and c.repo.exists()


def test_recursive_delete_already_started_is_timed_without_midway_cancellation(case,monkeypatch):
    c=case;inventory=inv(c);row=c.m.plan(str(c.repo),'clone',inventory,{},{});row['bytes']=4096
    clock={'now':0.0};original_delete=c.m.shutil.rmtree
    def delete(*a,**kw):clock['now']=11.0;return original_delete(*a,**kw)
    delete.avoids_symlink_attacks=original_delete.avoids_symlink_attacks
    with monkeypatch.context() as patch:
        patch.setattr(c.m.time,'monotonic',lambda:clock['now']);patch.setattr(c.m,'DEADLINE',100.0)
        patch.setattr(c.m,'bb_environment_inventory',lambda:[]);patch.setattr(c.m,'process_anchors',lambda anchors:{})
        patch.setattr(c.m.shutil,'rmtree',delete)
        c.m.execute([row],inventory,{},{},[str(c.root)],set(),'started',c.root/'events')
    assert row['result']=='removed' and not c.repo.exists()
    assert row['execution_timings']['delete']==11.0 and row['execution_timings']['total']==11.0


@pytest.mark.parametrize('separator',['\u00a0','\u2028','\u0085','\v','\f'])
def test_live_mount_path_keeps_non_ascii_and_non_newline_separators(case,monkeypatch,separator):
    c=case;renamed=c.root/('retired'+separator+'clone');c.repo.rename(renamed);c.repo=renamed
    payload=c.repo/'node_modules/payload';payload.mkdir(parents=True);age(c.repo)
    inventory=inv(c);row=c.m.plan(str(c.repo),'clone',inventory,{},{});row['bytes']=4096
    assert row['action']=='remove',row
    monkeypatch.setattr(c.m,'process_anchors',lambda anchors:{});monkeypatch.setattr(c.m,'bb_environment_inventory',lambda:[])
    actual_read=Path.read_text;original=c.m.remove;calls=[];device=c.repo.stat().st_dev
    text=f'999 1 {os.major(device)}:{os.minor(device)} / {payload} rw - ext4 /dev/same-device rw\n'
    def remove(item,*a,**kw):
        monkeypatch.setattr(Path,'read_text',lambda p,*args,**kwargs:text if str(p)=='/proc/self/mountinfo' else actual_read(p,*args,**kwargs))
        return original(item,*a,**kw)
    def never_delete(*a,**kw):calls.append(a);raise AssertionError('mounted data must stay')
    never_delete.avoids_symlink_attacks=True;monkeypatch.setattr(c.m,'remove',remove)
    with monkeypatch.context() as patch:
        patch.setattr(c.m.shutil,'rmtree',never_delete)
        c.m.execute([row],inventory,{},{},[str(c.root)],set(),'unicode-mount',c.root/'events')
    assert not calls and row.get('result')=='kept' and 'mounted path' in row.get('reason',''),row
    assert c.repo.exists()
