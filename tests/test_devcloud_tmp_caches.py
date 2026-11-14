"""Structural cache and batched process-evidence regressions (private fixtures)."""
import importlib.util
import os
from pathlib import Path
import shutil
import signal
import threading
import time
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('cleanup_fixtures', Path(__file__).with_name('test_devcloud_cleanup.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
m = fixtures.m


@unittest.skipUnless(fixtures.sys.platform.startswith('linux'), 'Linux /proc and renameat2 contract')
class CacheTests(unittest.TestCase):
    setUp = fixtures.CleanupTests.setUp
    old = fixtures.CleanupTests.old
    plan = fixtures.CleanupTests.plan
    run_cli = fixtures.CleanupTests.run_cli
    fake_process = fixtures.CleanupTests.fake_process

    def cache(self, kind):
        root = self.root / ('arbitrary-' + kind)
        root.mkdir()
        if kind == 'go-build':
            (root / 'README').write_text('This directory holds cached build artifacts from the Go build system.\n')
            for n in range(256):
                (root / f'{n:02x}').mkdir()
            (root / '00' / ('0' * 64 + '-d')).write_bytes(b'cache')
        elif kind == 'pnpm-store':
            for name in ('files/00', 'index/00'):
                (root / 'v10' / name).mkdir(parents=True)
            (root / 'v10/files/00' / ('a' * 126)).write_bytes(b'cache')
            (root / 'v10/index/00' / ('b' * 62 + '-package@1.0.json')).write_text('{}')
        else:
            (root / 'v24.22.1-x64-964aae3f-0').mkdir()
            (root / 'v24.22.1-x64-964aae3f-0/1234abcd').write_bytes(bytes.fromhex('b2dbdf8a') + b'cache')
        stamp = time.time() - 2 * 86400
        for path in [*root.rglob('*'), root]:
            os.utime(path, (stamp, stamp))
        return root

    def eligible(self, kind):
        root = self.cache(kind)
        plan, skipped = self.plan()
        self.assertEqual([r['name'] for r in plan], [root.name], skipped)
        self.assertEqual(plan[0]['cache_type'], kind)
        self.assertLess(plan[0]['latest_file_mtime'], time.time() - 86400)
        return root

    def test_structural_caches_are_deleted_through_isolation(self):
        for kind in ('go-build', 'pnpm-store', 'node-compile'):
            with self.subTest(kind=kind):
                root = self.eligible(kind)
                def sink(receipt, item, preview, deadline=None):
                    self.assertFalse(root.exists())
                    if not preview:
                        shutil.rmtree(item)  # This test's private fixture only.
                with mock.patch.object(m, 'process_snapshot', return_value=self.live):
                    code, result = self.run_cli(['--execute'], validate_root=lambda _: self.root,
                                                trusted=lambda _: None, owner_delete=sink)
                self.assertEqual(code, 0, result)
                self.assertEqual(result['deleted'], 1)
                self.assertFalse(root.exists())

    def test_cache_with_file_younger_than_24_hours_is_retained(self):
        for kind in ('go-build', 'pnpm-store', 'node-compile'):
            root = self.eligible(kind)
            leaf = next(p for p in root.rglob('*') if p.is_file())
            os.utime(leaf, None)
            plan, _ = self.plan()
            self.assertFalse(plan)
            shutil.rmtree(root)  # Private fixture.

    def test_cache_with_process_reference_is_retained(self):
        root = self.eligible('go-build')
        self.live['reserved'].add(root.name)
        self.assertFalse(self.plan()[0])
        self.assertTrue(root.exists())

    def test_unknown_process_probe_retains_cache(self):
        root = self.eligible('pnpm-store')
        with mock.patch.object(m, 'process_snapshot', side_effect=m.Refused('unknown probe')):
            code, result = self.run_cli(['--execute'], validate_root=lambda _: self.root, trusted=lambda _: None)
        self.assertEqual(code, 2)
        self.assertEqual(result['deleted'], 0)
        self.assertTrue(root.exists())

    def test_cache_name_without_structure_uses_original_rules(self):
        self.eligible('node-compile')
        for name in ('stage264-go-cache', '.pnpm-store', 'node-compile-cache'):
            self.old(name + '/data.db')
        plan, _ = self.plan()
        self.assertEqual([r['cache_type'] for r in plan], ['node-compile'])

    def test_cache_symlink_is_retained(self):
        root = self.eligible('go-build')
        moved = self.base / 'outside'
        root.rename(moved)
        root.symlink_to(moved)
        self.assertFalse(self.plan()[0])
        self.assertTrue(moved.exists())

    def test_known_pid_is_skipped_but_reused_pid_is_fully_probed(self):
        proc = self.fake_process(cwd=self.root / 'active')
        with mock.patch.object(m, 'PROC_ROOT', self.proc), mock.patch.object(m.os, 'getppid', return_value=1):
            snapshot = m.process_snapshot(self.root, time.monotonic() + 5)
            # A known process need not have maps read again. Reuse must never be skipped.
            (proc / 'maps').unlink()
            delta = m.process_snapshot(self.root, time.monotonic() + 5, snapshot['identities'])
            self.assertFalse(delta['reserved'])
            text = (proc / 'stat').read_text().replace('12345', '12346')
            (proc / 'stat').write_text(text)
            with mock.patch.object(m.time, 'sleep'), self.assertRaises(m.Refused):
                m.process_snapshot(self.root, time.monotonic() + 5, snapshot['identities'])

    def test_batch_probes_existing_processes_once(self):
        for n in range(8):
            self.old(f'fixture-{n}.log')
        full = []
        def probe(root, deadline, known=None):
            if known is None:
                full.append(root)
            return {**self.live, 'identities': {'999999': '12345'}}
        def sink(receipt, item, preview, deadline=None):
            if not preview:
                m.final_guard(receipt)
                item.unlink()  # Private fixture.
        with mock.patch.object(m, 'process_snapshot', side_effect=probe):
            code, result = self.run_cli(['--execute'], validate_root=lambda _: self.root,
                                        trusted=lambda _: None, owner_delete=sink)
        self.assertEqual(code, 0, result)
        self.assertEqual(result['deleted'], 8)
        self.assertLessEqual(len(full), 2)  # Planning plus one bounded execution batch.

    def test_pnpm_cache_with_credentials_or_repository_is_retained(self):
        for relative in ('v10/.git/config', 'v10/auth.json', 'v10/files/00/private.key'):
            root = self.eligible('pnpm-store')
            item = root / relative
            item.parent.mkdir(parents=True, exist_ok=True)
            item.write_text('private fixture')
            stamp = time.time() - 2 * 86400
            os.utime(item, (stamp, stamp))
            self.assertFalse(self.plan()[0])
            shutil.rmtree(root)  # Private fixture.

    def test_new_process_reference_and_unknown_after_isolation_restore_cache(self):
        for unknown in (False, True):
            root = self.eligible('go-build')
            plan, _ = self.plan()
            def probe(path, deadline, known=None):
                if path.parent.name.startswith('item-'):
                    if unknown:
                        raise m.Refused('unknown new PID')
                    return {**self.live, 'reserved': {'.'}}
                return {**self.live, 'identities': {}}
            with mock.patch.object(m, 'process_snapshot', side_effect=probe):
                code, result = self.run_cli(['--execute'], validate_root=lambda _: self.root,
                                            trusted=lambda _: None)
            self.assertEqual(code, 1, result)
            self.assertEqual(result['deleted'], 0)
            self.assertTrue(root.exists())
            self.assertFalse(list(self.state.glob('item-*')))
            shutil.rmtree(root)  # Private fixture.

    def test_pnpm_v3_embedded_index_and_v11_database_metadata_are_recognized(self):
        root = self.eligible('pnpm-store')
        (root / 'v10').rename(root / 'v3')
        shutil.rmtree(root / 'v3/index')  # Private fixture.
        index = root / 'v3/files/00' / ('a' * 126 + '-index.json')
        index.write_text('{}')
        stamp = time.time() - 2 * 86400
        os.utime(index, (stamp, stamp))
        self.assertEqual(self.plan()[0][0]['cache_type'], 'pnpm-store')
        (root / 'v3').rename(root / 'v11')
        (root / 'v11/files/00' / ('a' * 126 + '-index.json')).unlink()  # Private fixture.
        db = root / 'v11/index.db'
        db.write_bytes(b'SQLite format 3\0' + b'fixture')
        os.utime(db, (stamp, stamp))
        self.assertEqual(self.plan()[0][0]['cache_type'], 'pnpm-store')

    def test_pnpm_layout_cannot_hide_unrelated_authored_files(self):
        root = self.eligible('pnpm-store')
        item = root / 'v10/notes.md'
        item.write_text('authored fixture')
        stamp = time.time() - 2 * 86400
        os.utime(item, (stamp, stamp))
        self.assertFalse(self.plan()[0])

    def test_cancellation_stops_queued_deletes_and_restores_staged_items(self):
        for n in range(16):
            self.old(f'cancel-{n}.log')
        entered = threading.Event()
        def sink(receipt, item, preview, deadline=None):
            entered.set()
            until = time.monotonic() + 0.15
            while time.monotonic() < until:
                m.checkpoint(deadline)
                time.sleep(0.005)
            if not preview:
                item.unlink()  # Private fixture.
        def cancel():
            if entered.wait(5):
                os.kill(os.getpid(), signal.SIGTERM)
        timer = threading.Thread(target=cancel)
        timer.start()
        try:
            with mock.patch.object(m, 'process_snapshot', return_value={**self.live, 'identities': {}}):
                code, result = self.run_cli(['--execute'], validate_root=lambda _: self.root,
                                            trusted=lambda _: None, owner_delete=sink)
        finally:
            timer.join(timeout=5)
        self.assertNotEqual(code, 0)
        self.assertEqual(result['deleted'], 0, result)
        self.assertEqual(len(list(self.root.glob('cancel-*'))), 16)
        self.assertFalse(list(self.state.glob('item-*')))

    def test_cancellation_kills_inflight_mole_child_before_restoration(self):
        self.old('inflight.log')
        entered = self.base / 'child-started'
        real_popen = m.subprocess.Popen
        children = []
        def child_stub(command, **kwargs):
            item = command[7]
            script = "from pathlib import Path; import time; Path(%r).touch(); time.sleep(2); Path(%r).unlink()" % (str(entered), item)
            child = real_popen([fixtures.sys.executable, '-c', script], **kwargs)
            children.append(child)
            return child
        def cancel():
            until = time.monotonic() + 5
            while not entered.exists() and time.monotonic() < until:
                time.sleep(0.005)
            if entered.exists():
                os.kill(os.getpid(), signal.SIGTERM)
        timer = threading.Thread(target=cancel)
        timer.start()
        try:
            with mock.patch.object(m, 'process_snapshot', return_value={**self.live, 'identities': {}}), \
                    mock.patch.object(m.subprocess, 'Popen', side_effect=child_stub), \
                    mock.patch.object(m, 'policy_candidates', side_effect=lambda root, names, deadline: set(names)):
                code, result = self.run_cli(['--execute'], validate_root=lambda _: self.root,
                                            trusted=lambda _: None)
        finally:
            timer.join(timeout=5)
        self.assertNotEqual(code, 0, result)
        self.assertTrue(children)
        self.assertTrue(all(child.poll() is not None for child in children))
        self.assertEqual((self.root / 'inflight.log').read_text(), 'fixture')
        self.assertFalse(list(self.state.glob('item-*')))

    def test_permission_error_during_process_exit_requires_confirmed_exit(self):
        proc = self.fake_process()
        original = Path.read_text
        def denied(path, *args, **kwargs):
            if path == proc / 'maps':
                raise PermissionError('exiting non-dumpable process')
            return original(path, *args, **kwargs)
        def exited(_seconds):
            (proc / 'stat').unlink()
        with mock.patch.object(m, 'PROC_ROOT', self.proc), mock.patch.object(m.os, 'getppid', return_value=1), \
                mock.patch.object(Path, 'read_text', denied), mock.patch.object(m.time, 'sleep', side_effect=exited):
            result = m.process_snapshot(self.root, time.monotonic() + 5)
        self.assertFalse(result['reserved'])

    def test_repeated_cancellation_does_not_interrupt_restoration(self):
        def restoring(_args):
            try:
                os.kill(os.getpid(), signal.SIGTERM)
            except InterruptedError:
                os.kill(os.getpid(), signal.SIGTERM)
                return 2
            self.fail('initial cancellation was not raised')
        with mock.patch.object(m, '_main', side_effect=restoring):
            self.assertEqual(m.main([]), 2)

    def test_proc_io_error_only_clears_when_exit_is_confirmed(self):
        for errno in (5, 22):
            for exiting in (False, True):
                proc = self.fake_process(pid=900100 + errno * 2 + exiting)
                original = Path.read_text
                def failed(path, *args, **kwargs):
                    if path == proc / 'maps':
                        raise OSError(errno, 'fixture proc error')
                    return original(path, *args, **kwargs)
                def changes(_seconds):
                    if exiting:
                        (proc / 'stat').unlink()
                with mock.patch.object(m, 'PROC_ROOT', self.proc), mock.patch.object(m.os, 'getppid', return_value=1), \
                        mock.patch.object(Path, 'read_text', failed), mock.patch.object(m.time, 'sleep', side_effect=changes):
                    if exiting:
                        self.assertFalse(m.process_snapshot(self.root, time.monotonic() + 5)['reserved'])
                    else:
                        with self.assertRaises(m.Refused):
                            m.process_snapshot(self.root, time.monotonic() + 5)
                if not exiting:
                    (proc / 'maps').write_text('')
