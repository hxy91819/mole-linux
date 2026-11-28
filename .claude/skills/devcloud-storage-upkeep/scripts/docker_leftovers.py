#!/usr/bin/env python3
"""Remove Docker leftovers that nothing is using.

Default is a dry run. ``--execute`` acts. Targets (owner policy 2026-11-04, extended 2026-11-28):
anonymous test-database volumes older than one hour, dangling images older
than 24 hours, unreferenced commit/version tags older than 24 hours outside the latest
repository build batch, and BuildKit cache (72h age prune plus 10GB cap). Named
volumes, repository newest batches and owner-protected images stay.
Go, npm, and pnpm caches are not touched: those trees stay in use.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import time

GIB = 2**30
BUILD_CACHE_LIMIT_BYTES = 10_000_000_000
MAINTENANCE_LOCK_WAIT_SECONDS = 60
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


def hold_maintenance_lock(wait_seconds=0):
    """Wait through short competing maintenance jobs; never bypass their owner lock."""
    handle = open(MAINTENANCE_LOCK, 'a')
    deadline = time.monotonic() + wait_seconds
    try:
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return handle
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    handle.close()
                    return None
                time.sleep(min(1, remaining))
    except BaseException:
        handle.close()
        raise


def docker_caps(args, report, quiet_notes):
    if not shutil.which("docker"):
        return
    try:
        lock = hold_maintenance_lock(MAINTENANCE_LOCK_WAIT_SECONDS if args.execute else 0)
    except OSError as exc:
        report.append({'target': 'docker', 'action': 'maintenance lock unavailable', 'failed': True})
        quiet_notes.append(f'docker: maintenance lock unavailable: {exc}; cache limit not checked')
        return
    if lock is None:
        report.append({"target": "docker", "action": "skipped: maintenance lock busy"})
        quiet_notes.append('docker: skipped: maintenance lock busy; no Docker cleanup executed; cache limit not checked')
        return
    try:
        _docker_caps_locked(args, report, quiet_notes)
    finally:
        lock.close()


def docker_json(kind, names):
    """Only accept a complete, successful inspect; partial evidence never authorizes removal."""
    out = []
    for offset in range(0, len(names), 100):
        batch = names[offset:offset + 100]
        result = run(['docker', kind, 'inspect', *batch], timeout=120)
        if result.returncode:
            raise ValueError(f'{kind} inspect failed: {result.stderr.strip()}')
        payload = json.loads(result.stdout)
        if not isinstance(payload, list) or len(payload) != len(batch):
            raise ValueError(f'{kind} inspect incomplete')
        out.extend(payload)
    return out


def container_images():
    result = run(['docker', 'container', 'ls', '-aq', '--no-trunc'], timeout=60)
    if result.returncode:
        raise ValueError('container listing failed: ' + result.stderr.strip())
    images = set()
    for container in docker_json('container', result.stdout.split()):
        image = container['Image']
        if not isinstance(image, str) or not image:
            raise ValueError('container image missing')
        images.add(image)
    return images


def image_created(image):
    created = datetime.fromisoformat(image['Created'].replace('Z', '+00:00'))
    if created.tzinfo is None:
        raise ValueError('image Created has no timezone')
    return created.timestamp()


def owner_protected(image):
    labels = (image.get('Config') or {}).get('Labels') or {}
    return any(labels.get(key) == 'true' for key in ('mole.protect', 'devcloud.protect'))


def image_batch(tag):
    """Commit suffixes and dotted versions identify a build batch, independent of variant."""
    value = tag.rsplit(':', 1)[1]
    commit = re.search(r'(?:^|[-_])([0-9a-fA-F]{7,64})$', value)
    if commit:
        return 'commit:' + commit.group(1).lower()
    version = re.search(r'(?:^|[-_])(v?\d+\.\d+(?:\.\d+)?(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)$', value)
    return 'version:' + version.group(1).removeprefix('v') if version else None


def newest_batch_tags(tags, tag):
    repo = tag.rsplit(':', 1)[0]
    batches = {}
    for name, image in tags.items():
        batch = image_batch(name)
        if name.rsplit(':', 1)[0] == repo and batch:
            batches[batch] = max(batches.get(batch, float('-inf')), image_created(image))
    if not batches:
        return []
    newest = max(batches.values())
    latest = {key for key, created in batches.items() if created == newest}
    return sorted((name for name in tags if name.rsplit(':', 1)[0] == repo and image_batch(name) in latest),
                  key=lambda name: image_created(tags[name]), reverse=True)


def tagged_inventory():
    listed = run(['docker', 'image', 'ls', '-aq', '--no-trunc'], timeout=60)
    if listed.returncode:
        raise ValueError('image listing failed: ' + listed.stderr.strip())
    images = docker_json('image', list(dict.fromkeys(listed.stdout.split())))
    tags = {}
    for image in images:
        # Parse every creation time before any removal: unknown inventory stays intact.
        image_created(image)
        for tag in image.get('RepoTags') or []:
            if ':' in tag.rsplit('/', 1)[-1] and not tag.startswith('<none>'):
                tags[tag] = image
    return tags


def tagged_images(args, report, quiet_notes):
    target = 'docker old commit/version image batches'
    try:
        tags = tagged_inventory()
        if not tags:
            return
        try:
            references = container_images()
        except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
            for tag, image in tags.items():
                report.append({'target': tag, 'image_id': image['Id'],
                               'action': 'keep: reference query failed', 'failed': True})
            quiet_notes.append(f'{target}: reference query failed: {exc}')
            return
        for tag, image in tags.items():
            entry = {'target': tag, 'image_id': image['Id'], 'image_size_bytes': image['Size']}
            newest = newest_batch_tags(tags, tag)
            if owner_protected(image):
                reason = 'owner protection'
            elif image['Id'] in references:
                reason = 'container reference'
            elif image_batch(tag) is None:
                reason = 'fixed tag'
            elif tag in newest:
                reason = 'latest batch'
            elif time.time() - image_created(image) <= 24 * 3600:
                reason = 'not older than 24 hours'
            else:
                reason = None
            if reason is None and args.execute:
                # Rebind the exact tag and its strictly newer repository witness.
                try:
                    # Tags can be reassigned while a batch is being examined. Recompute
                    # the repository's latest batch before removing each old member.
                    fresh_tags = tagged_inventory()
                    newest = newest_batch_tags(fresh_tags, tag)
                    if tag not in fresh_tags or not newest:
                        raise ValueError('tag or latest batch disappeared during recheck')
                    if tag in newest:
                        entry['action'] = 'keep: latest batch'
                        report.append(entry)
                        continue
                    current, witness = docker_json('image', [tag, newest[0]])
                except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
                    entry.update(action='keep: final image query failed', failed=True, error=str(exc))
                    report.append(entry)
                    quiet_notes.append(f'{tag}: final image query failed: {exc}')
                    continue
                if current['Id'] != image['Id'] or tag not in (current.get('RepoTags') or []):
                    reason = 'tag changed'
                elif owner_protected(current):
                    reason = 'owner protection'
                elif time.time() - image_created(current) <= 24 * 3600:
                    reason = 'not older than 24 hours'
                elif newest[0] not in (witness.get('RepoTags') or []) or image_created(witness) <= image_created(current):
                    reason = 'repository newer image changed'
                else:
                    try:
                        fresh_references = container_images()
                    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
                        reason = 'reference query failed'
                        entry['failed'] = True
                        quiet_notes.append(f'{tag}: {reason}: {exc}')
                    else:
                        if current['Id'] in fresh_references:
                            reason = 'container reference'
            entry['action'] = f'keep: {reason}' if reason else 'docker image rm ' + tag
            if reason is None and args.execute:
                result = run(['docker', 'image', 'rm', tag], timeout=600)
                entry.update(rc=result.returncode, removed=result.returncode == 0,
                             failed=result.returncode != 0, result=result.stdout.strip(), error=result.stderr.strip())
                quiet_notes.append(f'{tag}: removed={result.returncode == 0} rc={result.returncode}')
            report.append(entry)
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
        report.append({'target': target, 'action': 'measure failed', 'error': str(exc)})
        quiet_notes.append(f'{target}: measure failed: {exc}')


def builder_cap(report, quiet_notes):
    try:
        help_result = run(['docker', 'builder', 'prune', '--help'], timeout=60)
        if help_result.returncode:
            raise ValueError('builder prune help failed')
        for flag in ('--max-used-space', '--keep-storage'):
            if flag in help_result.stdout:
                return ['docker', 'builder', 'prune', '-f', flag, str(BUILD_CACHE_LIMIT_BYTES)]
        raise ValueError('builder prune has no supported storage limit')
    except (ValueError, OSError, subprocess.TimeoutExpired) as exc:
        report.append({'target': 'docker BuildKit 10GB cap', 'action': 'measure failed', 'error': str(exc)})
        quiet_notes.append(f'docker BuildKit 10GB cap: {exc}')
        return None


def verify_builder_limit(report, quiet_notes):
    """Check Docker's post-prune total even when prune reports successful zero reclaim."""
    target = 'docker BuildKit cache limit verification'
    try:
        result = run(['docker', 'system', 'df', '--format', '{{json .}}'], timeout=120)
        if result.returncode:
            raise ValueError(result.stderr.strip() or 'docker system df failed')
        rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        sizes = [row['Size'] for row in rows if row.get('Type') == 'Build Cache']
        if len(sizes) != 1:
            raise ValueError('missing or duplicate Build Cache summary')
        match = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(B|kB|KB|MB|GB|TB|KiB|MiB|GiB|TiB)', sizes[0])
        if not match:
            raise ValueError('invalid Build Cache size: ' + str(sizes[0]))
        units = {'B': 1, 'kB': 1000, 'KB': 1000, 'MB': 10**6, 'GB': 10**9, 'TB': 10**12,
                 'KiB': 2**10, 'MiB': 2**20, 'GiB': 2**30, 'TiB': 2**40}
        size = int(Decimal(match.group(1)) * units[match.group(2)])
        over = size > BUILD_CACHE_LIMIT_BYTES
        report.append({'target': target, 'bytes': size, 'limit_bytes': BUILD_CACHE_LIMIT_BYTES,
                       'action': 'WARNING: build cache still exceeds 10GB' if over else 'within limit', 'failed': over})
        if over:
            quiet_notes.append(f'WARNING: docker BuildKit cache still {size} bytes after prune, limit {BUILD_CACHE_LIMIT_BYTES}; inspect docker buildx du --verbose for shared/in-use records')
    except (ValueError, KeyError, TypeError, OSError, subprocess.TimeoutExpired) as exc:
        report.append({'target': target, 'action': 'measure failed', 'error': str(exc)})
        quiet_notes.append(f'{target}: measure failed: {exc}')


def _docker_caps_locked(args, report, quiet_notes):
    tagged_images(args, report, quiet_notes)
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
    cap = builder_cap(report, quiet_notes)
    prunes = [
        ("docker dangling images older than 24h", ["docker", "image", "prune", "-f", "--filter", "until=24h",
             "--filter", "label!=mole.protect=true", "--filter", "label!=devcloud.protect=true"]),
        ("docker BuildKit cache older than 72h", ["docker", "builder", "prune", "-f", "--filter", "until=72h"]),
    ]
    if cap:
        prunes.append(("docker BuildKit cache at most 10GB", cap))
    for target, cmd in prunes:
        entry = {"target": target, "action": " ".join(cmd)}
        if args.execute:
            result = run(cmd, timeout=1800)
            # New buildx clients advertise this flag even with an older BuildKit daemon.
            # That version check rejects before pruning; only this exact refusal permits
            # the older keep-storage protocol, never a retry on permission/daemon errors.
            if (result.returncode != 0 and '--max-used-space' in cmd
                    and 'buildkit v0.17.0+ is required for max-used-space and min-free-space filters' in result.stderr):
                entry['unsupported_attempt'] = {'action': ' '.join(cmd), 'rc': result.returncode,
                                                'error': result.stderr.strip()}
                cmd = ['docker', 'builder', 'prune', '-f', '--keep-storage', str(BUILD_CACHE_LIMIT_BYTES)]
                entry['action'] = ' '.join(cmd)
                result = run(cmd, timeout=1800)
            entry.update(rc=result.returncode, result=(result.stdout.strip().splitlines() or [""])[-1][:200],
                         error=result.stderr.strip())
            if result.returncode != 0:
                entry["failed"] = True
            amount = reclaimed_amount(result.stdout + "\n" + result.stderr)
            if result.returncode != 0 or not reclaimed_is_zero(amount):
                quiet_notes.append(f"{target}: reclaimed {amount or 'unknown'} rc={result.returncode}"
                                   + (f" {result.stderr.strip()}" if result.returncode else ""))
        report.append(entry)

    if args.execute:
        verify_builder_limit(report, quiet_notes)


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
                    help="print only reclaim, failures, or blocked/unchecked maintenance")
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
