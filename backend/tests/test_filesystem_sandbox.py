"""Exercise the real mount namespace and the fail-closed tool entry point."""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

from devsquad.sandbox.network import NetworkIsolation
from devsquad.sandbox.pathguard import SandboxError
from devsquad.tools.sandbox import dispatch_tool


def test_missing_launcher_rejects_command(tmp_path, monkeypatch):
    monkeypatch.setenv("DEVSQUAD_BWRAP", str(tmp_path / "missing-bwrap"))
    with pytest.raises(SandboxError):
        dispatch_tool("run_command", {"args": ["python3", "-c", "pass"]}, {}, tmp_path)


def test_sandbox_confines_command_filesystem_and_network(tmp_path, monkeypatch):
    binary = Path(os.environ.get("DEVSQUAD_BWRAP", "/usr/bin/bwrap"))
    if sys.platform != "linux" or not binary.is_file():
        pytest.skip("requires Linux and an installed Bubblewrap launcher")

    workspace = tmp_path / "worktree"
    workspace.mkdir()
    source = workspace / "ordinary.txt"
    source.write_text("original", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside fixture", encoding="utf-8")
    (workspace / ".env").write_text("credential fixture", encoding="utf-8")
    (workspace / ".env.example").write_text("sample", encoding="utf-8")
    (workspace / ".git").write_text("git metadata fixture", encoding="utf-8")
    (workspace / "escape").symlink_to(outside)
    os.link(outside, workspace / "hardlink")
    monkeypatch.setenv("OPENAI_API_KEY", "credential-fixture-only")

    script = """
import os
import socket
import sys
from pathlib import Path

assert os.getcwd() == "/workspace"
assert os.environ.get("OPENAI_API_KEY") is None
assert Path("ordinary.txt").read_text() == "original"
assert Path(".env.example").read_text() == "sample"

for name in (sys.argv[1], ".env", ".git", "escape", "hardlink"):
    try:
        Path(name).read_text()
    except OSError:
        pass
    else:
        raise AssertionError(f"protected fixture was readable: {name}")

for name in (sys.argv[1], "ordinary.txt", ".git"):
    try:
        Path(name).write_text("corrupted")
    except OSError:
        pass
    else:
        raise AssertionError(f"protected fixture was writable: {name}")

Path("/tmp/sandbox-probe.txt").write_text("private")
with socket.socket() as client:
    client.settimeout(1)
    try:
        client.connect(("127.0.0.1", int(sys.argv[2])))
    except OSError:
        pass
    else:
        raise AssertionError("host loopback was reachable")
print("isolated")
"""
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        result = dispatch_tool(
            "run_command", {"args": ["python3", "-c", script, str(outside), str(port)]}, {},
            workspace,
        )

    assert result.returncode == 0, result.stderr.decode(errors="replace")
    assert result.network == NetworkIsolation.NAMESPACE
    assert result.stdout.strip() == b"isolated"
    assert source.read_text(encoding="utf-8") == "original"
    assert outside.read_text(encoding="utf-8") == "outside fixture"


def test_sandbox_cleans_up_detached_descendants(tmp_path):
    binary = Path(os.environ.get("DEVSQUAD_BWRAP", "/usr/bin/bwrap"))
    if sys.platform != "linux" or not binary.is_file():
        pytest.skip("requires Linux and an installed Bubblewrap launcher")
    script = """
import os
import time

if os.fork() == 0:
    os.setsid()
    time.sleep(15)
"""
    result = dispatch_tool(
        "run_command", {"args": ["python3", "-c", script], "timeout_s": 5}, {}, tmp_path,
    )
    assert result.returncode == 0
    assert not result.timed_out
    assert result.duration_s < 5
