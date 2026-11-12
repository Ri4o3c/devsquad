"""Create and remove one git worktree per task.

The public CLI entry is ``devsquad run --repo <path> --task "<requirement>"``.
``create_task`` is the internal function that command calls. There is no
``task new`` command.

This is the trusted control plane, not the agent sandbox. It shells out to
``git`` with an argument list (never a shell string) so it can add and
remove worktrees. Agent commands go through ``devsquad.sandbox`` instead,
where cwd, environment, and paths are locked down.

A task branch is always ``devsquad/<id>``. The worktree directory is always
outside the target repository. Cleanup will not delete ``main``, the
repository itself, or a dirty worktree unless ``force=True``. ``force``
does not lift the "never delete the user's checkout" rule.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from devsquad.db import connect

_TASK_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_BRANCH_RE = re.compile(r"^devsquad/[0-9a-f]{12}$")
_PROTECTED_BRANCHES = frozenset({"main", "master", "HEAD", "develop", "trunk"})


class TaskError(Exception):
    """The task could not be created or cleaned up."""


class GitError(TaskError):
    """A git command used by the control plane exited non-zero."""

    def __init__(self, args: tuple[str, ...], returncode: int, detail: str) -> None:
        self.git_args = args
        self.returncode = returncode
        self.detail = detail
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {detail}")


@dataclass(frozen=True)
class Task:
    """One row of the ``tasks`` table."""

    id: str
    repo_path: str
    branch: str
    worktree_path: str
    status: str
    created_at: str
    title: str
    base_commit: str


def new_task_id() -> str:
    """12 hex characters. The branch name is ``devsquad/<id>``."""
    return uuid.uuid4().hex[:12]


def create_task(
    *,
    repo: Path,
    title: str,
    workspaces_dir: Path,
    db_path: Path,
    task_id: str | None = None,
) -> Task:
    """Add a worktree on a new branch and insert the task row.

    The worktree is ``<workspaces_dir>/<task_id>``. ``git worktree add``
    checks that branch out there and leaves the repository's current branch
    and working tree alone.
    """
    cleaned_title = title.strip()
    if not cleaned_title:
        raise TaskError("title must not be empty")

    repo_root = _require_git_repo(repo)
    base_commit = git(repo_root, "rev-parse", "HEAD").stdout.strip()

    chosen_id = task_id or new_task_id()
    if not _TASK_ID_RE.fullmatch(chosen_id):
        raise TaskError(f"invalid task id: {chosen_id}")
    branch = f"devsquad/{chosen_id}"

    workspaces = Path(workspaces_dir).expanduser().resolve()
    workspaces.mkdir(parents=True, exist_ok=True)
    worktree = workspaces / chosen_id
    _assert_outside_repo(repo_root, worktree)
    if worktree.exists():
        raise TaskError(f"worktree path already exists: {worktree}")

    git(repo_root, "worktree", "add", "-b", branch, str(worktree), "HEAD")
    if not worktree.is_dir():
        raise TaskError(f"git did not create the worktree: {worktree}")

    task = Task(
        id=chosen_id,
        repo_path=str(repo_root),
        branch=branch,
        worktree_path=str(worktree),
        status="created",
        created_at=datetime.now(UTC).isoformat(),
        title=cleaned_title,
        base_commit=base_commit,
    )
    try:
        TaskStore(db_path).insert(task)
    except Exception:
        _rollback_worktree(repo_root, worktree, branch)
        raise
    return task


def cleanup_task(*, task_id: str, db_path: Path, force: bool = False) -> Task:
    """Remove the task worktree and its branch, then mark the row ``cleaned``.

    Refuses without ``force`` when the worktree has uncommitted changes
    (including untracked files) or the branch has commits that are not in
    ``HEAD``, ``main``, or ``master``.

    ``force`` discards that work. It still refuses to delete a branch that
    is not ``devsquad/<task id>``, and it still refuses to delete a path
    that is the target repo or sits inside it.
    """
    store = TaskStore(db_path)
    task = store.get(task_id)
    if task is None:
        raise TaskError(f"unknown task: {task_id}")
    if task.status == "cleaned":
        raise TaskError(f"task already cleaned: {task_id}")

    _assert_removable(task)
    repo = Path(task.repo_path)
    worktree = Path(task.worktree_path)
    if not repo.exists():
        raise TaskError(f"repo no longer exists: {repo}")

    if not force:
        reasons: list[str] = []
        if worktree.exists() and _is_dirty(worktree):
            reasons.append("worktree has uncommitted changes")
        if _branch_exists(repo, task.branch) and not _is_merged(repo, task.branch):
            reasons.append("branch has commits that are not merged into HEAD, main, or master")
        if reasons:
            raise TaskError("; ".join(reasons) + "; re-run with --force to discard them")

    _remove_worktree(repo, worktree, force=force)
    if _branch_exists(repo, task.branch):
        git(repo, "branch", "-D" if force else "-d", task.branch)
    store.mark_cleaned(task_id)
    updated = store.get(task_id)
    if updated is None:
        raise TaskError(f"task disappeared during cleanup: {task_id}")
    return updated


def format_task_line(task: Task) -> str:
    """One tab-separated list row: id, status, branch, worktree, title."""
    title = " ".join(task.title.split())
    return "\t".join([task.id, task.status, task.branch, task.worktree_path, title])


class TaskStore:
    """CRUD for the ``tasks`` table."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = Path(db_path)

    def insert(self, task: Task) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, repo_path, branch, worktree_path, status,
                    created_at, title, base_commit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task.id,
                    task.repo_path,
                    task.branch,
                    task.worktree_path,
                    task.status,
                    task.created_at,
                    task.title,
                    task.base_commit,
                ),
            )
        finally:
            conn.close()

    def get(self, task_id: str) -> Task | None:
        conn = connect(self.db_path)
        try:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return _row_to_task(row)

    def list_tasks(self) -> list[Task]:
        conn = connect(self.db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM tasks ORDER BY created_at ASC, id ASC"
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_task(row) for row in rows]

    def mark_cleaned(self, task_id: str) -> None:
        conn = connect(self.db_path)
        try:
            cur = conn.execute(
                "UPDATE tasks SET status = 'cleaned' WHERE id = ?",
                (task_id,),
            )
            if cur.rowcount != 1:
                raise TaskError(f"unknown task: {task_id}")
        finally:
            conn.close()


def git(
    cwd: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run git with an argument list. ``check=False`` returns a failed process."""
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=_git_env(),
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise GitError(args, completed.returncode, detail)
    return completed


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    # Never open a credential prompt from the control plane.
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _require_git_repo(repo: Path) -> Path:
    candidate = Path(repo).expanduser()
    if not candidate.exists():
        raise TaskError(f"repo does not exist: {candidate}")
    inside = git(candidate, "rev-parse", "--is-inside-work-tree", check=False)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise TaskError(f"not a git repository: {candidate}")
    top = Path(git(candidate, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    head = git(top, "rev-parse", "--verify", "HEAD", check=False)
    if head.returncode != 0:
        raise TaskError(f"repository has no commits yet: {top}")
    return top


def _assert_outside_repo(repo_root: Path, worktree: Path) -> None:
    repo_real = repo_root.resolve()
    wt_real = worktree.resolve(strict=False)
    if wt_real == repo_real or repo_real in wt_real.parents or wt_real in repo_real.parents:
        raise TaskError(
            "refusing to place the worktree inside the target repo "
            f"({wt_real} is not outside {repo_real})"
        )


def _assert_removable(task: Task) -> None:
    if task.branch in _PROTECTED_BRANCHES or not _BRANCH_RE.fullmatch(task.branch):
        raise TaskError(f"refusing to delete unexpected branch {task.branch!r} for task {task.id}")
    if task.branch != f"devsquad/{task.id}":
        raise TaskError(f"refusing to delete unexpected branch {task.branch!r} for task {task.id}")
    repo = Path(task.repo_path).resolve()
    worktree = Path(task.worktree_path).resolve(strict=False)
    if worktree == repo or repo in worktree.parents or worktree in repo.parents:
        raise TaskError(
            f"refusing to remove a path that is the target repo or inside it ({worktree})"
        )


def _is_dirty(worktree: Path) -> bool:
    status = git(worktree, "status", "--porcelain")
    return bool(status.stdout.strip())


def _branch_exists(repo: Path, branch: str) -> bool:
    ref = f"refs/heads/{branch}"
    found = git(repo, "show-ref", "--verify", "--quiet", ref, check=False)
    return found.returncode == 0


def _is_merged(repo: Path, branch: str) -> bool:
    """True when every commit on ``branch`` is already contained in a base ref.

    ``git merge-base --is-ancestor A B`` succeeds when ``A`` is reachable
    from ``B``. A branch created at ``HEAD`` and never committed on is an
    ancestor of ``HEAD``, so a no-op task can be cleaned up without
    ``--force``. A commit made inside the worktree is not.
    """
    for target in ("HEAD", "main", "master"):
        exists = git(repo, "rev-parse", "--verify", "--quiet", target, check=False)
        if exists.returncode != 0:
            continue
        ancestor = git(repo, "merge-base", "--is-ancestor", branch, target, check=False)
        if ancestor.returncode == 0:
            return True
    return False


def _remove_worktree(repo: Path, worktree: Path, *, force: bool) -> None:
    if not worktree.exists():
        git(repo, "worktree", "prune", check=False)
        return
    command = ["worktree", "remove"]
    if force:
        command.append("--force")
    command.append(str(worktree))
    git(repo, *command)


def _rollback_worktree(repo: Path, worktree: Path, branch: str) -> None:
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree)],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "branch", "-D", branch],
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def _row_to_task(row: sqlite3.Row) -> Task:
    return Task(
        id=row["id"],
        repo_path=row["repo_path"],
        branch=row["branch"],
        worktree_path=row["worktree_path"],
        status=row["status"],
        created_at=row["created_at"],
        title=row["title"],
        base_commit=row["base_commit"],
    )
