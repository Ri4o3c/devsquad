"""工具权限检查。

请使用 :class:`devsquad.sandbox.pathguard.PathGuard` 和
:func:`devsquad.sandbox.pathguard.is_secret_filename`，不要自己再写一套符号链接逻辑。
用字符串 ``startswith`` 判断路径，过不了符号链接和前缀测试。

决定只有三种：allow、ask-human、deny。未知工具一律拒绝（deny）。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from devsquad.sandbox.pathguard import PathGuard, SandboxError

READ_TOOLS = frozenset({"read_file", "list_dir", "grep", "search"})
WRITE_TOOLS = frozenset({"write_file", "edit_file"})
COMMAND_TOOLS = frozenset({"run_command", "run_tests"})
NETWORK_TOOLS = frozenset({"http_get", "http_post"})


class Decision(StrEnum):
    """主循环对一次工具调用该怎么处理。

    ``ASK_HUMAN`` 就是 "ask-human"。它的值是 ``ask_human``，
    这样存的时候不带连字符。
    """

    ALLOW = "allow"
    ASK_HUMAN = "ask_human"
    DENY = "deny"


class PermissionPolicy:
    """对 worktree 里的一次工具调用做 allow / ask-human / deny 决定。"""

    def __init__(self, worktree: Path, deny_tools: set[str] | None = None) -> None:
        self.worktree = Path(worktree)
        self.deny_tools = set(deny_tools or ())

    def check(self, tool_name: str, arguments: dict) -> Decision:
        """返回 ``tool_name`` 带着 ``arguments`` 调用时的决定。

        未知情况一律拒绝。

        * 工具名在 ``deny_tools`` 里 → ``DENY``，哪怕别的规则会允许。
        * ``http_get`` 或 ``http_post`` → ``DENY``。
        * 名字不在上面的读、写、命令集合里 → ``DENY``。
        * ``read_file``、``list_dir``、``grep``、``search``：
          - ``list_dir``、``search`` 没有 ``path``，表示 worktree 根目录 → ``ALLOW``。
          - 否则 ``arguments["path"]`` 必须通过 ``PathGuard.check_read``。
            成功 → ``ALLOW``。``PathEscapeError`` 或 ``SecretPathError``
            → ``DENY``。``read_file`` 或 ``grep`` 缺少 ``path`` →
            ``DENY``。
        * ``write_file``、``edit_file``：
          - 缺少 ``path`` → ``DENY``。
          - ``PathGuard.check_write`` 抛错 → ``DENY``（包括 worktree
            外面的路径，以及任何 ``.git`` 下面的路径）。
          - 请求的名字或解析后的名字是密钥（``is_secret_filename``）→
            ``DENY``。``check_write`` 本身不做密钥检查；这条规则由本策略
            负责，所以创建 ``.env`` 是拒绝，而不是询问。
          - 否则 → ``ASK_HUMAN``。写操作不要返回 ``ALLOW``。
        * ``run_command``、``run_tests``：
          - 没有 ``cwd``，或者 ``cwd`` 通过 ``check_read`` 并且是 worktree
            里的目录 → ``ASK_HUMAN``。
          - ``cwd`` 逃出 worktree、不是目录、或是密钥 → ``DENY``。
          - 本 PR 不要从 ``argv`` 里解析文件名。

        相对路径由 ``PathGuard`` 拼到 worktree 上。用 ``self.worktree`` 构造守卫。

        测试会创建真实的符号链接。用前缀判断会把 ``/tmp/wt-evil`` 当成
        ``/tmp/wt`` 里面，从而失败。

        """
        if not isinstance(tool_name, str) or not isinstance(arguments, dict):
            return Decision.DENY
        known = READ_TOOLS | WRITE_TOOLS | COMMAND_TOOLS
        if tool_name in self.deny_tools or tool_name not in known:
            return Decision.DENY
        try:
            guard = PathGuard(self.worktree)
            if tool_name in COMMAND_TOOLS:
                cwd = arguments.get("cwd")
                if cwd is None or guard.check_read(cwd).is_dir():
                    return Decision.ASK_HUMAN
                return Decision.DENY

            path = arguments.get("path", "." if tool_name in {"list_dir", "search"} else None)
            if not isinstance(path, (str, Path)) or not path:
                return Decision.DENY
            if tool_name in WRITE_TOOLS:
                guard.check_tool_write(path)
                return Decision.ASK_HUMAN
            guard.check_read(path)
            return Decision.ALLOW
        except (OSError, RuntimeError, SandboxError, TypeError, ValueError):
            return Decision.DENY
