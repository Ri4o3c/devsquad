"""Resolve a path and decide whether it may be used inside one worktree.

The check is: after normalizing ``.`` and ``..`` and following symlinks, is
the real path still inside the worktree? A string prefix is the wrong test.
``/tmp/wt-evil`` starts with ``/tmp/wt``, and a symlink named ``notes.txt``
can point at a file outside the worktree. Both have to be rejected.

Writes whose path contains a ``.git`` component are rejected. In a linked
worktree ``.git`` is a file pointing back at the main repository's git
directory, so writing it changes the user's real history. On Windows the
git directory and the rest of the path are compared case-insensitively,
and ``resolve`` follows both symlinks and directory junctions.

Reads of secret-shaped names are rejected. The check looks at the path the
caller asked for and at the path it resolves to, so either a file named
``.env`` or a harmless name that links to ``id_rsa`` is denied.

This is not a kernel sandbox. It protects paths DevSquad checks before
using them. A child process can still ``open()`` an absolute path; that
gap belongs with the command runner's network TODO, not with a fake check
here. There is also a time-of-check/time-of-use window between ``resolve``
and the later ``open``.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path

# Matched against the lowercased file or directory name.
_SECRET_GLOBS = (
    ".env*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
)

# These match ``.env*`` but they are templates, not secrets.
_SECRET_ALLOW = frozenset({
    ".env.example",
    ".env.sample",
    ".env.template",
})


class SandboxError(Exception):
    """Base class for path-guard and command-runner failures."""


class PathEscapeError(SandboxError):
    """The resolved path is not inside the worktree."""


class GitWriteError(SandboxError):
    """A write targeted a ``.git`` path."""


class SecretPathError(SandboxError):
    """A read targeted a secret-shaped name."""


def _strip_extended_prefix(path: str) -> str:
    """Drop the ``\\\\?\\`` prefix Windows adds to canonical paths.

    ``GetFinalPathNameByHandle`` sometimes returns ``\\\\?\\C:\\...`` and
    sometimes a normal path. Mixing the two makes ``relative_to`` treat an
    inside path as an escape.
    """
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _for_compare(path: Path) -> Path:
    text = _strip_extended_prefix(os.fspath(path))
    if os.name == "nt":
        text = os.path.normcase(text)
    return Path(text)


def _is_inside(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        if os.name != "nt":
            return False
    try:
        _for_compare(path).relative_to(_for_compare(root))
    except ValueError:
        return False
    return True


def _relative_parts(root: Path, path: Path) -> tuple[str, ...] | None:
    """Parts of ``path`` under ``root``, or None when ``path`` is outside."""
    try:
        return path.relative_to(root).parts
    except ValueError:
        if os.name != "nt":
            return None
    try:
        return _for_compare(path).relative_to(_for_compare(root)).parts
    except ValueError:
        return None


def _is_git_component(part: str) -> bool:
    if os.name == "nt":
        return part.casefold() == ".git"
    return part == ".git"


def is_secret_filename(name: str) -> bool:
    """Return True when ``name`` looks like a secret file or directory.

    Comparison is case-insensitive. ``.env.example``, ``.env.sample``, and
    ``.env.template`` are allowed so a task can still read templates.
    """
    lowered = name.lower()
    if lowered in _SECRET_ALLOW:
        return False
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in _SECRET_GLOBS)


class PathGuard:
    """Path checks rooted at a single task worktree."""

    def __init__(self, worktree: Path) -> None:
        root = Path(worktree).expanduser()
        if not root.exists() or not root.is_dir():
            raise SandboxError(f"worktree does not exist or is not a directory: {root}")
        # Follow a symlinked worktree root once, up front, so later
        # comparisons use the real directory.
        self.root = root.resolve()

    def check_read(self, path: str | Path) -> Path:
        """Return the resolved path when it may be read.

        Containment is checked before the secret-name check, so a path that
        leaves the worktree raises ``PathEscapeError`` even if the final
        component looks like a secret.

        Raises:
            PathEscapeError: the resolved path leaves the worktree.
            SecretPathError: the requested or resolved name looks like a secret.
        """
        raw = Path(path)
        resolved = self._resolve_inside(self._candidate(raw))
        if self._touches_secret(raw, resolved):
            raise SecretPathError(f"reading secret file is denied: {path}")
        return resolved

    def check_write(self, path: str | Path) -> Path:
        """Return the resolved path when it may be written.

        A path that resolves outside the worktree raises ``PathEscapeError``
        even when a component is named ``.git``. Writes that stay inside but
        touch ``.git`` raise ``GitWriteError``. Reading ``.git`` is allowed;
        use ``check_read`` for that.

        Secret filenames are not blocked on write. The permission policy is
        the layer that will refuse to create them. This function only
        enforces containment and the git-metadata rule from the phase-1
        baseline.
        """
        raw = Path(path)
        resolved = self._resolve_inside(self._candidate(raw))
        if self._touches_git(raw, resolved):
            raise GitWriteError(f"writing inside .git is denied: {path}")
        return resolved

    def _candidate(self, raw: Path) -> Path:
        if raw.is_absolute():
            return raw
        return self.root / raw

    def _resolve_inside(self, candidate: Path) -> Path:
        # strict=False so a file that does not exist yet still resolves.
        # Existing parents are symlink-expanded by Path.resolve.
        resolved = candidate.resolve(strict=False)
        if not _is_inside(self.root, resolved):
            raise PathEscapeError(f"path escapes the worktree: {candidate} -> {resolved}")
        return resolved

    def _user_parts(self, raw: Path) -> tuple[str, ...]:
        """Lexical components relative to the worktree, without following links.

        The symlink's own name has to be inspected. After ``resolve``, a
        link named ``.env`` that points at ``readme.txt`` no longer contains
        the secret name.
        """
        if raw.is_absolute():
            parts = _relative_parts(self.root, raw)
            return () if parts is None else parts
        return raw.parts

    def _touches_secret(self, raw: Path, resolved: Path) -> bool:
        names = list(_relative_parts(self.root, resolved) or ())
        names.extend(self._user_parts(raw))
        return any(is_secret_filename(part) for part in names if part not in {".", ".."})

    def _touches_git(self, raw: Path, resolved: Path) -> bool:
        resolved_parts = _relative_parts(self.root, resolved) or ()
        if any(_is_git_component(part) for part in resolved_parts):
            return True
        return any(_is_git_component(part) for part in self._user_parts(raw))
