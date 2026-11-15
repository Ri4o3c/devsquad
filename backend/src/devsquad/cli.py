"""Command line for the phase-1 control plane.

The only command that creates a task is::

    devsquad run --repo <path> --task "<requirement>"

``task list`` and ``task cleanup <id>`` are helpers. There is no
``task new`` command; ``devsquad.tasks.create_task`` is the internal
function ``run`` calls.

``run`` can opt in to a model-backed coding session. ``cleanup`` removes
the worktree and branch and protects unmerged work.

Paths and the database come from :mod:`devsquad.config` (``DEVSQUAD_DB``
and ``DEVSQUAD_WORKSPACES``).
"""

from __future__ import annotations

import json
import os
import sys
from contextlib import nullcontext
from math import isfinite
from pathlib import Path
from typing import Annotated

import typer

from devsquad.agent.deepseek import MODEL, DeepSeekModel, ModelError
from devsquad.agent.loop import run_loop
from devsquad.config import db_path, workspaces_dir
from devsquad.sandbox.pathguard import SandboxError
from devsquad.tasks import (
    Task,
    TaskError,
    TaskStore,
    cleanup_task,
    create_task,
    execution_lease,
    format_task_line,
    new_task_id,
)
from devsquad.tools.permissions import PermissionPolicy
from devsquad.tools.worktree import WorktreeFiles
from devsquad.traces import TraceRecorder, trace_summary

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
    model: Annotated[
        str | None, typer.Option("--model", help="Send selected task context to DeepSeek Flash.")
    ] = None,
    max_steps: Annotated[int, typer.Option("--max-steps", min=1, max=24)] = 8,
    max_cost_usd: Annotated[
        float, typer.Option("--max-cost-usd", help="Estimated model budget at peak USD rates.")
    ] = 0.05,
) -> None:
    """Create an isolated worktree and optionally run its coding agent."""
    provider = None
    if model is not None:
        if model != MODEL:
            raise typer.BadParameter(f"supported model: {MODEL}", param_hint="--model")
        if sys.platform != "linux":
            raise typer.BadParameter("model execution requires Linux or WSL", param_hint="--model")
        if not isfinite(max_cost_usd) or max_cost_usd <= 0:
            raise typer.BadParameter(
                "budget must be finite and positive", param_hint="--max-cost-usd",
            )
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            typer.echo("error: DEEPSEEK_API_KEY is missing", err=True)
            raise typer.Exit(code=1)
        provider = DeepSeekModel(key)
    database = db_path()
    task_id = new_task_id() if provider is not None else None
    lease = execution_lease(database, task_id) if task_id else nullcontext()
    try:
        with lease:
            created = create_task(
                repo=repo, title=task, workspaces_dir=workspaces_dir(), db_path=database,
                task_id=task_id, executing=provider is not None,
            )
            _echo_task(created)
            if provider is None:
                typer.echo("agent_execution: deferred")
                typer.echo("message: use --model to opt in to model execution")
                return
            typer.echo("agent_execution: running")
            typer.echo(
                "DeepSeek receives the requirement and selected, filtered tool results.", err=True,
            )
            _execute_task(created, database, provider, max_steps, max_cost_usd)
    except TaskError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _confirm_tool(name: str, args: dict) -> bool | None:
    if not sys.stdin.isatty() or not sys.stderr.isatty():
        return None
    typer.echo(json.dumps({"tool": name, "arguments": args}, indent=2), err=True)
    return typer.confirm("Allow this tool call once?", default=False, err=True)


def _execute_task(
    task: Task, database: Path, provider: DeepSeekModel, max_steps: int, budget: float,
) -> None:
    root = Path(task.worktree_path)
    files = None
    status = "error"
    try:
        files = WorktreeFiles(root)
        tools = {
            "list_dir": files.list_dir, "read_file": files.read_file,
            "edit_file": files.edit_file, "write_file": files.write_file,
            "run_command": None,  # Executed by the sandbox dispatcher.
        }

        def approve(name: str, args: dict) -> bool | None:
            preview = dict(args)
            files.approved = None
            try:
                if name in {"write_file", "edit_file"}:
                    change = files.prepare(name, args)
                    preview["path"] = change.path.relative_to(files.guard.root).as_posix()
                else:
                    cwd = files.guard.check_read(args.get("cwd", "."))
                    preview["cwd"] = cwd.relative_to(files.guard.root).as_posix()
            except (OSError, ValueError, SandboxError):
                typer.echo("The requested tool cannot be safely prepared.", err=True)
                return False
            answer = _confirm_tool(name, preview)
            if answer is True:
                if name in {"write_file", "edit_file"}:
                    files.approved = change
                else:
                    args["cwd"] = preview["cwd"]
            return answer

        result = run_loop(
            task=task.title, model=provider, tools=tools, permissions=PermissionPolicy(root),
            max_steps=max_steps, max_cost_usd=budget, repo_root=root,
            trace_recorder=TraceRecorder(database), task_id=task.id, approve=approve,
        )
        status = result.status
        typer.echo(f"result: {status}")
        typer.echo(f"steps: {result.steps}")
        typer.echo(f"estimated_cost_usd: {result.cost_usd:.6f}")
        if result.final_text:
            typer.echo("final_text: " + json.dumps(provider.redact(result.final_text)))
        if status == "ask_human":
            typer.echo("Tool approval requires an interactive terminal.", err=True)
        if status != "completed":
            raise typer.Exit(code=1)
    except (KeyboardInterrupt, typer.Abort):
        status = "interrupted"
        typer.echo("Execution interrupted.", err=True)
        raise typer.Exit(code=130) from None
    except ModelError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=1) from None
    except (OSError, SandboxError):
        typer.echo("error: could not access the execution worktree", err=True)
        raise typer.Exit(code=1) from None
    finally:
        if files is not None:
            files.close()
        TaskStore(database).finish_execution(task.id, status)


@app.command("trace")
def trace_command(
    task_id: Annotated[str, typer.Argument(help="Task id printed by devsquad run.")],
) -> None:
    """Display the ordered run events and their operational metadata."""
    path = db_path()
    if TaskStore(path).get(task_id) is None:
        typer.echo("error: task not found", err=True)
        raise typer.Exit(code=1)
    events = TraceRecorder(path).list_events(task_id)
    if not events:
        typer.echo("(no trace events)")
    for event in events:
        typer.echo(trace_summary(event))


def main() -> None:
    app()
