"""Behavioral regressions for the worktree patrol collector.

Run: python3 -m pytest tests/test_patrol_worktrees.py -q
Covers credential resolution (no value ever leaves the process) and the 工蜂 MR
lookup contract; never touches git.woa.com or a real token.
"""

import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


SPEC = importlib.util.spec_from_file_location(
    'patrol_worktrees',
    Path(__file__).resolve().parents[1] / '.claude/skills/devcloud-patrol/scripts/patrol_worktrees.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)


class GongfengCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='patrol-cred-test-')
        self.addCleanup(self.temp.cleanup)
        self.env_file = Path(self.temp.name) / '.env'
        self.patches = [mock.patch.object(m, 'GONGFENG_ENV_FILE', str(self.env_file)),
                        mock.patch.dict(os.environ, {}, clear=False)]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        os.environ.pop('GONGFENG_PRIVATE_TOKEN', None)

    def write_env(self, token, mode=0o600):
        self.env_file.write_text(f'OTHER=irrelevant\nGONGFENG_PRIVATE_TOKEN={token}\n')
        os.chmod(self.env_file, mode)

    def test_exported_token_wins_and_is_returned_without_logging(self):
        with mock.patch.dict(os.environ, {'GONGFENG_PRIVATE_TOKEN': 'from-env'}):
            token, source = m.gongfeng_token()
        self.assertEqual(token, 'from-env')
        self.assertEqual(source, 'env:GONGFENG_PRIVATE_TOKEN')

    def test_owner_file_is_read_when_group_or_other_cannot_read(self):
        self.write_env('file-token', 0o600)
        token, source = m.gongfeng_token()
        self.assertEqual(token, 'file-token')
        self.assertIn('.env', source)

    def test_readable_credentials_are_refused_instead_of_used(self):
        self.write_env('file-token', 0o644)
        token, source = m.gongfeng_token()
        self.assertIsNone(token)
        self.assertIn('权限不安全', source)

    def test_missing_file_reports_the_path_without_guessing(self):
        token, source = m.gongfeng_token()
        self.assertIsNone(token)
        self.assertIn('凭据文件缺失', source)

    def test_missing_key_in_file_is_not_an_empty_credential(self):
        self.env_file.write_text('OTHER=irrelevant\n')
        os.chmod(self.env_file, 0o600)
        token, source = m.gongfeng_token()
        self.assertIsNone(token)
        self.assertIn('没有 GONGFENG_PRIVATE_TOKEN', source)


class GongfengLookupTests(unittest.TestCase):
    def test_merged_mr_is_found_without_using_state_all(self):
        seen = []

        def fake_run(cmd, **kwargs):
            seen.append(cmd)
            self.assertNotIn('all', cmd)
            payload = {"ok": True, "result": [], "pagination": {"total": 0}}
            if '--state' in cmd and cmd[cmd.index('--state') + 1] == 'merged':
                payload = {"ok": True, "result": [{
                    "iid": 47, "state": "merged", "title": 'demo',
                    "source_branch": 'fix/x', "target_branch": 'master',
                    "source_commit": 'a' * 40, "merge_commit_sha": 'b' * 40}],
                    "pagination": {"total": 1}}
            return 0, json.dumps(payload), ''

        with mock.patch.object(m, 'run', side_effect=fake_run):
            mr = m.gongfeng_lookup('lighthouse/lightginkgo', 'fix/x', 'tok')
        self.assertEqual(mr['state'], 'merged')
        self.assertEqual(mr['iid'], 47)
        self.assertEqual(mr['url'],
                         'https://git.woa.com/lighthouse/lightginkgo/-/merge_requests/47')
        # Open MR takes precedence over an older merged MR on a reused branch.
        self.assertEqual([cmd[cmd.index("--state") + 1] for cmd in seen], ["opened", "merged"])

    def test_other_branch_match_is_not_accepted(self):
        def fake_run(cmd, **kwargs):
            payload = {"ok": True, "result": [{
                "iid": 1, "state": "merged", "source_branch": 'someone-else',
                "target_branch": 'master', "source_commit": 'a' * 40}],
                "pagination": {"total": 1}}
            return 0, json.dumps(payload), ''

        with mock.patch.object(m, 'run', side_effect=fake_run):
            mr = m.gongfeng_lookup('lighthouse/lightpower', 'fix/x', 'tok')
        self.assertEqual(mr['state'], 'none')

    def test_api_refusal_becomes_an_error_state_not_a_verdict(self):
        def fake_run(cmd, **kwargs):
            return 0, json.dumps({"ok": False, "error": {"message": 'unauthorized'}}), ''

        with mock.patch.object(m, 'run', side_effect=fake_run):
            mr = m.gongfeng_lookup('lighthouse/lightpower', 'fix/x', 'tok')
        self.assertEqual(mr['state'], 'error')

    def test_token_never_appears_in_the_returned_record(self):
        secret = 'super-secret-token-value'

        def fake_run(cmd, **kwargs):
            # The token must travel via env, never argv.
            self.assertNotIn(secret, cmd)
            self.assertEqual(kwargs.get('env', {}).get('GONGFENG_PRIVATE_TOKEN'), secret)
            return 0, json.dumps({"ok": True, "result": [], "pagination": {}}), ''

        with mock.patch.object(m, 'run', side_effect=fake_run):
            mr = m.gongfeng_lookup('lighthouse/lightpower', 'fix/x', secret)
        self.assertNotIn(secret, json.dumps(mr))

    def test_detached_head_cannot_be_queried_by_branch(self):
        mr = m.gongfeng_lookup('lighthouse/lightpower', '(detached)', 'tok')
        self.assertEqual(mr['state'], 'unknown')


class GongfengEvidenceTests(unittest.TestCase):
    """owner 工具 --remote-evidence 的输入契约：只有精确 head 的 clean 项能进文件。"""

    HEAD = 'a' * 40

    def setUp(self):
        patcher = mock.patch.object(
            m, 'remote_slug', return_value=('lighthouse/lightpower', 'git.woa.com'))
        patcher.start()
        self.addCleanup(patcher.stop)

    def item(self, **overrides):
        obj = {
            'path': '/data/code/lighthouse/lightpower-cos-gift',
            'repo': '/data/code/lighthouse/lightpower',
            'branch': 'feature/remove-cos-gift-gray',
            'head': self.HEAD,
            'dirty': False,
            'locked': False,
            'bb_environment': None,
            'who_uses': {'count': 0},
            'size_bytes': 22544384,
            'gongfeng': {
                'state': 'merged', 'iid': 1309, 'project': 'lighthouse/lightpower',
                'title': 'remove gray release flag',
                'target_branch': 'master', 'source_commit': self.HEAD,
                'url': 'https://git.woa.com/lighthouse/lightpower/-/merge_requests/1309',
            },
        }
        obj.update(overrides)
        return obj

    def test_exact_head_merged_item_becomes_a_full_sha_evidence_record(self):
        evidence = m.build_gongfeng_evidence([self.item()])

        self.assertEqual(list(evidence), ['/data/code/lighthouse/lightpower'])
        slot = evidence['/data/code/lighthouse/lightpower']
        self.assertEqual(slot['version'], 1)
        self.assertEqual(slot['host'], 'git.woa.com')
        self.assertEqual(slot['repository'], 'lighthouse/lightpower')
        self.assertEqual(len(slot['entries']), 1)
        record = slot['entries'][0]
        self.assertEqual(record['head'], self.HEAD)
        self.assertEqual(record['source_commit'], self.HEAD)
        self.assertEqual(record['state'], 'merged')
        self.assertEqual(record['mr_iid'], 1309)
        self.assertEqual(
            record['mr_url'],
            'https://git.woa.com/lighthouse/lightpower/-/merge_requests/1309')

    def test_non_exact_head_dirty_process_and_open_items_are_all_skipped(self):
        non_exact = self.item()
        non_exact['gongfeng']['source_commit'] = 'c' * 40
        items = [
            non_exact,
            self.item(path='/dirty', dirty=True),
            self.item(path='/busy', who_uses={'count': 1}),
            self.item(path='/open',
                      gongfeng={'state': 'opened', 'iid': 2,
                                'source_commit': self.HEAD,
                                'target_branch': 'master', 'url': 'https://x/2'}),
        ]

        evidence = m.build_gongfeng_evidence(items)

        self.assertEqual(evidence, {})

    def test_items_are_grouped_per_repo_and_never_carry_a_token(self):
        other = '/data/code/lighthouse/lightginkgo'
        evidence = m.build_gongfeng_evidence([
            self.item(),
            self.item(path='/data/code/lighthouse/lightginkgo-blueprint-preconditions',
                      repo=other, branch='fix/blueprint-preconditions'),
        ])

        self.assertEqual(sorted(evidence), ['/data/code/lighthouse/lightginkgo',
                                            '/data/code/lighthouse/lightpower'])
        payload = json.dumps(evidence)
        self.assertNotIn('GONGFENG', payload)
        self.assertNotIn('token', payload.lower())
        for slot in evidence.values():
            for record in slot['entries']:
                self.assertEqual(len(record['head']), 40)
                self.assertEqual(len(record['source_commit']), 40)


class SubmoduleProbeTests(unittest.TestCase):
    """含 submodule 的工作树必须提前判定阻塞，不能每轮去撞 git 的拒绝。"""

    def test_no_gitmodules_is_false_without_running_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(m, 'run') as runner:
                self.assertFalse(m.worktree_has_submodules(tmp))
            runner.assert_not_called()

    def test_submodule_status_output_blocks_the_worktree(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, '.gitmodules').write_text(
                '[submodule "x"]\n\tpath = x\n\turl = u\n', encoding='utf-8')
            with mock.patch.object(m, 'run',
                                   return_value=(0, '-3685c8646bccea336012e4239e7397f2a888f487 vendor/x\n', '')):
                self.assertTrue(m.worktree_has_submodules(tmp))

    def test_unreadable_submodule_state_defers_to_the_executor(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, '.gitmodules').write_text('[submodule "x"]\n', encoding='utf-8')
            with mock.patch.object(m, 'run', return_value=(1, '', 'not a git repository')):
                self.assertFalse(m.worktree_has_submodules(tmp))


class LastActiveParseTests(unittest.TestCase):
    """du --time 一行同时给出体积与上次活跃；形状不对就写「不可得」。"""

    def test_du_time_line_parses_size_and_timestamp(self):
        line = '123\t2026-08-14T18:06:05+0800\t/data/x\n'
        with mock.patch.object(m, 'run', return_value=(0, line, '')):
            self.assertEqual(m.du_bytes_last_active('/data/x'),
                             (123, '2026-08-14T18:06:05+0800'))

    def test_missing_time_column_yields_none_instead_of_guessing(self):
        with mock.patch.object(m, 'run', return_value=(0, '123\t/data/x\n', '')):
            size, when = m.du_bytes_last_active('/data/x')
        self.assertEqual(size, 123)
        self.assertIsNone(when)

    def test_du_failure_yields_none(self):
        with mock.patch.object(m, 'run', return_value=(1, '', 'no such directory')):
            self.assertEqual(m.du_bytes_last_active('/data/x'), (None, None))


class DirtySummaryTests(unittest.TestCase):
    """dirty 项要能压成一行「多少人工改动」，而不是让 owner 逐项读代码。"""

    def test_tracked_and_untracked_are_counted_with_diffstat(self):
        porcelain = ' M a.ts\n M b.ts\n?? c.sh\n'
        summary = m.summarize_dirty(porcelain, ' 2 files changed, 7 insertions(+), 1 deletion(-)')
        self.assertEqual(summary['tracked'], 2)
        self.assertEqual(summary['untracked'], 1)
        self.assertEqual(summary['files_changed'], 2)
        self.assertEqual(summary['insertions'], 7)
        self.assertEqual(summary['deletions'], 1)
        self.assertEqual(m.compact_dirty(summary), ' [2F +7/-1 2 tracked 1 untracked]')

    def test_clean_status_has_no_summary(self):
        self.assertIsNone(m.summarize_dirty('', ''))
        self.assertEqual(m.compact_dirty(None), '')

    def test_untracked_only_worktree_has_no_diffstat(self):
        summary = m.summarize_dirty('?? tools/x.py\n')
        self.assertIsNone(summary['files_changed'])
        self.assertEqual(summary['untracked'], 1)
        self.assertEqual(m.compact_dirty(summary), ' [1 untracked]')


if __name__ == '__main__':
    unittest.main()


class RunHealthTests(unittest.TestCase):
    """异常驱动的新增/持续/恢复/复现语义。"""

    def test_new_persistent_recovered_recurring(self):
        cur = [{"key": "bb-query-failed", "code": "bb-query-failed", "detail": "x"}]
        h1 = m.evaluate_run_health({}, cur, "T1")
        self.assertEqual(len(h1["notifiable"]), 1)
        self.assertEqual(h1["recovered"], [])

        h2 = m.evaluate_run_health(h1["exceptions"], cur, "T2")
        self.assertEqual(h2["notifiable"], [])   # 持续 → 静默
        self.assertEqual(h2["recovered"], [])

        h3 = m.evaluate_run_health(h2["exceptions"], [], "T3")
        self.assertEqual(len(h3["recovered"]), 1)  # 恢复 → 通知一次
        self.assertIsNotNone(h3["exceptions"]["bb-query-failed"]["resolved_at"])

        h4 = m.evaluate_run_health(h3["exceptions"], cur, "T4")
        self.assertEqual(len(h4["notifiable"]), 1)  # 恢复后复现 → 重新通知
        self.assertIsNone(h4["exceptions"]["bb-query-failed"]["resolved_at"])

    def test_different_entities_are_separate_exceptions(self):
        cur = [{"key": "execute-failed:/a", "code": "execute-failed", "entity": "/a"},
               {"key": "execute-failed:/b", "code": "execute-failed", "entity": "/b"}]
        h = m.evaluate_run_health({"execute-failed:/a": {"key": "execute-failed:/a", "notified": True}}, cur, "T1")
        self.assertEqual(len(h["notifiable"]), 1)
        self.assertEqual(h["notifiable"][0]["entity"], "/b")
