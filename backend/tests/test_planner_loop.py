"""验证规划进入编码上下文和 trace，以及关闭时的兼容性。"""

from unittest.mock import Mock

import pytest

from devsquad.agent.loop import ModelTurn, run_loop
from devsquad.tools.permissions import Decision


def test_loop_receives_plan_and_records_trace(tmp_path):
    (tmp_path / "search.py").write_text("def search_code():\n    return []\n")
    planner_model = Mock(complete=Mock(return_value=ModelTurn(0, final_text="先修改检索")))
    coder = Mock(peek_cost=Mock(return_value=0))
    recorder = Mock()

    def complete(messages):
        planned = messages[1]["content"]
        assert messages[0] == {"role": "user", "content": "修改 search_code 检索"}
        assert "先修改检索" in planned
        assert "相关文件" in planned and "search.py:1-2" in planned
        assert "来自检索 #1" in planned and "关键词=" in planned
        recorder.record.assert_called_once()
        assert recorder.record.call_args.args == (
            "task-1", "run.plan", {"plan": planned.split("\n", 1)[1]}
        )
        return ModelTurn(0, final_text="编码完成")

    coder.complete.side_effect = complete
    result = run_loop(
        task="修改 search_code 检索", model=coder, tools={}, permissions=Mock(),
        max_steps=2, max_cost_usd=1, planner_enabled=True, repo_root=tmp_path,
        planner_model=planner_model, trace_recorder=recorder, task_id="task-1",
    )
    assert result.status == "completed" and result.final_text == "编码完成"
    assert result.steps == 1 and result.cost_usd == 0
    planner_model.complete.assert_called_once()


@pytest.mark.parametrize("options", [{}, {"planner_enabled": False}])
def test_disabled_runs_without_planning(options):
    coder = Mock(peek_cost=Mock(return_value=0.25),
                 complete=Mock(return_value=ModelTurn(0.25, final_text="done")))
    planner_model, recorder = Mock(), Mock()
    result = run_loop(
        task="search", model=coder, tools={}, permissions=Mock(),
        max_steps=2, max_cost_usd=1, planner_model=planner_model,
        trace_recorder=recorder, task_id="task-1", **options,
    )
    assert result.status == "completed" and result.final_text == "done"
    assert result.steps == 1 and result.cost_usd == 0.25
    assert result.messages == [
        {"role": "user", "content": "search"},
        {"role": "assistant", "content": "done"},
    ]
    planner_model.assert_not_called()
    planner_model.complete.assert_not_called()
    recorder.record.assert_called_once_with(
        "task-1", "loop.result", {"status": "completed", "steps": 1, "cost_usd": 0.25},
    )


def test_disabled_completes_tool_round_trip_without_planner_model(tmp_path):
    coder = Mock(peek_cost=Mock(return_value=0.25))
    coder.complete.side_effect = [
        ModelTurn(0.25, tool_name="read", tool_args={"path": "a.py"}),
        ModelTurn(0.25, final_text="done"),
    ]
    tool = Mock(return_value="body")
    permissions = Mock(check=Mock(return_value=Decision.ALLOW))
    result = run_loop(
        task="search", model=coder, tools={"read": tool}, permissions=permissions,
        max_steps=2, max_cost_usd=0.5, planner_enabled=False, repo_root=tmp_path,
    )
    assert result.status == "completed" and result.final_text == "done"
    assert result.steps == 2 and result.cost_usd == 0.5
    permissions.check.assert_called_once_with("read", {"path": "a.py"})
    tool.assert_called_once_with(path="a.py")
    assert result.messages == [
        {"role": "user", "content": "search"},
        {"role": "assistant", "content": "", "tool_name": "read",
         "tool_args": {"path": "a.py"}},
        {"role": "tool", "name": "read", "content": "body"},
        {"role": "assistant", "content": "done"},
    ]


def test_enabled_requires_trace_before_planning(tmp_path):
    planner_model = Mock()
    with pytest.raises(ValueError, match="记录器"):
        run_loop(
            task="search", model=Mock(), tools={}, permissions=Mock(),
            max_steps=2, max_cost_usd=1, planner_enabled=True, repo_root=tmp_path,
            planner_model=planner_model,
        )
    planner_model.complete.assert_not_called()


def test_enabled_loop_dispatches_tools(tmp_path):
    coder = Mock(peek_cost=Mock(return_value=0))
    coder.complete.side_effect = [
        ModelTurn(0, tool_name="read", tool_args={"path": "a.py"}),
        ModelTurn(0, final_text="done"),
    ]
    tool = Mock(return_value="body")
    result = run_loop(
        task="search", model=coder, tools={"read": tool},
        permissions=Mock(check=Mock(return_value=Decision.ALLOW)),
        max_steps=2, max_cost_usd=1, planner_enabled=True, repo_root=tmp_path,
        planner_model=Mock(complete=Mock(return_value=ModelTurn(0, final_text="步骤"))),
        trace_recorder=Mock(), task_id="task-1",
    )
    assert result.status == "completed"
    tool.assert_called_once_with(path="a.py")
    assert result.messages[-2] == {"role": "tool", "name": "read", "content": "body"}
