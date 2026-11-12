"""CLI and worktree tests.

The public create command is ``devsquad run --repo <path> --task "<requirement>"``.
``task list`` and ``task cleanup`` are the helpers. ``task new`` is not a command.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devsquad.cli import app
from devsquad.tasks import Task, TaskError, TaskStore, cleanup_task
from tests.gitutil import git, worktree_fingerprint

runner = CliRunner()


def _text(result) -> str:
    parts = [result.stdout or "", result.stderr or ""]
    output = getattr(result, "output", None)
    if output and output not in parts:
        parts.append(output)
    return "\n".join(parts)


def _parse_kv(text: str) -> dict[str, str]:
    data: dict[str, str] = {}
    for line in text.splitlines():
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        data[key.strip()] = value.strip()
    return data


def _run(repo: Path, requirement: str):
    return runner.invoke(app, ["run", "--repo", str(repo), "--task", requirement])


def test_run_help_uses_task_option() -> None:
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--repo" in result.stdout
    assert "--task" in result.stdout


def test_task_new_is_not_a_command(git_repo: Path, cli_env: dict[str, Path]) -> None:
    before = worktree_fingerprint(git_repo)
    result = runner.invoke(
        app,
        ["task", "new", "--repo", str(git_repo), "--title", "should not exist"],
    )
    assert result.exit_code != 0
    assert "task_id:" not in result.stdout
    assert worktree_fingerprint(git_repo) == before
    assert not cli_env["workspaces"].exists() or not any(cli_env["workspaces"].iterdir())


def test_run_rejects_positional_requirement(git_repo: Path, cli_env: dict[str, Path]) -> None:
    result = runner.invoke(app, ["run", "--repo", str(git_repo), "add a test"])
    assert result.exit_code != 0
    assert not cli_env["workspaces"].exists() or not any(cli_env["workspaces"].iterdir())


def test_run_creates_worktree_outside_repo_and_records_row(
    git_repo: Path, cli_env: dict[str, Path]
) -> None:
    head = git(git_repo, "rev-parse", "HEAD").strip()
    branch = git(git_repo, "branch", "--show-current").strip()
    result = _run(git_repo, "cover the empty input")
    assert result.exit_code == 0, _text(result)
    info = _parse_kv(result.stdout)
    assert info["agent_execution"] == "deferred"
    assert "later PR" in info["message"]
    assert info["branch"] == f"devsquad/{info['task_id']}"
    assert info["status"] == "created"
    assert info["title"] == "cover the empty input"

    worktree = Path(info["worktree"])
    repo = git_repo.resolve()
    assert worktree.is_dir()
    assert worktree.parent.resolve() == cli_env["workspaces"].resolve()
    assert not worktree.resolve().is_relative_to(repo)
    assert not repo.is_relative_to(worktree.resolve())
    # A linked worktree has a .git file, not a .git directory.
    assert (worktree / ".git").is_file()
    assert git(worktree, "rev-parse", "HEAD").strip() == head
    assert git(worktree, "branch", "--show-current").strip() == info["branch"]
    assert git(git_repo, "branch", "--show-current").strip() == branch

    conn = sqlite3.connect(cli_env["db"])
    row = conn.execute(
        "SELECT id, repo_path, branch, worktree_path, status, created_at, title, base_commit "
        "FROM tasks"
    ).fetchone()
    conn.close()
    assert row[0] == info["task_id"]
    assert Path(row[1]) == repo
    assert row[2] == info["branch"]
    assert Path(row[3]) == worktree.resolve()
    assert row[4] == "created"
    assert "T" in row[5]
    assert row[6] == "cover the empty input"
    assert row[7] == head


def test_task_list_shows_the_run(git_repo: Path, cli_env: dict[str, Path]) -> None:
    created = _run(git_repo, "list me")
    assert created.exit_code == 0, _text(created)
    task_id = _parse_kv(created.stdout)["task_id"]
    listed = runner.invoke(app, ["task", "list"])
    assert listed.exit_code == 0
    line = next(line for line in listed.stdout.splitlines() if line.startswith(task_id))
    columns = line.split("\t")
    assert columns[0] == task_id
    assert columns[1] == "created"
    assert columns[2] == f"devsquad/{task_id}"
    assert columns[4] == "list me"


def test_list_is_empty_before_any_run(cli_env: dict[str, Path]) -> None:
    listed = runner.invoke(app, ["task", "list"])
    assert listed.exit_code == 0
    assert listed.stdout.strip() == "(no tasks)"


def test_original_repo_worktree_untouched_after_create_and_cleanup(
    git_repo: Path, cli_env: dict[str, Path]
) -> None:
    (git_repo / "local-note.txt").write_text("keep me\n", encoding="utf-8")
    before = worktree_fingerprint(git_repo)
    branches_before = git(git_repo, "branch", "--format=%(refname:short)")

    created = _run(git_repo, "look around")
    assert created.exit_code == 0, _text(created)
    info = _parse_kv(created.stdout)
    worktree = Path(info["worktree"])
    assert worktree.is_dir()
    assert not (worktree / "local-note.txt").exists()
    assert worktree_fingerprint(git_repo) == before

    cleaned = runner.invoke(app, ["task", "cleanup", info["task_id"]])
    assert cleaned.exit_code == 0, _text(cleaned)
    assert _parse_kv(cleaned.stdout)["status"] == "cleaned"
    assert worktree_fingerprint(git_repo) == before
    assert not worktree.exists()
    assert git(git_repo, "branch", "--format=%(refname:short)") == branches_before
    show = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{info['branch']}"],
        cwd=git_repo,
        check=False,
    )
    assert show.returncode != 0


def test_cleanup_refuses_dirty_worktree_without_force(
    git_repo: Path, cli_env: dict[str, Path]
) -> None:
    before = worktree_fingerprint(git_repo)
    created = _run(git_repo, "dirty")
    info = _parse_kv(created.stdout)
    worktree = Path(info["worktree"])
    (worktree / "notes.txt").write_text("wip\n", encoding="utf-8")

    refused = runner.invoke(app, ["task", "cleanup", info["task_id"]])
    assert refused.exit_code != 0
    assert "--force" in _text(refused)
    assert worktree.is_dir()
    assert (worktree / "notes.txt").is_file()
    assert worktree_fingerprint(git_repo) == before

    forced = runner.invoke(app, ["task", "cleanup", info["task_id"], "--force"])
    assert forced.exit_code == 0, _text(forced)
    assert not worktree.exists()
    assert worktree_fingerprint(git_repo) == before


def test_cleanup_refuses_unmerged_commits_without_force(
    git_repo: Path, cli_env: dict[str, Path]
) -> None:
    before = worktree_fingerprint(git_repo)
    created = _run(git_repo, "commit in the worktree")
    info = _parse_kv(created.stdout)
    worktree = Path(info["worktree"])
    (worktree / "added.txt").write_text("from the task\n", encoding="utf-8")
    subprocess.run(["git", "add", "added.txt"], cwd=worktree, check=True)
    subprocess.run(["git", "commit", "-m", "task commit"], cwd=worktree, check=True)

    refused = runner.invoke(app, ["task", "cleanup", info["task_id"]])
    assert refused.exit_code != 0
    assert "not merged" in _text(refused)
    assert worktree.is_dir()
    assert worktree_fingerprint(git_repo) == before

    forced = runner.invoke(app, ["task", "cleanup", info["task_id"], "--force"])
    assert forced.exit_code == 0, _text(forced)
    assert not worktree.exists()
    assert worktree_fingerprint(git_repo) == before


def test_cleanup_unknown_task(cli_env: dict[str, Path]) -> None:
    result = runner.invoke(app, ["task", "cleanup", "missing"])
    assert result.exit_code != 0
    assert "unknown task" in _text(result)


def test_run_rejects_empty_requirement(git_repo: Path, cli_env: dict[str, Path]) -> None:
    result = _run(git_repo, "   ")
    assert result.exit_code != 0
    assert "empty" in _text(result)


def test_run_rejects_non_repo(tmp_path: Path, cli_env: dict[str, Path]) -> None:
    result = _run(tmp_path / "nope", "hi")
    assert result.exit_code != 0
    assert "not a git repository" in _text(result) or "does not exist" in _text(result)


def test_run_rejects_repo_without_commits(tmp_path: Path, cli_env: dict[str, Path]) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    result = _run(repo, "hi")
    assert result.exit_code != 0
    assert "no commits" in _text(result)


def test_workspaces_inside_the_repo_are_rejected(
    git_repo: Path, cli_env: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    before = worktree_fingerprint(git_repo)
    monkeypatch.setenv("DEVSQUAD_WORKSPACES", str(git_repo / "nested-workspaces"))
    result = _run(git_repo, "do not nest")
    assert result.exit_code != 0
    assert "outside" in _text(result)
    assert worktree_fingerprint(git_repo) == before
    branches = git(git_repo, "branch", "--format=%(refname:short)")
    assert "devsquad/" not in branches


def test_cleanup_refuses_to_delete_the_main_checkout(
    git_repo: Path, cli_env: dict[str, Path]
) -> None:
    head = git(git_repo, "rev-parse", "HEAD").strip()
    store = TaskStore(cli_env["db"])
    store.insert(
        Task(
            id="abc123abc123",
            repo_path=str(git_repo.resolve()),
            branch="main",
            worktree_path=str(git_repo.resolve()),
            status="created",
            created_at="2026-01-01T00:00:00+00:00",
            title="tampered",
            base_commit=head,
        )
    )
    with pytest.raises(TaskError, match="refusing"):
        cleanup_task(task_id="abc123abc123", db_path=cli_env["db"], force=True)
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "hello\n"
    assert git(git_repo, "branch", "--show-current").strip() == "main"


def test_cleanup_refuses_worktree_path_that_is_the_repo(
    git_repo: Path, cli_env: dict[str, Path]
) -> None:
    head = git(git_repo, "rev-parse", "HEAD").strip()
    TaskStore(cli_env["db"]).insert(
        Task(
            id="def456def456",
            repo_path=str(git_repo.resolve()),
            branch="devsquad/def456def456",
            worktree_path=str(git_repo.resolve()),
            status="created",
            created_at="2026-01-01T00:00:00+00:00",
            title="tampered path",
            base_commit=head,
        )
    )
    with pytest.raises(TaskError, match="refusing"):
        cleanup_task(task_id="def456def456", db_path=cli_env["db"], force=True)
    assert (git_repo / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_two_runs_leave_the_original_branch(git_repo: Path, cli_env: dict[str, Path]) -> None:
    first = _run(git_repo, "one")
    second = _run(git_repo, "two")
    assert first.exit_code == 0 and second.exit_code == 0
    info1 = _parse_kv(first.stdout)
    info2 = _parse_kv(second.stdout)
    assert info1["task_id"] != info2["task_id"]
    assert Path(info1["worktree"]).is_dir()
    assert Path(info2["worktree"]).is_dir()
    assert git(git_repo, "branch", "--show-current").strip() == "main"
