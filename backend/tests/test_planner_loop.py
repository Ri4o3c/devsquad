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
def test_disabled_preserves_legacy_behavior(options):
    coder, planner_model, recorder = Mock(), Mock(), Mock()
    with pytest.raises(NotImplementedError, match="尚未实现 devsquad.agent.loop.run_loop"):
        run_loop(
            task="search", model=coder, tools={}, permissions=Mock(),
            max_steps=2, max_cost_usd=1, planner_model=planner_model,
            trace_recorder=recorder, **options,
        )
    coder.peek_cost.assert_not_called()
    coder.complete.assert_not_called()
    planner_model.complete.assert_not_called()
    recorder.record.assert_not_called()


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
