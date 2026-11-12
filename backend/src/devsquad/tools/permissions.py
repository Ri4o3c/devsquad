"""工具权限检查。

状态：只有接口。项目作者需要实现 ``PermissionPolicy.check``。
测试：``backend/tests/test_permissions.py``
这样跑目前会失败的测试::

    pytest -m student --runxfail tests/test_permissions.py

请使用 :class:`devsquad.sandbox.pathguard.PathGuard` 和
:func:`devsquad.sandbox.pathguard.is_secret_filename`，不要自己再写一套符号链接逻辑。
用字符串 ``startswith`` 判断路径，过不了符号链接和前缀测试。

决定只有三种：allow、ask-human、deny。未知工具一律拒绝（deny）。
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

READ_TOOLS = frozenset({"read_file", "list_dir", "grep"})
WRITE_TOOLS = frozenset({"write_file", "edit_file"})
COMMAND_TOOLS = frozenset({"run_command"})
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

        TODO(student): 实现下面这张表。未知情况一律拒绝。

        * 工具名在 ``deny_tools`` 里 → ``DENY``，哪怕别的规则会允许。
        * ``http_get`` 或 ``http_post`` → ``DENY``。
        * 名字不在上面的读、写、命令集合里 → ``DENY``。
        * ``read_file``、``list_dir``、``grep``：
          - ``list_dir`` 没有 ``path``，表示 worktree 根目录 → ``ALLOW``。
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
        * ``run_command``：
          - 没有 ``cwd``，或者 ``cwd`` 通过 ``check_read`` 并且是 worktree
            里的目录 → ``ASK_HUMAN``。
          - ``cwd`` 逃出 worktree、不是目录、或是密钥 → ``DENY``。
          - 本 PR 不要从 ``argv`` 里解析文件名。

        相对路径由 ``PathGuard`` 拼到 worktree 上。用 ``self.worktree`` 构造守卫。

        测试会创建真实的符号链接。用前缀判断会把 ``/tmp/wt-evil`` 当成
        ``/tmp/wt`` 里面，从而失败。

        抛出：
            NotImplementedError：在这个函数体被替换之前，每次都会抛出。
        """
        raise NotImplementedError(
            "尚未实现 PermissionPolicy.check"
            "（allow / ask_human / deny）。"
            "请看本函数文档字符串和 tests/test_permissions.py。"
            "运行：pytest -m student --runxfail tests/test_permissions.py"
        )
