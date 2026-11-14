#!/usr/bin/env python3
"""Delete Go build-cache entries that have not been used for 24 hours.

Default is a dry run. ``--execute`` unlinks. Only regular files named
``<64 hex>-a`` or ``<64 hex>-d`` inside the 256 hash buckets are eligible.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import subprocess
import sys
import time

# Go refreshes a cache entry's mtime to now when the entry is used and that
# mtime is already older than one hour (cmd/go/internal/cache, mtimeInterval).
# Keeping entries for 24 hours preserves day-to-day rebuild reuse while still
# pruning cold entries well after any running compile could need them.
MAX_AGE_SECONDS = 24 * 3600
ENTRY_NAME = re.compile(r"^[0-9a-f]{64}-[ad]$")
README_FIRST_LINE = "This directory holds cached build artifacts from the Go build system."
LINT_README_FIRST_LINE = "This directory holds cached build artifacts from golangci-lint."


def run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def cache_directory():
    go = shutil.which("go")
    if not go:
        raise RuntimeError("go not found")
    result = run([go, "env", "GOCACHE"])
    path = result.stdout.strip()
    if result.returncode != 0 or not path:
        raise RuntimeError((result.stderr or result.stdout or "go env GOCACHE failed").strip()[:200])
    return os.path.realpath(path)


def lint_cache_directory():
    return os.path.realpath(os.path.expanduser("~/.cache/golangci-lint"))


def readme_ok(cache, expected=README_FIRST_LINE):
    try:
        with open(os.path.join(cache, "README"), "r", encoding="utf-8", errors="replace") as handle:
            line = handle.readline()
    except OSError:
        return False
    if line.endswith("\n"):
        line = line[:-1]
    if line.endswith("\r"):
        line = line[:-1]
    return line == expected


def cache_bytes(cache):
    result = run(["du", "-sx", "-B1", cache], timeout=1800)
    parts = result.stdout.split()
    if result.returncode != 0 or not parts:
        return None
    try:
        return int(parts[0])
    except ValueError:
        return None


def eligible(cache, now):
    """Return (path, size) for entries old enough to remove. Raise on an unreadable bucket."""
    found = []
    cutoff = now - MAX_AGE_SECONDS
    for index in range(256):
        directory = os.path.join(cache, f"{index:02x}")
        try:
            info = os.lstat(directory)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(f"cannot read {directory}: {exc}") from exc
        if not stat.S_ISDIR(info.st_mode):
            continue  # a symlink bucket is not followed
        try:
            names = os.listdir(directory)
        except OSError as exc:
            raise RuntimeError(f"cannot read {directory}: {exc}") from exc
        for name in names:
            if ENTRY_NAME.match(name) is None:
                continue
            path = os.path.join(directory, name)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise RuntimeError(f"cannot stat {path}: {exc}") from exc
            if not stat.S_ISREG(st.st_mode) or st.st_mtime >= cutoff:
                continue
            found.append(path)
    return found


def still_eligible(path, cutoff):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"cannot stat {path}: {exc}") from exc
    if not stat.S_ISREG(st.st_mode) or st.st_mtime >= cutoff:
        return None
    return st.st_size


def trim_cache(cache, label, expected, args):
    try:
        if not readme_ok(cache, expected):
            raise RuntimeError("README missing or unexpected")
        now = time.time()
        paths = eligible(cache, now)
        deleted = 0
        deleted_bytes = 0
        if args.execute:
            cutoff = now - MAX_AGE_SECONDS
            for path in paths:
                size = still_eligible(path, cutoff)
                if size is None:
                    continue
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    continue
                deleted += 1
                deleted_bytes += size
        else:
            deleted = len(paths)
            deleted_bytes = 0
            for path in paths:
                try:
                    st = os.lstat(path)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode):
                    deleted_bytes += st.st_size
    except Exception as exc:
        print(f"{label}: {exc}", file=sys.stderr)
        return 1
    if not args.execute:
        if not args.quiet:
            print(f"{label}: would delete {deleted} files, {deleted_bytes} bytes")
        return 0
    if deleted == 0 and args.quiet:
        return 0
    size = cache_bytes(cache)
    if size is None:
        print(f"{label}: deleted {deleted} files, {deleted_bytes} bytes, cache unknown", file=sys.stderr)
        return 1
    if deleted or not args.quiet:
        print(f"{label}: deleted {deleted} files, {deleted_bytes} bytes, cache {size} bytes")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Trim Go and golangci-lint build caches older than 24 hours.")
    parser.add_argument("--execute", action="store_true", help="unlink; default is a dry run")
    parser.add_argument("--quiet", action="store_true", help="silent unless files were deleted or a command failed")
    args = parser.parse_args(argv)
    try:
        caches = [(cache_directory(), "go build cache", README_FIRST_LINE)]
        lint = lint_cache_directory()
        if os.path.lexists(lint):
            caches.append((lint, "golangci-lint cache", LINT_README_FIRST_LINE))
    except Exception as exc:
        print(f"build cache: {exc}", file=sys.stderr)
        return 1
    failed = False
    for cache, label, expected in caches:
        failed = trim_cache(cache, label, expected, args) != 0 or failed
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
