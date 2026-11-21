# Install and self-update

## Installer verification

- **`install.sh` stays fail-closed on verification failure.** A checksum or attestation mismatch aborts and says why; it must never downgrade to a source build, which turns "the binary was tampered with" into a quieter path with weaker verification. Resolving no release tag and falling back to `main` must warn that this is a nightly source install. The abort cases in `tests/install_checksum.bats` pin both. Keep the README install URL on unpinned `main`: pinning it there blocks fixes from reaching new installs.

## Refusal diagnostics

- **A gate that refuses must name which cause it hit and what to run next.** Install and update gates fail for causes with nothing in common: an untrusted ancestor, no admin session, a planted lock path, real contention. "Reinstall" fixes none of them, so a single catch-all message leaves the user with no move. `acquire_install_lock` returns a reason through `INSTALL_LOCK_FAILURE` and `report_install_lock_failure` prints one line of cause plus one line of command; keep that shape and add a reason rather than widening the catch-all. Before adding a gate, check what the previous path told the user and keep its diagnosis at least as actionable. The fix for #1335 swapped one vague message for another and locked it in with a source-invariant test. Assert the reason codes. See [source-invariant test pitfalls](testing-pitfalls.md#source-invariant-tests) for the assertion pattern.

- For unsafe ancestors, `install_lock_has_unsafe_ancestor` refuses for a symlink, an unreadable stat, a foreign owner, a loose mode, or an ACL, and they need different commands, since `chown` does not clear an ACL and `chmod` does not undo a symlink. It reports which through `INSTALL_LOCK_UNSAFE_ANCESTOR_REASON`; `tests/install_checksum.bats` asserts every raised code reaches its own branch and every branch names a next step.

## Bootstrap recovery and locking

- The `mo update` self-heal fallback (`_update_self_heal_reinstall`) exists because the local bootstrap (temp file, registry, exec) is frozen on the user's machine and a broken installed version cannot fix itself (#1297). Keep it streaming `install.sh` from `main` straight into bash with no local temp files.

- Verify stable success against the installed binary's version response. Installer output alone produced a false success in V1.47.1. Run every post-update version probe through `run_with_timeout` with `MOLE_TIMEOUT_QUICK_DETECT_SEC`. Bound the standalone `install.sh` probes for `--version` and `--help` with its local wrapper. Nightly success additionally requires a per-attempt install receipt. Pin the source archive to the resolved commit when HEAD is known. When it is unknown, discard any older `COMMIT_HASH`.

- Keep updates single-flight per install directory so receipt, commit metadata, and binary verification cannot cross concurrent generations. Both writers take the same target-adjacent mutex, preferring absolute `/usr/bin/lockf` because the kernel drops that lock even if the holder is killed. Keep the kernel lock held by a process tied to the parent's liveness.

- `lockf` only ships with newer macOS, so requiring it made install and update exit before writing a file on every older release (#1348). Where it is absent, both fall back to an atomic `mkdir` in the lock directory and reclaim it only with proof the recorded owner is gone (dead PID or a live PID whose start time no longer matches). Treat a lock command that runs and refuses as contention. When the platform lacks that command, refuse only if neither primitive is available. Avoid a shell array for the wrapper because the empty fallback array raises an unbound-variable error under `set -u` on macOS Bash 3.2. Regression tests live in `tests/update.bats` and `tests/install_checksum.bats`.
