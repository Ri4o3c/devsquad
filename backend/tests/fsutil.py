"""Filesystem helpers for tests."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

# Windows ERROR_PRIVILEGE_NOT_HELD. Creating a symlink needs this right
# unless Developer Mode is on. Directory junctions do not.
_WINERROR_PRIVILEGE_NOT_HELD = 1314


def symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool = False) -> None:
    """Create ``link`` pointing at ``target``, or skip if the OS refuses.

    A blanket ``skipif(win32)`` would hide a path-guard bug on machines
    that can create symlinks. Skip only when the call itself is rejected.
    """
    try:
        link.symlink_to(target, target_is_directory=target_is_directory)
    except FileExistsError:
        raise
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        unsupported = {
            errno.EPERM,
            errno.EACCES,
            getattr(errno, "ENOTSUP", None),
            getattr(errno, "EOPNOTSUPP", None),
        }
        unsupported.discard(None)
        refused = winerror == _WINERROR_PRIVILEGE_NOT_HELD or exc.errno in unsupported
        if not refused:
            raise
        pytest.skip(
            "当前系统拒绝创建符号链接"
            f"（{exc.__class__.__name__}: {exc}）。"
            "在 Windows 上需要开发者模式或 SeCreateSymbolicLinkPrivilege。"
        )
