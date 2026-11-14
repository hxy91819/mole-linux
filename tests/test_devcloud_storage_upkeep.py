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
import shutil
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

_REAL_STORAGE_RUN = subprocess.run
_REAL_STORAGE_POPEN = subprocess.Popen

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
        # git.example.invalid rejects WIP commits whose committer is the repo-local GitHub identity.
        gitconfig = Path(self.temp.name) / 'global.gitconfig'
        gitconfig.write_text('[user]\n\tname = corp\n\temail = corp@corp.example\n')
        sh('git', 'config', 'user.email', 'personal@proton.example', cwd=self.main)
        for patch in (mock.patch.object(m, 'parse_remote', lambda _url: ('git.example.invalid', 'team')),
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
        self.assertEqual(m.transport_url('git@git.example.invalid:wisp/mono2.git'), 'git@git.example.invalid:wisp/mono2.git')

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
        for patch in (mock.patch.object(m, 'parse_remote', lambda _url: ('git.example.invalid', 'team')),
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

    def _install_fake_bb_and_git(self, state_file, *, fail_mode=''):
        bindir = Path(self.temp.name) / 'fake-bin'
        bindir.mkdir(exist_ok=True)
        real_git = shutil.which('git')
        assert real_git
        git_wrapper = bindir / 'git'
        git_wrapper.write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, subprocess, sys\n'
            'log_path = os.environ.get("FAKE_GIT_LOG")\n'
            'if log_path:\n'
            '    with open(log_path, "a", encoding="utf-8") as fh:\n'
            '        fh.write(json.dumps(sys.argv[1:]) + "\\n")\n'
            f'os.execv("{real_git}", ["{real_git}"] + sys.argv[1:])\n'
        )
        git_wrapper.chmod(0o755)
        bb = bindir / 'bb'
        bb.write_text(
            '#!/usr/bin/env python3\n'
            'import json, os, sys\n'
            'state_path = os.environ["FAKE_BB_STATE"]\n'
            'fail_mode = os.environ.get("FAKE_BB_FAIL", "")\n'
            'with open(state_path, "r", encoding="utf-8") as fh:\n'
            '    state = json.load(fh)\n'
            'args = sys.argv[1:]\n'
            'state.setdefault("calls", []).append(args)\n'
            'with open(state_path, "w", encoding="utf-8") as fh:\n'
            '    json.dump(state, fh)\n'
            'if args[:3] == ["thread", "list", "--include-hidden"]:\n'
            '    print(json.dumps(state.get("threads", [])))\n'
            '    sys.exit(0)\n'
            'if args[:2] == ["environment", "list"]:\n'
            '    if fail_mode == "env_list":\n'
            '        sys.stderr.write("bb server unreachable\\n")\n'
            '        sys.exit(1)\n'
            '    print(json.dumps([e for e in state.get("environments", []) if e.get("status") != "destroyed"]))\n'
            '    sys.exit(0)\n'
            'if args[:2] == ["environment", "delete"]:\n'
            '    if fail_mode == "env_delete":\n'
            '        sys.stderr.write("bb rpc timeout\\n")\n'
            '        sys.exit(1)\n'
            '    env_id = args[2]\n'
            '    for env in state.get("environments", []):\n'
            '        if env["id"] == env_id:\n'
            '            if env.get("refuse"):\n'
            '                sys.stderr.write(env["refuse"] + "\\n")\n'
            '                sys.exit(1)\n'
            '            env["status"] = "destroyed"\n'
            '            with open(state_path, "w", encoding="utf-8") as fh:\n'
            '                json.dump(state, fh)\n'
            '            print(json.dumps({"ok": True}))\n'
            '            sys.exit(0)\n'
            '    sys.stderr.write("Environment not found\\n")\n'
            '    sys.exit(1)\n'
            'sys.stderr.write(f"unexpected bb command: {args}\\n")\n'
            'sys.exit(2)\n'
        )
        bb.chmod(0o755)
        env = dict(
            m.ENV,
            PATH=str(bindir) + os.pathsep + m.ENV.get('PATH', os.environ.get('PATH', '')),
            FAKE_BB_STATE=str(state_file),
            FAKE_BB_FAIL=fail_mode,
            FAKE_GIT_LOG=str(Path(self.temp.name) / 'fake-git-calls.jsonl'),
        )
        return env

    def _sweep_with_bb(self, env, *extra):
        out = io.StringIO()
        with mock.patch.object(m, 'ENV', env), contextlib.redirect_stdout(out):
            rc = m.main(['--root', str(self.code), '--state-dir', str(self.state), *extra])
        return rc, out.getvalue()

    def _jsonl_rows(self):
        logs = sorted(Path(self.state).glob('worktree-sweep-*.jsonl'))
        self.assertTrue(logs)
        return [json.loads(line) for line in logs[-1].read_text().splitlines() if line.strip()]

    def test_matching_bb_environment_is_deleted_with_recovery_command_after_worktree_removal(self):
        wt = self.add_worktree('bb-clean')
        head = sh('git', 'rev-parse', 'HEAD', cwd=wt)
        self.age(wt)
        state_file = Path(self.temp.name) / 'bb-state.json'
        state_file.write_text(json.dumps({
            'threads': [],
            'environments': [
                {'id': 'env_match', 'name': 'clean-env', 'path': str(wt), 'status': 'ready'},
                {'id': 'env_already', 'name': 'old-env', 'path': str(wt), 'status': 'destroyed'},
            ],
        }))
        env = self._install_fake_bb_and_git(state_file)
        rc, out = self._sweep_with_bb(env, '--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertFalse(wt.exists())
        saved = json.loads(state_file.read_text())
        by_id = {e['id']: e for e in saved['environments']}
        self.assertEqual(by_id['env_match']['status'], 'destroyed')
        delete_calls = [c for c in saved['calls'] if c[:2] == ['environment', 'delete']]
        self.assertEqual([c[2] for c in delete_calls], ['env_match'])
        git_calls = [
            json.loads(line)
            for line in Path(env['FAKE_GIT_LOG']).read_text().splitlines()
            if line.strip()
        ]
        self.assertTrue(any('worktree' in c and 'remove' in c and str(wt) in c for c in git_calls))
        rows = self._jsonl_rows()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row['result'], 'removed')
        self.assertIn('git fetch ', row.get('recovery_command', ''))
        self.assertIn(f'git worktree add ', row['recovery_command'])
        self.assertIn(str(wt), row['recovery_command'])
        self.assertIn(head, row['recovery_command'])
        self.assertEqual(row.get('bb_environments'), [{'id': 'env_match', 'action': 'delete', 'result': 'deleted'}])

    def test_non_exact_bb_environment_paths_are_untouched(self):
        wt = self.add_worktree('exact')
        self.age(wt)
        state_file = Path(self.temp.name) / 'bb-nonexact.json'
        state_file.write_text(json.dumps({
            'threads': [],
            'environments': [
                {'id': 'env_parent', 'name': 'parent', 'path': str(wt.parent), 'status': 'ready'},
                {'id': 'env_child', 'name': 'child', 'path': str(wt / 'sub'), 'status': 'ready'},
                {'id': 'env_sibling', 'name': 'sibling', 'path': str(wt) + '-2', 'status': 'ready'},
                {'id': 'env_main', 'name': 'main', 'path': str(self.main), 'status': 'ready'},
            ],
        }))
        env = self._install_fake_bb_and_git(state_file)
        rc, out = self._sweep_with_bb(env, '--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertFalse(wt.exists())
        saved = json.loads(state_file.read_text())
        self.assertEqual([c[2] for c in saved['calls'] if c[:2] == ['environment', 'delete']], ['env_child', 'env_sibling'])
        for item in saved['environments']:
            self.assertEqual(item['status'], 'destroyed' if item['id'] in ('env_child', 'env_sibling') else 'ready')
        row = self._jsonl_rows()[0]
        self.assertEqual(row.get('bb_environments'), [])
        self.assertIn('git worktree add ', row.get('recovery_command', ''))

    def test_refused_bb_environment_delete_is_logged_and_not_retried(self):
        wt = self.add_worktree('bb-refused', commit='local work')
        self.age(wt)
        state_file = Path(self.temp.name) / 'bb-refused.json'
        state_file.write_text(json.dumps({
            'threads': [],
            'environments': [
                {
                    'id': 'env_live',
                    'name': 'live-env',
                    'path': str(wt),
                    'status': 'ready',
                    'refuse': 'Environment still has live threads',
                },
            ],
        }))
        env = self._install_fake_bb_and_git(state_file)
        rc, out = self._sweep_with_bb(env, '--idle-hours', '1', '--execute')
        self.assertEqual(rc, 0, out)
        self.assertFalse(wt.exists())
        saved = json.loads(state_file.read_text())
        delete_calls = [c for c in saved['calls'] if c[:2] == ['environment', 'delete']]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(saved['environments'][0]['status'], 'ready')
        row = self._jsonl_rows()[0]
        self.assertEqual(row['result'], 'removed')
        self.assertIn('refs/heads/archive/worktree/bb-refused-', row.get('recovery_command', ''))
        self.assertIn(row['recovery']['sha'], row['recovery_command'])
        envs = row.get('bb_environments') or []
        self.assertEqual(len(envs), 1)
        self.assertEqual(envs[0]['id'], 'env_live')
        self.assertEqual(envs[0]['result'], 'refused')
        self.assertIn('Environment still has live threads', envs[0].get('reason', ''))

    def test_bb_failure_does_not_block_worktree_deletion_and_logs_error(self):
        for fail_mode in ('env_list', 'env_delete', 'missing_binary'):
            with self.subTest(fail_mode=fail_mode):
                wt = self.add_worktree(f'bb-down-{fail_mode}')
                self.age(wt)
                state_file = Path(self.temp.name) / f'bb-down-{fail_mode}.json'
                state_file.write_text(json.dumps({
                    'threads': [],
                    'environments': [
                        {'id': 'env_down', 'name': 'down-env', 'path': str(wt), 'status': 'ready'},
                    ],
                }))
                env = self._install_fake_bb_and_git(state_file, fail_mode=fail_mode)
                if fail_mode == 'missing_binary':
                    orig_execute = m.execute_item
                    no_bb_path = os.pathsep.join(
                        p for p in os.environ.get('PATH', '').split(os.pathsep)
                        if not (Path(p) / 'bb').exists()
                    )

                    def remove_bb_during_execute(repo, item, stamp, bb_envs=None):
                        (Path(self.temp.name) / 'fake-bin' / 'bb').unlink(missing_ok=True)
                        env['PATH'] = str(Path(self.temp.name) / 'fake-bin') + os.pathsep + no_bb_path
                        return orig_execute(repo, item, stamp, bb_envs)

                    with mock.patch.object(m, 'execute_item', side_effect=remove_bb_during_execute):
                        rc, out = self._sweep_with_bb(env, '--idle-hours', '1', '--execute')
                else:
                    rc, out = self._sweep_with_bb(env, '--idle-hours', '1', '--execute')
                self.assertEqual(rc, 0, out)
                self.assertFalse(wt.exists())
                row = [r for r in self._jsonl_rows() if r.get('path') == str(wt)][0]
                self.assertEqual(row['result'], 'removed')
                self.assertIn('git worktree add ', row.get('recovery_command', ''))
                has_err = bool(row.get('bb_environment_error')) or any(
                    e.get('result') == 'failed' for e in (row.get('bb_environments') or [])
                )
                self.assertTrue(has_err, row)


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
        lint_patch = mock.patch.object(trim, 'lint_cache_directory', lambda: str(root / 'absent-lint'), create=True)
        lint_patch.start(); self.addCleanup(lint_patch.stop)
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
        old_a = self.entry(1, 'a', 25, b'aaa')
        old_d = self.entry(2, 'd', 30, b'dddd')
        kept_root = self.cache / (('cd' * 32) + '-a')
        kept_root.write_bytes(b'root')
        os.utime(kept_root, (time.time() - 30 * 3600, time.time() - 30 * 3600))
        other = self.cache / 'not-a-bucket'
        other.mkdir()
        kept_other = other / (('ef' * 32) + '-a')
        kept_other.write_bytes(b'other')
        os.utime(kept_other, (time.time() - 30 * 3600, time.time() - 30 * 3600))
        rc, out, err = self.run_trim('--execute')
        self.assertEqual(rc, 0, out + err)
        self.assertFalse(old_a.exists())
        self.assertFalse(old_d.exists())
        self.assertEqual(kept_root.read_bytes(), b'root')
        self.assertEqual(kept_other.read_bytes(), b'other')
        self.assertIn('deleted 2', out)

    def test_recent_go_cache_entries_are_kept(self):
        recent = self.entry(3, 'a', 23, b'new')
        rc, out, err = self.run_trim('--execute')
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(recent.read_bytes(), b'new')

    def test_non_matching_go_cache_names_are_kept(self):
        directory = self.cache / '0a'
        directory.mkdir()
        when = time.time() - 30 * 3600
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
        when = time.time() - 30 * 3600
        os.utime(link, (when, when), follow_symlinks=False)
        rc, out, err = self.run_trim('--execute')
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_bytes(), b'target-bytes')

    def test_missing_go_cache_readme_deletes_nothing_and_exits_1(self):
        (self.cache / 'README').unlink()
        old = self.entry(4, 'a', 30, b'keep-me')
        rc, out, err = self.run_trim('--execute', '--quiet')
        self.assertEqual(rc, 1, out + err)
        self.assertEqual(old.read_bytes(), b'keep-me')
        self.assertTrue(out or err)

    def test_go_cache_dry_run_deletes_nothing(self):
        old = self.entry(5, 'd', 30, b'dry')
        rc, out, err = self.run_trim()
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(old.read_bytes(), b'dry')

    def test_go_cache_quiet_is_silent_when_nothing_is_deleted(self):
        self.entry(6, 'a', 23, b'fresh')
        rc, out, err = self.run_trim('--execute', '--quiet')
        self.assertEqual(rc, 0)
        self.assertEqual(out, '')
        self.assertEqual(err, '')


if __name__ == '__main__':
    unittest.main()


# New owner 2026-11-14 maintenance surfaces. External services are fakes.
import pytest


def load_upkeep(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parents[1] /
        '.claude/skills/devcloud-storage-upkeep/scripts' / (name + '.py'))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def clone_case(tmp_path, monkeypatch):
    c = load_upkeep('tmp_clone_sweep')
    root = tmp_path / 'tmp'
    root.mkdir()
    repo = root / 'clone'
    (repo / '.git').mkdir(parents=True)
    (repo / 'a').write_text('source')
    state = tmp_path / 'state.json'
    state.write_text(json.dumps({'path': str(repo), 'sha': 'a' * 40}))
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    fake = bindir / 'git'
    fake.write_text(r"""#!/usr/bin/env python3
import fcntl, json, os, sys
p = os.environ['CLONE_STATE']
lock = open(p + '.lock', 'a'); fcntl.flock(lock, fcntl.LOCK_EX)
s = json.load(open(p)); a = sys.argv[3:]; path = sys.argv[2]
s.setdefault('calls', []).append(a); json.dump(s, open(p, 'w'))
h = s['sha']; cmd = a[0]
if s.get('fail') == cmd: sys.exit(1)
if cmd == 'rev-parse': print(str(path) + '/.git' if '--git-common-dir' in a or '--git-dir' in a else h)
elif cmd == 'remote':
    if len(a) == 1: print('origin')
    else: print(s.get('url', 'https://github.com/hxy91819/example.git'))
elif cmd == 'config': print('remote.origin.url ' + s.get('url', 'https://github.com/hxy91819/example.git'))
elif cmd == 'status': print(s.get('dirty', ''), end='')
elif cmd == 'stash': print(s.get('stash', ''), end='')
elif cmd == 'worktree': print('worktree ' + path + '\nHEAD ' + h + '\n' + s.get('extra_worktree', ''))
elif cmd == 'ls-files': print(s.get('ignored', ''), end='')
elif cmd == 'for-each-ref':
    print('refs/heads/main ' + h)
    if s.get('local_branch'): print('refs/heads/local ' + 'b' * 40)
    if s.get('tag'): print('refs/tags/local ' + 'c' * 40)
elif cmd == 'ls-remote':
    if s.get('offline'): sys.exit(1)
    if a[-1].startswith(('refs/heads/archive/', 'refs/tags/archive/')):
        print(s.get('pushed', 'WRONG') + '\t' + a[-1])
    else:
        if not s.get('unpublished'): print(h + '\trefs/heads/main')
        for ref, sha in s.get('archives', {}).items(): print(sha + '\t' + ref)
elif cmd == 'push':
    if s.get('reject'): sys.exit(1)
    s.setdefault('archives', {})[a[-1].split(':', 1)[1]] = a[-1].split(':')[0]; s['pushed'] = a[-1].split(':')[0]; json.dump(s, open(p, 'w'))
elif cmd in ('merge-base', 'cat-file', 'symbolic-ref'): sys.exit(1)
else: sys.exit(2)
""")
    fake.chmod(0o755)
    monkeypatch.setattr(c.ws, 'ENV', dict(c.ws.ENV, PATH=str(bindir) + ':' + os.environ['PATH'], CLONE_STATE=str(state)))
    for target in (repo / 'a', repo / '.git', repo): os.utime(target, (OLD, OLD))
    return c, repo, root, state


def execute_single_clone(c, item, roots, references, stamp):
    c.prepare_clone(item, roots, references, stamp)
    c.remove_prepared(item, roots, c.reference_index(roots, [item['path']]))


def clone_plan(case, **changes):
    c, repo, root, state = case
    s = json.loads(state.read_text()); s.update(changes); state.write_text(json.dumps(s))
    return c.plan_clone(str(repo), [str(root)], {}, {})


@pytest.mark.parametrize('change,reason', [
    ({'dirty': '?? local.txt\n'}, 'dirty'),
    ({'stash': 'stash@{0}'}, 'stash'),
    ({'extra_worktree': 'worktree /somewhere/linked\n'}, 'linked worktrees'),
    ({'ignored': '.env\0'}, 'ignored credential'),
    ({'offline': True, 'unpublished': True}, 'unreachable'),
    ({'unpublished': True, 'url': 'https://github.com/other/upstream.git'}, 'no personal or internal remote'),
    ({'fail': 'status'}, 'check failed'),
])
def test_tmp_clone_guards_fail_closed(clone_case, change, reason):
    row = clone_plan(clone_case, **change)
    assert row['action'] == 'keep'
    assert reason in row['reason']


@pytest.mark.parametrize('guard', ['process', 'unknown', 'container', 'bb', 'recent', 'nested', 'remote_reference', 'alternate_reference', 'symlink', 'outside'])
def test_tmp_clone_filesystem_and_usage_guards(clone_case, guard, tmp_path):
    c, repo, root, state = clone_case
    anchors, refs, error = {}, {}, None
    if guard in ('process', 'container', 'bb'): c.ws.add_anchor(anchors, str(repo / 'child'), guard)
    if guard == 'unknown': error = 'process scan incomplete'
    if guard == 'recent': os.utime(repo / 'a', None)
    if guard == 'nested': (repo / 'inner' / '.git').mkdir(parents=True)
    if guard.endswith('_reference'): refs[str(repo)] = guard
    if guard == 'symlink':
        link = root / 'alias'; link.symlink_to(repo, target_is_directory=True); repo = link
    if guard == 'outside': root = tmp_path / 'other'; root.mkdir()
    row = c.plan_clone(str(repo), [str(root)], anchors, refs, scan_error=error)
    assert row['action'] == 'keep', row
    expected = {
        'process': 'in use', 'container': 'in use', 'bb': 'in use',
        'unknown': 'process scan incomplete', 'recent': 'active within',
        'nested': 'nested .git', 'remote_reference': 'remote_reference',
        'alternate_reference': 'alternate_reference', 'symlink': 'symlink', 'outside': 'outside',
    }
    assert expected[guard] in row['reason']


def test_tmp_clone_all_refs_need_live_proof(clone_case):
    assert clone_plan(clone_case)['action'] == 'remove'
    for change in ({'local_branch': True}, {'tag': True}, {'unpublished': True}):
        row = clone_plan(clone_case, **change)
        assert row['action'] == 'archive+remove'
        assert row['archive_objects']


@pytest.mark.parametrize('failure', ['reject', 'wrong_proof', 'busy_again', 'unknown_again', 'changed_again', 'delete_error'])
def test_tmp_clone_sink_failure_keeps_clone(clone_case, monkeypatch, failure):
    c, repo, root, state = clone_case
    row = clone_plan(clone_case, unpublished=True, reject=failure == 'reject')
    row['bytes'] = 4096
    anchors = {}
    if failure == 'busy_again': c.ws.add_anchor(anchors, str(repo), 'process new')
    def usage():
        if failure == 'unknown_again': raise c.ws.ScanIncomplete('process scan incomplete')
        if failure == 'changed_again': os.utime(repo / 'a', None)
        return anchors
    monkeypatch.setattr(c, 'usage_anchors', usage)
    if failure == 'wrong_proof':
        original = c.ws.git
        def wrong(path, *args, **kw):
            if args[0] == 'ls-remote' and args[-1].startswith('refs/heads/archive/'):
                return subprocess.CompletedProcess([], 0, 'WRONG\t' + args[-1] + '\n', '')
            return original(path, *args, **kw)
        monkeypatch.setattr(c.ws, 'git', wrong)
    if failure == 'delete_error': monkeypatch.setattr(c.shutil, 'rmtree', mock.Mock(side_effect=PermissionError('denied')))
    expected = {'reject': 'push', 'wrong_proof': 'does not match', 'busy_again': 'in use', 'unknown_again': 'scan incomplete', 'changed_again': 'active within', 'delete_error': 'denied'}[failure]
    with pytest.raises(Exception, match=expected): execute_single_clone(c, row, [str(root)], {}, 'test')
    assert repo.exists()
    calls = json.loads(state.read_text()).get('calls', [])
    assert not any('--force' in call or '-f' in call for call in calls if call[0] == 'push')


def test_tmp_clone_archive_logs_exact_refs_and_unlinks_no_symlink_target(clone_case, monkeypatch, tmp_path):
    c, repo, root, state = clone_case
    outside = tmp_path / 'outside'; outside.mkdir(); (outside / 'keep').write_text('keep')
    (repo / 'link').symlink_to(outside, target_is_directory=True)
    os.utime(repo, (OLD, OLD)); os.utime(repo / 'link', (OLD, OLD), follow_symlinks=False)
    row = clone_plan(clone_case, local_branch=True, tag=True); row['bytes'] = 4096
    monkeypatch.setattr(c, 'usage_anchors', lambda: {})
    execute_single_clone(c, row, [str(root)], {}, 'test')
    assert not repo.exists() and (outside / 'keep').exists()
    assert row['result'] == 'removed'
    assert len(row['archives']) == 2
    assert row['head'] == 'a' * 40 and row['urls'] and row['bytes'] == 4096


def test_tmp_clone_mono2_never_archives_to_github(clone_case):
    c, repo, root, state = clone_case
    (repo / '.git' / 'config').write_text('mono2')
    row = clone_plan(clone_case, unpublished=True, url='https://github.com/hxy91819/mono2.git')
    assert row['action'] == 'keep' and 'mono2' in row['reason']


@pytest.fixture
def pnpm_case(tmp_path, monkeypatch):
    p = load_upkeep('pnpm_store_prune')
    store = tmp_path / 'store' / 'v11'; store.mkdir(parents=True); (store / 'cached').write_bytes(b'x' * 8192)
    bindir = tmp_path / 'bin'; bindir.mkdir()
    fake = bindir / 'pnpm'
    fake.write_text(r"""#!/usr/bin/env python3
import os, sys
from pathlib import Path
with open(os.environ['PNPM_CALLS'], 'a') as f: f.write(' '.join(sys.argv[1:]) + '\n')
if os.environ.get('PNPM_FAIL') == sys.argv[-1]: sys.exit(1)
a=sys.argv[1:]
if a == ['--version']: print('12.0.0'); sys.exit(0)
if '--store-dir' in a:
    i=a.index('--store-dir'); a=a[:i]+a[i+2:]
if a == ['store', 'path']: print(os.environ['PNPM_STORE'])
elif a == ['store', 'prune']: Path(os.environ['PNPM_STORE'], 'cached').unlink(missing_ok=True)
else: sys.exit(2)
""")
    fake.chmod(0o755)
    monkeypatch.setattr(p.ws, 'ENV', dict(p.ws.ENV, PATH=str(bindir) + ':' + os.environ['PATH'], PNPM_STORE=str(store), PNPM_CALLS=str(tmp_path / 'calls')))
    monkeypatch.setattr(p, 'package_process_state', lambda: (1, 'idle'))
    return p, store, tmp_path


@pytest.mark.parametrize('state', [0, 2])
def test_pnpm_busy_or_unknown_skips_prune(pnpm_case, monkeypatch, state):
    p, store, root = pnpm_case
    monkeypatch.setattr(p, 'package_process_state', lambda: (state, 'busy or unknown'))
    assert p.main(['--execute', '--quiet', '--state-dir', str(root / 'log')]) == 0
    assert (store / 'cached').exists()
    row = json.loads(next((root / 'log').glob('pnpm*.jsonl')).read_text().splitlines()[-1])
    assert row['result'] == 'skipped' and row['reason'] == 'busy or unknown'
    assert 'store prune' not in (root / 'calls').read_text() if (root / 'calls').exists() else True


def test_pnpm_default_is_read_only_and_quiet(pnpm_case, capsys):
    p, store, root = pnpm_case
    assert p.main(['--quiet', '--state-dir', str(root / 'log')]) == 0
    assert (store / 'cached').exists() and not capsys.readouterr().out


def test_pnpm_executes_owner_command_and_records_sizes(pnpm_case):
    p, store, root = pnpm_case
    assert p.main(['--execute', '--state-dir', str(root / 'log')]) == 0
    assert not (store / 'cached').exists()
    row = json.loads(next((root / 'log').glob('pnpm*.jsonl')).read_text().splitlines()[-1])
    assert row['before_bytes'] > row['after_bytes'] and row['result'] == 'pruned'


@pytest.mark.parametrize('failure', ['path', 'prune', 'measure', 'missing'])
def test_pnpm_failures_are_reported(pnpm_case, monkeypatch, failure, capsys):
    p, store, root = pnpm_case
    if failure == 'measure': monkeypatch.setattr(p, 'store_bytes', lambda path: (_ for _ in ()).throw(RuntimeError('du failed')))
    elif failure == 'missing': monkeypatch.setitem(p.ws.ENV, 'PATH', '/nonexistent')
    else: monkeypatch.setitem(p.ws.ENV, 'PNPM_FAIL', failure)
    assert p.main(['--execute', '--quiet', '--state-dir', str(root / 'log')]) == 1
    assert capsys.readouterr().err


@pytest.mark.parametrize('args,busy', [(['node', '/usr/bin/pnpm.cjs', 'install'], True), (['npm', 'ci'], False), (['yarn'], False), (['pnpm', 'add', 'x'], True), (['pnpm', 'run', 'dev'], False), (['python', 'some-pnpm-install-test.py'], False)])
def test_package_process_classification(args, busy):
    p = load_upkeep('pnpm_store_prune')
    assert p.installing(args) is busy


def test_pnpm_proc_permission_is_unknown(tmp_path, monkeypatch):
    p = load_upkeep('pnpm_store_prune')
    with mock.patch.object(p.os, 'listdir', side_effect=PermissionError('denied')):
        assert p.package_process_state()[0] == 2


@pytest.mark.parametrize('execute', [False, True])
def test_worktree_missing_environments_swept_even_without_worktrees(tmp_path, execute):
    # Reuse the fake bb binary, not the real BB service.
    helper = WorktreeSweepTests('test_dry_run_changes_nothing'); helper.setUp()
    try:
        state = tmp_path / 'bb.json'
        state.write_text(json.dumps({'environments': [
            {'id': 'gone', 'path': str(tmp_path / 'gone'), 'status': 'ready'},
            {'id': 'refused', 'path': str(tmp_path / 'gone2'), 'status': 'ready', 'refuse': 'Environment still has live threads'},
            {'id': 'existing', 'path': str(helper.main), 'status': 'ready'},
            {'id': 'destroyed', 'path': str(tmp_path / 'old'), 'status': 'destroyed'}]}))
        env = helper._install_fake_bb_and_git(state)
        rc, out = helper._sweep_with_bb(env, *(['--execute'] if execute else []))
        assert rc == 0
        calls = [x for x in json.loads(state.read_text())['calls'] if x[:2] == ['environment', 'delete']]
        assert [x[2] for x in calls] == (['gone', 'refused'] if execute else [])
        if execute:
            rows = helper._jsonl_rows()
            assert any(x.get('bb_environments', [{}])[0].get('result') == 'refused' for x in rows)
    finally: helper.doCleanups()


def test_tmp_clone_reference_index_reads_remote_and_alternates(clone_case, monkeypatch):
    c, repo, root, state = clone_case
    other = root / 'other'
    (other / '.git' / 'objects' / 'info').mkdir(parents=True)
    original = c.ws.git
    def fake_config(path, *args, **kw):
        if args[0] == 'config':
            return subprocess.CompletedProcess([], 0, f'remote.origin.url file://{repo}\n', '')
        return original(path, *args, **kw)
    monkeypatch.setattr(c.ws, 'git', fake_config)
    assert 'remote URL reference' in c.reference_index([str(root)], [str(repo)])[str(repo)]
    def empty_config(path, *args, **kw):
        if args[0] == 'config': return subprocess.CompletedProcess([], 1, '', '')
        return original(path, *args, **kw)
    monkeypatch.setattr(c.ws, 'git', empty_config)
    (other / '.git' / 'objects' / 'info' / 'alternates').write_text(str(repo / '.git' / 'objects') + '\n')
    assert 'alternates reference' in c.reference_index([str(root)], [str(repo)])[str(repo)]


def test_tmp_clone_local_remote_aliases_and_tilde_are_resolved(clone_case, monkeypatch):
    c, repo, root, state = clone_case
    monkeypatch.setenv('HOME', str(root))
    assert c.local_path('~/clone', str(root / 'other')) == str(repo)


def test_tmp_clone_still_checks_usage_after_archive(clone_case, monkeypatch):
    c, repo, root, state = clone_case
    row = clone_plan(clone_case, unpublished=True)
    calls = []
    def usage():
        calls.append(1)
        anchors = {}
        if len(calls) > 1: c.ws.add_anchor(anchors, str(repo), 'process after push')
        return anchors
    monkeypatch.setattr(c, 'usage_anchors', usage)
    with pytest.raises(c.ws.Keep, match='in use'): execute_single_clone(c, row, [str(root)], {}, 'test')
    assert repo.exists() and row['archives']


@pytest.mark.parametrize('failure', ['status', 'stash', 'worktree', 'ls-files', 'for-each-ref', 'rev-parse'])
def test_tmp_clone_git_failure_never_deletes(clone_case, failure):
    assert clone_plan(clone_case, fail=failure)['action'] == 'keep'


def test_pnpm_rechecks_processes_after_sizing(pnpm_case, monkeypatch):
    p, store, root = pnpm_case
    monkeypatch.setattr(p, 'package_process_state', mock.Mock(side_effect=[(1, 'idle'), (0, 'new installer')]))
    assert p.main(['--execute', '--quiet', '--state-dir', str(root / 'log')]) == 0
    assert (store / 'cached').exists()
    assert 'store prune' not in (root / 'calls').read_text()


def test_pnpm_process_title_is_detected():
    p = load_upkeep('pnpm_store_prune')
    assert not p.installing(['npm install', '', ''])
    assert p.installing(['node', '--no-warnings', '/usr/bin/pnpm.cjs', 'install'])


def test_tmp_clone_real_git_preserves_unpublished_branch_and_annotated_tag(tmp_path, monkeypatch):
    c = load_upkeep('tmp_clone_sweep')
    root = tmp_path / 'clones'; root.mkdir()
    repo = root / 'repo'; remote = tmp_path / 'remote.git'
    sh('git', 'init', '-q', '--bare', '-b', 'main', str(remote))
    sh('git', 'init', '-q', '-b', 'main', str(repo))
    sh('git', 'config', 'user.name', 'T', cwd=repo)
    sh('git', 'config', 'user.email', 't@example.com', cwd=repo)
    sh('git', 'config', 'core.excludesFile', '/dev/null', cwd=repo)
    (repo / 'a').write_text('published')
    sh('git', 'add', '.', cwd=repo); sh('git', 'commit', '-qm', 'published', cwd=repo)
    sh('git', 'remote', 'add', 'origin', str(remote), cwd=repo)
    sh('git', 'push', '-q', 'origin', 'main', cwd=repo)
    sh('git', 'checkout', '-qb', 'local', cwd=repo)
    (repo / 'a').write_text('only local')
    sh('git', 'commit', '-qam', 'local', cwd=repo)
    sh('git', 'tag', '-am', 'local tag', 'local-tag', cwd=repo)
    tag = sh('git', 'rev-parse', 'refs/tags/local-tag', cwd=repo)
    head = sh('git', 'rev-parse', 'HEAD', cwd=repo)
    for target in (repo / 'a', repo): os.utime(target, (OLD, OLD))
    monkeypatch.setattr(c.ws.Repo, '_archive_destination', lambda *a: 'origin')
    monkeypatch.setattr(c, 'usage_anchors', lambda: {})
    row = c.plan_clone(str(repo), [str(root)], {}, {})
    assert row['action'] == 'archive+remove', row
    execute_single_clone(c, row, [str(root)], {}, 'real')
    assert not repo.exists()
    refs = sh('git', 'for-each-ref', '--format=%(objectname)', cwd=remote).splitlines()
    assert tag in refs and head in refs


def test_tmp_clone_discovery_does_not_include_linked_worktree_or_symlink(clone_case, monkeypatch, tmp_path, capsys):
    c, repo, root, state = clone_case
    linked = root / 'linked'; linked.mkdir(); (linked / '.git').write_text('gitdir: somewhere')
    (root / 'alias').symlink_to(repo, target_is_directory=True)
    monkeypatch.setattr(c, 'ROOTS', (str(root),))
    monkeypatch.setattr(c, 'usage_anchors', lambda: {})
    monkeypatch.setattr(c, 'reference_index', lambda *a: {})
    assert c.main(['--quiet', '--state-dir', str(tmp_path / 'logs')]) == 0
    assert repo.exists() and not capsys.readouterr().out
    rows = [json.loads(x) for x in next((tmp_path / 'logs').glob('tmp-clone-sweep-*.jsonl')).read_text().splitlines()]
    assert len(rows) == 1 and rows[0]['path'] == str(repo)


def test_tmp_clone_proc_unknown_refuses_whole_run(clone_case, monkeypatch, tmp_path, capsys):
    c, repo, root, state = clone_case
    monkeypatch.setattr(c, 'ROOTS', (str(root),))
    monkeypatch.setattr(c, 'usage_anchors', mock.Mock(side_effect=c.ws.ScanIncomplete('process scan incomplete')))
    assert c.main(['--execute', '--quiet', '--state-dir', str(tmp_path / 'logs')]) == 1
    assert repo.exists() and 'scan incomplete' in capsys.readouterr().err


def test_pnpm_failure_after_prune_records_after_size(pnpm_case, monkeypatch):
    p, store, root = pnpm_case
    original = p.ws.run
    def fail_after_deletion(cmd, **kw):
        result = original(cmd, **kw)
        if cmd[-2:] == ['store', 'prune']:
            raise subprocess.CalledProcessError(1, cmd, '', 'partial prune failed')
        return result
    monkeypatch.setattr(p.ws, 'run', fail_after_deletion)
    assert p.main(['--execute', '--state-dir', str(root / 'logs')]) == 1
    row = json.loads(next((root / 'logs').glob('pnpm*.jsonl')).read_text().splitlines()[-1])
    assert row['after_bytes'] < row['before_bytes']


def test_tmp_clone_reference_scan_handles_non_git_cache_markers(clone_case, monkeypatch):
    c, repo, root, state = clone_case
    fixture = root / 'fixture'; fixture.mkdir(); (fixture / '.git').write_text('not a gitfile')
    original = c.ws.git
    def config_only(path, *args, **kw):
        if str(path) == str(fixture):
            raise subprocess.CalledProcessError(128, ['git'], '', 'fatal: invalid gitfile format')
        if args[0] == 'config': return subprocess.CompletedProcess([], 1, '', '')
        return original(path, *args, **kw)
    monkeypatch.setattr(c.ws, 'git', config_only)
    assert c.reference_index([str(root)], [str(repo)]) == {}


@pytest.mark.parametrize('args', [
    ['pnpm', '--dir', '/project', 'install'],
    ['node', '--require', '/tracer.js', '/usr/bin/pnpm.cjs', 'add', 'dep'],
])
def test_pnpm_installers_with_flags_and_process_titles(args):
    p = load_upkeep('pnpm_store_prune')
    assert p.installing(args)


def test_tmp_clone_partial_du_is_not_a_reclaimable_size(monkeypatch):
    c = load_upkeep('tmp_clone_sweep')
    monkeypatch.setattr(c.ws, 'run', lambda *a, **kw: subprocess.CompletedProcess([], 1, '123 /incomplete\n', 'permission denied'))
    rows = [{'path': '/unused'}]
    c.ws.measure(rows)
    assert rows[0]['bytes'] is None


@pytest.mark.parametrize('kind', ['bb', 'bb-failed', 'docker-unknown', 'proc-unknown'])
def test_tmp_clone_usage_probes_reserve_environment_paths(clone_case, monkeypatch, kind):
    c, repo, root, state = clone_case
    bindir = Path(c.ws.ENV['PATH'].split(':')[0])
    bb = bindir / 'bb'
    bb.write_text('#!/usr/bin/env python3\nimport sys, json\n' +
        ('sys.exit(1)\n' if kind == 'bb-failed' else f'print(json.dumps([{{"id":"env","path":{str(repo / "child")!r},"status":"ready"}}]))\n'))
    bb.chmod(0o755)
    monkeypatch.setattr(c.ws, 'process_anchors', mock.Mock(side_effect=c.ws.ScanIncomplete('process unknown')) if kind == 'proc-unknown' else lambda a: None)
    monkeypatch.setattr(c.ws, 'docker_anchors', mock.Mock(side_effect=c.ws.ScanIncomplete('docker unknown')) if kind == 'docker-unknown' else lambda a: None)
    if kind == 'bb':
        assert 'BB environment' in c.ws.anchored(c.usage_anchors(), str(repo))
    else:
        with pytest.raises(Exception): c.usage_anchors()


def test_tmp_clone_missing_proc_root_is_unknown(monkeypatch):
    c = load_upkeep('tmp_clone_sweep')
    monkeypatch.setattr(c.ws.os, 'listdir', mock.Mock(side_effect=FileNotFoundError('/proc unavailable')))
    with pytest.raises(c.ws.ScanIncomplete): c.ws.process_anchors({})


def test_tmp_clone_reference_scan_skips_dead_linked_gitfiles(clone_case, monkeypatch):
    c, repo, root, state = clone_case
    dead = root / 'dead'; dead.mkdir(); (dead / '.git').write_text('gitdir: /does-not-exist/gitdir\n')
    original = c.ws.git
    def fake(path, *args, **kw):
        if str(path) == str(dead): raise subprocess.CalledProcessError(128, ['git'], '', 'not a git repository')
        if args[0] == 'config': return subprocess.CompletedProcess([], 1, '', '')
        return original(path, *args, **kw)
    monkeypatch.setattr(c.ws, 'git', fake)
    assert c.reference_index([str(root)], [str(repo)]) == {}


def test_tmp_clone_reference_scan_handles_empty_git_directory(clone_case, monkeypatch):
    c, repo, root, state = clone_case
    fixture = root / 'empty'; (fixture / '.git').mkdir(parents=True)
    original = c.ws.git
    def fake(path, *args, **kw):
        if str(path) == str(fixture) and args[0] == 'rev-parse':
            return subprocess.CompletedProcess([], 128, '', 'fatal: not a git repository') if kw.get('check') is False else (_ for _ in ()).throw(subprocess.CalledProcessError(128, ['git'], '', 'fatal: not a git repository'))
        if args[0] == 'config': return subprocess.CompletedProcess([], 1, '', '')
        return original(path, *args, **kw)
    monkeypatch.setattr(c.ws, 'git', fake)
    assert c.reference_index([str(root)], [str(repo)]) == {}


def test_tmp_clone_last_network_window_cannot_hide_new_write(clone_case, monkeypatch):
    c, repo, root, state = clone_case
    row = clone_plan(clone_case)
    original = c.ws.git
    calls = []
    def write_during_last_proof(path, *args, **kw):
        result = original(path, *args, **kw)
        if args[0] == 'ls-remote':
            calls.append(1)
            if len(calls) == 2: (repo / 'a').write_text('new work while remote was queried')
        return result
    monkeypatch.setattr(c.ws, 'git', write_during_last_proof)
    monkeypatch.setattr(c, 'usage_anchors', lambda: {})
    monkeypatch.setattr(c, 'reference_index', lambda *a: {})
    try:
        execute_single_clone(c, row, [str(root)], {}, 'test')
    except c.ws.Keep as exc:
        assert 'active within' in str(exc)
    if len(calls) >= 2:
        assert repo.exists()  # a late network query introduced real new work
    else:
        assert row['result'] == 'removed'  # no late query, so the mutation never occurred


def test_tmp_clone_new_reference_during_archive_is_rechecked(clone_case, monkeypatch):
    c, repo, root, state = clone_case
    row = clone_plan(clone_case, local_branch=True)
    monkeypatch.setattr(c, 'usage_anchors', lambda: {})
    def refreshed(*args):
        assert json.loads(state.read_text()).get('archives')
        return {str(repo): 'objects alternates reference: newly changed repo'}
    monkeypatch.setattr(c, 'reference_index', refreshed)
    with pytest.raises(c.ws.Keep, match='alternates reference'): execute_single_clone(c, row, [str(root)], {}, 'test')
    assert repo.exists()


@pytest.mark.parametrize('incomplete', [False, True])
def test_tmp_clone_batch_refreshes_after_all_network_and_fails_closed(clone_case, monkeypatch, tmp_path, capsys, incomplete):
    c, repo, root, state = clone_case
    second = root / 'clone2'; (second / '.git').mkdir(parents=True); (second / 'a').write_text('source')
    for p in (second, second / 'a'): os.utime(p, (OLD, OLD))
    monkeypatch.setattr(c, 'ROOTS', (str(root),))
    monkeypatch.setattr(c, 'usage_anchors', lambda: {})
    prepared = []
    original = c.prepare_clone
    def prepare(*args):
        original(*args)
        prepared.append(args[0]['path'])
    monkeypatch.setattr(c, 'prepare_clone', prepare)
    scans = []
    def refs(*args):
        scans.append(1)
        if len(scans) == 1: return {}
        assert len(prepared) == 2
        if incomplete: raise c.ws.ScanIncomplete('reference unknown')
        return {str(repo): 'remote URL reference: changed while archiving'}
    monkeypatch.setattr(c, 'reference_index', refs)
    result = c.main(['--execute', '--quiet', '--state-dir', str(tmp_path / 'logs')])
    assert repo.exists() and second.exists() is incomplete
    assert len(scans) == 2
    assert result == (1 if incomplete else 0)
    if incomplete: assert 'reference unknown' in capsys.readouterr().err


def test_tmp_clone_remote_inside_clone_is_not_a_recovery_copy(clone_case):
    c, repo, root, state = clone_case
    row = clone_plan(clone_case, url=str(repo / '.git' / 'remote.git'))
    assert row['action'] == 'keep' and 'no personal or internal remote' in row['reason']


def test_tmp_clone_bare_repository_alternate_is_a_reference(clone_case, monkeypatch):
    c, repo, root, state = clone_case
    bare = root / 'bare.git'
    (bare / 'objects' / 'info').mkdir(parents=True); (bare / 'refs').mkdir()
    (bare / 'HEAD').write_text('ref: refs/heads/main\n'); (bare / 'config').write_text('[core]\n bare = true\n')
    (bare / 'objects' / 'info' / 'alternates').write_text(str(repo / '.git' / 'objects') + '\n')
    original = c.ws.git
    def fake(path, *args, **kw):
        if str(path) == str(bare) and args[0] == 'rev-parse': return subprocess.CompletedProcess([], 0, str(bare), '')
        if args[0] == 'config': return subprocess.CompletedProcess([], 1, '', '')
        return original(path, *args, **kw)
    monkeypatch.setattr(c.ws, 'git', fake)
    assert 'alternates reference' in c.reference_index([str(root)], [str(repo)])[str(repo)]


def test_tmp_clone_split_fetch_push_url_deletes_after_verified_archive(tmp_path, monkeypatch):
    c = load_upkeep('tmp_clone_sweep')
    helper = WorktreeSweepTests('test_dry_run_changes_nothing'); helper.setUp()
    try:
        repo = helper.main
        fork = Path(helper.temp.name) / 'fork.git'
        sh('git', 'init', '-q', '--bare', '-b', 'main', str(fork))
        sh('git', 'remote', 'set-url', '--push', 'origin', str(fork), cwd=repo)
        (repo / 'a.txt').write_text('only in local branch')
        sh('git', 'commit', '-qam', 'local fork work', cwd=repo)
        head = sh('git', 'rev-parse', 'HEAD', cwd=repo)
        for parent, dirs, files in os.walk(repo):
            dirs[:] = [x for x in dirs if x != '.git']
            for name in files: os.utime(Path(parent) / name, (OLD, OLD))
            os.utime(parent, (OLD, OLD))
        monkeypatch.setattr(c.ws.Repo, '_archive_destination', lambda *a: 'origin')
        monkeypatch.setattr(c, 'usage_anchors', lambda: {})
        row = c.plan_clone(str(repo), [str(helper.code)], {}, {})
        assert row['action'] == 'archive+remove'
        execute_single_clone(c, row, [str(helper.code)], {}, 'split')
        assert not repo.exists() and row['archives']
        assert head in sh('git', 'for-each-ref', '--format=%(objectname)', cwd=fork).splitlines()
        assert head not in sh('git', 'for-each-ref', '--format=%(objectname)', cwd=helper.bare).splitlines()
    finally:
        helper.doCleanups()


@pytest.mark.parametrize('change,reason', [({'dirty': '?? new-work\n'}, 'dirty'), ({'ignored': '.env\0'}, 'ignored credential'), ({'stash': 'new stash'}, 'stash')])
def test_tmp_clone_final_local_guards_run_after_reference_io(clone_case, monkeypatch, change, reason):
    c, repo, root, state = clone_case
    row = clone_plan(clone_case)
    monkeypatch.setattr(c, 'usage_anchors', lambda: {})
    def change_during_scan(*args):
        s = json.loads(state.read_text()); s.update(change); state.write_text(json.dumps(s))
        return {}
    monkeypatch.setattr(c, 'reference_index', change_during_scan)
    with pytest.raises(c.ws.Keep, match=reason): execute_single_clone(c, row, [str(root)], {}, 'test')
    assert repo.exists()


@pytest.mark.parametrize('name', ['tmp_clone_sweep.py', 'pnpm_store_prune.py'])
def test_storage_snapshot_help_does_not_write_bytecode(tmp_path, name):
    source = Path(__file__).resolve().parents[1] / '.claude/skills/devcloud-storage-upkeep/scripts'
    for f in (name, 'worktree_sweep.py'): shutil.copy2(source / f, tmp_path / f)
    before = sorted(p.name for p in tmp_path.iterdir())
    subprocess.run(['python3', '-I', str(tmp_path / name), '--help'], check=True, capture_output=True, text=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_tmp_clone_internal_origin_pushurl_cannot_cross_to_github(clone_case, monkeypatch):
    c, repo, root, state = clone_case
    original = c.ws.git
    def split(path, *args, **kw):
        if args[:2] == ('remote', 'get-url') and '--push' in args:
            return subprocess.CompletedProcess([], 0, 'https://github.com/hxy91819/public-copy.git\n', '')
        return original(path, *args, **kw)
    monkeypatch.setattr(c.ws, 'git', split)
    row = clone_plan(clone_case, unpublished=True, url='https://git.example.invalid/team/internal-project.git')
    assert row['action'] == 'keep' and 'no personal or internal remote' in row['reason']


@pytest.mark.parametrize('args,busy', [
    (['npm', 'install'], False), (['npm exec vite --host 127.0.0.1'], False),
    (['npx', 'pnpm@9', 'install'], False), (['vite'], False), (['yarn', 'install'], False),
    (['pnpm', 'exec', 'install'], False), (['pnpm', 'run', 'install'], False),
    (['pnpm', 'start', 'install'], False), (['pnpm', 'build', 'add'], False),
    (['node', 'app.js', 'pnpm', 'install'], False),
    (['node', '/usr/bin/npm-cli.js', 'install'], False),
    (['pnpm', '--filter', 'install', 'run', 'dev'], False),
    (['pnpm', '--store-dir', 'install', 'store', 'path'], False),
    (['pnpm', 'store', 'path'], False), (['pnpm', 'store', 'status'], False),
    (['pnpm', 'install'], True), (['pnpm', '-r', 'i'], True),
    (['pnpm', '--dir', '/project', 'add', 'dep'], True),
    (['pnpm', 'up'], True), (['pnpm', 'remove', 'dep'], True),
    (['pnpm', 'fetch'], True), (['pnpm', 'import'], True),
    (['pnpm', 'dlx', 'tool'], True), (['pnpm', 'rebuild'], True),
    (['pnpm', 'store', 'prune'], True), (['pnpm', 'store', 'add', 'dep'], True),
    (['node', '--require', '/tracer.js', '/bin/pnpm.cjs', 'install'], True),
    (['corepack', 'pnpm', '--filter=x', 'install'], True),
    (['pnpm install'], True),
])
def test_pnpm_only_store_writers_block(args, busy):
    assert load_upkeep('pnpm_store_prune').installing(args) is busy


@pytest.fixture
def multi_store_case(tmp_path, monkeypatch):
    p = load_upkeep('pnpm_store_prune')
    root = tmp_path / 'store'
    for version in ('v3','v10','v11'):
        (root/version).mkdir(parents=True)
        (root/version/'cached').write_bytes(b'x'*8192)
    bindir=tmp_path/'bin';bindir.mkdir();state=tmp_path/'state.json';state.write_text('{}')
    body=r'''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
s=json.load(open(os.environ['MULTI_STATE']));a=sys.argv[1:];major=12
if Path(sys.argv[0]).name=='npx':
    major=int(a[1].split('@')[1]);a=a[2:]
versions={9:'v3',10:'v10',12:'v11'}
s.setdefault('calls',[]).append({'major':major,'args':a});json.dump(s,open(os.environ['MULTI_STATE'],'w'))
if s.get('unavailable')==major:sys.exit(1)
if a==['--version']:print(str(major)+'.0.0');sys.exit(0)
root=Path(os.environ['MULTI_ROOT'])
if '--store-dir' in a:
    at=a.index('--store-dir');root=Path(a[at+1]);a=a[:at]+a[at+2:]
version=versions[major]
if s.get('mismatch')==major:version='v99'
if a==['store','path']:print(root/version)
elif a==['store','prune']:
    (root/version/'cached').unlink(missing_ok=True)
    if s.get('prune_failure')==major:sys.exit(1)
else:sys.exit(2)
'''
    for name in ('pnpm','npx'):
        f=bindir/name;f.write_text(body);f.chmod(0o755)
    monkeypatch.setattr(p.ws,'ENV',dict(p.ws.ENV,PATH=str(bindir)+':'+os.environ['PATH'],MULTI_ROOT=str(root),MULTI_STATE=str(state)))
    monkeypatch.setattr(p,'package_process_state',lambda:(1,'idle'))
    return p,root,state,tmp_path/'logs'


def test_pnpm_all_versions_use_same_explicit_root_and_matching_major(multi_store_case):
    p,root,state,logs=multi_store_case
    assert p.main(['--execute','--state-dir',str(logs)])==0
    assert all(not (root/v/'cached').exists() for v in ('v3','v10','v11'))
    rows=[json.loads(s) for s in next(logs.glob('pnpm*.jsonl')).read_text().splitlines()]
    assert {r['version']:r['pnpm_major'] for r in rows}=={'v3':9,'v10':10,'v11':12}
    assert all(r['before_bytes']>r['after_bytes'] and r['result']=='pruned' for r in rows)
    calls=json.loads(state.read_text())['calls']
    for c in calls:
        if c['args'][-2:]==['store','prune']:
            assert c['args'][:2]==['--store-dir',str(root)]


@pytest.mark.parametrize('failure', ['unavailable','mismatch','prune_failure'])
def test_pnpm_version_failures_preserve_or_log_and_continue(multi_store_case,failure):
    p,root,state,logs=multi_store_case
    state.write_text(json.dumps({failure:9}))
    result=p.main(['--execute','--state-dir',str(logs)])
    assert result==(1 if failure=='prune_failure' else 0)
    rows=[json.loads(s) for s in next(logs.glob('pnpm*.jsonl')).read_text().splitlines()]
    row=next(r for r in rows if r['version']=='v3')
    assert row['result']==('failed' if failure=='prune_failure' else 'skipped')
    assert row.get('reason')
    assert all(not (root/v/'cached').exists() for v in ('v10','v11'))
    assert (root/'v3'/'cached').exists() is (failure!='prune_failure')
    assert row['after_bytes'] is not None


def test_pnpm_multi_store_dry_run_and_unknown_do_not_prune(multi_store_case,monkeypatch,capsys):
    p,root,state,logs=multi_store_case
    assert p.main(['--quiet','--state-dir',str(logs)])==0
    assert all((root/v/'cached').exists() for v in ('v3','v10','v11'))
    rows=[json.loads(s) for s in next(logs.glob('pnpm*.jsonl')).read_text().splitlines()]
    assert {r['version'] for r in rows}=={'v3','v10','v11'}
    monkeypatch.setattr(p,'package_process_state',lambda:(2,'unknown proc'))
    assert p.main(['--execute','--quiet','--state-dir',str(logs)])==0
    assert all((root/v/'cached').exists() for v in ('v3','v10','v11'))
    assert not capsys.readouterr().out


@pytest.fixture
def lint_case(tmp_path,monkeypatch):
    p=_load_go_cache_trim()
    go=tmp_path/'go';go.mkdir();(go/'README').write_text(README_LINE)
    lint=tmp_path/'lint';lint.mkdir();(lint/'README').write_text('This directory holds cached build artifacts from golangci-lint.\n')
    link=tmp_path/'golangci-lint';link.symlink_to(lint,target_is_directory=True)
    monkeypatch.setattr(p,'cache_directory',lambda:str(go))
    monkeypatch.setattr(p,'lint_cache_directory',lambda:os.path.realpath(link),raising=False)
    for bucket,age in [('aa',25),('bb',23)]:
        d=lint/bucket;d.mkdir();f=d/(bucket*32+'-d');f.write_bytes(b'x'*1024)
        os.utime(f,(time.time()-age*3600,)*2)
    return p,go,lint


def test_golangci_shared_cache_follows_root_symlink_and_preserves_recent(lint_case,capsys):
    p,go,lint=lint_case
    assert p.main([])==0
    assert 'golangci-lint' in capsys.readouterr().out
    old=lint/'aa'/('aa'*32+'-d');recent=lint/'bb'/('bb'*32+'-d')
    assert old.exists()
    assert p.main(['--execute'])==0
    assert not old.exists() and recent.exists()


@pytest.mark.parametrize('bad', ['readme','symlink','non_matching'])
def test_golangci_invalid_targets_are_preserved(lint_case,bad):
    p,go,lint=lint_case
    old=lint/'aa'/('aa'*32+'-d')
    (lint/'cc').mkdir(); buddy=lint/'cc'/('cc'*32+'-a'); buddy.write_bytes(b'old buddy')
    os.utime(buddy,(OLD,OLD))
    if bad=='readme':(lint/'README').write_text('wrong cache\n')
    elif bad=='symlink':
        old.unlink();outside=lint.parent/'outside';outside.write_text('keep');old.symlink_to(outside)
    else:
        old.rename(old.parent/'notes.txt');old=old.parent/'notes.txt'
    assert p.main(['--execute','--quiet'])==(1 if bad=='readme' else 0)
    assert old.exists()
    assert buddy.exists() is (bad=='readme')


def test_pnpm_writer_executable_path_can_contain_spaces():
    assert load_upkeep('pnpm_store_prune').installing(['/tools with space/pnpm','install'])


def test_npm_process_title_is_ignored_even_with_unmatched_quotes():
    assert not load_upkeep('pnpm_store_prune').installing(["npm exec vite --define='unterminated"])


def test_pnpm_process_parse_error_is_unknown(monkeypatch):
    p=load_upkeep('pnpm_store_prune')
    monkeypatch.setattr(p.os,'listdir',lambda _:['123'])
    monkeypatch.setattr(p,'installing',mock.Mock(side_effect=ValueError('bad title')))
    with mock.patch('builtins.open',mock.mock_open(read_data=b'pnpm\0install\0')):
        assert p.package_process_state()[0]==2


def test_pnpm_old_stores_are_pruned_when_current_version_directory_is_absent(multi_store_case):
    p,root,state,logs=multi_store_case
    shutil.rmtree(root/'v11')
    assert p.main(['--execute','--state-dir',str(logs)])==0
    assert all(not (root/v/'cached').exists() for v in ('v3','v10'))
    rows=[json.loads(s) for s in next(logs.glob('pnpm*.jsonl')).read_text().splitlines()]
    assert {r['version'] for r in rows}=={'v3','v10'}


@pytest.mark.parametrize('args', [
    ['pnpm','--npmrc-auth-file','/auth','install'],
    ['pnpm','--https-proxy','http://proxy','fetch'],
])
def test_pnpm_native_rc_option_operands_do_not_hide_store_writer(args):
    assert load_upkeep('pnpm_store_prune').installing(args)


@pytest.mark.parametrize('args,busy', [
    *[(['pnpm', command], True) for command in
      ('ci', 'clean-install', 'install-clean', 'ic', 'install-test', 'it', 'pipeline', 'create')],
    *[(['pnpm', wrapper, 'install'], True) for wrapper in ('recursive', 'multi', 'm')],
    *[(['pnpm', wrapper, 'run', 'install'], False) for wrapper in ('recursive', 'multi', 'm')],
    (['pnpm', 'recursive', '--filter', 'install', 'exec', 'vite'], False),
    (['pnpm', 'recursive', '--filter', 'run', 'install'], True),
    (['pnpx', 'vite'], True), (['pnx', 'vite'], True),
    (['node', '/usr/bin/pnpx.cjs', 'vite'], True),
    (['pnpx vite'], True), (['corepack', 'pnpx', 'vite'], True),
    (['pnpm', 'pnpx', 'vite'], True), (['pnpm', 'pnx', 'vite'], True),
])
def test_pnpm_writer_aliases_and_recursive_script_consumers(args, busy):
    assert load_upkeep('pnpm_store_prune').installing(args) is busy


@pytest.mark.parametrize('flag,value', [
    ('--node-linker', 'hoisted'), ('--lockfile-dir', '/tmp/lock'),
    ('--cpu', 'x64'), ('--os', 'linux'), ('--libc', 'glibc'),
    ('--fetch-timeout', '10000'), ('--fetch-warn-timeout-ms', '1000'),
    ('--fetch-min-speed-ki-bps', '10'), ('--user-agent', 'install'),
    ('--pnpr-server', 'http://localhost'), ('--allow-build', 'sharp'),
    ('--merge-git-branch-lockfiles-branch-pattern', 'main'),
])
def test_pnpm_value_options_before_command_preserve_writer_and_consumer(flag, value):
    p = load_upkeep('pnpm_store_prune')
    assert p.installing(['pnpm', flag, value, 'install'])
    assert not p.installing(['pnpm', flag, value, 'run', 'install'])


@pytest.mark.parametrize('args,busy', [
    (['pnpm', '--', 'install'], True), (['pnpm', '--', 'run', 'install'], False),
    (['node', '--', '/usr/bin/pnpm.cjs', 'install'], True),
    (['node', '--env-file', '/tmp/env', '/usr/bin/pnpm.cjs', 'install'], True),
    (['node', '--title', 'install', '/usr/bin/pnpm.cjs', 'install'], True),
    (['node', '--title', 'install', '/usr/bin/pnpm.cjs', 'run', 'install'], False),
])
def test_pnpm_node_options_and_option_terminator(args, busy):
    assert load_upkeep('pnpm_store_prune').installing(args) is busy


@pytest.mark.parametrize('args', [
    ['pnpm', alias] for alias in ('rb', 'uni', 'ln', 'dislink', 'prune')
] + [['pnpm', 'pm', 'install']])
def test_pnpm_verified_remaining_writer_aliases(args):
    assert load_upkeep('pnpm_store_prune').installing(args)


@pytest.fixture
def watermark_case(tmp_path, monkeypatch):
    w = load_upkeep('storage_watermark_brake')
    monkeypatch.setattr(w, 'STATE', tmp_path / 'state' / 'watermark.json')
    monkeypatch.setattr(w, 'available', lambda disk: 200 * w.GIB)
    calls = []
    def fake_bb(*args):
        calls.append(args)
        return '[]' if args[:2] == ('automation', 'runs') else '{}'
    monkeypatch.setattr(w, 'bb', fake_bb)
    # No test can escape to the real clean-tmp launcher or any other command.
    monkeypatch.setattr(w.subprocess, 'run', mock.Mock(side_effect=AssertionError('unmocked command')))
    return w, calls


@pytest.mark.parametrize('gib,expected', [(0, 'danger'), (49, 'danger'), (50, 'warning'),
                                        (149, 'warning'), (150, None), (200, None)])
def test_watermark_thresholds(gib, expected):
    w = load_upkeep('storage_watermark_brake')
    assert w.level(gib * w.GIB) == expected


@pytest.mark.parametrize('disk,count', [('/', 1), ('/data', 6)])
def test_watermark_selects_only_low_disk_and_prints_trigger_summary(watermark_case, monkeypatch, capsys, disk, count):
    w, calls = watermark_case
    monkeypatch.setattr(w, 'available', lambda p: (100 if p == disk else 200) * w.GIB)
    invoked = []
    monkeypatch.setattr(w, 'trigger', lambda aid: invoked.append(aid) or 'triggered')
    assert w.main([]) == 0
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1 and f'{disk} warning' in output
    assert invoked == [aid for _, aid in w.TASKS[disk]] and len(invoked) == count
    assert len(calls) == 1 and calls[0][:3] == ('thread', 'tell', 'thr_tikzbd34dg')


def test_watermark_healthy_is_silent_and_does_not_create_state(watermark_case, capsys):
    w, calls = watermark_case
    assert w.main([]) == 0
    assert capsys.readouterr().out == '' and not calls
    assert not w.STATE.parent.exists()


def test_watermark_cooldown_silent_and_expiry_retriggers(watermark_case, monkeypatch, capsys):
    w, calls = watermark_case
    now = [100000]
    monkeypatch.setattr(w.time, 'time', lambda: now[0])
    monkeypatch.setattr(w, 'available', lambda _: 100 * w.GIB)
    fired = []
    monkeypatch.setattr(w, 'trigger', lambda aid: fired.append(aid) or 'triggered')
    assert w.main([]) == 0
    capsys.readouterr()
    snapshot = w.STATE.read_bytes()
    for minutes in (15, 30, 60, 119):
        now[0] = 100000 + minutes * 60
        assert w.main([]) == 0
        assert capsys.readouterr().out == ''
        assert w.STATE.read_bytes() == snapshot
    assert len(calls) == 1 and len(fired) == 6
    now[0] = 100000 + 7200
    assert w.main([]) == 0
    assert 'warning' in capsys.readouterr().out
    assert len(calls) == 2 and len(fired) == 12


def test_watermark_escalation_notifies_but_downgrade_stays_silent(watermark_case, monkeypatch, capsys):
    w, calls = watermark_case
    free = [100]
    monkeypatch.setattr(w, 'available', lambda _: free[0] * w.GIB)
    monkeypatch.setattr(w, 'trigger', lambda _: 'triggered')
    assert w.main([]) == 0
    capsys.readouterr()
    free[0] = 40
    assert w.main([]) == 0
    assert 'danger' in capsys.readouterr().out and len(calls) == 2
    free[0] = 100
    assert w.main([]) == 0
    assert capsys.readouterr().out == '' and len(calls) == 2


@pytest.mark.parametrize('existing', [False, True])
def test_watermark_preview_has_zero_bb_calls_and_zero_state_changes(watermark_case, monkeypatch, capsys, existing):
    w, calls = watermark_case
    monkeypatch.setattr(w, 'available', lambda _: 40 * w.GIB)
    if existing:
        w.STATE.parent.mkdir()
        w.STATE.write_text('{"/": {"level": "warning", "ts": 1}}\n')
    before = w.STATE.read_bytes() if existing else None
    assert w.main(['--dry-run']) == 0
    assert 'would trigger' in capsys.readouterr().out
    assert not calls and w.subprocess.run.call_count == 0
    assert (w.STATE.read_bytes() if w.STATE.exists() else None) == before
    if not existing:
        assert not w.STATE.parent.exists()


@pytest.mark.parametrize('reply', ['bad json', '{}', '[{}]', '[{"status":"mystery"}]', None])
def test_watermark_query_unknown_never_triggers(watermark_case, monkeypatch, capsys, reply):
    w, calls = watermark_case
    def fake_bb(*args):
        calls.append(args)
        if args[:2] == ('automation', 'runs'):
            if reply is None:
                raise subprocess.TimeoutExpired('bb', 8)
            return reply
        return '{}'
    monkeypatch.setattr(w, 'bb', fake_bb)
    monkeypatch.setattr(w, 'TASKS', {'/': [], '/data': [('docker', 'auto-stub')]})
    monkeypatch.setattr(w, 'available', lambda p: (40 if p == '/data' else 200) * w.GIB)
    assert w.main([]) == 0
    assert 'unknown (查询失败，跳过)' in capsys.readouterr().out
    assert not any(c[:2] == ('automation', 'run') for c in calls)


def test_watermark_running_task_is_skipped(watermark_case, monkeypatch):
    w, calls = watermark_case
    monkeypatch.setattr(w, 'bb', lambda *a: calls.append(a) or '[{"status":"running"}]')
    assert w.trigger('auto-stub') == 'running'
    assert len(calls) == 1 and calls[0][1] == 'runs'


def test_watermark_internal_failure_is_nonzero(watermark_case, monkeypatch, capsys):
    w, calls = watermark_case
    monkeypatch.setattr(w, 'available', lambda p: (40 if p == '/' else 200) * w.GIB)
    monkeypatch.setattr(w, 'trigger', mock.Mock(side_effect=OSError('launcher failed')))
    assert w.main([]) == 1
    assert 'failed (launcher failed)' in capsys.readouterr().out


@pytest.fixture
def runner_case(tmp_path, monkeypatch):
    r = load_upkeep('clean_tmp_runner')
    for name, path in [('LOCK', 'cleanup.lock'), ('LOG', 'ledger.jsonl'), ('STDERR', 'stderr.log')]:
        monkeypatch.setattr(r, name, tmp_path / path)
    monkeypatch.setattr(r.subprocess, 'run', mock.Mock(side_effect=AssertionError('real cleanup forbidden')))
    monkeypatch.setattr(r.subprocess, 'Popen', mock.Mock(side_effect=AssertionError('real cleanup forbidden')))
    return r


def test_clean_tmp_busy_lock_skips_without_launch(runner_case, capsys):
    import fcntl
    r = runner_case
    with r.LOCK.open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert r.main(['--launch', '--path', '/var/tmp']) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'lock'
    assert not r.LOG.exists() and r.subprocess.Popen.call_count == r.subprocess.run.call_count == 0


def test_clean_tmp_detached_child_keeps_inherited_lock(runner_case, monkeypatch, capsys):
    import fcntl
    r = runner_case
    inherited = []
    def launch(cmd, **kw):
        assert kw['start_new_session'] is True
        assert cmd[0] == '/usr/bin/timeout' and '1790' in cmd and '-I' in cmd
        fd, = kw['pass_fds']
        inherited.append(os.dup(fd))  # model the child inheriting the open file description
        return mock.Mock(pid=42)
    monkeypatch.setattr(r.subprocess, 'Popen', launch)
    try:
        assert r.main(['--launch', '--path', '/data/system-tmp']) == 0
        assert json.loads(capsys.readouterr().out)['status'] == 'triggered'
        with r.LOCK.open() as another:
            with pytest.raises(BlockingIOError):
                fcntl.flock(another, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        for fd in inherited:
            os.close(fd)
    with r.LOCK.open() as another:
        fcntl.flock(another, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_clean_tmp_cron_and_single_root_share_commands_budgets_and_ledger(runner_case, monkeypatch):
    import fcntl
    r = runner_case
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        with r.LOCK.open() as another:
            with pytest.raises(BlockingIOError):
                fcntl.flock(another, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert kw['pass_fds'] and kw['timeout'] <= 1790
        return subprocess.CompletedProcess(cmd, 0, '{"event":"summary","status":"ok"}\n', '')
    monkeypatch.setattr(r.subprocess, 'run', run)
    assert r.main([]) == 0
    assert [c[3] for c in calls] == ['325', '1435']
    assert all('-B' in c and '--execute' in c and '--json' in c for c in calls)
    single = calls[1]
    calls.clear()
    assert r.main(['--path', '/data/system-tmp']) == 0
    assert calls == [single]
    rows = [json.loads(line) for line in r.LOG.read_text().splitlines()]
    assert sum(row['event'] == 'runner_finish' for row in rows) == 3
    assert all(row['returncode'] == 0 and 'summary' in row['output']
               for row in rows if row['event'] == 'runner_finish')


@pytest.mark.parametrize('failure', ['timeout', 'failed'])
def test_growth_failed_measurement_is_unknown_without_bytes(monkeypatch, failure):
    g = load_upkeep('storage_growth_measure')
    monkeypatch.setattr(g.os, 'lstat', lambda _: mock.Mock(st_mode=0o40700))
    fake = (mock.Mock(side_effect=subprocess.TimeoutExpired('du-stub', 0.01)) if failure == 'timeout'
            else mock.Mock(return_value=subprocess.CompletedProcess([], 1, '999', 'denied')))
    monkeypatch.setattr(g.subprocess, 'run', fake)
    row = g.measure('/stub', time.monotonic() + 1)
    assert row['status'] == 'unknown' and 'bytes' not in row
    assert 'error' in row


def test_growth_discovery_measures_children_not_data_aggregate(tmp_path, monkeypatch):
    g = load_upkeep('storage_growth_measure')
    listings = {'/data': ['code', 'system-tmp'], '/data/system-tmp': ['clone', 'cache'], '/root/.bb': ['logs', 'plugins']}
    monkeypatch.setattr(g.Path, 'iterdir', lambda p: [p / name for name in listings[str(p)]])
    roots, errors = g.discover()
    assert not errors and '/data' not in roots and '/root/.bb' not in roots
    assert {'/data/code', '/data/system-tmp/clone', '/root/.bb/plugins',
            '/var/tmp', '/root/.cache', '/data/bb-data/thread-storage'} <= set(roots)


def test_growth_compare_top5_ownership_and_unknown_exclusion():
    g = load_upkeep('storage_growth_measure')
    rules = g.fingerprints()
    assert g.owner('/data/itfs', rules) == 'itfs'
    assert g.owner('/data/system-tmp/oc-local-smoke-123', rules) == 'opencraft'
    assert g.owner('/data/unregistered', rules) == 'unknown'
    rows = [{'root': f'/root/{n}', 'status': 'ok', 'bytes': n * g.GIB, 'owner': 'unknown'} for n in range(7)]
    before = {'ts': 100, 'rows': [{**row, 'bytes': 0} for row in rows]}
    before['rows'][6]['status'] = 'unknown'
    rows.append({'root': '/unknown-now', 'status': 'unknown', 'owner': 'unknown'})
    before['rows'].append({'root': '/unknown-now', 'status': 'ok', 'bytes': 999})
    compared, top5 = g.compare(rows, before, 100 + 43200)
    assert [row['root'] for row in top5] == [f'/root/{n}' for n in (5, 4, 3, 2, 1)]
    assert top5[0]['daily_bytes'] == 10 * g.GIB
    assert '/root/6' not in {r['root'] for r in compared}
    assert '/unknown-now' not in {r['root'] for r in compared}


def test_growth_retention_uses_sample_time_and_keeps_latest(tmp_path):
    g = load_upkeep('storage_growth_measure')
    now = 50 * 86400
    for name, age in [('expired.json', 31), ('boundary.json', 30), ('recent.json', 2), ('latest.json', 40)]:
        (tmp_path / name).write_text(json.dumps({'ts': now - age * 86400}))
    g.retain(tmp_path, now)
    assert {p.name for p in tmp_path.iterdir()} == {'boundary.json', 'recent.json', 'latest.json'}


def test_growth_main_compares_previous_persists_top5_and_alerts(tmp_path, monkeypatch):
    g = load_upkeep('storage_growth_measure')
    monkeypatch.setattr(g, 'OUT', tmp_path)
    monkeypatch.setattr(g, 'discover', lambda: (['/data/itfs', '/new'], []))
    monkeypatch.setattr(g, 'measure', lambda root, deadline: {'root': root, 'status': 'ok', 'bytes': 30 * g.GIB})
    monkeypatch.setattr(g.time, 'time', lambda: 86401)
    (tmp_path / 'latest.json').write_text(json.dumps({'ts': 1, 'rows': [
        {'root': root, 'status': 'ok', 'bytes': 0} for root in ('/data/itfs', '/new')]}))
    assert g.main([]) == 0
    result = json.loads((tmp_path / 'latest.json').read_text())
    assert len(result['top5']) == 2 and result['unknown_count'] == 0
    assert {row['owner'] for row in result['top5']} == {'itfs', 'unknown'}
    assert len(result['alerts']) == 2 and result['interval_seconds'] == 86400


def test_growth_overall_budget_bounds_queued_roots(tmp_path, monkeypatch):
    g = load_upkeep('storage_growth_measure')
    monkeypatch.setattr(g, 'OUT', tmp_path)
    monkeypatch.setattr(g, 'TOTAL_SECONDS', 0.05)
    monkeypatch.setattr(g, 'PER_ROOT_SECONDS', 0.02)
    monkeypatch.setattr(g, 'WORKERS', 2)
    monkeypatch.setattr(g, 'discover', lambda: ([f'/stub/{i}' for i in range(20)], []))
    monkeypatch.setattr(g.os, 'lstat', lambda _: mock.Mock(st_mode=0o40700))
    def run(cmd, **kw):
        time.sleep(kw['timeout'])
        raise subprocess.TimeoutExpired('stub', kw['timeout'])
    monkeypatch.setattr(g.subprocess, 'run', run)
    start = time.monotonic()
    assert g.main([]) == 0
    assert time.monotonic() - start < 1
    result = json.loads((tmp_path / 'latest.json').read_text())
    assert result['unknown_count'] == 21 and not result['comparisons']
    assert any(row.get('error') == 'total budget exhausted' for row in result['rows'])


def test_watermark_reports_shared_launcher_lock_skip(watermark_case, monkeypatch):
    w, calls = watermark_case
    launcher = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '{"status":"lock"}\n', ''))
    monkeypatch.setattr(w.subprocess, 'run', launcher)
    assert w.trigger('/var/tmp') == 'lock'
    command = launcher.call_args.args[0]
    assert command[0:2] == ['/usr/bin/python3', '-I']
    assert command[-3:] == ['--launch', '--path', '/var/tmp']
    assert command[2].endswith('/clean_tmp_runner.py') and not calls


def test_watermark_both_disks_use_one_locked_worker(watermark_case, runner_case, monkeypatch, capsys):
    import fcntl
    w, calls = watermark_case
    r = runner_case
    monkeypatch.setattr(w, 'available', lambda _: 40 * w.GIB)
    inherited, launches = [], []
    def launch(cmd, **kw):
        launches.append(cmd)
        inherited.append(os.dup(kw['pass_fds'][0]))
        return mock.Mock(pid=42)
    monkeypatch.setattr(r.subprocess, 'Popen', launch)
    def run_runner(cmd, **kw):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            assert r.main(cmd[3:]) == 0
        return subprocess.CompletedProcess(cmd, 0, out.getvalue(), '')
    monkeypatch.setattr(w.subprocess, 'run', run_runner)
    try:
        assert w.main([]) == 0
        assert len(launches) == 1 and '--path' not in launches[0]
        output = capsys.readouterr().out
        assert 'clean-tmp /var/tmp=triggered' in output
        assert 'clean-tmp /data/system-tmp=triggered' in output
        assert '=lock' not in output
        with r.LOCK.open() as contender:
            with pytest.raises(BlockingIOError):
                fcntl.flock(contender, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        for fd in inherited:
            os.close(fd)


def test_watermark_notification_failure_preserves_cooldown(watermark_case, monkeypatch, capsys):
    w, calls = watermark_case
    monkeypatch.setattr(w, 'available', lambda _: 40 * w.GIB)
    fired = []
    monkeypatch.setattr(w, 'trigger', lambda aid: fired.append(aid) or 'triggered')
    monkeypatch.setattr(w, 'bb', mock.Mock(side_effect=subprocess.TimeoutExpired('notification-stub', 8)))
    with pytest.raises(subprocess.TimeoutExpired):
        w.main([])
    assert w.STATE.exists() and len(fired) == 6
    capsys.readouterr()
    snapshot = w.STATE.read_bytes()
    assert w.main([]) == 0
    assert capsys.readouterr().out == '' and len(fired) == 6
    assert w.STATE.read_bytes() == snapshot and w.bb.call_count == 1


def test_clean_tmp_executor_preserves_same_directory_imports_without_pycache(runner_case, tmp_path, monkeypatch):
    r = runner_case
    executor = tmp_path / 'devcloud.py'
    executor.write_text('from devcloud_tmp import value\nprint(value)\n')
    (tmp_path / 'devcloud_tmp.py').write_text('value = "executor-dependency-ok"\n')
    monkeypatch.setattr(r, 'EXECUTOR', str(executor))
    # Use only our local harmless executor stub, never the real cleanup.
    command = r.command('/var/tmp')
    # subprocess is patched by runner_case; restore just for this harmless stub.
    monkeypatch.setattr(r.subprocess, 'Popen', _REAL_STORAGE_POPEN)
    result = _REAL_STORAGE_RUN(command, capture_output=True, text=True, check=True)
    assert result.stdout == 'executor-dependency-ok\n'
    assert not (tmp_path / '__pycache__').exists()


def test_growth_plain_files_use_allocated_blocks_without_du(tmp_path, monkeypatch):
    g = load_upkeep('storage_growth_measure')
    sparse = tmp_path / 'sparse'
    with sparse.open('wb') as stream:
        stream.truncate(100 * 1024 * 1024)
    link = tmp_path / 'link'
    link.symlink_to(sparse)
    monkeypatch.setattr(g.subprocess, 'run', mock.Mock(side_effect=AssertionError('du fork for plain file')))
    for path in (sparse, link):
        row = g.measure(str(path), time.monotonic() + 1)
        assert row['status'] == 'ok' and row['bytes'] == path.lstat().st_blocks * 512
        assert row['bytes'] < sparse.stat().st_size
    assert g.subprocess.run.call_count == 0


def test_growth_critical_roots_start_before_large_tmp_inventory(monkeypatch):
    g = load_upkeep('storage_growth_measure')
    listings = {'/data': ['code'], '/root/.bb': ['logs'], '/data/system-tmp': [f'file-{i}' for i in range(10000)]}
    monkeypatch.setattr(g.Path, 'iterdir', lambda p: [p / name for name in listings[str(p)]])
    roots, _ = g.discover()
    assert roots[:5] == ['/var/tmp', '/root/.cache', '/data/bb-data/thread-storage', '/data/code', '/root/.bb/logs']
    assert len(roots) == 10005
