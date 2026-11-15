"""Bounded local file tools for one interactive run."""

from __future__ import annotations

import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from devsquad.sandbox.pathguard import PathGuard, SandboxError, is_secret_filename

_MAX_FILE = 64 * 1024


def _protected(name: str) -> bool:
    return name.casefold() == ".git" or is_secret_filename(name)


@dataclass(frozen=True)
class FileChange:
    path: Path
    before: tuple[bytes, int, int] | None
    after: bytes


class WorktreeFiles:
    def __init__(self, root: Path) -> None:
        self.guard = PathGuard(root)
        self._dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        self._root = os.open(self.guard.root, self._dir_flags)
        self.approved: FileChange | None = None

    def close(self) -> None:
        os.close(self._root)

    def _relative(self, path: Path) -> Path:
        try:
            relative = path.relative_to(self.guard.root)
        except ValueError as exc:
            raise SandboxError("Outside the worktree") from exc
        if any(part == ".." or _protected(part) for part in relative.parts):
            raise SandboxError("Protected path")
        return relative

    @contextmanager
    def _directory(self, relative: Path):
        fd = os.dup(self._root)
        try:
            for part in relative.parts:
                child = os.open(part, self._dir_flags, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        finally:
            os.close(fd)

    def _read_at(self, directory: int, name: str) -> tuple[bytes, int, int]:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        fd = os.open(name, flags, dir_fd=directory)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise SandboxError("Only regular, single-link files are allowed")
            data = stream.read(_MAX_FILE + 1)
        if len(data) > _MAX_FILE or b"\0" in data:
            raise SandboxError("File is too large or binary")
        data.decode("utf-8")
        return data, info.st_mode, info.st_ino

    def list_dir(self, path: str) -> str:
        with self._directory(self._relative(Path(path))) as fd, os.scandir(fd) as entries:
            names = []
            for entry in entries:
                if _protected(entry.name) or entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    names.append(entry.name + "/")
                elif entry.is_file(follow_symlinks=False):
                    names.append(entry.name)
                if len(names) >= 100:
                    names.append("[listing limited to 100 entries]")
                    break
        return "\n".join(sorted(names))

    def read_file(self, path: str) -> str:
        relative = self._relative(Path(path))
        with self._directory(relative.parent) as directory:
            data, _, _ = self._read_at(directory, relative.name)
        return data.decode("utf-8")

    def prepare(self, name: str, args: dict) -> FileChange:
        path = self.guard.check_tool_write(args["path"])
        relative = self._relative(path)
        before = None
        with self._directory(relative.parent) as directory:
            if name == "edit_file":
                before = self._read_at(directory, relative.name)
                old = args["old_text"].encode()
                if not old or before[0].count(old) != 1:
                    raise SandboxError("The edit must match exactly once")
                after = before[0].replace(old, args["new_text"].encode(), 1)
            else:
                try:
                    os.stat(relative.name, dir_fd=directory, follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise SandboxError("write_file can only create a new file")
                after = args["text"].encode()
        if len(after) > _MAX_FILE:
            raise SandboxError("Edited file exceeds the size limit")
        return FileChange(path, before, after)

    def _apply(self, path: str, *, existing: bool) -> str:
        change, self.approved = self.approved, None
        if (
            change is None or change.path != Path(path)
            or (change.before is not None) != existing
        ):
            raise SandboxError("The target differs from the approved edit")
        relative = self._relative(change.path)
        temp = ".devsquad-" + uuid4().hex
        with self._directory(relative.parent) as directory:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(change.after)
                    mode = stat.S_IMODE(change.before[1]) & 0o777 if change.before else 0o644
                    os.fchmod(stream.fileno(), mode)
                    stream.flush()
                    os.fsync(stream.fileno())
                if existing:
                    if self._read_at(directory, relative.name) != change.before:
                        raise SandboxError("File changed since approval")
                    os.replace(temp, relative.name, src_dir_fd=directory, dst_dir_fd=directory)
                else:
                    os.link(temp, relative.name, src_dir_fd=directory, dst_dir_fd=directory)
            finally:
                try:
                    os.unlink(temp, dir_fd=directory)
                except FileNotFoundError:
                    pass
        return "Updated " + relative.as_posix()

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        return self._apply(path, existing=True)

    def write_file(self, path: str, text: str) -> str:
        return self._apply(path, existing=False)
