#!/usr/bin/env python3
"""Worktree 巡检的确定性执行器：采集 → 增量指纹 → 判定 → （可选）执行 → 预聚合输出。

设计目标：把可脚本化的部分全部脚本化，让调用它的 Agent 只读一段紧凑输出并写判断，
将证据与分类留在结构化产物中，报告轮无需重复调查。

每轮重新核对登记项、年龄、远端持久证明、目录体积和进程引用。
状态账本只保留身份、历史回收与异常去重；Git 状态不能证明 ignored 内容未变，
不复用旧体积冒充本轮测量。`--execute` 时由 owner 工具在 sink 前复验指纹。

只读是默认；`--execute` 才调用 owner 工具的 `--apply --approve-report`。
禁止 --force / rm -rf / git clean；分支删除仅由 owner 执行器按远端证明处理。
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from typing import Any
from urllib.parse import urlsplit
from datetime import datetime, timezone

# ---------------------------------------------------------------- 常量

DEVCLOUD = "/data/tools/devcloud-maintenance/releases/cf2bbbefdbdee7af/bin/devcloud.py"
WORKTREE_TOOL = None
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
        raise RuntimeError(f"仓库发现失败，退出 {rc}")
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


def worktree_entries(repo: str) -> tuple[list[dict], bool]:
    """返回 (entries, scan_ok)；扫描失败不再是“无工作树”，不能淘汰状态账本。"""
    rc, out, _ = run(["git", "-C", repo, "worktree", "list", "--porcelain"], timeout=60)
    if rc != 0:
        return [], False
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
    return entries, True


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
    lines = [line for line in porcelain.splitlines() if line.strip()]
    if not lines:
        return None
    tracked = sum(1 for line in lines if not line.startswith("??"))
    untracked = sum(1 for line in lines if line.startswith("??"))
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
    if "://" in url:
        parsed = urlsplit(url)
        host, path = parsed.hostname, parsed.path.lstrip("/")
    else:
        match = re.fullmatch(r"(?:[^@/:]+@)?([^/:]+):(.+)", url)
        if not match:
            return None, "unknown"
        host, path = match.groups()
    path = path.rstrip("/").removesuffix(".git")
    return (path or None), (host.lower() if host else "unknown")


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


def gongfeng_lookup(project, branch, token, timeout=60, head=None):
    """按源分支查工蜂 MR：opened 优先；merged 中优先精确 HEAD，查询全部分页。

    注意 `--state all` 在 0.1.0 上恒返回 0 条（实测），必须逐状态查询。
    """
    if not project or not branch or branch == "(detached)":
        return {"state": "unknown", "reason": "无分支名，无法按源分支查询 MR"}
    env = os.environ.copy()
    env["GONGFENG_PRIVATE_TOKEN"] = token
    binary = gongfeng_bin()
    for state in ("opened", "merged", "closed"):
        rc, out, err = run([binary, "mr", "list", project, "--state", state,
                            "--source-branch", branch, "--per-page", "100", "--all", "--json"],
                           timeout=timeout, env=env)
        if rc != 0:
            return {"state": "error", "reason": f"gongfengcli 退出 {rc}"}
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            return {"state": "error", "reason": "gongfengcli 输出不是 JSON"}
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            return {"state": "error", "reason": "工蜂接口拒绝或响应结构无效"}
        rows = payload.get("result")
        if not isinstance(rows, list) or any(not isinstance(mr, dict) for mr in rows):
            return {"state": "error", "reason": "工蜂 MR 列表结构无效"}
        matches = [mr for mr in rows if mr.get("source_branch") == branch
                   and mr.get("state", state) == state]
        matches.sort(key=lambda mr: (bool(head and mr.get("source_commit") == head),
                                     str(mr.get("updated_at") or "")), reverse=True)
        for mr in matches:
            return {
                "state": str(mr.get("state") or state),
                "iid": mr.get("iid"),
                "title": mr.get("title"),
                "source_branch": mr.get("source_branch"),
                "target_branch": mr.get("target_branch"),
                "source_commit": mr.get("source_commit"),
                "merge_commit_sha": mr.get("merge_commit_sha"),
                "updated_at": mr.get("updated_at"),
                "url": (f"https://git.example.invalid/{project}/-/merge_requests/{mr.get('iid')}"
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
        if not isinstance(data, dict):
            raise ValueError("expected object")
    except (json.JSONDecodeError, ValueError):
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



def owner_object_index(objects):
    """登记路径可以含符号链接；关联仍要求同仓、同实体路径、同完整 HEAD。"""
    return {(os.path.realpath(o["repo"]), os.path.realpath(o["path"]), o.get("head")): o
            for o in objects}


def exact_head(source, head):
    return isinstance(head, str) and bool(re.fullmatch(r"[0-9a-f]{40}", head)) and source == head


def local_blockers(o, bb_query_ok=True):
    reasons = []
    if o.get("dirty") is None:
        reasons.append("工作区状态未知")
    elif o["dirty"]:
        reasons.append("dirty(未提交)")
    if "ignored" in o and o["ignored"] is None:
        reasons.append("ignored 清单未知")
    if o.get("locked"):
        reasons.append("locked")
    if o.get("prunable"):
        reasons.append("prunable")
    if not bb_query_ok:
        reasons.append("BB 归属查询失败")
    if o.get("bb_environment") or o.get("bb_threads"):
        reasons.append("活跃 BB 环境")
    count = (o.get("who_uses") or {}).get("count")
    if type(count) is not int or count < 0:
        reasons.append("进程引用未知")
    elif count:
        reasons.append("其他(活跃进程引用)")
    return reasons


def probe_usage(path, method="serial"):
    probe = path
    if path.startswith("/tmp/"):
        candidate = "/data/system-tmp/" + path[len("/tmp/"):]
        if os.path.exists(candidate):
            probe = candidate
    rc, out, _ = run([sys.executable, "-B", DEVCLOUD, "who-uses", "--json", probe], timeout=180)
    try:
        result = json.loads(out)
    except json.JSONDecodeError:
        result = None
    count = result.get("count") if isinstance(result, dict) and rc == 0 else None
    if type(count) is not int or count < 0 or (isinstance(result, dict) and result.get("error")):
        count = None
    return {"count": count, "probe_path": probe, "at": now_iso(), "method": method,
            "error": "进程引用探测失败" if count is None else None}


def classify_retained(o, bb_query_ok=True, execute=False):
    """保留原因只含阻塞项；平台与 PR/MR 事实单独展示。所有输出共用此判定。"""
    reasons = local_blockers(o, bb_query_ok)
    host = o.get("remote_host")
    gf = o.get("gongfeng") or {}
    state = gf.get("state")
    skip = str(o.get("skip_reason") or "")
    remote_status = "远端不可证"
    if host == "git.example.invalid":
        if state == "merged":
            if exact_head(gf.get("source_commit"), o.get("head")):
                remote_status = "MR merged(工蜂核验·精确 head)"
            else:
                remote_status = "MR merged(工蜂核验·非精确 head)"
                reasons.append("MR HEAD 不匹配")
        else:
            label = {"opened": "MR-open", "closed": "MR closed(未合并)",
                     "none": "未找到 MR", "unknown": "detached HEAD，MR 无法按分支查询",
                     "error": "MR 查询失败", "no-credential": "凭据缺失"}.get(state, "MR 未核验")
            remote_status = label
            reasons.append(label)
    elif host == "github.com":
        if str((o.get("pr") or {}).get("state", "")).upper() == "OPEN":
            reasons.append("PR-open")
            remote_status = "PR-open"
        elif o.get("owner_error"):
            reasons.append("执行器查询失败")
        elif "ambiguous" in skip:
            reasons.append("工具判 ambiguous")
            remote_status = "PR 关联存在歧义"
        elif "is not contained in the PR remote head" in skip:
            reasons.append("关联证据(非精确 head)")
            remote_status = "本地 HEAD 不在 PR 远端提交谱系中"
        elif o.get("remote_proof"):
            remote_status = (o["remote_proof"].get("description") or "远端已核验")
        elif not o.get("owner_eligible"):
            if skip == "owner 工具未评估":
                reasons.append("执行器未评估")
                remote_status = "执行器未返回该对象的结论"
            elif any(text in skip for text in ("lookup failed", "durability check failed", "no completed PR")):
                reasons.append("远端不可证")
            elif reasons:
                remote_status = "未核验（本地条件未通过）"
            elif skip:
                reasons.append("执行器保留：" + skip)
            else:
                reasons.append("远端不可证")
        if skip and not reasons:
            reasons.append("执行器保留：" + skip)
    else:
        reasons.append("未配置 origin" if host is None else "不支持的远端 host")
    if o.get("submodule_blocked") or o.get("gongfeng_blocked"):
        reasons.append("含 submodule")
    if o.get("execution_error"):
        reasons.append("执行失败")
    if o.get("age_hours") is None:
        reasons.append("年龄不可得")
    if not reasons:
        reasons.append("本轮未执行" if execute else "仅预览，未执行")
    o["remote_status"] = remote_status
    o["reasons"] = list(dict.fromkeys(reasons))
    o["reason"] = ";".join(o["reasons"])
    # 未找到 MR、查询失败、在办工作均按现有规则保留；只有策略需人选择才进决策队列。
    o["needs_decision"] = any(r in reasons for r in ("工具判 ambiguous", "含 submodule"))
    o["decision"] = "需决策" if o["needs_decision"] else "不清理"
    actions = {
        "dirty(未提交)": "保留；提交或妥善保存改动后自动重评",
        "活跃 BB 环境": "保留；环境/线程结束后自动重评",
        "其他(活跃进程引用)": "保留；进程释放引用后自动重评",
        "MR-open": "保留；MR 合并后自动重评", "PR-open": "保留；PR 结束后自动重评",
        "MR HEAD 不匹配": "保留；核对当前 HEAD 与已合并 MR 的差异",
        "未找到 MR": "保留；当前分支未查到 MR，不能据此断定提交未推送",
        "detached HEAD，MR 无法按分支查询": "保留；按当前 HEAD 核对远端提交归属",
        "含 submodule": "由 owner 决定继续保留或处理子模块；巡检不强制删除",
        "工具判 ambiguous": "由 owner 核对关联 PR，明确应使用的远端证据",
        "仅预览，未执行": "满足巡检条件；下次执行轮由 owner 工具复验",
        "本轮未执行": "保留；核对本轮执行器回报",
        "执行器未评估": "保留；核对 raw/owner_tool 中该仓库登记路径与执行器输出",
    }
    priority = next((r for r in ("含 submodule", "工具判 ambiguous", "执行失败") if r in reasons), reasons[0])
    o["next_step"] = actions.get(priority, "保留；" + (o.get("execution_error") or priority) + "，下轮重新核验")
    return o


def project_totals(unreclaimed, executed):
    rows = {}
    for o in unreclaimed + executed:
        rows.setdefault(o["repo"], {"repo": o["repo"], "executed_count": 0,
                                  "unreclaimed_count": 0, "unreclaimed_bytes": 0,
                                  "unknown_size_count": 0, "needs_decision_count": 0, "reasons": {}})
    for o in executed:
        rows[o["repo"]]["executed_count"] += 1
    for o in unreclaimed:
        row = rows[o["repo"]]
        row["unreclaimed_count"] += 1
        row["unreclaimed_bytes"] += o.get("size_bytes") or 0
        row["unknown_size_count"] += o.get("size_bytes") is None
        row["needs_decision_count"] += bool(o.get("needs_decision"))
        for reason in o["reasons"]:
            item = row["reasons"].setdefault(reason, {"count": 0, "bytes": 0})
            item["count"] += 1
            item["bytes"] += o.get("size_bytes") or 0
    for row in rows.values():
        row["main_reasons"] = sorted(row["reasons"], key=lambda r: (
            -row["reasons"][r]["count"], -row["reasons"][r]["bytes"], r))[:2]
    return [rows[k] for k in sorted(rows)]


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
        if gf.get("state") != "merged" or not exact_head(source, head):
            continue
        if local_blockers(o):
            continue
        project, host = remote_slug(o["repo"])
        if host != "git.example.invalid":
            continue
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

    执行器仍是唯一 sink（内部 `git worktree remove` 非 --force、分支按远端证明删除、`.local`
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
        if not isinstance(data, dict):
            raise ValueError("expected object")
    except (json.JSONDecodeError, ValueError):
        data = {"error": (err or out or "no output")[:400], "removed": 0,
                "entries": [], "failed": len(evidence.get("entries") or [])}
    if rc != 0:
        data["error"] = data.get("error") or f"执行器退出 {rc}"
    data["evidence"] = epath
    data["dry_run_exit"] = dry_rc
    return data


def evaluate_run_health(
    prev: dict[str, dict], current: list[dict], now: str
) -> dict[str, Any]:
    """异常去重（纯函数）：新异常首次通知，持续静默，恢复通知一次。

    key 是稳定标识（code + entity），中文 detail 仅展示；措辞变更不影响匹配。
    已恢复项保留 resolved_at 供账本审计。"""
    current_keys = {e["key"]: e for e in current}
    health: dict[str, Any] = {"exceptions": {}, "notifiable": [], "recovered": []}
    for key, exc in current_keys.items():
        ent = dict(prev.get(key) or {})
        first = ent.get("first_seen") or now
        # 首次出现，或恢复后再次出现 → 重新通知
        newly_notified = not ent.get("notified") or bool(ent.get("resolved_at"))
        ent.update(exc)
        ent["first_seen"] = first
        ent["last_seen"] = now
        ent["resolved_at"] = None
        ent["notified"] = True
        health["exceptions"][key] = ent
        if newly_notified:
            health["notifiable"].append(ent)
    for key, ent in prev.items():
        if key not in current_keys and not ent.get("resolved_at"):
            closed = dict(ent, resolved_at=now)
            health["exceptions"][key] = closed
            health["recovered"].append(closed)
    return health


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
    ap.add_argument("--worktree-tool", required=True,
                    help="owner 工具路径（测试时可换 stub）")
    ap.add_argument("--devcloud", default=DEVCLOUD, help="devcloud.py 路径（测试时可换 stub）")
    ap.add_argument("--mirror-root", default="/data/code/mole-linux/ai-storage-maintenance",
                    help="仓库镜像根（默认按 AGENTS.md 约定）")
    ap.add_argument("--no-mirror", action="store_true", help="不写仓库镜像（隔离测试时用）")
    args = ap.parse_args()
    if args.min_age_hours < MIN_AGE_HOURS or args.jobs < 1:
        ap.error("min-age-hours 必须至少 168，jobs 必须为正数")
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
            round_dir = f"{base}-{datetime.now().strftime('%H%M%S-%f')}"
    os.makedirs(os.path.join(round_dir, "raw", "owner_tool"), exist_ok=True)

    state = load_state(args.state)
    if args.force_fresh:
        state["worktrees"] = {}

    print(f"[patrol] run_id={run_id} round={round_dir}")
    print(f"[patrol] state={args.state} cached_worktrees={len(state['worktrees'])}")

    roots = [r for r in args.roots.split(",") if r]
    repos = []
    discovery_errors = []
    for r in roots:
        try:
            repos.extend(discover_repos(r))
        except RuntimeError as exc:
            discovery_errors.append({"root": r, "error": str(exc)})
    repos = sorted(set(repos))
    print(f"[patrol] repos={len(repos)}")

    # --- 1. 便宜必查：登记项 + 指纹 ---
    # 默认只看"含链接工作树"的仓库：本巡检的处置对象是 worktree，主干本体永久保留，
    # 其可重建产物只是 review-only 背景值。全量扫 168 个仓库会稀释口径并浪费 5 倍时间。
    all_entries = []
    scan_ok_repos: set[str] = set()
    scan_failed_repos: list[str] = []
    for repo in repos:
        repo_entries, scan_ok = worktree_entries(repo)
        if not scan_ok:
            scan_failed_repos.append(repo)
            continue
        scan_ok_repos.add(repo)
        for e in repo_entries:
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
            if not isinstance(bb_envs, list) or any(not isinstance(e, dict) for e in bb_envs):
                bb_envs, bb_query_ok = [], False
        except json.JSONDecodeError:
            bb_query_ok = False
    else:
        bb_query_ok = False
    rc, out, _ = run(["bb", "thread", "list", "--json"], timeout=90)
    if rc == 0:
        try:
            bb_threads = json.loads(out)
            if not isinstance(bb_threads, list) or any(not isinstance(t, dict) for t in bb_threads):
                bb_threads, bb_query_ok = [], False
        except json.JSONDecodeError:
            bb_query_ok = False
    else:
        bb_query_ok = False
    env_by_path = {os.path.realpath(e["path"]): e for e in bb_envs if e.get("path")}
    threads_by_path: dict[str, list] = {}
    for t in bb_threads:
        p = t.get("environmentPath")
        if p and not t.get("archivedAt") and not t.get("deletedAt"):
            threads_by_path.setdefault(os.path.realpath(p), []).append({"id": t["id"], "status": t["status"]})

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
            if any(not line.startswith("??") for line in sout.splitlines() if line.strip()):
                rc3, sstat, _ = run(["git", "-C", path, "diff", "HEAD", "--shortstat"], timeout=120)
                if rc3 == 0:
                    shortstat = sstat.strip()
            obj["dirty_summary"] = summarize_dirty(sout, shortstat)
        obj["ignored"] = {"count": ignored["count"], "sample": ignored["sample"],
                          "digest": ignored["digest"]} if ignored else None
        fp = fingerprint(entry, dirty, ignored) if ignored_err is None else None
        obj["fingerprint"] = fp
        cached = state["worktrees"].get(path)
        # Git 状态与 ignored 根集合不能证明目录内容/体积未变；每轮重新测量。
        b, last_active = du_bytes_last_active(path, timeout=180)
        ab, hits = artifact_bytes(path)
        obj.update(size_bytes=b, last_active_at=last_active, artifact_bytes=ab,
                   artifact_hits=sorted(hits, key=lambda x: -x["kb"])[:20],
                   size_source="measured", size_measured_at=now_iso())
        measured += 1
        env = env_by_path.get(os.path.realpath(path))
        obj["bb_environment"] = env["id"] if env else None
        obj["bb_threads"] = threads_by_path.get(os.path.realpath(path), [])
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
    # 扫描失败 ≠ 仓库消失：只在本轮完整覆盖（scan_ok）的仓库范围内淘汰旧记录。
    scan_ok_abspaths = {os.path.abspath(r) for r in scan_ok_repos}
    for p in list(state_new["worktrees"]):
        rec_repo = (state_new["worktrees"][p] or {}).get("repo") or ""
        if os.path.abspath(rec_repo) not in scan_ok_abspaths:
            continue
        if p not in live_paths:
            state_new["worktrees"].pop(p, None)
            removed_state += 1
    print(f"[patrol] entries={len(objects)} measured={measured} reused={reused} "
          f"state_dropped={removed_state}")

    # --- 2. owner 工具远端核对（决定量，必须每轮做）---
    repos_with_linked = sorted({e["_repo"] for e in raw_entries})
    ot_dir = os.path.join(round_dir, "raw", "owner_tool")
    host_by_repo = {r: remote_slug(r)[1] for r in repos_with_linked}
    for o in objects:
        o["remote_host"] = host_by_repo.get(o["repo"])
    ot_reports, owner_eligible = {}, []
    with cf.ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(owner_tool_dry_run, r, ot_dir, args.min_age_hours)
                for r in repos_with_linked if host_by_repo[r] == "github.com"]
        for fut in futs:
            repo, slug, rc, data, apath = fut.result()
            _, host = remote_slug(repo)
            ot_reports[slug] = {"repo": repo, "host": host, "exit": rc,
                                "error": data.get("error"), "eligible": data.get("eligible"),
                                "entries": data.get("entries") or [], "approval_report": apath}
    obj_by_path = {o["path"]: o for o in objects}
    owner_objects = owner_object_index(objects)
    for slug, rep in ot_reports.items():
        for e in rep["entries"]:
            if not e.get("path"):
                continue
            o = owner_objects.get((os.path.realpath(rep["repo"]),
                                   os.path.realpath(e["path"]), e.get("head")))
            if not o:
                continue
            o["owner_path"] = e["path"]
            o["owner_eligible"] = e.get("action") == "would-remove"
            o["skip_reason"] = e.get("skip_reason")
            o["pr"] = e.get("pr")
            o["remote_proof"] = e.get("remote_proof")
            o["approval_token"] = e.get("approval_token")
            if e.get("action") == "would-remove":
                owner_eligible.append({"path": o["path"], "repo": rep["repo"],
                                       "owner_path": e["path"],
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
            if o["remote_host"] == "github.com":
                o["skip_reason"] = "owner 工具未评估"

    # --- 2b. 非 GitHub host：用 gongfengcli 核验 MR ---
    # token 只在本进程内解析并经 env 传给子进程；不进 argv、不进任何产物。
    host_by_repo = {r: remote_slug(r)[1] for r in repos_with_linked}
    gf_targets = [o for o in objects
                  if not o["primary"] and o["exists_now"]
                  and host_by_repo.get(o["repo"]) == "git.example.invalid"
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
            key = (o["repo"], o["branch"], o["head"])
            if key not in cache:
                project = remote_slug(o["repo"])[0]
                cache[key] = gongfeng_lookup(project, o["branch"], gf_token, head=o["head"])
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
        o["who_uses"] = probe_usage(o.get("owner_path") or o["path"])
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
    owner_errors = {rep["repo"]: rep.get("error") or f"退出 {rep['exit']}"
                    for rep in ot_reports.values() if rep.get("error") or rep.get("exit")}
    for o in objects:
        o["owner_error"] = owner_errors.get(o["repo"])
        if o.get("primary") or not o.get("exists_now"):
            continue
        if (o.get("age_hours") or 0) >= args.min_age_hours:
            gf = o.get("gongfeng") or {}
            remote_ok = (o.get("owner_eligible") and not o.get("owner_error")) or (
                o.get("remote_host") == "git.example.invalid" and gf.get("state") == "merged"
                and exact_head(gf.get("source_commit"), o.get("head")))
            if remote_ok and not local_blockers(o, bb_query_ok):
                o["submodule_blocked"] = worktree_has_submodules(o["path"])
                if gf.get("state") == "merged":
                    verified_reclaimable.append(o)
            o["eligible"] = bool(remote_ok and not local_blockers(o, bb_query_ok)
                                 and not o.get("submodule_blocked"))
    eligible_paths = {o["path"] for o in objects if o.get("eligible")}
    for o in objects:
        if o["primary"]:
            continue
        if not o["exists_now"]:
            if o["prunable"]:
                prunable_regs.append(o)
            continue
        if o["age_hours"] is not None and o["age_hours"] < args.min_age_hours:
            held_under7.append(o)
            o["decision"], o["reason"], o["reasons"] = "保留(<7天)", "age<7d", ["age<7d"]
            continue
        classify_retained(o, bb_query_ok, args.execute)
        unreclaimed.append(o)

    age_unknown = [o for o in unreclaimed if o["age_hours"] is None]
    unreclaimed = [o for o in unreclaimed if o["age_hours"] is not None]
    unreclaimed.sort(key=lambda x: -(x["size_bytes"] or 0))
    unreclaimed_bytes = sum(o["size_bytes"] or 0 for o in unreclaimed)
    held_bytes = sum(o["size_bytes"] or 0 for o in held_under7)

    # 孤儿分支清扫统计（执行器 apply 内建，GitHub 与工蜂两跳都收）
    orphan_branches_removed: list[dict] = []
    apply_touched_repos: set[str] = set()

    # --- 5. 执行（仅在 --execute 且条件齐备时）---
    executed_this_round, failed = [], []
    executable = [e for e in owner_eligible if e["path"] in eligible_paths]

    # 两种 host 共用动态引用复验，未知状态不会变成“没有引用”。
    if args.execute:
        for o in objects:
            if o.get("eligible"):
                o["who_uses"] = probe_usage(o.get("owner_path") or o["path"], "execute-refresh")
                if local_blockers(o, bb_query_ok):
                    o["eligible"] = False
                    eligible_paths.discard(o["path"])
        executable = [e for e in executable if e["path"] in eligible_paths]

    if args.execute and executable:
        by_report: dict[str, list] = {}
        for e in executable:
            by_report.setdefault(e["approval_report"], []).append(e)
        for report, items in by_report.items():
            if not report:
                continue
            apply_touched_repos.add(items[0]["repo"])
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
            for item in (res.get("branch_sweep") or {}).get("branches_removed") or []:
                orphan_branches_removed.append(
                    {"repo": items[0]["repo"], "branch": item["branch"], "proof": item.get("proof")})
            if rc != 0:
                failed.append({"repo": items[0]["repo"], "error": res.get("error") or f"执行器退出 {rc}"})
            for e in items:
                ent = next((x for x in res.get("entries", []) if x.get("path") == e["owner_path"]), {})
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
                    failed.append({"path": e["path"], "error": ent.get("error") or ent.get("skip_reason") or res.get("error") or "执行器未执行"})

    # --- 5b. 工蜂（非 GitHub host）：证据经 owner 工具 --remote-evidence 走同一 sink ---
    # 唯一执行器不变（`git worktree remove` 非 --force、.local 备份、apply 前指纹复验）；
    # 分支按 owner 2026-09-19 政策默认删除（远端证明就是删除分支的依据）；
    # 工蜂场景只是把「远端已有」的证人从 gh API 换成 gongfengcli 核验结果，sink 端仍按完整对象名全等校验。
    verified_remaining = list(verified_reclaimable)
    gongfeng_blocked_items = []
    attemptable = []
    for o in verified_reclaimable:
        if o.get("submodule_blocked"):
            # git worktree remove（非 --force）拒绝含 submodule 的工作树；提前判定，
            # 不每轮重复撞同一个拒绝，也不绕过它。
            o["gongfeng_blocked"] = "含 submodule：git 拒绝非 --force 移除（需 owner 决定）"
            gongfeng_blocked_items.append(o)
        elif o["path"] in eligible_paths:
            attemptable.append(o)
    if args.execute and attemptable:
        gongfeng_executed_paths: set[str] = set()
        for repo, ev in sorted(build_gongfeng_evidence(attemptable).items()):
            apply_touched_repos.add(repo)
            res = owner_tool_gongfeng_apply(repo, ot_dir, ev, args.min_age_hours)
            if res.get("error"):
                failed.append({"repo": repo, "error": res["error"]})
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

    # --- 5c. 全仓孤儿分支清扫（owner 2026-09-19 分支政策的独立入口）---
    # worktree 消失的仓库不再有 apply 触发点；对全部扫描仓库统一跑 sweep-only
    # （已随 apply 清扫过的 repo 跳过），避免「最后一个 worktree 消失后仓库
    # 退出分支治理范围」。
    if args.execute:
        sweep_candidates = []
        for repo in repos:
            if repo in apply_touched_repos:
                continue
            # 本地预过滤：无「未被认领且非主干」分支的仓库没有清扫候选，
            # 直接跳过，避免对 169 个仓库逐个做远端 fetch。
            claimed = {e.get("branch", "") for e in worktree_entries(repo)[0]}
            rc_b, out_b, _ = run(
                ["git", "-C", repo, "branch", "--format=%(refname:short)"], timeout=60)
            locals_ = [b.strip() for b in (out_b or "").splitlines() if b.strip()]
            if not [b for b in locals_
                    if b not in claimed and b not in ("main", "master")]:
                continue
            sweep_candidates.append(repo)

        def run_repo_sweep(repo: str) -> None:
            rc, out, err = run(
                [sys.executable, WORKTREE_TOOL, "--repo", repo,
                 "--sweep-only", "--apply", "--json"], timeout=300)
            if rc != 0:
                failed.append({"repo": repo, "error":
                               ("孤儿分支清扫失败: " + (err or out or f"退出 {rc}"))[:200]})
                return
            try:
                res = json.loads(out)
            except json.JSONDecodeError:
                failed.append({"repo": repo, "error": "孤儿分支清扫输出非 JSON"})
                return
            for item in (res.get("branch_sweep") or {}).get("branches_removed") or []:
                orphan_branches_removed.append(
                    {"repo": repo, "branch": item["branch"], "proof": item.get("proof")})

        with cf.ThreadPoolExecutor(max_workers=4) as ex:
            list(ex.map(run_repo_sweep, sweep_candidates))
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

    # --- 5d. prunable 注册清理（owner 授权：--execute 时自动 prune）---
    # git 已确认 gitdir 指向的目录不存在，prune 只删失效元数据，零数据丢失。
    prunable_pruned: list[dict] = []
    if args.execute and prunable_regs:
        for repo in sorted({o["repo"] for o in prunable_regs}):
            rc, out, err = run(["git", "-C", repo, "worktree", "prune", "--verbose"],
                               timeout=120)
            # git 2.43 实测：prune --verbose 的 "Removing…" 行走 stderr
            merged = ((out or "") + "\n" + (err or "")).splitlines()
            removed_lines = [line.strip() for line in merged
                             if line.strip().startswith("Removing ")]
            prunable_pruned.append({
                "repo": repo, "removed": len(removed_lines) if rc == 0 else 0,
                "detail": removed_lines[:10],
                "error": None if rc == 0 else (err or out or f"git 退出 {rc}")[:200],
            })

    # --- 7. 运行结果协议（异常驱动；采纳 Oracle 二轮 #1）---
    run_exceptions: list[dict] = []
    if not bb_query_ok:
        run_exceptions.append({"key": "bb-query-failed", "code": "bb-query-failed",
                               "detail": "BB 环境/线程查询失败，本轮候选全部保守保留"})
    if gf_targets and not gf_token:
        run_exceptions.append({"key": "gongfeng-credential-missing",
                               "code": "gongfeng-credential-missing",
                               "detail": str(gf_source)[:160]})
    gf_err_n = sum(1 for o in objects if (o.get("gongfeng") or {}).get("state") == "error")
    if gf_err_n:
        run_exceptions.append({"key": "gongfeng-mr-error", "code": "gongfeng-mr-error",
                               "detail": f"{gf_err_n} 项工蜂 MR 查询失败"})
    ot_err_github: list[str] = []
    for rep, v in ot_reports.items():
        if v.get("error") or v.get("exit"):
            host = v.get("host") or "unknown"
            if host != "github.com":
                # 非 GitHub host 的 gh 失败是设计内路径（5b 工蜂证据接管），非异常
                continue
            ot_err_github.append(rep)
    if ot_err_github:
        run_exceptions.append({"key": "owner-tool-error:github", "code": "owner-tool-error",
                               "entity": "github",
                               "detail": f"{len(ot_err_github)} 个 GitHub 仓库 owner 工具失败: "
                                         + ", ".join(ot_err_github[:5])})
    for f in failed:
        is_sweep = "孤儿分支清扫" in str(f.get("error") or "")
        ent = f.get("repo") or f.get("path") or ""
        run_exceptions.append({
            "key": f"{'sweep-failed' if is_sweep else 'execute-failed'}:{ent}",
            "code": "sweep-failed" if is_sweep else "execute-failed",
            "entity": ent, "detail": str(f.get("error") or "")[:160]})
    for v in verify:
        if v.get("commit_type") == "missing":
            run_exceptions.append({"key": f"verify-commit-missing:{v['path']}",
                                   "code": "verify-commit-missing", "entity": v["path"],
                                   "detail": "回收后无法读取提交对象，需核对执行器回报"})
        if v.get("branch_anomaly"):
            run_exceptions.append({"key": f"verify-branch-present:{v['path']}",
                                   "code": "verify-branch-present", "entity": v["path"],
                                   "detail": "回收后分支仍在（删除失败）"})
    for o in need_who:
        if (o.get("who_uses") or {}).get("count") is None:
            run_exceptions.append({"key": f"who-uses-failed:{o['path']}", "code": "who-uses-failed",
                                   "entity": o["path"], "detail": "进程引用未知，本轮保留"})
    for pruned in prunable_pruned:
        if pruned.get("error"):
            run_exceptions.append({"key": f"prune-failed:{pruned['repo']}", "code": "prune-failed",
                                   "entity": pruned["repo"], "detail": pruned["error"]})
    for item in discovery_errors:
        run_exceptions.append({"key": f"discovery-failed:{item['root']}", "code": "discovery-failed",
                               "entity": item["root"], "detail": item["error"]})
    for repo in scan_failed_repos:
        run_exceptions.append({"key": f"scan-failed:{repo}", "code": "scan-failed",
                               "entity": repo,
                               "detail": "worktree list 失败，本轮不淘汰该仓状态记录"})

    prev_health = state_new.get("run_health") or {}
    health = evaluate_run_health(prev_health.get("exceptions") or {}, run_exceptions, now_iso())
    state_new["run_health"] = {"exceptions": health["exceptions"]}

    prune_total = sum(p["removed"] for p in prunable_pruned)
    run_failed = bool(failed) or bool(scan_failed_repos) or bool(discovery_errors) or any(p.get("error") for p in prunable_pruned)
    run_status = {
        "schema": "worktree-patrol-run/v1",
        "run_id": run_id,
        "finished_at": now_iso(),
        "duration_s": round(time.time() - started, 1),
        "mode": "execute" if args.execute else "dry-run",
        "status": "failed" if run_failed else ("degraded" if run_exceptions else "success"),
        "coverage": {"repos": len(repos), "entries": len(objects),
                     "scan_failed_repos": scan_failed_repos, "discovery_errors": discovery_errors,
                     "owner_tool_errors": sum(1 for v in ot_reports.values() if v.get("error"))},
        "executed": {"worktrees": len(executed_this_round),
                     "branches_worktree": sum(1 for x in executed_this_round if x.get("branch_deleted")),
                     "branches_orphan": len(orphan_branches_removed),
                     "prunable_pruned": prune_total},
        "exceptions": run_exceptions,
        "notifiable": health["notifiable"],
        "recovered": health["recovered"],
    }
    state_new["updated_at"] = now_iso()

    # --- 5c. 分支价值信号：相对远端默认分支的 ahead/behind + 独有 diff 规模 ---
    # 借鉴 gmc wt list --diff-base；只对进入决策表的 ≥7 天对象计算，不作删除授权。
    for o in unreclaimed:
        if o.get("branch"):
            o["standing"] = branch_standing(o["repo"], o["branch"])

    for f in failed:
        if f.get("path") in obj_by_path:
            obj_by_path[f["path"]]["execution_error"] = f.get("error") or "执行器未返回成功"
    for o in unreclaimed:
        classify_retained(o, bb_query_ok, args.execute)
    for x in executed_this_round:
        o = obj_by_path[x["path"]]
        o.update(decision="已清理", reasons=[], reason="已清理", needs_decision=False,
                 exists_now=False, next_step="已完成")
        state_new["worktrees"].pop(x["path"], None)

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
        pr = pr or o.get("gongfeng") or {}
        if pr.get("title"):
            return f"{pr['title']}（推断）"
        return f"{o['branch']}（推断）" if o["branch"] else "无描述"

    _web_cache: dict[str, str] = {}

    def remote_web(repo: str):
        if repo in _web_cache:
            return _web_cache[repo] or None
        project, host = remote_slug(repo)
        web = f"{host}/{project}" if host and project else ""
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
    for o in unreclaimed + verified_reclaimable + age_unknown:
        o["pr_url"] = pr_url(o)
        o["function"] = func_of(o)
        o.setdefault("next_step", "保留")
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
        "eligible_count": len(eligible_paths),
        "owner_eligible_count": len(owner_eligible),
        "executed_count": len(executed_this_round), "executed_bytes": executed_bytes,
        "failed_count": len(failed),
        "unreclaimed_over_7d_count": len(unreclaimed),
        "unreclaimed_over_7d_bytes": unreclaimed_bytes,
        "unreclaimed_by_reason": by_reason,
        "needs_decision_count": sum(o["needs_decision"] for o in unreclaimed),
        "retained_count": sum(not o["needs_decision"] for o in unreclaimed),
        "unreclaimed_unknown_size_count": sum(o["size_bytes"] is None for o in unreclaimed),
        "age_unknown_count": sum(o["age_hours"] is None for o in objects if not o["primary"] and o["exists_now"]),
        "projects": project_totals(unreclaimed, executed_this_round),
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
        "gongfeng_eligible_count": sum(o["path"] in eligible_paths for o in verified_reclaimable),
        "gongfeng_executed_count": len(gongfeng_executed_items),
        "gongfeng_executed_bytes": gongfeng_executed_bytes,
        "gongfeng_blocked_count": len(gongfeng_blocked_items),
        "verified_reclaimable_remaining_count": len(verified_remaining),
        "cleaner_consecutive_failures": infra["cleaner"]["consecutive_failures"],
        "listeners_unknown": sum(1 for r in infra["listeners"] if r["whitelist"] != "白名单命中"),
    }
    evidence = {"summary": summary, "objects": objects, "executed": executed_this_round,
                "unreclaimed": unreclaimed,
                "run_status": run_status, "exceptions": run_exceptions,
                "age_unknown": age_unknown,
                "needs_decision": [o for o in unreclaimed if o["needs_decision"]],
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
    summary["root_used_pct"] = df_pct("/")
    summary["data_used_pct"] = df_pct("/data")
    summary["scope"] = roots
    summary["cleaner_status"] = infra["cleaner"]["last"].get("status")
    summary["mirror_dir"] = (None if args.no_mirror else
                             os.path.join(args.mirror_root, os.path.basename(round_dir)))
    run_status["round_dir"] = round_dir
    run_status["finished_at"] = now_iso()
    run_status["duration_s"] = round(time.time() - started, 1)
    audit = render_audit(summary, executed_this_round, unreclaimed, by_reason, verify, failed)
    with open(os.path.join(round_dir, "audit_section.md"), "w") as fh:
        fh.write(audit)
    for name, data in (("evidence.json", evidence), ("summary.json", summary),
                       ("run-status.json", run_status)):
        with open(os.path.join(round_dir, name), "w") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
    with open(os.path.join(round_dir, "report.md"), "w") as fh:
        fh.write(render_report(evidence))
    for o in objects:
        if o["path"] in state_new["worktrees"]:
            state_new["worktrees"][o["path"]]["who_uses"] = o.get("who_uses")
    with open(os.path.join(round_dir, "raw", "state_snapshot.json"), "w") as fh:
        json.dump(state_new, fh, ensure_ascii=False, indent=1)
    if args.execute:
        save_state(args.state, state_new)
    if not args.no_mirror:
        mirror_round(round_dir, args.mirror_root)
    if args.append_audit:
        with open(LEDGER, "a") as fh:
            fh.write("\n" + audit)

    if args.execute:
        status_path = os.path.join(STATE_DIR, "run-status.json")
        os.makedirs(os.path.dirname(status_path), exist_ok=True)
        with open(status_path + ".tmp", "w", encoding="utf-8") as fh:
            json.dump(run_status, fh, ensure_ascii=False, indent=1)
        os.replace(status_path + ".tmp", status_path)

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
    print("\n--- ≥7 天未清理 · 前 30 项（按项目分组、组内空间降序，供统一表格：项目|结果|原因|空间|年龄|上次活跃|领先/落后）---")
    for o in sorted(unreclaimed, key=lambda o: (o["repo"], -(o["size_bytes"] or 0), o["path"]))[:30]:
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
               if o.get("needs_decision")]
    print(f"\n--- 需 owner 决定的 {len(mr_rows)} 项（完整）---")
    for o in mr_rows:
        print(f"  {fmt_bytes(o['size_bytes']):>10}  {o['repo']}  {o['branch']}  "
              f"{o.get('pr_url') or pr_url(o)}  {o['reason']}  {o['next_step']}")
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
    print("\n--- 基础设施 ---")
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
            print(f"  {f.get('path') or f.get('repo')}: {f['error']}")
    if run_failed:
        print("[patrol] 本轮存在执行/扫描失败：见上方失败清单与 run-status.json")
    print(f"\n[patrol] 产物 {round_dir}  用时 {time.time()-started:.1f}s")
    return 1 if run_failed else 0


def render_report(evidence):
    """一段汇总、一张项目明细；每个对象与待办只出现一次。"""
    s = evidence["summary"]
    status = evidence["run_status"]
    mode = "只读预览，未执行清理" if s["mode"] == "dry-run" else "执行轮"
    lines = ["# Worktree 巡检报告", "",
             f"{status['finished_at']} · {mode} · `{s['run_id']}`", "",
             f"**已清理 {s['executed_count']} · 不清理 {s['retained_count']} · 需决策 {s['needs_decision_count']}**"
             f"（≥7 天口径；未清理 {s['unreclaimed_over_7d_count']} 项 / {fmt_bytes(s['unreclaimed_over_7d_bytes'])}）。"
             f"另有 <7 天保留 {s['held_under7_count']} 项 / {fmt_bytes(s['held_under7_bytes'])}。", ""]
    if s.get("executed_count"):
        lines += [f"本轮回收 {fmt_bytes(s['executed_bytes'])}。", ""]
    if s.get("age_unknown_count") or s.get("unreclaimed_unknown_size_count"):
        lines += [f"数据缺口：年龄不可得 {s.get('age_unknown_count', 0)} 项；"
                  f"大小不可得 {s.get('unreclaimed_unknown_size_count', 0)} 项。", ""]
    for item in status.get("exceptions") or []:
        lines.append(f"- 异常 `{item['code']}`：{item['detail']}")
    if status.get("exceptions"):
        lines.append("")

    def order(o):
        return o["repo"], -(o.get("size_bytes") or 0), o["path"]

    retained = sorted(evidence["unreclaimed"], key=order)
    selected = retained[:30]
    seen = {o["path"] for o in selected}
    selected += [o for o in retained if o.get("needs_decision") and o["path"] not in seen]
    shown_retained = len(selected)
    selected += [dict(o, decision="已清理") for o in evidence.get("executed", [])]
    selected += evidence.get("age_unknown", [])
    selected = sorted({o["path"]: o for o in selected}.values(), key=order)

    def cell(value):
        return str(value if value is not None else "不可得").replace("|", "\\|").replace("\n", " ")

    if selected:
        lines += ["| 项目 | 工作树 | 结果 | 空间 | 年龄 | 依据 / 待办 |",
                  "|---|---|---|---|---|---|"]
        previous_repo = None
        for o in selected:
            repo = o["repo"]
            project = repo.removeprefix("/data/code/") if repo != previous_repo else "↳"
            previous_repo = repo
            label = os.path.relpath(o["path"], repo)
            if label.startswith("../"):
                label = os.path.basename(o["path"])
            path = f"[{label}](<{o['path']}>)"
            reasons = o.get("reasons") or []
            facts = list(reasons)
            remote = o.get("remote_status") or o.get("remote_proof")
            if isinstance(remote, dict):
                remote = remote.get("description")
            if remote and remote not in facts and remote != "未核验（本地条件未通过）":
                facts.append(remote)
            url = o.get("pr_url") or ""
            if url.startswith("https://"):
                gf = o.get("gongfeng") or {}
                pr = o.get("pr") or {}
                number = gf.get("iid") or (pr.get("number") if isinstance(pr, dict) else pr)
                name = (f"MR!{number}" if gf or urlsplit(url).hostname == "git.example.invalid"
                        else f"PR#{number}" if number else "远端证据")
                facts.append(f"[{name}]({url})")
            if o.get("needs_decision") or o.get("execution_error"):
                facts.append("待办：" + o["next_step"])
            if o.get("decision") == "已清理":
                facts.append("分支已删" if o.get("branch_deleted") else "分支保留")
                if o.get("backup_root"):
                    facts.append(f"备份：{o['backup_root']}")
            age = (f"{o['age_hours']/24:.1f}d" if o.get("age_hours") is not None
                   else f"{o['age_days']:.1f}d" if o.get("age_days") is not None else "不可得")
            values = [project, path, o.get("decision", "不清理"), fmt_bytes(o.get("size_bytes")),
                      age, "；".join(facts) or "已完成"]
            lines.append("| " + " | ".join(cell(v) for v in values) + " |")
        lines.append("")
    if shown_retained < len(retained):
        lines += [f"≥7 天未清理展示 {shown_retained} / {len(retained)} 项，所有需决策项均已列出。", ""]
    lines += [f"空间为表观字节；上次活跃、领先/落后、基础设施及完整明细见"
              f"[证据]({s['round_dir']}/evidence.json) · [运行状态]({s['round_dir']}/run-status.json)。", ""]
    return "\n".join(lines)


def mirror_round(round_dir: str, mirror_root: str):
    """把本轮产物镜像进仓库（best-effort；仓库不可写不影响本轮结论）。"""
    dst = os.path.join(mirror_root, os.path.basename(round_dir.rstrip("/")))
    try:
        os.makedirs(dst, exist_ok=True)
        for item in ("evidence.json", "summary.json", "audit_section.md", "run-status.json", "report.md"):
            src = os.path.join(round_dir, item)
            if os.path.exists(src):
                shutil.copy2(src, os.path.join(dst, item))
        raw_src = os.path.join(round_dir, "raw")
        if os.path.isdir(raw_src):
            shutil.copytree(raw_src, os.path.join(dst, "raw"), dirs_exist_ok=True)
        pointer = os.path.join(mirror_root, ".current_round")
        with open(pointer + ".tmp", "w") as fh:
            fh.write(os.path.basename(round_dir.rstrip("/")) + "\n")
        os.replace(pointer + ".tmp", pointer)
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
             f"执行器 worktree-cleanup（非 --force、分支按远端证明删除）。")
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
                     f"#{x['pr']} | {x['size_bytes'] if x['size_bytes'] is not None else '不可得'} |")
        if summary.get("gongfeng_executed_count"):
            L.append(f"- 其中工蜂（git.example.invalid）{summary['gongfeng_executed_count']} 项经 "
                     f"`--remote-evidence` 走同一 owner 执行器（非 --force、分支按远端证明删除）。")
        for v in verify:
            L.append(f"- 复核 {v['path']}：commit={v['commit_type']}，"
                     f"分支{'已删（政策默认）' if not v.get('branch_present') else '未删（需关注）'}")
    if failed:
        L.append("\n失败：" + "; ".join(f"{f.get('path') or f.get('repo')}: {f['error']}" for f in failed))
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
    L.append("\n### 基础设施健康\n")
    L.append(f"- clean-tmp 连续失败 {summary.get('cleaner_consecutive_failures', '?')} 天。")
    L.append(f"- 非回环监听中未命中白名单 {summary.get('listeners_unknown', '?')} 个。")
    L.append(f"- 增量：测量 {summary['measured']} / 缓存复用 {summary['reused']}；"
             f"who-uses 串行 {summary['who_uses_probed']}。")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    sys.exit(main())
