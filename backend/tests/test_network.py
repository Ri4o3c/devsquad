"""网络策略、探测降级和本机真实 connect 验证。"""

from __future__ import annotations

import socket
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

from devsquad.sandbox import network
from devsquad.sandbox import runner as runner_module
from devsquad.sandbox.network import NetworkIsolation, network_command_reason
from devsquad.sandbox.runner import CommandRejected, CommandRunner


@pytest.mark.parametrize(
    "argv",
    [
        [name]
        for name in (
            "curl",
            "wget",
            "ssh",
            "scp",
            "sftp",
            "rsync",
            "nc",
            "ncat",
            "netcat",
            "telnet",
            "ftp",
        )
    ]
    + [
        ["git", verb]
        for verb in ("push", "fetch", "pull", "clone", "remote", "ls-remote", "submodule")
    ]
    + [
        ["git", "-C", "x", "push"],
        ["GIT.EXE", "fetch"],
        [r"C:\Git\git.EXE", "-c", "k=v", "--git-dir=x", "--no-pager", "push"],
        ["git", "--work-tree", "x", "--git-dir", "x", "fetch"],
        ["pip", "install"],
        ["pip3", "download"],
        ["python", "-m", "pip", "install"],
        ["python3", "-m", "pip", "download"],
        ["py", "-3", "-m", "pip", "install"],
        [sys.executable, "-m", "pip", "install"],
        ["npm", "i"],
        ["npm.cmd", "ci"],
        ["pnpm.bat", "add"],
        ["yarn", "install"],
        ["uv", "pip", "install"],
        ["uv", "add"],
        ["uv", "sync"],
        ["cargo", "install"],
        ["go", "get"],
    ],
)
def test_network_commands(argv: list[str]) -> None:
    assert network_command_reason(argv)


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["git", "status"],
        ["git", "-C", "push", "status"],
        ["python", "-c", "print(1)"],
        [sys.executable, "-c", "print(1)"],
        ["python", "-c", "pass", "-m", "pip", "install"],
        ["npm", "run", "test"],
        ["pip", "list"],
        ["uv", "run", "pytest"],
    ],
)
def test_local_commands(argv: list[str]) -> None:
    assert network_command_reason(argv) is None


@pytest.mark.parametrize("argv", [["git", "push"], ["curl"], ["npm", "i"]])
def test_rejects_before_any_process_starts(tmp_path, monkeypatch, argv) -> None:
    popen = Mock(side_effect=AssertionError("不应启动任何进程，包括探测"))
    monkeypatch.setattr(subprocess, "Popen", popen)
    with pytest.raises(CommandRejected, match="默认禁止联网"):
        CommandRunner(tmp_path).run(argv)
    popen.assert_not_called()


def test_allow_network_bypasses_policy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        runner_module,
        "network_namespace_command",
        Mock(
            side_effect=AssertionError("开放网络时无需探测"),
        ),
    )
    with pytest.raises(FileNotFoundError):
        CommandRunner(tmp_path, allow_network=True).run([str(tmp_path / "curl")])


def test_policy_only_and_fail_closed(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(runner_module, "network_namespace_command", lambda: None)
    argv = [sys.executable, "-c", "print(1)"]
    result = CommandRunner(tmp_path).run(argv)
    assert result.returncode == 0
    assert result.network == NetworkIsolation.POLICY_ONLY
    assert result.args == tuple(argv)
    with pytest.raises(CommandRejected, match="不支持非特权网络隔离"):
        CommandRunner(tmp_path, require_network_isolation=True).run(argv)


@pytest.fixture
def local_server():
    accepted = threading.Event()
    stop = threading.Event()
    try:
        server_socket = socket.socket()
        server_socket.bind(("127.0.0.1", 0))
    except PermissionError as exc:
        if "server_socket" in locals():
            server_socket.close()
        pytest.skip(f"当前执行沙箱禁止父进程创建或绑定本机 socket，无法验证 connect：{exc}")
    with server_socket as server:
        server.listen()
        server.settimeout(0.1)

        def accept() -> None:
            while not stop.is_set():
                try:
                    conn, _ = server.accept()
                except TimeoutError:
                    continue
                with conn:
                    accepted.set()
                return

        thread = threading.Thread(target=accept)
        thread.start()
        try:
            yield server.getsockname()[1], accepted
        finally:
            stop.set()
            thread.join(timeout=2)
            assert not thread.is_alive()


def _connect_command(port: int) -> list[str]:
    return [
        sys.executable,
        "-c",
        (
            "import socket, sys\n"
            "try:\n"
            f"    s = socket.create_connection(('127.0.0.1', {port}), timeout=1)\n"
            "except OSError:\n"
            "    sys.exit(23)\n"
            "else:\n"
            "    s.close()\n"
        ),
    ]


def require_namespace() -> None:
    if network.network_namespace_command() is None:
        pytest.skip(
            f"{sys.platform} 无可用非特权 user/net namespace：Linux 可能缺少 unshare、"
            "或被内核/AppArmor 禁止；Windows/macOS 第一期只有 argv 拦截"
        )


def test_namespace_blocks_real_connect(tmp_path, local_server) -> None:
    require_namespace()
    port, accepted = local_server
    argv = _connect_command(port)
    result = CommandRunner(tmp_path).run(argv)
    assert result.network == NetworkIsolation.NAMESPACE
    assert result.args == tuple(argv)
    assert result.returncode == 23, result.stderr
    assert not accepted.is_set()


def test_allowed_real_connect(tmp_path, local_server) -> None:
    port, accepted = local_server
    result = CommandRunner(tmp_path, allow_network=True).run(_connect_command(port))
    assert result.returncode == 0, result.stderr
    assert result.network == NetworkIsolation.ALLOWED
    assert accepted.wait(timeout=2)


@pytest.mark.parametrize(
    "outcome",
    [0, 1, OSError("拒绝"), subprocess.TimeoutExpired("unshare", 3)],
)
def test_probe_cached_and_degrades(monkeypatch, outcome) -> None:
    network.clear_network_probe_cache()
    monkeypatch.setattr(network.sys, "platform", "linux")
    monkeypatch.setattr(network.shutil, "which", lambda name: "/usr/bin/unshare")
    probe = Mock()
    if isinstance(outcome, Exception):
        probe.side_effect = outcome
    else:
        probe.return_value = subprocess.CompletedProcess([], outcome)
    monkeypatch.setattr(network.subprocess, "run", probe)
    try:
        prefix = network.network_namespace_command()
        assert (prefix is not None) == (outcome == 0)
        assert network.network_namespace_command() == prefix
        probe.assert_called_once()
        argv = probe.call_args.args[0]
        assert Path(argv[0]).is_absolute()
        assert argv[1:] == [
            "--user",
            "--map-root-user",
            "--net",
            "--",
            sys.executable,
            "-c",
            "pass",
        ]
        assert probe.call_args.kwargs["timeout"] == 3
    finally:
        network.clear_network_probe_cache()


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_probe_without_support(monkeypatch, platform) -> None:
    network.clear_network_probe_cache()
    monkeypatch.setattr(network.sys, "platform", platform)
    monkeypatch.setattr(network.shutil, "which", lambda name: None)
    try:
        assert network.network_namespace_command() is None
    finally:
        network.clear_network_probe_cache()


def test_namespace_exec_keeps_pid(tmp_path, monkeypatch) -> None:
    """真实 unshare 不 fork，目标进程 PID 仍是 Popen 返回的 PID。"""
    require_namespace()
    original_popen = subprocess.Popen
    child_pids = []

    def capture_popen(*args, **kwargs):
        proc = original_popen(*args, **kwargs)
        child_pids.append(proc.pid)
        return proc

    monkeypatch.setattr(subprocess, "Popen", capture_popen)
    result = CommandRunner(tmp_path).run([sys.executable, "-c", "import os; print(os.getpid())"])
    assert result.network == NetworkIsolation.NAMESPACE
    assert result.returncode == 0, result.stderr
    assert int(result.stdout) == child_pids[0]


def test_timeout_kills_grandchild(tmp_path) -> None:
    """默认隔离下杀整个进程组，孙进程不能在超时后写文件。"""
    import time

    marker = tmp_path / "survived"
    grandchild = (
        "import pathlib, time; time.sleep(1.5); "
        f"pathlib.Path({str(marker)!r}).write_text('survived')"
    )
    script = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}]); "
        "print('ready', flush=True); time.sleep(30)"
    )
    result = CommandRunner(tmp_path, timeout_s=1).run([sys.executable, "-c", script])
    assert result.timed_out
    assert b"ready" in result.stdout
    time.sleep(1)
    assert not marker.exists()
