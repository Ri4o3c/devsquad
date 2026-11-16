"""Single-worker local runs and one-shot browser approvals."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event, Lock, Thread
from time import monotonic, time
from uuid import uuid4

from devsquad.agent.deepseek import DeepSeekModel, ModelError
from devsquad.agent.session import execute_task
from devsquad.attachments import TaskAttachment
from devsquad.sandbox.pathguard import SandboxError
from devsquad.tasks import (
    Task,
    TaskError,
    create_task,
    execution_lease,
    git,
    new_task_id,
)
from devsquad.tools.worktree import WorktreeFiles
from devsquad.traces import TraceRecorder, trace_summary

APPROVAL_SECONDS = 120
_MAX_PATCH = 160_000


class RunConflict(Exception):
    """A run is occupied or its approval is no longer pending."""


class RunUnavailable(Exception):
    """The execution environment is not configured."""


def model_from_environment() -> DeepSeekModel:
    if sys.platform != "linux":
        raise RunUnavailable("模型执行需要 Linux 或 WSL。")
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        raise RunUnavailable("本机后端尚未配置 DEEPSEEK_API_KEY。")
    return DeepSeekModel(key)


@dataclass
class PendingApproval:
    id: str
    tool: str
    arguments: dict
    deadline: float
    expires_at: float
    ready: Event = field(default_factory=Event)
    answer: bool | None = None


@dataclass
class LiveRun:
    id: str
    status: str = "starting"
    task: Task | None = None
    pending: PendingApproval | None = None
    detail: str | None = None
    final_text: str | None = None
    steps: int | None = None
    cost_usd: float | None = None


class RunManager:
    def __init__(
        self, database: Path, workspaces: Path,
        provider_factory: Callable[[], DeepSeekModel] = model_from_environment,
    ) -> None:
        self.database = database
        self.workspaces = workspaces
        self.provider_factory = provider_factory
        self._lock = Lock()
        self._runs: dict[str, LiveRun] = {}
        self._active: str | None = None
        self._latest: str | None = None
        self._closed = False

    def start(
        self, repo: Path, requirement: str, max_steps: int, budget: float,
        attachments: tuple[TaskAttachment, ...] = (),
    ) -> str:
        with self._lock:
            if self._closed:
                raise RunUnavailable("运行服务正在关闭。")
            if self._active:
                raise RunConflict("已有任务在运行，请等待它结束。")
            provider = self.provider_factory()
            run = LiveRun(new_task_id())
            done = list(self._runs)
            if len(done) >= 24:
                self._runs.pop(done[0])
            self._runs[run.id] = run
            self._active = run.id
            self._latest = run.id
            worker = Thread(
                target=self._worker,
                args=(run, repo, requirement, provider, max_steps, budget, attachments),
                daemon=True, name=f"devsquad-{run.id}",
            )
            try:
                worker.start()
            except RuntimeError:
                self._runs.pop(run.id)
                self._active = None
                self._latest = done[-1] if done else None
                raise RunUnavailable("无法启动运行线程。") from None
            return run.id

    def _worker(self, run, repo, requirement, provider, max_steps, budget, attachments) -> None:
        status = "error"
        detail = None
        result = None
        final_text = None
        try:
            with execution_lease(self.database, run.id):
                task = create_task(
                    repo=repo, title=requirement, workspaces_dir=self.workspaces,
                    db_path=self.database, task_id=run.id, executing=True,
                )
                with self._lock:
                    run.task = task
                    run.status = "running"
                result = execute_task(
                    task, self.database, provider, max_steps, budget,
                    lambda name, args: self._confirm(run, name, args),
                    attachments,
                )
                status = result.status
                final_text = provider.redact(result.final_text) if result.final_text else None
        except (ModelError, TaskError, OSError, SandboxError):
            detail = "运行失败。请检查仓库路径、本机凭据、模型连接或沙箱配置。"
        except Exception:
            detail = "运行因本机执行错误结束。"
        finally:
            with self._lock:
                run.status = status
                run.pending = None
                run.detail = detail
                if result is not None:
                    run.final_text = final_text
                    run.steps = result.steps
                    run.cost_usd = result.cost_usd
                self._active = None

    def _confirm(self, run: LiveRun, name: str, args: dict) -> bool | None:
        pending = PendingApproval(
            uuid4().hex, name, args, monotonic() + APPROVAL_SECONDS, time() + APPROVAL_SECONDS,
        )
        with self._lock:
            if self._closed:
                return None
            run.pending = pending
            run.status = "waiting_approval"
        pending.ready.wait(APPROVAL_SECONDS)
        with self._lock:
            run.pending = None
            run.status = "running"
            return None if self._closed else pending.answer is True

    def decide(self, run_id: str, approval_id: str, approve: bool) -> None:
        with self._lock:
            run = self._runs[run_id]
            pending = run.pending
            if (
                pending is None or pending.id != approval_id or pending.ready.is_set()
                or monotonic() >= pending.deadline or self._closed
            ):
                raise RunConflict("这条审批已处理或已过期。")
            pending.answer = approve
            pending.ready.set()

    def latest(self) -> str | None:
        with self._lock:
            return self._latest

    def snapshot(self, run_id: str) -> dict:
        with self._lock:
            run = self._runs[run_id]
            pending = run.pending
            proposal = (
                {"id": pending.id, "tool": pending.tool, "arguments": pending.arguments,
                 "expires_at": pending.expires_at}
                if pending is not None and not pending.ready.is_set() else None
            )
            task = run.task
            view = {
                "id": run.id, "status": run.status, "approval": proposal,
                "worktree": task.worktree_path if task else None,
                "branch": task.branch if task else None,
                "detail": run.detail, "final_text": run.final_text,
                "steps": run.steps, "estimated_cost_usd": run.cost_usd,
            }
        view["events"] = [
            trace_summary(event) for event in TraceRecorder(self.database).list_events(run_id)
        ] if task else []
        return view

    def patch(self, run_id: str) -> dict:
        with self._lock:
            task = self._runs[run_id].task
        if task is None:
            raise RunConflict("任务工作区还未就绪。")
        return current_patch(Path(task.worktree_path))

    def close(self) -> None:
        with self._lock:
            self._closed = True
            if self._active:
                pending = self._runs[self._active].pending
                if pending:
                    pending.ready.set()


def current_patch(root: Path) -> dict:
    tracked = git(root, "diff", "--no-ext-diff", "--no-textconv", "--no-renames", "--name-only",
                  "-z", "HEAD", "--").stdout.split("\0")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout.split("\0")
    files = WorktreeFiles(root)
    parts = []
    clipped = False
    try:
        paths = [(path, False) for path in tracked if path]
        paths.extend((path, True) for path in untracked if path)
        clipped = len(paths) > 40
        for path, new in paths[:40]:
            try:
                checked = files.guard.check_read(path)
                git_metadata = any(part.casefold() == ".git" for part in Path(path).parts)
                if checked != root / path or git_metadata:
                    clipped = True
                    continue
                try:
                    files.read_file(str(checked))
                except FileNotFoundError:
                    if new or int(git(root, "cat-file", "-s", f"HEAD:{path}").stdout) > _MAX_PATCH:
                        clipped = True
                        continue
                if new:
                    diff = git(
                        root, "diff", "--no-index", "--no-ext-diff", "--no-textconv", "--no-color",
                        "--", os.devnull, path, check=False,
                    )
                    if diff.returncode not in {0, 1}:
                        clipped = True
                        continue
                    patch = diff.stdout
                else:
                    patch = git(
                        root, "--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv",
                        "--no-renames", "--no-color", "HEAD", "--", path,
                    ).stdout
            except (OSError, ValueError, TaskError, SandboxError):
                clipped = True
                continue
            parts.append(patch)
    finally:
        files.close()
    patch = "".join(parts)
    return {"patch": patch[:_MAX_PATCH], "truncated": clipped or len(patch) > _MAX_PATCH}
