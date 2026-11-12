"""Tool permission check.

STATUS: interface only. The project owner implements ``PermissionPolicy.check``.
Tests: ``backend/tests/test_permissions.py``
Run the failing tests with::

    pytest -m student --runxfail tests/test_permissions.py

Use :class:`devsquad.sandbox.pathguard.PathGuard` and
:func:`devsquad.sandbox.pathguard.is_secret_filename` instead of
reimplementing symlink logic. A string ``startswith`` check will fail the
symlink and prefix tests.

The decision is one of allow, ask-human, and deny. Unknown tools fail
closed (deny).
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

READ_TOOLS = frozenset({"read_file", "list_dir", "grep"})
WRITE_TOOLS = frozenset({"write_file", "edit_file"})
COMMAND_TOOLS = frozenset({"run_command"})
NETWORK_TOOLS = frozenset({"http_get", "http_post"})


class Decision(StrEnum):
    """What the loop should do with one tool call.

    ``ASK_HUMAN`` is the "ask-human" outcome. Its value is ``ask_human``
    so it can be stored without a hyphen.
    """

    ALLOW = "allow"
    ASK_HUMAN = "ask_human"
    DENY = "deny"


class PermissionPolicy:
    """Decide allow / ask-human / deny for one tool call inside a worktree."""

    def __init__(self, worktree: Path, deny_tools: set[str] | None = None) -> None:
        self.worktree = Path(worktree)
        self.deny_tools = set(deny_tools or ())

    def check(self, tool_name: str, arguments: dict) -> Decision:
        """Return the decision for ``tool_name`` called with ``arguments``.

        TODO(student): implement the table below. Fail closed.

        * The tool name is in ``deny_tools`` → ``DENY``, even if another
          rule would allow it.
        * ``http_get`` or ``http_post`` → ``DENY``.
        * Any name not in the read, write, or command sets above → ``DENY``.
        * ``read_file``, ``list_dir``, ``grep``:
          - ``list_dir`` with no ``path`` means the worktree root → ``ALLOW``.
          - otherwise ``arguments["path"]`` must pass ``PathGuard.check_read``.
            Success → ``ALLOW``. ``PathEscapeError`` or ``SecretPathError``
            → ``DENY``. A missing ``path`` on ``read_file`` or ``grep`` →
            ``DENY``.
        * ``write_file``, ``edit_file``:
          - missing ``path`` → ``DENY``.
          - ``PathGuard.check_write`` raises → ``DENY`` (this covers paths
            outside the worktree and anything under ``.git``).
          - the requested name or the resolved name is secret
            (``is_secret_filename``) → ``DENY``. ``check_write`` itself does
            not apply the secret rule; this policy does, so creating
            ``.env`` is denied rather than asked.
          - otherwise → ``ASK_HUMAN``. Do not return ``ALLOW`` for a write.
        * ``run_command``:
          - no ``cwd``, or ``cwd`` passes ``check_read`` and is a directory
            inside the worktree → ``ASK_HUMAN``.
          - ``cwd`` escapes the worktree, is not a directory, or is secret →
            ``DENY``.
          - do not parse ``argv`` for filenames in this PR.

        Relative paths are joined to the worktree by ``PathGuard``. Build
        the guard from ``self.worktree``.

        The tests create real symlinks. A prefix check will mark
        ``/tmp/wt-evil`` as inside ``/tmp/wt`` and fail.

        Raises:
            NotImplementedError: always, until this body is replaced.
        """
        raise NotImplementedError(
            "student TODO: implement PermissionPolicy.check "
            "(allow / ask_human / deny). "
            "See this docstring and tests/test_permissions.py. "
            "Run: pytest -m student --runxfail tests/test_permissions.py"
        )
