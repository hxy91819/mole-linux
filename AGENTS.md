# Mole Agent Guide

Linux DevCloud 存储维护先读 [设计与教训](docs/DEVCLOUD_STORAGE_DESIGN.md)，查现行任务、命令、部署和复现读 [自动化手册](docs/DEVCLOUD_AUTOMATION.md)。
分类或清理具体目标前读唯一规则源 [storage-rules](.claude/skills/devcloud-patrol/references/storage-rules.md)，全机巡检走 [devcloud-patrol](.claude/skills/devcloud-patrol/SKILL.md)。
调查结束记账或晋升长期规则时读 [本机账本与晋升条件](docs/DEVCLOUD_STORAGE_DESIGN.md#linux-devcloud-audit-ledger)。

This guide and the references it routes to form the shared project contract for all agents. Put machine-specific or personal overrides in gitignored `AGENTS.local.md` or `CLAUDE.local.md`.

## Project

Mole is a macOS system cleanup and optimization tool with shell and Go components. It performs file cleanup, app protection checks, and maintenance tasks, so safety rules matter more than speed.

## Product Direction

Mole is a terminal-first macOS maintenance toolkit. Its core job is to help power users inspect reclaimable space, remove known-safe leftovers, uninstall apps safely, run bounded maintenance, and check health from a CLI, script, or compact TUI. It is not a general Mac control center, package manager, background monitor, or GUI feature mirror.

### What Mole Should Do

- Make cleanup and uninstall actions boring, reviewable, logged, protected by path/app rules, and dry-run capable.
- Keep `clean`, `uninstall`, `purge`, and `installer` focused on reclaimable files, app leftovers, rebuildable caches, installer artifacts, and exact known cleanup targets.
- Keep `analyze` as a disk explorer and ad hoc cleanup surface. Optimize first paint, navigation, sorting, filtering, and safe deletion before adding dashboard-style features.
- Keep `status` as a compact read-only health dashboard plus stable JSON/NDJSON automation output. It may surface actionable signals, but should not become an iStat clone, alerting daemon, or configurable metrics workbench.
- Keep `optimize` focused on explicit, bounded maintenance tasks that can be explained before execution and tested without real authorization prompts.
- Keep command UX dense and terminal-native: short labels, stable alignment, predictable shortcuts, one-screen summaries, then optional drill-down.
- Keep Mole Mac references as a cross-link or support path. The CLI and Mac app can share product values without requiring feature parity.

### What Mole Should Not Do

- Do not add broad system modification, privacy reset, package management, app bundle patching, or device-management features just because they are technically possible.
- Do not remove or rewrite third-party app bundle contents, signed resources, user documents, credentials, sessions, active databases, or active developer-tool state.
- Do not add background agents, persistent monitoring, notifications, schedulers, menu bar behavior, or GUI-like state unless explicitly requested and justified as CLI scope.
- Do not broaden leftover matching from exact app or bundle evidence into vendor-wide, TeamID-prefix, generic-name, or fallback wildcard deletion.
- Do not turn `status` into a noisy dashboard. Extra rows, live alerts, and tuning controls need a common user action, not just an available metric.
- Do not add prompts, preferences, or output modes to solve every edge case. Prefer quieter defaults, preview/read-only guidance, or declining unsupported operations. A new flag, environment variable, or config key is the same weight as a new setting: it passes only when no single default is right for everyone, and the fix-by-default alternative has to be stated and rejected first. Reaching for a knob to close an issue is the default failure here, not an edge case.
- Do not treat Mole Mac features as required CLI gaps. The CLI should stay narrower, scriptable, and safety-first when parity would add complexity or ambiguity.

### Product Decision Filter

Before accepting a new feature, answer these questions in the PR, issue, or review notes when the fit is not obvious:

1. Does it clearly belong to clean, uninstall, analyze, optimize, status, purge, history, installer, update, completion, touchid, or remove?
2. Is it safe by default, previewable where destructive, testable without real auth, and explainable in one terminal screen?
3. Can the user verify what will change before Mole changes it?
4. Is the target data locally rebuildable, disposable, or backed by exact app/bundle evidence?
5. Would this be better as Mole Mac UI, documentation, a warning, or an explicit "not supported" answer?

If the answer is no or unclear, decline the feature, narrow it, or park it until the product value beats the added surface area.

## Repository Map

- Keep tracked `CLAUDE.md` as a symlink to `AGENTS.md` so Claude and Codex receive the same project contract.
- `.claude/skills/` is the canonical home for project skills. `.agents/skills/` contains relative symlinks for Codex discovery; do not maintain copied skill bodies.
- `.claude/agents/` contains focused Claude review profiles. They read this guide and its current references for safety and portability rules.
- `mole` - the CLI entrypoint. It is a **router only**: it parses args, renders the menu, and dispatches. Business logic does not belong here. Self-update lives in `lib/manage/update.sh` and self-removal in `lib/manage/remove.sh`; both are `source`d (not `exec`d) because the interactive menu and the update banner call them in-process. `VERSION=` stays in `mole` because `install.sh` reads it out of this file with `sed`.
- `lib/core/` - shared shell safety, UI, file operations, operation logs, app protection logic, and centralized timeout constants (`timeouts.sh`).
- `lib/core/app_protection_data.sh` - readonly bundle ID and pattern arrays consumed by `app_protection.sh`. Data only, no logic.
- `cmd/analyze/` - Go disk-analysis TUI. `main.go` is bootstrap only; `model.go` holds types and accessor methods; `update.go` holds the Bubble Tea Update chain.
- `tests/fuzz_corpus/` holds property-test corpora consumed by `path_validation_fuzz.bats`.
- `scripts/` - check, test, build, and release helpers. `audit_bundle_drift.sh` backs the monthly bundle audit; `audit_function_duplication.py` gates same-body-different-name shell functions and runs inside `check.sh` (`--list` shows every group); per-PR perf is covered by `tests/core_performance.bats`.
- [Security design](docs/SECURITY_DESIGN.md) covers the path validation, app protection, and raw deletion annotation contracts.
- [Security audit](SECURITY_AUDIT.md) records security review notes.

## Commands

```bash
./scripts/check.sh --format
MOLE_TEST_NO_AUTH=1 ./scripts/test.sh
MOLE_TEST_NO_AUTH=1 bats tests/clean_core.bats
MOLE_DRY_RUN=1 ./mole clean
MOLE_TEST_NO_AUTH=1 ./mole clean --dry-run
MOLE_TEST_NO_AUTH=1 ./mole purge --dry-run
MOLE_TEST_NO_AUTH=1 ./mole installer --dry-run
find bin lib -name '*.sh' -print0 | xargs -0 -n1 bash -n
make build
go test ./...
```

Public docs and examples should prefer the installed `mo` command. Use `./mole` in this repository when verifying source-tree behavior before installation. `analyze` and `analyse` are both accepted command spellings.

## Critical Safety Rules

- Route removals through `mole_delete` in `lib/core/file_ops.sh` to preserve Trash routing, operation logs, dry-run behavior, and path protection.
- Before writing raw removal code, read the [`# SAFE:` annotation and scratch-file exceptions](docs/SECURITY_DESIGN.md#layer-2--safe-reason-contract-for-raw-rm).
- Never modify protected paths such as `/System`, `/Library/Apple`, or `com.apple.*`.
- Route user-facing cleanup through Trash where recoverability is expected, especially for analyze-driven ad hoc cleanup.
- Route user-facing cleanup through Trash where the project expects recoverability, especially for analyze-driven ad hoc cleanup.
- Never let verification block on sudo, AppleScript, or macOS authorization prompts unless the task explicitly targets auth behavior.
- Use `MOLE_DRY_RUN=1` before destructive cleanup flows.
- Use `MOLE_TEST_NO_AUTH=1` for tests, manual repro, and verification unless real auth behavior is being tested.
- Any new direct use of `sudo`, `osascript`, or `launchctl` must have a `MOLE_TEST_MODE` / `MOLE_TEST_NO_AUTH` guard or be fully mocked in tests.
- Keep Software Update-owned staging trees such as `/Library/Updates` and `/macOS Install Data` read-only.
- Keep the active PowerLog database at `/private/var/db/powerlog/Library/PerfPowerTelemetry/BackgroundProcessing/CurrentBackgroundProcessingDB.BGSQL` and its `-wal` and `-shm` companions read-only.
- Never run a privileged path-based delete or move through an invoking-user-mutable ancestor.
- Do not change ESC timeout behavior in `lib/core/ui.sh` unless explicitly requested.
- Preserve operation logging to the project log path unless the user explicitly asks to change `MO_NO_OPLOG` behavior.

## Read before working on these areas

- Cleanup targets, deletion review, cache recovery, purge scans, uninstall matching and teardown, or privileged system cleanup → [cleanup and uninstall safety](docs/agent-rules/cleanup-safety.md).
- Installer verification, install locks and refusals, or `mo update` bootstrap recovery → [install and self-update](docs/agent-rules/install-update.md).
- Changes to clean, purge, installer, uninstall, optimize, update, shared shell helpers, the paginated menu, analyze, or status → [hotspot ownership and test commands](docs/agent-rules/hotspots.md).
- Selecting verification, writing Bats or source-invariant tests, chasing stale lint paths, or removing duplicate or unused code → [verification and testing pitfalls](docs/agent-rules/testing-pitfalls.md).
- Committing or pushing, handling GitHub issues, pull requests, and discussions, or fixing the bundle drift audit → [GitHub operations](docs/agent-rules/github-ops.md).
- Deletion evidence or guards, uncertain discovery, bounded scans or TTY, Bash 3.2 or system parsing, stale derivations or competing totals, silent progress, test validity, or refusal diagnostics → [bugs incident catalog](.claude/skills/bugs/SKILL.md), using only the matching sections after classifying the touched surfaces.
- Shell code, Bats, install/update, timeout wrappers, TTY handling, plist fixtures, or macOS CI → [shell and test pitfalls](.claude/skills/bugs/references/shell-and-test-pitfalls.md).
- Release assessment or execution → [release flow](.claude/skills/release-flow/SKILL.md), including distribution-channel confirmation before acting.
- Curating release notes → [release notes](.claude/skills/release-notes/SKILL.md).
