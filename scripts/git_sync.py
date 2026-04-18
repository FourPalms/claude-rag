#!/usr/bin/env python3
"""
Sync each code repo to its primary branch + latest before indexing.

For every configured code path, walk up to find the enclosing .git repo,
de-dupe by repo root (multiple code paths often live inside one repo), then
for each repo:

  - If the working tree has uncommitted tracked changes: skip with a loud
    warning. We never touch dirty trees — losing in-progress work would
    be worse than indexing slightly stale code.
  - Otherwise: detect the default branch (origin/HEAD), check it out if not
    already there, and `git pull --ff-only`. Fast-forward-only means we'd
    rather fail loudly than merge/rebase on Jeremy's behalf.

Untracked files are NOT blockers — they don't participate in branch switching
unless they'd be clobbered, and `git checkout` already refuses in that case.
"""

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


@dataclass
class RepoSyncResult:
    repo_root: Path
    default_branch: Optional[str]
    # One of: updated, already-current, skipped-dirty, skipped-non-ff,
    # skipped-error, skipped-no-remote
    status: str
    message: str


def find_repo_root(path: Path) -> Optional[Path]:
    """Walk upward from ``path`` until we find a .git entry (dir or file).

    Returns the repo root, or None if ``path`` isn't inside a git repo.
    """
    path = path.expanduser()
    if not path.exists():
        return None
    path = path.resolve()
    cur = path if path.is_dir() else path.parent
    while True:
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent


def _git(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def get_default_branch(repo: Path) -> Optional[str]:
    """Return the repo's default branch (main/master/develop/…), or None."""
    # Preferred: origin/HEAD is a symbolic ref that points at the remote
    # default, e.g. refs/remotes/origin/main.
    res = _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD")
    if res.returncode == 0 and res.stdout.strip():
        return res.stdout.strip().rsplit("/", 1)[-1]
    # Fallback: parse `git remote show origin`. Slower (network) but works
    # when origin/HEAD wasn't set locally.
    res = _git(repo, "remote", "show", "origin")
    if res.returncode == 0:
        for line in res.stdout.splitlines():
            line = line.strip()
            if line.startswith("HEAD branch:"):
                return line.split(":", 1)[1].strip()
    return None


def get_current_branch(repo: Path) -> Optional[str]:
    res = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if res.returncode == 0:
        return res.stdout.strip()
    return None


def is_dirty(repo: Path) -> bool:
    """True if there are uncommitted modifications to tracked files."""
    res = _git(repo, "diff-index", "--quiet", "HEAD", "--")
    return res.returncode != 0


def has_remote(repo: Path) -> bool:
    res = _git(repo, "remote")
    return res.returncode == 0 and bool(res.stdout.strip())


def sync_repo(repo: Path) -> RepoSyncResult:
    if not has_remote(repo):
        return RepoSyncResult(repo, None, "skipped-no-remote", "no git remote configured")

    default = get_default_branch(repo)
    if default is None:
        return RepoSyncResult(
            repo, None, "skipped-error", "couldn't determine default branch"
        )

    if is_dirty(repo):
        current = get_current_branch(repo) or "?"
        return RepoSyncResult(
            repo,
            default,
            "skipped-dirty",
            f"uncommitted changes on '{current}' — not touching",
        )

    current = get_current_branch(repo)
    if current != default:
        checkout = _git(repo, "checkout", default)
        if checkout.returncode != 0:
            return RepoSyncResult(
                repo,
                default,
                "skipped-error",
                f"checkout {default} failed: {(checkout.stderr or checkout.stdout).strip()}",
            )

    pull = _git(repo, "pull", "--ff-only")
    if pull.returncode != 0:
        return RepoSyncResult(
            repo,
            default,
            "skipped-non-ff",
            f"pull --ff-only failed: {(pull.stderr or pull.stdout).strip()}",
        )

    # Distinguish "already up to date" from a real fast-forward update by
    # looking for the stable phrase git emits in the up-to-date case.
    stdout = pull.stdout.strip()
    if "Already up to date" in stdout or "up-to-date" in stdout.lower():
        return RepoSyncResult(repo, default, "already-current", f"on {default}, already current")
    return RepoSyncResult(repo, default, "updated", f"fast-forwarded {default}")


def find_dirty_repos(paths: List[str]) -> List[Path]:
    """Pre-flight check: return every unique repo with uncommitted changes.

    Used by the indexer to refuse to start if switching to the primary branch
    anywhere would risk the user's in-progress work. Doesn't run network ops
    — it's purely a local working-tree check.
    """
    dirty: List[Path] = []
    for root in dedupe_repo_roots(paths):
        if is_dirty(root):
            dirty.append(root)
    return dirty


def dedupe_repo_roots(paths: List[str]) -> List[Path]:
    """Expand ~, resolve, find each enclosing repo root, return unique roots."""
    roots: List[Path] = []
    seen: set = set()
    for p in paths:
        root = find_repo_root(Path(p).expanduser())
        if root is None or root in seen:
            continue
        seen.add(root)
        roots.append(root)
    return roots


def sync_all(paths: List[str]) -> List[RepoSyncResult]:
    """Sync every unique repo referenced by ``paths``."""
    return [sync_repo(root) for root in dedupe_repo_roots(paths)]


_STATUS_GLYPH = {
    "updated": "✓",
    "already-current": "✓",
    "skipped-dirty": "⚠️ ",
    "skipped-non-ff": "⚠️ ",
    "skipped-error": "✗",
    "skipped-no-remote": "·",
}


def print_sync_report(results: List[RepoSyncResult]) -> None:
    """Human-readable summary, one line per repo."""
    if not results:
        print("  (no repos to sync)")
        return
    # Right-align the repo name for readability.
    name_width = max(len(r.repo_root.name) for r in results)
    for r in results:
        glyph = _STATUS_GLYPH.get(r.status, "?")
        branch = r.default_branch or "?"
        print(f"  {glyph} {r.repo_root.name.ljust(name_width)}  [{branch}]  {r.message}")
