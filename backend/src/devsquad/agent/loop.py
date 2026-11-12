"""Agent main loop.

STATUS: interface only. The project owner implements ``run_loop``.
Tests: ``backend/tests/test_agent_loop.py``
Run the failing tests with::

    pytest -m student --runxfail tests/test_agent_loop.py

The tests inject a fake model. This function must not open a socket and
must not import an LLM SDK.

Expected ``messages`` shape (the tests assert this)::

    {"role": "user", "content": "<task text>"}
    {"role": "assistant", "content": "", "tool_name": "<name>", "tool_args": { ... }}
    {"role": "tool", "name": "<name>", "content": "<tool return value as str>"}
    {"role": "assistant", "content": "<final text>"}

A tool result uses the return value when it is a ``str``, otherwise ``str(value)``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelTurn:
    """One scripted or real model response.

    Set ``final_text`` and leave ``tool_name`` as None to finish.
    Set ``tool_name`` to request a tool. ``cost_usd`` is the price of this
    response and must be ``>= 0``. Tests use values such as ``0.25`` and
    ``0.5`` because those fractions are exact in binary floating point.
    ``0.1 + 0.2`` is not ``0.3``; a later version can switch the unit to
    integer micro-dollars. This PR keeps USD floats and compares with ``>``.
    """

    cost_usd: float
    final_text: str | None = None
    tool_name: str | None = None
    tool_args: dict | None = None


@dataclass(frozen=True)
class LoopResult:
    """Outcome of :func:`run_loop`.

    ``status`` is one of ``completed``, ``step_limit``, ``cost_limit``,
    ``denied``, ``ask_human``, or ``error``.
    ``steps`` counts model completions that actually ran, not peeks.
    ``cost_usd`` is the sum of those completions.
    ``messages`` is the transcript, including the original user task.
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
) -> LoopResult:
    """Run the agent until it finishes or a limit trips.

    TODO(student): replace this body. The tests are the spec. Suggested order:

    1. If ``max_steps < 1`` or ``max_cost_usd < 0``, raise ``ValueError``
       before calling the model.
    2. Start ``messages`` at ``[{"role": "user", "content": task}]``.
    3. On every iteration, in this order:
       a. If ``steps >= max_steps``, return ``status="step_limit"``. Do not
          call ``peek_cost`` or ``complete`` again.
       b. ``upcoming = float(model.peek_cost(messages))``. If
          ``spent + upcoming > max_cost_usd``, return ``status="cost_limit"``
          and do **not** call ``model.complete``. Landing exactly on the
          budget is allowed; the comparison is ``>``, not ``>=``.
       c. ``turn = model.complete(messages)``. Add ``turn.cost_usd`` to
          ``spent`` and count one step.
       d. If ``turn.tool_name`` is None, append
          ``{"role": "assistant", "content": turn.final_text}`` and return
          ``status="completed"`` with that final text. If ``final_text`` is
          also None, return ``status="error"`` instead.
       e. Otherwise append the assistant tool message shown in the module
          docstring, then ``decision = permissions.check(name, args)`` where
          ``args`` is ``dict(turn.tool_args or {})``.
          * ``Decision.DENY``: do not call the tool. Return ``status="denied"``.
          * ``Decision.ASK_HUMAN``: do not call the tool. Return
            ``status="ask_human"``.
          * ``Decision.ALLOW``: if ``name`` is not in ``tools``, return
            ``status="error"`` without calling anything. Otherwise call
            ``tools[name](**args)``, append the tool message, and loop.
          * Any other decision: return ``status="error"``.
    4. Do not catch exceptions raised by the tool. Let them propagate.
    5. ``Decision`` lives in ``devsquad.tools.permissions``.

    Recording each step with ``TraceRecorder`` is a good follow-up. The
    tests in this PR do not require that write, and they do not require a
    network call.

    Raises:
        NotImplementedError: always, until this body is replaced.
        ValueError: once implemented, for a non-positive step limit or a
            negative cost limit.
    """
    raise NotImplementedError(
        "student TODO: implement devsquad.agent.loop.run_loop "
        "(step limit, cost limit, tool dispatch). "
        "See this docstring and tests/test_agent_loop.py. "
        "Run: pytest -m student --runxfail tests/test_agent_loop.py"
    )
