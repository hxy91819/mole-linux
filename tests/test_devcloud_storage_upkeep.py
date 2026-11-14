"""Behavioral regressions for the devcloud-storage-upkeep worktree sweep.

Run: python3 -m pytest tests/test_devcloud_storage_upkeep.py -q
Builds throwaway repos with a local bare remote; never touches /data/code or a real remote.
"""

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    'worktree_sweep',
    Path(__file__).resolve().parents[1] / '.claude/skills/devcloud-storage-upkeep/scripts/worktree_sweep.py')
assert SPEC and SPEC.loader
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)

OLD = time.time() - 3 * 86400


def sh(*args, cwd=None):
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class WorktreeSweepTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wt-sweep-test-', dir='/var/tmp')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.code, self.bare, self.state = root / 'code', root / 'remote.git', root / 'state'
        self.main = self.code / 'proj'
        self.code.mkdir()
        sh('git', 'init', '-q', '--bare', '-b', 'main', str(self.bare))
        sh('git', 'init', '-q', '-b', 'main', str(self.main))
        # core.excludesFile: keep the host's global ignores (which include .env) out of the test.
        for key, value in (('user.name', 'T'), ('user.email', 't@example.com'), ('core.excludesFile', '/dev/null')):
            sh('git', 'config', key, value, cwd=self.main)
        (self.main / '.gitignore').write_text('node_modules/\n')
        (self.main / 'a.txt').write_text('a\n')
        sh('git', 'add', '.', cwd=self.main)
        sh('git', 'commit', '-qm', 'init', cwd=self.main)
        sh('git', 'remote', 'add', 'origin', str(self.bare), cwd=self.main)
        sh('git', 'push', '-q', 'origin', 'main', cwd=self.main)
        sh('git', 'fetch', '-q', 'origin', cwd=self.main)
        sh('git', 'remote', 'set-head', 'origin', 'main', cwd=self.main)
        # Local remotes have no host; treat origin as the archive destination.
        patch = mock.patch.object(m.Repo, '_archive_destination', lambda _self, _logins: 'origin')
        patch.start()
        self.addCleanup(patch.stop)

    def add_worktree(self, name, *, commit=None, ignored=True):
        path = self.code / 'proj-worktrees' / name
        sh('git', 'worktree', 'add', '-q', '-b', name, str(path), 'main', cwd=self.main)
        if commit:
            (path / f'{name}.txt').write_text(commit)
            sh('git', 'add', '.', cwd=path)
            sh('git', 'commit', '-qm', commit, cwd=path)
        if ignored:
            (path / 'node_modules').mkdir()
            (path / 'node_modules' / 'dep.js').write_text('x')
        return path

    def age(self, path):
        gitdir = sh('git', 'rev-parse', '--path-format=absolute', '--git-dir', cwd=path)
        for target in (Path(gitdir) / 'HEAD', Path(gitdir) / 'index', Path(gitdir) / 'logs' / 'HEAD', path):
            if target.exists():
                os.utime(target, (OLD, OLD))

    def sweep(self, *extra):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = m.main(['--root', str(self.code), '--no-bb', '--state-dir', str(self.state), *extra])
        return rc, out.getvalue()

    def remote_refs(self):
        return sh('git', 'for-each-ref', '--format=%(refname) %(objectname)', cwd=self.bare)

    def test_dry_run_changes_nothing(self):
        merged = self.add_worktree('merged')
        self.age(merged)
        rc, out = self.sweep('--idle-hours', '1')
        self.assertEqual(rc, 0)
        self.assertIn('remove ×1', out)
        self.assertTrue(merged.exists())

    def test_merged_clean_worktree_is_removed_with_its_ignored_files(self):
        merged = self.add_worktree('merged')
        self.age(merged)
        rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertFalse(merged.exists())
        self.assertTrue(self.main.exists())
        self.assertNotIn('archive/worktree', self.remote_refs())

    def test_merged_worktree_is_proven_without_a_local_origin_head(self):
        # Repos pushed after a local `git init` have no refs/remotes/origin/HEAD.
        merged = self.add_worktree('old-main')
        (self.main / 'later.txt').write_text('later')
        sh('git', 'add', '.', cwd=self.main)
        sh('git', 'commit', '-qm', 'later', cwd=self.main)
        sh('git', 'push', '-q', 'origin', 'main', cwd=self.main)
        sh('git', 'remote', 'set-head', 'origin', '-d', cwd=self.main)
        sh('git', 'update-ref', '-d', 'refs/remotes/origin/main', cwd=self.main)
        self.age(merged)
        rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertFalse(merged.exists())
        self.assertNotIn('archive/worktree', self.remote_refs())

    def test_local_only_commit_is_pushed_to_archive_branch_before_removal(self):
        local = self.add_worktree('local', commit='only here')
        head = sh('git', 'rev-parse', 'HEAD', cwd=local)
        self.age(local)
        rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertFalse(local.exists())
        self.assertRegex(self.remote_refs(), rf'refs/heads/archive/worktree/local-\S+ {head}')

    def test_uncommitted_changes_are_archived_as_a_wip_commit(self):
        dirty = self.add_worktree('dirty')
        (dirty / 'a.txt').write_text('edited\n')
        (dirty / 'new.txt').write_text('untracked\n')
        rc, out = self.sweep('--idle-hours', '0', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertFalse(dirty.exists())
        ref = [line.split()[0] for line in self.remote_refs().splitlines() if 'archive/worktree/dirty' in line][0]
        self.assertEqual(sh('git', 'show', f'{ref}:a.txt', cwd=self.bare), 'edited')
        self.assertEqual(sh('git', 'show', f'{ref}:new.txt', cwd=self.bare), 'untracked')
        self.assertNotIn('node_modules', sh('git', 'ls-tree', '-r', '--name-only', ref, cwd=self.bare))

    def test_secret_like_untracked_file_keeps_the_worktree(self):
        dirty = self.add_worktree('withenv')
        (dirty / '.env').write_text('TOKEN=x\n')
        rc, out = self.sweep('--idle-hours', '0', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertTrue(dirty.exists())
        self.assertIn('keep: archive would push risky files ×1', out)
        self.assertNotIn('archive/worktree', self.remote_refs())

    def test_worktree_used_by_a_process_is_kept(self):
        busy = self.add_worktree('busy')
        self.age(busy)
        proc = subprocess.Popen(['sleep', '60'], cwd=busy)
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertTrue(busy.exists())
        self.assertIn('keep: in use ×1', out)

    def test_internal_archive_commits_with_the_global_identity(self):
        # git.woa.com rejects WIP commits whose committer is the repo-local GitHub identity.
        gitconfig = Path(self.temp.name) / 'global.gitconfig'
        gitconfig.write_text('[user]\n\tname = corp\n\temail = corp@corp.example\n')
        sh('git', 'config', 'user.email', 'personal@proton.example', cwd=self.main)
        for patch in (mock.patch.object(m, 'parse_remote', lambda _url: ('git.woa.com', 'team')),
                      mock.patch.object(m, 'ENV', dict(m.ENV, GIT_CONFIG_GLOBAL=str(gitconfig)))):
            patch.start()
            self.addCleanup(patch.stop)
        dirty = self.add_worktree('identity')
        (dirty / 'a.txt').write_text('edited\n')
        rc, out = self.sweep('--idle-hours', '0', '--execute')
        self.assertEqual(rc, 0, out)
        ref = [line.split()[0] for line in self.remote_refs().splitlines() if 'archive/worktree/identity' in line][0]
        self.assertEqual(sh('git', 'log', '-1', '--format=%cn <%ce>', ref, cwd=self.bare), 'corp <corp@corp.example>')

    def test_same_named_worktrees_get_distinct_archive_branches(self):
        first = self.add_worktree('one', commit='first')
        second = self.code / 'other' / 'one'
        sh('git', 'worktree', 'add', '-q', '-b', 'one-b', str(second), 'main', cwd=self.main)
        (second / 'b.txt').write_text('b')
        sh('git', 'add', '.', cwd=second)
        sh('git', 'commit', '-qm', 'second', cwd=second)
        for path in (first, second):
            self.age(path)
        rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertFalse(first.exists() or second.exists())
        self.assertEqual(self.remote_refs().count('refs/heads/archive/worktree/one-'), 2)

    def test_github_ssh_remotes_are_pushed_over_https(self):
        self.assertEqual(m.transport_url('git@github.com:hxy91819/bb.git'), 'https://github.com/hxy91819/bb.git')
        self.assertEqual(m.transport_url('ssh://git@github.com/o/r'), 'https://github.com/o/r.git')
        self.assertEqual(m.transport_url('git@git.woa.com:wisp/mono2.git'), 'git@git.woa.com:wisp/mono2.git')

    def test_committer_check_rejection_pushes_one_snapshot_and_keeps_exact_commits_locally(self):
        hook = self.bare / 'hooks' / 'pre-receive'
        hook.write_text('#!/bin/sh\nwhile read old new ref; do\n'
                        '  for c in $(git rev-list "$new" --not --all); do\n'
                        '    [ "$(git log -1 --format=%ce "$c")" = corp@corp.example ] || '
                        '{ echo "Rejected by committer-check" >&2; exit 1; }\n'
                        '  done\ndone\n')
        hook.chmod(0o755)
        gitconfig = Path(self.temp.name) / 'global.gitconfig'
        gitconfig.write_text('[user]\n\tname = corp\n\temail = corp@corp.example\n')
        for patch in (mock.patch.object(m, 'parse_remote', lambda _url: ('git.woa.com', 'team')),
                      mock.patch.object(m, 'ENV', dict(m.ENV, GIT_CONFIG_GLOBAL=str(gitconfig)))):
            patch.start()
            self.addCleanup(patch.stop)
        sh('git', 'config', 'user.email', 'personal@proton.example', cwd=self.main)
        local = self.add_worktree('personal', commit='made with the personal identity')
        head = sh('git', 'rev-parse', 'HEAD', cwd=local)
        self.age(local)
        rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertFalse(local.exists())
        ref = [line.split()[0] for line in self.remote_refs().splitlines() if 'archive/worktree/personal' in line][0]
        self.assertEqual(sh('git', 'log', '-1', '--format=%ce', ref, cwd=self.bare), 'corp@corp.example')
        self.assertEqual(sh('git', 'rev-parse', f'{ref}^{{tree}}', cwd=self.bare),
                         sh('git', 'rev-parse', f'{head}^{{tree}}', cwd=self.main))
        self.assertEqual(sh('git', 'rev-parse', ref, cwd=self.main), head)

    def test_push_rejection_reason_survives_server_noise(self):
        stderr = ('remote: \nremote: Processing pre-receive: ref-rules: 1        \n'
                  'remote: \x1b[31m[push-check] commit message must start with a type\x1b[0m\n'
                  'remote: +-------+\nremote: | To create a merge request |\n'
                  ' ! [remote rejected] abc -> archive/worktree/x (pre-receive hook declined)\n')
        reason = m.failure_text(subprocess.CalledProcessError(1, ['git', 'push'], '', stderr))
        self.assertIn('commit message must start with a type', reason)
        self.assertIn('pre-receive hook declined', reason)
        self.assertNotIn('Processing pre-receive', reason)
        self.assertNotIn('\x1b', reason)

    def test_recent_activity_keeps_the_worktree(self):
        fresh = self.add_worktree('fresh')
        rc, out = self.sweep('--execute')
        self.assertEqual(rc, 0, out)
        self.assertTrue(fresh.exists())
        self.assertIn('keep: active within 24h ×1', out)


    def test_opencraft_app_is_a_personal_login(self):
        self.assertIn('opencraft-app', m.DEFAULT_LOGINS)

    def test_deleted_tracked_npmrc_does_not_block_archive(self):
        path = self.add_worktree('npmrc')
        (path / '.npmrc').write_text('registry=https://example.invalid\n')
        sh('git', 'add', '.npmrc', cwd=path)
        sh('git', 'commit', '-qm', 'track npmrc', cwd=path)
        (path / '.npmrc').unlink()
        self.age(path)
        rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertFalse(path.exists(), out)
        self.assertIn('archive/worktree', self.remote_refs())

    def test_untracked_npmrc_still_blocks_archive(self):
        path = self.add_worktree('npmrcnew')
        (path / '.npmrc').write_text('registry=https://example.invalid\n')
        self.age(path)
        rc, out = self.sweep('--idle-hours', '0', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertTrue(path.exists(), out)
        self.assertIn('risky files', out)
        self.assertNotIn('archive/worktree', self.remote_refs())

    def test_recent_head_mtime_keeps_without_status(self):
        path = self.add_worktree('freshhead')
        self.age(path)
        gitdir = Path(sh('git', 'rev-parse', '--path-format=absolute', '--git-dir', cwd=path))
        now = time.time()
        os.utime(gitdir / 'HEAD', (now, now))

        def status_paths(_path):
            raise AssertionError('status should not run')

        with mock.patch.object(m, 'status_paths', side_effect=status_paths):
            rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertTrue(path.exists(), out)
        self.assertIn('keep: active within 1h ×1', out)

    def test_time_budget_keeps_the_directory_and_records_the_reason(self):
        path = self.add_worktree('budget')
        self.age(path)
        real = m.time.time
        state = {'start': None}

        def fake():
            now = real()
            if state['start'] is None:
                state['start'] = now
                return now
            return state['start'] + 3600

        with mock.patch.object(m.time, 'time', side_effect=fake):
            rc, out = self.sweep('--idle-hours', '1', '--budget-minutes', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertTrue(path.exists(), out)
        self.assertIn('time budget reached', out)
        logged = list(Path(self.state).glob('worktree-sweep-*.jsonl'))
        self.assertTrue(logged)
        self.assertIn('"result": "kept"', logged[0].read_text())
        self.assertIn('time budget reached', logged[0].read_text())

    def _quiet(self, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = m.main(['--root', str(self.code), '--no-bb', '--state-dir', str(self.state), '--quiet', *extra])
        return rc, out.getvalue(), err.getvalue()

    def test_quiet_is_silent_when_nothing_is_removed(self):
        path = self.add_worktree('quiet')
        rc, out, err = self._quiet()
        self.assertEqual(rc, 0)
        self.assertEqual(out, '')
        self.assertEqual(err, '')
        self.assertTrue(path.exists())

    def test_quiet_reports_a_removal_and_the_log(self):
        path = self.add_worktree('qremove')
        self.age(path)
        rc, out, err = self._quiet('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(path.exists())
        self.assertIn('removed ×1', out)
        self.assertIn('log:', out)
        self.assertEqual(err, '')

    def _ignore(self, line):
        gitignore = self.main / '.gitignore'
        gitignore.write_text(gitignore.read_text() + line)
        sh('git', 'add', '.gitignore', cwd=self.main)
        sh('git', 'commit', '-qm', 'ignore', cwd=self.main)

    def test_ignored_env_missing_from_main_keeps_the_worktree(self):
        self._ignore('.env\n')
        path = self.add_worktree('withignored')
        (path / '.env').write_text('TOKEN=only-here\n')
        self.age(path)
        rc, out = self.sweep('--idle-hours', '1', '--execute', '--json')
        self.assertEqual(rc, 0, out)
        self.assertTrue(path.exists(), out)
        self.assertIn('ignored secret-like file not in main checkout: .env', out)
        self.assertNotIn('archive/worktree', self.remote_refs())

    def test_ignored_env_with_different_contents_keeps_the_worktree(self):
        self._ignore('.env\n')
        (self.main / '.env').write_text('TOKEN=main\n')
        path = self.add_worktree('diffenv')
        (path / '.env').write_text('TOKEN=other\n')
        self.age(path)
        rc, out = self.sweep('--idle-hours', '1', '--execute', '--json')
        self.assertEqual(rc, 0, out)
        self.assertTrue(path.exists(), out)
        self.assertIn('ignored secret-like file not in main checkout: .env', out)

    def test_ignored_env_identical_to_main_is_removed(self):
        self._ignore('.env\n')
        (self.main / '.env').write_text('TOKEN=same\n')
        path = self.add_worktree('sameenv')
        (path / '.env').write_text('TOKEN=same\n')
        self.age(path)
        rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertFalse(path.exists(), out)

    def test_secret_inside_ignored_directory_not_in_main_keeps_the_worktree(self):
        self._ignore('secret-dir/\n')
        path = self.add_worktree('secretdir')
        directory = path / 'secret-dir'
        directory.mkdir()
        (directory / 'credentials.json').write_text('{"a": 1}\n')
        self.age(path)
        rc, out = self.sweep('--idle-hours', '1', '--execute', '--json')
        self.assertEqual(rc, 0, out)
        self.assertTrue(path.exists(), out)
        self.assertIn('ignored secret-like file not in main checkout: secret-dir/credentials.json', out)

    def test_secrets_inside_node_modules_or_venvs_do_not_block(self):
        self._ignore('venv/\n')
        path = self.add_worktree('deps')
        (path / 'node_modules' / '.npmrc').write_text('token=x\n')
        venv = path / 'venv'
        venv.mkdir()
        (venv / 'pyvenv.cfg').write_text('home = /usr\n')
        (venv / '.env').write_text('TOKEN=venv\n')
        self.age(path)
        rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertFalse(path.exists(), out)

    def test_process_scan_permission_error_keeps_all_candidates(self):
        path = self.add_worktree('scan')
        self.age(path)
        real = os.readlink

        def readlink(target):
            if str(target).startswith('/proc/') and str(target).endswith('/cwd'):
                raise PermissionError(13, 'Permission denied', target)
            return real(target)

        with mock.patch.object(m.os, 'readlink', side_effect=readlink):
            rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertTrue(path.exists(), out)
        self.assertIn('process scan incomplete', out)

    def test_process_maps_permission_error_keeps_all_candidates(self):
        path = self.add_worktree('maps')
        self.age(path)
        real_open = open

        def fake_open(file, *args, **kwargs):
            if str(file).startswith('/proc/') and str(file).endswith('/maps'):
                raise PermissionError(13, 'Permission denied', file)
            return real_open(file, *args, **kwargs)

        with mock.patch('builtins.open', side_effect=fake_open):
            rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertTrue(path.exists(), out)
        self.assertIn('process scan incomplete', out)

    def test_process_fd_permission_error_keeps_all_candidates(self):
        path = self.add_worktree('fds')
        self.age(path)
        real = os.listdir

        def listdir(target):
            if str(target).startswith('/proc/') and str(target).endswith('/fd'):
                raise PermissionError(13, 'Permission denied', target)
            return real(target)

        with mock.patch.object(m.os, 'listdir', side_effect=listdir):
            rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertTrue(path.exists(), out)
        self.assertIn('process scan incomplete', out)

    def test_missing_proc_entry_still_removes_an_idle_worktree(self):
        path = self.add_worktree('goneproc')
        self.age(path)
        real = os.readlink

        def readlink(target):
            if str(target).startswith('/proc/') and str(target).endswith('/exe'):
                raise FileNotFoundError(2, 'No such file', target)
            return real(target)

        with mock.patch.object(m.os, 'readlink', side_effect=readlink):
            rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertFalse(path.exists(), out)

    def _docker_failure(self, failing):
        path = self.add_worktree(failing)
        self.age(path)
        real_run, real_which = m.run, m.shutil.which

        def which(cmd):
            if cmd == 'docker':
                return '/usr/bin/docker'
            return real_which(cmd)

        def run(cmd, *args, **kwargs):
            check = kwargs.get('check', True)
            if cmd[:2] == ['docker', 'ps']:
                if failing == 'ps':
                    if check:
                        raise subprocess.CalledProcessError(1, cmd, '', 'down')
                    return subprocess.CompletedProcess(cmd, 1, '', 'down')
                return subprocess.CompletedProcess(cmd, 0, 'cid\n', '')
            if cmd[:2] == ['docker', 'inspect']:
                if check:
                    raise subprocess.CalledProcessError(1, cmd, '', 'down')
                return subprocess.CompletedProcess(cmd, 1, '', 'down')
            return real_run(cmd, *args, **kwargs)

        with mock.patch.object(m.shutil, 'which', side_effect=which), mock.patch.object(m, 'run', side_effect=run):
            rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertTrue(path.exists(), out)
        self.assertIn('process scan incomplete', out)

    def test_docker_ps_failure_keeps_all_candidates(self):
        self._docker_failure('ps')

    def test_docker_inspect_failure_keeps_all_candidates(self):
        self._docker_failure('inspect')

    def test_unreachable_archive_destination_is_kept_without_push(self):
        local = self.add_worktree('gone-remote', commit='only here')
        self.age(local)
        sh('git', 'remote', 'set-url', 'origin', str(Path(self.temp.name) / 'missing.git'), cwd=self.main)
        rc, out = self.sweep('--idle-hours', '1', '--execute', '--json')
        self.assertEqual(rc, 0, out)
        self.assertTrue(local.exists(), out)
        self.assertIn('archive destination unreachable: origin', out)
        self.assertNotIn('archive/worktree', self.remote_refs())

    def test_push_rejection_still_fails_the_run(self):
        hook = self.bare / 'hooks' / 'pre-receive'
        hook.write_text('#!/bin/sh\necho rejected on purpose >&2\nexit 1\n')
        hook.chmod(0o755)
        local = self.add_worktree('rejected', commit='only here')
        self.age(local)
        rc, out = self.sweep('--idle-hours', '1', '--execute')
        self.assertEqual(rc, 1, out)
        self.assertTrue(local.exists(), out)
        self.assertIn('failed', out)

    def test_measure_reports_total_size_for_a_repo_with_only_kept_worktrees(self):
        kept = self.add_worktree('kept')
        (kept / 'blob.bin').write_bytes(b'\0' * (100 * 1024 * 1024))
        rc, out = self.sweep('--idle-hours', '1', '--measure')
        self.assertEqual(rc, 0, out)
        line = next(row for row in out.splitlines() if row.startswith('proj ['))
        seg = line.split('[', 1)[1].split(']', 1)[0]
        self.assertIn('GiB to free', seg)
        self.assertGreater(float(seg.split(' GiB, ', 1)[0]), 0.0, out)
        self.assertNotIn('unmeasured', seg)
        self.assertTrue(kept.exists(), out)


def _load_docker_leftovers():
    spec = importlib.util.spec_from_file_location(
        'docker_leftovers',
        Path(__file__).resolve().parents[1] / '.claude/skills/devcloud-storage-upkeep/scripts/docker_leftovers.py')
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


leftovers = _load_docker_leftovers()


class DockerLeftoversTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='cache-caps-test-', dir='/var/tmp')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.lock = root / 'run.lock'
        self.lock.touch()
        self.retained = root / 'modes.json'
        self.retained.write_text('{"retained_volumes": []}\n')
        for name, value in (('MAINTENANCE_LOCK', str(self.lock)), ('RETAINED_VOLUMES_PATH', str(self.retained))):
            patch = mock.patch.object(leftovers, name, value, create=True)
            patch.start()
            self.addCleanup(patch.stop)
        which = mock.patch.object(leftovers.shutil, 'which', return_value=None)
        which.start()
        self.addCleanup(which.stop)

    def invoke(self, args, run=None, which=None):
        out, err = io.StringIO(), io.StringIO()
        patches = []
        if run is not None:
            patches.append(mock.patch.object(leftovers, 'run', side_effect=run))
        if which is not None:
            patches.append(mock.patch.object(leftovers.shutil, 'which', side_effect=which))
        for patch in patches:
            patch.start()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = leftovers.main(args)
        finally:
            for patch in patches:
                patch.stop()
        return rc, out.getvalue(), err.getvalue()

    def test_quiet_is_silent_when_nothing_is_configured(self):
        rc, out, err = self.invoke(['--quiet'])
        self.assertEqual(rc, 0)
        self.assertEqual(out, '')
        self.assertEqual(err, '')

    def _mount(self, name):
        mount = Path(self.temp.name) / name / '_data'
        mount.mkdir(parents=True)
        (mount / 'auto.cnf').write_text('x')
        return mount

    def _volume(self, name, mount):
        return {
            'Name': name,
            'Labels': {'com.docker.volume.anonymous': ''},
            'Mountpoint': str(mount),
            'Driver': 'local',
            'CreatedAt': '2020-01-01T00:00:00Z',
        }

    def test_docker_volume_ls_failure_does_not_delete_and_exits_1(self):
        commands = []

        def run(cmd, timeout=1800, check=False):
            commands.append(cmd)
            if cmd[:3] == ['docker', 'image', 'ls']:
                return subprocess.CompletedProcess(cmd, 0, '', '')
            if cmd == ['docker', 'builder', 'prune', '--help']:
                return subprocess.CompletedProcess(cmd, 0, '--max-used-space bytes', '')
            if cmd[:3] == ['docker', 'volume', 'ls']:
                return subprocess.CompletedProcess(cmd, 1, '', 'denied')
            if cmd[:2] == ['docker', 'image'] or cmd[:2] == ['docker', 'builder']:
                return subprocess.CompletedProcess(cmd, 0, stdout='Total reclaimed space: 0B\n', stderr='')
            raise AssertionError(cmd)

        rc, out, err = self.invoke(['--execute'], run=run, which=lambda name: '/usr/bin/docker' if name == 'docker' else None)
        self.assertEqual(rc, 1, out + err)
        self.assertIn('measure failed', out)
        self.assertFalse(any(cmd[:3] == ['docker', 'volume', 'rm'] for cmd in commands), commands)

    def test_retained_anonymous_volume_is_not_removed(self):
        kept = 'a' * 64
        other = 'b' * 64
        self.retained.write_text('{"retained_volumes": [{"name": "%s"}]}\n' % kept)
        mounts = {kept: self._mount('kept'), other: self._mount('other')}
        commands = []

        def run(cmd, timeout=1800, check=False):
            commands.append(cmd)
            if cmd[:3] == ['docker', 'image', 'ls']:
                return subprocess.CompletedProcess(cmd, 0, '', '')
            if cmd == ['docker', 'builder', 'prune', '--help']:
                return subprocess.CompletedProcess(cmd, 0, '--max-used-space bytes', '')
            if cmd[:3] == ['docker', 'volume', 'ls']:
                return subprocess.CompletedProcess(cmd, 0, stdout=kept + '\n' + other + '\n', stderr='')
            if cmd[:3] == ['docker', 'volume', 'inspect']:
                payload = [self._volume(item, mounts[item]) for item in (kept, other)]
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr='')
            if cmd[0] == 'du':
                return subprocess.CompletedProcess(cmd, 0, stdout='100\n', stderr='')
            if cmd[:3] == ['docker', 'volume', 'rm']:
                return subprocess.CompletedProcess(cmd, 0, stdout='\n'.join(cmd[3:]) + '\n', stderr='')
            if cmd[:2] == ['docker', 'image'] or cmd[:2] == ['docker', 'builder']:
                return subprocess.CompletedProcess(cmd, 0, stdout='Total reclaimed space: 0B\n', stderr='')
            raise AssertionError(cmd)

        rc, out, err = self.invoke(['--execute'], run=run, which=lambda name: '/usr/bin/docker' if name == 'docker' else None)
        self.assertEqual(rc, 0, out + err)
        removed = [cmd for cmd in commands if cmd[:3] == ['docker', 'volume', 'rm']]
        self.assertTrue(removed, commands)
        self.assertNotIn(kept, removed[0])
        self.assertIn(other, removed[0])

    def test_unreadable_retained_file_deletes_no_volumes(self):
        self.retained.write_text('{not json')
        name = 'c' * 64
        mount = self._mount('bad')
        commands = []

        def run(cmd, timeout=1800, check=False):
            commands.append(cmd)
            if cmd[:3] == ['docker', 'image', 'ls']:
                return subprocess.CompletedProcess(cmd, 0, '', '')
            if cmd == ['docker', 'builder', 'prune', '--help']:
                return subprocess.CompletedProcess(cmd, 0, '--max-used-space bytes', '')
            if cmd[:3] == ['docker', 'volume', 'ls']:
                return subprocess.CompletedProcess(cmd, 0, stdout=name + '\n', stderr='')
            if cmd[:3] == ['docker', 'volume', 'inspect']:
                return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps([self._volume(name, mount)]), stderr='')
            if cmd[0] == 'du':
                return subprocess.CompletedProcess(cmd, 0, stdout='100\n', stderr='')
            if cmd[:3] == ['docker', 'volume', 'rm']:
                raise AssertionError('volume removed')
            if cmd[:2] == ['docker', 'image'] or cmd[:2] == ['docker', 'builder']:
                return subprocess.CompletedProcess(cmd, 0, stdout='Total reclaimed space: 0B\n', stderr='')
            raise AssertionError(cmd)

        rc, out, err = self.invoke(['--execute'], run=run, which=lambda name: '/usr/bin/docker' if name == 'docker' else None)
        self.assertEqual(rc, 1, out + err)
        self.assertIn('retained volumes unreadable', out)
        self.assertFalse(any(cmd[:3] == ['docker', 'volume', 'rm'] for cmd in commands))

    def test_busy_maintenance_lock_skips_docker(self):
        held = open(self.lock, 'a')
        self.addCleanup(held.close)
        import fcntl
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        commands = []

        def run(cmd, timeout=1800, check=False):
            commands.append(cmd)
            if cmd[:3] == ['docker', 'image', 'ls']:
                return subprocess.CompletedProcess(cmd, 0, '', '')
            if cmd == ['docker', 'builder', 'prune', '--help']:
                return subprocess.CompletedProcess(cmd, 0, '--max-used-space bytes', '')
            raise AssertionError(cmd)

        rc, out, err = self.invoke(
            ['--execute', '--quiet'], run=run, which=lambda name: '/usr/bin/docker' if name == 'docker' else None)
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out, '')
        self.assertEqual(err, '')
        self.assertEqual(commands, [])

    def test_quiet_reports_a_real_prune_and_omits_zero_reclaim(self):
        commands = []

        def run(cmd, timeout=1800, check=False):
            commands.append(cmd)
            if cmd[:3] == ['docker', 'image', 'ls']:
                return subprocess.CompletedProcess(cmd, 0, '', '')
            if cmd == ['docker', 'builder', 'prune', '--help']:
                return subprocess.CompletedProcess(cmd, 0, '--max-used-space bytes', '')
            if cmd[:3] == ['docker', 'volume', 'ls']:
                return subprocess.CompletedProcess(cmd, 0, '', '')
            if cmd[:3] == ['docker', 'image', 'prune']:
                return subprocess.CompletedProcess(cmd, 0, stdout='Total reclaimed space: 0B\n', stderr='')
            if cmd[:3] == ['docker', 'builder', 'prune']:
                return subprocess.CompletedProcess(cmd, 0, stdout='Total reclaimed space: 2.5GB\n', stderr='')
            raise AssertionError(cmd)

        rc, out, err = self.invoke(
            ['--execute', '--quiet'], run=run, which=lambda name: '/usr/bin/docker' if name == 'docker' else None)
        self.assertEqual(rc, 0, out + err)
        self.assertNotIn('0B', out)
        self.assertIn('docker BuildKit cache older than 72h: reclaimed 2.5GB rc=0', out)
        self.assertNotIn('dangling', out)
        self.assertEqual(err, '')


class TaggedImagePolicyTests(unittest.TestCase):
    setUp = DockerLeftoversTests.setUp
    invoke = DockerLeftoversTests.invoke

    def policy(self, *, referenced=False, latest=False, recent=False, labels=None,
               query_failure=False, final_reference=False, retag=False, protect_final=False,
               execute=True, help_text='--max-used-space bytes', rm_failure=False, old_buildkit=False,
               prune_failure=False, final_query_failure=False, fallback_failure=False,
               multiple=False, final_inspect_failure=False):
        from datetime import datetime, timezone
        old = {'Id': 'sha256:old', 'RepoTags': ['registry:5000/team/app:old'],
               'Created': '2020-01-01T00:00:00Z', 'Size': 1234, 'Config': {'Labels': labels or {}}}
        newer = {'Id': 'sha256:new', 'RepoTags': ['registry:5000/team/app:new'],
                 'Created': '2026-10-01T00:00:00Z', 'Size': 5678, 'Config': {'Labels': {}}}
        if recent:
            old['Created'] = datetime.now(timezone.utc).isoformat()
            newer['Created'] = '2099-01-01T00:00:00Z'
        middle = {'Id': 'sha256:middle', 'RepoTags': ['registry:5000/team/app:middle'],
                  'Created': '2021-01-01T00:00:00Z', 'Size': 2345, 'Config': {'Labels': {}}}
        images = [middle, old, newer] if multiple else ([old] if latest else [old, newer])
        commands = []
        queries = 0

        def run(cmd, timeout=1800, check=False):
            nonlocal queries
            commands.append(cmd)
            data, rc = '', 0
            if cmd[:3] == ['docker', 'volume', 'ls']:
                pass
            elif cmd[:3] == ['docker', 'image', 'ls']:
                data = '\n'.join(i['Id'] for i in images)
            elif cmd[:3] == ['docker', 'image', 'inspect']:
                if final_inspect_failure and 'registry:5000/team/app:old' in cmd:
                    return subprocess.CompletedProcess(cmd, 1, '', 'inspect denied')
                found = [dict(i) for i in images if i['Id'] in cmd[3:] or set(i['RepoTags']) & set(cmd[3:])]
                if 'registry:5000/team/app:old' in cmd:
                    if retag:
                        found[0]['Id'] = 'sha256:replacement'
                    if protect_final:
                        found[0]['Config'] = {'Labels': {'mole.protect': 'true'}}
                data = json.dumps(found)
            elif cmd[:3] == ['docker', 'container', 'ls']:
                queries += 1
                rc = 1 if query_failure == 'ls' or (final_query_failure and queries > 1) else 0
                data = 'container' if referenced or query_failure == 'inspect' or (final_reference and queries > 1) else ''
            elif cmd[:3] == ['docker', 'container', 'inspect']:
                rc = 1 if query_failure == 'inspect' else 0
                data = json.dumps([{'Image': 'sha256:old'}])
            elif cmd[:3] == ['docker', 'image', 'rm']:
                rc = int(rm_failure)
                data = 'Untagged: ' + cmd[3]
                if not rc:
                    for image in images:
                        image['RepoTags'] = [t for t in image['RepoTags'] if t != cmd[3]]
            elif cmd[:3] == ['docker', 'builder', 'prune'] and '--help' in cmd:
                data = help_text
            elif cmd[:3] in (['docker', 'builder', 'prune'], ['docker', 'image', 'prune']):
                data = 'Total reclaimed space: 0B'
                if fallback_failure and '--keep-storage' in cmd:
                    return subprocess.CompletedProcess(cmd, 1, '', 'fallback denied')
                if prune_failure:
                    return subprocess.CompletedProcess(cmd, 1, '', 'permission denied')
                if old_buildkit and '--max-used-space' in cmd:
                    return subprocess.CompletedProcess(cmd, 1, '', 'ERROR: buildkit v0.17.0+ is required for max-used-space and min-free-space filters')
            else:
                raise AssertionError(cmd)
            return subprocess.CompletedProcess(cmd, rc, data, 'denied' if rc else '')

        rc, out, err = self.invoke(['--execute'] if execute else [], run=run,
                                   which=lambda name: '/usr/bin/docker' if name == 'docker' else None)
        return rc, out, [c for c in commands if c[:3] == ['docker', 'image', 'rm']], commands

    def assert_kept(self, reason, **kwargs):
        rc, out, removed, _ = self.policy(**kwargs)
        self.assertEqual(removed, [], out)
        self.assertIn(reason, out)
        return rc

    def test_tagged_referenced_including_stopped_and_created_kept(self):
        self.assert_kept('container reference', referenced=True)

    def test_tagged_repository_latest_kept(self):
        self.assert_kept('repository latest', latest=True)

    def test_tagged_under_30_days_kept(self):
        self.assert_kept('not older than 30 days', recent=True)

    def test_tagged_owner_protection_kept(self):
        for key in ('mole.protect', 'devcloud.protect'):
            with self.subTest(key=key):
                self.assert_kept('owner protection', labels={key: 'true'})

    def test_tagged_reference_query_failure_keeps_and_fails(self):
        for probe in ('ls', 'inspect'):
            with self.subTest(probe=probe):
                self.assertEqual(self.assert_kept('reference query failed', query_failure=probe), 1)

    def test_tagged_only_eligible_tag_removed_without_force(self):
        rc, out, removed, _ = self.policy()
        self.assertEqual(rc, 0, out)
        self.assertEqual(removed, [['docker', 'image', 'rm', 'registry:5000/team/app:old']])
        self.assertTrue(all('-aq' in c for c in _ if c[:3] == ['docker', 'container', 'ls']))

    def test_tagged_final_reference_rechecked(self):
        self.assert_kept('container reference', final_reference=True)

    def test_tagged_final_tag_pointer_rechecked(self):
        self.assert_kept('tag changed', retag=True)

    def test_tagged_final_owner_label_rechecked(self):
        self.assert_kept('owner protection', protect_final=True)

    def test_tagged_dry_run_does_not_remove(self):
        rc, out, removed, commands = self.policy(execute=False)
        self.assertEqual(rc, 0, out)
        self.assertEqual(removed, [])
        self.assertFalse(any(c[:3] == ['docker', 'builder', 'prune'] and '--help' not in c for c in commands))

    def test_tagged_remove_failure_exits_1(self):
        self.assertEqual(self.policy(rm_failure=True)[0], 1)

    def test_builder_cap_is_separate_from_age_prune(self):
        for flag in ('--max-used-space', '--keep-storage'):
            with self.subTest(flag=flag):
                rc, out, _, commands = self.policy(help_text=flag + ' bytes')
                self.assertEqual(rc, 0, out)
                prunes = [c for c in commands if c[:3] == ['docker', 'builder', 'prune'] and '--help' not in c]
                self.assertIn(['docker', 'builder', 'prune', '-f', flag, '10GB'], prunes)
                self.assertIn(['docker', 'builder', 'prune', '-f', '--filter', 'until=72h'], prunes)

    def test_builder_old_server_uses_supported_keep_storage(self):
        rc, out, _, commands = self.policy(old_buildkit=True)
        self.assertEqual(rc, 0, out)
        self.assertIn(['docker', 'builder', 'prune', '-f', '--keep-storage', '10GB'], commands)

    def test_builder_real_failure_does_not_fall_back_and_exits_1(self):
        rc, out, _, commands = self.policy(prune_failure=True)
        self.assertEqual(rc, 1, out)
        self.assertIn('permission denied', out)
        self.assertNotIn(['docker', 'builder', 'prune', '-f', '--keep-storage', '10GB'], commands)

    def test_tagged_multiple_old_tags_use_newest_witness_in_any_order(self):
        rc, out, removed, _ = self.policy(multiple=True)
        self.assertEqual(rc, 0, out)
        self.assertEqual([c[3] for c in removed], ['registry:5000/team/app:middle', 'registry:5000/team/app:old'])

    def test_tagged_final_image_query_failure_is_reported_per_tag(self):
        rc, out, removed, _ = self.policy(final_inspect_failure=True)
        self.assertEqual(rc, 1, out)
        self.assertEqual(removed, [])
        self.assertIn('keep: final image query failed', out)
        self.assertIn('keep: repository latest', out)

    def test_tagged_final_reference_query_failure_keeps_and_exits_1(self):
        self.assertEqual(self.assert_kept('reference query failed', final_query_failure=True), 1)

    def test_builder_fallback_failure_exits_1_and_reports_error(self):
        rc, out, _, commands = self.policy(old_buildkit=True, fallback_failure=True)
        self.assertEqual(rc, 1, out)
        self.assertIn('fallback denied', out)
        self.assertIn(['docker', 'builder', 'prune', '-f', '--keep-storage', '10GB'], commands)

    def test_builder_unsupported_cap_exits_1(self):
        self.assertEqual(self.policy(help_text='unknown')[0], 1)


def _load_go_cache_trim():
    spec = importlib.util.spec_from_file_location(
        'go_cache_trim',
        Path(__file__).resolve().parents[1] / '.claude/skills/devcloud-storage-upkeep/scripts/go_cache_trim.py')
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


trim = _load_go_cache_trim()
README_LINE = 'This directory holds cached build artifacts from the Go build system.\n'


class GoCacheTrimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='go-cache-trim-', dir='/var/tmp')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.cache = root / 'gocache'
        self.cache.mkdir()
        (self.cache / 'README').write_text(README_LINE + 'Do not delete.\n')
        bindir = root / 'bin'
        bindir.mkdir()
        go = bindir / 'go'
        go.write_text('#!/bin/sh\nif [ "$1" = env ] && [ "$2" = GOCACHE ]; then\n'
                      '  printf "%s\\n" "$GO_CACHE_TRIM_DIR"\n  exit 0\nfi\nexit 1\n')
        go.chmod(0o755)
        self._env = mock.patch.dict(os.environ, {
            'PATH': str(bindir) + os.pathsep + os.environ.get('PATH', ''),
            'GO_CACHE_TRIM_DIR': str(self.cache),
        })
        self._env.start()
        self.addCleanup(self._env.stop)

    def entry(self, bucket, suffix, age_hours, body=b'payload'):
        directory = self.cache / f'{bucket:02x}'
        directory.mkdir(exist_ok=True)
        path = directory / (('ab' * 32) + '-' + suffix)
        path.write_bytes(body)
        when = time.time() - age_hours * 3600
        os.utime(path, (when, when))
        return path

    def run_trim(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = trim.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_old_go_cache_entries_are_deleted(self):
        old_a = self.entry(1, 'a', 3, b'aaa')
        old_d = self.entry(2, 'd', 5, b'dddd')
        kept_root = self.cache / (('cd' * 32) + '-a')
        kept_root.write_bytes(b'root')
        os.utime(kept_root, (time.time() - 5 * 3600, time.time() - 5 * 3600))
        other = self.cache / 'not-a-bucket'
        other.mkdir()
        kept_other = other / (('ef' * 32) + '-a')
        kept_other.write_bytes(b'other')
        os.utime(kept_other, (time.time() - 5 * 3600, time.time() - 5 * 3600))
        rc, out, err = self.run_trim('--execute')
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(old_a.exists())
        self.assertFalse(old_d.exists())
        self.assertEqual(kept_root.read_bytes(), b'root')
        self.assertEqual(kept_other.read_bytes(), b'other')
        self.assertIn('deleted 2', out)

    def test_recent_go_cache_entries_are_kept(self):
        recent = self.entry(3, 'a', 0.5, b'new')
        rc, out, err = self.run_trim('--execute')
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(recent.read_bytes(), b'new')

    def test_non_matching_go_cache_names_are_kept(self):
        directory = self.cache / '0a'
        directory.mkdir()
        when = time.time() - 5 * 3600
        kept = {}
        for name, body in (
            ('notes.txt', b'notes'),
            (('a' * 63) + '-a', b'short'),
            (('a' * 64) + '-b', b'wrong-suffix'),
            ('trim.txt', b'trim'),
            ('testexpire.txt', b'expire'),
        ):
            path = directory / name
            path.write_bytes(body)
            os.utime(path, (when, when))
            kept[path] = body
        fuzz = self.cache / 'fuzz'
        fuzz.mkdir()
        fuzz_file = fuzz / 'case'
        fuzz_file.write_bytes(b'fuzz')
        os.utime(fuzz_file, (when, when))
        kept[fuzz_file] = b'fuzz'
        rc, out, err = self.run_trim('--execute')
        self.assertEqual(rc, 0, out + err)
        for path, body in kept.items():
            self.assertEqual(path.read_bytes(), body, path)

    def test_go_cache_symlink_is_kept_and_target_is_untouched(self):
        target = Path(self.temp.name) / 'outside'
        target.write_bytes(b'target-bytes')
        directory = self.cache / '0b'
        directory.mkdir()
        link = directory / (('12' * 32) + '-a')
        link.symlink_to(target)
        when = time.time() - 5 * 3600
        os.utime(link, (when, when), follow_symlinks=False)
        rc, out, err = self.run_trim('--execute')
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b'target-bytes')

    def test_missing_go_cache_readme_deletes_nothing_and_exits_1(self):
        (self.cache / 'README').unlink()
        old = self.entry(4, 'a', 4, b'keep-me')
        rc, out, err = self.run_trim('--execute', '--quiet')
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(old.read_bytes(), b'keep-me')
        self.assertTrue(out or err)

    def test_go_cache_dry_run_deletes_nothing(self):
        old = self.entry(5, 'd', 4, b'dry')
        rc, out, err = self.run_trim()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(old.read_bytes(), b'dry')

    def test_go_cache_quiet_is_silent_when_nothing_is_deleted(self):
        self.entry(6, 'a', 0.25, b'fresh')
        rc, out, err = self.run_trim('--execute', '--quiet')
        self.assertEqual(rc, 0)
        self.assertEqual(out, '')
        self.assertEqual(err, '')


if __name__ == '__main__':
    unittest.main()
