#!/usr/bin/env python3
"""Update or inspect the HYGON-AI cookbook sparse cache."""

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


REPO_URL = "https://github.com/HYGON-AI/dcu-inference-cookbook.git"
SPARSE_PATH = "docs/model-deployment"
DEFAULT_CACHE_ROOT = Path.home() / "cookbook"
DEFAULT_TTL_DAYS = 3


def utc_now():
    return datetime.now(timezone.utc)


def iso_now():
    return utc_now().isoformat(timespec="seconds")


def run_git(args, cwd=None):
    cmd = ["git"] + args
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        universal_newlines=True,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip()
        raise RuntimeError("{} failed: {}".format(" ".join(cmd), detail))
    return proc.stdout.strip()


def read_state(state_path):
    if not state_path.exists():
        return {}
    try:
        with state_path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_state(state_path, state):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_suffix(state_path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
    tmp_path.replace(state_path)


def repo_exists(repo_dir):
    return (repo_dir / ".git").exists()


def get_head(repo_dir):
    commit = run_git(["rev-parse", "HEAD"], cwd=repo_dir)
    commit_date = run_git(["log", "-1", "--format=%cI"], cwd=repo_dir)
    return commit, commit_date


def is_stale(state, repo_dir, ttl_days):
    if not repo_exists(repo_dir):
        return True, "missing_cache"
    last_epoch = state.get("last_update_epoch")
    if not last_epoch:
        return True, "missing_update_time"
    age_seconds = time.time() - float(last_epoch)
    ttl_seconds = ttl_days * 24 * 60 * 60
    if age_seconds > ttl_seconds:
        return True, "ttl_expired"
    return False, "fresh"


def clone_repo(repo_dir):
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    run_git(["clone", "--depth", "1", "--filter=blob:none", "--sparse", REPO_URL, str(repo_dir)])
    run_git(["sparse-checkout", "set", SPARSE_PATH], cwd=repo_dir)


def pull_repo(repo_dir):
    run_git(["sparse-checkout", "set", SPARSE_PATH], cwd=repo_dir)
    run_git(["pull", "--ff-only", "--depth", "1", "origin", "main"], cwd=repo_dir)


def build_state(cache_root, repo_dir, state_path, ttl_days, status, reason, update_performed):
    commit, commit_date = get_head(repo_dir) if repo_exists(repo_dir) else ("", "")
    state = read_state(state_path)
    now = iso_now()
    state.update(
        {
            "repo_url": REPO_URL,
            "sparse_path": SPARSE_PATH,
            "cache_root": str(cache_root),
            "repo_dir": str(repo_dir),
            "state_file": str(state_path),
            "ttl_days": ttl_days,
            "last_checked_utc": now,
            "head_commit": commit,
            "head_commit_date": commit_date,
            "status": status,
            "reason": reason,
            "update_performed": update_performed,
        }
    )
    if update_performed:
        state["last_update_utc"] = now
        state["last_update_epoch"] = time.time()
    return state


def main():
    parser = argparse.ArgumentParser(description="Maintain the HYGON-AI cookbook sparse cache.")
    parser.add_argument("--cache-root", default=os.environ.get("DCU_LLMTEST_CACHE_ROOT"))
    parser.add_argument("--ttl-days", type=int, default=DEFAULT_TTL_DAYS)
    parser.add_argument("--check", action="store_true", help="Check TTL and update only when stale.")
    parser.add_argument("--force", action="store_true", help="Update even if the cache is fresh.")
    parser.add_argument("--status", action="store_true", help="Only print current state; do not update.")
    args = parser.parse_args()

    cache_root = Path(args.cache_root).expanduser() if args.cache_root else DEFAULT_CACHE_ROOT
    repo_dir = cache_root / "dcu-inference-cookbook"
    state_path = cache_root / "cookbook_state.json"
    state = read_state(state_path)

    stale, reason = is_stale(state, repo_dir, args.ttl_days)
    should_update = args.force or (stale and not args.status)

    try:
        if should_update:
            if repo_exists(repo_dir):
                pull_repo(repo_dir)
            else:
                clone_repo(repo_dir)
            out_state = build_state(cache_root, repo_dir, state_path, args.ttl_days, "updated", reason, True)
        else:
            status = "stale" if stale else "fresh"
            out_state = build_state(cache_root, repo_dir, state_path, args.ttl_days, status, reason, False)
        write_state(state_path, out_state)
        print(json.dumps(out_state, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        error_state = {
            "repo_url": REPO_URL,
            "cache_root": str(cache_root),
            "repo_dir": str(repo_dir),
            "state_file": str(state_path),
            "ttl_days": args.ttl_days,
            "last_checked_utc": iso_now(),
            "status": "error",
            "reason": reason,
            "update_performed": False,
            "error": str(exc),
        }
        write_state(state_path, error_state)
        print(json.dumps(error_state, ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
