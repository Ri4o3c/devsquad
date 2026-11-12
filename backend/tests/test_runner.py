"""Command runner tests: argument lists, timeout, output cap, env, cwd."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from devsquad.sandbox.pathguard import PathEscapeError
from devsquad.sandbox.runner import CommandRejected, CommandRunner, minimal_env


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    root.mkdir()
    return root


def test_rejects_a_shell_string(worktree: Path) -> None:
    runner = CommandRunner(worktree)
    with pytest.raises(CommandRejected):
        runner.run("echo hello && echo pwned")  # type: ignore[arg-type]


def test_argument_list_does_not_invoke_the_shell(worktree: Path, tmp_path: Path) -> None:
    marker = tmp_path / "should-not-exist"
    runner = CommandRunner(worktree, timeout_s=5)
    result = runner.run(
        [sys.executable, "-c", "print(123)", "&&", "touch", str(marker)],
    )
    assert result.returncode == 0
    assert result.stdout.strip() == b"123"
    assert result.timed_out is False
    assert not marker.exists()


def test_cwd_defaults_to_the_worktree(worktree: Path) -> None:
    runner = CommandRunner(worktree, timeout_s=5)
    result = runner.run([sys.executable, "-c", "import os; print(os.getcwd())"])
    assert result.returncode == 0
    assert Path(result.stdout.decode().strip()) == worktree.resolve()


def test_cwd_subdirectory_inside_the_worktree_is_allowed(worktree: Path) -> None:
    sub = worktree / "sub"
    sub.mkdir()
    runner = CommandRunner(worktree, timeout_s=5)
    result = runner.run(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd=sub,
    )
    assert Path(result.stdout.decode().strip()) == sub.resolve()


def test_cwd_outside_the_worktree_is_rejected(worktree: Path, tmp_path: Path) -> None:
    runner = CommandRunner(worktree, timeout_s=5)
    with pytest.raises(PathEscapeError):
        runner.run([sys.executable, "-c", "print(1)"], cwd=tmp_path)
    with pytest.raises(PathEscapeError):
        runner.run([sys.executable, "-c", "print(1)"], cwd="..")


def test_cwd_symlink_escape_is_rejected(worktree: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    link = worktree / "out"
    link.symlink_to(outside, target_is_directory=True)
    runner = CommandRunner(worktree, timeout_s=5)
    with pytest.raises(PathEscapeError):
        runner.run([sys.executable, "-c", "print(1)"], cwd=link)


def test_timeout_kills_a_sleeping_process(worktree: Path) -> None:
    runner = CommandRunner(worktree, timeout_s=0.4, max_output_bytes=1000)
    result = runner.run([sys.executable, "-c", "import time; time.sleep(15)"])
    assert result.timed_out is True
    assert result.duration_s < 5
    assert result.returncode != 0


def test_output_is_capped_and_exact_fit_is_not_truncated(worktree: Path) -> None:
    cap = 128
    runner = CommandRunner(worktree, timeout_s=5, max_output_bytes=cap)
    exact = runner.run(
        [sys.executable, "-c", f"import sys; sys.stdout.buffer.write(b'a'*{cap})"]
    )
    assert exact.truncated is False
    assert exact.stdout == b"a" * cap

    over = runner.run(
        [sys.executable, "-c", f"import sys; sys.stdout.buffer.write(b'b'*{cap + 4096})"]
    )
    assert over.truncated is True
    assert len(over.stdout) <= cap
    assert over.stdout == b"b" * cap


def test_child_does_not_inherit_secret_env(worktree: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-test-not-a-real-key")
    monkeypatch.setenv("GITHUB_TOKEN", "gh-test-not-a-real-token")
    monkeypatch.setenv("DEVSQUAD_TOKEN", "local-test-token")
    monkeypatch.setenv("DEVSQUAD_DB", "/tmp/devsquad-test.db")
    runner = CommandRunner(worktree, timeout_s=5)
    result = runner.run(
        [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"]
    )
    assert result.returncode == 0, result.stderr
    child = json.loads(result.stdout.decode())
    blob = result.stdout.decode()
    assert "OPENAI_API_KEY" not in child
    assert "AWS_SECRET_ACCESS_KEY" not in child
    assert "GITHUB_TOKEN" not in child
    assert "DEVSQUAD_TOKEN" not in child
    assert "DEVSQUAD_DB" not in child
    assert "sk-test-not-a-real-key" not in blob
    assert "aws-test-not-a-real-key" not in blob
    assert "PATH" in child


def test_minimal_env_drops_secret_looking_names_even_if_allowlisted() -> None:
    env = minimal_env(
        {
            "PATH": "/usr/bin",
            "HOME": "/tmp/home",
            "OPENAI_API_KEY": "nope",
            "LANG": "C",
        }
    )
    assert env["PATH"] == "/usr/bin"
    assert "OPENAI_API_KEY" not in env
    assert "TOKEN" not in env["HOME"]


def test_cwd_must_be_a_directory(worktree: Path) -> None:
    (worktree / "file.txt").write_text("x\n", encoding="utf-8")
    runner = CommandRunner(worktree, timeout_s=5)
    with pytest.raises(CommandRejected):
        runner.run([sys.executable, "-c", "print(1)"], cwd=worktree / "file.txt")
