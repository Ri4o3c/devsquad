"""Exercise the CLI, HTTP adapter, task persistence, and approval together."""

from __future__ import annotations

import io
import json
import os
import sys
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from devsquad import cli
from devsquad.tasks import TaskError, TaskStore, cleanup_task
from devsquad.traces import TraceRecorder

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="interactive agent uses Linux fd tools",
)


class ScriptedProvider:
    def __init__(self, actions: list[dict]) -> None:
        self.actions = iter(actions)
        self.requests = []

    def open(self, request, timeout):
        assert request.full_url == "https://api.deepseek.com/chat/completions"
        self.requests.append(json.loads(request.data))
        message = {"content": json.dumps(next(self.actions))}
        return io.BytesIO(json.dumps({
            "usage": {"prompt_tokens": 110, "completion_tokens": 22},
            "choices": [{"finish_reason": "stop", "message": message}],
        }).encode())


def _run(repo: Path, *args):
    return CliRunner().invoke(cli.app, [
        "run", "--repo", str(repo), "--task", "Update the greeting",
        "--model", "deepseek-flash", *args,
    ])


@pytest.mark.parametrize(
    "approval,result_status,expected_text,calls",
    [(True, "completed", "welcome\n", 3), (False, "completed", "hello\n", 3),
     (None, "ask_human", "hello\n", 2)],
)
def test_cli_approval(
    git_repo, cli_env, monkeypatch, approval, result_status, expected_text, calls,
):
    provider = ScriptedProvider([
        {"tool_name": "read_file", "tool_args": {"path": "README.md"}},
        {"tool_name": "edit_file", "tool_args": {
            "path": "README.md", "old_text": "hello", "new_text": "welcome",
        }},
        {"final_text": "The interaction is complete."},
    ])
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-provider-key")
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: provider)
    previews = []

    def confirm(name, args):
        previews.append((name, args))
        task = TaskStore(cli_env["db"]).list_tasks()[0]
        assert task.status == "running"
        with pytest.raises(TaskError, match="task is running"):
            cleanup_task(task_id=task.id, db_path=cli_env["db"], force=True)
        return approval

    monkeypatch.setattr(cli, "_confirm_tool", confirm)
    result = _run(git_repo)
    assert result.exit_code == (0 if result_status == "completed" else 1), result.output
    assert f"result: {result_status}" in result.stdout
    assert previews == [("edit_file", {
        "path": "README.md", "old_text": "hello", "new_text": "welcome",
    })]
    assert len(provider.requests) == calls
    latest = provider.requests[1]["messages"][-1]["content"]
    assert json.loads(latest)["untrusted_data"] == "hello\n"
    assert "fixture-provider-key" not in json.dumps(provider.requests)

    task, = TaskStore(cli_env["db"]).list_tasks()
    assert task.status == result_status
    assert (Path(task.worktree_path) / "README.md").read_text() == expected_text
    assert (git_repo / "README.md").read_text() == "hello\n"
    trace = TraceRecorder(cli_env["db"]).list_events(task.id)
    decisions = [
        event.payload["approved"] for event in trace if event.event_type == "tool.approval"
    ]
    assert decisions == ([] if approval is None else [approval])
    assert "welcome" not in str(trace)
    if approval is False:
        assert "Operator declined." in provider.requests[2]["messages"][-1]["content"]


def test_below_request_budget_avoids_provider(git_repo, cli_env, monkeypatch):
    provider = ScriptedProvider([])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: provider)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-provider-key")
    result = _run(git_repo, "--max-cost-usd", "0.000000001")
    assert result.exit_code == 1
    assert "result: cost_limit" in result.stdout
    assert provider.requests == []


def test_approved_edit_refuses_a_stale_preview(git_repo, cli_env, monkeypatch):
    provider = ScriptedProvider([{
        "tool_name": "edit_file",
        "tool_args": {"path": "README.md", "old_text": "hello", "new_text": "welcome"},
    }])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: provider)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-provider-key")

    def change_during_confirmation(name, args):
        task = TaskStore(cli_env["db"]).list_tasks()[0]
        (Path(task.worktree_path) / "README.md").write_text("another edit\n", encoding="utf-8")
        return True

    monkeypatch.setattr(cli, "_confirm_tool", change_during_confirmation)
    result = _run(git_repo)
    assert result.exit_code == 1
    assert "result: denied" in result.stdout
    task, = TaskStore(cli_env["db"]).list_tasks()
    assert (Path(task.worktree_path) / "README.md").read_text() == "another edit\n"


def test_approved_command_uses_the_isolated_worktree(git_repo, cli_env, monkeypatch):
    binary = Path(os.environ.get("DEVSQUAD_BWRAP", "/usr/bin/bwrap"))
    if not binary.is_file():
        pytest.skip("requires an installed Bubblewrap launcher")
    script = "from pathlib import Path; assert Path('README.md').exists(); print('isolated-cwd')"
    argv = ["python3", "-c", script]
    provider = ScriptedProvider([
        {"tool_name": "run_command", "tool_args": {"args": argv}},
        {"final_text": "Command observed."},
    ])
    monkeypatch.setattr(urllib.request, "build_opener", lambda *_: provider)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-provider-key")

    def confirm(name, args):
        assert name == "run_command"
        assert args == {"args": argv, "cwd": "."}
        return True

    monkeypatch.setattr(cli, "_confirm_tool", confirm)
    result = _run(git_repo)
    assert result.exit_code == 0, result.output
    task, = TaskStore(cli_env["db"]).list_tasks()
    trace = TraceRecorder(cli_env["db"]).list_events(task.id)
    done, = [event for event in trace if event.event_type == "tool.completed"]
    assert done.payload["returncode"] == 0
