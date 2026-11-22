"""Patrol reporting and execution boundary regressions; all external tools mocked."""
import contextlib
import copy
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location(
    'patrol_report_tests', Path(__file__).resolve().parents[1] /
    '.claude/skills/devcloud-patrol/scripts/patrol_worktrees.py')
m = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(m)
HEAD = 'a' * 40


class ClassificationTests(unittest.TestCase):
    def item(self, **kw):
        o = dict(repo='/repo', path='/tree', dirty=False, locked=False, prunable=False,
                 bb_environment=None, bb_threads=[], who_uses={'count': 0},
                 head=HEAD, age_hours=200, size_bytes=128, remote_host='git.example.invalid',
                 gongfeng={'state': 'merged', 'source_commit': HEAD})
        o.update(kw)
        return o

    def test_host_is_not_a_retention_reason_or_a_decision(self):
        o = m.classify_retained(self.item())
        self.assertEqual(o['reasons'], ['仅预览，未执行'])
        self.assertFalse(o['needs_decision'])
        self.assertIn('精确 head', o['remote_status'])

    def test_unknown_and_live_state_prevents_remote_evidence(self):
        for change in [dict(dirty=None), dict(who_uses={'count': None}),
                       dict(who_uses={'count': False}), dict(bb_threads=[{'id': 'live'}]),
                       dict(prunable=True), dict(head='a' * 12)]:
            with self.subTest(change=change), mock.patch.object(m, 'remote_slug', return_value=('p/r', 'git.example.invalid')):
                self.assertEqual(m.build_gongfeng_evidence([self.item(**change)]), {})

    def test_mr_states_are_explained_without_asking_owner_to_recheck_everything(self):
        for state, reason in [('none', '未找到 MR'), ('unknown', 'detached HEAD，MR 无法按分支查询'),
                              ('error', 'MR 查询失败'), ('no-credential', '凭据缺失'),
                              ('opened', 'MR-open'), ('closed', 'MR closed(未合并)')]:
            with self.subTest(state=state):
                o = m.classify_retained(self.item(gongfeng={'state': state}))
                self.assertIn(reason, o['reasons'])
                self.assertFalse(o['needs_decision'])
                self.assertNotIn('非 GitHub', o['next_step'])

    def test_submodule_and_ambiguous_require_explicit_decision(self):
        a = m.classify_retained(self.item(submodule_blocked=True))
        b = m.classify_retained(self.item(remote_host='github.com', skip_reason='ambiguous PR'))
        self.assertTrue(a['needs_decision'])
        self.assertTrue(b['needs_decision'])
        totals = m.project_totals([a, b], [{'repo': '/executed'}])
        self.assertEqual(sum(x['needs_decision_count'] for x in totals), 2)
        self.assertEqual(sum(x['executed_count'] for x in totals), 1)
        self.assertEqual(sum(x['unreclaimed_bytes'] for x in totals), 256)

    def test_local_dirty_does_not_claim_a_remote_lookup_failed(self):
        o = m.classify_retained(self.item(remote_host='github.com', dirty=True,
                                         skip_reason='worktree has staged, unstaged, or untracked changes'))
        self.assertEqual(o['reasons'], ['dirty(未提交)'])
        self.assertEqual(o['remote_status'], '未核验（本地条件未通过）')

    def test_discovery_failure_is_not_an_empty_repository_list(self):
        with mock.patch.object(m, 'run', return_value=(124, '/partial/.git', 'timeout')):
            with self.assertRaises(RuntimeError):
                m.discover_repos('/root')

    def test_open_mr_on_reused_branch_takes_priority_over_old_merged_mr(self):
        def run(cmd, **kw):
            state = cmd[cmd.index('--state') + 1]
            return 0, json.dumps({'ok': True, 'result': [dict(iid=1, state=state, source_branch='x', source_commit=HEAD)]}), ''
        with mock.patch.object(m, 'run', side_effect=run):
            self.assertEqual(m.gongfeng_lookup('p/r', 'x', 'secret', head=HEAD)['state'], 'opened')

    def test_full_head_equality(self):
        self.assertTrue(m.exact_head(HEAD, HEAD))
        for source, head in [(HEAD[:12], HEAD), (HEAD, HEAD[:12]), ('a'*12+'b'*28, HEAD), (None, HEAD)]:
            self.assertFalse(m.exact_head(source, head))

    def test_remote_parser_does_not_confuse_hosts_or_ssh_ports(self):
        for url, expected in [
            ('git@git.example.invalid:p/r.git', ('p/r', 'git.example.invalid')),
            ('ssh://git@git.example.invalid:22/p/r.git', ('p/r', 'git.example.invalid')),
            ('https://github.com/p/r.git', ('p/r', 'github.com')),
            ('https://notgithub.com/p/r.git', ('p/r', 'notgithub.com'))]:
            with mock.patch.object(m, 'run', return_value=(0, url, '')):
                self.assertEqual(m.remote_slug('/repo'), expected)

    def test_usage_failed_process_with_zero_in_output_is_unknown(self):
        for rc, out in [(1, '{"count":0}'), (0, '[]'), (0, '{"count":false}'),
                        (0, '{"count":0,"error":"partial"}'), (0, '{"count":-1}')]:
            with mock.patch.object(m, 'run', return_value=(rc, out, '')):
                self.assertIsNone(m.probe_usage('/tree')['count'])

    def test_mr_lookup_prioritizes_open_and_exact_merged_heads_and_all_pages(self):
        seen = []
        def run(cmd, **kw):
            seen.append(cmd)
            state = cmd[cmd.index('--state') + 1]
            rows = [] if state == 'opened' else [
                dict(iid=1, state=state, source_branch='x', source_commit='b'*40),
                dict(iid=2, state=state, source_branch='x', source_commit=HEAD)]
            return 0, json.dumps(dict(ok=True, result=rows)), ''
        with mock.patch.object(m, 'run', side_effect=run):
            result = m.gongfeng_lookup('p/r', 'x', 'secret', head=HEAD)
        self.assertEqual(result['iid'], 2)
        self.assertEqual(seen[0][seen[0].index('--state')+1], 'opened')
        self.assertTrue(all('--all' in cmd for cmd in seen))

    def test_malformed_mr_response_cannot_be_none_or_leak_api_error(self):
        for payload in [[], dict(ok=True, result={}), dict(ok=True, result=[1]),
                        dict(ok=False, error={'message': 'secret'})]:
            with mock.patch.object(m, 'run', return_value=(0, json.dumps(payload), '')):
                result = m.gongfeng_lookup('p/r', 'x', 'secret')
            self.assertEqual(result['state'], 'error')
            self.assertNotIn('secret', json.dumps(result))


class CollectorTests(unittest.TestCase):
    def setUp(self):
        example_site = Path(__file__).resolve().parents[1] / 'devcloud.example.gitconfig'
        self.enterContext(mock.patch.dict(os.environ, {'MOLE_DEVCLOUD_SITE': str(example_site)}))

    def collect(self, *, execute=False, host='git.example.invalid', bb_ok=True,
                usage=0, refresh=None, submodule=False, dirty=False, owner_error=False,
                age=200, apply_error=False, alias=False, bb_alias=False):
        temp = tempfile.TemporaryDirectory(prefix='patrol-test-report-')
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        repo, tree = root/'repo', root/'tree'
        repo.mkdir()
        tree.mkdir()
        owner_tree = tree
        if alias:
            tree = root/'tree-alias'
            tree.symlink_to(owner_tree, target_is_directory=True)
        state = root/'state.json'
        original = {'version': 1, 'worktrees': {}, 'history': [], 'run_health': {'exceptions': {'old': {'notified': True}}}}
        state.write_text(json.dumps(original))
        entries = [dict(path=str(repo), head=HEAD, branch='main'),
                   dict(path=str(tree), head=HEAD, branch='x')]
        tool_calls = []
        def run(cmd, **kw):
            if cmd[:3] == ['bb', 'environment', 'list']:
                return (0, json.dumps([{'id': 'env-live', 'path': str(owner_tree)}] if bb_alias else []), '') if bb_ok else (1, '', '')
            if cmd[:3] == ['bb', 'thread', 'list']:
                return 0, '[]', ''
            if '--sweep-only' in cmd:
                tool_calls.append(cmd)
                return 0, '{"branch_sweep":{"branches_removed":[]}}', ''
            if '--apply' in cmd:
                tool_calls.append(cmd)
                return 0, json.dumps({'entries': [{'path': str(owner_tree), 'action': 'removed'}],
                                     'branch_sweep': {'branches_removed': [{'branch': 'orphan'}]}}), ''
            if 'status' in cmd:
                return (1, '', '') if dirty is None else (0, ' M x' if dirty else '', '')
            if 'branch' in cmd and '--format=%(refname:short)' in cmd:
                return 0, 'main\nx\n', ''
            return 0, '', ''
        def owner(*args):
            if owner_error:
                return str(repo), 'repo', 1, {'error': 'API failure'}, None
            return str(repo), 'repo', 0, {'eligible': 1, 'entries': [
                dict(path=str(owner_tree), action='would-remove', head=HEAD, age_hours=age,
                     approval_token='token', remote_proof={'description': 'remote tip'})]}, '/tmp/report.approval.json'
        calls = []
        def probe(path, method='serial'):
            if alias and host == 'github.com':
                self.assertEqual(path, str(owner_tree))
            count = refresh if method == 'execute-refresh' and refresh is not None else usage
            calls.append(method)
            return dict(count=count)
        def apply(*args):
            return {'error': 'test failure', 'entries': []} if apply_error else {
                'entries': [dict(path=str(tree), action='removed', branch_deletion={'deleted': True})]}
        argv = ['patrol', '--worktree-tool', '/mock/owner', '--round-dir', str(root/'round'),
                '--state', str(state), '--mirror-root', str(root/'mirror')]
        if execute:
            argv += ['--execute']
        with contextlib.ExitStack() as stack:
            for name, value in dict(STATE_DIR=str(root/'status'), discover_repos=lambda _: [str(repo)],
                worktree_entries=lambda _: (copy.deepcopy(entries), True),
                git_ignored=lambda _: ({'count': 0, 'digest': '', 'sample': [], 'local_tree': None}, None),
                worktree_age_hours=lambda _: age, du_bytes_last_active=lambda *a, **kw: (256, '2026-09-20T00:00:00+0800'),
                artifact_bytes=lambda _: (0, []), remote_slug=lambda _: ('p/r', host),
                gongfeng_token=lambda: ('secret', 'mock'), probe_usage=probe,
                worktree_has_submodules=lambda _: submodule, branch_standing=lambda *a: None,
                gather_infra=lambda: {'cleaner': {'last': {}, 'consecutive_failures': 0}, 'docker_df': [], 'listeners': []},
                df_pct=lambda _: 50).items():
                stack.enter_context(mock.patch.object(m, name, value))
            lookup = stack.enter_context(mock.patch.object(m, 'gongfeng_lookup', return_value={
                'state': 'merged', 'source_commit': HEAD, 'iid': 3, 'url': 'https://git.example.invalid/p/r/-/merge_requests/3'}))
            applier = stack.enter_context(mock.patch.object(m, 'owner_tool_gongfeng_apply', side_effect=apply))
            stack.enter_context(mock.patch.object(m, 'owner_tool_dry_run', side_effect=owner))
            stack.enter_context(mock.patch.object(m, 'run', side_effect=run))
            stack.enter_context(mock.patch('sys.argv', argv))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            rc = m.main()
        evidence = json.loads((root/'round/evidence.json').read_text())
        summary = json.loads((root/'round/summary.json').read_text())
        self.assertEqual(evidence['summary'], summary)
        self.assertEqual(json.loads((root/'mirror/round/summary.json').read_text()), summary)
        self.assertEqual(json.loads((root/'round/run-status.json').read_text()), evidence['run_status'])
        if execute:
            self.assertEqual(json.loads((root/'round/raw/state_snapshot.json').read_text()), json.loads(state.read_text()))
        if not execute:
            self.assertEqual(json.loads(state.read_text()), original)
            self.assertFalse((root/'status/run-status.json').exists())
            applier.assert_not_called()
        self.assertEqual(summary['needs_decision_count'] + summary['retained_count'], len(evidence['unreclaimed']))
        self.assertEqual(sum(x['unreclaimed_count'] for x in summary['projects']), len(evidence['unreclaimed']))
        return evidence, applier.call_count, lookup.call_count, rc, (root/'round/report.md').read_text(), tool_calls

    def test_preview_produces_consistent_report_without_execution_or_state_changes(self):
        e, _, _, rc, report, _ = self.collect()
        self.assertEqual(rc, 0)
        self.assertEqual(e['summary']['eligible_count'], 1)
        self.assertEqual(e['summary']['needs_decision_count'], 0)
        self.assertNotIn('非 GitHub host', report)
        self.assertIn('精确 head', report)

    def test_submodule_is_real_blocker_and_not_eligible(self):
        e, applies, _, _, report, _ = self.collect(execute=True, submodule=True)
        self.assertEqual(applies, 0)
        self.assertEqual(e['summary']['eligible_count'], 0)
        self.assertEqual(e['summary']['needs_decision_count'], 1)
        self.assertIn('含 submodule', report)

    def test_unknown_dirty_bb_or_usage_never_reaches_gongfeng_apply(self):
        for change in [dict(dirty=None), dict(bb_ok=False), dict(usage=None), dict(refresh=1)]:
            with self.subTest(change=change):
                e, applies, _, _, _, _ = self.collect(execute=True, **change)
                self.assertEqual(applies, 0)
                self.assertEqual(e['summary']['executed_count'], 0)
                self.assertEqual(e['summary']['eligible_count'], 0)

    def test_success_leaves_only_executed_and_consistent_objects(self):
        e, applies, _, rc, _, _ = self.collect(execute=True)
        self.assertEqual(applies, 1)
        self.assertEqual(rc, 0)
        self.assertEqual(e['summary']['executed_count'], 1)
        self.assertEqual(e['unreclaimed'], [])
        self.assertEqual(e['objects'][1]['decision'], '已清理')

    def test_github_error_is_degraded_and_not_suppressed_as_other_host(self):
        e, _, lookups, rc, _, _ = self.collect(host='github.com', owner_error=True)
        self.assertEqual(lookups, 0)
        self.assertEqual(e['run_status']['status'], 'degraded')
        self.assertIn('owner-tool-error', [x['code'] for x in e['exceptions']])
        self.assertEqual(e['unreclaimed'][0]['reasons'], ['执行器查询失败'])

    def test_other_hosts_do_not_get_sent_to_gongfeng(self):
        e, applies, lookups, _, report, _ = self.collect(host='gitlab.example.com', execute=True)
        self.assertEqual((applies, lookups), (0, 0))
        self.assertIn('不支持的远端 host', report)

    def test_execution_failure_is_reported_without_crashing(self):
        e, _, _, rc, report, _ = self.collect(execute=True, apply_error=True)
        self.assertEqual(rc, 1)
        self.assertEqual(e['run_status']['status'], 'failed')
        self.assertIn('执行失败', report)
        self.assertEqual(e['summary']['executed_count'], 0)

    def test_young_or_unknown_age_is_not_eligible_or_counted_over_seven_days(self):
        for age in [20, None]:
            with self.subTest(age=age):
                e, applies, _, _, _, _ = self.collect(age=age)
                self.assertEqual(e['summary']['eligible_count'], 0)
                self.assertEqual(e['summary']['unreclaimed_over_7d_count'], 0)
                self.assertEqual(e['summary']['age_unknown_count'], int(age is None))

    def test_owner_realpath_result_matches_registered_symlink_in_preview_and_apply(self):
        e, _, _, _, report, _ = self.collect(host='github.com', alias=True)
        self.assertEqual(e['summary']['eligible_count'], 1)
        self.assertNotIn('执行器未评估', report)
        e, _, _, _, _, _ = self.collect(host='github.com', alias=True, execute=True)
        self.assertEqual(e['summary']['executed_count'], 1)
        self.assertTrue(e['executed'][0]['path'].endswith('tree-alias'))
        self.assertEqual(e['unreclaimed'], [])

    def test_bb_realpath_ownership_blocks_a_registered_alias(self):
        e, _, _, _, _, cmds = self.collect(host='github.com', alias=True, bb_alias=True, execute=True)
        self.assertEqual(e['summary']['eligible_count'], 0)
        self.assertIn('活跃 BB 环境', e['unreclaimed'][0]['reasons'])
        self.assertFalse(any('--approve' in cmd for cmd in cmds))

    def test_github_pr_candidate_still_obeys_patrol_age_floor(self):
        e, _, _, _, _, cmds = self.collect(host='github.com', execute=True, age=20)
        self.assertEqual(e['summary']['owner_eligible_count'], 1)
        self.assertEqual(e['summary']['eligible_count'], 0)
        self.assertFalse(any('--approve' in cmd for cmd in cmds))

    def test_decision_beyond_thirty_rows_is_never_truncated(self):
        e, _, _, _, _, _ = self.collect()
        base = e['unreclaimed'][0]
        e['unreclaimed'] = [dict(copy.deepcopy(base), path=f'/a/{i:02d}') for i in range(35)]
        decision = dict(copy.deepcopy(base), repo='/zz-last', path='/zz-last/decision', needs_decision=True, decision='需决策')
        e['unreclaimed'].append(decision)
        e['summary'].update(needs_decision_count=1, retained_count=35, unreclaimed_over_7d_count=36)
        report = m.render_report(e)
        self.assertIn('/zz-last/decision', report)
        self.assertIn('展示 31 / 36 项', report)
        self.assertIn('不清理 35 · 需决策 1', report)

    def test_github_apply_records_orphan_sweep_results(self):
        e, _, _, _, _, cmds = self.collect(host='github.com', execute=True)
        self.assertEqual(e['summary']['orphan_branches_removed_count'], 1)
        self.assertTrue(any('--approve' in cmd for cmd in cmds))


if __name__ == '__main__':
    unittest.main()
