from __future__ import annotations

import subprocess
from pathlib import Path

from council_os.handoff.schemas import RepoSnapshot


def _run_git(args: list[str], cwd: Path) -> str:
    return subprocess.check_output(["git", *args], text=True, stderr=subprocess.DEVNULL, cwd=cwd).strip()


def resolve_repo_root(start: Path) -> Path:
    try:
        root = _run_git(["rev-parse", "--show-toplevel"], cwd=start)
    except Exception as exc:
        raise RuntimeError(f"Unable to resolve git repo root from {start}") from exc
    return Path(root)


def capture_repo_snapshot(start: Path) -> RepoSnapshot:
    repo_root = resolve_repo_root(start)
    commit_sha = _run_git(["rev-parse", "HEAD"], cwd=repo_root)
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
    status = _run_git(["status", "--porcelain"], cwd=repo_root)
    dirty = bool(status.strip())
    return RepoSnapshot(commit_sha=commit_sha, branch=branch, dirty=dirty)
