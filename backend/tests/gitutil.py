"""Tiny git helpers for tests. Not part of the devsquad package."""

from __future__ import annotations

import subprocess
from pathlib import Path


def git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout


def worktree_fingerprint(repo: Path) -> tuple:
    """Files outside ``.git``, plus HEAD, branch, status, and diff."""
    files: list[tuple[str, bytes]] = []
    for path in sorted(repo.rglob("*")):
        rel = path.relative_to(repo)
        if ".git" in rel.parts:
            continue
        if path.is_symlink():
            files.append((rel.as_posix(), b"link:" + path.readlink().as_posix().encode()))
        elif path.is_file():
            files.append((rel.as_posix(), path.read_bytes()))
    status = subprocess.check_output(["git", "status", "--porcelain=v1"], cwd=repo)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo)
    branch = subprocess.check_output(["git", "branch", "--show-current"], cwd=repo)
    diff = subprocess.check_output(["git", "diff", "HEAD"], cwd=repo)
    return (tuple(files), status, head, branch, diff)
