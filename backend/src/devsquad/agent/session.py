"""Run the shared coding session with a caller-supplied approval prompt."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from devsquad.agent.loop import LoopResult, run_loop
from devsquad.attachments import TaskAttachment
from devsquad.sandbox.pathguard import SandboxError
from devsquad.tasks import Task, TaskStore
from devsquad.tools.permissions import PermissionPolicy
from devsquad.tools.worktree import WorktreeFiles
from devsquad.traces import TraceRecorder

Approval = Callable[[str, dict], bool | None]


def execute_task(
    task: Task, database: Path, provider: object, max_steps: int, budget: float,
    confirm: Approval,
    attachments: tuple[TaskAttachment, ...] = (),
) -> LoopResult:
    root = Path(task.worktree_path)
    files = None
    status = "error"
    try:
        files = WorktreeFiles(root)
        tools = {
            "list_dir": files.list_dir, "read_file": files.read_file, "search": files.search,
            "edit_file": files.edit_file, "write_file": files.write_file,
            "run_command": None, "run_tests": None,
        }

        def approve(name: str, args: dict) -> bool | None:
            preview = dict(args)
            change = None
            files.approved = None
            try:
                if name in {"write_file", "edit_file"}:
                    change = files.prepare(name, args)
                    preview["path"] = change.path.relative_to(files.guard.root).as_posix()
                else:
                    cwd = files.guard.check_read(args.get("cwd", "."))
                    preview["cwd"] = cwd.relative_to(files.guard.root).as_posix()
                    if name == "run_tests":
                        preview["argv"] = files.test_argv(cwd, args.get("argv"), args.get("files"))
                        preview.pop("files", None)
            except (OSError, ValueError, SandboxError):
                return False
            answer = confirm(name, preview)
            if answer is True:
                if change is not None:
                    files.approved = change
                else:
                    args.update(preview)
                    if name == "run_tests":
                        args.pop("files", None)
            return answer

        result = run_loop(
            task=task.title, model=provider, tools=tools, permissions=PermissionPolicy(root),
            max_steps=max_steps, max_cost_usd=budget, repo_root=root,
            trace_recorder=TraceRecorder(database), task_id=task.id, approve=approve,
            attachments=attachments,
        )
        status = result.status
        return result
    except (KeyboardInterrupt, EOFError):
        status = "interrupted"
        raise
    finally:
        if files is not None:
            files.close()
        TaskStore(database).finish_execution(task.id, status)
