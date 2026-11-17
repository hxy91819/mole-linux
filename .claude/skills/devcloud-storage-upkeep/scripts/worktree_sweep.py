#!/usr/bin/env python3
"""Legacy helpers and regression API; CLI delegates to git_tree_sweep.py.

The historical main() below is retained for isolated regression fixtures.
Production CLI policy is the full-disk owner policy 2026-11-17.

A linked worktree is removed when nothing is using it and it has been idle
for --idle-hours. If its HEAD is not provably on a live remote branch, or it
has uncommitted changes, the exact state is first pushed to an
``archive/worktree/...`` branch and verified with ``git ls-remote``.

Default is a dry run that prints the plan. ``--execute`` performs it.
Main checkouts are never touched. Ignored rebuildable files are deleted with
the worktree. An ignored secret-like file keeps the worktree unless the main
checkout already has the same bytes at that path.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ENV = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", LC_ALL="C")
INTERNAL_HOSTS = {"git.woa.com"}
DEFAULT_LOGINS = ("hxy91819", "masonxhuang", "opencraft-app")
TMP_ALIASES = (("/tmp/", "/data/system-tmp/"),)
# Credential *files* by name. Source files that merely mention "credential" or "secret",
# and sops-encrypted manifests, are ordinary code and get archived like anything else.
SECRET_NAME = re.compile(
    r"(^|/)(\.env(\.[^/]*)?|id_(rsa|ed25519|ecdsa|dsa)(\.pub)?|[^/]*\.(pem|key|p12|pfx|kdbx|keystore|jks)"
    r"|\.npmrc|\.pypirc|\.netrc|\.git-credentials|kubeconfig[^/]*|credentials(\.(json|ya?ml|toml|ini))?"
    r"|secrets?\.(json|ya?ml|toml|env)|service[-_]account[^/]*\.json)$",
    re.I,
)
SECRET_TEMPLATE = re.compile(r"\.(example|sample|template|dist)$", re.I)
MAX_ARCHIVE_FILE = 20 * 2**20
IN_PROGRESS = ("rebase-merge", "rebase-apply", "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "BISECT_LOG")
BB_BUSY = {"active", "pending", "starting", "stopping"}
RESCAN_SECONDS = 120


class Keep(Exception):
    """The worktree stays; the message is the reason."""


class ScanIncomplete(Exception):
    """Process or docker evidence is incomplete; delete nothing this round."""


def run(cmd, cwd=None, timeout=300, check=True, env=None):
    r = subprocess.run(cmd, cwd=cwd, env=env or ENV, capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise subprocess.CalledProcessError(r.returncode, cmd, r.stdout, r.stderr)
    return r


def git(path, *args, timeout=300, check=True, env=None):
    return run(["git", "-C", str(path), *args], timeout=timeout, check=check, env=env)


def aliases(path: str):
    out = {path}
    for a, b in TMP_ALIASES:
        for x, y in ((a, b), (b, a)):
            if path.startswith(x):
                out.add(y + path[len(x):])
    return out


def add_anchor(anchors: dict, path: str, reason: str, include_self=True):
    """Record ``path`` and its ancestors, so a worktree containing it is found by lookup."""
    for p in aliases(os.path.normpath(path)):
        cur = p if include_self else os.path.dirname(p)
        while cur not in ("/", ""):
            anchors.setdefault(cur, reason)
            cur = os.path.dirname(cur)


def anchored(anchors: dict, path: str):
    for p in aliases(os.path.realpath(path)) | aliases(os.path.normpath(path)):
        if p in anchors:
            return anchors[p]
    return None


def _read_proc(path):
    """Read a /proc link. A gone process or a kernel thread without exe is normal;
    permission and other IO errors mean the scan cannot prove a worktree is idle."""
    try:
        return os.readlink(path)
    except (FileNotFoundError, ProcessLookupError):
        return None
    except OSError as exc:
        raise ScanIncomplete("process scan incomplete") from exc


def process_anchors(anchors: dict):
    me = str(os.getpid())
    skip = ("/proc", "/sys", "/dev", "/usr", "/lib", "/bin", "/sbin", "/etc", "/run")
    try:
        pids = os.listdir("/proc")
    except OSError as exc:
        raise ScanIncomplete("process scan incomplete") from exc
    for pid in pids:
        if not pid.isdigit() or pid == me:
            continue
        refs = []
        for link in ("cwd", "exe"):
            ref = _read_proc(f"/proc/{pid}/{link}")
            if ref:
                refs.append(ref)
        try:
            fds = os.listdir(f"/proc/{pid}/fd")
        except (FileNotFoundError, ProcessLookupError):
            fds = []
        except OSError as exc:
            raise ScanIncomplete("process scan incomplete") from exc
        for fd in fds:
            ref = _read_proc(f"/proc/{pid}/fd/{fd}")
            if ref:
                refs.append(ref)
        try:
            with open(f"/proc/{pid}/maps") as fh:
                for line in fh:
                    parts = line.split(None, 5)
                    if len(parts) == 6 and parts[5].startswith("/"):
                        refs.append(parts[5].strip())
        except (FileNotFoundError, ProcessLookupError):
            pass
        except OSError as exc:
            raise ScanIncomplete("process scan incomplete") from exc
        for ref in refs:
            ref = ref.removesuffix(" (deleted)")
            if ref.startswith("/") and not ref.startswith(skip):
                add_anchor(anchors, ref, f"process {pid}")


def docker_anchors(anchors: dict):
    if not shutil.which("docker"):
        return
    ps = run(["docker", "ps", "-aq"], timeout=60, check=False)
    if ps.returncode != 0:
        raise ScanIncomplete("process scan incomplete")
    ids = ps.stdout.split()
    if not ids:
        return
    fmt = "{{.Name}}{{range .Mounts}}\t{{.Source}}{{end}}"
    inspected = run(["docker", "inspect", "--format", fmt, *ids], timeout=120, check=False)
    if inspected.returncode != 0:
        raise ScanIncomplete("process scan incomplete")
    for line in inspected.stdout.splitlines():
        name, *sources = line.split("\t")
        for src in sources:
            if src.startswith("/"):
                add_anchor(anchors, src, f"container {name.lstrip('/')}")


def bb_anchors(anchors: dict, idle_seconds: float):
    now = time.time()
    threads = json.loads(run(["bb", "thread", "list", "--include-hidden", "--json"], timeout=120).stdout)
    for t in threads:
        path = t.get("environmentPath")
        if not path:
            continue
        activity = t.get("activity") or {}
        busy = (
            t.get("status") in BB_BUSY
            or (t.get("queuedWork") or "none") != "none"
            or t.get("hasPendingInteraction")
            or any(v for v in activity.values() if isinstance(v, int))
        )
        recent = t.get("archivedAt") is None and now - (t.get("updatedAt") or 0) / 1000 < idle_seconds
        if busy or recent:
            add_anchor(anchors, path, f"BB thread {t['id']} ({t.get('status')})")


def usage_anchors(idle_seconds: float, use_bb: bool):
    anchors = {}
    process_anchors(anchors)
    docker_anchors(anchors)
    if use_bb:
        bb_anchors(anchors, idle_seconds)
    return anchors


def discover_repos(roots, max_depth=3):
    """Main checkouts under ``roots`` keyed by their shared Git directory."""
    found = {}
    for root in roots:
        root = os.path.realpath(root)
        for dirpath, dirnames, _ in os.walk(root):
            dotgit = os.path.join(dirpath, ".git")
            if os.path.isdir(dotgit):
                try:
                    common = git(dirpath, "rev-parse", "--path-format=absolute", "--git-common-dir", timeout=30).stdout.strip()
                    found.setdefault(os.path.realpath(common), dirpath)
                except Exception:
                    pass
                dirnames[:] = []
            elif os.path.exists(dotgit) or dirpath[len(root):].count(os.sep) >= max_depth:
                dirnames[:] = []  # linked worktree, or deep enough
            else:
                dirnames[:] = [d for d in dirnames if not d.startswith(".") or d == ".worktrees"]
    return found


def list_worktrees(repo):
    out = git(repo, "worktree", "list", "--porcelain", timeout=60).stdout
    items, cur = [], {}
    for line in out.splitlines() + [""]:
        if not line:
            if cur:
                items.append(cur)
            cur = {}
            continue
        key, _, val = line.partition(" ")
        cur[key] = val if val else True
    return items


def parse_remote(url: str):
    m = re.match(r"^(?:ssh://)?(?:[^@/]+@)?([^:/]+)[:/](?:\d+/)?([^/]+)/", url) or re.match(
        r"^https?://(?:[^@/]+@)?([^/:]+)(?::\d+)?/([^/]+)/", url
    )
    return (m.group(1).lower(), m.group(2).lower()) if m else (None, None)


def transport_url(url: str):
    """GitHub goes over https: this host has gh's credential helper but no SSH agent for github.com."""
    m = re.match(r"^(?:ssh://)?git@github\.com[:/](.+?)(?:\.git)?/?$", url)
    return f"https://github.com/{m.group(1)}.git" if m else url


def archive_slug(path: str):
    """Unique per worktree: BB and ops trees reuse basenames such as weekly-report or mono2."""
    name = re.sub(r"[^A-Za-z0-9-]+", "-", Path(path).name).strip("-")[:40] or "worktree"
    return f"{name}-{hashlib.sha1(os.path.realpath(path).encode()).hexdigest()[:8]}"


class Repo:
    def __init__(self, main, logins):
        self.main = main
        self.common = git(main, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
        self.remotes = {}
        for name in git(main, "remote").stdout.split():
            fetch = git(main, "remote", "get-url", name, check=False).stdout.strip()
            push = git(main, "remote", "get-url", "--push", name, check=False).stdout.strip()
            self.remotes[name] = (fetch, push)
        self.live = {}  # remote -> {refname: sha}
        self.unreachable = set()
        self.by_sha = {}
        self._default = {}
        self.dest = self._archive_destination(logins)

    def _archive_destination(self, logins):
        """Your fork on the origin host, else an internal origin; public upstreams get nothing."""
        base = self.remotes.get("origin") or next(iter(self.remotes.values()), None)
        if not base:
            return None
        base_host, _ = parse_remote(base[0])
        for name, (_, push) in self.remotes.items():
            host, owner = parse_remote(push)
            if host and host == base_host and owner in logins:
                return name
        if (
            base_host in INTERNAL_HOSTS
            and "origin" in self.remotes
            and parse_remote(self.remotes["origin"][1])[0] == base_host
        ):
            return "origin"
        return None

    def load_live(self, include_tags=False):
        for name in self.remotes:
            try:
                out = git(self.main, "ls-remote", "--heads", *(["--tags"] if include_tags else []), transport_url(self.remotes[name][0]), timeout=90).stdout
            except Exception:
                self.unreachable.add(name)  # do not plan a push we already know cannot land
                continue
            refs = {ref: sha for sha, ref in (line.split("\t") for line in out.splitlines() if "\t" in line)}
            self.live[name] = refs
            for ref, sha in refs.items():
                self.by_sha.setdefault(sha, f"{name}:{ref}")

    def default_branch(self, name):
        """Default branch of remote ``name``: the local <name>/HEAD, else ask the remote.
        Repos created locally and pushed later never get refs/remotes/origin/HEAD."""
        if name not in self._default:
            sym = git(self.main, "symbolic-ref", "-q", f"refs/remotes/{name}/HEAD", check=False).stdout.strip()
            branch = sym.split("/", 3)[3] if sym.count("/") >= 3 else None
            if not branch:
                out = git(self.main, "ls-remote", "--symref", transport_url(self.remotes[name][0]), "HEAD",
                          timeout=90, check=False).stdout
                found = re.match(r"ref: refs/heads/(\S+)\tHEAD", out)
                branch = found.group(1) if found else None
            self._default[name] = branch
        return self._default[name]

    def has_commit(self, sha):
        return git(self.main, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0

    def proof(self, head):
        """'remote:ref' of a live remote branch containing HEAD, else None."""
        if head in self.by_sha:
            return self.by_sha[head]
        for name, refs in self.live.items():
            branch = self.default_branch(name)
            if not branch:
                continue
            ref = "refs/heads/" + branch
            sha = refs.get(ref)
            if sha and self.has_commit(sha) and git(self.main, "merge-base", "--is-ancestor", head, sha, check=False).returncode == 0:
                return f"{name}:{ref}"
        out = git(self.main, "for-each-ref", "--contains", head, "--format=%(refname) %(objectname)", "refs/remotes", check=False).stdout
        for line in out.splitlines():
            refname, sha = line.split()
            parts = refname.split("/", 3)
            if len(parts) == 4 and self.live.get(parts[2], {}).get("refs/heads/" + parts[3]) == sha:
                return f"{parts[2]}:refs/heads/{parts[3]}"
        return None


def status_paths(path):
    out = git(path, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignore-submodules=none", timeout=600).stdout
    paths, entries, i = [], out.split("\0"), 0
    while i < len(entries):
        e = entries[i]
        if len(e) > 3:
            paths.append(e[3:])
            if e[0] in "RC":
                i += 1  # skip the rename source
        i += 1
    return paths


def last_activity(path, gitdir, paths):
    stamps = []
    for name in ("HEAD", "index", "logs/HEAD"):
        try:
            stamps.append(os.stat(os.path.join(gitdir, name)).st_mtime)
        except OSError:
            pass
    for p in [path] + [os.path.join(path, x) for x in paths]:
        try:
            st = os.lstat(p)
            stamps.append(max(st.st_mtime, st.st_ctime) if p != path else st.st_mtime)
        except OSError:
            pass
    return max(stamps) if stamps else 0


def populated_submodule(path):
    for entry in git(path, "ls-files", "--stage", "-z", timeout=300).stdout.split("\0"):
        if entry.startswith("160000 "):
            name = entry.split("\t", 1)[1]
            child = os.path.join(path, name)
            if os.path.isdir(child) and os.listdir(child):
                return name
    return None


def risky_files(path, paths):
    risky = []
    for p in paths:
        try:
            st = os.lstat(os.path.join(path, p))
        except OSError:
            continue  # already gone; deleting a tracked file pushes no content
        if SECRET_NAME.search(p) and not SECRET_TEMPLATE.search(p):
            risky.append(f"secret-like file {p}")
            continue
        if st.st_size > MAX_ARCHIVE_FILE:
            risky.append(f"large file {p} ({st.st_size >> 20} MiB)")
    return risky


def _regular_file(path):
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def _same_bytes(left, right):
    try:
        if os.lstat(left).st_size != os.lstat(right).st_size:
            return False
        with open(left, "rb") as fa, open(right, "rb") as fb:
            while True:
                a, b = fa.read(1024 * 1024), fb.read(1024 * 1024)
                if a != b:
                    return False
                if not a:
                    return True
    except OSError:
        return False


def _skip_ignored_dir(full, name):
    if name == "node_modules" or os.path.islink(full):
        return True
    return os.path.isfile(os.path.join(full, "pyvenv.cfg"))


def _walk_ignored_files(root, rel_dir):
    base = os.path.join(root, rel_dir)
    if _skip_ignored_dir(base, os.path.basename(rel_dir)) or not os.path.isdir(base):
        return
    for dirpath, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = [name for name in dirnames if not _skip_ignored_dir(os.path.join(dirpath, name), name)]
        for name in filenames:
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            yield os.path.relpath(full, root)


def _ignored_secret_reason(repo, worktree, rel):
    if not (SECRET_NAME.search(rel) and not SECRET_TEMPLATE.search(rel)):
        return None
    full = os.path.join(worktree, rel)
    if not _regular_file(full):
        return None
    main_file = os.path.join(repo.main, rel)
    if not _regular_file(main_file) or not _same_bytes(full, main_file):
        return f"ignored secret-like file not in main checkout: {rel}"
    return None


def ignored_secret_block(repo, worktree):
    """Keep the worktree when an ignored credential file is not already in the main checkout.

    ``git status`` hides files matched by the global gitignore, and removing the
    worktree would delete the only copy. node_modules and virtualenvs are skipped:
    those trees are rebuildable and routinely contain template env files.
    """
    out = git(
        worktree, "ls-files", "-z", "--others", "--ignored", "--exclude-standard", "--directory", timeout=300
    ).stdout
    for entry in out.split("\0"):
        if not entry:
            continue
        collapsed = entry.endswith("/")
        rel = entry[:-1] if collapsed else entry
        full = os.path.join(worktree, rel)
        if collapsed or (not os.path.islink(full) and os.path.isdir(full)):
            for child in _walk_ignored_files(worktree, rel):
                reason = _ignored_secret_reason(repo, worktree, child)
                if reason:
                    return reason
            continue
        reason = _ignored_secret_reason(repo, worktree, rel)
        if reason:
            return reason
    return None


def snapshot_tree(path, gitdir):
    """Tree of the working directory as ``git add -A`` sees it; the real index is untouched."""
    with tempfile.TemporaryDirectory(prefix="wt-sweep-", dir="/var/tmp") as tmp:
        index = os.path.join(tmp, "index")
        env = dict(ENV, GIT_INDEX_FILE=index)
        real = os.path.join(gitdir, "index")
        if os.path.exists(real):
            shutil.copy2(real, index)  # keeps stat data, so unchanged files are not re-hashed
        else:
            git(path, "read-tree", "HEAD", env=env)
        git(path, "add", "-A", env=env, timeout=1800)
        return git(path, "write-tree", env=env).stdout.strip()


def plan_worktree(repo: Repo, wt: dict, anchors, nested, idle_seconds, now, scan_error=None):
    path = wt["worktree"]
    item = {"repo": repo.main, "path": path, "branch": wt.get("branch", "(detached)"), "head": wt.get("HEAD")}
    try:
        if scan_error:
            raise Keep(scan_error)
        if wt.get("bare"):
            raise Keep("bare")
        if "locked" in wt:
            raise Keep("locked")
        if "prunable" in wt or not os.path.isdir(path):
            raise Keep("missing directory")
        holder = anchored(anchors, path)
        if holder:
            raise Keep(f"in use: {holder}")
        inner = anchored(nested, path)
        if inner:
            raise Keep(f"contains another checkout: {inner}")
        gitdir = git(path, "rev-parse", "--path-format=absolute", "--git-dir").stdout.strip()
        item["gitdir"] = gitdir
        for name in IN_PROGRESS:
            if os.path.exists(os.path.join(gitdir, name)):
                raise Keep(f"git operation in progress: {name}")
        meta_idle = now - last_activity(path, gitdir, [])
        if meta_idle < idle_seconds:
            item["idle_hours"] = round(meta_idle / 3600, 1)
            raise Keep(f"active within {idle_seconds / 3600:g}h")
        paths = status_paths(path)
        idle = now - last_activity(path, gitdir, paths)
        item["idle_hours"] = round(idle / 3600, 1)
        if idle < idle_seconds:
            raise Keep(f"active within {idle_seconds / 3600:g}h")
        sub = populated_submodule(path)
        if sub:
            raise Keep(f"populated submodule: {sub}")
        item["dirty"] = bool(paths)
        item["proof"] = None if paths else repo.proof(item["head"])
        secret = ignored_secret_block(repo, path)
        if secret:
            raise Keep(secret)
        if item["proof"]:
            item["action"] = "remove"
            return item
        if not repo.dest:
            raise Keep("unpushed work and no personal or internal remote")
        if repo.dest in repo.unreachable:
            raise Keep(f"archive destination unreachable: {repo.dest}")
        risky = risky_files(path, paths)
        if risky:
            raise Keep("archive would push risky files: " + "; ".join(risky[:3]))
        item["action"] = "archive+remove"
    except Keep as k:
        item.update(action="keep", reason=str(k))
    except Exception as exc:
        item.update(action="keep", reason=f"check failed: {str(getattr(exc, 'stderr', '') or exc).strip()[:160]}")
    return item


def archive_identity(repo: Repo):
    """Committer for WIP snapshots. git.woa.com's committer-check rejects anything but the
    account's own address, and repos here may set a personal GitHub identity locally
    (mono2: hxy91819 <…@proton.me>), so internal pushes use the global identity."""
    host, _ = parse_remote(repo.remotes[repo.dest][1])
    if host not in INTERNAL_HOSTS:
        return {}
    name = run(["git", "config", "--global", "user.name"], check=False).stdout.strip()
    email = run(["git", "config", "--global", "user.email"], check=False).stdout.strip()
    if not (name and email):
        return {}
    return {"GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email, "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}


def published_base(repo: Repo, head: str):
    """Newest ancestor of HEAD that the archive remote's default branch already has."""
    branch = repo.default_branch(repo.dest)
    if not branch:
        return None
    live = repo.live.get(repo.dest, {}).get("refs/heads/" + branch)
    tracking = f"refs/remotes/{repo.dest}/{branch}"
    candidates = [live] if live and repo.has_commit(live) else []
    if git(repo.main, "rev-parse", "--verify", "-q", tracking, check=False).returncode == 0:
        candidates.append(tracking)
    for candidate in candidates:
        r = git(repo.main, "merge-base", head, candidate, check=False)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    return None


def push_archive(repo: Repo, commit: str, ref: str, url: str, name: str, stamp: str):
    """Push ``commit`` to ``ref``; return the commit the remote now holds.

    git.woa.com refuses already-made local commits whose committer is a personal identity.
    Those are pushed as one snapshot of the same tree, committed with the account identity
    on top of the published base; the exact commits stay on the local archive ref."""
    push = git(repo.main, "push", url, f"{commit}:{ref}", timeout=900, check=False)
    if push.returncode != 0 and "committer-check" in push.stderr and archive_identity(repo):
        base = published_base(repo, commit)
        if not base:
            raise Keep("committer-check rejected local commits and no published base to squash onto")
        tree = git(repo.main, "rev-parse", f"{commit}^{{tree}}").stdout.strip()
        commit = git(repo.main, "commit-tree", tree, "-p", base, "-m",
                     f"chore: archive worktree {name} as one snapshot before cleanup ({stamp})",
                     env=dict(ENV, **archive_identity(repo))).stdout.strip()
        push = git(repo.main, "push", url, f"{commit}:{ref}", timeout=900, check=False)
    if push.returncode != 0:
        raise subprocess.CalledProcessError(push.returncode, ["git", "push"], push.stdout, push.stderr)
    got = git(repo.main, "ls-remote", url, ref, timeout=120).stdout.split()
    if not got or got[0] != commit:
        raise Keep("pushed archive ref does not match")
    return commit


def recovery_command_for(repo: Repo, item: dict):
    path = item["path"]
    rec = item.get("recovery")
    if rec:
        url, ref, sha = rec["url"], rec["ref"], rec["sha"]
    else:
        proof = item.get("proof") or ""
        remote_name, _, ref = proof.partition(":")
        fetch_url, _ = repo.remotes.get(remote_name, ("", ""))
        url = transport_url(fetch_url) if fetch_url else remote_name
        sha = item["head"]
    return f"git fetch {shlex.quote(url)} {shlex.quote(ref)} && git worktree add {shlex.quote(path)} {shlex.quote(sha)}"


class BBEnvironmentCleaner:
    """Best-effort BB environment cleanup after a worktree has been removed.

    ``bb environment delete <id>`` only cleans up the environment record/attempt
    files (``removeWorktree`` returns immediately when the target path is already
    gone and never deletes Git branches or refs), and refuses with 409 when live
    threads remain. Any BB query or command error fails open after logging."""

    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._lock = threading.Lock()
        self._loaded = False
        self._by_path = {}
        self._load_error = None

    def _ensure_loaded(self):
        if self._loaded:
            return
        with self._lock:
            if self._loaded:
                return
            try:
                res = run(["bb", "environment", "list", "--json"], timeout=120, check=False)
                if res.returncode != 0:
                    self._load_error = failure_text(res.stderr or res.stdout or f"bb environment list exited {res.returncode}")
                else:
                    envs = json.loads(res.stdout)
                    by_path = {}
                    for env in envs:
                        if not isinstance(env, dict) or env.get("status") == "destroyed":
                            continue
                        env_path = env.get("path")
                        env_id = env.get("id")
                        if isinstance(env_path, str) and env_path and isinstance(env_id, str) and env_id:
                            by_path.setdefault(env_path, []).append(env)
                    self._by_path = by_path
            except Exception as exc:
                self._load_error = failure_text(exc)
            self._loaded = True

    def process_removed_worktree(self, item: dict):
        if not self.enabled:
            return
        self._ensure_loaded()
        if self._load_error is not None:
            item["bb_environment_error"] = self._load_error
            return
        with self._lock:
            matched = list(self._by_path.pop(item["path"], ()))
        outcomes = []
        for env in matched:
            env_id = env["id"]
            try:
                res = run(["bb", "environment", "delete", env_id, "--json"], timeout=120, check=False)
            except Exception as exc:
                outcomes.append({"id": env_id, "action": "delete", "result": "failed", "reason": failure_text(exc)})
                continue
            if res.returncode == 0:
                outcomes.append({"id": env_id, "action": "delete", "result": "deleted"})
                continue
            reason = failure_text(res.stderr or res.stdout or f"exit {res.returncode}")
            low = reason.lower()
            refused = "live thread" in low or "cannot be deleted while" in low
            outcomes.append({
                "id": env_id,
                "action": "delete",
                "result": "refused" if refused else "failed",
                "reason": reason,
            })
        item["bb_environments"] = outcomes

    def process_missing(self):
        """Retry missing environment records once per run, never within a run."""
        if not self.enabled:
            return []
        self._ensure_loaded()
        if self._load_error:
            return [{"action": "bb-environment-sweep", "bb_environment_error": self._load_error}]
        rows = []
        for path in list(self._by_path):
            try:
                os.lstat(path)  # lexists, so a dangling symlink is NOT missing
            except FileNotFoundError:
                item = {"action": "bb-environment-sweep", "path": path}
                self.process_removed_worktree(item)
                rows.append(item)
            except OSError as exc:
                rows.append({"action": "bb-environment-sweep", "path": path, "bb_environment_error": failure_text(exc)})
        return rows


def execute_item(repo: Repo, item: dict, stamp: str, bb_envs: BBEnvironmentCleaner | None = None):
    path, head = item["path"], item["head"]
    secret = ignored_secret_block(repo, path)
    if secret:
        raise Keep(secret)
    tree = None
    if item["action"] == "archive+remove":
        commit = head
        if item["dirty"]:
            tree = snapshot_tree(path, item["gitdir"])
            commit = git(path, "commit-tree", tree, "-p", head, "-m",
                         f"chore: archive worktree {Path(path).name} before cleanup ({stamp})",
                         env=dict(ENV, **archive_identity(repo))).stdout.strip()
        ref = f"refs/heads/archive/worktree/{archive_slug(path)}-{stamp}"
        url = transport_url(repo.remotes[repo.dest][1])
        remote_sha = push_archive(repo, commit, ref, url, Path(path).name, stamp)
        git(repo.main, "update-ref", ref, commit)  # the local ref keeps the exact commits
        item["recovery"] = {"remote": repo.dest, "url": url, "ref": ref, "sha": remote_sha, "local_sha": commit}
    if tree:
        if snapshot_tree(path, item["gitdir"]) != tree:
            raise Keep("changed after archive; archive ref kept")
        git(repo.main, "worktree", "remove", "--force", path, timeout=3600)
    else:
        git(repo.main, "worktree", "remove", path, timeout=3600)  # refuses if it became dirty
    item["result"] = "removed"
    item["recovery_command"] = recovery_command_for(repo, item)
    if bb_envs is not None:
        bb_envs.process_removed_worktree(item)


def sweep_repo(repo: Repo, items, stamp, state_log, idle_seconds, use_bb, deadline, bb_envs=None):
    with open(os.path.join(repo.common, "worktree-cleanup.lock"), "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)  # same lock as the worktree-cleanup tool
        except BlockingIOError:
            for it in items:
                it.update(result="kept", reason="repo cleanup lock busy")
            return
        anchors, scanned = {}, 0.0
        for it in items:
            if deadline is not None and time.time() >= deadline:
                it.update(result="kept", reason="time budget reached")
                with open(state_log, "a") as fh:
                    fh.write(json.dumps(it, ensure_ascii=False) + "\n")
                continue
            try:
                if time.time() - scanned > RESCAN_SECONDS:
                    anchors, scanned = usage_anchors(idle_seconds, use_bb), time.time()
                holder = anchored(anchors, it["path"])
                if holder:
                    raise Keep(f"in use: {holder}")
                execute_item(repo, it, stamp, bb_envs)
            except ScanIncomplete:
                it.update(result="kept", reason="process scan incomplete")
            except Keep as k:
                it.update(result="kept", reason=str(k))
            except Exception as exc:
                it.update(result="failed", reason=failure_text(exc))
            with open(state_log, "a") as fh:
                fh.write(json.dumps(it, ensure_ascii=False) + "\n")


def failure_text(exc):
    """The lines that say why: no ANSI colour, no server progress, no MR-link banners."""
    raw = str(getattr(exc, "stderr", "") or exc)
    raw = re.sub(r"\x1b\[[0-9;]*m", "", raw).replace("\r", "\n")
    noise = re.compile(r"^(remote:\s*)?(Processing pre-receive|\+-+\+|\|.*\||To create a merge request|$)")
    lines = [line.strip() for line in raw.splitlines() if not noise.match(line.strip())]
    return " | ".join(lines)[:1500] or raw.strip()[:300]


def measure(items):
    def du(it):
        try:
            r = run(["du", "-sx", "-B1", it["path"]], timeout=1800, check=False)
            it["bytes"] = int(r.stdout.split()[0]) if r.returncode == 0 and r.stdout.strip() else None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            it["bytes"] = None

    with cf.ThreadPoolExecutor(8) as pool:
        list(pool.map(du, items))


def reason_key(reason: str):
    """Group reasons by their category; details after ':' are per item."""
    return reason.split(":", 1)[0].strip()[:80]


def repo_summary(repo, items, measured):
    counts = {}
    for i in items:
        outcome = i.get("result") or i["action"]
        if outcome in ("keep", "kept", "failed"):
            outcome = f"{outcome}: {reason_key(i['reason'])}"
        counts[outcome] = counts.get(outcome, 0) + 1
    sized = ""
    if measured:
        # du 失败的项 bytes 为 None：不算 0 GiB，只计入 unmeasured。
        total = sum(i.get("bytes") for i in items if i.get("bytes") is not None)
        to_free = sum(i.get("bytes") for i in items
                      if i.get("bytes") is not None and i["action"] != "keep")
        unmeasured = sum(1 for i in items if i.get("bytes") is None)
        sized = f" [{total / 2**30:.1f} GiB, {to_free / 2**30:.1f} GiB to free]"
        if unmeasured:
            sized += f", {unmeasured} unmeasured"
    detail = ", ".join(f"{k} ×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
    return f"{os.path.basename(repo.main)}{sized}: {detail}"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Archive and remove idle linked Git worktrees.")
    ap.add_argument("--root", action="append", help="directory holding main checkouts (default /data/code)")
    ap.add_argument("--repo", action="append", help="only these main checkouts")
    ap.add_argument("--idle-hours", type=float, default=24)
    ap.add_argument("--login", action="append",
                    help="remote namespaces that are yours (default: hxy91819, masonxhuang, opencraft-app)")
    ap.add_argument("--state-dir", default=os.path.expanduser("~/.local/state/devcloud-storage-upkeep"))
    ap.add_argument("--no-bb", action="store_true", help="skip the BB thread check (isolated tests only)")
    ap.add_argument("--measure", action="store_true", help="du every linked worktree before acting")
    ap.add_argument("--execute", action="store_true", help="archive and remove; default is a dry run")
    ap.add_argument("--budget-minutes", type=float, default=0,
                    help="stop starting archives and deletions after this many minutes (0 means no limit)")
    ap.add_argument("--quiet", action="store_true",
                    help="print nothing unless a worktree was removed or a removal failed")
    ap.add_argument("--json", action="store_true", help="print every item as JSON")
    args = ap.parse_args(argv)

    started = time.time()
    logins = {x.lower() for x in (args.login or DEFAULT_LOGINS)}
    now, idle_seconds, use_bb = started, args.idle_hours * 3600, not args.no_bb
    deadline = started + args.budget_minutes * 60 if args.budget_minutes > 0 else None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    if args.repo:
        repos = {}
        for r in args.repo:
            common = git(r, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
            repos.setdefault(os.path.realpath(common), os.path.realpath(r))
    else:
        repos = discover_repos(args.root or ["/data/code"])

    try:
        anchors = usage_anchors(idle_seconds, use_bb)
        scan_error = None
    except ScanIncomplete:
        anchors, scan_error = {}, "process scan incomplete"
    repo_objs, worktrees, nested = {}, {}, {}
    for common, main_path in repos.items():
        try:
            repo, wts = Repo(main_path, logins), list_worktrees(main_path)
        except Exception as exc:
            print(f"skip {main_path}: {exc}", file=sys.stderr)
            continue
        repo_objs[common], worktrees[common] = repo, wts[1:]  # entry 0 is the main checkout
        for w in wts:
            add_anchor(nested, w["worktree"], f"checkout {w['worktree']}", include_self=False)

    with cf.ThreadPoolExecutor(8) as pool:
        list(pool.map(lambda r: r.load_live(), repo_objs.values()))
        futures = {
            pool.submit(plan_worktree, repo_objs[c], wt, anchors, nested, idle_seconds, now, scan_error): c
            for c, wts in worktrees.items()
            for wt in wts
        }
        plan = {}
        for fut in cf.as_completed(futures):
            plan.setdefault(futures[fut], []).append(fut.result())

    todo = {c: [i for i in items if i["action"] != "keep"] for c, items in plan.items()}
    if args.measure:
        measure([i for items in plan.values() for i in items])
    os.makedirs(args.state_dir, exist_ok=True)
    state_log = os.path.join(args.state_dir, f"worktree-sweep-{stamp}.jsonl")
    if args.execute:
        bb_envs = BBEnvironmentCleaner(use_bb)
        with cf.ThreadPoolExecutor(4) as pool:
            list(pool.map(
                lambda c: sweep_repo(repo_objs[c], todo[c], stamp, state_log, idle_seconds, use_bb, deadline, bb_envs),
                [c for c in todo if todo[c]],
            ))

    environment_rows = []
    if args.execute:
        environment_rows = bb_envs.process_missing()
        with open(state_log, "a") as fh:
            for row in environment_rows:
                fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    rows = [i for items in plan.values() for i in items]
    failed = any(i.get("result") == "failed" for i in rows)
    order = sorted(plan, key=lambda c: -len(plan[c]))
    if args.quiet:
        if scan_error:
            print(scan_error)
        acted = False
        for c in order:
            if any(i.get("result") in ("removed", "failed") for i in plan[c]):
                print(repo_summary(repo_objs[c], plan[c], args.measure))
                acted = True
        deleted_envs = sum(e["result"] == "deleted" for row in environment_rows for e in row.get("bb_environments", []))
        if deleted_envs:
            print(f"missing BB environments deleted: {deleted_envs}")
            acted = True
        if acted and args.execute:
            print(f"log: {state_log}")
        return 1 if failed else 0
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    for c in order:
        print(repo_summary(repo_objs[c], plan[c], args.measure))
    print(f"log: {state_log}" if args.execute else "dry run: nothing changed (add --execute)")
    return 1 if failed else 0


if __name__ == "__main__":
    entry = Path(__file__).with_name('git_tree_sweep.py')
    namespace = {'__name__': 'git_tree_sweep_cli', '__file__': str(entry)}
    exec(compile(entry.read_bytes(), str(entry), 'exec'), namespace)
    sys.exit(namespace['main']())
