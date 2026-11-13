"""循环的沙箱工具分发；调用方提供的回调不能替换命令隔离器。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from devsquad.sandbox.pathguard import PathGuard, SandboxError, is_secret_filename
from devsquad.sandbox.runner import CommandRunner
from devsquad.tools.permissions import COMMAND_TOOLS, NETWORK_TOOLS, READ_TOOLS, WRITE_TOOLS


def dispatch_tool(
    name: str, args: dict, tools: dict[str, Callable], root: str | Path,
):
    """对文件参数重新守卫；命令强制禁网，不支持网络命名空间时拒绝启动。"""
    if name in NETWORK_TOOLS:
        raise SandboxError("网络工具被禁止")
    guarded = dict(args)
    if name in READ_TOOLS | WRITE_TOOLS | COMMAND_TOOLS:
        guard = PathGuard(Path(root))
        if name in COMMAND_TOOLS:
            # 不调用 tools 中的 run_command 回调，防止绕开网络隔离。
            runner = CommandRunner(guard.root, require_network_isolation=True)
            return runner.run(**guarded)
        path = guarded.get("path", "." if name == "list_dir" else None)
        if path is None:
            raise SandboxError("缺少工具路径")
        if name in WRITE_TOOLS:
            resolved = guard.check_write(path)
            # 写操作也禁止密钥路径；同时检查请求路径和解析路径的目录名。
            parts = Path(path).parts + resolved.relative_to(guard.root).parts
            if any(is_secret_filename(part) for part in parts):
                raise SandboxError("禁止写入密钥路径")
        else:
            resolved = guard.check_read(path)
        guarded["path"] = str(resolved)
    return tools[name](**guarded)
