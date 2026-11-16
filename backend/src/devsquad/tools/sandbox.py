"""循环的沙箱工具分发；调用方提供的回调不能替换命令隔离器。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from devsquad.sandbox.pathguard import PathGuard, SandboxError
from devsquad.sandbox.runner import CommandRunner
from devsquad.tools.permissions import COMMAND_TOOLS, NETWORK_TOOLS, READ_TOOLS, WRITE_TOOLS


def dispatch_tool(
    name: str, args: dict, tools: dict[str, Callable], root: str | Path,
):
    """复核文件参数，强制隔离命令的文件系统与网络。"""
    if name in NETWORK_TOOLS:
        raise SandboxError("网络工具被禁止")
    guarded = dict(args)
    if name in READ_TOOLS | WRITE_TOOLS | COMMAND_TOOLS:
        guard = PathGuard(Path(root))
        if name in COMMAND_TOOLS:
            runner = CommandRunner(
                guard.root, require_network_isolation=True, require_filesystem_isolation=True,
            )
            if name == "run_tests":
                guarded["args"] = guarded.pop("argv")
            return runner.run(**guarded)
        path = guarded.get("path", "." if name in {"list_dir", "search"} else None)
        if path is None:
            raise SandboxError("缺少工具路径")
        if name in WRITE_TOOLS:
            resolved = guard.check_tool_write(path)
        else:
            resolved = guard.check_read(path)
        guarded["path"] = str(resolved)
    return tools[name](**guarded)
