"""智能体主循环。

状态：默认执行编码循环；开启 Planner 后先规划再编码。
测试：``backend/tests/test_agent_loop.py``

测试会注入一个假的模型。这个函数不能打开套接字，也不能导入大模型 SDK。

测试断言的 ``messages`` 形状如下::

    {"role": "user", "content": "<task text>"}
    {"role": "assistant", "content": "", "tool_name": "<name>", "tool_args": { ... }}
    {"role": "tool", "name": "<name>", "content": "<tool return value as str>"}
    {"role": "assistant", "content": "<final text>"}

工具结果如果本身是 ``str`` 就用返回值，否则用 ``str(value)``。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from devsquad.sandbox.pathguard import SandboxError
from devsquad.sandbox.runner import RunResult
from devsquad.tools.permissions import Decision
from devsquad.tools.sandbox import dispatch_tool

if TYPE_CHECKING:
    from devsquad.agent.planner import PlannerConfig, PlannerModel
    from devsquad.traces import TraceRecorder


@dataclass(frozen=True)
class ModelTurn:
    """一次脚本化的或真实的模型回复。

    设置 ``final_text`` 并且让 ``tool_name`` 保持 None，表示结束。
    设置 ``tool_name`` 表示要调用工具。``cost_usd`` 是这次回复的价格，
    必须 ``>= 0``。测试用 ``0.25`` 和 ``0.5`` 这样的值，因为这些小数在二进制
    浮点里是精确的。``0.1 + 0.2`` 不等于 ``0.3``；以后可以把单位改成整数的
    微美元。本 PR 仍用美元浮点，比较时用 ``>``。
    """

    cost_usd: float
    final_text: str | None = None
    tool_name: str | None = None
    tool_args: dict | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None


@dataclass(frozen=True)
class LoopResult:
    """:func:`run_loop` 的结果。

    ``status`` 是 ``completed``、``step_limit``、``cost_limit``、
    ``denied``、``ask_human`` 或 ``error`` 之一。
    ``steps`` 统计真正执行过的模型补全次数，不含预先查看。
    ``cost_usd`` 是这些补全的费用之和。
    ``messages`` 是对话记录，包含最初的用户任务。
    """

    status: str
    steps: int
    cost_usd: float
    final_text: str | None
    messages: list[dict]


def run_loop(
    *,
    task: str,
    model: object,
    tools: dict[str, Callable],
    permissions: object,
    max_steps: int,
    max_cost_usd: float,
    planner_enabled: bool = False,
    repo_root: str | Path | None = None,
    planner_model: PlannerModel | None = None,
    planner_config: PlannerConfig | None = None,
    trace_recorder: TraceRecorder | None = None,
    task_id: str | None = None,
    approve: Callable[[str, dict], bool | None] | None = None,
) -> LoopResult:
    """运行共享编码循环，开启规划时先生成计划并注入上下文。

    开启时必须提供仓库、独立规划模型、记录器和已有任务 ID。
    规划使用独立模型，步数和费用上限仅统计编码阶段。
    轨迹仅记录运行元数据。规划输出、检索词与代码片段仅传入模型上下文。
    """
    if not (max_steps >= 1 and max_cost_usd >= 0):
        raise ValueError("步数上限必须为正数，费用上限不能为负")
    messages = [{"role": "user", "content": task}]
    if planner_enabled and (repo_root is None or planner_model is None
                            or trace_recorder is None or not task_id):
        raise ValueError("开启 Planner 必须提供仓库、规划模型、记录器和任务 ID")
    if trace_recorder is not None and task_id:
        trace_recorder.record(task_id, "run.started", {"planner_enabled": planner_enabled})
    if planner_enabled:
        # 延迟导入，关闭时不加载或调用 Planner，也避免模型接口的循环导入。
        from devsquad.agent.planner import plan

        def report_retrieval(hits: int, failed: bool) -> None:
            trace_recorder.record(task_id, "run.retrieval", {
                "hit_count": hits, "retrieval_failed": failed,
            })

        started = monotonic()
        try:
            planned = plan(
                root=repo_root, requirement=task, model=planner_model,
                config=planner_config, report_retrieval=report_retrieval,
            )
        except Exception:
            trace_recorder.record(task_id, "run.plan.error", _elapsed(started))
            raise
        trace_recorder.record(task_id, "run.plan", _elapsed(started))
        messages.append({"role": "user", "content": "实施计划（含相关文件）\n" + planned})
    return _run_coding_loop(
        messages=messages, model=model, tools=tools, permissions=permissions,
        max_steps=max_steps, max_cost_usd=max_cost_usd,
        repo_root=repo_root if repo_root is not None else Path.cwd(),
        trace_recorder=trace_recorder, task_id=task_id,
        approve=approve,
    )


def _elapsed(started: float) -> dict[str, int]:
    return {"duration_ms": round((monotonic() - started) * 1000)}


def _run_coding_loop(
    *, messages: list[dict], model: object, tools: dict[str, Callable],
    permissions: object, max_steps: int, max_cost_usd: float,
    repo_root: str | Path, trace_recorder: TraceRecorder | None, task_id: str | None,
    approve: Callable[[str, dict], bool | None] | None = None,
) -> LoopResult:
    """编码阶段沿用原接口约定，限制步数、费用并检查工具权限。"""
    steps = 0
    spent = 0.0

    def record(event_type: str, **payload) -> None:
        if trace_recorder is not None and task_id:
            trace_recorder.record(task_id, event_type, payload)

    def result(status: str, final_text: str | None = None) -> LoopResult:
        record("loop.result", status=status, steps=steps, cost_usd=spent)
        return LoopResult(status, steps, spent, final_text, messages)

    while steps < max_steps:
        upcoming = float(model.peek_cost(messages))
        if not isfinite(upcoming) or upcoming < 0:
            return result("error")
        if spent + upcoming > max_cost_usd:
            return result("cost_limit")
        record("model.started", step=steps + 1)
        started = monotonic()
        try:
            turn = model.complete(messages)
        except Exception:
            record("model.error", step=steps + 1, **_elapsed(started))
            raise
        steps += 1
        if not isfinite(turn.cost_usd) or turn.cost_usd < 0:
            record("model.error", step=steps, **_elapsed(started))
            return result("error")
        spent += turn.cost_usd
        record("model.completed", step=steps, cost_usd=turn.cost_usd,
               input_tokens=getattr(turn, "input_tokens", None),
               output_tokens=getattr(turn, "output_tokens", None),
               **_elapsed(started))
        if spent > max_cost_usd:
            return result("cost_limit")
        if turn.tool_name is None:
            if turn.final_text is None:
                return result("error")
            messages.append({"role": "assistant", "content": turn.final_text})
            return result("completed", turn.final_text)
        name = turn.tool_name
        args = dict(turn.tool_args or {})
        messages.append({
            "role": "assistant", "content": "", "tool_name": name, "tool_args": args,
        })
        if name not in tools:
            record("tool.rejected", reason="unknown_tool", step=steps)
            return result("error")
        try:
            decision = permissions.check(name, args)
        except Exception:
            record("tool.rejected", tool_name=name, reason="permission_error")
            return result("error")
        if decision == Decision.DENY:
            record("tool.rejected", tool_name=name, reason="permission_denied")
            return result("denied")
        if decision == Decision.ASK_HUMAN:
            approved = approve(name, args) if approve is not None else None
            if approved is None:
                record("tool.rejected", tool_name=name, reason="ask_human")
                return result("ask_human")
            record("tool.approval", tool_name=name, step=steps, approved=approved is True)
            if approved is not True:
                messages.append({"role": "tool", "name": name, "content": "Operator declined."})
                continue
            decision = Decision.ALLOW
        if decision != Decision.ALLOW:
            record("tool.rejected", tool_name=name, reason="invalid_decision")
            return result("error")
        record("tool.started", step=steps, tool_name=name)
        started = monotonic()
        try:
            value = dispatch_tool(name, args, tools, repo_root)
        except SandboxError:
            record("tool.rejected", tool_name=name, reason="sandbox_denied", **_elapsed(started))
            return result("denied")
        except Exception:
            # 不保存异常原文，避免路径或敏感内容进入 trace 和模型上下文。
            record("tool.error", tool_name=name, reason="execution_failed", **_elapsed(started))
            messages.append({"role": "tool", "name": name, "content": "工具执行失败"})
            return result("error")
        details = _elapsed(started)
        if isinstance(value, RunResult):
            details.update(returncode=value.returncode, timed_out=value.timed_out)
        record("tool.completed", step=steps, tool_name=name, **details)
        messages.append({"role": "tool", "name": name, "content": str(value)})
    return result("step_limit")
