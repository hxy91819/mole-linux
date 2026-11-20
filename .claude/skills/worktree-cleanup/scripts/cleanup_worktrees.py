#!/usr/bin/env python3
"""Audit and retire Git worktrees whose commits are durable on a remote host.

Inputs: a Git repository, a minimum age for PR-less worktrees, a reviewed dry-run
report or selected approval tokens, optional `.local` archive and size limits, and
an optional verifier-produced remote evidence file for hosts without a `gh` API
(for example git.woa.com merge requests). Outputs: a bounded text or JSON report
plus per-target archive metadata in apply mode. Before a worktree is removed, a
non-empty `.local` is copied to the primary checkout's
`.local/worktree-archive/<worktree>-<UTC>/` unless the primary checkout does not
gitignore `.local`, in which case the candidate is kept. Oversized `.local`
content is listed but not archived. Exit 0 means every approved candidate
completed, 1 means discovery, approval, archive, or removal had a rejected item,
and 2 is an argparse usage error.

With `--remote-evidence` the tool makes no GitHub call at all: the evidence file
is the only durability witness, and every record it accepts must name the exact
local HEAD of a merged merge request. An external verifier may be looser; the
sink is not.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


PR_NUMBER_RE = re.compile(r"\bpr-(\d+)\b")
PR_JSON_FIELDS = (
    "number,state,title,url,headRefName,headRefOid,mergedAt,closedAt"
)
IGNORED_SAMPLE_LIMIT = 50
LOCAL_INVENTORY_LIST_LIMIT = 500
PRESERVATION_TEXT_LIMIT = 20
DEFAULT_MAX_LOCAL_FILE_MB = 50.0
DEFAULT_MAX_LOCAL_TOTAL_MB = 500.0
# External evidence replaces the GitHub query path, so the caller cannot rely on
# a host-side age gate. 168h is the standing owner rule for worktree retirement;
# a lower --min-no-pr-age-hours is refused in evidence mode instead of silently
# weakening it.
EXTERNAL_EVIDENCE_MIN_AGE_HOURS = 168.0


@dataclass(frozen=True)
class PullRequestInfo:
    number: int
    state: str
    title: str
    url: str
    head_ref_name: str | None
    head_ref_oid: str | None
    merged_at: str | None
    closed_at: str | None

    @property
    def expiration_reason(self) -> str | None:
        state = self.state.upper()
        if self.merged_at or state == "MERGED":
            return "merged"
        if self.closed_at or state == "CLOSED":
            return "closed"
        return None


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path
    head: str
    branch_ref: str | None
    detached: bool
    locked_reason: str | None
    prunable_reason: str | None

    @property
    def branch_name(self) -> str | None:
        if self.branch_ref is None:
            return None
        prefix = "refs/heads/"
        if self.branch_ref.startswith(prefix):
            return self.branch_ref[len(prefix) :]
        return self.branch_ref


@dataclass(frozen=True)
class RepositoryInfo:
    name_with_owner: str
    default_branch: str
    default_head_oid: str


class CommandError(RuntimeError):
    pass


def run_command(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # 网络类操作（fetch/ls-remote）在不可达远端上会无限挂起；超时按失败返回，
        # 调用方走保守路径（fetch 失败仍可用本地谱系清扫）。
        out = exc.stdout
        if isinstance(out, bytes):
            out = out.decode(errors="replace")
        return subprocess.CompletedProcess(
            args,
            returncode=124,
            stdout=out or "",
            stderr=f"timed out after {timeout:g}s",
        )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise CommandError(f"{' '.join(args)} failed: {detail or result.returncode}")
    return result


def run_git(*args: str, cwd: Path | None = None) -> str:
    return run_command(["git", *args], cwd=cwd).stdout.strip()


def parse_worktree_list(output: str) -> list[WorktreeInfo]:
    entries: list[WorktreeInfo] = []
    for block in output.strip().split("\n\n"):
        if not block.strip():
            continue
        path: Path | None = None
        head = ""
        branch_ref: str | None = None
        detached = False
        locked_reason: str | None = None
        prunable_reason: str | None = None
        for line in block.splitlines():
            if line.startswith("worktree "):
                path = Path(line.removeprefix("worktree ").strip())
            elif line.startswith("HEAD "):
                head = line.removeprefix("HEAD ").strip()
            elif line.startswith("branch "):
                branch_ref = line.removeprefix("branch ").strip()
            elif line == "detached":
                detached = True
            elif line == "locked" or line.startswith("locked "):
                locked_reason = line.removeprefix("locked").strip() or "locked"
            elif line == "prunable" or line.startswith("prunable "):
                prunable_reason = line.removeprefix("prunable").strip() or "prunable"
        if path is None:
            raise CommandError(f"Unable to parse worktree block:\n{block}")
        entries.append(
            WorktreeInfo(
                path=path.resolve(),
                head=head,
                branch_ref=branch_ref,
                detached=detached,
                locked_reason=locked_reason,
                prunable_reason=prunable_reason,
            )
        )
    return entries


def infer_pr_number(*values: str | None) -> int | None:
    for value in values:
        if value and (match := PR_NUMBER_RE.search(value)):
            return int(match.group(1))
    return None


def parse_json_result(
    result: subprocess.CompletedProcess[str], *, source: str
) -> tuple[Any | None, str | None]:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
        return None, detail
    try:
        return json.loads(result.stdout), None
    except json.JSONDecodeError as exc:
        return None, f"invalid JSON from {source}: {exc}"


def parse_pr_info(raw: dict[str, Any]) -> PullRequestInfo:
    return PullRequestInfo(
        number=int(raw["number"]),
        state=str(raw["state"]),
        title=str(raw["title"]),
        url=str(raw["url"]),
        head_ref_name=(
            str(raw["headRefName"]) if raw.get("headRefName") is not None else None
        ),
        head_ref_oid=(
            str(raw["headRefOid"]) if raw.get("headRefOid") is not None else None
        ),
        merged_at=str(raw["mergedAt"]) if raw.get("mergedAt") is not None else None,
        closed_at=str(raw["closedAt"]) if raw.get("closedAt") is not None else None,
    )


def query_repository(repo_root: Path) -> RepositoryInfo:
    view = run_command(
        ["gh", "repo", "view", "--json", "nameWithOwner,defaultBranchRef"],
        cwd=repo_root,
        check=False,
    )
    raw, error = parse_json_result(view, source="gh repo view")
    if error is not None:
        raise CommandError(f"GitHub repository lookup failed: {error}")
    try:
        name_with_owner = str(raw["nameWithOwner"])
        default_branch = str(raw["defaultBranchRef"]["name"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CommandError(f"Invalid GitHub repository data: {exc}") from exc

    commit = run_command(
        [
            "gh",
            "api",
            f"repos/{name_with_owner}/commits/{quote(default_branch, safe='')}",
        ],
        cwd=repo_root,
        check=False,
    )
    commit_raw, commit_error = parse_json_result(commit, source="gh api")
    if commit_error is not None:
        raise CommandError(f"Default branch lookup failed: {commit_error}")
    try:
        default_head_oid = str(commit_raw["sha"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CommandError(f"Invalid default branch data: {exc}") from exc
    return RepositoryInfo(name_with_owner, default_branch, default_head_oid)


def load_remote_evidence(path: str) -> dict[str, Any]:
    """Load a verifier-produced durability record for a non-GitHub host.

    The schema is deliberately narrow: version 1, one host, one repository, and
    one record per worktree path carrying the merge-request state and the exact
    full object name that the merge request's source branch pointed at. Anything
    unreadable, duplicated, or short of a full 40-character object name is
    refused rather than treated as unknown, so a malformed file cannot turn into
    a deletion approval.
    """
    evidence_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"Cannot read remote evidence {evidence_path}: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise CommandError(f"Invalid remote evidence {evidence_path}: expected version 1")
    host = raw.get("host")
    repository = raw.get("repository")
    entries = raw.get("entries")
    if not isinstance(host, str) or not host.strip():
        raise CommandError(f"Invalid remote evidence {evidence_path}: missing host")
    if not isinstance(repository, str) or not repository.strip():
        raise CommandError(f"Invalid remote evidence {evidence_path}: missing repository")
    if not isinstance(entries, list):
        raise CommandError(f"Invalid remote evidence {evidence_path}: missing entries")

    by_path: dict[str, dict[str, Any]] = {}
    for record in entries:
        if not isinstance(record, dict):
            raise CommandError(
                f"Invalid remote evidence {evidence_path}: every entry must be an object"
            )
        try:
            record_path = str(Path(str(record["path"])).expanduser().resolve())
            branch = str(record["branch"])
            head = str(record["head"])
            source_commit = str(record["source_commit"])
            state = str(record["state"]).lower()
            mr_iid = int(record["mr_iid"])
            mr_url = str(record["mr_url"])
        except (KeyError, TypeError, ValueError) as exc:
            raise CommandError(f"Invalid remote evidence {evidence_path}: {exc}") from exc
        if len(head) != 40 or len(source_commit) != 40:
            raise CommandError(
                f"Invalid remote evidence {evidence_path}: head and source_commit "
                "must be full 40-character object names"
            )
        if record_path in by_path:
            raise CommandError(
                f"Invalid remote evidence {evidence_path}: duplicate entry for {record_path}"
            )
        by_path[record_path] = {
            "path": record_path,
            "branch": branch,
            "head": head,
            "source_commit": source_commit,
            "state": state,
            "mr_iid": mr_iid,
            "mr_url": mr_url,
            "target_branch": str(record.get("target_branch") or ""),
            "title": str(record.get("title") or ""),
        }
    return {
        "path": str(evidence_path),
        "host": host.strip(),
        "repository": repository.strip(),
        "default_branch": str(raw.get("default_branch") or ""),
        "by_path": by_path,
    }


def external_repository_info(evidence: dict[str, Any]) -> RepositoryInfo:
    return RepositoryInfo(
        name_with_owner=str(evidence["repository"]),
        default_branch=str(evidence["default_branch"] or "-"),
        default_head_oid="",
    )


def evaluate_external_evidence(
    *,
    worktree: WorktreeInfo,
    age_hours: float | None,
    evidence: dict[str, Any],
    min_age_hours: float,
) -> tuple[str | None, dict[str, Any] | None, dict[str, Any] | None]:
    """Return (skip_reason, pr, remote_proof) for one evidence-mode candidate."""
    record = evidence["by_path"].get(str(worktree.path))
    if record is None:
        return "no external remote evidence for this worktree", None, None
    if record["branch"] != (worktree.branch_name or ""):
        return "external remote evidence branch does not match the worktree branch", None, None
    if record["head"] != worktree.head:
        return "external remote evidence head does not match the worktree HEAD", None, None
    if record["state"] != "merged":
        return f"external remote evidence records merge-request state {record['state']}", None, None
    if record["source_commit"] != worktree.head:
        return "worktree HEAD is not the exact head of the external merge request", None, None
    if age_hours is None:
        return "worktree age is unavailable", None, None
    if age_hours < min_age_hours:
        return (
            f"external-evidence candidate is younger than {min_age_hours:g} hours",
            None,
            None,
        )
    pr = {
        "number": record["mr_iid"],
        "state": "MERGED",
        "title": record["title"],
        "url": record["mr_url"],
        "head_ref_name": record["branch"],
        "head_ref_oid": record["head"],
        "merged_at": None,
        "closed_at": None,
    }
    remote_proof = {
        "kind": "external-merge-request",
        "host": evidence["host"],
        "remote_head": record["head"],
        "url": record["mr_url"],
        "description": (
            f"HEAD equals the exact head of merged {evidence['host']} merge request "
            f"!{record['mr_iid']} ({record['mr_url']})"
        ),
    }
    return None, pr, remote_proof


def query_pr_by_number(
    number: int, *, repo_root: Path
) -> tuple[PullRequestInfo | None, str | None]:
    result = run_command(
        ["gh", "pr", "view", str(number), "--json", PR_JSON_FIELDS],
        cwd=repo_root,
        check=False,
    )
    raw, error = parse_json_result(result, source="gh pr view")
    if error is not None:
        return None, error
    try:
        return parse_pr_info(raw), None
    except (KeyError, TypeError, ValueError) as exc:
        return None, f"invalid PR data from gh: {exc}"


def query_pr_by_branch(
    branch_name: str, *, repo_root: Path
) -> tuple[PullRequestInfo | None, str | None]:
    result = run_command(
        [
            "gh",
            "pr",
            "list",
            "--state",
            "all",
            "--head",
            branch_name,
            "--json",
            PR_JSON_FIELDS,
            "--limit",
            "20",
        ],
        cwd=repo_root,
        check=False,
    )
    rows, error = parse_json_result(result, source="gh pr list")
    if error is not None:
        return None, error
    if not isinstance(rows, list):
        return None, "invalid PR list data from gh"
    if not rows:
        return None, None

    exact = [row for row in rows if row.get("headRefName") == branch_name]
    matches = exact or rows
    open_matches = [row for row in matches if str(row.get("state", "")).upper() == "OPEN"]
    if open_matches:
        matches = open_matches
    if len(matches) > 1:
        numbers = ", ".join(str(row.get("number")) for row in matches)
        return None, f"ambiguous branch match for {branch_name}: {numbers}"
    try:
        return parse_pr_info(matches[0]), None
    except (KeyError, TypeError, ValueError) as exc:
        return None, f"invalid PR data from gh: {exc}"


def remote_contains_commit(
    *,
    repo_root: Path,
    repository: RepositoryInfo,
    local_head: str,
    remote_head: str,
) -> tuple[bool, str | None]:
    if local_head == remote_head:
        return True, None
    endpoint = (
        f"repos/{repository.name_with_owner}/compare/"
        f"{quote(local_head, safe='')}...{quote(remote_head, safe='')}"
    )
    result = run_command(["gh", "api", endpoint], cwd=repo_root, check=False)
    raw, error = parse_json_result(result, source="gh api compare")
    if error is not None:
        return False, error
    try:
        merge_base = str(raw["merge_base_commit"]["sha"])
    except (KeyError, TypeError, ValueError) as exc:
        return False, f"invalid compare data from gh: {exc}"
    return merge_base == local_head, None


def remote_branches_at_head(
    *, repo_root: Path, repository: RepositoryInfo, local_head: str
) -> tuple[list[str], str | None]:
    endpoint = (
        f"repos/{repository.name_with_owner}/commits/"
        f"{quote(local_head, safe='')}/branches-where-head"
    )
    result = run_command(["gh", "api", endpoint], cwd=repo_root, check=False)
    rows, error = parse_json_result(result, source="gh api branches-where-head")
    if error is not None:
        return [], error
    if not isinstance(rows, list):
        return [], "invalid branches-where-head data from gh"
    try:
        return sorted(str(row["name"]) for row in rows), None
    except (KeyError, TypeError, ValueError) as exc:
        return [], f"invalid branches-where-head data from gh: {exc}"


def local_branches(repo_root: Path) -> list[str]:
    result = run_command(
        ["git", "-C", str(repo_root), "branch", "--format=%(refname:short)"],
        check=False,
    )
    if result.returncode != 0:
        raise CommandError(
            result.stderr.strip() or "git branch --format failed"
        )
    return [name for name in result.stdout.splitlines() if name.strip()]


def worktree_claimed_branches(repo_root: Path) -> set[str]:
    claimed: set[str] = set()
    for worktree in parse_worktree_list(
        run_git("-C", str(repo_root), "worktree", "list", "--porcelain")
    ):
        if worktree.branch_name:
            claimed.add(worktree.branch_name)
    return claimed


def remote_branch_tips(repo_root: Path) -> dict[str, str]:
    """Map remote branch name -> tip object name; empty when origin is unreachable."""
    result = run_command(
        ["git", "-C", str(repo_root), "ls-remote", "--heads", "origin"],
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        return {}
    tips: dict[str, str] = {}
    for line in result.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        ref = ref.strip()
        if sha and ref.startswith("refs/heads/"):
            tips[ref[len("refs/heads/") :]] = sha.strip()
    return tips


def resolve_default_branch_ref(repo_root: Path, default_branch: str) -> str | None:
    """Best local ref that represents the remote default branch lineage."""
    candidates = []
    if default_branch and not default_branch.startswith("-"):
        candidates = [
            f"refs/remotes/origin/{default_branch}",
            f"refs/heads/{default_branch}",
        ]
    for ref in candidates:
        result = run_command(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet", ref],
            check=False,
        )
        if result.returncode == 0:
            return ref
    return None


def branch_delete_proof(
    repo_root: Path,
    branch: str,
    *,
    default_branch: str,
    claimed: set[str],
    tips: dict[str, str],
    default_ref: str | None,
) -> tuple[str | None, str | None]:
    """Return (proof_kind, proof_detail) when the local branch is safe to delete.

    Zero-loss bar: the branch tip must be reachable from the remote default
    lineage (origin/<default> preferred, local <default> as fallback) or be the
    exact tip of a branch that still exists on the remote. A squash-merged
    branch with no surviving remote branch has no object-level proof here and
    is kept; its work is landed but the commits are only durably referenced by
    the host, which this sink does not re-litigate without its own evidence.
    """
    if branch == default_branch:
        return None, "is the default branch"
    if branch in claimed:
        return None, "claimed by a worktree"
    if branch.startswith("-"):
        return None, "unusual branch name"
    if default_ref is not None:
        result = run_command(
            ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", branch, default_ref],
            check=False,
        )
        if result.returncode == 0:
            return "merged-into-default", f"{branch} is contained in {default_ref}"
    branch_tip = run_command(
        ["git", "-C", str(repo_root), "rev-parse", branch], check=False
    ).stdout.strip()
    if branch_tip and tips.get(branch) == branch_tip:
        return "remote-branch-tip", "tip equals the current tip of the remote branch"
    return None, "no object-level durability proof (not merged into the default lineage, not a remote tip)"


def delete_local_branch(
    repo_root: Path, branch: str, *, expect_old_oid: str | None = None
) -> tuple[bool, str | None]:
    """Delete refs/heads/<branch> with compare-and-delete semantics.

    `git update-ref -d <ref> <old>` only deletes when the ref still points at
    the audited object name, closing the proof-then-delete race: a branch that
    moved after the audit is kept and reported instead of force-deleted.
    """
    ref = f"refs/heads/{branch}"
    args = ["git", "-C", str(repo_root), "update-ref", "-d", ref]
    if expect_old_oid:
        args.append(expect_old_oid)
    result = run_command(args, check=False)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"git exited {result.returncode}"
        return False, detail
    return True, None


def retire_worktree_branch(
    summary: dict[str, Any], candidate: dict[str, Any], keep_branches: bool
) -> dict[str, Any]:
    """Delete the removed worktree's local branch under the standing branch policy.

    Owner policy (2026-09-19): branches of removed worktrees are deleted by
    default. The remote durability proof that authorized the worktree removal
    is also the deletion warrant for its branch — including squash merges,
    where the branch tip is not an ancestor of the default branch but the work
    is landed. Failures are recorded and never retroactively fail the removal.
    """
    outcome: dict[str, Any] = {"deleted": False}
    branch = candidate.get("branch")
    if keep_branches:
        outcome["reason"] = "--keep-branches"
        return outcome
    if not branch:
        outcome["reason"] = "detached HEAD; no local branch"
        return outcome
    if branch == (summary["repository"].get("default_branch") or ""):
        outcome["reason"] = "is the default branch"
        return outcome
    proof = candidate.get("remote_proof") or {}
    if not proof:
        outcome["reason"] = "no remote durability proof recorded"
        return outcome
    repo_root = Path(summary["common_repo_root"])
    outcome["proof"] = f"worktree-removal-proof:{proof.get('kind')}"
    outcome["old_oid"] = candidate.get("head")
    deleted, error = delete_local_branch(
        repo_root, branch, expect_old_oid=candidate.get("head")
    )
    outcome["deleted"] = deleted
    if error:
        outcome["error"] = error
    return outcome


def sweep_orphan_branches(
    summary: dict[str, Any], keep_branches: bool, execute: bool = True
) -> None:
    """Delete unclaimed local branches whose work is provably durable remotely.

    Runs only in apply mode. Two zero-loss proofs are accepted: the branch tip
    is contained in the default branch lineage, or the branch tip is the exact
    current tip of a branch that still exists on the remote. Everything else is
    kept and reported with the reason. Failures never affect the worktree
    removal outcome; they only land in this section's counters.
    """
    repo_root = Path(summary["common_repo_root"])
    default_branch = str(summary["repository"].get("default_branch") or "")
    sweep: dict[str, Any] = {
        "ran": False,
        "fetch_ok": None,
        "branches_removed": [],
        "branches_kept": [],
        "skipped_reason": None,
    }
    summary["branch_sweep"] = sweep
    if keep_branches:
        sweep["skipped_reason"] = "--keep-branches"
        return
    claimed = worktree_claimed_branches(repo_root)
    fetch = run_command(
        ["git", "-C", str(repo_root), "fetch", "--prune", "origin"],
        check=False,
        timeout=90,
    )
    sweep["fetch_ok"] = fetch.returncode == 0
    tips = remote_branch_tips(repo_root) if sweep["fetch_ok"] else {}
    default_ref = resolve_default_branch_ref(repo_root, default_branch)
    sweep["default_ref"] = default_ref
    if default_ref is None and not tips:
        sweep["skipped_reason"] = "no default-branch ref and no remote tips available"
        return
    for branch in local_branches(repo_root):
        if branch == default_branch or branch in claimed:
            continue
        proof, keep_reason = branch_delete_proof(
            repo_root,
            branch,
            default_branch=default_branch,
            claimed=claimed,
            tips=tips,
            default_ref=default_ref,
        )
        if proof is None:
            sweep["branches_kept"].append({"branch": branch, "reason": keep_reason})
            continue
        branch_tip = run_command(
            ["git", "-C", str(repo_root), "rev-parse", branch], check=False
        ).stdout.strip()
        if not execute:
            sweep.setdefault("branches_would_remove", []).append(
                {"branch": branch, "proof": proof, "old_oid": branch_tip}
            )
            continue
        deleted, error = delete_local_branch(
            repo_root, branch, expect_old_oid=branch_tip or None
        )
        if deleted:
            sweep["branches_removed"].append(
                {"branch": branch, "proof": proof, "old_oid": branch_tip}
            )
        else:
            sweep["branches_kept"].append(
                {"branch": branch, "reason": f"git update-ref -d failed: {error}"}
            )


def detect_default_branch(repo_root: Path) -> str:
    """Host-agnostic default branch detection for the sweep-only entry."""
    result = run_command(
        ["git", "-C", str(repo_root), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip().removeprefix("origin/")
    for candidate in ("main", "master"):
        probe = run_command(
            ["git", "-C", str(repo_root), "rev-parse", "--verify", "--quiet",
             f"refs/heads/{candidate}"],
            check=False,
        )
        if probe.returncode == 0:
            return candidate
    return ""


def origin_identity(repo_root: Path) -> tuple[str, str]:
    """Best-effort (name_with_owner, host) from the origin URL."""
    result = run_command(
        ["git", "-C", str(repo_root), "remote", "get-url", "origin"], check=False
    )
    url = result.stdout.strip() if result.returncode == 0 else ""
    slug = re.search(r"(?:@|//)[^/:]+[:/](.+?)(?:\.git)?/?$", url)
    host = re.search(r"(?:@|//)([^/:]+)[:/]", url)
    return (
        slug.group(1) if slug else "",
        host.group(1) if host else "unknown",
    )


def sweep_only_summary(args: argparse.Namespace) -> dict[str, Any]:
    """Minimal summary for the branch-governance entry: no worktree audit.

    Exists because orphan-branch governance must reach every repository, not
    just those that still have removable worktrees: the last worktree's
    disappearance must not retire the repository from branch hygiene.
    """
    selected_repo = Path(args.repo).expanduser().resolve()
    selected_root = Path(
        run_git("-C", str(selected_repo), "rev-parse", "--show-toplevel")
    ).resolve()
    worktrees = parse_worktree_list(
        run_git("-C", str(selected_root), "worktree", "list", "--porcelain")
    )
    primary_repo_root = worktrees[0].path if worktrees else selected_root
    name_with_owner, host = origin_identity(selected_root)
    return {
        "mode": "apply" if args.apply else "dry-run",
        "sweep_only": True,
        "common_repo_root": str(primary_repo_root),
        "selected_repo_root": str(selected_root),
        "repository": {
            "name_with_owner": name_with_owner,
            "default_branch": detect_default_branch(selected_root),
            "default_head_oid": "",
            "host": host or "unknown",
            "remote_evidence": None,
        },
        "archive_root": "",
        "backup_root": "",
        "local_limits": {
            "max_file_bytes": int(
                getattr(args, "max_local_file_mb", DEFAULT_MAX_LOCAL_FILE_MB)
                * 1024
                * 1024
            ),
            "max_total_bytes": int(
                getattr(args, "max_local_total_mb", DEFAULT_MAX_LOCAL_TOTAL_MB)
                * 1024
                * 1024
            ),
        },
        "min_no_pr_age_hours": args.min_no_pr_age_hours,
        "worktree_count_before": len(worktrees),
        "worktree_count_after": len(worktrees),
        "scanned": 0,
        "eligible": 0,
        "approved": 0,
        "removed": 0,
        "failed": 0,
        "rejected_approvals": [],
        "entries": [],
    }


def path_is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def read_worktree_dirty(path: Path) -> tuple[bool | None, str | None]:
    result = run_command(
        ["git", "-C", str(path), "status", "--porcelain", "--untracked-files=all"],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git status failed"
        return None, detail
    return bool(result.stdout.strip()), None


def empty_local_inventory() -> dict[str, Any]:
    return {
        "exists": False,
        "is_symlink": False,
        "entry_count": 0,
        "file_count": 0,
        "total_bytes": 0,
        "files": [],
        "largest_files": [],
        "files_truncated": False,
        "max_file": None,
    }


def read_local_state(
    local_dir: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return the approval fingerprint and the preservation inventory for `.local`.

    The fingerprint covers every entry (mode, size, mtime, symlink target) so a
    change between audit and apply invalidates the approval. The inventory counts
    descendants only, never follows symlinks, and records regular files plus
    symlinks so the dry-run can name what a removal would preserve.
    """
    try:
        root_metadata = local_dir.lstat()
    except FileNotFoundError:
        encoded = b"[]"
        return {
            "fingerprint": {
                "entry_count": 0,
                "metadata_digest": hashlib.sha256(encoded).hexdigest(),
            },
            "inventory": empty_local_inventory(),
        }, None
    except OSError as exc:
        return None, f"cannot inspect .local: {exc}"

    if stat.S_ISLNK(root_metadata.st_mode):
        try:
            target = os.readlink(local_dir)
        except OSError as exc:
            return None, f"cannot read .local symlink {local_dir}: {exc}"
        entry = {
            "path": ".",
            "mode": root_metadata.st_mode,
            "size": root_metadata.st_size,
            "mtime_ns": root_metadata.st_mtime_ns,
            "symlink_target": target,
        }
        encoded = json.dumps([entry], sort_keys=True, separators=(",", ":")).encode()
        file_entry = {
            "path": ".",
            "bytes": root_metadata.st_size,
            "kind": "symlink",
            "target": target,
        }
        return {
            "fingerprint": {
                "entry_count": 1,
                "metadata_digest": hashlib.sha256(encoded).hexdigest(),
            },
            "inventory": {
                "exists": True,
                "is_symlink": True,
                "entry_count": 1,
                "file_count": 1,
                "total_bytes": root_metadata.st_size,
                "files": [file_entry],
                "largest_files": [file_entry],
                "files_truncated": False,
                "max_file": file_entry,
            },
        }, None

    if not stat.S_ISDIR(root_metadata.st_mode):
        return None, f".local is not a directory: {local_dir}"

    pending = [local_dir]
    entries: list[dict[str, Any]] = []
    files: list[dict[str, Any]] = []
    largest_files: list[dict[str, Any]] = []
    entry_count = 0
    file_count = 0
    total_bytes = 0
    max_file: dict[str, Any] | None = None
    files_truncated = False

    while pending:
        current = pending.pop()
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            return None, f"path disappeared while inventorying .local: {current}"
        except OSError as exc:
            return None, f"cannot inventory .local path {current}: {exc}"

        relative = "." if current == local_dir else str(current.relative_to(local_dir))
        item = {
            "path": relative,
            "mode": metadata.st_mode,
            "size": metadata.st_size,
            "mtime_ns": metadata.st_mtime_ns,
            "symlink_target": None,
        }
        kind: str | None = None
        if stat.S_ISLNK(metadata.st_mode):
            try:
                item["symlink_target"] = os.readlink(current)
            except OSError as exc:
                return None, f"cannot read .local symlink {current}: {exc}"
            kind = "symlink"
        elif stat.S_ISDIR(metadata.st_mode):
            try:
                children = sorted(
                    (Path(entry.path) for entry in os.scandir(current)),
                    key=lambda child: child.name,
                    reverse=True,
                )
            except OSError as exc:
                return None, f"cannot inventory .local directory {current}: {exc}"
            pending.extend(children)
            if current != local_dir:
                entry_count += 1
        elif stat.S_ISREG(metadata.st_mode):
            kind = "file"
            entry_count += 1
        else:
            kind = "special"
            entry_count += 1

        if kind in ("file", "symlink"):
            file_count += 1
            total_bytes += metadata.st_size
            file_entry = {
                "path": relative,
                "bytes": metadata.st_size,
                "kind": kind,
            }
            if kind == "symlink":
                file_entry["target"] = item["symlink_target"]
            if max_file is None or file_entry["bytes"] > max_file["bytes"]:
                max_file = file_entry
            largest_files.append(file_entry)
            largest_files.sort(key=lambda item: item["bytes"], reverse=True)
            del largest_files[PRESERVATION_TEXT_LIMIT:]
            if len(files) < LOCAL_INVENTORY_LIST_LIMIT:
                files.append(file_entry)
            else:
                files_truncated = True
        entries.append(item)

    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {
        "fingerprint": {
            "entry_count": len(entries),
            "metadata_digest": hashlib.sha256(encoded).hexdigest(),
        },
        "inventory": {
            "exists": True,
            "is_symlink": False,
            "entry_count": entry_count,
            "file_count": file_count,
            "total_bytes": total_bytes,
            "files": sorted(files, key=lambda file: file["path"]),
            "largest_files": largest_files,
            "files_truncated": files_truncated,
            "max_file": max_file,
        },
    }, None


def read_ignored_roots(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    result = run_command(
        [
            "git",
            "-C",
            str(path),
            "ls-files",
            "--others",
            "--ignored",
            "--exclude-standard",
            "--directory",
            "-z",
        ],
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "git ls-files failed"
        return None, detail
    local_state, local_state_error = read_local_state(path / ".local")
    if local_state_error is not None:
        return None, local_state_error
    roots = sorted(value for value in result.stdout.split("\0") if value)
    encoded = "\0".join(roots).encode("utf-8", errors="surrogateescape")
    discarded = [
        value
        for value in roots
        if value != ".local" and not value.startswith(".local/")
    ]
    return (
        {
            "count": len(roots),
            "count_kind": "ignored path roots, not files or bytes",
            "digest": hashlib.sha256(encoded).hexdigest(),
            "sample": roots[:IGNORED_SAMPLE_LIMIT],
            "sample_truncated": len(roots) > IGNORED_SAMPLE_LIMIT,
            "discarded_count": len(discarded),
            "discarded_sample": discarded[:IGNORED_SAMPLE_LIMIT],
            "discarded_sample_truncated": len(discarded) > IGNORED_SAMPLE_LIMIT,
            "local_tree": local_state["fingerprint"],
            "local_inventory": local_state["inventory"],
        },
        None,
    )


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def ensure_unique_archive_dir(
    archive_root: Path, worktree_name: str, stamp: str | None = None
) -> Path:
    stem = f"{worktree_name}-{stamp or utc_timestamp()}"
    candidate = archive_root / stem
    if not candidate.exists():
        return candidate
    for index in range(2, 1000):
        suffixed = archive_root / f"{stem}-{index}"
        if not suffixed.exists():
            return suffixed
    raise CommandError(f"Unable to allocate archive directory for {worktree_name}")


def check_path_ignored(repo_root: Path, path: Path) -> tuple[bool | None, str | None]:
    """Return whether `path` is gitignored in `repo_root`; None means external."""
    try:
        relative = path.relative_to(repo_root)
    except ValueError:
        return None, None
    if relative == Path("."):
        return False, None
    result = run_command(
        ["git", "-C", str(repo_root), "check-ignore", "-q", "--", str(relative)],
        check=False,
    )
    if result.returncode == 0:
        return True, None
    if result.returncode == 1:
        return False, None
    detail = (
        result.stderr.strip()
        or result.stdout.strip()
        or f"git check-ignore exited {result.returncode}"
    )
    return None, detail


def copy_local_evidence(source: Path, destination: Path) -> None:
    """Copy `.local` without following symlinks; the caller removes it with the worktree."""
    if source.is_symlink():
        os.symlink(os.readlink(source), destination)
    elif source.is_dir():
        shutil.copytree(source, destination, symlinks=True)
    else:
        shutil.copy2(source, destination)


def plan_local_preservation(
    *,
    worktree_name: str,
    local_dir: Path,
    inventory: dict[str, Any],
    archive_root: Path,
    archive_root_inside_primary: bool,
    archive_root_ignored: bool | None,
    archive_root_ignore_error: str | None,
    max_file_bytes: int,
    max_total_bytes: int,
) -> dict[str, Any]:
    destination = str(ensure_unique_archive_dir(archive_root, worktree_name))
    preservation: dict[str, Any] = {
        "status": "none",
        "source": str(local_dir),
        "destination": destination,
        "archive_root": str(archive_root),
        "ignored_by_git": archive_root_ignored,
        "entry_count": inventory["entry_count"],
        "file_count": inventory["file_count"],
        "total_bytes": inventory["total_bytes"],
        "files": inventory["files"],
        "files_truncated": inventory["files_truncated"],
        "max_file": inventory["max_file"],
        "limits": {
            "max_file_bytes": max_file_bytes,
            "max_total_bytes": max_total_bytes,
        },
        "oversized_files": [],
        "oversized_reasons": [],
        "warning": None,
        "moved_to": None,
    }

    if not inventory["exists"] or inventory["entry_count"] == 0:
        preservation["destination"] = None
        return preservation

    if inventory["is_symlink"]:
        preservation["status"] = "blocked"
        preservation["warning"] = (
            ".local is a symlink; resolve it to a real directory inside the "
            "worktree before cleanup so the archived evidence is self-contained"
        )
        return preservation

    if archive_root_inside_primary and archive_root_ignored is not True:
        preservation["status"] = "blocked"
        if archive_root_ignore_error is not None:
            preservation["warning"] = (
                "cannot prove the primary checkout archive path is gitignored: "
                f"{archive_root_ignore_error}"
            )
        else:
            preservation["warning"] = (
                f"primary checkout archive path {archive_root} is not gitignored; "
                ".local evidence cannot be archived, so the worktree is kept"
            )
        return preservation

    oversized_files: list[dict[str, Any]] = []
    oversized_reasons: list[str] = []
    max_file = inventory["max_file"]
    if (
        max_file_bytes > 0
        and max_file is not None
        and max_file["bytes"] > max_file_bytes
    ):
        oversized_reasons.append(
            f"largest file {max_file['path']} is {max_file['bytes']} bytes "
            f"(limit {max_file_bytes})"
        )
        oversized_files = [
            item for item in inventory["files"] if item["bytes"] > max_file_bytes
        ]
        if all(item["path"] != max_file["path"] for item in oversized_files):
            oversized_files.append(max_file)
    if max_total_bytes > 0 and inventory["total_bytes"] > max_total_bytes:
        oversized_reasons.append(
            f"total size is {inventory['total_bytes']} bytes "
            f"(limit {max_total_bytes})"
        )
        largest = inventory.get("largest_files") or inventory["files"]
        for item in largest:
            if all(existing["path"] != item["path"] for existing in oversized_files):
                oversized_files.append(item)
        if max_file is not None and all(
            item["path"] != max_file["path"] for item in oversized_files
        ):
            oversized_files.append(max_file)

    if oversized_reasons:
        preservation["status"] = "oversized"
        preservation["oversized_files"] = sorted(
            oversized_files, key=lambda item: item["bytes"], reverse=True
        )
        preservation["oversized_reasons"] = oversized_reasons
        preservation["warning"] = (
            ".local exceeds the preservation size limits; only its manifest is "
            "recorded and the listed files are discarded when the worktree is removed"
        )
    else:
        preservation["status"] = "archive"
    return preservation


def worktree_age_hours(path: Path) -> float | None:
    marker = path / ".git"
    try:
        modified = marker.stat().st_mtime
    except OSError:
        return None
    now = datetime.now(timezone.utc).timestamp()
    return max(0.0, (now - modified) / 3600)


def to_repo_relative(path: Path, repo_root: Path) -> str:
    try:
        return str(path.relative_to(repo_root))
    except ValueError:
        return str(path)


def local_fingerprint(
    worktree: WorktreeInfo, dirty: bool | None, ignored: dict[str, Any] | None
) -> str:
    value = {
        "path": str(worktree.path),
        "head": worktree.head,
        "branch": worktree.branch_name,
        "detached": worktree.detached,
        "locked": worktree.locked_reason,
        "prunable": worktree.prunable_reason,
        "dirty": dirty,
        "ignored_count": ignored["count"] if ignored else None,
        "ignored_digest": ignored["digest"] if ignored else None,
        "local_tree": ignored["local_tree"] if ignored else None,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def approval_token(entry: dict[str, Any], repo_root: Path) -> str:
    value = {
        "repo": str(repo_root),
        "path": entry["path"],
        "head": entry["head"],
        "branch": entry["branch"],
        "local_fingerprint": entry["local_fingerprint"],
        # The dry-run report tells the operator whether `.local` will be archived,
        # listed as oversized, or blocked. Bind that decision to the approval so a
        # different limit or ignore state cannot silently change the sink.
        "preservation_status": entry["preservation"]["status"],
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(path)


def parse_nonnegative_float(value: str) -> float:
    try:
        parsed = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a number") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit and retire remotely durable Git worktrees.",
        epilog=(
            "Examples:\n"
            "  cleanup_worktrees.py --repo . --dry-run --json "
            "--write-approval-report /tmp/audit.json\n"
            "  cleanup_worktrees.py --repo . --apply "
            "--approve-report /tmp/audit.json\n"
            "  cleanup_worktrees.py --repo /src/project --apply --approve TOKEN\n\n"
            "Outputs:\n"
            "  Dry-run can save the reviewed candidate set as an approval report. "
            "Apply rechecks remote durability and local state, removes still-matching "
            "approved worktrees, skips changed candidates, and never adds new candidates."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--repo",
        default=".",
        help="Repository checkout to inspect (default: current directory)",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Remove worktrees approved by a report or one or more tokens",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Report worktree status and approval tokens without deleting (default)",
    )
    parser.add_argument(
        "--sweep-only",
        action="store_true",
        help=(
            "Branch governance only: run (apply) or preview (dry-run) the "
            "merged-orphan-branch sweep; no worktree removals, no approvals"
        ),
    )
    approval = parser.add_mutually_exclusive_group()
    approval.add_argument(
        "--approve",
        action="append",
        default=[],
        metavar="TOKEN",
        help="Approve one dry-run candidate; repeat for multiple worktrees",
    )
    approval.add_argument(
        "--approve-report",
        metavar="PATH",
        help="Approve every candidate recorded in one reviewed dry-run JSON report",
    )
    parser.add_argument(
        "--write-approval-report",
        metavar="PATH",
        help="Write the complete dry-run JSON to PATH for later --approve-report",
    )
    parser.add_argument(
        "--min-no-pr-age-hours",
        type=parse_nonnegative_float,
        default=24.0,
        help=(
            "Minimum worktree age before a PR-less HEAD already in the remote default "
            "branch is eligible (default: 24)"
        ),
    )
    parser.add_argument(
        "--remote-evidence",
        metavar="PATH",
        help=(
            "Verifier-produced JSON durability record for a non-GitHub host. "
            "With this flag no GitHub API is called and only worktrees named by "
            "an exact merged-merge-request head are eligible. Requires "
            f"--min-no-pr-age-hours >= {EXTERNAL_EVIDENCE_MIN_AGE_HOURS:g}."
        ),
    )
    parser.add_argument(
        "--keep-branches",
        action="store_true",
        help=(
            "Opt out of all branch deletion: keep the local branch of every "
            "removed worktree and skip the merged orphan-branch sweep"
        ),
    )
    parser.add_argument(
        "--backup-root",
        help=(
            "Archive root for preserved .local evidence; relative paths resolve "
            "from --repo (default: <primary checkout>/.local/worktree-archive)"
        ),
    )
    parser.add_argument(
        "--max-local-file-mb",
        type=parse_nonnegative_float,
        default=DEFAULT_MAX_LOCAL_FILE_MB,
        metavar="MB",
        help=(
            "Do not move .local when any file exceeds this size in MiB "
            f"(default: {DEFAULT_MAX_LOCAL_FILE_MB:g}; 0 disables the limit)"
        ),
    )
    parser.add_argument(
        "--max-local-total-mb",
        type=parse_nonnegative_float,
        default=DEFAULT_MAX_LOCAL_TOTAL_MB,
        metavar="MB",
        help=(
            "Do not move .local when its total size exceeds this size in MiB "
            f"(default: {DEFAULT_MAX_LOCAL_TOTAL_MB:g}; 0 disables the limit)"
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON instead of a text summary",
    )
    args = parser.parse_args()
    if args.sweep_only:
        if args.approve or args.approve_report or args.write_approval_report:
            parser.error(
                "--sweep-only needs no approvals; worktree audit flags do not apply"
            )
    elif args.apply and not (args.approve or args.approve_report):
        parser.error("--apply requires --approve-report or at least one --approve token")
    if not args.apply and (args.approve or args.approve_report):
        parser.error("--approve and --approve-report are valid only with --apply")
    if args.apply and args.write_approval_report:
        parser.error("--write-approval-report is valid only with dry-run")
    if args.remote_evidence and args.min_no_pr_age_hours < EXTERNAL_EVIDENCE_MIN_AGE_HOURS:
        parser.error(
            "--remote-evidence requires --min-no-pr-age-hours >= "
            f"{EXTERNAL_EVIDENCE_MIN_AGE_HOURS:g}; external evidence mode has no "
            "host-side age gate"
        )
    if not args.apply:
        args.dry_run = True
    return args


def discover(args: argparse.Namespace) -> dict[str, Any]:
    selected_repo = Path(args.repo).expanduser().resolve()
    selected_root = Path(
        run_git("-C", str(selected_repo), "rev-parse", "--show-toplevel")
    ).resolve()
    common_git_dir = Path(
        run_git(
            "-C",
            str(selected_repo),
            "rev-parse",
            "--path-format=absolute",
            "--git-common-dir",
        )
    ).resolve()
    worktrees = parse_worktree_list(
        run_git("-C", str(selected_root), "worktree", "list", "--porcelain")
    )
    if not worktrees:
        raise CommandError("No Git worktrees found")

    primary_repo_root = worktrees[0].path
    managed_worktrees_root = primary_repo_root / ".worktrees"
    evidence_path = getattr(args, "remote_evidence", None)
    evidence = load_remote_evidence(evidence_path) if evidence_path else None
    repository = (
        external_repository_info(evidence)
        if evidence is not None
        else query_repository(selected_root)
    )
    # The requested archive location is the primary checkout (the parent of the
    # common Git directory), not the checkout selected by --repo: a linked
    # worktree may be removed, while the primary checkout and its ignored .local
    # survive every candidate.
    primary_checkout_root = common_git_dir.parent
    if args.backup_root:
        backup_root = Path(args.backup_root).expanduser()
        if not backup_root.is_absolute():
            backup_root = selected_root / backup_root
        backup_root = backup_root.resolve()
    else:
        backup_root = (primary_checkout_root / ".local" / "worktree-archive").resolve()
    archive_root_inside_primary = path_is_relative_to(
        backup_root, primary_checkout_root
    )
    if archive_root_inside_primary:
        archive_root_ignored, archive_ignore_error = check_path_ignored(
            primary_checkout_root, backup_root
        )
    else:
        archive_root_ignored, archive_ignore_error = None, None
    max_file_bytes = int(
        getattr(args, "max_local_file_mb", DEFAULT_MAX_LOCAL_FILE_MB) * 1024 * 1024
    )
    max_total_bytes = int(
        getattr(args, "max_local_total_mb", DEFAULT_MAX_LOCAL_TOTAL_MB)
        * 1024
        * 1024
    )

    entries: list[dict[str, Any]] = []
    for worktree in worktrees:
        if worktree.path == primary_repo_root:
            continue

        dirty, dirty_error = read_worktree_dirty(worktree.path)
        ignored, ignored_error = read_ignored_roots(worktree.path)
        if ignored is not None:
            local_inventory = ignored.pop("local_inventory")
            preservation = plan_local_preservation(
                worktree_name=worktree.path.name,
                local_dir=worktree.path / ".local",
                inventory=local_inventory,
                archive_root=backup_root,
                archive_root_inside_primary=archive_root_inside_primary,
                archive_root_ignored=archive_root_ignored,
                archive_root_ignore_error=archive_ignore_error,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
            )
            if preservation["status"] == "oversized":
                ignored["discarded_count"] += 1
                sample = [".local", *ignored["discarded_sample"]]
                if len(sample) > IGNORED_SAMPLE_LIMIT:
                    sample = sample[:IGNORED_SAMPLE_LIMIT]
                    ignored["discarded_sample_truncated"] = True
                ignored["discarded_sample"] = sample
        else:
            preservation = plan_local_preservation(
                worktree_name=worktree.path.name,
                local_dir=worktree.path / ".local",
                inventory=empty_local_inventory(),
                archive_root=backup_root,
                archive_root_inside_primary=archive_root_inside_primary,
                archive_root_ignored=archive_root_ignored,
                archive_root_ignore_error=archive_ignore_error,
                max_file_bytes=max_file_bytes,
                max_total_bytes=max_total_bytes,
            )
            preservation["status"] = "unknown"
            preservation["destination"] = None
            preservation["warning"] = (
                f"local evidence inventory failed: {ignored_error}"
            )
        age_hours = worktree_age_hours(worktree.path)
        entry: dict[str, Any] = {
            "name": worktree.path.name,
            "path": str(worktree.path),
            "path_relative": to_repo_relative(worktree.path, primary_repo_root),
            "managed": path_is_relative_to(worktree.path, managed_worktrees_root),
            "branch": worktree.branch_name,
            "detached": worktree.detached,
            "head": worktree.head,
            "locked_reason": worktree.locked_reason,
            "prunable_reason": worktree.prunable_reason,
            "age_hours": round(age_hours, 2) if age_hours is not None else None,
            "dirty": dirty,
            "ignored": ignored,
            "preservation": preservation,
            "local_fingerprint": local_fingerprint(worktree, dirty, ignored),
            "pr": None,
            "expiration_reason": None,
            "remote_proof": None,
            "skip_reason": None,
            "approval_token": None,
            "action": "would-remove",
            "backup_path": None,
            "error": None,
        }

        skip_reason: str | None = None
        if worktree.path == selected_root:
            skip_reason = "selected --repo checkout cannot be removed"
        elif worktree.locked_reason is not None:
            skip_reason = f"worktree is locked: {worktree.locked_reason}"
        elif worktree.prunable_reason is not None:
            skip_reason = f"worktree metadata is prunable: {worktree.prunable_reason}"
        elif dirty_error is not None:
            skip_reason = f"git status failed: {dirty_error}"
        elif ignored_error is not None:
            skip_reason = f"ignored-file inventory failed: {ignored_error}"
        elif dirty:
            skip_reason = "worktree has staged, unstaged, or untracked changes"
        elif preservation["status"] == "blocked":
            skip_reason = preservation["warning"]
        elif preservation["status"] == "unknown":
            skip_reason = (
                preservation["warning"] or "local evidence inventory failed"
            )

        pr_info: PullRequestInfo | None = None
        if skip_reason is None and evidence is not None:
            skip_reason, entry["pr"], entry["remote_proof"] = evaluate_external_evidence(
                worktree=worktree,
                age_hours=age_hours,
                evidence=evidence,
                min_age_hours=args.min_no_pr_age_hours,
            )
            if skip_reason is None:
                entry["expiration_reason"] = "external-merged-merge-request"
        elif skip_reason is None:
            branch_name = worktree.branch_name
            pr_number = infer_pr_number(worktree.path.name, branch_name)
            if pr_number is not None:
                pr_info, pr_lookup_error = query_pr_by_number(
                    pr_number, repo_root=selected_root
                )
            elif branch_name:
                pr_info, pr_lookup_error = query_pr_by_branch(
                    branch_name, repo_root=selected_root
                )
            else:
                pr_lookup_error = None

            if pr_lookup_error is not None:
                skip_reason = f"PR lookup failed: {pr_lookup_error}"
            elif pr_info is not None:
                entry["pr"] = {
                    "number": pr_info.number,
                    "state": pr_info.state,
                    "title": pr_info.title,
                    "url": pr_info.url,
                    "head_ref_name": pr_info.head_ref_name,
                    "head_ref_oid": pr_info.head_ref_oid,
                    "merged_at": pr_info.merged_at,
                    "closed_at": pr_info.closed_at,
                }
                expiration_reason = pr_info.expiration_reason
                if expiration_reason is None:
                    skip_reason = "PR is still open"
                elif pr_info.head_ref_oid is None:
                    skip_reason = "PR has no remote head OID"
                else:
                    durable, durability_error = remote_contains_commit(
                        repo_root=selected_root,
                        repository=repository,
                        local_head=worktree.head,
                        remote_head=pr_info.head_ref_oid,
                    )
                    if durability_error is not None:
                        skip_reason = (
                            f"remote durability check failed: {durability_error}"
                        )
                    elif not durable:
                        skip_reason = "worktree HEAD is not contained in the PR remote head"
                    else:
                        entry["expiration_reason"] = expiration_reason
                        entry["remote_proof"] = {
                            "kind": "pull-request-head",
                            "remote_head": pr_info.head_ref_oid,
                            "description": (
                                f"HEAD is durable in {pr_info.url} ({expiration_reason})"
                            ),
                        }
            elif age_hours is None:
                skip_reason = "worktree age is unavailable"
            elif age_hours < args.min_no_pr_age_hours:
                skip_reason = (
                    "no associated PR and worktree is younger than "
                    f"{args.min_no_pr_age_hours:g} hours"
                )
            else:
                remote_branches, branch_error = remote_branches_at_head(
                    repo_root=selected_root,
                    repository=repository,
                    local_head=worktree.head,
                )
                if branch_error is not None:
                    skip_reason = f"remote branch lookup failed: {branch_error}"
                elif remote_branches:
                    entry["expiration_reason"] = "pushed-remote-branch"
                    entry["remote_proof"] = {
                        "kind": "remote-branch-head",
                        "remote_head": worktree.head,
                        "branches": remote_branches,
                        "description": (
                            "HEAD is the current tip of remote branch(es): "
                            + ", ".join(remote_branches)
                        ),
                    }
                else:
                    durable, durability_error = remote_contains_commit(
                        repo_root=selected_root,
                        repository=repository,
                        local_head=worktree.head,
                        remote_head=repository.default_head_oid,
                    )
                    if durability_error is not None:
                        skip_reason = (
                            f"remote durability check failed: {durability_error}"
                        )
                    elif not durable:
                        skip_reason = (
                            "no completed PR, remote branch tip, or containment in "
                            f"the remote default branch {repository.default_branch}"
                        )
                    else:
                        entry["expiration_reason"] = "landed-on-default"
                        entry["remote_proof"] = {
                            "kind": "default-branch",
                            "remote_head": repository.default_head_oid,
                            "description": (
                                f"HEAD is durable in remote default branch "
                                f"{repository.default_branch}"
                            ),
                        }

        entry["skip_reason"] = skip_reason
        if skip_reason is not None:
            entry["action"] = "skip"
        else:
            entry["approval_token"] = approval_token(entry, primary_repo_root)
        entries.append(entry)

    return {
        "mode": "apply" if args.apply else "dry-run",
        "common_repo_root": str(primary_repo_root),
        "selected_repo_root": str(selected_root),
        "repository": {
            "name_with_owner": repository.name_with_owner,
            "default_branch": repository.default_branch,
            "default_head_oid": repository.default_head_oid,
            "host": evidence["host"] if evidence is not None else "github.com",
            "remote_evidence": evidence["path"] if evidence is not None else None,
        },
        "managed_worktrees_root": str(managed_worktrees_root),
        "primary_checkout_root": str(primary_checkout_root),
        "archive_root": str(backup_root),
        # Keep the old key so existing consumers keep reading the archive base.
        "backup_root": str(backup_root),
        "local_limits": {
            "max_file_bytes": max_file_bytes,
            "max_total_bytes": max_total_bytes,
        },
        "min_no_pr_age_hours": args.min_no_pr_age_hours,
        "worktree_count_before": len(worktrees),
        "worktree_count_after": len(worktrees),
        "scanned": len(entries),
        "eligible": sum(entry["skip_reason"] is None for entry in entries),
        "approved": 0,
        "removed": 0,
        "failed": 0,
        "rejected_approvals": [],
        "entries": entries,
    }


def read_approval_report(path: str, summary: dict[str, Any]) -> list[str]:
    report_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CommandError(f"Cannot read approval report {report_path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        raise CommandError(f"Invalid approval report {report_path}: missing entries")
    if raw.get("common_repo_root") != summary["common_repo_root"]:
        raise CommandError(
            f"Approval report {report_path} belongs to a different repository"
        )
    tokens = [
        entry.get("approval_token")
        for entry in raw["entries"]
        if isinstance(entry, dict) and entry.get("approval_token") is not None
    ]
    if not tokens or not all(isinstance(token, str) for token in tokens):
        raise CommandError(f"Approval report {report_path} has no valid candidates")
    return tokens


def current_worktree(repo_root: Path, path: Path) -> WorktreeInfo | None:
    worktrees = parse_worktree_list(
        run_git("-C", str(repo_root), "worktree", "list", "--porcelain")
    )
    return next((worktree for worktree in worktrees if worktree.path == path), None)


def revalidate_local_state(entry: dict[str, Any], repo_root: Path) -> str | None:
    path = Path(entry["path"])
    worktree = current_worktree(repo_root, path)
    if worktree is None:
        return "worktree registration disappeared after audit"
    dirty, dirty_error = read_worktree_dirty(path)
    ignored, ignored_error = read_ignored_roots(path)
    if dirty_error is not None:
        return f"git status failed during revalidation: {dirty_error}"
    if ignored_error is not None:
        return f"ignored-file inventory failed during revalidation: {ignored_error}"
    current_fingerprint = local_fingerprint(worktree, dirty, ignored)
    if current_fingerprint != entry["local_fingerprint"]:
        return "worktree HEAD, lock, dirty state, or ignored files changed after audit"
    return None


def apply_candidates(
    summary: dict[str, Any], approved_tokens: list[str], keep_branches: bool = False
) -> None:
    eligible_by_token = {
        entry["approval_token"]: entry
        for entry in summary["entries"]
        if entry["approval_token"] is not None
    }
    approvals = set(approved_tokens)
    if len(approvals) != len(approved_tokens):
        raise CommandError("Duplicate approval tokens are not allowed")
    unknown = sorted(approvals - set(eligible_by_token))
    summary["rejected_approvals"] = [token[:12] for token in unknown]
    candidates = [
        eligible_by_token[token]
        for token in approved_tokens
        if token in eligible_by_token
    ]
    for entry in summary["entries"]:
        if entry["approval_token"] is not None:
            entry["action"] = "approved" if entry in candidates else "not-approved"
    summary["approved"] = len(candidates)
    if not candidates:
        sweep_orphan_branches(summary, keep_branches)
        summary["branches_removed"] = len(
            (summary.get("branch_sweep") or {}).get("branches_removed") or []
        )
        summary["branches_kept"] = len(
            (summary.get("branch_sweep") or {}).get("branches_kept") or []
        )
        return

    archive_root = Path(summary["backup_root"]).resolve()
    for candidate in candidates:
        worktree_path = Path(candidate["path"]).resolve()
        if path_is_relative_to(archive_root, worktree_path):
            raise CommandError(
                f"Archive root {archive_root} is inside cleanup target {worktree_path}"
            )

    for candidate in candidates:
        worktree_path = Path(candidate["path"])
        local_dir = worktree_path / ".local"
        preservation = candidate["preservation"]
        status = preservation["status"]
        metadata_path: Path | None = None

        def update_metadata() -> None:
            if metadata_path is None:
                return
            write_json(
                metadata_path,
                {
                    "updated_at": datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                    "source_local_dir": str(local_dir),
                    "archive_root": str(archive_root),
                    "content_preserved": status == "archive",
                    "preservation": preservation,
                    "worktree": candidate,
                },
            )

        def fail_candidate(error: str) -> None:
            candidate["action"] = "failed"
            candidate["error"] = error
            summary["failed"] += 1
            update_metadata()

        state_error = revalidate_local_state(
            candidate, Path(summary["common_repo_root"])
        )
        if state_error is not None:
            fail_candidate(state_error)
            continue

        try:
            if status in ("archive", "oversized"):
                archive_root.mkdir(parents=True, mode=0o700, exist_ok=True)
                archive_dir = ensure_unique_archive_dir(
                    archive_root, candidate["name"]
                )
                archive_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
                archive_dir.chmod(0o700)
                candidate["backup_path"] = str(archive_dir)
                preservation["destination"] = str(archive_dir)
                metadata_path = archive_dir / "metadata.json"
                if status == "archive":
                    copy_local_evidence(local_dir, archive_dir / ".local")
                    preservation["moved_to"] = str(archive_dir / ".local")
                else:
                    preservation["moved_to"] = None
            elif status != "none":
                fail_candidate(
                    f"local evidence preservation is {status}; refusing removal"
                )
                continue
            candidate["action"] = "remove-pending"
        except (OSError, CommandError) as exc:
            fail_candidate(f"local archive failed: {exc}")
            continue

        update_metadata()

        state_error = revalidate_local_state(
            candidate, Path(summary["common_repo_root"])
        )
        if state_error is not None:
            fail_candidate(state_error)
            continue

        remove_args = ["git", "worktree", "remove", str(worktree_path)]
        result = run_command(
            remove_args,
            cwd=Path(summary["common_repo_root"]),
            check=False,
        )
        if result.returncode != 0:
            candidate["action"] = "failed"
            candidate["error"] = (
                result.stderr.strip()
                or result.stdout.strip()
                or f"git exited {result.returncode}"
            )
            summary["failed"] += 1
        else:
            candidate["action"] = "removed"
            summary["removed"] += 1
            candidate["branch_deletion"] = retire_worktree_branch(
                summary, candidate, keep_branches
            )

        update_metadata()

    sweep_orphan_branches(summary, keep_branches)
    summary["branches_removed"] = len(
        (summary.get("branch_sweep") or {}).get("branches_removed") or []
    )
    summary["branches_kept"] = len(
        (summary.get("branch_sweep") or {}).get("branches_kept") or []
    )

    summary["worktree_count_after"] = len(
        parse_worktree_list(
            run_git(
                "-C",
                summary["common_repo_root"],
                "worktree",
                "list",
                "--porcelain",
            )
        )
    )


def print_text(summary: dict[str, Any]) -> None:
    print(f"mode: {summary['mode']}")
    print(f"repo: {summary['common_repo_root']}")
    print(f"github_repo: {summary['repository']['name_with_owner']}")
    print(f"host: {summary['repository'].get('host', 'github.com')}")
    print(f"default_branch: {summary['repository']['default_branch']}")
    print(f"archive_root: {summary['archive_root']}")
    print(f"worktrees_before: {summary['worktree_count_before']}")
    print(f"worktrees_after: {summary['worktree_count_after']}")
    print(f"scanned: {summary['scanned']}")
    print(f"eligible: {summary['eligible']}")
    print(f"approved: {summary['approved']}")
    print(f"removed: {summary['removed']}")
    print(f"failed: {summary['failed']}")
    print(f"rejected_approvals: {len(summary['rejected_approvals'])}")
    sweep = summary.get("branch_sweep")
    if sweep is not None:
        print(f"branches_removed: {len(sweep.get('branches_removed') or [])}")
        for item in sweep.get("branches_removed") or []:
            print(f"  branch-removed: {item['branch']} ({item['proof']})")
        print(f"branches_kept: {len(sweep.get('branches_kept') or [])}")
        for item in sweep.get("branches_kept") or []:
            print(f"  branch-kept: {item['branch']} ({item['reason']})")
        if sweep.get("skipped_reason"):
            print(f"  branch-sweep-skipped: {sweep['skipped_reason']}")
    print()

    for entry in summary["entries"]:
        pr = entry["pr"]
        pr_summary = f"PR #{pr['number']} {pr['state']}" if pr else "no-pr"
        print(f"- {entry['path_relative']}")
        print(f"  scope: {'managed' if entry['managed'] else 'external'}")
        print(f"  pr: {pr_summary}")
        print(f"  head: {entry['head']}")
        print(f"  remote_proof: {entry['remote_proof'] or 'none'}")
        print(f"  dirty: {entry['dirty']}")
        print(f"  locked: {entry['locked_reason'] or 'no'}")
        ignored = entry["ignored"]
        print(
            "  ignored_path_roots_not_files: "
            f"{ignored['count'] if ignored else 'unknown'}"
        )
        if ignored:
            print(f"  ignored_roots_deleted_count: {ignored['discarded_count']}")
            print(f"  ignored_roots_deleted_sample: {ignored['discarded_sample']}")
            print(
                "  ignored_roots_deleted_sample_truncated: "
                f"{'yes' if ignored['discarded_sample_truncated'] else 'no'}"
            )
        else:
            print("  ignored_roots_deleted_count: unknown")
            print("  ignored_roots_deleted_sample: unknown")
            print("  ignored_roots_deleted_sample_truncated: unknown")
        preservation = entry.get("preservation") or {
            "status": "unknown",
            "destination": None,
            "file_count": 0,
            "total_bytes": 0,
            "files": [],
            "oversized_files": [],
            "warning": "preservation state unavailable",
        }
        print(f"  local_preservation: {preservation['status']}")
        if preservation["status"] != "none":
            print(
                "  local_archive_destination: "
                f"{preservation.get('destination') or 'none'}"
            )
        if preservation.get("warning"):
            print(f"  local_warning: {preservation['warning']}")
        if preservation["status"] == "archive":
            print(f"  local_preserved_files: {preservation['file_count']}")
            print(f"  local_preserved_bytes: {preservation['total_bytes']}")
            for item in preservation.get("files", [])[:PRESERVATION_TEXT_LIMIT]:
                print(
                    f"    preserved: {item['path']} "
                    f"({format_bytes(item['bytes'])})"
                )
            remaining = preservation["file_count"] - min(
                len(preservation.get("files", [])), PRESERVATION_TEXT_LIMIT
            )
            if remaining > 0:
                print(f"    preserved: ... {remaining} more")
        elif preservation["status"] == "oversized":
            print("  local_preserved_files: 0")
            print(f"  local_discarded_files: {preservation['file_count']}")
            print(f"  local_discarded_bytes: {preservation['total_bytes']}")
            listed = sorted(
                preservation.get("files", []),
                key=lambda item: item["bytes"],
                reverse=True,
            )
            for item in listed[:PRESERVATION_TEXT_LIMIT]:
                print(
                    f"    not-preserved: {item['path']} "
                    f"({format_bytes(item['bytes'])})"
                )
            remaining = preservation["file_count"] - min(
                len(listed), PRESERVATION_TEXT_LIMIT
            )
            if remaining > 0:
                print(f"    not-preserved: ... {remaining} more")
        elif preservation["status"] == "blocked":
            print("  local_preserved_files: 0")
            for item in preservation.get("files", [])[:PRESERVATION_TEXT_LIMIT]:
                print(
                    f"    would-preserve: {item['path']} "
                    f"({format_bytes(item['bytes'])})"
                )
        else:
            print("  local_preserved_files: 0")
        print(f"  action: {entry['action']}")
        if entry["approval_token"]:
            print(f"  approval_token: {entry['approval_token']}")
        if entry["backup_path"]:
            print(f"  backup_path: {entry['backup_path']}")
        if entry["skip_reason"]:
            print(f"  note: {entry['skip_reason']}")
        if entry["error"]:
            print(f"  error: {entry['error']}")
        print()


def emit_error(args: argparse.Namespace, error: str) -> None:
    if args.json:
        json.dump({"error": error, "mode": "apply" if args.apply else "dry-run"}, sys.stdout)
        sys.stdout.write("\n")
    else:
        print(f"ERROR: {error}", file=sys.stderr)


def main() -> int:
    args = parse_args()
    try:
        if args.sweep_only:
            summary = sweep_only_summary(args)
            sweep_orphan_branches(summary, args.keep_branches, execute=args.apply)
            sweep = summary.get("branch_sweep") or {}
            summary["branches_removed"] = len(sweep.get("branches_removed") or [])
            summary["branches_kept"] = len(sweep.get("branches_kept") or [])
            summary["branches_would_remove"] = len(
                sweep.get("branches_would_remove") or []
            )
        else:
            summary = discover(args)
            if args.apply:
                approved_tokens = (
                    read_approval_report(args.approve_report, summary)
                    if args.approve_report
                    else args.approve
                )
                apply_candidates(summary, approved_tokens, args.keep_branches)
            elif args.write_approval_report:
                write_json(Path(args.write_approval_report).expanduser(), summary)
    except (CommandError, OSError) as exc:
        emit_error(args, str(exc))
        return 1

    if args.json:
        json.dump(summary, sys.stdout, indent=2, sort_keys=True)
        sys.stdout.write("\n")
    else:
        print_text(summary)
    return 1 if summary["failed"] or summary["rejected_approvals"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
