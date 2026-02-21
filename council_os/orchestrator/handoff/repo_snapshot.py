from __future__ import annotations

from pathlib import Path

from council_os.handoff.schemas import RepoSnapshot
from council_os.utils import run_git


def resolve_repo_root(start: Path) -> Path:
    try:
        root = run_git(["rev-parse", "--show-toplevel"], cwd=start)
    except Exception as exc:
        raise RuntimeError(f"Unable to resolve git repo root from {start}") from exc
    return Path(root)


def capture_repo_snapshot(start: Path) -> RepoSnapshot:
    repo_root = resolve_repo_root(start)
    commit_sha = run_git(["rev-parse", "HEAD"], cwd=repo_root)
    branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo_root)
    status = run_git(["status", "--porcelain"], cwd=repo_root)
    tree_hash = run_git(["rev-parse", "HEAD^{tree}"], cwd=repo_root)
    dirty = bool(status.strip())
    return RepoSnapshot(commit_sha=commit_sha, branch=branch, dirty=dirty, tree_hash=tree_hash)


def resolve_repo_url(start: Path) -> str | None:
    repo_root = resolve_repo_root(start)
    try:
        return run_git(["remote", "get-url", "origin"], cwd=repo_root)
    except Exception:
        return None
