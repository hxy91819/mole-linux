from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parents[1] / "scripts" / "cleanup_worktrees.py"
REAL_GIT = "/usr/bin/git"
SCRIPT_SPEC = spec_from_file_location("cleanup_worktrees", SCRIPT)
assert SCRIPT_SPEC is not None and SCRIPT_SPEC.loader is not None
CLEANUP_WORKTREES = module_from_spec(SCRIPT_SPEC)
sys.modules[SCRIPT_SPEC.name] = CLEANUP_WORKTREES
SCRIPT_SPEC.loader.exec_module(CLEANUP_WORKTREES)


class CleanupWorktreesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.repo = self.root / "repository"
        self.state_home = self.root / "state"
        self.fake_bin = self.root / "bin"
        self.fake_bin.mkdir()
        self.remote_heads: dict[str, str] = {}
        self.fake_pr_state = "CLOSED"
        self.fake_no_pr = False
        self.fake_durable = True

        self.run_git("init", "-b", "main", str(self.repo), cwd=self.root)
        self.run_git("config", "user.name", "Test User")
        self.run_git("config", "user.email", "test@example.invalid")
        (self.repo / ".gitignore").write_text(
            ".local/\n.env\nignored-*/\n.local-test/\n", encoding="utf-8"
        )
        (self.repo / "tracked.txt").write_text("baseline\n", encoding="utf-8")
        self.run_git("add", ".")
        self.run_git("commit", "-m", "baseline")
        self.default_head = self.run_git("rev-parse", "HEAD").stdout.strip()
        self.worktree = self.add_worktree("task-123")
        self.install_fake_gh()
        self.docker_state = self.root / "docker-state.json"
        self.docker_state.write_text(
            json.dumps({"containers": [], "unavailable": False, "fail_down": False}),
            encoding="utf-8",
        )
        self.service_log = self.root / "services.log"
        self.install_fake_docker()

    def run_git(self, *args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [REAL_GIT, *args],
            cwd=cwd or self.repo,
            check=True,
            capture_output=True,
            text=True,
        )

    def add_worktree(self, branch: str) -> Path:
        path = self.repo / ".worktrees" / branch
        path.parent.mkdir(exist_ok=True)
        self.run_git("worktree", "add", "-b", branch, str(path))
        self.remote_heads[branch] = self.run_git(
            "-C", str(path), "rev-parse", "HEAD"
        ).stdout.strip()
        return path

    def install_fake_gh(self) -> None:
        script = """#!/usr/bin/env python3
import json
import os
import re
import sys

args = sys.argv[1:]
heads = json.loads(os.environ["FAKE_HEADS_JSON"])
state = os.environ["FAKE_PR_STATE"]
no_pr = os.environ["FAKE_NO_PR"] == "1"

def pr_row(branch):
    number_match = re.search(r"(\\d+)$", branch)
    number = int(number_match.group(1)) if number_match else 1
    timestamp = "2026-01-02T03:04:05Z"
    return {
        "number": number,
        "state": state,
        "title": f"Test pull request for {branch}",
        "url": f"https://github.example/pulls/{number}",
        "headRefName": branch,
        "headRefOid": heads[branch],
        "mergedAt": timestamp if state == "MERGED" else None,
        "closedAt": timestamp if state == "CLOSED" else None,
    }

if args[:2] == ["repo", "view"]:
    print(json.dumps({
        "nameWithOwner": "example/repository",
        "defaultBranchRef": {"name": "main"},
    }))
elif args[:2] == ["pr", "view"]:
    number = args[2]
    branch = next((name for name in heads if name.endswith(number)), None)
    if no_pr or branch is None:
        print("pull request not found", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(pr_row(branch)))
elif args[:2] == ["pr", "list"]:
    branch = args[args.index("--head") + 1]
    print(json.dumps([] if no_pr else [pr_row(branch)]))
elif args and args[0] == "api":
    endpoint = args[1]
    if "/commits/" in endpoint:
        if endpoint.endswith("/branches-where-head"):
            commit = endpoint.split("/commits/", 1)[1].split("/", 1)[0]
            print(json.dumps([
                {"name": branch}
                for branch, head in heads.items()
                if head == commit
            ]))
        else:
            print(json.dumps({"sha": os.environ["FAKE_DEFAULT_HEAD"]}))
    elif "/compare/" in endpoint:
        comparison = endpoint.rsplit("/", 1)[-1]
        base = comparison.split("...", 1)[0]
        merge_base = base if os.environ["FAKE_DURABLE"] == "1" else "0" * 40
        print(json.dumps({"merge_base_commit": {"sha": merge_base}}))
    else:
        sys.exit(2)
else:
    sys.exit(2)
"""
        gh = self.fake_bin / "gh"
        gh.write_text(script, encoding="utf-8")
        gh.chmod(0o755)

    def install_fake_docker(self) -> None:
        script = """#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["FAKE_DOCKER_STATE"], encoding="utf-8") as handle:
    state = json.load(handle)
args = sys.argv[1:]
if state.get("unavailable"):
    print("Cannot connect to the Docker daemon", file=sys.stderr)
    sys.exit(1)
if args[:1] == ["ps"]:
    for row in state.get("containers", []):
        print("\\t".join([row["name"], row["project"], row["working_dir"]]))
    sys.exit(0)
if args[:1] == ["compose"]:
    with open(os.environ["FAKE_SERVICE_LOG"], "a", encoding="utf-8") as log:
        log.write("docker " + " ".join(args) + "\\n")
    if state.get("fail_down"):
        print("simulated compose down failure", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
print("unexpected docker invocation: " + " ".join(args), file=sys.stderr)
sys.exit(2)
"""
        docker = self.fake_bin / "docker"
        docker.write_text(script, encoding="utf-8")
        docker.chmod(0o755)

    def set_docker_state(self, **updates: object) -> None:
        state = json.loads(self.docker_state.read_text(encoding="utf-8"))
        state.update(updates)
        self.docker_state.write_text(json.dumps(state), encoding="utf-8")

    def service_commands(self) -> list[str]:
        if not self.service_log.exists():
            return []
        return self.service_log.read_text(encoding="utf-8").splitlines()

    def helper_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment["PATH"] = f"{self.fake_bin}:/usr/bin:/bin"
        environment["XDG_STATE_HOME"] = str(self.state_home)
        environment["FAKE_HEADS_JSON"] = json.dumps(self.remote_heads)
        environment["FAKE_PR_STATE"] = self.fake_pr_state
        environment["FAKE_NO_PR"] = "1" if self.fake_no_pr else "0"
        environment["FAKE_DURABLE"] = "1" if self.fake_durable else "0"
        environment["FAKE_DEFAULT_HEAD"] = self.default_head
        environment["FAKE_DOCKER_STATE"] = str(self.docker_state)
        environment["FAKE_SERVICE_LOG"] = str(self.service_log)
        return environment

    def run_helper(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(self.repo), *args],
            cwd=self.root,
            env=self.helper_environment(),
            capture_output=True,
            text=True,
        )

    def audit(
        self, *extra_args: str
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        result = self.run_helper("--dry-run", "--json", *extra_args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result, json.loads(result.stdout)

    def approval_for(self, report: dict[str, object], path: Path) -> str:
        entry = next(
            item for item in report["entries"] if item["path"] == str(path)
        )
        token = entry["approval_token"]
        self.assertIsInstance(token, str)
        return token

    def test_apply_requires_a_dry_run_approval_source(self) -> None:
        result = self.run_helper("--apply", "--json")

        self.assertEqual(result.returncode, 2)
        self.assertIn("--apply requires", result.stderr)
        self.assertTrue(self.worktree.exists())

    def test_dirty_closed_pr_worktree_is_kept(self) -> None:
        (self.worktree / "uncommitted.txt").write_text("keep me\n", encoding="utf-8")

        _, report = self.audit()

        entry = report["entries"][0]
        self.assertEqual(report["worktree_count_before"], 2)
        self.assertEqual(report["worktree_count_after"], 2)
        self.assertEqual(report["eligible"], 0)
        self.assertEqual(entry["action"], "skip")
        self.assertIn("untracked changes", entry["skip_reason"])
        self.assertTrue((self.worktree / "uncommitted.txt").exists())

    def test_apply_reports_ignored_data_and_removes_only_the_approved_target(self) -> None:
        local_file = self.worktree / ".local" / "notes.txt"
        local_file.parent.mkdir()
        local_file.write_text("restore me\n", encoding="utf-8")
        (self.worktree / ".env").write_text("discard after approval\n", encoding="utf-8")
        _, report = self.audit()
        entry = report["entries"][0]

        self.assertEqual(entry["ignored"]["discarded_sample"], [".env"])
        token = self.approval_for(report, self.worktree)
        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["removed"], 1)
        self.assertEqual(applied["worktree_count_before"], 2)
        self.assertEqual(applied["worktree_count_after"], 1)
        self.assertFalse(self.worktree.exists())
        archive_root = Path(applied["archive_root"])
        self.assertEqual(archive_root, self.repo / ".local" / "worktree-archive")
        entry = next(e for e in applied["entries"] if e["action"] == "removed")
        archive_dir = Path(entry["backup_path"])
        self.assertEqual(archive_dir.parent, archive_root)
        self.assertRegex(archive_dir.name, r"^task-123-\d{8}T\d{6}Z$")
        self.assertEqual(
            (archive_dir / ".local" / "notes.txt").read_text(encoding="utf-8"),
            "restore me\n",
        )
        self.assertFalse((archive_dir / ".local" / ".env").exists())
        self.assertEqual(entry["preservation"]["status"], "archive")
        self.assertEqual(
            entry["preservation"]["moved_to"], str(archive_dir / ".local")
        )
        metadata = json.loads(
            (archive_dir / "metadata.json").read_text(encoding="utf-8")
        )
        self.assertTrue(metadata["content_preserved"])
        self.assertEqual(metadata["source_local_dir"], str(self.worktree / ".local"))
        branches = self.run_git("branch", "--format=%(refname:short)").stdout.splitlines()
        self.assertNotIn("task-123", branches)
        self.assertTrue(entry["branch_deletion"]["deleted"])
        self.assertIn("worktree-removal-proof", entry["branch_deletion"]["proof"])

    def test_locked_worktree_is_kept(self) -> None:
        self.run_git("worktree", "lock", "--reason", "active agent", str(self.worktree))

        _, report = self.audit()

        entry = report["entries"][0]
        self.assertEqual(entry["action"], "skip")
        self.assertEqual(entry["locked_reason"], "active agent")
        self.assertIn("locked", entry["skip_reason"])

    def test_pr_head_that_does_not_contain_local_head_is_kept(self) -> None:
        self.remote_heads["task-123"] = "f" * 40
        self.fake_durable = False

        _, report = self.audit()

        entry = report["entries"][0]
        self.assertEqual(entry["action"], "skip")
        self.assertIn("not contained", entry["skip_reason"])

    def test_branch_without_pr_number_is_looked_up_by_head_branch(self) -> None:
        worktree = self.add_worktree("descriptive-branch")

        _, report = self.audit()

        entry = next(item for item in report["entries"] if item["path"] == str(worktree))
        self.assertEqual(entry["pr"]["head_ref_name"], "descriptive-branch")
        self.assertIsInstance(entry["approval_token"], str)

    def test_old_prless_worktree_landed_on_default_branch_is_eligible(self) -> None:
        self.fake_no_pr = True
        self.remote_heads["task-123"] = "f" * 40
        old = time.time() - 25 * 3600
        os.utime(self.worktree / ".git", (old, old))

        _, report = self.audit()

        entry = report["entries"][0]
        self.assertEqual(entry["expiration_reason"], "landed-on-default")
        self.assertEqual(entry["remote_proof"]["kind"], "default-branch")
        self.assertIsInstance(entry["approval_token"], str)

    def test_old_prless_worktree_pushed_to_remote_branch_is_eligible(self) -> None:
        (self.worktree / "tracked.txt").write_text("pushed branch\n", encoding="utf-8")
        self.run_git("-C", str(self.worktree), "add", "tracked.txt")
        self.run_git("-C", str(self.worktree), "commit", "-m", "pushed branch")
        self.remote_heads["task-123"] = self.run_git(
            "-C", str(self.worktree), "rev-parse", "HEAD"
        ).stdout.strip()
        self.fake_no_pr = True
        self.fake_durable = False
        old = time.time() - 25 * 3600
        os.utime(self.worktree / ".git", (old, old))

        _, report = self.audit()

        entry = report["entries"][0]
        self.assertEqual(entry["remote_proof"]["kind"], "remote-branch-head")
        self.assertEqual(entry["remote_proof"]["branches"], ["task-123"])
        self.assertIsInstance(entry["approval_token"], str)

    def test_remote_branch_witness_change_keeps_stable_approval_valid(self) -> None:
        self.fake_no_pr = True
        old = time.time() - 25 * 3600
        os.utime(self.worktree / ".git", (old, old))
        _, report = self.audit()
        token = self.approval_for(report, self.worktree)
        self.remote_heads["alias-branch"] = self.remote_heads["task-123"]

        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["removed"], 1)
        self.assertFalse(self.worktree.exists())

    def test_recent_prless_worktree_is_kept(self) -> None:
        self.fake_no_pr = True

        _, report = self.audit()

        entry = report["entries"][0]
        self.assertEqual(entry["action"], "skip")
        self.assertIn("younger than 24 hours", entry["skip_reason"])

    def test_state_drift_invalidates_the_approval_token(self) -> None:
        _, report = self.audit()
        token = self.approval_for(report, self.worktree)
        (self.worktree / "new-untracked.txt").write_text("changed\n", encoding="utf-8")

        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 1)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["rejected_approvals"], [token[:12]])
        self.assertEqual(applied["removed"], 0)
        self.assertTrue(self.worktree.exists())

    def test_stale_approval_does_not_block_other_approved_target(self) -> None:
        second = self.add_worktree("task-456")
        _, report = self.audit()
        first_token = self.approval_for(report, self.worktree)
        second_token = self.approval_for(report, second)
        (self.worktree / "new-untracked.txt").write_text("changed\n", encoding="utf-8")

        result = self.run_helper(
            "--apply",
            "--approve",
            first_token,
            "--approve",
            second_token,
            "--json",
        )

        self.assertEqual(result.returncode, 1)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["rejected_approvals"], [first_token[:12]])
        self.assertEqual(applied["removed"], 1)
        self.assertTrue(self.worktree.exists())
        self.assertFalse(second.exists())

    def test_state_drift_during_backup_stops_removal(self) -> None:
        local_file = self.worktree / ".local" / "notes.txt"
        local_file.parent.mkdir()
        local_file.write_text("backup me\n", encoding="utf-8")
        environment = self.helper_environment()
        args = Namespace(
            repo=str(self.repo),
            apply=False,
            backup_root=None,
            min_no_pr_age_hours=24.0,
        )
        real_copytree = CLEANUP_WORKTREES.shutil.copytree

        def copy_then_mutate(*copy_args: object, **copy_kwargs: object) -> object:
            copied = real_copytree(*copy_args, **copy_kwargs)
            local_file.write_text("changed during backup\n", encoding="utf-8")
            return copied

        with mock.patch.dict(os.environ, environment, clear=True):
            report = CLEANUP_WORKTREES.discover(args)
            token = self.approval_for(report, self.worktree)
            with mock.patch.object(
                CLEANUP_WORKTREES.shutil,
                "copytree",
                side_effect=copy_then_mutate,
            ):
                CLEANUP_WORKTREES.apply_candidates(report, [token])

        entry = report["entries"][0]
        self.assertEqual(entry["action"], "failed")
        self.assertIn("ignored files changed", entry["error"])
        self.assertTrue(self.worktree.exists())

    def test_duplicate_approval_token_is_rejected_before_removal(self) -> None:
        _, report = self.audit()
        token = self.approval_for(report, self.worktree)

        result = self.run_helper(
            "--apply",
            "--approve",
            token,
            "--approve",
            token,
            "--json",
        )

        self.assertEqual(result.returncode, 1)
        error = json.loads(result.stdout)
        self.assertIn("Duplicate approval tokens", error["error"])
        self.assertTrue(self.worktree.exists())

    def test_text_report_discloses_truncated_ignored_deletion_scope(self) -> None:
        local_file = self.worktree / ".local" / "notes.txt"
        local_file.parent.mkdir()
        local_file.write_text("backup me\n", encoding="utf-8")
        for index in range(51):
            ignored_file = self.worktree / f"ignored-{index:02d}" / "data.txt"
            ignored_file.parent.mkdir()
            ignored_file.write_text("discard me\n", encoding="utf-8")
        _, report = self.audit()
        output = io.StringIO()

        with redirect_stdout(output):
            CLEANUP_WORKTREES.print_text(report)

        rendered = output.getvalue()
        self.assertIn("archive_root:", rendered)
        self.assertIn("local_preservation: archive", rendered)
        self.assertIn("local_archive_destination:", rendered)
        self.assertIn("preserved: notes.txt", rendered)
        self.assertIn("ignored_roots_deleted_count: 51", rendered)
        self.assertIn("ignored_roots_deleted_sample_truncated: yes", rendered)

    def test_worktree_without_local_is_removed_without_an_archive(self) -> None:
        _, report = self.audit()
        entry = report["entries"][0]

        self.assertEqual(entry["preservation"]["status"], "none")
        self.assertIsNone(entry["preservation"]["destination"])
        token = self.approval_for(report, self.worktree)
        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["removed"], 1)
        self.assertFalse(self.worktree.exists())
        self.assertFalse((self.repo / ".local" / "worktree-archive").exists())

    def test_worktree_compose_project_is_listed_and_stopped(self) -> None:
        (self.worktree / "deploy").mkdir()
        self.set_docker_state(
            containers=[
                {
                    "name": "yongyuan-ui-gates-postgres-1",
                    "project": "yongyuan-ui-gates",
                    "working_dir": str(self.worktree / "deploy"),
                },
                {
                    "name": "yongyuan-ui-gates-s3-1",
                    "project": "yongyuan-ui-gates",
                    "working_dir": str(self.worktree / "deploy"),
                },
            ]
        )

        _, report = self.audit()
        entry = report["entries"][0]
        services = entry["services"]
        self.assertEqual(services["status"], "would-stop")
        self.assertEqual(
            [project["project"] for project in services["compose_projects"]],
            ["yongyuan-ui-gates"],
        )
        project = services["compose_projects"][0]
        self.assertEqual(project["action"], "would-stop")
        self.assertEqual(
            project["containers"],
            ["yongyuan-ui-gates-postgres-1", "yongyuan-ui-gates-s3-1"],
        )
        self.assertEqual(
            project["working_dirs"], [str(self.worktree / "deploy")]
        )
        self.assertEqual(self.service_commands(), [])

        token = self.approval_for(report, self.worktree)
        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["removed"], 1)
        self.assertEqual(applied["services_stopped"], 1)
        self.assertFalse(self.worktree.exists())
        self.assertEqual(
            self.service_commands(),
            ["docker compose -p yongyuan-ui-gates down"],
        )
        stopped = applied["entries"][0]["services"]["compose_projects"][0]
        self.assertEqual(stopped["action"], "stopped")

    def test_text_report_lists_the_compose_project_it_would_stop(self) -> None:
        (self.worktree / "deploy").mkdir()
        self.set_docker_state(
            containers=[
                {
                    "name": "yongyuan-ui-gates-postgres-1",
                    "project": "yongyuan-ui-gates",
                    "working_dir": str(self.worktree / "deploy"),
                },
                {
                    "name": "yongyuan-ui-gates-s3-1",
                    "project": "yongyuan-ui-gates",
                    "working_dir": str(self.worktree / "deploy"),
                },
            ]
        )

        _, report = self.audit()
        output = io.StringIO()
        with redirect_stdout(output):
            CLEANUP_WORKTREES.print_text(report)

        rendered = output.getvalue()
        self.assertIn("services_would_stop: 1", rendered)
        self.assertIn("compose_project: yongyuan-ui-gates (would-stop)", rendered)
        self.assertIn("containers: yongyuan-ui-gates-postgres-1", rendered)
        self.assertIn("yongyuan-ui-gates-s3-1", rendered)

    def test_mixed_ownership_compose_project_is_not_stopped(self) -> None:
        (self.worktree / "deploy").mkdir()
        self.set_docker_state(
            containers=[
                {
                    "name": "shared-postgres-1",
                    "project": "shared-project",
                    "working_dir": str(self.worktree / "deploy"),
                },
                {
                    "name": "main-postgres-1",
                    "project": "shared-project",
                    "working_dir": str(self.repo / "deploy"),
                },
            ]
        )

        _, report = self.audit()
        entry = report["entries"][0]
        self.assertEqual(entry["services"]["status"], "ambiguous")
        self.assertEqual(entry["services"]["compose_projects"], [])
        self.assertEqual(
            [project["project"] for project in entry["services"]["ambiguous_projects"]],
            ["shared-project"],
        )
        token = self.approval_for(report, self.worktree)
        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["removed"], 1)
        self.assertEqual(self.service_commands(), [])

    def test_worktree_without_compose_project_skips_docker_down(self) -> None:
        _, report = self.audit()
        entry = report["entries"][0]

        self.assertEqual(entry["services"]["status"], "none")
        self.assertEqual(entry["services"]["compose_projects"], [])
        token = self.approval_for(report, self.worktree)
        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["removed"], 1)
        self.assertEqual(self.service_commands(), [])

    def test_compose_projects_outside_worktree_are_not_touched(self) -> None:
        other = self.add_worktree("other")
        self.set_docker_state(
            containers=[
                {
                    "name": "main-postgres-1",
                    "project": "main-project",
                    "working_dir": str(self.repo / "deploy"),
                },
                {
                    "name": "other-postgres-1",
                    "project": "other-project",
                    "working_dir": str(other / "deploy"),
                },
            ]
        )

        _, report = self.audit()
        entry = next(
            item for item in report["entries"] if item["path"] == str(self.worktree)
        )
        self.assertEqual(entry["services"]["status"], "none")
        self.assertEqual(entry["services"]["compose_projects"], [])
        token = self.approval_for(report, self.worktree)
        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["removed"], 1)
        self.assertEqual(self.service_commands(), [])

    def test_docker_unavailable_skips_service_cleanup_without_blocking(self) -> None:
        self.set_docker_state(unavailable=True)

        _, report = self.audit()
        entry = report["entries"][0]
        self.assertEqual(entry["services"]["docker"], "unavailable")
        self.assertEqual(entry["services"]["status"], "docker-unavailable")
        self.assertIn("docker is unavailable", entry["services"]["warning"])
        token = self.approval_for(report, self.worktree)
        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["removed"], 1)
        self.assertEqual(applied["services_docker_unavailable"], 1)
        self.assertEqual(self.service_commands(), [])
        self.assertFalse(self.worktree.exists())
        applied_entry = applied["entries"][0]
        self.assertEqual(applied_entry["services"]["status"], "docker-unavailable")

    def test_compose_down_failure_keeps_worktree(self) -> None:
        (self.worktree / "deploy").mkdir()
        self.set_docker_state(
            containers=[
                {
                    "name": "yongyuan-ui-gates-postgres-1",
                    "project": "yongyuan-ui-gates",
                    "working_dir": str(self.worktree / "deploy"),
                }
            ],
            fail_down=True,
        )

        _, report = self.audit()
        token = self.approval_for(report, self.worktree)
        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 1)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["removed"], 0)
        entry = applied["entries"][0]
        self.assertEqual(entry["action"], "failed")
        self.assertIn("Compose shutdown failed", entry["error"])
        self.assertEqual(entry["services"]["status"], "failed")
        self.assertTrue(self.worktree.exists())
        self.assertEqual(
            self.service_commands(),
            ["docker compose -p yongyuan-ui-gates down"],
        )

    def test_local_test_destroy_runs_before_compose_down(self) -> None:
        (self.worktree / ".local-test" / "run").mkdir(parents=True)
        lt_dir = self.worktree / "bin"
        lt_dir.mkdir()
        lt = lt_dir / "lt"
        lt.write_text(
            """#!/usr/bin/env bash
set -euo pipefail
printf 'lt destroy\\n' >> "$FAKE_SERVICE_LOG"
""",
            encoding="utf-8",
        )
        lt.chmod(0o755)
        exclude = self.repo / ".git" / "info" / "exclude"
        exclude.write_text("bin/lt\n", encoding="utf-8")
        (self.worktree / "deploy").mkdir()
        self.set_docker_state(
            containers=[
                {
                    "name": "yongyuan-ui-gates-postgres-1",
                    "project": "yongyuan-ui-gates",
                    "working_dir": str(self.worktree / "deploy"),
                }
            ]
        )

        _, report = self.audit()
        entry = report["entries"][0]
        self.assertEqual(
            entry["services"]["local_test"]["action"], "would-destroy"
        )
        self.assertEqual(self.service_commands(), [])
        token = self.approval_for(report, self.worktree)
        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.worktree.exists())
        self.assertEqual(
            self.service_commands(),
            [
                "lt destroy",
                "docker compose -p yongyuan-ui-gates down",
            ],
        )

    def test_empty_local_directory_is_not_archived(self) -> None:
        (self.worktree / ".local").mkdir()
        _, report = self.audit()
        entry = report["entries"][0]

        self.assertEqual(entry["preservation"]["status"], "none")
        self.assertEqual(entry["preservation"]["entry_count"], 0)
        token = self.approval_for(report, self.worktree)
        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["removed"], 1)
        self.assertFalse(self.worktree.exists())
        self.assertFalse((self.repo / ".local" / "worktree-archive").exists())

    def test_primary_checkout_without_ignored_local_keeps_worktree(self) -> None:
        local_dir = self.worktree / ".local"
        local_dir.mkdir()
        (local_dir / "RESULT.md").write_text("evidence\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text(".env\nignored-*/\n", encoding="utf-8")
        self.run_git("add", ".gitignore")
        self.run_git("commit", "-m", "stop ignoring local")

        _, report = self.audit()
        entry = report["entries"][0]

        self.assertEqual(entry["action"], "skip")
        self.assertEqual(entry["preservation"]["status"], "blocked")
        self.assertIn("not gitignored", entry["skip_reason"])
        self.assertIsNone(entry["approval_token"])
        self.assertTrue((local_dir / "RESULT.md").exists())
        self.assertFalse((self.repo / ".local" / "worktree-archive").exists())

    def test_oversized_local_file_is_listed_and_not_moved(self) -> None:
        local_dir = self.worktree / ".local"
        local_dir.mkdir()
        (local_dir / "RESULT.md").write_text("keep me\n", encoding="utf-8")
        (local_dir / "trace.bin").write_bytes(b"x" * 4096)
        limit_args = ("--max-local-file-mb", "0.001")

        _, report = self.audit(*limit_args)
        entry = report["entries"][0]
        preservation = entry["preservation"]

        self.assertEqual(preservation["status"], "oversized")
        listed = {item["path"] for item in preservation["oversized_files"]}
        self.assertIn("trace.bin", listed)
        self.assertNotIn("RESULT.md", listed)
        self.assertTrue(
            any("largest file" in reason for reason in preservation["oversized_reasons"])
        )
        self.assertIn("trace.bin", {item["path"] for item in preservation["files"]})
        token = self.approval_for(report, self.worktree)
        result = self.run_helper(
            "--apply", "--approve", token, "--json", *limit_args
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["removed"], 1)
        self.assertFalse(self.worktree.exists())
        applied_entry = next(
            item for item in applied["entries"] if item["action"] == "removed"
        )
        archive_dir = Path(applied_entry["backup_path"])
        self.assertRegex(archive_dir.name, r"^task-123-\d{8}T\d{6}Z$")
        self.assertFalse((archive_dir / ".local").exists())
        metadata = json.loads(
            (archive_dir / "metadata.json").read_text(encoding="utf-8")
        )
        self.assertFalse(metadata["content_preserved"])
        self.assertEqual(metadata["preservation"]["status"], "oversized")

    def test_oversized_local_total_is_listed_and_not_moved(self) -> None:
        local_dir = self.worktree / ".local"
        local_dir.mkdir()
        (local_dir / "a.bin").write_bytes(b"a" * 700)
        (local_dir / "b.bin").write_bytes(b"b" * 700)
        limit_args = ("--max-local-total-mb", "0.001")

        _, report = self.audit(*limit_args)
        entry = report["entries"][0]
        preservation = entry["preservation"]

        self.assertEqual(preservation["status"], "oversized")
        self.assertTrue(
            any("total size" in reason for reason in preservation["oversized_reasons"])
        )
        listed = {item["path"] for item in preservation["oversized_files"]}
        self.assertEqual(listed, {"a.bin", "b.bin"})
        token = self.approval_for(report, self.worktree)
        result = self.run_helper(
            "--apply", "--approve", token, "--json", *limit_args
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["removed"], 1)
        self.assertFalse(self.worktree.exists())
        applied_entry = next(
            item for item in applied["entries"] if item["action"] == "removed"
        )
        archive_dir = Path(applied_entry["backup_path"])
        self.assertFalse((archive_dir / ".local").exists())
        self.assertTrue((archive_dir / "metadata.json").exists())

    def test_preservation_limit_change_invalidates_approval(self) -> None:
        local_dir = self.worktree / ".local"
        local_dir.mkdir()
        (local_dir / "trace.bin").write_bytes(b"x" * 4096)
        _, report = self.audit()
        token = self.approval_for(report, self.worktree)

        result = self.run_helper(
            "--apply",
            "--approve",
            token,
            "--json",
            "--max-local-file-mb",
            "0.001",
        )

        self.assertEqual(result.returncode, 1)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["rejected_approvals"], [token[:12]])
        self.assertEqual(applied["removed"], 0)
        self.assertTrue(self.worktree.exists())
        self.assertFalse((self.repo / ".local" / "worktree-archive").exists())

    def test_worktree_removal_does_not_probe_git_wrapper(self) -> None:
        _, report = self.audit()
        token = self.approval_for(report, self.worktree)
        git_wrapper = self.fake_bin / "git"
        git_wrapper.write_text(
            """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if args[:1] == ["--wrapper-help"] or (
    args and args[0].startswith("--user-approved=")
):
    print("unexpected wrapper compatibility call", file=sys.stderr)
    sys.exit(99)
os.execv("/usr/bin/git", ["git", *args])
""",
            encoding="utf-8",
        )
        git_wrapper.chmod(0o755)

        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["removed"], 1)
        self.assertFalse(self.worktree.exists())

    def test_failed_removal_does_not_block_later_approved_target(self) -> None:
        second = self.add_worktree("task-456")
        _, report = self.audit()
        approvals = [
            self.approval_for(report, self.worktree),
            self.approval_for(report, second),
        ]
        git_wrapper = self.fake_bin / "git"
        git_wrapper.write_text(
            """#!/usr/bin/env python3
import os
import sys

args = sys.argv[1:]
if args[:2] == ["worktree", "remove"] and args[2].endswith("task-123"):
    print("simulated removal failure", file=sys.stderr)
    sys.exit(1)
os.execv("/usr/bin/git", ["git", *args])
""",
            encoding="utf-8",
        )
        git_wrapper.chmod(0o755)

        result = self.run_helper(
            "--apply",
            "--approve",
            approvals[0],
            "--approve",
            approvals[1],
            "--json",
        )

        self.assertEqual(result.returncode, 1)
        applied = json.loads(result.stdout)
        actions = {entry["path"]: entry["action"] for entry in applied["entries"]}
        self.assertEqual(actions[str(self.worktree)], "failed")
        self.assertEqual(actions[str(second)], "removed")
        self.assertTrue(self.worktree.exists())
        self.assertFalse(second.exists())

    def test_approval_report_survives_default_branch_advancing(self) -> None:
        self.fake_no_pr = True
        self.remote_heads["task-123"] = "f" * 40
        old = time.time() - 25 * 3600
        os.utime(self.worktree / ".git", (old, old))
        report_path = self.root / "approval-report.json"
        audit = self.run_helper(
            "--dry-run",
            "--json",
            "--write-approval-report",
            str(report_path),
        )
        self.assertEqual(audit.returncode, 0, audit.stderr)
        self.default_head = "e" * 40

        result = self.run_helper(
            "--apply",
            "--approve-report",
            str(report_path),
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["removed"], 1)
        self.assertEqual(applied["rejected_approvals"], [])
        self.assertFalse(self.worktree.exists())

    def test_approval_report_does_not_include_new_candidate(self) -> None:
        report_path = self.root / "approval-report.json"
        audit = self.run_helper(
            "--dry-run",
            "--json",
            "--write-approval-report",
            str(report_path),
        )
        self.assertEqual(audit.returncode, 0, audit.stderr)
        second = self.add_worktree("task-456")

        result = self.run_helper(
            "--apply",
            "--approve-report",
            str(report_path),
            "--json",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)
        actions = {entry["path"]: entry["action"] for entry in applied["entries"]}
        self.assertEqual(actions[str(self.worktree)], "removed")
        self.assertEqual(actions[str(second)], "not-approved")
        self.assertFalse(self.worktree.exists())
        self.assertTrue(second.exists())


    def age_worktree(self, path: Path, hours: float = 200.0) -> None:
        old = time.time() - hours * 3600
        os.utime(path / ".git", (old, old))

    def write_evidence(
        self,
        *,
        path: Path | None = None,
        branch: str = "task-123",
        head: str | None = None,
        source_commit: str | None = None,
        state: str = "merged",
        entries: list[dict[str, object]] | None = None,
        version: int = 1,
        name: str = "evidence.json",
    ) -> Path:
        worktree = path or self.worktree
        exact = head or self.remote_heads.get(branch, self.default_head)
        record = {
            "path": str(worktree),
            "branch": branch,
            "head": exact,
            "source_commit": source_commit or exact,
            "state": state,
            "mr_iid": 47,
            "mr_url": "https://git.example.invalid/lighthouse/example/-/merge_requests/47",
            "target_branch": "master",
        }
        payload = {
            "version": version,
            "host": "git.example.invalid",
            "repository": "lighthouse/example",
            "default_branch": "master",
            "entries": entries if entries is not None else [record],
        }
        evidence = self.root / name
        evidence.write_text(json.dumps(payload), encoding="utf-8")
        return evidence

    def run_evidence_helper(
        self, evidence: Path, *args: str
    ) -> subprocess.CompletedProcess[str]:
        return self.run_helper(
            "--remote-evidence",
            str(evidence),
            "--min-no-pr-age-hours",
            "168",
            *args,
        )

    def audit_evidence(
        self, evidence: Path
    ) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        result = self.run_evidence_helper(evidence, "--dry-run", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        return result, json.loads(result.stdout)

    def test_external_evidence_exact_merged_head_is_eligible_without_gh(self) -> None:
        (self.fake_bin / "gh").rename(self.fake_bin / "gh.disabled")
        self.age_worktree(self.worktree)
        evidence = self.write_evidence()

        result = self.run_evidence_helper(evidence, "--dry-run", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["repository"]["host"], "git.example.invalid")
        self.assertEqual(report["repository"]["remote_evidence"], str(evidence))
        entry = report["entries"][0]
        self.assertEqual(entry["action"], "would-remove")
        self.assertIsNone(entry["skip_reason"])
        self.assertEqual(entry["remote_proof"]["kind"], "external-merge-request")
        self.assertEqual(entry["pr"]["number"], 47)
        self.assertEqual(entry["pr"]["state"], "MERGED")

    def test_external_evidence_young_worktree_is_kept(self) -> None:
        evidence = self.write_evidence()

        _, report = self.audit_evidence(evidence)

        entry = report["entries"][0]
        self.assertEqual(entry["action"], "skip")
        self.assertIn("younger than 168 hours", entry["skip_reason"])

    def test_external_evidence_non_exact_source_commit_is_kept(self) -> None:
        self.age_worktree(self.worktree)
        evidence = self.write_evidence(
            head=self.remote_heads["task-123"], source_commit="f" * 40
        )

        _, report = self.audit_evidence(evidence)

        entry = report["entries"][0]
        self.assertEqual(entry["action"], "skip")
        self.assertIn("not the exact head", entry["skip_reason"])

    def test_external_evidence_branch_mismatch_is_kept(self) -> None:
        self.age_worktree(self.worktree)
        evidence = self.write_evidence(branch="someone-else")

        _, report = self.audit_evidence(evidence)

        entry = report["entries"][0]
        self.assertEqual(entry["action"], "skip")
        self.assertIn("branch does not match", entry["skip_reason"])

    def test_external_evidence_open_state_is_kept(self) -> None:
        self.age_worktree(self.worktree)
        evidence = self.write_evidence(state="opened")

        _, report = self.audit_evidence(evidence)

        entry = report["entries"][0]
        self.assertEqual(entry["action"], "skip")
        self.assertIn("merge-request state opened", entry["skip_reason"])

    def test_external_evidence_missing_record_is_kept(self) -> None:
        self.age_worktree(self.worktree)
        evidence = self.write_evidence(entries=[])

        _, report = self.audit_evidence(evidence)

        entry = report["entries"][0]
        self.assertEqual(entry["action"], "skip")
        self.assertIn("no external remote evidence", entry["skip_reason"])

    def test_external_evidence_without_full_object_name_is_refused(self) -> None:
        evidence = self.write_evidence(head="f" * 12, source_commit="f" * 12)

        result = self.run_evidence_helper(evidence, "--dry-run", "--json")

        self.assertEqual(result.returncode, 1)
        self.assertIn("40-character", result.stdout)

    def test_external_evidence_requires_the_168h_floor(self) -> None:
        evidence = self.write_evidence()

        result = self.run_helper(
            "--remote-evidence", str(evidence), "--min-no-pr-age-hours", "24", "--json"
        )

        self.assertEqual(result.returncode, 2)
        self.assertIn("168", result.stderr)

    def test_sweep_only_dry_run_previews_without_deleting(self) -> None:
        self.run_git("checkout", "-q", "-b", "merged-orphan")
        (self.repo / "orphan.txt").write_text("landed\n", encoding="utf-8")
        self.run_git("add", "orphan.txt")
        self.run_git("commit", "-q", "-m", "orphan work")
        self.run_git("checkout", "-q", "main")
        self.run_git("merge", "--ff-only", "merged-orphan")

        result = self.run_helper("--sweep-only", "--dry-run", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertTrue(summary["sweep_only"])
        self.assertEqual(summary["branches_removed"], 0)
        self.assertEqual(
            [item["branch"] for item in summary["branch_sweep"]["branches_would_remove"]],
            ["merged-orphan"],
        )
        branches = self.run_git("branch", "--format=%(refname:short)").stdout.splitlines()
        self.assertIn("merged-orphan", branches)

    def test_sweep_only_apply_removes_orphans_without_approval_source(self) -> None:
        self.run_git("checkout", "-q", "-b", "merged-orphan")
        (self.repo / "orphan.txt").write_text("landed\n", encoding="utf-8")
        self.run_git("add", "orphan.txt")
        self.run_git("commit", "-q", "-m", "orphan work")
        self.run_git("checkout", "-q", "main")
        self.run_git("merge", "--ff-only", "merged-orphan")

        result = self.run_helper("--sweep-only", "--apply", "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["branches_removed"], 1)
        self.assertEqual(summary["removed"], 0)  # 无 worktree 移除
        self.assertNotIn(
            "merged-orphan",
            self.run_git("branch", "--format=%(refname:short)").stdout.splitlines(),
        )

    def test_sweep_only_rejects_worktree_audit_flags(self) -> None:
        result = self.run_helper("--sweep-only", "--apply", "--approve", "x" * 64, "--json")

        self.assertEqual(result.returncode, 2)
        self.assertIn("needs no approvals", result.stderr)

    def test_external_evidence_apply_removes_clean_target_and_keeps_branch(self) -> None:
        self.age_worktree(self.worktree)
        evidence = self.write_evidence()
        report_path = self.root / "evidence-approval.json"
        audit = self.run_evidence_helper(
            evidence, "--dry-run", "--json", "--write-approval-report", str(report_path)
        )
        self.assertEqual(audit.returncode, 0, audit.stderr)

        result = self.run_evidence_helper(
            evidence, "--apply", "--approve-report", str(report_path), "--json"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["removed"], 1)
        self.assertEqual(applied["rejected_approvals"], [])
        self.assertFalse(self.worktree.exists())
        branches = self.run_git("branch", "--format=%(refname:short)").stdout.splitlines()
        self.assertNotIn("task-123", branches)

    def test_keep_branches_flag_retains_every_branch(self) -> None:
        local_file = self.worktree / ".local" / "notes.txt"
        local_file.parent.mkdir()
        local_file.write_text("restore me\n", encoding="utf-8")
        _, report = self.audit()
        token = self.approval_for(report, self.worktree)

        result = self.run_helper(
            "--apply", "--approve", token, "--keep-branches", "--json"
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["removed"], 1)
        entry = next(e for e in applied["entries"] if e["action"] == "removed")
        self.assertFalse(entry["branch_deletion"]["deleted"])
        self.assertEqual(entry["branch_deletion"]["reason"], "--keep-branches")
        branches = self.run_git("branch", "--format=%(refname:short)").stdout.splitlines()
        self.assertIn("task-123", branches)

    def test_orphan_sweep_removes_merged_branch_and_keeps_unmerged(self) -> None:
        self.run_git("checkout", "-q", "-b", "merged-orphan")
        (self.repo / "orphan.txt").write_text("landed\n", encoding="utf-8")
        self.run_git("add", "orphan.txt")
        self.run_git("commit", "-m", "orphan work")
        self.run_git("checkout", "-q", "main")
        self.run_git("merge", "--ff-only", "merged-orphan")
        self.run_git("branch", "merged-live", "main")
        self.run_git("checkout", "-q", "merged-live")
        self.run_git("commit", "--allow-empty", "-q", "-m", "unmerged-only-on-branch")
        self.run_git("checkout", "-q", "main")

        _, report = self.audit()
        token = self.approval_for(report, self.worktree)
        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)
        sweep = applied["branch_sweep"]
        removed = {item["branch"] for item in sweep["branches_removed"]}
        kept = {item["branch"]: item["reason"] for item in sweep["branches_kept"]}
        self.assertIn("merged-orphan", removed)
        self.assertIn("merged-live", kept)
        self.assertIn("no object-level durability proof", kept["merged-live"])
        branches = self.run_git("branch", "--format=%(refname:short)").stdout.splitlines()
        self.assertNotIn("merged-orphan", branches)
        self.assertIn("merged-live", branches)
        self.assertNotIn("task-123", branches)  # 随 worktree 回收按策略删除；清扫本身只碰无认领分支

    def test_orphan_sweep_records_fetch_failure_but_still_sweeps_local_lineage(self) -> None:
        self.run_git("checkout", "-q", "-b", "merged-orphan")
        (self.repo / "orphan.txt").write_text("landed\n", encoding="utf-8")
        self.run_git("add", "orphan.txt")
        self.run_git("commit", "-q", "-m", "orphan work")
        self.run_git("checkout", "-q", "main")
        self.run_git("merge", "--ff-only", "merged-orphan")

        _, report = self.audit()
        token = self.approval_for(report, self.worktree)
        result = self.run_helper("--apply", "--approve", token, "--json")

        self.assertEqual(result.returncode, 0, result.stderr)
        applied = json.loads(result.stdout)
        sweep = applied["branch_sweep"]
        self.assertFalse(sweep["fetch_ok"])  # 测试仓无 origin
        self.assertIn("merged-orphan", [i["branch"] for i in sweep["branches_removed"]])

    def test_external_evidence_apply_rejects_a_worktree_changed_after_audit(self) -> None:
        self.age_worktree(self.worktree)
        evidence = self.write_evidence()
        report_path = self.root / "evidence-approval.json"
        audit = self.run_evidence_helper(
            evidence, "--dry-run", "--json", "--write-approval-report", str(report_path)
        )
        self.assertEqual(audit.returncode, 0, audit.stderr)
        (self.worktree / "appeared-later.txt").write_text("drift\n", encoding="utf-8")

        result = self.run_evidence_helper(
            evidence, "--apply", "--approve-report", str(report_path), "--json"
        )

        self.assertEqual(result.returncode, 1)
        applied = json.loads(result.stdout)
        self.assertEqual(applied["removed"], 0)
        self.assertTrue(applied["rejected_approvals"])
        self.assertTrue(self.worktree.exists())


if __name__ == "__main__":
    unittest.main()
