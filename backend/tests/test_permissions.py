"""Spec for ``PermissionPolicy.check``.

Fails until the owner implements the decision table. ``xfail(strict=False)``
keeps CI green.

    pytest -m student --runxfail tests/test_permissions.py
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devsquad.tools.permissions import Decision, PermissionPolicy

pytestmark = [
    pytest.mark.student,
    pytest.mark.xfail(
        reason="student TODO: implement PermissionPolicy.check",
        strict=False,
    ),
]


def _policy(root: Path, deny_tools: set[str] | None = None) -> PermissionPolicy:
    return PermissionPolicy(root, deny_tools=deny_tools)


def test_read_inside_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    assert _policy(tmp_path).check("read_file", {"path": "a.py"}) == Decision.ALLOW


def test_grep_inside_is_allowed(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    assert _policy(tmp_path).check("grep", {"path": "a.py"}) == Decision.ALLOW


def test_list_dir_without_path_is_the_worktree_root(tmp_path: Path) -> None:
    assert _policy(tmp_path).check("list_dir", {}) == Decision.ALLOW


def test_list_dir_outside_is_denied(tmp_path: Path) -> None:
    outside = tmp_path.parent / "elsewhere"
    decision = _policy(tmp_path).check("list_dir", {"path": str(outside)})
    assert decision == Decision.DENY


def test_write_and_edit_inside_ask_the_human(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    assert policy.check("write_file", {"path": "a.py"}) == Decision.ASK_HUMAN
    assert policy.check("edit_file", {"path": "a.py"}) == Decision.ASK_HUMAN


def test_run_command_asks_the_human(tmp_path: Path) -> None:
    decision = _policy(tmp_path).check("run_command", {"argv": ["pytest"]})
    assert decision == Decision.ASK_HUMAN


def test_read_traversal_outside_the_worktree_is_denied(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    root.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("nope\n", encoding="utf-8")
    assert _policy(root).check("read_file", {"path": "../secret.txt"}) == Decision.DENY
    assert _policy(root).check("read_file", {"path": str(outside)}) == Decision.DENY


def test_read_symlink_escape_is_denied(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope\n", encoding="utf-8")
    (root / "escape").symlink_to(outside / "secret.txt")
    assert _policy(root).check("read_file", {"path": "escape"}) == Decision.DENY


def test_write_outside_is_denied_not_ask(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    root.mkdir()
    assert _policy(root).check("write_file", {"path": "../x"}) == Decision.DENY


def test_write_into_git_is_denied(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    root.mkdir()
    (root / ".git").write_text("gitdir: /somewhere\n", encoding="utf-8")
    policy = _policy(root)
    assert policy.check("write_file", {"path": ".git"}) == Decision.DENY
    assert policy.check("edit_file", {"path": ".git/config"}) == Decision.DENY
    assert policy.check("write_file", {"path": "nested/.git/HEAD"}) == Decision.DENY


def test_secret_read_and_write_are_denied(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    root.mkdir()
    (root / ".env").write_text("TOKEN=x\n", encoding="utf-8")
    policy = _policy(root)
    assert policy.check("read_file", {"path": ".env"}) == Decision.DENY
    assert policy.check("write_file", {"path": "id_rsa"}) == Decision.DENY
    assert policy.check("read_file", {"path": ".env.example"}) == Decision.ALLOW


def test_run_command_cwd_escape_is_denied(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    decision = _policy(root).check("run_command", {"cwd": str(outside)})
    assert decision == Decision.DENY


def test_network_and_unknown_tools_are_denied(tmp_path: Path) -> None:
    policy = _policy(tmp_path)
    assert policy.check("http_get", {"url": "https://example.com"}) == Decision.DENY
    assert policy.check("http_post", {"url": "https://example.com"}) == Decision.DENY
    assert policy.check("format_disk", {}) == Decision.DENY


def test_deny_list_overrides_an_otherwise_safe_read(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8")
    policy = _policy(tmp_path, deny_tools={"read_file"})
    assert policy.check("read_file", {"path": "a.py"}) == Decision.DENY
