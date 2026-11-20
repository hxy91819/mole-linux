---
name: worktree-cleanup
description: Explicitly audit and retire clean Git worktrees whose HEAD is durably stored on a remote host (GitHub, or a verifier-supplied evidence file for another host), using review-bound approval tokens and .local evidence archives.
disable-model-invocation: true
triggers:
  - user
---

# Worktree Cleanup

Run this workflow only when the user explicitly invokes `$worktree-cleanup`. It performs repository-wide discovery and can remove worktrees. Always audit first, present the exact candidates, and apply only the approval tokens the user selects.

## Requirements

Require Git, Python 3.10+, GitHub CLI, and an authenticated `gh` session with read access to the repository's pull requests. Run from a repository checkout or pass `--repo <path>`.

For a host without a `gh` API (for example `git.example.invalid`), pass
`--remote-evidence <json>` instead: a verifier-produced file supplies the
durability witness and **no GitHub call is made at all**. See "External remote
evidence" below.

## Audit

1. Check the current branch, `git status --short`, and `git worktree list`. Preserve concurrent and unrelated work.
2. Create a new temporary report path outside the repository and run the helper in dry-run mode:

   ```bash
   python3 <skill-dir>/scripts/cleanup_worktrees.py \
     --repo <repo-path> \
     --dry-run \
     --json \
     --write-approval-report <temporary-report.json>
   ```

3. Review every candidate and skip reason. A candidate is eligible only when all of these hold:
   - Git status is known and clean, including untracked files.
   - The worktree is neither locked nor marked prunable.
   - It is not the primary checkout or the checkout selected by `--repo`.
   - Its exact HEAD is contained in the remote head of a merged or closed GitHub pull request; or no pull request exists, the worktree is at least 24 hours old by default, and its HEAD is either the tip of a GitHub remote branch or contained in the remote default branch.
     With `--remote-evidence`, this bullet is replaced by an exact merged
     merge-request head from the evidence file (see "External remote evidence").
4. Review `ignored.discarded_sample` and `ignored.discarded_count` for every candidate. These count ignored path roots, not files or bytes; one listed directory may contain substantial data. A non-empty `.local` is archived to the primary checkout before removal; every other ignored file is deliberately discarded if that candidate is approved. Review `preservation.status`, `preservation.destination`, the listed files, and `preservation.oversized_files`. If the primary checkout does not gitignore its archive path, the candidate is kept with a `blocked` warning instead of being archived.
5. Tell the user the exact paths, whether each path is managed or external, remote durability proof, ignored data impact, the archive root and per-candidate destination, which `.local` files would be preserved or left behind, and all retained paths with their reasons. Ask once whether to remove all listed candidates or a named subset; do not make the user copy or re-approve token strings. Worktrees outside `.worktrees/` follow the same eligibility rules.

Completion criterion: every listed worktree has a resolved action or a conservative skip reason, and no state has changed.

## Apply

After the user approves every candidate in the saved dry-run report, apply it directly:

```bash
python3 <skill-dir>/scripts/cleanup_worktrees.py \
  --repo <repo-path> \
  --apply \
  --approve-report <temporary-report.json>
```

For a subset, pass the selected candidates' `--approve <token>` values internally; users should approve paths, not transcribe tokens. The helper rediscovers all worktrees. Approval remains valid when a remote default branch or other durability witness advances, provided the exact worktree path, branch, HEAD, and local data state are unchanged and a valid remote durability proof still exists. Newly eligible worktrees are never added to an old report.

Changed or no-longer-eligible approvals are rejected individually while other approved candidates continue. Archive, authorization, or removal failure also affects only that candidate. Report every rejected or failed candidate; rerun dry-run only if the user wants to retry those candidates or include newly eligible ones.

For each approved target, the helper revalidates local state, copies a non-empty `.local` into the primary checkout, writes `metadata.json` beside the copy, and only then removes the clean worktree without `--force`. The primary checkout is the parent of `git rev-parse --git-common-dir`, not the checkout selected by `--repo`. Other ignored files are not archived. The default layout is:

```text
<primary-checkout>/.local/worktree-archive/
  task-123-20261117T093000Z/
    .local/...
    metadata.json
```

The UTC stamp is generated at apply time; dry-run shows the destination that would be used. `metadata.json` records the source `.local`, the worktree inventory, and whether content was preserved. Use `--backup-root <path>` when the user requests another durable location; relative paths resolve from `--repo`.

Size limits are configurable with `--max-local-file-mb` (default 50) and `--max-local-total-mb` (default 500); `0` disables a limit. If any file or the total exceeds its limit, nothing from that `.local` is moved. The helper still creates the timestamped directory with a `metadata.json` manifest (`content_preserved: false`) listing the files, and the approved worktree may still be removed. The dry-run report lists those files as `not-preserved`; treat that as deliberate data loss and stop before applying if the evidence matters. Pass the same limit flags to apply as to audit: the preservation status is bound into the approval token.

## External remote evidence

`--remote-evidence <json>` is for non-GitHub hosts. Schema: `version` (must be
1), `host`, `repository` (namespace/project), optional `default_branch`, and
`entries[]`, each with `path`, `branch`, `head`, `source_commit`, `state`,
`mr_iid`, `mr_url`. Hard rules:

- The tool makes no `gh` call in this mode; the file is the only durability witness.
- `head` and `source_commit` must be full 40-character object names.
- A candidate is eligible only when its path, branch, and HEAD match the record,
  `state` is `merged`, `source_commit` equals the local HEAD exactly, and the
  worktree is at least `--min-no-pr-age-hours` old.
- `--min-no-pr-age-hours` must be at least 168 when `--remote-evidence` is used;
  a lower value is refused as a usage error because there is no host-side age gate.
- Worktrees without a matching record are kept with `no external remote evidence`.
- Pass the same file to both the dry-run and the apply invocation.

## Guardrails

- Never remove a dirty worktree. This skill has no force-removal mode.
- Never remove a worktree whose non-empty `.local` was not archived. A failed copy keeps the worktree; only the reviewed oversized case proceeds with a manifest and no content.
- Never archive into a primary-checkout path that Git does not ignore. Keep the candidate and report the path.
- Keep targets whose pull request or remote durability lookup is ambiguous or failed.
- Keep worktrees for open pull requests.
- Keep locked and prunable worktrees for explicit operator handling.
- Never remove the primary checkout or the checkout selected by `--repo`.
- Keep the archive root outside every cleanup target; the helper rejects unsafe paths before writing.
- Continue independent candidates after a stale approval, archive failure, state-validation failure, authorization denial, or removal failure.
- Treat branch deletion as part of this skill (owner policy 2026-09-19): after a successful removal the worktree's local branch is deleted by default, using the same remote durability proof as the removal; `--keep-branches` opts out. In apply mode the helper also sweeps unclaimed local branches whose tip is contained in the default branch lineage or equals the current remote branch tip; everything else is kept with a reason. Branch failures never retroactively fail a removal.
- Remote proof is point-in-time evidence, not a promise that a remote ref will exist forever. Branch deletion removes the last local ref to the worktree's commits; durability on the remote is what makes that acceptable, and `--keep-branches` restores the previous behavior.

## BB child threads bound to a worktree

BB can bind a child thread to a worktree directory. Once that directory is removed, the thread can no longer continue the conversation: resume requests against the missing path fail, and this skill cannot detach or rebind it. This is a BB limitation, not a cleanup failure, and the helper does not attempt to work around it. Preserve the `.local` diagnostics in the primary checkout archive so a fresh thread can resume from the evidence instead of the removed thread's history.

## Output

Use `--json` for machine-readable reporting. On completion, report removed paths, retained paths and reasons, the archive root and each per-candidate archive destination, preserved file counts and bytes, any oversized files that were listed but not moved, failures, and the final worktree count.
