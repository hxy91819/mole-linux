#!/usr/bin/env python3
"""Worktree 巡检的确定性执行器：采集 → 增量指纹 → 判定 → （可选）执行 → 预聚合输出。

设计目标：把可脚本化的部分全部脚本化，让调用它的 Agent 只读一段紧凑输出并写判断，
不再每天重复测量没变过的对象。

三层数据：
  1. 每轮必查（便宜、权威）：`git worktree list --porcelain`（增删/HEAD/branch/locked/prunable）、
     age 与 168h 门槛、owner 工具的远端核对（PR merged / 远端分支 tip / 含于主干）。
  2. 指纹缓存：HEAD/branch/locked/prunable/dirty/ignored 根集合/.local 指纹不变时，
     复用上次的 du 体积与「上次活跃」时间（一次 `du -sbx --time` 同时取两者）、artifact
     体积与 who-uses 引用；报告里标注"上次测得"。
  3. 不做目录内容 hash：对几十 GiB 的树做 sha256 比 du 贵一个数量级，且决策信号已被指纹覆盖。

缓存绝不参与删除授权：`--execute` 时仍由 owner 工具按指纹在 sink 前复验。

只读是默认；`--execute` 才调用 owner 工具的 `--apply --approve-report`。
禁止 --force / rm -rf / git clean / 删分支 —— 本脚本不提供这些路径。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------- 常量

DEVCLOUD = "/data/tools/devcloud-maintenance/releases/cf2bbbefdbdee7af/bin/devcloud.py"
WORKTREE_TOOL = "/root/.agents/skills/worktree-cleanup/scripts/cleanup_worktrees.py"
LEDGER = "/data/code/mole-linux/storage-audit.log"
METRICS_ROOT = "/data/metrics/ai-storage-maintenance"
STATE_DIR = os.path.join(METRICS_ROOT, "worktree-state")
DEFAULT_ROOTS = ["/data/code"]
MIN_AGE_HOURS = 168.0
# du --time-style 的时间列形状；不匹配就当「不可得」，绝不把别的字段当时间。
LAST_ACTIVE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}$")
# owner 维护的工蜂凭据（root 600）。只读 token 值本身，绝不复制、不回显。
GONGFENG_ENV_FILE = "/data/code/lighthouse/developer/.env"

# Mole 的 MOLE_PURGE_TARGETS 白名单（与 lib/clean/purge_shared.sh 对齐）。
# `bin` 会被 is_protected_purge_artifact 拦，但体积照测。
ARTIFACTS = [
    "node_modules", "target", "build", "dist", "venv", ".venv", ".pytest_cache",
    ".mypy_cache", ".tox", ".nox", ".ruff_cache", ".gradle", ".terragrunt-cache",
    "__pycache__", ".next", ".nuxt", ".output", "vendor", "bin", "obj", ".turbo",
    ".parcel-cache", ".dart_tool", ".zig-cache", "zig-out", ".angular",
    ".svelte-kit", ".astro", "coverage", "DerivedData", "Pods", ".cxx", ".expo",
    ".build",
]
ARTIFACT_MAX_DEPTH = 12
ARTIFACT_DEADLINE = 45  # 秒/根


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def run(cmd, timeout=120, cwd=None, env=None):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           env=env)
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except OSError as exc:
        return 127, "", str(exc)


def fmt_bytes(b):
    if b is None:
        return "—"
    for unit, div in (("GiB", 1024 ** 3), ("MiB", 1024 ** 2), ("KiB", 1024)):
        if b >= div:
            return f"{b / div:.2f} {unit}"
    return f"{b} B"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest()


# ---------------------------------------------------------------- 采集

def discover_repos(root: str) -> list[str]:
    """有 .git 的 checkout，按 git-common-dir 去重（排除 submodule 与嵌套根）。"""
    rc, out, _ = run(["find", root, "-maxdepth", "4", "-name", ".git"], timeout=180)
    if rc != 0:
        return []
    seen: dict[str, str] = {}
    for line in out.splitlines():
        checkout = os.path.dirname(line)
        if ".git" in checkout.split(os.sep):
            continue
        rc2, sup, _ = run(["git", "-C", checkout, "rev-parse",
                           "--show-superproject-working-tree"], timeout=30)
        if rc2 == 0 and sup.strip():
            continue
        rc3, common, _ = run(["git", "-C", checkout, "rev-parse", "--path-format=absolute",
                              "--git-common-dir"], timeout=30)
        if rc3 != 0 or not common.strip():
            continue
        key = common.strip()
        prev = seen.get(key)
        if prev is None or (os.path.isdir(line) and not os.path.isdir(os.path.join(prev, ".git"))):
            seen[key] = checkout
    return sorted(seen.values())


def worktree_entries(repo: str) -> list[dict]:
    rc, out, _ = run(["git", "-C", repo, "worktree", "list", "--porcelain"], timeout=60)
    if rc != 0:
        return []
    entries, cur = [], {}
    for line in [*out.splitlines(), ""]:
        if not line.strip():
            if cur:
                entries.append(cur)
                cur = {}
            continue
        k, _, v = line.partition(" ")
        if k == "worktree":
            cur["path"] = v
        elif k == "HEAD":
            cur["head"] = v
        elif k == "branch":
            cur["branch"] = v.replace("refs/heads/", "")
        elif k == "detached":
            cur["branch"] = "(detached)"
        elif k in ("locked", "prunable"):
            cur[k] = v or True
    return entries


def git_ignored(path: str, timeout=90):
    rc, out, _ = run(["git", "-C", path, "ls-files", "--others", "--ignored",
                      "--exclude-standard", "--directory", "-z"], timeout=timeout)
    if rc != 0:
        return None, "git ls-files --ignored failed"
    roots = sorted(x for x in out.split("\0") if x)
    local = os.path.join(path, ".local")
    local_tree = None
    if os.path.isdir(local):
        try:
            st = os.stat(local)
            local_tree = {"mtime": int(st.st_mtime), "entries": len(os.listdir(local))}
        except OSError:
            local_tree = None
    return {"count": len(roots), "digest": sha("\0".join(roots)), "sample": roots[:20],
            "local_tree": local_tree}, None


def fingerprint(entry: dict, dirty, ignored) -> str:
    value = {
        "path": entry["path"], "head": entry.get("head"), "branch": entry.get("branch"),
        "detached": entry.get("branch") == "(detached)",
        "locked": entry.get("locked"), "prunable": entry.get("prunable"),
        "dirty": dirty,
        "ignored_count": ignored["count"] if ignored else None,
        "ignored_digest": ignored["digest"] if ignored else None,
        "local_tree": ignored["local_tree"] if ignored else None,
    }
    return sha(json.dumps(value, sort_keys=True, separators=(",", ":")))


def worktree_age_hours(path: str):
    try:
        return max(0.0, (time.time() - os.stat(os.path.join(path, ".git")).st_mtime) / 3600)
    except OSError:
        return None


def du_bytes(path: str, timeout=120):
    rc, out, _ = run(["du", "-sbx", "--", path], timeout=timeout)
    if rc != 0 or not out:
        return None
    try:
        return int(out.split("\t", 1)[0])
    except ValueError:
        return None


def summarize_dirty(porcelain: str, shortstat: str = ""):
    """把 `git status --porcelain` 压成可判断的一行摘要，供 owner 快速决策。

    只统计数量与 diffstat，不读文件内容：目的是把「dirty 12 项」从「要逐项读代码」
    降成「看一眼就知道有多少人工改动」。
    """
    lines = [l for l in porcelain.splitlines() if l.strip()]
    if not lines:
        return None
    tracked = sum(1 for l in lines if not l.startswith("??"))
    untracked = sum(1 for l in lines if l.startswith("??"))
    m = re.search(r"(\d+) files? changed(?:, (\d+) insertions?\(\+\))?"
                  r"(?:, (\d+) deletions?\(-\))?", shortstat or "")
    return {
        "tracked": tracked,
        "untracked": untracked,
        "files_changed": int(m.group(1)) if m else None,
        "insertions": int(m.group(2)) if m and m.group(2) else 0,
        "deletions": int(m.group(3)) if m and m.group(3) else 0,
    }


def compact_dirty(summary) -> str:
    if not summary:
        return ""
    bits = []
    if summary.get("files_changed") is not None:
        bits.append(f"{summary['files_changed']}F +{summary['insertions']}/-{summary['deletions']}")
    if summary.get("tracked"):
        bits.append(f"{summary['tracked']} tracked")
    if summary.get("untracked"):
        bits.append(f"{summary['untracked']} untracked")
    return " [" + " ".join(bits) + "]" if bits else ""


def du_bytes_last_active(path: str, timeout=180):
    """一次 du 同时取表观字节与「树内最新 mtime」（上次活跃），不额外遍历。

    口径：GNU `du -sbx --time` 在 `-s` 汇总行给出树内最新 mtime（含目录）。
    取不到或时间列不是预期形状时返回 None，报告写「不可得」而不是猜。
    """
    rc, out, _ = run(["du", "-sbx", "--time", "--time-style=+%Y-%m-%dT%H:%M:%S%z",
                      "--", path], timeout=timeout)
    if rc != 0 or not out:
        return None, None
    parts = out.split("\t")
    try:
        size = int(parts[0])
    except (IndexError, ValueError):
        return None, None
    when = None
    if len(parts) > 1 and LAST_ACTIVE_RE.match(parts[1].strip()):
        when = parts[1].strip()
    return size, when


def artifact_bytes(root: str, exclude_roots=()):
    total, hits = 0, []
    ex = [os.path.abspath(e) for e in exclude_roots]
    started = time.time()
    base_depth = root.rstrip("/").count("/")

    def excluded(p):
        ap = os.path.abspath(p)
        return any(ap == e or ap.startswith(e + os.sep) for e in ex)

    for dirpath, dirnames, _ in os.walk(root, followlinks=False):
        if time.time() - started > ARTIFACT_DEADLINE:
            break
        if dirpath.rstrip("/").count("/") - base_depth >= ARTIFACT_MAX_DEPTH:
            dirnames[:] = []
            continue
        if ".git" in dirnames:
            dirnames.remove(".git")
        keep = []
        for name in dirnames:
            full = os.path.join(dirpath, name)
            if name in ARTIFACTS and not os.path.islink(full) and not excluded(full):
                b = du_bytes(full, timeout=60)
                if b:
                    total += b
                    hits.append({"p": full, "kb": b // 1024})
            else:
                keep.append(name)
        dirnames[:] = keep
    return total, hits


# ---------------------------------------------------------------- 远端证据

def df_pct(mount: str):
    rc, out, _ = run(["df", "-P", mount], timeout=30)
    if rc != 0:
        return None
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 5:
            try:
                return float(parts[4].rstrip("%"))
            except ValueError:
                return None
    return None


def remote_slug(repo: str):
    rc, out, _ = run(["git", "-C", repo, "remote", "get-url", "origin"], timeout=30)
    if rc != 0 or not out.strip():
        return None, None
    url = out.strip()
    m = re.search(r"github\.com[:/]+([^/]+)/([^/]+?)(?:\.git)?$", url)
    if m:
        return f"{m.group(1)}/{m.group(2)}", "github.com"
    m2 = re.search(r"(?:@|//)([^/:]+)[:/](.+?)(?:\.git)?$", url)
    if m2:
        return m2.group(2), m2.group(1)
    return None, "unknown"


def gongfeng_bin():
    return shutil.which("gongfengcli") or "/root/.nvm/versions/node/v24.15.0/bin/gongfengcli"


def gongfeng_token():
    """解析工蜂 token，返回 (token, source)；token 值绝不写入任何产物或日志。

    顺序：已导出的环境变量 → owner 维护的凭据文件。凭据文件要求 root 所有且
    组/其他不可读，否则拒绝并报「凭据文件权限不安全」而不是照读。
    """
    value = os.environ.get("GONGFENG_PRIVATE_TOKEN")
    if value and value.strip():
        return value.strip(), "env:GONGFENG_PRIVATE_TOKEN"
    path = GONGFENG_ENV_FILE
    try:
        info = os.stat(path)
    except OSError:
        return None, f"凭据文件缺失：{path}"
    if info.st_uid != 0 or info.st_mode & 0o077:
        return None, f"凭据文件权限不安全（需 root 且 600）：{path}"
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                key, sep, raw = line.partition("=")
                if sep and key.strip() == "GONGFENG_PRIVATE_TOKEN":
                    token = raw.strip().strip('"').strip("'")
                    if token:
                        return token, f"file:{path}"
    except OSError:
        return None, f"凭据文件不可读：{path}"
    return None, f"凭据文件中没有 GONGFENG_PRIVATE_TOKEN：{path}"


def gongfeng_lookup(project, branch, token, timeout=60):
    """按源分支查工蜂 MR：merged → opened → closed。

    注意 `--state all` 在 0.1.0 上恒返回 0 条（实测），必须逐状态查询。
    """
    if not project or not branch or branch == "(detached)":
        return {"state": "unknown", "reason": "无分支名，无法按源分支查询 MR"}
    env = os.environ.copy()
    env["GONGFENG_PRIVATE_TOKEN"] = token
    binary = gongfeng_bin()
    for state in ("merged", "opened", "closed"):
        rc, out, err = run([binary, "mr", "list", project, "--state", state,
                            "--source-branch", branch, "--per-page", "20", "--json"],
                           timeout=timeout, env=env)
        if rc != 0:
            return {"state": "error", "reason": f"gongfengcli 退出 {rc}"}
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            return {"state": "error", "reason": "gongfengcli 输出不是 JSON"}
        if payload.get("ok") is False:
            message = str((payload.get("error") or {}).get("message") or "")[:160]
            return {"state": "error", "reason": f"工蜂接口拒绝：{message}"}
        for mr in payload.get("result") or []:
            if mr.get("source_branch") != branch:
                continue
            return {
                "state": str(mr.get("state") or state),
                "iid": mr.get("iid"),
                "title": mr.get("title"),
                "source_branch": mr.get("source_branch"),
                "target_branch": mr.get("target_branch"),
                "source_commit": mr.get("source_commit"),
                "merge_commit_sha": mr.get("merge_commit_sha"),
                "updated_at": mr.get("updated_at"),
                "url": (f"https://git.woa.com/{project}/-/merge_requests/{mr.get('iid')}"
                        if project and mr.get("iid") else None),
                "project": project,
            }
    return {"state": "none", "project": project, "branch": branch}


def owner_tool_dry_run(repo: str, out_dir: str, min_age: float):
    slug = repo.strip("/").replace("/", "__")
    jpath = os.path.join(out_dir, f"{slug}.json")
    apath = os.path.join(out_dir, f"{slug}.approval.json")
    cmd = [sys.executable, WORKTREE_TOOL, "--repo", repo, "--dry-run", "--json",
           "--min-no-pr-age-hours", str(min_age), "--write-approval-report", apath]
    rc, out, err = run(cmd, timeout=900)
    with open(jpath, "w") as fh:
        fh.write(out)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        data = {"error": (err or out or "no output")[:400]}
    return repo, slug, rc, data, (apath if os.path.exists(apath) else None)


def worktree_has_submodules(path: str) -> bool:
    """`git worktree remove`（非 --force）拒绝含 submodule 的工作树，提前判定。

    只读探测；命中即保留并交 owner 决定（先 deinit submodule 需要改动工作树，属
    owner 决策，巡检不代为执行）。
    """
    if not os.path.exists(os.path.join(path, ".gitmodules")):
        return False
    rc, out, _ = run(["git", "-C", path, "submodule", "status"], timeout=120)
    return rc == 0 and bool(out.strip())


def branch_standing(repo_root: str, branch: str | None):
    """相对远端默认分支的 ahead/behind + 独有 diff 规模（借鉴 gmc wt list --diff-base）。

    回答「这个残留还有独有提交吗」：0 ahead 即纯残留，有独有提交则值得 owner
    多看一眼。只作报告信号，不参与删除授权；取不到基线就缺省，不影响判定。
    """
    if not branch or branch == "(detached)":
        return None
    base = None
    rc, out, _ = run(["git", "-C", repo_root, "symbolic-ref", "-q", "--short",
                      "refs/remotes/origin/HEAD"], timeout=30)
    if rc == 0 and out.strip():
        base = out.strip()
    if base is None:
        for ref in ("origin/main", "origin/master", "main", "master"):
            rc, _, _ = run(["git", "-C", repo_root, "rev-parse", "--verify", "-q",
                            ref], timeout=30)
            if rc == 0:
                base = ref
                break
    if base is None or base == branch:
        return None
    rc, out, _ = run(["git", "-C", repo_root, "rev-list", "--left-right", "--count",
                      f"{branch}...{base}"], timeout=30)
    if rc != 0:
        return None
    parts = (out.split() + ["0", "0"])[:2]
    rc, dstat, _ = run(["git", "-C", repo_root, "diff", "--shortstat",
                        f"{base}...{branch}"], timeout=60)
    try:
        ahead, behind = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    return {"base": base, "ahead": ahead, "behind": behind,
            "unique_diff": (dstat or "").strip() or "无差异"}


def build_gongfeng_evidence(items):
    """把工蜂精确 head 项按 repo 组装成 owner 工具的 `--remote-evidence` 内容。

    只收 clean/无引用/无 BB 归属、且 `source_commit` 与本地 HEAD 全等的项；记录里
    写完整 40 位对象名，sink 端会再做一次全等校验。返回 {repo: evidence_dict}。
    token 不参与构造，凭据永远不进这个文件。
    """
    by_repo: dict[str, dict] = {}
    for o in items:
        gf = o.get("gongfeng") or {}
        head = o.get("head") or ""
        source = gf.get("source_commit") or ""
        if gf.get("state") != "merged" or not head or head != source:
            continue
        if o.get("dirty") or o.get("locked") or o.get("bb_environment") \
                or (o.get("who_uses") or {}).get("count"):
            continue
        project, host = remote_slug(o["repo"])
        record = {
            "path": o["path"], "branch": o.get("branch") or "", "head": head,
            "source_commit": source, "state": "merged",
            "mr_iid": gf.get("iid"), "mr_url": gf.get("url"),
            "target_branch": gf.get("target_branch") or "",
            "title": gf.get("title") or "",
        }
        slot = by_repo.get(o["repo"])
        if slot is None:
            slot = by_repo[o["repo"]] = {
                "version": 1, "host": host or "unknown",
                "repository": gf.get("project") or project or "",
                "default_branch": gf.get("target_branch") or "",
                "generated_by": "patrol_worktrees.py", "entries": [],
            }
        slot["entries"].append(record)
    return by_repo


def owner_tool_gongfeng_apply(repo: str, out_dir: str, evidence: dict, min_age: float):
    """对单个 repo 走 owner 工具的 dry-run → apply，两跳都只带该 repo 的证据文件。

    执行器仍是唯一 sink（内部 `git worktree remove` 非 --force、不删分支、`.local`
    备份、apply 前按指纹复验）；这里只是把「远端已有」的证人换成工蜂核验结果。
    """
    slug = repo.strip("/").replace("/", "__")
    epath = os.path.join(out_dir, f"{slug}.gongfeng-evidence.json")
    with open(epath, "w") as fh:
        json.dump(evidence, fh, ensure_ascii=False, indent=1)
    apath = os.path.join(out_dir, f"{slug}.gongfeng.approval.json")
    base = [sys.executable, WORKTREE_TOOL, "--repo", repo, "--json",
            "--min-no-pr-age-hours", str(min_age), "--remote-evidence", epath]
    dry_rc, dry_out, dry_err = run(
        base + ["--dry-run", "--write-approval-report", apath], timeout=900)
    with open(os.path.join(out_dir, f"{slug}.gongfeng.dry-run.json"), "w") as fh:
        fh.write(dry_out)
    if dry_rc != 0 or not os.path.exists(apath):
        return {"error": (dry_err or dry_out or "dry-run failed")[:400], "removed": 0,
                "entries": [], "evidence": epath, "dry_run_exit": dry_rc}
    rc, out, err = run(base + ["--apply", "--approve-report", apath], timeout=900)
    with open(os.path.join(out_dir, f"{slug}.gongfeng.apply.json"), "w") as fh:
        fh.write(out)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        data = {"error": (err or out or "no output")[:400], "removed": 0,
                "entries": [], "failed": len(evidence.get("entries") or [])}
    data["evidence"] = epath
    data["dry_run_exit"] = dry_rc
    return data


# ---------------------------------------------------------------- 状态账本

def load_state(path: str) -> dict:
    try:
        with open(path) as fh:
            d = json.load(fh)
        if d.get("version") == 1 and isinstance(d.get("worktrees"), dict):
            return d
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "updated_at": None, "worktrees": {}, "history": []}


def save_state(path: str, state: dict):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# ---------------------------------------------------------------- 主流程

def main() -> int:
    global WORKTREE_TOOL, DEVCLOUD, LEDGER
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--roots", default=",".join(DEFAULT_ROOTS))
    ap.add_argument("--round-dir", default=None,
                    help="报告目录；默认 <metrics>/<date>-worktree-patrol，重名自动加时间后缀")
    ap.add_argument("--state", default=os.path.join(STATE_DIR, "state.json"))
    ap.add_argument("--min-age-hours", type=float, default=MIN_AGE_HOURS)
    ap.add_argument("--execute", action="store_true",
                    help="对满足全部条件的项调用 owner 工具 --apply（默认只报告）")
    ap.add_argument("--append-audit", action="store_true",
                    help="把本轮事实小节追加到 storage-audit.log")
    ap.add_argument("--force-fresh", action="store_true",
                    help="忽略状态缓存，全量重测（缓存损坏或人工核对时用）")
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--all-repos", action="store_true",
                    help="扫描全部仓库（默认只扫含链接工作树的仓库）")
    ap.add_argument("--worktree-tool", default=WORKTREE_TOOL,
                    help="owner 工具路径（测试时可换 stub）")
    ap.add_argument("--devcloud", default=DEVCLOUD, help="devcloud.py 路径（测试时可换 stub）")
    ap.add_argument("--mirror-root", default="/data/code/mole-linux/ai-storage-maintenance",
                    help="仓库镜像根（默认按 AGENTS.md 约定）")
    ap.add_argument("--no-mirror", action="store_true", help="不写仓库镜像（隔离测试时用）")
    args = ap.parse_args()
    WORKTREE_TOOL, DEVCLOUD = args.worktree_tool, args.devcloud
    if args.round_dir and args.round_dir.rstrip("/").startswith(
            ("/root/.local/state/patrol-test", "/tmp/patrol-test")):
        LEDGER = os.path.join(args.round_dir.rstrip("/") + ".ledger.log")

    started = time.time()
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    date = datetime.now().strftime("%Y-%m-%d")

    # 目录：同日重名自动避让，绝不覆盖已有产物
    if args.round_dir:
        round_dir = args.round_dir
    else:
        base = os.path.join(METRICS_ROOT, f"{date}-worktree-patrol")
        round_dir = base
        if os.path.exists(os.path.join(base, "summary.json")):
            round_dir = f"{base}-{datetime.now().strftime('%H%M')}"
    os.makedirs(os.path.join(round_dir, "raw", "owner_tool"), exist_ok=True)

    state = load_state(args.state)
    if args.force_fresh:
        state["worktrees"] = {}

    print(f"[patrol] run_id={run_id} round={round_dir}")
    print(f"[patrol] state={args.state} cached_worktrees={len(state['worktrees'])}")

    roots = [r for r in args.roots.split(",") if r]
    repos = []
    for r in roots:
        repos.extend(discover_repos(r))
    print(f"[patrol] repos={len(repos)}")

    # --- 1. 便宜必查：登记项 + 指纹 ---
    # 默认只看"含链接工作树"的仓库：本巡检的处置对象是 worktree，主干本体永久保留，
    # 其可重建产物只是 review-only 背景值。全量扫 168 个仓库会稀释口径并浪费 5 倍时间。
    all_entries = []
    for repo in repos:
        for e in worktree_entries(repo):
            e["_repo"] = repo
            all_entries.append(e)
    linked_repos = {e["_repo"] for e in all_entries
                    if os.path.abspath(e["path"]) not in {os.path.abspath(r) for r in repos}}
    if args.all_repos:
        linked_repos = set(repos)
    raw_entries = [e for e in all_entries if e["_repo"] in linked_repos]
    primary_paths = set()
    for repo in repos:
        primary_paths.add(os.path.abspath(repo))

    by_path = {}
    for e in raw_entries:
        by_path.setdefault(e["path"], e)

    bb_envs, bb_threads = [], []
    bb_query_ok = True
    rc, out, _ = run(["bb", "environment", "list", "--json"], timeout=60)
    if rc == 0:
        try:
            bb_envs = json.loads(out)
        except json.JSONDecodeError:
            bb_query_ok = False
    else:
        bb_query_ok = False
    rc, out, _ = run(["bb", "thread", "list", "--json"], timeout=90)
    if rc == 0:
        try:
            bb_threads = json.loads(out)
        except json.JSONDecodeError:
            bb_query_ok = False
    else:
        bb_query_ok = False
    env_by_path = {e["path"]: e for e in bb_envs if e.get("path")}
    threads_by_path: dict[str, list] = {}
    for t in bb_threads:
        p = t.get("environmentPath")
        if p and t.get("status") != "archived":
            threads_by_path.setdefault(p, []).append({"id": t["id"], "status": t["status"]})

    objects = []
    state_new = dict(state)
    state_new["worktrees"] = dict(state["worktrees"])
    reused = measured = removed_state = 0

    def build(entry):
        nonlocal reused, measured
        path = entry["path"]
        primary = os.path.abspath(path) in primary_paths
        exists = os.path.exists(path)
        obj = {
            "path": path, "repo": entry["_repo"], "primary": primary, "exists_now": exists,
            "branch": entry.get("branch", "(unknown)"),
            "detached": entry.get("branch") == "(detached)",
            "head": entry.get("head", ""), "locked": bool(entry.get("locked")),
            "prunable": bool(entry.get("prunable")), "dirty": None,
            "age_hours": None, "size_bytes": None, "artifact_bytes": 0,
            "artifact_hits": [], "size_source": None, "size_measured_at": None,
            "last_active_at": None, "dirty_summary": None,
            "ignored": None, "fingerprint": None, "who_uses": None,
            "bb_environment": None, "bb_threads": [],
            "remote_proof": None, "skip_reason": None, "pr": None, "approval_token": None,
        }
        if not exists:
            return obj, None
        obj["age_hours"] = worktree_age_hours(path)
        ignored, ignored_err = git_ignored(path)
        rc2, sout, _ = run(["git", "-C", path, "status", "--porcelain", "-unormal"], timeout=120)
        dirty = None if rc2 != 0 else bool(sout.strip())
        obj["dirty"] = dirty
        if dirty:
            shortstat = ""
            if any(not l.startswith("??") for l in sout.splitlines() if l.strip()):
                rc3, sstat, _ = run(["git", "-C", path, "diff", "HEAD", "--shortstat"], timeout=120)
                if rc3 == 0:
                    shortstat = sstat.strip()
            obj["dirty_summary"] = summarize_dirty(sout, shortstat)
        obj["ignored"] = {"count": ignored["count"], "sample": ignored["sample"],
                          "digest": ignored["digest"]} if ignored else None
        fp = fingerprint(entry, dirty, ignored) if ignored_err is None else None
        obj["fingerprint"] = fp
        cached = state["worktrees"].get(path)
        same = (cached and fp and cached.get("fingerprint") == fp
                and cached.get("size_bytes") is not None)
        if same:
            obj["size_bytes"] = cached["size_bytes"]
            obj["artifact_bytes"] = cached.get("artifact_bytes") or 0
            obj["artifact_hits"] = cached.get("artifact_hits") or []
            obj["size_source"] = "cached"
            obj["size_measured_at"] = cached.get("size_measured_at")
            obj["last_active_at"] = cached.get("last_active_at")
            obj["who_uses"] = cached.get("who_uses")
            reused += 1
            if not obj["last_active_at"]:
                # 旧状态没有 last_active_at：一次性回填（同一次 du 顺带刷新体积），
                # 之后该字段照常进缓存，不再每轮重测。
                b, last_active = du_bytes_last_active(path, timeout=180)
                if b is not None:
                    obj["size_bytes"] = b
                    obj["size_source"] = "measured"
                    obj["size_measured_at"] = now_iso()
                obj["last_active_at"] = last_active
        else:
            b, last_active = du_bytes_last_active(path, timeout=180)
            ab, hits = artifact_bytes(path)
            obj["size_bytes"] = b
            obj["last_active_at"] = last_active
            obj["artifact_bytes"] = ab
            obj["artifact_hits"] = sorted(hits, key=lambda x: -x["kb"])[:20]
            obj["size_source"] = "measured"
            obj["size_measured_at"] = now_iso()
            measured += 1
        env = env_by_path.get(path)
        obj["bb_environment"] = env["id"] if env else None
        obj["bb_threads"] = threads_by_path.get(path, [])
        rec = dict(cached or {})
        rec.update({
            "repo": entry["_repo"], "fingerprint": fp, "head": obj["head"],
            "branch": obj["branch"], "detached": obj["detached"],
            "locked": obj["locked"], "prunable": obj["prunable"], "dirty": dirty,
            "age_hours": round(obj["age_hours"], 2) if obj["age_hours"] is not None else None,
            "size_bytes": obj["size_bytes"], "artifact_bytes": obj["artifact_bytes"],
            "artifact_hits": obj["artifact_hits"],
            "size_measured_at": obj["size_measured_at"], "size_source": obj["size_source"],
            "last_active_at": obj["last_active_at"], "dirty_summary": obj["dirty_summary"],
            "who_uses": obj["who_uses"], "last_seen": now_iso(),
            "first_seen": (cached or {}).get("first_seen") or now_iso(),
        })
        return obj, rec

    with cf.ThreadPoolExecutor(max_workers=args.jobs) as ex:
        for obj, rec in ex.map(build, list(by_path.values())):
            objects.append(obj)
            if rec:
                state_new["worktrees"][obj["path"]] = rec

    live_paths = {o["path"] for o in objects}
    for p in list(state_new["worktrees"]):
        if p not in live_paths:
            state_new["worktrees"].pop(p, None)
            removed_state += 1
    print(f"[patrol] entries={len(objects)} measured={measured} reused={reused} "
          f"state_dropped={removed_state}")

    # --- 2. owner 工具远端核对（决定量，必须每轮做）---
    repos_with_linked = sorted({e["_repo"] for e in raw_entries})
    ot_dir = os.path.join(round_dir, "raw", "owner_tool")
    ot_reports, owner_eligible = {}, []
    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(owner_tool_dry_run, r, ot_dir, args.min_age_hours)
                for r in repos_with_linked]
        for fut in futs:
            repo, slug, rc, data, apath = fut.result()
            _, host = remote_slug(repo)
            ot_reports[slug] = {"repo": repo, "host": host, "exit": rc,
                                "error": data.get("error"), "eligible": data.get("eligible"),
                                "entries": data.get("entries") or [], "approval_report": apath}
    obj_by_path = {o["path"]: o for o in objects}
    for slug, rep in ot_reports.items():
        for e in rep["entries"]:
            o = obj_by_path.get(e.get("path"))
            if not o:
                continue
            o["skip_reason"] = e.get("skip_reason")
            o["pr"] = e.get("pr")
            o["remote_proof"] = e.get("remote_proof")
            o["approval_token"] = e.get("approval_token")
            if e.get("action") == "would-remove":
                owner_eligible.append({"path": o["path"], "repo": rep["repo"],
                                       "token": e.get("approval_token"),
                                       "approval_report": rep["approval_report"],
                                       "age_hours": e.get("age_hours"),
                                       "head": e.get("head"), "pr": e.get("pr"),
                                       "remote_proof": e.get("remote_proof"),
                                       "ignored": e.get("ignored")})
    for o in objects:
        if o["primary"] or not o["exists_now"]:
            continue
        if o["skip_reason"] is None and not any(x["path"] == o["path"] for x in owner_eligible):
            o["skip_reason"] = "owner 工具未评估（repo 非 GitHub host 或查询失败）"

    # --- 2b. 非 GitHub host：用 gongfengcli 核验 MR ---
    # token 只在本进程内解析并经 env 传给子进程；不进 argv、不进任何产物。
    host_by_repo = {r: remote_slug(r)[1] for r in repos_with_linked}
    gf_targets = [o for o in objects
                  if not o["primary"] and o["exists_now"]
                  and host_by_repo.get(o["repo"]) not in (None, "github.com")
                  and (o["age_hours"] or 0) >= args.min_age_hours]
    gf_token, gf_source = gongfeng_token()
    gf_meta = {"credential_source": gf_source if gf_token else None,
               "credential_ok": bool(gf_token), "reason": None if gf_token else gf_source,
               "lookups": 0, "cached": 0}
    if gf_targets and not gf_token:
        for o in gf_targets:
            o["gongfeng"] = {"state": "no-credential", "reason": gf_source}
    elif gf_targets:
        cache: dict[tuple, dict] = {}
        for o in gf_targets:
            key = (o["repo"], o["branch"])
            if key not in cache:
                project = remote_slug(o["repo"])[0]
                cache[key] = gongfeng_lookup(project, o["branch"], gf_token)
                gf_meta["lookups"] += 1
            else:
                gf_meta["cached"] += 1
            o["gongfeng"] = cache[key]
    print(f"[patrol] gongfeng targets={len(gf_targets)} lookups={gf_meta['lookups']} "
          f"credential={'ok' if gf_token else gf_source}")

    # --- 3. who-uses：只探需要判定的对象（串行，避免 argv 自匹配）---
    need_who = []
    for o in objects:
        if o["primary"] or not o["exists_now"]:
            continue
        eligible = any(x["path"] == o["path"] for x in owner_eligible)
        old_enough = (o["age_hours"] or 0) >= args.min_age_hours
        # 执行前必须无引用；≥7 天的项无论脏净都探一次，把"有进程在用"作为保留理由报出来。
        if eligible or old_enough:
            need_who.append(o)
    probed = 0
    for o in need_who:
        probe = o["path"]
        if probe.startswith("/tmp/"):
            cand = "/data/system-tmp/" + probe[len("/tmp/"):]
            if os.path.exists(cand):
                probe = cand
        rc, out, _ = run(["python3", "-B", DEVCLOUD, "who-uses", "--json", probe], timeout=180)
        try:
            res = json.loads(out)
        except json.JSONDecodeError:
            res = {"count": None, "error": (out or "")[:200]}
        o["who_uses"] = {"count": res.get("count"), "probe_path": probe,
                         "at": now_iso(), "method": "serial"}
        if o["path"] in state_new["worktrees"]:
            state_new["worktrees"][o["path"]]["who_uses"] = o["who_uses"]
        probed += 1
    print(f"[patrol] who_uses_probed={probed} owner_eligible={len(owner_eligible)}")

    # --- 4. 分类 ---
    executed_prev = state.get("history") or []
    cumulative = sum(h.get("executed_bytes") or 0 for h in executed_prev)
    cumulative_since = state.get("cumulative_since") or (
        executed_prev[0]["date"] if executed_prev else date)

    unreclaimed, held_under7, prunable_regs, verified_reclaimable = [], [], [], []
    ot_error_repos = {rep["repo"] for rep in ot_reports.values() if rep.get("error")}

    def head_matches(source_commit: str, head: str) -> bool:
        if not source_commit or not head:
            return False
        return source_commit.startswith(head[:12]) or head.startswith(source_commit[:12])

    def reasons_for(o) -> list[str]:
        rs = []
        if o["dirty"]:
            rs.append("dirty(未提交)")
        if o["locked"]:
            rs.append("locked")
        if o["bb_environment"] or o["bb_threads"]:
            rs.append("活跃 BB 环境")
        if (o["who_uses"] or {}).get("count"):
            rs.append("其他(活跃进程引用)")
        if o["pr"] and str(o["pr"].get("state", "")).upper() == "OPEN":
            rs.append("PR-open")
        gf = o.get("gongfeng") or {}
        if host_by_repo.get(o["repo"]) not in (None, "github.com"):
            rs.append("非 GitHub host")
            gs = gf.get("state")
            if gs == "merged":
                exact = head_matches(gf.get("source_commit"), o["head"])
                rs.append("MR merged(工蜂核验·精确 head)" if exact
                          else "MR merged(工蜂核验·非精确 head)")
                if exact and not o["dirty"] and not o["locked"] and not o["bb_environment"] \
                        and not (o["who_uses"] or {}).get("count"):
                    verified_reclaimable.append(o)
            elif gs == "opened":
                rs.append("MR-open")
            elif gs == "closed":
                rs.append("MR closed(未合并)")
            elif gs == "none":
                rs.append("非 GitHub host(无 MR)")
            elif gs == "error":
                rs.append("MR 查询失败")
            elif gs == "no-credential":
                rs.append("凭据缺失")
        elif o["repo"] in ot_error_repos:
            rs.append("非 GitHub host")
        skip = str(o["skip_reason"] or "")
        if "ambiguous" in skip:
            rs.append("工具判 ambiguous")
        elif "is not contained in the PR remote head" in skip and "durability" not in skip:
            rs.append("关联证据(非精确 head)")
        elif any(k in skip for k in ("durability check failed", "remote branch lookup failed",
                                     "PR lookup failed", "no completed PR, remote branch tip",
                                     "no associated PR and worktree is younger")):
            rs.append("远端不可证")
        elif not skip and not o["remote_proof"]:
            rs.append("远端不可证")
        if not rs:
            rs.append("其他(本轮未执行)")
        return list(dict.fromkeys(rs))

    for o in objects:
        if o["primary"]:
            continue
        if not o["exists_now"]:
            if o["prunable"]:
                prunable_regs.append(o)
            continue
        if (o["age_hours"] or 0) < args.min_age_hours:
            held_under7.append(o)
            o["decision"], o["reason"], o["reasons"] = "保留(<7天)", "age<7d", ["age<7d"]
            continue
        o["reasons"] = reasons_for(o)
        o["decision"], o["reason"] = "保留(≥7天)", ";".join(o["reasons"])
        unreclaimed.append(o)

    unreclaimed.sort(key=lambda x: -(x["size_bytes"] or 0))
    unreclaimed_bytes = sum(o["size_bytes"] or 0 for o in unreclaimed)
    held_bytes = sum(o["size_bytes"] or 0 for o in held_under7)

    # 孤儿分支清扫统计（执行器 apply 内建，GitHub 与工蜂两跳都收）
    orphan_branches_removed: list[dict] = []

    # --- 5. 执行（仅在 --execute 且条件齐备时）---
    executed_this_round, failed = [], []
    executable = [e for e in owner_eligible
                  if e["age_hours"] is not None and e["age_hours"] >= args.min_age_hours
                  and e["path"] in obj_by_path
                  and (obj_by_path[e["path"]].get("who_uses") or {}).get("count") == 0
                  and not obj_by_path[e["path"]]["bb_environment"]
                  and not obj_by_path[e["path"]]["bb_threads"]]

    # 动态证据在执行前刷新：who-uses 重探，非零或未知即撤出本轮（下轮自动重评）。
    if args.execute and executable:
        refreshed = []
        for e in executable:
            probe = e["path"]
            if probe.startswith("/tmp/"):
                cand = "/data/system-tmp/" + probe[len("/tmp/"):]
                if os.path.exists(cand):
                    probe = cand
            rc, out, _ = run(["python3", "-B", DEVCLOUD, "who-uses", "--json", probe],
                             timeout=180)
            try:
                res = json.loads(out)
            except json.JSONDecodeError:
                res = {}
            count = res.get("count")
            obj_by_path[e["path"]]["who_uses"] = {
                "count": count, "probe_path": probe, "at": now_iso(),
                "method": "execute-refresh", "error": None if count == 0 else (out or "")[:200],
            }
            if count == 0:
                refreshed.append(e)
        dropped_refresh = len(executable) - len(refreshed)
        if dropped_refresh:
            print(f"[patrol] 执行前刷新：{dropped_refresh} 项 who-uses 非零/未知，撤出本轮保留待重评")
        executable = refreshed
    if args.execute and executable and not bb_query_ok:
        print("[patrol] BB 归属查询失败，本轮全部保守保留（含合格项），下轮自动重评")
        executable = []

    if args.execute and executable:
        by_report: dict[str, list] = {}
        for e in executable:
            by_report.setdefault(e["approval_report"], []).append(e)
        for report, items in by_report.items():
            if not report:
                continue
            # owner 2026-09-19（采纳 Oracle P0）：只审批筛后集合，不再整报审批——
            # 同仓被 who-uses/BB/年龄排除的候选不得随报告获批。
            approve_args: list[str] = []
            for e in items:
                if e.get("token"):
                    approve_args += ["--approve", e["token"]]
            if not approve_args:
                continue
            rc, out, err = run([sys.executable, WORKTREE_TOOL, "--repo", items[0]["repo"],
                                "--apply", "--json", *approve_args], timeout=900)
            try:
                res = json.loads(out)
            except json.JSONDecodeError:
                res = {"error": (err or out)[:300], "removed": 0, "failed": len(items)}
            with open(os.path.join(ot_dir, os.path.basename(report).replace(
                    ".approval.json", ".apply.json")), "w") as fh:
                fh.write(out)
            for e in items:
                ent = next((x for x in res.get("entries", []) if x.get("path") == e["path"]), {})
                if ent.get("action") == "removed":
                    o = obj_by_path[e["path"]]
                    executed_this_round.append({
                        "path": e["path"], "repo": e["repo"], "head": e["head"],
                        "branch": o["branch"], "age_days": round((e["age_hours"] or 0) / 24, 2),
                        "pr": (e.get("pr") or {}).get("number"),
                        "remote_proof": (e.get("remote_proof") or {}).get("description"),
                        "size_bytes": o["size_bytes"], "artifact_bytes": o["artifact_bytes"],
                        "ignored_sample": (e.get("ignored") or {}).get("sample"),
                        "backup_root": res.get("backup_root"),
                        "last_active_at": o.get("last_active_at"),
                        "branch_deleted": (ent.get("branch_deletion") or {}).get("deleted"),
                    })
                else:
                    failed.append({"path": e["path"], "error": ent.get("error") or res.get("error")})

    # --- 5b. 工蜂（非 GitHub host）：证据经 owner 工具 --remote-evidence 走同一 sink ---
    # 唯一执行器不变（`git worktree remove` 非 --force、.local 备份、apply 前指纹复验）；
    # 分支按 owner 2026-09-19 政策默认删除（远端证明就是删除分支的依据）；
    # 工蜂场景只是把「远端已有」的证人从 gh API 换成 gongfengcli 核验结果，sink 端仍按完整对象名全等校验。
    verified_remaining = list(verified_reclaimable)
    gongfeng_blocked_items = []
    attemptable = []
    for o in verified_reclaimable:
        if worktree_has_submodules(o["path"]):
            # git worktree remove（非 --force）拒绝含 submodule 的工作树；提前判定，
            # 不每轮重复撞同一个拒绝，也不绕过它。
            o["gongfeng_blocked"] = "含 submodule：git 拒绝非 --force 移除（需 owner 决定）"
            gongfeng_blocked_items.append(o)
        else:
            attemptable.append(o)
    if args.execute and attemptable:
        gongfeng_executed_paths: set[str] = set()
        for repo, ev in sorted(build_gongfeng_evidence(attemptable).items()):
            res = owner_tool_gongfeng_apply(repo, ot_dir, ev, args.min_age_hours)
            for item in (res.get("branch_sweep") or {}).get("branches_removed") or []:
                orphan_branches_removed.append(
                    {"repo": repo, "branch": item["branch"], "proof": item.get("proof")})
            entries = res.get("entries") if isinstance(res.get("entries"), list) else []
            for record in ev["entries"]:
                o = obj_by_path.get(record["path"])
                ent = next((x for x in entries
                            if isinstance(x, dict) and x.get("path") == record["path"]), {})
                if ent.get("action") == "removed" and o is not None:
                    gongfeng_executed_paths.add(record["path"])
                    executed_this_round.append({
                        "path": record["path"], "repo": repo, "head": record["head"],
                        "branch": o["branch"],
                        "age_days": round((o["age_hours"] or 0) / 24, 2),
                        "pr": record["mr_iid"],
                        "remote_proof": (ent.get("remote_proof") or {}).get("description"),
                        "size_bytes": o["size_bytes"], "artifact_bytes": o["artifact_bytes"],
                        "ignored_sample": (ent.get("ignored") or {}).get("sample"),
                        "backup_root": res.get("backup_root"),
                        "last_active_at": o.get("last_active_at"),
                        "branch_deleted": (ent.get("branch_deletion") or {}).get("deleted"),
                    })
                else:
                    failed.append({
                        "path": record["path"],
                        "error": ent.get("error") or ent.get("skip_reason")
                        or res.get("error") or "owner 工具未执行",
                    })
        verified_remaining = [o for o in verified_reclaimable
                              if o["path"] not in gongfeng_executed_paths]
    executed_bytes = sum(x["size_bytes"] or 0 for x in executed_this_round)

    # 复核：提交仍可达；分支按 owner 2026-09-19 政策默认已删，仍在反而要关注
    verify = []
    for x in executed_this_round:
        rc1, _, _ = run(["git", "-C", x["repo"], "cat-file", "-t", x["head"]], timeout=60)
        rc2, out2, _ = run(["git", "-C", x["repo"], "branch", "--list", x["branch"]], timeout=60)
        branch_present = bool(out2.strip())
        verify.append({"path": x["path"], "commit_type": "commit" if rc1 == 0 else "missing",
                       "branch_present": branch_present,
                       "branch_deleted": x.get("branch_deleted"),
                       "branch_anomaly": branch_present})

    if executed_this_round:
        state_new.setdefault("history", []).append({
            "date": date, "run_id": run_id, "executed_bytes": executed_bytes,
            "count": len(executed_this_round), "items": [x["path"] for x in executed_this_round],
        })
        cumulative += executed_bytes
        # 已执行项不再有磁盘实体，必须同时退出「≥7 天未清理」集合与原因分布，
        # 否则同一对象会同时出现在「已执行」和「未清理」两张表里。
        executed_paths = {x["path"] for x in executed_this_round}
        unreclaimed = [o for o in unreclaimed if o["path"] not in executed_paths]
        unreclaimed_bytes = sum(o["size_bytes"] or 0 for o in unreclaimed)

    state_new["updated_at"] = now_iso()
    save_state(args.state, state_new)
    os.makedirs(os.path.dirname(args.state), exist_ok=True)
    with open(os.path.join(round_dir, "raw", "state_snapshot.json"), "w") as fh:
        json.dump(state_new, fh, ensure_ascii=False, indent=1)

    # --- 5c. 分支价值信号：相对远端默认分支的 ahead/behind + 独有 diff 规模 ---
    # 借鉴 gmc wt list --diff-base；只对进入决策表的 ≥7 天对象计算，不作删除授权。
    for o in unreclaimed:
        if o.get("branch"):
            o["standing"] = branch_standing(o["repo"], o["branch"])

    # --- 5d. prunable 注册清理（owner 2026-09-19 授权：--execute 时自动 prune）---
    # git 已确认 gitdir 指向的目录不存在，prune 只删失效元数据，零数据丢失。
    prunable_pruned: list[dict] = []
    if args.execute and prunable_regs:
        for repo in sorted({o["repo"] for o in prunable_regs}):
            rc, out, err = run(["git", "-C", repo, "worktree", "prune", "--verbose"],
                               timeout=120)
            removed_lines = [l.strip() for l in (out or "").splitlines() if l.strip()]
            prunable_pruned.append({
                "repo": repo, "removed": len(removed_lines) if rc == 0 else 0,
                "detail": removed_lines[:10],
                "error": None if rc == 0 else (err or out or f"git 退出 {rc}")[:200],
            })

    # --- 6. 预聚合输出 ---
    by_reason: dict[str, dict] = {}
    for o in unreclaimed:
        for r in o["reasons"]:
            slot = by_reason.setdefault(r, {"count": 0, "bytes": 0})
            slot["count"] += 1
            slot["bytes"] += o["size_bytes"] or 0
    by_project_exec: dict[str, list] = {}
    for x in executed_this_round:
        by_project_exec.setdefault(x["repo"], []).append(x)

    infra = gather_infra()

    def func_of(o):
        pr = o.get("pr") or {}
        if pr.get("title"):
            return f"{pr['title']}（推断）"
        return f"{o['branch']}（推断）" if o["branch"] else "无描述"

    _web_cache: dict[str, str] = {}

    def remote_web(repo: str):
        if repo in _web_cache:
            return _web_cache[repo] or None
        rc, out, _ = run(["git", "-C", repo, "remote", "get-url", "origin"], timeout=30)
        url = out.strip() if rc == 0 else ""
        web = ""
        m = re.search(r"^(?:ssh://)?(?:[^@/]+@)?([^:/]+)[:/](.+?)(?:\.git)?/?$", url)
        if m:
            web = f"{m.group(1)}/{m.group(2)}"
        _web_cache[repo] = web
        return web or None

    def pr_url(o):
        pr = o.get("pr") or {}
        if pr.get("url"):
            return pr["url"]
        gf = o.get("gongfeng") or {}
        if gf.get("url"):
            return gf["url"]
        if not o.get("repo"):
            return "无"
        _, host = remote_slug(o["repo"])
        if host and host != "github.com":
            if gf.get("state") == "none":
                return f"无 MR（{host}）"
            web = remote_web(o["repo"])
            return f"{web or host} · 分支 {o['branch']}（MR 未验证）"
        return "无"

    # evidence 与回复表格读对象上的字段，这里一次性补齐，避免只有 stdout 有链接。
    for o in unreclaimed + verified_reclaimable:
        o["pr_url"] = pr_url(o)
        o["function"] = func_of(o)
        o["next_step"] = o.get("skip_reason") or "保留"
    # 已执行项进 evidence.json 前补齐功能/链接字段（回复表格直接取用）。
    for x in executed_this_round:
        o = obj_by_path.get(x["path"], {})
        x["function"] = func_of(o)
        x["pr_url"] = pr_url(o)

    gongfeng_executed_items = [o for o in verified_reclaimable
                               if o["path"] not in {x["path"] for x in verified_remaining}]
    gongfeng_executed_bytes = sum(o["size_bytes"] or 0 for o in gongfeng_executed_items)

    summary = {
        "run_id": run_id, "collected_at": now_iso(), "mode":
            "execute" if args.execute else "dry-run",
        "round_dir": round_dir, "min_age_hours": args.min_age_hours,
        "registered_entries": len(objects),
        "primary_checkouts": sum(1 for o in objects if o["primary"]),
        "linked_present": sum(1 for o in objects if o["exists_now"] and not o["primary"]),
        "measured": measured, "reused": reused,
        "eligible_count": len(owner_eligible) + len(verified_reclaimable),
        "executed_count": len(executed_this_round), "executed_bytes": executed_bytes,
        "failed_count": len(failed),
        "unreclaimed_over_7d_count": len(unreclaimed),
        "unreclaimed_over_7d_bytes": unreclaimed_bytes,
        "unreclaimed_by_reason": by_reason,
        "held_under7_count": len(held_under7), "held_under7_bytes": held_bytes,
        "cumulative_reclaimed_bytes": cumulative, "cumulative_since": cumulative_since,
        "who_uses_probed": probed,
        "prunable_registrations": len(prunable_regs),
        "prunable_pruned": prunable_pruned,
        "orphan_branches_removed": orphan_branches_removed,
        "orphan_branches_removed_count": len(orphan_branches_removed),
        "branch_deleted_count": sum(1 for x in executed_this_round if x.get("branch_deleted")),
        "verified_reclaimable_count": len(verified_reclaimable),
        "verified_reclaimable_bytes": sum(o["size_bytes"] or 0 for o in verified_reclaimable),
        "gongfeng_credential_ok": bool(gf_token),
        "gongfeng_credential_source": gf_meta["credential_source"],
        "gongfeng_lookups": gf_meta["lookups"],
        "gongfeng_eligible_count": len(verified_reclaimable),
        "gongfeng_executed_count": len(gongfeng_executed_items),
        "gongfeng_executed_bytes": gongfeng_executed_bytes,
        "gongfeng_blocked_count": len(gongfeng_blocked_items),
        "verified_reclaimable_remaining_count": len(verified_remaining),
        "cleaner_consecutive_failures": infra["cleaner"]["consecutive_failures"],
        "listeners_unknown": sum(1 for r in infra["listeners"] if r["whitelist"] != "白名单命中"),
    }
    evidence = {"summary": summary, "objects": objects, "executed": executed_this_round,
                "unreclaimed": [{"path": o["path"], "repo": o["repo"],
                                 "size_bytes": o["size_bytes"], "age_hours": o["age_hours"],
                                 "last_active_at": o.get("last_active_at"),
                                 "dirty_summary": o.get("dirty_summary"),
                                 "standing": o.get("standing"),
                                 "reasons": o["reasons"], "pr": o["pr"],
                                 "pr_url": pr_url(o), "function": func_of(o),
                                 "next_step": o["skip_reason"] or "保留"} for o in unreclaimed],
                "held_under_7d_paths": [o["path"] for o in held_under7],
                "prunable_registrations": [o["path"] for o in prunable_regs],
                "verified_reclaimable": [
                    {"path": o["path"], "repo": o["repo"], "size_bytes": o["size_bytes"],
                     "age_hours": o["age_hours"], "last_active_at": o.get("last_active_at"),
                     "iid": (o.get("gongfeng") or {}).get("iid"),
                     "url": (o.get("gongfeng") or {}).get("url"),
                     "source_branch": o["branch"], "head": o["head"]}
                    for o in verified_remaining],
                "gongfeng": {k: v for k, v in gf_meta.items() if k != "reason"},
                "infra": infra,
                "owner_tool": {k: {kk: vv for kk, vv in v.items() if kk != "entries"}
                               for k, v in ot_reports.items()},
                "verify": verify}
    with open(os.path.join(round_dir, "evidence.json"), "w") as fh:
        json.dump(evidence, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(round_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)

    root_pct = df_pct("/") or 0.0
    data_pct = df_pct("/data") or 0.0
    summary["root_used_pct"] = root_pct
    summary["data_used_pct"] = data_pct
    with open(os.path.join(round_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)

    # 账本先落盘：stdout 被调用方截断（SIGPIPE）也不能丢产物
    audit = render_audit(summary, executed_this_round, unreclaimed, by_reason, verify, failed)
    with open(os.path.join(round_dir, "audit_section.md"), "w") as fh:
        fh.write(audit)
    mirror_path = None if args.no_mirror else mirror_round(round_dir, args.mirror_root)
    summary["mirror_dir"] = mirror_path
    with open(os.path.join(round_dir, "summary.json"), "w") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    if args.append_audit:
        with open(LEDGER, "a") as fh:
            fh.write("\n" + audit)

    # 紧凑的 Agent 输出：只给需要判断的部分
    print("\n===== 本轮汇总 =====")
    print(f"登记 {summary['registered_entries']}（主 {summary['primary_checkouts']}）；"
          f"可访问链接 {summary['linked_present']}；测量 {measured} / 复用缓存 {reused}")
    print(f"eligible {summary['eligible_count']}；本轮执行 {summary['executed_count']} 项 "
          f"/ {fmt_bytes(executed_bytes)}；失败 {summary['failed_count']}")
    print(f"≥7 天未清理 {summary['unreclaimed_over_7d_count']} 项 / "
          f"{fmt_bytes(unreclaimed_bytes)}；<7 天保留 {summary['held_under7_count']} 项 / "
          f"{fmt_bytes(held_bytes)}")
    print(f"累计回收 {fmt_bytes(cumulative)}（自 {cumulative_since}）")
    print("\n--- 已执行（按项目）---")
    if not by_project_exec:
        print("无")
    for repo, items in sorted(by_project_exec.items()):
        for x in items:
            print(f"  {repo}\n    {x['path']}  {x['age_days']}d  {fmt_bytes(x['size_bytes'])}  "
                  f"上次活跃 {x.get('last_active_at') or '不可得'}  PR#{x['pr']}  {x['branch']}")
    print("\n--- ≥7 天未清理 · 原因分布 ---")
    for r, v in sorted(by_reason.items(), key=lambda kv: -kv[1]["bytes"]):
        print(f"  {r:22s} {v['count']:3d} 项 / {fmt_bytes(v['bytes'])}")
    print("\n--- ≥7 天未清理 · 前 30 项（按空间，供统一表格：项目|结果|原因|空间|年龄|上次活跃|领先/落后）---")
    for o in unreclaimed[:30]:
        st = o.get("standing") or {}
        st_txt = f"a{st.get('ahead', '?')}/b{st.get('behind', '?')}" if st else "—"
        print(f"  {fmt_bytes(o['size_bytes']):>10}  {round((o['age_hours'] or 0)/24,1):>5}d  "
              f"{(o.get('last_active_at') or '不可得')[:19]:19s}  "
              f"{'/'.join(o['reasons'])[:30]:30s}{compact_dirty(o.get('dirty_summary')):22s} "
              f"{st_txt:>10}  "
              f"{o['repo']}  {o['path'][:48]}")
        if st and st.get("ahead"):
            print(f"             独有提交 {st['ahead']} 个（vs {st['base']}）：{st['unique_diff'][:90]}")
    mr_rows = [o for o in unreclaimed
               if "非 GitHub host" in o["reasons"] or "工具判 ambiguous" in o["reasons"]]
    print(f"\n--- 需 owner 查 MR/PR 的 {len(mr_rows)} 项（完整）---")
    for o in mr_rows:
        print(f"  {fmt_bytes(o['size_bytes']):>10}  {o['repo']}  {o['branch']}  "
              f"{o.get('pr_url') or pr_url(o)}")
    if verified_reclaimable:
        print(f"\n--- 工蜂 MR 已核验且精确 head {len(verified_reclaimable)} 项"
              f"（{fmt_bytes(sum(o['size_bytes'] or 0 for o in verified_reclaimable))}；"
              f"经 --remote-evidence 走同一 owner 执行器，本轮执行 "
              f"{summary['gongfeng_executed_count']} 项 / "
              f"{fmt_bytes(summary['gongfeng_executed_bytes'])}）---")
        for o in sorted(verified_remaining, key=lambda x: -(x["size_bytes"] or 0)):
            gf = o.get("gongfeng") or {}
            note = f"  [{o['gongfeng_blocked']}]" if o.get("gongfeng_blocked") else ""
            print(f"  未执行 {fmt_bytes(o['size_bytes']):>10}  "
                  f"{round((o['age_hours'] or 0)/24,1):>5}d  "
                  f"上次活跃 {(o.get('last_active_at') or '不可得')[:19]:19s}  "
                  f"MR!{gf.get('iid')} -> {gf.get('target_branch')}  {o['path'][:56]}{note}")
            print(f"              {gf.get('url')}")
    if not gf_meta["credential_ok"] and gf_targets:
        print(f"\n[patrol] 工蜂凭据不可用：{gf_meta['reason']}"
              f"（对象已保留并标注「凭据缺失」，未尝试猜测）")
    print(f"\n--- 基础设施 ---")
    c = infra["cleaner"]
    print(f"  clean-tmp 连续失败 {c['consecutive_failures']} 天；最近 {c['last'].get('ts')} "
          f"status={c['last'].get('status')} scan={c['last'].get('scan')} "
          f"deleted={c['last'].get('deleted')}")
    for ln in infra["docker_df"]:
        print(f"  docker| {ln}")
    for r in infra["listeners"]:
        print(f"  端口 {r['port']:<6} {r['addr']:<16} {'/'.join(r['procs'])[:34]:<34} {r['whitelist']}")
    if prunable_regs:
        pruned_total = sum(p["removed"] for p in prunable_pruned)
        print(f"  prunable 无实体登记项 {len(prunable_regs)}："
              + (f"--execute 已自动 prune {pruned_total} 条" if args.execute
                 else "本轮 dry-run 未清理，--execute 时自动 prune"))
        for p in prunable_pruned:
            if p.get("error"):
                print(f"    prune 失败 {p['repo']}: {p['error']}")
    if not bb_query_ok:
        print("  [保守] BB 归属查询失败：本轮所有候选保留，下轮自动重评")
    if orphan_branches_removed:
        print(f"  孤儿分支已删 {len(orphan_branches_removed)} 条："
              + "；".join(f"{r['repo'].rsplit('/',1)[-1]}:{r['branch']}" for r in orphan_branches_removed[:10]))
    if failed:
        print("\n--- 执行失败 ---")
        for f in failed:
            print(f"  {f['path']}: {f['error']}")
    print(f"\n[patrol] 产物 {round_dir}  用时 {time.time()-started:.1f}s")
    return 0


def mirror_round(round_dir: str, mirror_root: str):
    """把本轮产物镜像进仓库（best-effort；仓库不可写不影响本轮结论）。"""
    dst = os.path.join(mirror_root, os.path.basename(round_dir.rstrip("/")))
    try:
        os.makedirs(dst, exist_ok=True)
        for item in ("evidence.json", "summary.json", "audit_section.md"):
            src = os.path.join(round_dir, item)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(dst, item))
        raw_src = os.path.join(round_dir, "raw")
        if os.path.isdir(raw_src):
            shutil.copytree(raw_src, os.path.join(dst, "raw"), dirs_exist_ok=True)
        with open(os.path.join(mirror_root, ".current_round"), "w") as fh:
            fh.write(os.path.basename(round_dir.rstrip("/")) + "\n")
        return dst
    except OSError:
        return None


def gather_infra() -> dict:
    """执行器与基础设施健康：clean-tmp 连续失败、Docker 可回收、非白名单监听。"""
    import glob
    import gzip

    summaries = []
    for p in sorted(glob.glob("/data/metrics/devcloud-clean-tmp.log*")):
        try:
            opener = gzip.open if p.endswith(".gz") else open
            with opener(p, "rt", errors="replace") as fh:
                for line in fh:
                    if '"event": "summary"' in line:
                        try:
                            summaries.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except OSError:
            continue
    summaries.sort(key=lambda d: d.get("ts") or "")
    consecutive_failures = 0
    for d in reversed(summaries):
        if d.get("status") == "ok" and d.get("scan") == "complete":
            break
        consecutive_failures += 1
    last = summaries[-1] if summaries else {}

    rc, out, _ = run(["docker", "system", "df"], timeout=60)
    docker_lines = [ln for ln in out.strip().splitlines() if ln.strip()]

    whitelist = {}
    try:
        with open("/data/code/mole-linux/devcloud-exposure/whitelist.json") as fh:
            wl = json.load(fh)
        whitelist = {e["port"]: e for e in wl.get("entries", [])}
    except (OSError, json.JSONDecodeError, KeyError):
        pass

    rc, dout, _ = run(["docker", "ps", "--format", "{{.Names}}\t{{.Ports}}"], timeout=30)
    docker_ports = {}
    for ln in dout.splitlines():
        if "\t" in ln:
            name, ports = ln.split("\t", 1)
            for m in re.finditer(r":(\d+)->", ports):
                docker_ports.setdefault(int(m.group(1)), []).append(name)

    rc, out, _ = run(["ss", "-tlnp"], timeout=30)
    exposed = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] != "LISTEN":
            continue
        m = re.match(r"^(.*):(\d+)$", parts[3])
        if not m:
            continue
        addr, port = m.group(1), int(m.group(2))
        if addr.startswith("127.") or addr in ("[::1]", "::1"):
            continue
        procs = sorted({p[0] for p in re.findall(r'\("([^"]+)",pid=(\d+)', line)})
        exposed[port] = {"port": port, "addr": addr, "procs": procs or docker_ports.get(port, [])}

    rows = []
    for port, info in sorted(exposed.items()):
        w = whitelist.get(port)
        if not w:
            state, note = "未知", "不在 devcloud-exposure/whitelist.json"
        else:
            ident = json.dumps(w.get("identity", {}), ensure_ascii=False)
            names = info["procs"]
            hit = any(n and (n in ident or n in w.get("service", "")) for n in names)
            if port in docker_ports:
                hit = any(w.get("identity", {}).get("container") == n for n in names)
            state = "白名单命中" if hit else "仅端口命中(身份待核)"
            note = f"{w.get('service')} / verify={w.get('verify')}"
        rows.append({**info, "whitelist": state, "note": note})

    return {
        "cleaner": {"consecutive_failures": consecutive_failures,
                    "last": {k: last.get(k) for k in ("ts", "mode", "status", "scan",
                                                      "deleted", "elapsed_seconds")},
                    "summaries_seen": len(summaries)},
        "docker_df": docker_lines,
        "listeners": rows,
    }


def render_audit(summary, executed, unreclaimed, by_reason, verify, failed) -> str:
    L = []
    L.append(f"\n## {datetime.now().strftime('%Y-%m-%d')} — Worktree 巡检"
             f"（脚本轮 {summary['run_id']}；执行 {summary['executed_count']} 项 / "
             f"{fmt_bytes(summary['executed_bytes'])}）\n")
    L.append(f"- 规则：age ≥ {summary['min_age_hours']:g}h + 远端/主干已有 + 干净无引用无 BB 归属；"
             f"执行器 worktree-cleanup（非 --force、不删分支）。")
    L.append(f"- 采集：登记 {summary['registered_entries']}，可访问链接 {summary['linked_present']}，"
             f"测量 {summary['measured']} / 缓存复用 {summary['reused']}，"
             f"who-uses 串行 {summary['who_uses_probed']}。")
    L.append(f"- 产物：`{summary['round_dir']}/{{evidence.json,summary.json,audit_section.md}}`（按 owner 决定不产出 HTML）。\n")
    L.append("### 已执行\n")
    if not executed:
        L.append(f"无（eligible={summary['eligible_count']}，失败 {summary['failed_count']}）。")
    else:
        L.append("| 路径 | HEAD | age | PR/MR | 回收前表观字节 |")
        L.append("|---|---|---|---|---|")
        for x in executed:
            L.append(f"| {x['path']} | {x['head'][:12]} | {x['age_days']}d | "
                     f"#{x['pr']} | {x['size_bytes']:,} |")
        if summary.get("gongfeng_executed_count"):
            L.append(f"- 其中工蜂（git.woa.com）{summary['gongfeng_executed_count']} 项经 "
                     f"`--remote-evidence` 走同一 owner 执行器（非 --force、不删分支）。")
        for v in verify:
            L.append(f"- 复核 {v['path']}：commit={v['commit_type']}，"
                     f"分支{'已删（政策默认）' if not v.get('branch_present') else '未删（需关注）'}")
    if failed:
        L.append("\n失败：" + "; ".join(f"{f['path']}: {f['error']}" for f in failed))
    if summary.get("gongfeng_blocked_count"):
        L.append(f"- 工蜂已核验但被 git 拒绝非 --force 移除 "
                 f"{summary['gongfeng_blocked_count']} 项（含 submodule，需 owner 决定）。")
    L.append("\n### 未执行（按原因）\n")
    for r, v in sorted(by_reason.items(), key=lambda kv: -kv[1]["bytes"]):
        L.append(f"- `{r}` {v['count']} 项 / {fmt_bytes(v['bytes'])}")
    L.append(f"\n≥7 天未清理合计 {summary['unreclaimed_over_7d_count']} 项 / "
             f"{fmt_bytes(summary['unreclaimed_over_7d_bytes'])}；"
             f"<7 天保留 {summary['held_under7_count']} 项 / "
             f"{fmt_bytes(summary['held_under7_bytes'])}；"
             f"prunable 无实体登记 {summary.get('prunable_registrations', 0)} 项；"
             f"累计回收 {fmt_bytes(summary['cumulative_reclaimed_bytes'])}"
             f"（自 {summary['cumulative_since']}）。")
    L.append(f"\n### 基础设施健康\n")
    L.append(f"- clean-tmp 连续失败 {summary.get('cleaner_consecutive_failures', '?')} 天。")
    L.append(f"- 非回环监听中未命中白名单 {summary.get('listeners_unknown', '?')} 个。")
    L.append(f"- 增量：测量 {summary['measured']} / 缓存复用 {summary['reused']}；"
             f"who-uses 串行 {summary['who_uses_probed']}。")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
