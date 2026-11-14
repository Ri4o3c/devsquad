"""编码循环的标准库测试；所有文件均为临时普通文件，不执行网络请求。"""

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch

from devsquad.agent.loop import ModelTurn, _run_coding_loop
from devsquad.tools.permissions import Decision


class CodingLoopTests(TestCase):
    def setUp(self):
        # 临时资源统一放在仓库内；外层冒烟脚本负责删除公共临时目录。
        base = Path(__file__).resolve().parents[1] / ".codex-tmp"
        base.mkdir(exist_ok=True)
        self.temp = TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.recorder = Mock()
        self.permissions = Mock(check=Mock(return_value=Decision.ALLOW))
        self.model = Mock(peek_cost=Mock(return_value=0.25))
        self.tool = Mock(return_value="ok")

    def run_turns(self, turns, *, max_steps=4, max_cost=2, tools=None):
        self.model.complete.side_effect = turns
        return _run_coding_loop(
            messages=[{"role": "user", "content": "task"}], model=self.model,
            tools={"test_tool": self.tool} if tools is None else tools,
            permissions=self.permissions, max_steps=max_steps, max_cost_usd=max_cost,
            repo_root=self.root, trace_recorder=self.recorder, task_id="task-1",
        )

    def assert_event(self, event_type, **payload):
        self.assertIn((("task-1", event_type, payload), {}), self.recorder.record.call_args_list)

    def test_step_limit_stops_before_next_model_call(self):
        result = self.run_turns([ModelTurn(0.25, tool_name="test_tool")], max_steps=1)
        self.assertEqual(result.status, "step_limit")
        self.assertEqual(self.model.complete.call_count, 1)
        self.assertEqual(self.model.peek_cost.call_count, 1)
        self.tool.assert_called_once()

    def test_cost_limit_stops_before_over_budget_call(self):
        result = self.run_turns([ModelTurn(0.25, tool_name="test_tool")], max_cost=0.25)
        self.assertEqual(result.status, "cost_limit")
        self.assertEqual(result.cost_usd, 0.25)
        self.assertEqual(self.model.complete.call_count, 1)
        self.assertEqual(self.model.peek_cost.call_count, 2)

    def test_exact_budget_allows_final_answer(self):
        result = self.run_turns([ModelTurn(0.25, final_text="done")], max_cost=0.25)
        self.assertEqual(result.status, "completed")

    def test_permission_denied_is_traced_without_execution(self):
        self.permissions.check.return_value = Decision.DENY
        result = self.run_turns([ModelTurn(0.25, tool_name="test_tool")])
        self.assertEqual(result.status, "denied")
        self.tool.assert_not_called()
        self.assert_event("tool.rejected", tool_name="test_tool", reason="permission_denied")

    def test_tool_exception_returns_error_without_details(self):
        self.tool.side_effect = RuntimeError("内部详情")
        result = self.run_turns([ModelTurn(0.25, tool_name="test_tool")])
        self.assertEqual(result.status, "error")
        self.assertNotIn("内部详情", str(result) + str(self.recorder.record.call_args_list))
        self.assert_event("tool.error", tool_name="test_tool", reason="execution_failed")

    def test_unknown_tool_returns_error(self):
        result = self.run_turns([ModelTurn(0.25, tool_name="unknown")])
        self.assertEqual(result.status, "error")
        self.permissions.check.assert_not_called()
        self.assert_event("tool.rejected", tool_name="unknown", reason="unknown_tool")

    def test_path_escape_is_blocked_even_if_policy_allows(self):
        result = self.run_turns(
            [ModelTurn(0.25, tool_name="read_file", tool_args={"path": "../outside.txt"})],
            tools={"read_file": self.tool},
        )
        self.assertEqual(result.status, "denied")
        self.tool.assert_not_called()
        self.assert_event("tool.rejected", tool_name="read_file", reason="sandbox_denied")

    def test_symlink_escape_is_blocked(self):
        outside = self.root.parent / "outside.txt"
        outside.write_text("ordinary fixture")
        (self.root / "link.txt").symlink_to(outside)
        result = self.run_turns(
            [ModelTurn(0.25, tool_name="read_file", tool_args={"path": "link.txt"})],
            tools={"read_file": self.tool},
        )
        self.assertEqual(result.status, "denied")
        self.tool.assert_not_called()

    def test_safe_read_receives_guarded_absolute_path(self):
        result = self.run_turns(
            [ModelTurn(0.25, tool_name="read_file", tool_args={"path": "a.txt"}),
             ModelTurn(0.25, final_text="done")], tools={"read_file": self.tool},
        )
        self.assertEqual(result.status, "completed")
        self.tool.assert_called_once_with(path=str(self.root / "a.txt"))

    def test_command_uses_strict_runner_instead_of_callback(self):
        # 只模拟 runner，不创建子进程，不读取进程环境，也不探测网络。
        with patch("devsquad.tools.sandbox.CommandRunner") as runner:
            result = self.run_turns(
                [ModelTurn(0.25, tool_name="run_command", tool_args={"args": ["echo", "ok"]}),
                 ModelTurn(0.25, final_text="done")], tools={"run_command": self.tool},
            )
        self.assertEqual(result.status, "completed")
        runner.assert_called_once_with(self.root, require_network_isolation=True)
        runner.return_value.run.assert_called_once_with(args=["echo", "ok"])
        self.tool.assert_not_called()

    def test_missing_network_namespace_rejects_without_process(self):
        with patch("devsquad.sandbox.runner.network_namespace_command", return_value=None), \
                patch("devsquad.sandbox.runner.subprocess.Popen") as popen:
            result = self.run_turns(
                [ModelTurn(0.25, tool_name="run_command", tool_args={"args": ["echo", "ok"]})],
                tools={"run_command": self.tool},
            )
        self.assertEqual(result.status, "denied")
        popen.assert_not_called()
        self.tool.assert_not_called()

    def test_network_tool_is_blocked_even_if_policy_allows(self):
        result = self.run_turns(
            [ModelTurn(0.25, tool_name="http_get")], tools={"http_get": self.tool},
        )
        self.assertEqual(result.status, "denied")
        self.tool.assert_not_called()

    def test_git_write_is_blocked_before_callback(self):
        result = self.run_turns(
            [ModelTurn(0.25, tool_name="write_file", tool_args={"path": ".git/config"})],
            tools={"write_file": self.tool},
        )
        self.assertEqual(result.status, "denied")
        self.tool.assert_not_called()
