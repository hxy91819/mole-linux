#!/usr/bin/env python3
"""Remove Docker leftovers that nothing is using.

Default is a dry run. ``--execute`` acts. Targets (owner policy 2026-11-04):
anonymous test-database volumes older than one hour, dangling images older
than 24 hours, and BuildKit cache older than 72 hours. Named volumes, tagged
images, and anything retained or locked by the maintenance owner stay.
Go, npm, and pnpm caches are not touched: those trees stay in use.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time

GIB = 2**30
MAINTENANCE_LOCK = "/root/.local/share/fork-maintenance/state/run.lock"
RETAINED_VOLUMES_PATH = "/data/metrics/ai-storage-maintenance/local-data-modes.json"
FAILED_ACTIONS = {"measure failed", "retained volumes unreadable"}


def run(cmd, timeout=1800, check=False):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)


def du(path):
    try:
        result = run(["du", "-sx", "-B1", path], timeout=1800)
    except subprocess.TimeoutExpired:
        return None
    parts = result.stdout.split()
    if result.returncode != 0 or not parts:
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


def free_bytes(path="/data"):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def byte_sum(vols):
    total = 0
    for vol in vols:
        size = vol.get("bytes")
        if not isinstance(size, int):
            return None
        total += size
    return total


def load_retained_names():
    """Names that must not be deleted. None means the file exists but is unusable."""
    path = RETAINED_VOLUMES_PATH
    if not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        vols = data["retained_volumes"]
        if not isinstance(vols, list):
            raise ValueError("retained_volumes")
        names = set()
        for item in vols:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise ValueError("name")
            names.add(item["name"])
        return names
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


def anonymous_db_volumes(min_age_seconds):
    """Dangling anonymous MySQL/PostgreSQL volumes, or empty ones. None if the listing failed."""
    listed = run(["docker", "volume", "ls", "-q", "-f", "dangling=true"], timeout=60)
    if listed.returncode != 0:
        return None
    ids = listed.stdout.split()
    if not ids:
        return []
    inspected = run(["docker", "volume", "inspect", *ids], timeout=120)
    if inspected.returncode != 0:
        return None
    try:
        payload = json.loads(inspected.stdout or "[]")
    except json.JSONDecodeError:
        return None
    out = []
    now = time.time()
    for vol in payload:
        name, labels, mount = vol["Name"], vol.get("Labels") or {}, vol.get("Mountpoint", "")
        if not re.fullmatch(r"[0-9a-f]{64}", name) or not set(labels) <= {"com.docker.volume.anonymous"}:
            continue  # named or compose volumes are never removed here
        if vol.get("Driver") != "local" or not mount.endswith("/_data"):
            continue
        created = time.mktime(time.strptime(vol["CreatedAt"][:19], "%Y-%m-%dT%H:%M:%S"))
        if now - created < min_age_seconds:
            continue
        try:
            entries = os.listdir(mount)
        except OSError:
            continue
        if not entries:
            kind = "empty"
        elif "auto.cnf" in entries or "ibdata1" in entries:
            kind = "mysql"
        elif "PG_VERSION" in entries:
            kind = "postgres"
        else:
            continue  # unknown content stays
        out.append({"id": name, "kind": kind, "bytes": du(mount)})
    return out


def reclaimed_amount(text):
    match = re.search(r"Total reclaimed space:\s*(\S+)", text) or re.search(r"(?m)^Total:\s*(\S+)", text)
    return match.group(1) if match else None


def reclaimed_is_zero(amount):
    return bool(amount) and re.fullmatch(r"0+(?:\.0+)?B", amount.replace(" ", "")) is not None


def hold_maintenance_lock():
    """Non-blocking owner lock shared with fork-maintenance. None if it is unavailable."""
    try:
        handle = open(MAINTENANCE_LOCK, "a")
    except OSError:
        return None
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def docker_caps(args, report, quiet_notes):
    if not shutil.which("docker"):
        return
    lock = hold_maintenance_lock()
    if lock is None:
        report.append({"target": "docker", "action": "skipped: maintenance lock busy"})
        return
    try:
        _docker_caps_locked(args, report, quiet_notes)
    finally:
        lock.close()


def _docker_caps_locked(args, report, quiet_notes):
    vols = anonymous_db_volumes(args.volume_min_age_minutes * 60)
    if vols is None:
        report.append({"target": "docker anonymous test-db volumes", "action": "measure failed"})
        quiet_notes.append("docker anonymous test-db volumes: measure failed")
    else:
        retained = load_retained_names()
        if retained is None:
            report.append({"target": "docker anonymous test-db volumes", "action": "retained volumes unreadable"})
            quiet_notes.append("docker anonymous test-db volumes: retained volumes unreadable")
        else:
            chosen = [vol for vol in vols if vol["id"] not in retained]
            entry: dict[str, object] = {
                "target": "docker anonymous test-db volumes",
                "count": len(chosen),
                "bytes": byte_sum(chosen),
                "action": "docker volume rm" if chosen else "none",
            }
            if args.execute and chosen:
                removed = []
                rc = 0
                for offset in range(0, len(chosen), 25):
                    batch = [vol["id"] for vol in chosen[offset:offset + 25]]
                    result = run(["docker", "volume", "rm", *batch], timeout=600)
                    if result.returncode != 0:
                        rc = result.returncode
                    removed += result.stdout.split()  # in-use volumes are refused by Docker itself
                removed_ids = set(removed)
                removed_bytes = byte_sum([vol for vol in chosen if vol["id"] in removed_ids]) or 0
                entry.update(removed=len(removed))
                if removed or rc:
                    quiet_notes.append(
                        f"docker anonymous test-db volumes: removed {len(removed)} ({removed_bytes} bytes) rc={rc}"
                    )
            report.append(entry)
    for target, cmd in (
        ("docker dangling images older than 24h", ["docker", "image", "prune", "-f", "--filter", "until=24h"]),
        ("docker BuildKit cache older than 72h", ["docker", "builder", "prune", "-f", "--filter", "until=72h"]),
    ):
        entry = {"target": target, "action": " ".join(cmd)}
        if args.execute:
            result = run(cmd, timeout=1800)
            entry.update(rc=result.returncode, result=(result.stdout.strip().splitlines() or [""])[-1][:200])
            if result.returncode != 0:
                entry["failed"] = True
            amount = reclaimed_amount(result.stdout + "\n" + result.stderr)
            if result.returncode != 0 or not reclaimed_is_zero(amount):
                quiet_notes.append(f"{target}: reclaimed {amount or 'unknown'} rc={result.returncode}")
        report.append(entry)


def report_failed(report):
    return any(entry.get("failed") or entry.get("action") in FAILED_ACTIONS for entry in report)


def entry_line(entry):
    size = f" {entry['bytes'] / GIB:.1f} GiB" if isinstance(entry.get("bytes"), int) else ""
    where = f" ({entry['path']})" if entry.get("path") else ""
    return f"- {entry['target']}{where}{size}: {entry['action']}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Remove unused Docker leftovers.")
    ap.add_argument("--volume-min-age-minutes", type=float, default=60)
    ap.add_argument("--execute", action="store_true", help="act; default is a dry run")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing unless something was reclaimed or a command failed")
    args = ap.parse_args(argv)

    before = free_bytes()
    report = []
    quiet_notes = []
    docker_caps(args, report, quiet_notes)
    after = free_bytes()
    if args.quiet:
        if quiet_notes:
            print("\n".join(quiet_notes))
        return 1 if report_failed(report) else 0
    print(json.dumps({"execute": args.execute, "free_before": before, "free_after": after,
                      "items": report}, ensure_ascii=False, indent=1))
    for entry in report:
        print(entry_line(entry), file=sys.stderr)
    return 1 if report_failed(report) else 0


if __name__ == "__main__":
    sys.exit(main())
