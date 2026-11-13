"""Shared fixtures for git repos and the CLI's on-disk locations."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(
        ["git", "init", "-b", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "devsquad-test@example.com"],
        cwd=repo,
        check=True,
    )
    subprocess.run(["git", "config", "user.name", "DevSquad Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    # Windows runners default to autocrlf, which makes a fresh worktree look dirty.
    subprocess.run(["git", "config", "core.autocrlf", "false"], cwd=repo, check=True)
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return repo


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    db = tmp_path / "state" / "devsquad.db"
    workspaces = tmp_path / "workspaces"
    monkeypatch.setenv("DEVSQUAD_DB", str(db))
    monkeypatch.setenv("DEVSQUAD_WORKSPACES", str(workspaces))
    monkeypatch.delenv("DEVSQUAD_TOKEN", raising=False)
    return {"db": db, "workspaces": workspaces}
