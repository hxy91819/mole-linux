#!/usr/bin/env python3
"""Storage audit for Linux development hosts.

The default mode follows Mole's safe workflow: inspect first, classify
candidates, and print commands for an operator to review; it never removes
files or calls Docker prune, package-manager cleanup, or database maintenance
by itself.

The one exception is `clean-tmp`, a bounded age-based cleanup for the /tmp
bind mount and /var/tmp on this host (owner-approved policy: E2E debris otherwise
accumulates forever). It stays dry-run by default, refuses protected roots,
and re-scans live process state immediately before deleting.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def bytes_human(value: int) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    number = float(value)
    for unit in units:
        if number < 1024 or unit == units[-1]:
            return f"{number:.1f}{unit}"
        number /= 1024
    return f"{value}B"


def parse_docker_size(value: str) -> int:
    units = {"TB": 1000**4, "GB": 1000**3, "MB": 1000**2, "kB": 1000, "B": 1}
    for suffix, multiplier in units.items():
        if value.endswith(suffix):
            try:
                return int(float(value[: -len(suffix)]) * multiplier)
            except ValueError:
                return 0
    return 0


def docker_used_image_ids() -> set[str]:
    """Return image digests held by any stopped or running container."""
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "-q"], capture_output=True, text=True,
            timeout=10, check=True,
        )
        container_ids = [line for line in result.stdout.splitlines() if line]
        if not container_ids:
            return set()
        inspected = subprocess.run(
            ["docker", "inspect", "--format", "{{.Image}}", *container_ids],
            capture_output=True, text=True, timeout=15, check=False,
        )
        return {line.strip() for line in inspected.stdout.splitlines() if line.strip()}
    except (OSError, subprocess.SubprocessError):
        return set()


def docker_dangling_images() -> list[dict]:
    try:
        result = subprocess.run(
            ["docker", "image", "ls", "--filter", "dangling=true", "--format", "{{.ID}}\t{{.CreatedAt}}\t{{.Size}}"],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    used_ids = docker_used_image_ids()
    report = []
    for line in result.stdout.splitlines():
        image_id, _, size = line.partition("\t")
        created, _, size = size.partition("\t")
        image_ref = image_id if image_id.startswith("sha256:") else f"sha256:{image_id}"
        referenced = any(
            candidate == image_ref
            or candidate.startswith(image_ref)
            or image_ref.startswith(candidate)
            for candidate in used_ids
        )
        report.append({
            "image": image_id,
            "created": created,
            "bytes": parse_docker_size(size),
            "class": "dangling_docker_image_in_use" if referenced else "dangling_docker_image_unreferenced",
            "referenced_by_container": referenced,
            "risk": "high" if referenced else "medium",
            "action": (
                "保留：仍被容器引用，先确认容器归属"
                if referenced else
                "可纳入 docker image prune；先按创建时间和项目 owner 复核"
            ),
        })
    return sorted(report, key=lambda row: row["bytes"], reverse=True)


def du_bytes(path: Path, timeout: int = 5) -> int | None:
    try:
        result = subprocess.run(
            ["du", "-sx", "--bytes", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        return int(result.stdout.split()[0])
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def docker_volume_report(root: Path) -> list[dict]:
    volumes = root / "lib" / "volumes"
    containers = root / "lib" / "containers"
    if not volumes.is_dir():
        return []

    owners: dict[str, list[str]] = {}
    for config in containers.glob("*/config.v2.json"):
        try:
            payload = json.loads(config.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        running = bool(payload.get("State", {}).get("Running"))
        name = payload.get("Name", config.parent.name)
        for mount in (payload.get("MountPoints") or {}).values():
            volume = mount.get("Name")
            if volume:
                owners.setdefault(volume, []).append(
                    f"{name} ({'running' if running else 'stopped'})"
                )

    report: list[dict] = []
    for volume in volumes.iterdir():
        data = volume / "_data"
        if not data.is_dir():
            continue
        total = 0
        binlogs = 0
        binlog_files = 0
        try:
            for entry in data.rglob("*"):
                if not entry.is_file():
                    continue
                size = entry.stat().st_size
                total += size
                if entry.name.startswith("binlog."):
                    binlogs += size
                    binlog_files += 1
        except OSError:
            continue
        if total:
            report.append(
                {
                    "volume": volume.name,
                    "path": str(data),
                    "bytes": total,
                    "owners": owners.get(volume.name, []),
                    "binlog_bytes": binlogs,
                    "binlog_files": binlog_files,
                }
            )
    return sorted(report, key=lambda row: row["bytes"], reverse=True)


def docker_container_report() -> list[dict]:
    """Classify containers without exposing environment variables or secrets."""
    try:
        ids = subprocess.check_output(["docker", "ps", "-a", "-q"], text=True, timeout=10).split()
        if not ids:
            return []
        payload = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True, timeout=15))
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return []

    rows = []
    review_words = ("e2e", "test", "debug", "preview", "clone", "immutable", "migrate", "init")
    owner_cleanup_names = {"platform-platform-1", "saiens_postgres", "kb-vector-qdrant", "echo-web"}
    for container in payload:
        config = container.get("Config") or {}
        labels = config.get("Labels") or {}
        host = container.get("HostConfig") or {}
        state = container.get("State") or {}
        name = container.get("Name", "").lstrip("/")
        project = labels.get("com.docker.compose.project", "")
        service = labels.get("com.docker.compose.service", "")
        explicit = labels.get("mole.protect") or labels.get("devcloud.protect")
        restart = (host.get("RestartPolicy") or {}).get("Name") or "no"
        searchable = f"{name} {project} {service}".lower()
        if explicit == "true":
            classification = "protected_by_label"
        elif explicit == "false":
            classification = "cleanup_allowed_by_label"
        elif name in owner_cleanup_names or name.startswith("platform-"):
            classification = "owner_confirmed_non_long_term"
        elif state.get("Status") != "running":
            classification = "stopped_unlabeled_review"
        elif any(word in searchable for word in review_words):
            classification = "running_test_like_unlabeled_review"
        elif restart == "unless-stopped":
            classification = "long_service_candidate_unlabeled"
        else:
            classification = "running_unlabeled_review"
        rows.append({
            "name": name,
            "status": state.get("Status", "unknown"),
            "created": container.get("Created", ""),
            "project": project or "standalone",
            "service": service or "standalone",
            "image": config.get("Image", ""),
            "restart_policy": restart,
            "classification": classification,
            "protect_label": explicit,
            "compose_file": labels.get("com.docker.compose.project.config_files"),
            "working_dir": labels.get("com.docker.compose.project.working_dir"),
        })
    return sorted(rows, key=lambda row: (row["classification"], row["name"]))


def stale_file_report(roots: list[Path], age_days: int = 7) -> list[dict]:
    """Find large, old files without assuming they are safe to remove."""
    cutoff = time.time() - age_days * 86400
    report: list[dict] = []
    for root in roots:
        if not root.is_dir():
            continue
        for current, dirs, files in os.walk(root):
            dirs[:] = [name for name in dirs if name not in {".git", "node_modules"}]
            for name in files:
                path = Path(current) / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if stat.st_size < 10 * 1024 * 1024 or stat.st_mtime > cutoff:
                    continue
                report.append({
                    "path": str(path),
                    "bytes": stat.st_size,
                    "class": "stale_file_review_only",
                    "risk": "medium",
                    "action": "confirm owner/process; use logrotate or the producing tool, never truncate blindly",
                })
    return sorted(report, key=lambda row: row["bytes"], reverse=True)[:100]


def filesystem_inventory(root: Path = Path("/data")) -> list[dict]:
    """Measure each direct child independently so one huge tree cannot hide others."""
    report = []
    try:
        children = sorted(root.iterdir())
    except OSError:
        return report
    for child in children:
        started = time.monotonic()
        size = du_bytes(child, timeout=20)
        report.append({
            "path": str(child),
            "bytes": size,
            "scan": "ok" if size is not None else "timeout_or_error",
            "elapsed_seconds": round(time.monotonic() - started, 1),
        })
    return sorted(report, key=lambda row: row["bytes"] or 0, reverse=True)


def deleted_open_report() -> list[dict]:
    """Find deleted /data files still held by processes, including other namespaces."""
    report: dict[str, dict] = {}
    for proc in Path("/proc").glob("[0-9]*"):
        pid = proc.name
        try:
            command = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")[:180]
        except OSError:
            continue
        for fd in (proc / "fd").glob("*"):
            try:
                link = os.readlink(fd)
                if not link.startswith("/data/") or " (deleted)" not in link:
                    continue
                size = fd.stat().st_size
            except OSError:
                continue
            row = report.setdefault(pid, {"pid": pid, "bytes": 0, "files": 0, "command": command})
            row["bytes"] += size
            row["files"] += 1
    return sorted(report.values(), key=lambda row: row["bytes"], reverse=True)


def audit(deep: bool = False) -> dict:
    mounts = []
    try:
        for line in Path("/proc/mounts").read_text().splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[1] in ("/", "/data"):
                mounts.append({"mount": fields[1], "device": fields[0], "fstype": fields[2]})
    except OSError:
        pass

    # Avoid recursively walking every child of /data: Docker overlay2 and
    # large monorepos can take minutes and duplicate mounted-container data.
    # These are the storage pools that commonly fill a development host.
    roots = [
        Path("/data/anydev"),
        Path("/data/tmp"),
        Path("/data/.pnpm-store"),
        Path("/data/home"),
        Path("/data/code/SaiensCreditSystemRails/.worktrees"),
        Path("/var/cache"),
        Path("/var/log"),
        Path("/root/.cache"),
    ]
    top_level = []
    for root in roots:
        if not root.is_dir():
            continue
        size = du_bytes(root)
        if size:
            top_level.append({"path": str(root), "bytes": size})

    candidates = []
    for row in top_level:
        path = row["path"]
        if path in ("/data/tmp", "/data/system-tmp") or "/worktrees" in path:
            candidates.append({
                **row,
                "class": "reviewable_rebuildable",
                "risk": "medium",
                "action": f"find {path} -xdev -type f -mtime +7 -print",
            })
        elif path.endswith("/.pnpm-store") or path.endswith("/.npm"):
            candidates.append({
                **row,
                "class": "package_cache",
                "risk": "medium",
                "action": "pnpm store prune --dry-run (if supported), then pnpm store prune",
            })
        elif path.endswith("/log"):
            candidates.append({
                **row,
                "class": "logs",
                "risk": "medium",
                "action": "logrotate -d /etc/logrotate.conf; use journalctl --vacuum-time=14d only after policy review",
            })

    docker_volumes = docker_volume_report(Path("/data/docker"))
    for row in docker_volumes:
        if row["binlog_bytes"]:
            row["class"] = "database_binlog_review_only"
            row["risk"] = "high"
            row["action"] = "inspect MySQL binlog_expire_logs_seconds / PURGE BINARY LOGS; never rm binlog.*"
        else:
            row["class"] = "docker_volume"
            row["risk"] = "high"
            row["action"] = "docker volume inspect; remove only when owner and container references are proven absent"

    stale_files = stale_file_report([
        Path("/data/anydev/agent/logs"),
        Path("/var/log"),
        Path("/data/tmp"),
    ])
    dangling_images = docker_dangling_images()
    containers = docker_container_report()
    inventory = filesystem_inventory() if deep else []
    deleted_open = deleted_open_report()
    return {
        "mounts": mounts,
        "top_level": sorted(top_level, key=lambda row: row["bytes"], reverse=True),
        "candidates": sorted(candidates, key=lambda row: row["bytes"], reverse=True),
        "docker_volumes": docker_volumes,
        "stale_files": stale_files,
        "dangling_images": dangling_images,
        "containers": containers,
        "filesystem_inventory": inventory,
        "deleted_open": deleted_open,
    }


def cmd_clean_tmp(argv: list[str]) -> int:
    from devcloud_tmp import main as clean_tmp_main

    return clean_tmp_main(argv)


def proc_path_refs(target: str) -> list[dict]:
    from devcloud_tmp import process_snapshot

    return process_snapshot(Path(target), time.monotonic() + 30)["processes"]


def cmd_who_uses(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="devcloud.py who-uses",
        description=(
            "Report live processes referencing a path (exe/cwd/fd/maps). "
            "Exit 0 = no path references, 1 = references found, 2 = incomplete probe. "
            "The result alone does not authorize deletion."
        ),
    )
    parser.add_argument("path", type=Path, help="file or directory to probe")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)
    target = str(args.path.resolve())
    try:
        hits = proc_path_refs(target)
    except (OSError, RuntimeError, ValueError) as error:
        if args.json:
            print(json.dumps({"path": target, "references": [], "count": 0,
                              "state": "unknown", "error": str(error)}))
        else:
            print(f"who-uses: unknown: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({"path": target, "references": hits, "count": len(hits)}, indent=2))
    else:
        print(f"who-uses: {target}")
        if not hits:
            print("  no live process references found")
        for row in hits:
            print(f"  pid={row['pid']} via={','.join(row['via'])}  {row['cmdline']}")
    return 1 if hits else 0


def run_text(cmd: list[str], timeout: int = 60) -> str | None:
    """Run a read-only command and return stdout, or None on any failure."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _mount_table() -> list[dict]:
    rows = []
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return rows
    for line in lines:
        fields = line.split()
        if len(fields) < 10 or "-" not in fields:
            continue
        sep = fields.index("-")
        if len(fields) < sep + 3:
            continue
        rows.append({
            "root": fields[3],
            "target": fields[4],
            "fstype": fields[sep + 1],
            "source": fields[sep + 2],
        })
    return rows


def bind_source(alias: Path) -> Path | None:
    """Map a bind-mounted alias back to the directory whose bytes it really uses.

    /tmp here is an empty tmpfs layered over a read-only bind of
    /data/system-tmp, so paths under it can exist for every other process while
    this one cannot see them — git reports those worktrees "prunable" for the
    same reason.  `findmnt -T <path>` only answers with the topmost mount
    (tmpfs), so the bind is recovered from mountinfo: the deepest ancestor
    mount whose root is not "/" gives the subdirectory, and the same device's
    root mount gives the filesystem to prepend it to.
    """
    mounts = _mount_table()
    if not mounts:
        return None
    best = None
    for mount in mounts:
        target = Path(mount["target"])
        if mount["root"] in ("/", "") or mount["fstype"] in ("tmpfs", "devtmpfs", "proc", "sysfs"):
            continue
        if alias != target and target not in alias.parents:
            continue
        if best is None or len(mount["target"]) > len(best["target"]):
            best = mount
    if best is None:
        return None
    bases = [m for m in mounts if m["source"] == best["source"] and m["root"] == "/"]
    if not bases:
        return None
    base = Path(min(bases, key=lambda m: len(m["target"]))["target"])
    return base / best["root"].lstrip("/") / alias.relative_to(best["target"])


def project_sizes(root: Path, timeout: int) -> tuple[list[dict], bool]:
    """One traversal beats one du per child: separate walks re-read shared data."""
    rows: list[dict] = []
    complete = False
    try:
        proc = subprocess.Popen(
            ["du", "-xd1", "--bytes", str(root)],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError:
        return rows, complete
    try:
        stdout, _ = proc.communicate(timeout=timeout)
        complete = proc.returncode == 0
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, _ = proc.communicate()
    for line in (stdout or "").splitlines():
        size, _, path = line.partition("\t")
        if not path or Path(path) == root:
            continue
        # A .git directory is repository metadata, not a project of its own; other
        # dot-directories are kept because they can hold real data.
        if Path(path).name == ".git":
            continue
        try:
            rows.append({"path": path, "bytes": int(size)})
        except ValueError:
            continue
    rows.sort(key=lambda row: row["bytes"], reverse=True)
    return rows, complete


def git_worktree_entries(repo: Path) -> list[dict]:
    out = run_text(["git", "-C", str(repo), "worktree", "list", "--porcelain"], timeout=60)
    if out is None:
        return []
    entries: list[dict] = []
    current: dict = {}
    for line in [*out.splitlines(), ""]:
        if not line.strip():
            if current:
                entries.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value[:12]
        elif key == "branch":
            current["branch"] = value.removeprefix("refs/heads/")
        elif key == "detached":
            current["branch"] = "(detached)"
        elif key in ("locked", "prunable"):
            current[key] = value or True
    return entries


def describe_worktree(entry: dict, primary: Path, du_timeout: int) -> dict:
    path = Path(entry["path"])
    real = bind_source(path) or path
    row = {
        "path": str(path),
        "real_path": str(real) if real != path else None,
        "branch": entry.get("branch", "(unknown)"),
        "head": entry.get("head", ""),
        "primary": path == primary,
        "locked": bool(entry.get("locked")),
        "git_prunable": bool(entry.get("prunable")),
        "exists": real.exists(),
        "dirty": None,
        "bytes": None,
        "head_epoch": None,
    }
    if not row["exists"]:
        return row
    status = run_text(["git", "-C", str(real), "status", "--porcelain", "--untracked-files=all"], timeout=90)
    row["dirty"] = None if status is None else bool(status.strip())
    row["bytes"] = du_bytes(real, timeout=du_timeout)
    stamp = run_text(["git", "-C", str(real), "log", "-1", "--format=%ct"], timeout=30)
    row["head_epoch"] = int(stamp) if stamp and stamp.strip().isdigit() else None
    return row


def discover_repos(root: Path, max_depth: int) -> list[Path]:
    """Checkouts under root, one entry per repository, primary checkout preferred."""
    out = run_text(["find", str(root), "-maxdepth", str(max_depth), "-name", ".git"], timeout=180)
    if out is None:
        return []
    seen: dict[str, Path] = {}
    for line in out.splitlines():
        checkout = Path(line).parent
        # .git/modules/<name> holds submodule internals, not a checkout of its own.
        if ".git" in checkout.parts:
            continue
        # A submodule checkout is part of its parent repository's tree; listing it
        # as a separate repository reports the parent's worktrees twice.
        if run_text(["git", "-C", str(checkout), "rev-parse", "--show-superproject-working-tree"], timeout=30):
            continue
        common = run_text(
            ["git", "-C", str(checkout), "rev-parse", "--path-format=absolute", "--git-common-dir"],
            timeout=30,
        )
        if not common:
            continue
        existing = seen.get(common.strip())
        if existing is None or (Path(line).is_dir() and not (existing / ".git").is_dir()):
            seen[common.strip()] = checkout
    return sorted(seen.values())


def worktree_inventory(root: Path, max_depth: int, du_timeout: int) -> list[dict]:
    repos = []
    for repo in discover_repos(root, max_depth):
        # The primary checkout is already measured by project_sizes; only linked
        # worktrees need their own status and du pass.
        rows = [
            describe_worktree(entry, repo, du_timeout)
            for entry in git_worktree_entries(repo)
            if Path(entry["path"]) != repo
        ]
        if not rows:
            continue
        repos.append({
            "repo": str(repo),
            "linked": len(rows),
            "linked_bytes": sum(row["bytes"] or 0 for row in rows),
            "dirty": sum(1 for row in rows if row["dirty"]),
            "absent": sum(1 for row in rows if not row["exists"]),
            "rows": rows,
        })
    repos.sort(key=lambda row: row["linked_bytes"], reverse=True)
    return repos


def cmd_projects(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="devcloud.py projects",
        description=(
            "Read-only per-project storage report: top-level directories under a root plus "
            "every linked git worktree with its real size, cleanliness, and on-disk presence. "
            "It never deletes; removal decisions belong to the worktree-cleanup skill after a "
            "dry-run, because only that skill checks pull-request and remote durability."
        ),
    )
    parser.add_argument("--root", type=Path, default=Path("/data/code"), help="directory holding project checkouts")
    parser.add_argument("--top", type=int, default=20, help="how many top-level directories to print")
    parser.add_argument("--max-depth", type=int, default=4, help="search depth for .git entries")
    parser.add_argument("--du-timeout", type=int, default=60, help="per-worktree du timeout in seconds")
    parser.add_argument("--no-worktrees", action="store_true", help="skip the git worktree inventory")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    sizes, complete = project_sizes(args.root, timeout=max(180, args.du_timeout * 4))
    repos = [] if args.no_worktrees else worktree_inventory(args.root, args.max_depth, args.du_timeout)

    if args.json:
        print(json.dumps({
            "root": str(args.root),
            "scan_complete": complete,
            "projects": sizes,
            "repos": repos,
            "deleted": 0,
        }, indent=2))
        return 0

    print(f"Project usage under {args.root} (read-only; du -xd1)")
    if not complete:
        print("  WARNING: traversal was interrupted, sizes are partial and must not be summed as a total")
    for row in sizes[: args.top]:
        print(f"  {bytes_human(row['bytes']):>10}  {row['path']}")
    if len(sizes) > args.top:
        rest = sum(row["bytes"] for row in sizes[args.top:])
        print(f"  {bytes_human(rest):>10}  (+{len(sizes) - args.top} smaller directories)")

    if repos:
        print("\nLinked git worktrees by repository (primary checkout excluded)")
        for repo in repos:
            print(
                f"\n  {repo['repo']}\n"
                f"    linked={repo['linked']}  size={bytes_human(repo['linked_bytes'])}"
                f"  dirty={repo['dirty']}  absent={repo['absent']}"
            )
            for row in repo["rows"]:
                if row["primary"]:
                    continue
                flags = ["ABSENT" if not row["exists"] else
                         "UNKNOWN" if row["dirty"] is None else
                         "DIRTY" if row["dirty"] else "clean"]
                if row["locked"]:
                    flags.append("locked")
                if row["git_prunable"]:
                    flags.append("git-prunable")
                size = "absent" if not row["exists"] else "timeout" if row["bytes"] is None else bytes_human(row["bytes"])
                when = time.strftime("%Y-%m-%d", time.localtime(row["head_epoch"])) if row["head_epoch"] else "----------"
                alias = f"  -> {row['real_path']}" if row["real_path"] else ""
                print(f"    {size:>9}  {'/'.join(flags):<22} head={when}  {row['path']}{alias}")
        print(
            "\n  ABSENT means the directory is not in this process's namespace; git calls those paths\n"
            "  prunable. Check the -> path before concluding anything, then read a real size there.\n"
            "  Removal is a separate, human-approved step: run the worktree-cleanup skill dry-run\n"
            "  (it verifies merged/closed PRs and remote durability; never remove a DIRTY worktree)."
        )
    print("\nNo files were deleted.")
    return 0


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "clean-tmp":
        raise SystemExit(cmd_clean_tmp(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "who-uses":
        raise SystemExit(cmd_who_uses(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "projects":
        raise SystemExit(cmd_projects(sys.argv[2:]))
    parser = argparse.ArgumentParser(description="Audit Linux development storage without deleting anything")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--deep", action="store_true", help="scan every /data direct child independently")
    args = parser.parse_args()
    report = audit(deep=args.deep)
    if args.json:
        print(json.dumps(report, indent=2))
        return

    print("DevCloud storage audit (read-only; no cleanup performed)")
    print("\nMounts:")
    for row in report["mounts"]:
        print(f"  {row['mount']}: {row['device']} ({row['fstype']})")
    print("\nLargest top-level paths:")
    for row in report["top_level"][:20]:
        print(f"  {bytes_human(row['bytes']):>9}  {row['path']}")
    if report["filesystem_inventory"]:
        print("\nFull /data direct-child inventory:")
        for row in report["filesystem_inventory"]:
            size = bytes_human(row["bytes"]) if row["bytes"] is not None else "TIMEOUT"
            print(f"  {size:>9}  [{row['scan']}] {row['path']}")
    if report["deleted_open"]:
        print("\nDeleted /data files still held open:")
        for row in report["deleted_open"][:20]:
            print(f"  {bytes_human(row['bytes']):>9}  pid={row['pid']} files={row['files']} {row['command']}")
    print("\nReviewable candidates:")
    for row in report["candidates"]:
        print(f"  {bytes_human(row['bytes']):>9}  [{row['risk']}] {row['class']}  {row['path']}")
        print(f"             review: {row['action']}")
    print("\nDocker volumes requiring operator review:")
    for row in report["docker_volumes"][:20]:
        owners = ", ".join(row["owners"]) or "unreferenced by config"
        note = f", binlogs={bytes_human(row['binlog_bytes'])}" if row["binlog_bytes"] else ""
        action = row.get("action", "")
        print(f"  {bytes_human(row['bytes']):>9}  {row['volume']} ({owners}{note})")
        print(f"             review: {action}")
    print("\nLarge stale files requiring owner review:")
    for row in report["stale_files"][:20]:
        print(f"  {bytes_human(row['bytes']):>9}  {row['path']}")
        print(f"             review: {row['action']}")
    if report["dangling_images"]:
        print("\nDangling Docker images (virtual size; shared layers not additive):")
        referenced = [row for row in report["dangling_images"] if row["referenced_by_container"]]
        unreferenced = [row for row in report["dangling_images"] if not row["referenced_by_container"]]
        virtual_total = sum(row["bytes"] for row in report["dangling_images"])
        print(
            f"  summary: {len(report['dangling_images'])} total, "
            f"{len(unreferenced)} unreferenced, {len(referenced)} referenced; "
            f"virtual total={bytes_human(virtual_total)} (not reclaim estimate)"
        )
        for row in report["dangling_images"][:20]:
            state = "IN-USE" if row["referenced_by_container"] else "unreferenced"
            print(f"  {bytes_human(row['bytes']):>9}  [{state}] {row['image']}  created={row['created']}")
            print(f"             review: {row['action']}")
    if report["containers"]:
        print("\nDocker containers (label-aware; no container was changed):")
        counts = {}
        for row in report["containers"]:
            counts[row["classification"]] = counts.get(row["classification"], 0) + 1
        print("  summary: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
        for row in report["containers"]:
            print(
                f"  [{row['classification']}] {row['name']} "
                f"status={row['status']} restart={row['restart_policy']} project={row['project']}"
            )
    print("\nNo files were deleted. Review database retention and container ownership before any cleanup.")


if __name__ == "__main__":
    main()
