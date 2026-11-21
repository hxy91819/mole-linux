# Verification and testing pitfalls

## Verification

- Prefer targeted Bats tests during development and run the full suite before committing.
- Shell changes: run `./scripts/check.sh --format`, then the relevant Bats test or `MOLE_TEST_NO_AUTH=1 ./scripts/test.sh`.
- Go changes: run `go test ./...`.
- Cleanup behavior: verify with dry-run or test mode first.
- File operation changes: run at least the `file_ops_mole_delete` and `user_file_ops` Bats suites listed in [Hotspot Ownership](hotspots.md).
- Installer, purge, and uninstall changes: use the corresponding commands in [Hotspot Ownership](hotspots.md), including the `uninstall` and `uninstall_remove_file_list` Bats suites for uninstall changes.
- Whitelist or management changes: run `MOLE_TEST_NO_AUTH=1 bats tests/manage_whitelist.bats tests/manage_sudo.bats`.
- Documentation-only changes: check links and commands.

`make check`, `make format`, `make test`, `make test-go`, and `make verify` are wrappers around the scripts above. `make verify` intentionally runs `check` plus Go tests only; use the full Bats suite before risky cleanup, uninstall, or release work.

If `golangci-lint` reports issues from deleted temporary worktrees or non-existent paths, clear its local cache and rerun the linter:

```bash
golangci-lint cache clean
golangci-lint run ./cmd/...
```

## Check exit status and Bats assertions

- Never pipe a test, check, or CI run into `tail` or `head`. The pipeline reports the pager's exit code, so a red run reads green. Let it print in full, or capture to a file and check the status separately.
- **Make each `[[ ... ]]` inside a `run ... /bin/bash <<'EOF'` heredoc gate the test.** `set -e` does not abort a script bash reads from stdin, which is how every one of these tests is fed, so a failed assertion just falls through to the next line and the test still reports ok. Only the heredoc's LAST command decides `$status`. End every in-heredoc assertion with `|| exit 1`, or `printf` the value out and assert on `$output` in the bats body. This is not theoretical: `SQLite cache directory guard ignores and restores caller GLOBIGNORE (#1439)` passed identically with and without the code it was written to protect. A repo-wide sweep found 46 more across 9 files and fixed all of them, so the current count is zero; keep it there. When counting them yourself, note that a heredoc's last command and a line inside a mock function body are both legitimately bare, and that not every block spells the interpreter `/bin/bash` (four use a bare `bash`). When a guard test is added or changed, prove it fails: break the code it protects, watch the test go red, then restore. A green test is not evidence until it has been seen red, and neither is a scanner that reports zero until it has been seen report one.

## Source-invariant tests

A source-invariant test that greps for a forbidden call must strip comment lines first: the comment explaining why `brew list mole` was removed read as the call coming back, and the guard failed on prose rather than on code. `installer source-build guidance names the main branch explicitly` is the shape to copy: it opens with `/^[[:space:]]*#/ { next }` so the assertion only ever sees code, and it ends with `exit (!seen || bad)` so matching nothing fails instead of passing. That second half matters as much as the first, because a grep-shaped guard whose target string was renamed matches zero lines and reports green, which reads as "the forbidden call is gone" when it actually means "the guard stopped looking".

## Refactors and dead code

- **Judge duplication by body, not by name.** Copies that matter have already been renamed, so grep and a read-through both class them as separate helpers: two guards differing only by a `_MOLE_DEV_` vs `_MOLE_USER_` prefix, nine more re-implementing one process-state translation. `scripts/audit_function_duplication.py` hashes normalized bodies and gates on new groups; it found two pairs a manual sweep of the same release had missed. It is blind in the other direction: a probe plus guard that duplicate a live pair's purpose while differing in both name and body pass it cleanly, which is how a second, narrower Autodesk guard sat unreferenced until a caller sweep found it. When comparing counts, check the scope in the [raw deletion contract](../SECURITY_DESIGN.md#layer-2--safe-reason-contract-for-raw-rm).
- **Test-orphan pattern:** before declaring a symbol dead, grep `lib`, `bin`, `cmd`, `scripts`, `tests`, and top-level entry/install scripts; check dynamic lookup through `eval`, `declare -f`, and `compgen`; then re-grep after removal. Trace variables and config written by a removed helper. Tests alone are not production callers, and sub-agent reports are leads, not verdicts.

## Shell portability

For Shell code, Bats tests, install/update flows, timeout wrappers, TTY handling, plist fixtures, or macOS-specific CI, read the [shell and test pitfalls](../../.claude/skills/bugs/references/shell-and-test-pitfalls.md).
