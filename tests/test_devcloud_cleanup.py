"""Behavioral regressions for the Linux temporary cleanup boundary.

Run: python3 -B -m unittest discover -s tests -p test_devcloud_cleanup.py -v
Uses private temporary fixtures; subprocess probes never delete host candidates.
"""

import contextlib
import fcntl
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location('devcloud_tmp', Path(__file__).resolve().parents[1] / 'bin/devcloud_tmp.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


@unittest.skipUnless(sys.platform.startswith('linux'), 'Linux /proc and renameat2 contract')
class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='mole-devcloud-test-', dir='/var/tmp')
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'tmp'
        self.root.mkdir()
        self.state = self.base / 'state'
        self.state.mkdir()
        self.proc = self.base / 'proc'
        self.proc.mkdir()
        self.cutoff = time.time() - 7 * 86400
        self.live = {'reserved': set(), 'watches': set(), 'processes': []}
        self.patches = [mock.patch.object(m, key, value) for key, value in
                        [('TMP_ROOT', self.root), ('DATA_ROOT', self.base),
                         ('STATE', self.state), ('LOG', self.base / 'audit.jsonl')]]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        # CI fixtures stand in for the daemon's root-owned files without chown or sudo.
        real_lstat = Path.lstat
        real_fstat = os.fstat

        def fixture_owner(path, result):
            if os.geteuid() == 0 or not m.beneath(str(path), str(self.base)):
                return result
            from types import SimpleNamespace
            fields = {key: getattr(result, key) for key in dir(result) if key.startswith('st_')}
            fields['st_uid'] = 0
            return SimpleNamespace(**fields)

        def fixture_lstat(path):
            return fixture_owner(path, real_lstat(path))

        def fixture_fstat(fd):
            return fixture_owner(os.readlink(f'/proc/self/fd/{fd}'), real_fstat(fd))

        for patch in (mock.patch.object(Path, 'lstat', fixture_lstat),
                      mock.patch.object(os, 'fstat', fixture_fstat)):
            patch.start()
            self.addCleanup(patch.stop)

    def old(self, name, content='fixture'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        for item in (path, *path.parents):
            if not m.beneath(str(item), str(self.root)):
                break
            os.utime(item, (self.cutoff - 86400, self.cutoff - 86400))
        return path

    def tree(self, path, watches=()):
        return m.tree_snapshot(path, self.cutoff, watches, time.monotonic() + 5)

    def plan(self):
        with mock.patch.object(m, 'process_snapshot', return_value=self.live):
            return m.build_plan(self.root, self.cutoff, time.monotonic() + 10)

    def candidate(self, name='old.log'):
        self.old(name)
        return {'name': name, **self.tree(self.root / name)}

    def fake_process(self, pid=900001, cwd=None):
        proc = self.proc / str(pid)
        proc.mkdir()
        (proc / 'fd').mkdir()
        (proc / 'fdinfo').mkdir()
        (proc / 'ns').mkdir()
        os.symlink('mnt:[fixture]', proc / 'ns/mnt')
        os.symlink(str(cwd or self.base), proc / 'cwd')
        os.symlink('/usr/bin/python3', proc / 'exe')
        (proc / 'maps').write_text('')
        (proc / 'comm').write_text('fixture\n')
        (proc / 'cmdline').write_bytes(b'fixture\0')
        fields = ['S', '1', '1', '1', '0', '0', '0'] + ['0'] * 12 + ['12345']
        (proc / 'stat').write_text(f'{pid} (fixture) ' + ' '.join(fields))
        device = self.root.stat().st_dev
        dev = f'{os.major(device)}:{os.minor(device)}'
        (proc / 'mountinfo').write_text(f'1 0 {dev} / {self.base} rw - ext4 fixture rw\n'
                                       f'2 1 {dev} /tmp /tmp rw - ext4 fixture rw\n')
        return proc

    def probe(self, root=None):
        with mock.patch.object(m, 'PROC_ROOT', self.proc), mock.patch.object(m.os, 'getppid', return_value=1):
            return m.process_snapshot(root or self.root, time.monotonic() + 5)

    def run_cli(self, args, **overrides):
        stack = contextlib.ExitStack()
        with stack, contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()):
            for name, value in overrides.items():
                stack.enter_context(mock.patch.object(m, name, value))
            code = m.main(args + ['--json'])
        return code, json.loads(out.getvalue())

    def var_tmp_cli(self):
        # Approved root and staging live on the fixture filesystem; real planning,
        # isolation, receipts and final guards run with host evidence and Mole stubbed.
        def sink(receipt, item, preview, deadline=None):
            if not preview:
                item.unlink()  # Only the fixture this test created.
        with mock.patch.object(m, 'TMP_ROOT', self.base / 'data-tmp'), \
                mock.patch.object(m, 'VAR_TMP_ROOT', self.root, create=True), \
                mock.patch.object(m, 'VAR_STATE', self.state, create=True), \
                mock.patch.object(m, 'trusted'), \
                mock.patch.object(m, 'validate_data_mount', create=True), \
                mock.patch.object(m, 'process_snapshot', return_value=self.live), \
                mock.patch.object(m, 'owner_delete', side_effect=sink):
            return self.run_cli(['--path', str(self.root), '--execute'])

    def test_var_tmp_systemd_private_is_retained(self):
        path = self.old('systemd-private-fixture')
        code, output = self.var_tmp_cli()
        self.assertEqual(code, 0, output)
        self.assertEqual(output['path'], str(self.root))
        self.assertEqual(output['skipped']['protected name'], 1)
        self.assertTrue(path.exists())

    def test_var_tmp_git_tree_is_retained(self):
        path = self.old('repository/.git')
        code, output = self.var_tmp_cli()
        self.assertEqual(code, 0, output)
        self.assertEqual(output['path'], str(self.root))
        self.assertEqual(output['skipped']['repository or persistent state'], 1)
        self.assertTrue(path.exists())

    def test_var_tmp_recent_tree_is_retained(self):
        path = self.old('recent/new.log')
        path.write_text('recent write')
        code, output = self.var_tmp_cli()
        self.assertEqual(code, 0, output)
        self.assertEqual(output['path'], str(self.root))
        self.assertEqual(output['skipped']['recent descendant'], 1)
        self.assertTrue(path.exists())

    def test_var_tmp_live_reference_is_retained(self):
        path = self.old('active.log')
        self.live['reserved'].add('active.log')
        code, output = self.var_tmp_cli()
        self.assertEqual(code, 0, output)
        self.assertEqual(output['path'], str(self.root))
        self.assertEqual(output['skipped']['live or mount reference'], 1)
        self.assertTrue(path.exists())

    def test_var_tmp_eligible_item_is_deleted(self):
        path = self.old('eligible.log')
        code, output = self.var_tmp_cli()
        self.assertEqual(code, 0, output)
        self.assertEqual(output['path'], str(self.root))
        self.assertEqual(output['deleted'], 1)
        self.assertFalse(path.exists())

    def test_var_tmp_unknown_process_evidence_denies_execution(self):
        path = self.old('unknown.log')
        with mock.patch.object(m, 'VAR_TMP_ROOT', self.root), \
                mock.patch.object(m, 'VAR_STATE', self.state), mock.patch.object(m, 'trusted'), \
                mock.patch.object(m, 'validate_data_mount', create=True), \
                mock.patch.object(m, 'process_snapshot', side_effect=m.Refused('unknown PID')):
            code, output = self.run_cli(['--path', str(self.root), '--execute'])
        self.assertEqual(code, 2)
        self.assertEqual(output['scan'], 'incomplete')
        self.assertEqual(output['deleted'], 0)
        self.assertTrue(path.exists())

    def test_var_tmp_policy_uses_its_own_whitelist_and_not_tmp_alias(self):
        self.old('keep.log')
        self.old('eligible.log')
        config = self.base / '.config/mole'
        config.mkdir(parents=True)
        (config / 'whitelist').write_text(f'{self.root}/keep.log\n/tmp/eligible.log\n')
        with mock.patch.dict(os.environ, {'HOME': str(self.base)}):
            code, output = self.var_tmp_cli()
        self.assertEqual(code, 0, output)
        self.assertEqual(output['deleted'], 1)
        self.assertTrue((self.root / 'keep.log').exists())
        self.assertFalse((self.root / 'eligible.log').exists())

    def test_var_tmp_rejects_symlink_and_nonsticky_writable_root(self):
        for mode, accepted in ((0o700, True), (0o1777, True), (0o777, False)):
            self.root.chmod(mode)
            with mock.patch.object(m, 'VAR_TMP_ROOT', self.root), mock.patch.object(m, 'trusted'), \
                    mock.patch.object(m, 'validate_data_mount', create=True):
                if accepted:
                    self.assertEqual(m.validate_root(self.root), self.root)
                else:
                    with self.assertRaises(m.Refused):
                        m.validate_root(self.root)
        self.root.chmod(0o700)
        alias = self.base / 'alias'
        alias.symlink_to(self.root)
        with mock.patch.object(m, 'VAR_TMP_ROOT', alias), mock.patch.object(m, 'trusted'), \
                mock.patch.object(m, 'validate_data_mount', create=True):
            with self.assertRaises(m.Refused):
                m.validate_root(alias)

    def test_var_tmp_rejects_same_device_bind_mount_root(self):
        device = self.root.stat().st_dev
        dev = f'{os.major(device)}:{os.minor(device)}'
        mountinfo = f'1 0 {dev} / {self.base} rw - ext4 fixture rw\n' + \
                    f'2 1 {dev} /elsewhere {self.root} rw - ext4 fixture rw\n'
        with mock.patch.object(m, 'VAR_TMP_ROOT', self.root), mock.patch.object(m, 'trusted'), \
                mock.patch.object(m, 'validate_data_mount', create=True), \
                mock.patch.object(Path, 'read_text', return_value=mountinfo):
            with self.assertRaises(m.Refused):
                m.validate_root(self.root)

    def test_var_tmp_requires_mounted_data_disk_for_its_audit_log(self):
        with mock.patch.object(m, 'VAR_TMP_ROOT', self.root), mock.patch.object(m, 'trusted'):
            with self.assertRaisesRegex(m.Refused, '/data is not a separate mounted filesystem'):
                m.validate_root(self.root)

    def test_var_tmp_staged_sink_reloads_original_path_whitelist(self):
        stage = self.state / 'item-fixture'
        stage.mkdir()
        item = stage / 'keep.log'
        item.write_text('retained fixture')
        config = self.base / '.config/mole'
        config.mkdir(parents=True)
        (config / 'whitelist').write_text(f'{self.root}/keep.log\n')
        with mock.patch.object(m, 'VAR_TMP_ROOT', self.root), \
                mock.patch.object(m, 'VAR_STATE', self.state), \
                mock.patch.dict(os.environ, {'HOME': str(self.base)}):
            with self.assertRaisesRegex(m.Refused, 'Mole protection or whitelist'):
                m.owner_delete(stage / 'receipt.json', item, preview=False)
        self.assertEqual(item.read_text(), 'retained fixture')

    def test_old_temp_file_is_candidate_and_preview_does_not_remove(self):
        path = self.old('old.log')
        plan, _ = self.plan()
        self.assertEqual([x['name'] for x in plan], ['old.log'])
        code, output = self.run_cli([], validate_root=lambda _: self.root,
                                    build_plan=lambda *args: (plan, {}))
        self.assertEqual(code, 0)
        self.assertEqual(output['candidates'], 1)
        self.assertEqual(path.read_text(), 'fixture')

    def test_old_parent_with_recent_child_is_retained(self):
        self.old('old/new.log')
        (self.root / 'old/new.log').write_text('recent write')
        os.utime(self.root / 'old', (self.cutoff - 1, self.cutoff - 1))
        plan, skipped = self.plan()
        self.assertFalse(plan)
        self.assertEqual(skipped['recent descendant'], 1)

    def test_cwd_and_executable_references_are_detected(self):
        path = self.old('active/executable')
        proc = self.fake_process(cwd=path.parent)
        (proc / 'exe').unlink()
        os.symlink(path, proc / 'exe')
        result = self.probe()
        self.assertIn('active', result['reserved'])
        self.assertEqual(result['processes'][0]['via'], ['cwd', 'exe'])

    def test_bind_alias_maps_reference_to_original_child(self):
        proc = self.fake_process()
        (proc / 'maps').write_text('1-2 r--p 0 0:0 1 /tmp/in-use/library.so\n')
        self.assertIn('in-use', self.probe()['reserved'])

    def test_child_bind_mount_is_reserved_without_open_file(self):
        proc = self.fake_process()
        dev = self.root.stat().st_dev
        with (proc / 'mountinfo').open('a') as out:
            out.write(f'3 1 {os.major(dev)}:{os.minor(dev)} /tmp/mounted /service rw - ext4 fixture rw\n')
        self.assertIn('mounted', self.probe()['reserved'])

    def test_permission_error_is_unknown_not_unused(self):
        proc = self.fake_process()
        original = Path.read_text

        def denied(path, *args, **kwargs):
            if path == proc / 'maps':
                raise PermissionError('fixture denied')
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', denied), self.assertRaises(m.Refused):
            self.probe()

    def test_exiting_process_is_rechecked_before_refusal(self):
        for outcome in ('gone', 'zombie', 'reused'):
            with self.subTest(outcome=outcome):
                proc = self.fake_process(pid=900010 + len(list(self.proc.iterdir())))
                original = Path.read_text

                def missing_maps(path, *args, **kwargs):
                    if path == proc / 'maps':
                        raise FileNotFoundError('exiting process')
                    return original(path, *args, **kwargs)

                def exits(_seconds):
                    info = proc / 'stat'
                    if outcome == 'gone':
                        info.unlink()
                    elif outcome == 'zombie':
                        info.write_text(original(info).replace('(fixture) S', '(fixture) Z'))
                    else:
                        info.write_text(original(info).replace('12345', '12346'))

                with mock.patch.object(Path, 'read_text', missing_maps), \
                        mock.patch.object(m.time, 'sleep', side_effect=exits):
                    self.assertEqual(self.probe()['processes'], [])

    def test_live_process_with_persistently_missing_evidence_is_refused(self):
        proc = self.fake_process()
        original = Path.read_text

        def missing_maps(path, *args, **kwargs):
            if path == proc / 'maps':
                raise FileNotFoundError('still alive but unreadable')
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', missing_maps), \
                mock.patch.object(m.time, 'sleep') as pause:
            with self.assertRaisesRegex(m.Refused, 'incomplete evidence for live PID 900001'):
                self.probe()
        self.assertEqual(pause.call_args_list, [mock.call(0.1)] * 3)

    def test_retry_rechecks_all_evidence_and_preserves_live_reference(self):
        proc = self.fake_process()
        original = Path.read_text
        failures = [True]

        def transient_missing(path, *args, **kwargs):
            if path == proc / 'maps' and failures[0]:
                failures[0] = False
                raise FileNotFoundError('transient disappearance')
            return original(path, *args, **kwargs)

        def new_reference(_seconds):
            (proc / 'cwd').unlink()
            os.symlink(self.root / 'now-active', proc / 'cwd')

        with mock.patch.object(Path, 'read_text', transient_missing), \
                mock.patch.object(m.time, 'sleep', side_effect=new_reference):
            self.assertIn('now-active', self.probe()['reserved'])

    def test_socket_symlink_repository_and_backup_are_retained(self):
        self.old('repository/.git')
        self.old('backup.bak')
        outside = self.base / 'preserved'
        outside.write_text('preserved')
        os.symlink(outside, self.root / 'link')
        with socket.socket(socket.AF_UNIX) as sock:
            sock.bind(str(self.root / 'worker.sock'))
            plan, skipped = self.plan()
        self.assertFalse(plan)
        self.assertEqual(outside.read_text(), 'preserved')
        self.assertIn('symlink, socket or special file', skipped)

    def test_receipt_name_is_excluded_from_preview(self):
        self.old('receipt.json')
        plan, skipped = self.plan()
        self.assertFalse(plan)
        self.assertEqual(skipped['protected name'], 1)

    def test_preview_honors_mole_whitelist_on_tmp_alias(self):
        self.old('keep.log')
        self.old('eligible.log')
        config = self.base / '.config/mole'
        config.mkdir(parents=True)
        (config / 'whitelist').write_text('/tmp/keep.log\n')
        with mock.patch.dict(os.environ, {'HOME': str(self.base)}):
            plan, skipped = self.plan()
        self.assertEqual([row['name'] for row in plan], ['eligible.log'])
        self.assertEqual(skipped['Mole protection or whitelist'], 1)
        self.assertTrue((self.root / 'keep.log').exists())

    def test_policy_failure_does_not_accept_partial_output(self):
        self.old('old.log')
        result = subprocess.CompletedProcess([], 2, b'old.log\0', b'policy failed')
        with mock.patch.object(m.subprocess, 'run', return_value=result), self.assertRaises(m.Refused):
            self.plan()
        self.assertTrue((self.root / 'old.log').exists())

    def test_policy_shards_cover_every_candidate_exactly_once(self):
        names = [f'item-{index:03d}.log' for index in range(200)]
        batches = []

        def fake_run(command, **kwargs):
            batch = [name for name in kwargs['input'].decode().split('\0') if name]
            batches.append(batch)
            allowed = [name for name in batch if name.endswith('.log')]
            return subprocess.CompletedProcess(
                command, 0, ''.join(name + '\0' for name in allowed).encode(), b'')

        with mock.patch.object(m.subprocess, 'run', side_effect=fake_run):
            allowed = m.policy_candidates(self.root, names, time.monotonic() + 60)

        self.assertEqual(allowed, set(names))
        # Sharding must partition the candidate set: no name checked twice, none lost.
        self.assertGreater(len(batches), 1)
        self.assertEqual(sorted(name for batch in batches for name in batch), sorted(names))

    def test_policy_shard_refusal_discards_the_whole_candidate_set(self):
        names = [f'item-{index:03d}.log' for index in range(200)]

        def fake_run(command, **kwargs):
            batch = [name for name in kwargs['input'].decode().split('\0') if name]
            if 'item-000.log' in batch:
                return subprocess.CompletedProcess(command, 2, b'', b'policy failed')
            return subprocess.CompletedProcess(
                command, 0, ''.join(name + '\0' for name in batch).encode(), b'')

        with mock.patch.object(m.subprocess, 'run', side_effect=fake_run), \
                self.assertRaises(m.Refused):
            m.policy_candidates(self.root, names, time.monotonic() + 60)

    def test_staged_delete_reloads_original_path_whitelist(self):
        config = self.base / '.config/mole'
        config.mkdir(parents=True)
        (config / 'whitelist').write_text('/tmp/keep.log\n')
        staged = self.state / 'keep.log'
        staged.write_text('retained fixture')
        with mock.patch.dict(os.environ, {'HOME': str(self.base)}), \
                self.assertRaisesRegex(m.Refused, 'Mole protection or whitelist'):
            m.owner_delete(self.state / 'receipt.json', staged, preview=False)
        self.assertEqual(staged.read_text(), 'retained fixture')

    def test_inotify_inode_reference_retains_tree(self):
        path = self.old('watched/data.log')
        s = path.stat()
        watches = {(os.major(s.st_dev) << 20 | os.minor(s.st_dev), s.st_ino)}
        with self.assertRaisesRegex(m.Refused, 'inotify'):
            self.tree(path.parent, watches)

    def test_inotify_descriptor_is_parsed(self):
        path = self.old('watched.log')
        s = path.stat()
        proc = self.fake_process()
        os.symlink('anon_inode:inotify', proc / 'fd/7')
        key = (os.major(s.st_dev) << 20 | os.minor(s.st_dev), s.st_ino)
        (proc / 'fdinfo/7').write_text(f'inotify wd:1 ino:{key[1]:x} sdev:{key[0]:x} mask:fff\n')
        self.assertIn(key, self.probe()['watches'])

    def test_arbitrary_root_and_shorter_retention_are_refused(self):
        for args in (['--path', str(self.base)], ['--age-days', '1'], ['--age-days', 'nan']):
            code, output = self.run_cli(args)
            self.assertEqual(code, 2)
            self.assertEqual(output['deleted'], 0)

    def test_three_days_is_default_and_minimum(self):
        for args in ([], ['--age-days', '3']):
            code, output = self.run_cli(args, validate_root=lambda _: self.root,
                                        build_plan=lambda *args: ([], {}))
            self.assertEqual(code, 0)
            self.assertEqual(output['age_days'], 3)
        scan = mock.Mock()
        code, _ = self.run_cli(['--age-days', '2.999'], build_plan=scan)
        self.assertEqual(code, 2)
        scan.assert_not_called()

    def test_final_guard_enforces_three_day_receipt_floor(self):
        candidate = self.candidate()
        stage = self.state / 'item-fixture'
        stage.mkdir()
        receipt = stage / 'receipt.json'
        for days, accepted in ((3.01, True), (2.999, False)):
            receipt.write_text(json.dumps({**candidate, 'cutoff': time.time() - days * 86400,
                                           'source': str(self.root / candidate['name'])}))
            with mock.patch.object(m, 'trusted'), mock.patch.object(m, 'validate_root'), \
                    mock.patch.object(m, 'process_snapshot', return_value=self.live), \
                    mock.patch.object(m, 'tree_snapshot', return_value=candidate):
                if accepted:
                    m.final_guard(receipt)
                else:
                    with self.assertRaises(m.Refused):
                        m.final_guard(receipt)

    def test_all_candidates_are_previewed_and_executed_without_fifty_item_cap(self):
        plan = [{'name': f'item-{index}', 'bytes': 75 - index} for index in range(75)]
        sink = mock.Mock()
        for args in ([], ['--execute']):
            code, output = self.run_cli(args, validate_root=lambda _: self.root,
                                        trusted=lambda _: None,
                                        build_plan=lambda *args: (plan, {}), execute_item=sink)
            self.assertEqual(code, 0)
            self.assertEqual(output['candidates'], 75)
            self.assertEqual(output['deferred'], 0)
            self.assertEqual(output['candidate_bytes'], sum(row['bytes'] for row in plan))
        self.assertEqual([call.args[1] for call in sink.call_args_list], plan)
        self.assertEqual(output['deleted'], 75)

    def test_execution_budget_defers_remaining_candidates_without_failure(self):
        plan = [{'name': f'item-{index}', 'bytes': 1} for index in range(75)]
        clock = [1000.0]

        def execute(*args):
            clock[0] += 60

        with mock.patch.object(m.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(m, 'RUN_SECONDS', 250):
            code, output = self.run_cli(['--execute'], validate_root=lambda _: self.root,
                                        trusted=lambda _: None,
                                        build_plan=lambda *args: (plan, {}), execute_item=execute)
        self.assertEqual(code, 0)
        self.assertEqual(output['deleted'], 3)
        self.assertEqual(output['failed'], 0)
        self.assertEqual(output['deferred'], 72)
        self.assertEqual(output['deferred_reason'], 'execution budget')

    def test_timeout_discards_plan_instead_of_deleting_prefix(self):
        self.old('first.log')
        code, output = self.run_cli(['--execute'], validate_root=lambda _: self.root,
                                    trusted=lambda _: None,
                                    build_plan=mock.Mock(side_effect=TimeoutError('scan incomplete')))
        self.assertEqual(code, 2)
        self.assertEqual(output['scan'], 'incomplete')
        self.assertTrue((self.root / 'first.log').exists())

    def test_failed_deletion_returns_nonzero_and_logs_once(self):
        candidate = self.candidate()
        code, output = self.run_cli(['--execute'], validate_root=lambda _: self.root,
                                    trusted=lambda _: None,
                                    build_plan=lambda *args: ([candidate], {}),
                                    execute_item=mock.Mock(side_effect=OSError('delete failed')))
        self.assertEqual(code, 1)
        self.assertEqual(output['failed'], 1)
        rows = [json.loads(x) for x in m.LOG.read_text().splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'failed')

    def test_lock_contention_does_not_scan_or_delete(self):
        candidate = self.candidate()
        with (self.state / 'cleanup.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            scan = mock.Mock()
            code, output = self.run_cli(['--execute'], validate_root=lambda _: self.root,
                                        trusted=lambda _: None, build_plan=scan)
        self.assertEqual(code, 2)
        scan.assert_not_called()
        self.assertTrue((self.root / candidate['name']).exists())

    def test_audit_failure_prevents_mutation(self):
        candidate = self.candidate()
        with mock.patch.object(m, 'validate_root', return_value=self.root), \
                mock.patch.object(m, 'process_snapshot', return_value=self.live), \
                mock.patch.object(m, 'append_log', side_effect=OSError('audit denied')), \
                mock.patch.object(m, 'owner_delete') as sink:
            with self.assertRaises(OSError):
                m.execute_item(self.root, candidate, self.cutoff, time.monotonic() + 10)
        sink.assert_not_called()
        self.assertEqual((self.root / candidate['name']).read_text(), 'fixture')
        self.assertFalse(list(self.state.glob('item-*')))

    def test_changed_candidate_is_preserved_before_staging(self):
        candidate = self.candidate()
        (self.root / candidate['name']).write_text('new data')
        with mock.patch.object(m, 'validate_root', return_value=self.root), \
                mock.patch.object(m, 'process_snapshot', return_value=self.live), self.assertRaises(m.Refused):
            m.execute_item(self.root, candidate, self.cutoff, time.monotonic() + 10)
        self.assertEqual((self.root / candidate['name']).read_text(), 'new data')

    def test_isolation_rechecks_object_and_restores_on_live_reference(self):
        candidate = self.candidate()
        with mock.patch.object(m, 'validate_root', return_value=self.root), \
                mock.patch.object(m, 'process_snapshot', return_value=self.live), \
                mock.patch.object(m, 'final_guard', side_effect=m.Refused('live')), \
                mock.patch.object(m, 'trusted'), self.assertRaises(m.Refused):
            m.execute_item(self.root, candidate, self.cutoff, time.monotonic() + 10)
        self.assertEqual((self.root / candidate['name']).read_text(), 'fixture')
        self.assertFalse(list(self.state.glob('item-*')))

    def test_safe_fixture_deleted_after_isolation_despite_root_cwd(self):
        candidate = self.candidate()
        self.live['reserved'].add('.')
        phases = []

        def sink(receipt, item, preview, deadline=None):
            self.assertFalse((self.root / candidate['name']).exists())
            self.assertEqual(item.read_text(), 'fixture')
            phases.append(preview)
            if not preview:
                item.unlink()  # Only the fixture created by this test.

        with mock.patch.object(m, 'validate_root', return_value=self.root), \
                mock.patch.object(m, 'process_snapshot', return_value=self.live), \
                mock.patch.object(m, 'final_guard'), mock.patch.object(m, 'trusted'), \
                mock.patch.object(m, 'owner_delete', side_effect=sink):
            m.execute_item(self.root, candidate, self.cutoff, time.monotonic() + 10)
        self.assertEqual(phases, [True, False])
        self.assertFalse((self.root / candidate['name']).exists())
        self.assertFalse(list(self.state.glob('item-*')))

    def test_source_recreation_is_not_overwritten_on_failed_restore(self):
        candidate = self.candidate()

        def guard(_receipt):
            (self.root / candidate['name']).write_text('replacement')
            raise m.Refused('new reference')

        with mock.patch.object(m, 'validate_root', return_value=self.root), \
                mock.patch.object(m, 'process_snapshot', return_value=self.live), \
                mock.patch.object(m, 'final_guard', side_effect=guard), mock.patch.object(m, 'trusted'), \
                self.assertRaisesRegex(m.Refused, 'retained item'):
            m.execute_item(self.root, candidate, self.cutoff, time.monotonic() + 10)
        self.assertEqual((self.root / candidate['name']).read_text(), 'replacement')
        stages = list(self.state.glob('item-*'))
        self.assertEqual((stages[0] / candidate['name']).read_text(), 'fixture')

    def test_mole_failure_propagates(self):
        class Child:
            returncode = 1

            def communicate(self, timeout):
                return '', 'refused'

        with mock.patch.object(m.subprocess, 'Popen', return_value=Child()), self.assertRaises(m.Refused):
            m.owner_delete(self.state / 'receipt.json', self.root / 'old', preview=False)


if __name__ == '__main__':
    unittest.main()
