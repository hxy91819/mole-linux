"""Link and IPC regressions for the temporary cleaner, using private fixtures."""
import importlib.util
import os
from pathlib import Path
import socket
import time
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location('cleanup_fixtures', Path(__file__).with_name('test_devcloud_cleanup.py'))
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)
m = fixtures.m


@unittest.skipUnless(fixtures.sys.platform.startswith('linux'), 'Linux temporary cleaner')
class TempLinkTests(unittest.TestCase):
    setUp = fixtures.CleanupTests.setUp
    old = fixtures.CleanupTests.old
    tree = fixtures.CleanupTests.tree
    plan = fixtures.CleanupTests.plan
    fake_process = fixtures.CleanupTests.fake_process
    probe = fixtures.CleanupTests.probe

    def age(self, *paths):
        for path in paths:
            os.utime(path, (self.cutoff - 1, self.cutoff - 1), follow_symlinks=False)

    def execute_fixture(self, candidate):
        # Exercise Mole's real sink without requiring the fixture directory to be
        # installed as an approved host root in the guard subprocess.
        policy = m.MOLE_POLICY + '\n/usr/bin/python3() { return 0; }\n'
        with mock.patch.object(m, 'validate_root', return_value=self.root), \
                mock.patch.object(m, 'trusted'), mock.patch.object(m, 'final_guard'), \
                mock.patch.object(m, 'MOLE_POLICY', policy), \
                mock.patch.dict(os.environ, {'HOME': str(self.base), 'MOLE_TEST_MODE': '0',
                                           'MOLE_TEST_NO_AUTH': '1'}):
            m.execute_item(self.root, candidate, self.cutoff, time.monotonic() + 20, self.live)

    def test_nested_symlinks_are_unlinked_without_touching_targets(self):
        self.old('packages/content')
        path = self.root / 'packages'
        outside = self.base / 'outside'
        outside.mkdir()
        secret = outside / 'auth.json'
        secret.write_text('sentinel')
        self_link = path / 'loop'
        self_link.symlink_to('.')
        link = path / 'external'
        link.symlink_to(outside)
        self.age(self_link, link, path)
        plan, _ = self.plan()
        self.assertEqual([r['name'] for r in plan], ['packages'])
        self.execute_fixture(plan[0])
        self.assertFalse(path.exists())
        self.assertEqual(secret.read_text(), 'sentinel')

    def test_hardlinks_are_candidates_without_claiming_shared_blocks(self):
        path = self.old('packages/artifact')
        external = self.base / 'owner-cache'
        os.link(path, external)
        plan, _ = self.plan()
        self.assertEqual([r['name'] for r in plan], ['packages'])
        self.assertEqual(plan[0]['bytes'], path.parent.stat().st_blocks * 512)
        self.execute_fixture(plan[0])
        self.assertEqual(external.read_text(), 'fixture')
        self.assertEqual(external.stat().st_nlink, 1)

    def test_idle_fifo_can_be_removed_but_open_fifo_is_retained(self):
        path = self.root / 'pipe'
        os.mkfifo(path)
        self.age(path)
        self.assertEqual([r['name'] for r in self.plan()[0]], ['pipe'])
        proc = self.fake_process()
        (proc / 'fd/8').symlink_to(path)
        self.live.update(self.probe())
        self.assertFalse(self.plan()[0])

    def test_bound_socket_inode_is_retained_after_rename_then_stale_socket_can_go(self):
        old_name = self.root / 'original.sock'
        path = self.root / 'renamed.sock'
        # CI need not enter the network namespace of unrelated processes.
        with mock.patch.object(m, 'PROC_ROOT', self.proc):
            with socket.socket(socket.AF_UNIX) as sock:
                sock.bind(str(old_name))
                old_name.rename(path)
                self.age(path)
                _, skipped = self.plan()
                self.assertEqual(skipped.get('live Unix socket'), 1)
            self.assertEqual([r['name'] for r in self.plan()[0]], ['renamed.sock'])

    def test_credential_symlink_and_bare_git_remain_protected(self):
        self.old('fixture/content')
        link = self.root / 'fixture/.env.local'
        link.symlink_to('../missing')
        self.age(link, link.parent)
        bare = self.root / 'bare'
        self.old('bare/HEAD', 'ref: refs/heads/main\n')
        for subdir in ('objects', 'refs'):
            (bare / subdir).mkdir()
            self.age(bare / subdir)
        self.age(bare)
        plan, skipped = self.plan()
        self.assertFalse(plan)
        self.assertEqual(skipped.get('credential or backup'), 1)
        self.assertEqual(skipped.get('Git state'), 1)

    def test_slow_prestage_recheck_skips_item_without_staging(self):
        item = self.old('slow/payload')
        candidate = dict(name='slow', **self.tree(item.parent))
        with mock.patch.object(m, 'validate_root', return_value=self.root), \
                mock.patch.object(m, 'tree_snapshot', side_effect=TimeoutError('fixture')), \
                mock.patch.object(m, 'owner_delete') as sink:
            with self.assertRaisesRegex(m.CandidateSkipped, 'slow tree before staging'):
                m.execute_item(self.root, candidate, self.cutoff, time.monotonic() + 20, self.live)
        sink.assert_not_called()
        self.assertEqual(item.read_text(), 'fixture')
        self.assertFalse(list(self.state.iterdir()))
