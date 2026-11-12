"""Command line for the phase-1 control plane.

The only command that creates a task is::

    devsquad run --repo <path> --task "<requirement>"

``task list`` and ``task cleanup <id>`` are helpers. There is no
``task new`` command; ``devsquad.tasks.create_task`` is the internal
function ``run`` calls.

``run`` does not start an agent. That arrives in a later PR. ``cleanup``
removes the worktree and the branch, refusing unmerged work unless
``--force`` is set.

Paths and the database come from :mod:`devsquad.config` (``DEVSQUAD_DB``
and ``DEVSQUAD_WORKSPACES``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from devsquad.config import db_path, workspaces_dir
from devsquad.tasks import (
    Task,
    TaskError,
    TaskStore,
    cleanup_task,
    create_task,
    format_task_line,
)

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="DevSquad control plane.",
)
task_app = typer.Typer(
    no_args_is_help=True,
    pretty_exceptions_enable=False,
    help="List and remove isolated task worktrees.",
)
app.add_typer(task_app, name="task")


def _echo_task(task: Task) -> None:
    typer.echo(f"task_id: {task.id}")
    typer.echo(f"branch: {task.branch}")
    typer.echo(f"worktree: {task.worktree_path}")
    typer.echo(f"repo: {task.repo_path}")
    typer.echo(f"status: {task.status}")
    typer.echo(f"title: {task.title}")
    typer.echo("agent_execution: deferred")


@task_app.command("list")
def task_list() -> None:
    """List tasks. Columns are id, status, branch, worktree, title."""
    tasks = TaskStore(db_path()).list_tasks()
    if not tasks:
        typer.echo("(no tasks)")
        return
    for task in tasks:
        typer.echo(format_task_line(task))


@task_app.command("cleanup")
def task_cleanup(
    task_id: Annotated[str, typer.Argument(help="Task id printed by `devsquad run`.")],
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Delete the worktree and branch even when changes are unmerged.",
        ),
    ] = False,
) -> None:
    """Remove the task worktree and its branch.

    Refuses when the worktree is dirty or the branch is not merged into
    HEAD, main, or master. ``--force`` discards that work. It still will
    not delete the repository's own checkout or a branch outside
    ``devsquad/<id>``.
    """
    try:
        task = cleanup_task(task_id=task_id, db_path=db_path(), force=force)
    except TaskError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"cleaned: {task.id}")
    typer.echo(f"removed_worktree: {task.worktree_path}")
    typer.echo(f"removed_branch: {task.branch}")
    typer.echo(f"status: {task.status}")


@app.command("run")
def run_command(
    repo: Annotated[Path, typer.Option("--repo", help="Local git repository.")],
    task: Annotated[
        str,
        typer.Option("--task", help="Requirement text. Stored as the task title."),
    ],
) -> None:
    """Create a worktree for a requirement and print its id.

    Agent execution is not part of this command. A later PR will run the
    Planner / Coder / Tester / Reviewer loop inside the worktree. This
    command only creates the task.
    """
    try:
        created = create_task(
            repo=repo,
            title=task,
            workspaces_dir=workspaces_dir(),
            db_path=db_path(),
        )
    except TaskError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    _echo_task(created)
    typer.echo("message: agent execution comes in a later PR")


def main() -> None:
    app()
