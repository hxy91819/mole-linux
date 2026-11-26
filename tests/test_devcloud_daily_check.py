"""Regressions for cleanup reporting and daily checks; all mutations use fixtures."""
from datetime import date, datetime, time, timedelta
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / '.claude/skills/devcloud-storage-upkeep/scripts'


def load(path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


daily = load(SCRIPTS / 'daily_git_cleanup_check.py')


@pytest.mark.parametrize('missing_cli', [False, True])
def test_precommit_rejects_unsynced_staged_prompt_even_when_worktree_matches(tmp_path, missing_cli):
    prompt = tmp_path / 'docs/DEVCLOUD_DAILY_CHECK_PROMPT.md'
    prompt.parent.mkdir()
    script = tmp_path / 'scripts/sync_daily_check_prompt.py'
    script.parent.mkdir()
    shutil.copyfile(ROOT / 'scripts/sync_daily_check_prompt.py', script)
    subprocess.run(['git', 'init', '-q'], cwd=tmp_path, check=True)
    prompt.write_text('staged prompt\n')
    subprocess.run(['git', 'add', str(prompt)], cwd=tmp_path, check=True)
    prompt.write_text('live prompt\n')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name in ('git', 'grep', 'bash'):
        (bin_dir / name).symlink_to(shutil.which(name))
    (bin_dir / 'python3').symlink_to(sys.executable)
    if not missing_cli:
        bb = bin_dir / 'bb'
        bb.write_text('#!/bin/sh\n'
                      """printf '%s\\n' '{"id":"auto_5gacevqssoe","projectId":"proj_8d3wrcajxn","execution":{"mode":"agent","prompt":"live prompt\\n"}}'\n""")
        bb.chmod(0o755)
    out = subprocess.run([shutil.which('bash'), str(ROOT / '.githooks/pre-commit')],
                         cwd=tmp_path, env=dict(os.environ, PATH=str(bin_dir)),
                         capture_output=True, text=True)
    assert out.returncode == 1
    if missing_cli:
        assert 'BB CLI (bb) is required' in out.stdout
    else:
        assert '+staged prompt' in out.stdout and '--apply' in out.stdout
sweep = load(SCRIPTS / 'git_tree_sweep.py')
GIB = 2**30
DAYS = [date(2026, 11, 21) + timedelta(days=i) for i in range(3)]


def kept(path, size, reason='locked', **fields):
    return dict(path=path, bytes=size, reason=reason, action='keep', **fields)


def test_retention_ranks_by_both_dimensions_without_counting_nested_bytes_twice():
    rows = [
        kept('/data/code/kept', 21 * GIB, 'locked: reason details'),
        kept('/data/code/kept/nested', 4 * GIB),
        kept('/data/code/a', 4, 'recent'),
        kept('/data/code/b', None, 'recent'),
        kept('/data/code/c', 3, 'recent'),
        dict(kept('/data/code/d', 50 * GIB), result='removed'),
        dict(kept('/data/code/e', 2 * GIB, 'final refusal: detail'), action='remove', result='kept'),
    ]
    ranking = sweep.summarize(rows)['retention']
    assert ranking['by_count'] == [
        {'reason': 'recent', 'count': 3}, {'reason': 'locked', 'count': 2}, {'reason': 'final refusal', 'count': 1}]
    assert [(row['reason'], row['bytes'], row['unmeasured']) for row in ranking['by_bytes']] == [
        ('locked', 21 * GIB, 0), ('final refusal', 2 * GIB, 0), ('recent', 7, 1)]
    assert ranking['by_bytes'][0]['largest_paths'] == [{'path': '/data/code/kept', 'bytes': 21 * GIB}]


def test_environment_retention_distinguishes_local_unknown_and_released_paths(tmp_path):
    local = tmp_path / 'used'
    local.mkdir()
    (local / 'data').write_bytes(b'data')
    rows = [
        kept(str(local), None, 'unarchived threads', host_id='local'),
        kept(str(local), None, 'different/unknown host', host_id='remote'),
        dict(kept(str(local), None, host_id='local'), action='release-protection'),
        kept(str(local), None, 'environment destroyed', host_id='local'),
    ]
    ranked = sweep.environment_retention(rows, 'local')['by_bytes']
    assert [(row['reason'], row['unmeasured']) for row in ranked] == [
        ('unarchived threads', 0), ('different/unknown host', 1)]
    assert ranked[0]['bytes'] > 0
    assert ranked[1]['bytes'] is None


@pytest.mark.parametrize('stage', list(daily.STAGES))
@pytest.mark.parametrize('size,alert', [(20 * GIB, False), (20 * GIB + 1, True)])
def test_large_retention_threshold_for_each_stage(stage, size, alert):
    rows = [kept('/data/code/large', size, reason='Git registration unavailable')]
    reports = {(stage, 'today'): dict(complete=True, items=rows, summary=sweep.summarize(rows))}
    result = daily.assess(DAYS, {DAYS[-1]: 'today'}, reports, [{}, {}, {}])
    assert bool(result['alerts']) is alert
    if alert:
        [item] = result['alerts']
        assert item['stage'] == stage
        assert item['reason'] == 'Git registration unavailable'
        assert item['largest_paths'] == [{'path': '/data/code/large', 'bytes': size}]


@pytest.mark.parametrize('stage', ['clones', 'linked'])
@pytest.mark.parametrize('case', ['growth', 'deleted', 'incomplete', 'missing-day', 'unknown-size', 'flat'])
def test_three_completed_zero_delete_days_only_alert_for_matching_growth(stage, case):
    invocations = {day: str(day) for day in DAYS}
    reports = {(stage, invocations[day]): dict(complete=True, items=[kept('/tmp/project/sub', 1)]) for day in DAYS}
    root = '/data/system-tmp'
    sizes = [{root: size, '/data/code': size + 1} for size in (101, 102, 103)]
    middle = reports[(stage, invocations[DAYS[1]])]
    if case == 'deleted':
        middle['items'][0]['result'] = 'removed'
    elif case == 'incomplete':
        middle['complete'] = False
    elif case == 'missing-day':
        del invocations[DAYS[1]]
    elif case == 'unknown-size':
        sizes[1][root] = None
    elif case == 'flat':
        sizes[2][root] = sizes[1][root]
    result = daily.assess(DAYS, invocations, reports, sizes)
    if case == 'growth':
        [alert] = result['alerts']
        assert alert['stage'] == stage
        assert alert['path'] == root
        assert alert['bytes_by_day'] == [101, 102, 103]
        assert alert['retention_reasons'] == ['locked']
    else:
        assert result['alerts'] == []


def test_only_scheduled_invocation_with_execute_report_counts(tmp_path):
    today = DAYS[-1]
    invocation = 'a' * 32
    scheduled_for = int(datetime.combine(today, time(6, 30), daily.LOCAL).timestamp() * 1000)
    run = dict(scheduledFor=scheduled_for, status='succeeded', output=f'invocation ID: {invocation}')
    runs = [dict(run, trigger='manual'), dict(run, trigger='schedule')]
    assert daily.scheduled_invocations(runs, DAYS) == {today: invocation}
    assert daily.scheduled_invocations([runs[1], runs[1]], DAYS) == {today: None}
    stamp = (datetime.combine(today, time(6, 30), daily.LOCAL)
             .astimezone(daily.timezone.utc).strftime('%Y%m%dT%H%M%SZ'))
    path = tmp_path / f'linked-worktree-sweep-{stamp}.json'
    report = dict(policy='linked', invocation_id=invocation, complete=True)
    path.write_text(json.dumps(dict(report, mode='dry-run')))
    assert daily.collect_reports(tmp_path, DAYS) == {}
    actual = dict(report, mode='execute')
    path.write_text(json.dumps(actual))
    assert daily.collect_reports(tmp_path, DAYS) == {('linked', invocation): actual}


@pytest.mark.parametrize('version', ['0.49.0', '0.46.1'])
def test_clean_path_does_not_reuse_injected_bb_cli(tmp_path, monkeypatch, version):
    clean = tmp_path / 'bb'
    clean.write_text(f'#!/bin/sh\n[ -z "$BB_CLI" ] || exit 7\nprintf "%s\\n" {version}\n')
    clean.chmod(0o755)
    monkeypatch.setenv('BB_CLI', '/not-the-clean-path/bb')
    server = tmp_path / 'server'
    server.write_text('#!/bin/sh\necho 0.49.0\n')
    server.chmod(0o755)
    observed = daily.version_check([str(server)], str(tmp_path))
    assert observed['match'] is (version == '0.49.0')
    assert observed['cli'] == str(clean)
    assert observed['cli_version'] == version
    assert observed['server_version'] == '0.49.0'


def test_prompt_check_and_apply_read_back_exact_full_file(tmp_path, monkeypatch):
    sync = load(ROOT / 'scripts/sync_daily_check_prompt.py')
    source = tmp_path / 'prompt.md'
    source.write_bytes('检查\n\n'.encode())
    state = tmp_path / 'stored'
    state.write_text('检查\n')
    calls = tmp_path / 'calls'
    cli = tmp_path / 'bb'
    cli.write_text(f'''#!{sys.executable}
import json, os, sys
from pathlib import Path
state = Path(os.environ["MOCK_PROMPT"])
args = sys.argv[1:]
assert args[0] == "automation"
assert args[2:6] == [{sync.AUTOMATION!r}, "--project", {sync.PROJECT!r}, "--json"]
with open(os.environ["MOCK_CALLS"], "a") as out:
    print(args[1], file=out)
if args[1] == "update":
    assert args[6] == "--prompt"
    state.write_text(args[7].rstrip() if os.getenv("MOCK_TRIM") else args[7])
print(json.dumps(dict(id=args[2], projectId=args[4], execution=dict(mode="agent", prompt=state.read_text()))))
''')
    cli.chmod(0o755)
    monkeypatch.setenv('PATH', str(tmp_path))
    monkeypatch.setenv('MOCK_PROMPT', str(state))
    monkeypatch.setenv('MOCK_CALLS', str(calls))
    monkeypatch.setattr(sync, 'SOURCE', source)
    assert sync.main(['--check']) == 1
    assert calls.read_text().splitlines() == ['show']
    assert sync.main(['--apply']) == 0
    assert state.read_bytes() == source.read_bytes()
    assert calls.read_text().splitlines() == ['show', 'show', 'update', 'show']
    assert sync.main(['--check']) == 0
    assert sync.main(['--apply']) == 0  # No write when already synchronized.
    assert calls.read_text().splitlines()[-2:] == ['show', 'show']
    state.write_text('drift')
    monkeypatch.setenv('MOCK_TRIM', '1')
    assert sync.main(['--apply']) == 1  # Update success alone proves nothing.


@pytest.mark.parametrize('reasons', [
    ['BB project source'] * 3,
    ['BB environment'] * 3,
    ['unarchived threads'] * 3,
    ['dirty working tree (including untracked)'] * 3,
    ['unpushed refs and no personal or internal remote'] * 3,
    ['process 10 cwd', 'process 11 cwd', 'process 12 cwd'],
])
@pytest.mark.parametrize('scenario', ['growth', 'flat', 'small', 'missing', 'partial', 'unmeasured'])
def test_normal_protection_only_alerts_on_valid_three_day_growth(reasons, scenario):
    stage = 'bb-environments'
    sizes = [30 * GIB, 31 * GIB, 33 * GIB]
    if scenario == 'flat':
        sizes[1] = sizes[0]
    if scenario == 'small':
        sizes[-1] -= 1
    reports = {}
    for day, reason, size in zip(DAYS, reasons, sizes):
        rows = [kept('/data/big', size, reason=reason)]
        if day == DAYS[1] and scenario == 'unmeasured':
            rows.append(kept('/data/unknown', None, reason=reason))
        reports[(stage, day)] = dict(complete=(scenario != 'partial' or day != DAYS[1]),
                                     items=rows, summary=sweep.summarize(rows))
    if scenario == 'missing':
        del reports[(stage, DAYS[1])]
    result = daily.assess(DAYS, {day: day for day in DAYS}, reports, [{}, {}, {}])
    if scenario == 'growth':
        [alert] = result['alerts']
        assert alert['kind'] == 'retention_protection_continuous_growth'
        assert alert['growth_total_bytes'] == 3 * GIB
        assert alert['largest_paths'][0]['path'] == '/data/big'
    else:
        assert result['alerts'] == []


@pytest.mark.parametrize(('stage', 'reason'), [
    ('linked', 'active within 24h'),
    ('clones', 'active within 7d (168h)'),
    ('linked', 'active within 12h'),
    ('bb-environments', 'BB activity within 24h'),
    ('bb-environments', 'provider workspace active within 24h'),
    ('linked', 'dirty working tree (including untracked); archive requires 30 days idle'),
    ('clones', 'stash exists; dirty archive requires 30 days idle'),
])
def test_time_window_growth_stays_in_retention_rankings_without_alert(stage, reason):
    reports = {}
    for day, gib in zip(DAYS, [30, 32, 35]):
        rows = [kept('/data/recent', gib * GIB, reason=reason)]
        reports[(stage, day)] = dict(complete=True, items=rows, summary=sweep.summarize(rows))
    result = daily.assess(DAYS, {day: day for day in DAYS}, reports, [{}, {}, {}])
    assert result['alerts'] == []
    ranking = result['stages'][stage]['retention']
    assert ranking['by_bytes'][0]['reason'] == reason
    assert ranking['by_bytes'][0]['bytes'] == 35 * GIB
    assert ranking['by_count'][0]['reason'] == reason
    assert ranking['by_count'][0]['count'] == 1


@pytest.mark.parametrize('reason', ['Git registration unavailable', 'gitdir missing',
                                    'remote query incomplete', 'check unknown',
                                    'clone recheck exceeded 120s; retry next round',
                                    'unresolved project reference', 'name collision'])
def test_evidence_gap_is_an_absolute_threshold_category(reason):
    assert daily.retention_class(reason) == 'evidence-gap'


@pytest.mark.parametrize('execute', [False, True])
def test_bb_storage_inventory_defers_unbound_destructive_operations(execute, monkeypatch):
    storage = load(SCRIPTS / 'bb_storage_maintenance.py')
    host = 'local'
    scan = dict(hostId=host, scannedAt=2, orphanCount=2, orphanBytes=10,
                leftoverWorktreeBytes=20, leftoverWorktrees=[{'path': '/local/worktree'}],
                externalWorktrees=[{'path': '/local/external'}],
                developerStorage={'path': '/local/dev', 'entries': [
                    dict(name='missing', current=False, sourcePathState='missing', sizeBytes=30),
                    dict(name='unknown', current=False, sourcePathState='unknown', sizeBytes=40),
                    dict(name='current', current=True, sourcePathState='missing', sizeBytes=50),
                ]})
    calls = []
    def call(*args):
        calls.append(args)
        if args[:2] == ('machine', 'list'):
            return [dict(id=host, type='persistent', status='connected', machineProviderId=None)]
        if args == ('storage', 'retention'):
            return {'policy': {'deleteDevDataOnCheckoutRemoval': False}}
        if args == ('storage', 'usage', '--machine', host):
            return {'report': {'scannedAt': 1}}
        if args == ('storage', 'usage', '--machine', host, '--rescan'):
            return {'report': scan, 'scan': {'state': 'idle'}}
        raise AssertionError(args)
    result = storage.maintain(call, execute)
    assert result['removed_bytes'] == 0
    assert result['external_worktree_count'] == 1
    assert result['actions']['remove-dev-instances']['paths'] == ['/local/dev/missing']
    assert result['actions']['remove-dev-instances']['unknown_entries'] == ['unknown']
    assert {row['result'] for row in result['actions'].values()} == {'deferred' if execute else 'preview'}


def test_bb_storage_rescan_refuses_enabled_developer_auto_cleanup():
    storage = load(SCRIPTS / 'bb_storage_maintenance.py')
    def call(*args):
        assert args == ('storage', 'retention')
        return {'policy': {'deleteDevDataOnCheckoutRemoval': True}}
    with pytest.raises(ValueError, match='may auto-remove'):
        storage.fresh_report(call, 'local')


def test_bb_storage_rescan_rejects_stale_report():
    storage = load(SCRIPTS / 'bb_storage_maintenance.py')
    def call(*args):
        if args == ('storage', 'retention'):
            return {'policy': {'deleteDevDataOnCheckoutRemoval': False}}
        return {'report': {'hostId': 'local', 'scannedAt': 1}, 'scan': {'state': 'idle'}}
    with pytest.raises(ValueError, match='no fresh'):
        storage.fresh_report(call, 'local')
