"""``devsquad.agent.loop.run_loop`` 的规格。

在作者实现主循环之前，这些测试会失败。它们标了 ``student`` 和
``xfail(strict=False)``，所以默认的 pytest 仍然是绿的。

    pytest -m student --runxfail tests/test_agent_loop.py
"""

from __future__ import annotations

import pytest

from devsquad.agent.loop import ModelTurn, run_loop
from devsquad.tools.permissions import Decision

pytestmark = [
    pytest.mark.student,
    pytest.mark.xfail(
        reason="student TODO: implement devsquad.agent.loop.run_loop",
        strict=False,
    ),
]


class ScriptedModel:
    def __init__(self, turns: list[ModelTurn]) -> None:
        self.turns = list(turns)
        self.completed: list[list[dict]] = []
        self.peeked = 0
        self.calls = 0

    def peek_cost(self, messages: list[dict]) -> float:
        self.peeked += 1
        return self.turns[self.calls].cost_usd

    def complete(self, messages: list[dict]) -> ModelTurn:
        self.completed.append([dict(message) for message in messages])
        turn = self.turns[self.calls]
        self.calls += 1
        return turn


class FakePermissions:
    def __init__(self, decision: Decision) -> None:
        self.decision = decision
        self.calls: list[tuple[str, dict]] = []

    def check(self, tool_name: str, arguments: dict) -> Decision:
        self.calls.append((tool_name, dict(arguments)))
        return self.decision


def test_final_answer_without_tools() -> None:
    model = ScriptedModel([ModelTurn(cost_usd=0.25, final_text="done")])
    result = run_loop(
        task="ship it",
        model=model,
        tools={},
        permissions=FakePermissions(Decision.ALLOW),
        max_steps=4,
        max_cost_usd=1,
    )
    assert result.status == "completed"
    assert result.final_text == "done"
    assert result.steps == 1
    assert result.cost_usd == pytest.approx(0.25)
    assert model.calls == 1
    assert model.peeked == 1
    assert result.messages == [
        {"role": "user", "content": "ship it"},
        {"role": "assistant", "content": "done"},
    ]


def test_step_limit_stops_before_another_model_call() -> None:
    calls: list[dict] = []

    def read_file(path: str) -> str:
        calls.append({"path": path})
        return "ok"

    model = ScriptedModel(
        [
            ModelTurn(cost_usd=0.25, tool_name="read_file", tool_args={"path": "a.py"}),
            ModelTurn(cost_usd=0.25, tool_name="read_file", tool_args={"path": "b.py"}),
            ModelTurn(cost_usd=0.25, tool_name="read_file", tool_args={"path": "c.py"}),
        ]
    )
    result = run_loop(
        task="read files",
        model=model,
        tools={"read_file": read_file},
        permissions=FakePermissions(Decision.ALLOW),
        max_steps=2,
        max_cost_usd=5,
    )
    assert result.status == "step_limit"
    assert result.steps == 2
    assert model.calls == 2
    assert model.peeked == 2
    assert calls == [{"path": "a.py"}, {"path": "b.py"}]


def test_cost_limit_does_not_run_the_call_that_would_exceed_the_budget() -> None:
    model = ScriptedModel(
        [
            ModelTurn(cost_usd=0.25, tool_name="read_file", tool_args={"path": "a.py"}),
            ModelTurn(cost_usd=0.5, final_text="too expensive"),
        ]
    )
    result = run_loop(
        task="budget",
        model=model,
        tools={"read_file": lambda path: "body"},
        permissions=FakePermissions(Decision.ALLOW),
        max_steps=5,
        max_cost_usd=0.5,
    )
    assert result.status == "cost_limit"
    assert result.final_text is None
    assert result.steps == 1
    assert result.cost_usd == pytest.approx(0.25)
    assert model.calls == 1
    assert model.peeked == 2
    assert any(message["role"] == "tool" for message in result.messages)


def test_exact_budget_is_allowed() -> None:
    model = ScriptedModel(
        [
            ModelTurn(cost_usd=0.25, tool_name="read_file", tool_args={"path": "a.py"}),
            ModelTurn(cost_usd=0.25, final_text="exact"),
        ]
    )
    result = run_loop(
        task="exact",
        model=model,
        tools={"read_file": lambda path: "body"},
        permissions=FakePermissions(Decision.ALLOW),
        max_steps=5,
        max_cost_usd=0.5,
    )
    assert result.status == "completed"
    assert result.final_text == "exact"
    assert result.cost_usd == pytest.approx(0.5)
    assert model.calls == 2


def test_first_call_over_budget_does_not_call_the_model() -> None:
    model = ScriptedModel([ModelTurn(cost_usd=0.5, final_text="no")])
    result = run_loop(
        task="too big",
        model=model,
        tools={},
        permissions=FakePermissions(Decision.ALLOW),
        max_steps=3,
        max_cost_usd=0.25,
    )
    assert result.status == "cost_limit"
    assert model.calls == 0
    assert model.peeked == 1
    assert result.steps == 0
    assert result.cost_usd == 0
    assert result.messages == [{"role": "user", "content": "too big"}]


def test_allow_dispatches_tool_and_feeds_the_result_back() -> None:
    def read_file(path: str) -> str:
        assert path == "a.py"
        return "file-body"

    model = ScriptedModel(
        [
            ModelTurn(cost_usd=0.0, tool_name="read_file", tool_args={"path": "a.py"}),
            ModelTurn(cost_usd=0.0, final_text="used the file"),
        ]
    )
    result = run_loop(
        task="read a.py",
        model=model,
        tools={"read_file": read_file},
        permissions=FakePermissions(Decision.ALLOW),
        max_steps=4,
        max_cost_usd=1,
    )
    assert result.status == "completed"
    assert result.final_text == "used the file"
    assert model.completed[0] == [{"role": "user", "content": "read a.py"}]
    assert model.completed[1] == [
        {"role": "user", "content": "read a.py"},
        {
            "role": "assistant",
            "content": "",
            "tool_name": "read_file",
            "tool_args": {"path": "a.py"},
        },
        {"role": "tool", "name": "read_file", "content": "file-body"},
    ]
    assert result.messages[-1] == {"role": "assistant", "content": "used the file"}


def test_deny_does_not_execute_the_tool() -> None:
    def boom(**kwargs):
        raise AssertionError("tool should not run")

    permissions = FakePermissions(Decision.DENY)
    model = ScriptedModel(
        [ModelTurn(cost_usd=0.0, tool_name="write_file", tool_args={"path": "a"})]
    )
    result = run_loop(
        task="write",
        model=model,
        tools={"write_file": boom},
        permissions=permissions,
        max_steps=3,
        max_cost_usd=1,
    )
    assert result.status == "denied"
    assert permissions.calls == [("write_file", {"path": "a"})]
    assert result.messages == [
        {"role": "user", "content": "write"},
        {
            "role": "assistant",
            "content": "",
            "tool_name": "write_file",
            "tool_args": {"path": "a"},
        },
    ]


def test_ask_human_does_not_execute_the_tool() -> None:
    def boom(**kwargs):
        raise AssertionError("tool should not run")

    result = run_loop(
        task="write",
        model=ScriptedModel(
            [ModelTurn(cost_usd=0.0, tool_name="write_file", tool_args={"path": "a"})]
        ),
        tools={"write_file": boom},
        permissions=FakePermissions(Decision.ASK_HUMAN),
        max_steps=3,
        max_cost_usd=1,
    )
    assert result.status == "ask_human"
    assert result.final_text is None


def test_unknown_tool_is_an_error_when_allowed() -> None:
    result = run_loop(
        task="nope",
        model=ScriptedModel([ModelTurn(cost_usd=0.0, tool_name="missing", tool_args={})]),
        tools={},
        permissions=FakePermissions(Decision.ALLOW),
        max_steps=3,
        max_cost_usd=1,
    )
    assert result.status == "error"


def test_invalid_limits_raise_value_error() -> None:
    model = ScriptedModel([ModelTurn(cost_usd=0.0, final_text="x")])
    with pytest.raises(ValueError):
        run_loop(
            task="x",
            model=model,
            tools={},
            permissions=FakePermissions(Decision.ALLOW),
            max_steps=0,
            max_cost_usd=1,
        )
    with pytest.raises(ValueError):
        run_loop(
            task="x",
            model=model,
            tools={},
            permissions=FakePermissions(Decision.ALLOW),
            max_steps=1,
            max_cost_usd=-0.1,
        )
    assert model.calls == 0
