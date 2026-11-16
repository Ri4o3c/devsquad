"""Create a bounded source snapshot and mount it into a Bubblewrap sandbox."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from devsquad.sandbox.pathguard import SandboxError, is_secret_filename

_BWRAP = Path("/usr/bin/bwrap")
_WORKSPACE = Path("/workspace")
_RUNTIME = ("/usr/bin", "/usr/lib", "/usr/lib64", "/usr/share", "/bin", "/lib", "/lib64")
_EXCLUDED = frozenset({
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
})
_MAX_ENTRIES = 10_000
_MAX_BYTES = 64 * 1024 * 1024
_MAX_DEPTH = 64


def _bubblewrap(root: Path) -> Path:
    if sys.platform != "linux":
        raise SandboxError("命令需要支持用户命名空间的 Linux 和 Bubblewrap")
    binary = Path(os.environ.get("DEVSQUAD_BWRAP", str(_BWRAP)))
    if not binary.is_absolute():
        raise SandboxError("Bubblewrap 必须使用可信安装的绝对路径")
    try:
        binary = binary.resolve(strict=True)
        mode = binary.stat().st_mode
        if (
            binary.is_relative_to(root)
            or not stat.S_ISREG(mode)
            or mode & (stat.S_ISUID | stat.S_ISGID | stat.S_IWGRP | stat.S_IWOTH)
            or not os.access(binary, os.X_OK)
        ):
            raise SandboxError("命令需要从 worktree 外可信位置安装非 setuid 的 Bubblewrap")
    except (OSError, RuntimeError) as exc:
        raise SandboxError("找不到可用的 Bubblewrap，已拒绝启动命令") from exc
    return binary


def _copy_source(root: Path | int, destination: Path) -> None:
    # Open relative to directory descriptors, without following links even if an
    # entry is replaced after enumeration. Never open protected names for reading.
    dir_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    file_flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    entries = 0
    remaining = _MAX_BYTES

    def copy_dir(parent: int, target: Path, device: int, depth: int) -> None:
        nonlocal entries, remaining
        if depth > _MAX_DEPTH:
            raise SandboxError("工作区快照目录层级过深")
        with os.scandir(parent) as children:
            for entry in children:
                entries += 1
                if entries > _MAX_ENTRIES:
                    raise SandboxError("工作区快照文件数量超限")
                name = entry.name
                if name.casefold() in _EXCLUDED or is_secret_filename(name):
                    continue
                source = entry.stat(follow_symlinks=False)
                if source.st_dev != device:
                    continue
                is_dir = stat.S_ISDIR(source.st_mode)
                if not is_dir and (not stat.S_ISREG(source.st_mode) or source.st_nlink != 1):
                    continue
                handle = os.open(name, dir_flags if is_dir else file_flags, dir_fd=parent)
                try:
                    opened = os.fstat(handle)
                    if (
                        (opened.st_dev, opened.st_ino, stat.S_IFMT(opened.st_mode))
                        != (source.st_dev, source.st_ino, stat.S_IFMT(source.st_mode))
                        or (not is_dir and opened.st_nlink != 1)
                    ):
                        raise SandboxError("创建快照时工作区发生变化")
                    path = target / name
                    if is_dir:
                        path.mkdir()
                        copy_dir(handle, path, device, depth + 1)
                        continue
                    if opened.st_size > remaining:
                        raise SandboxError("工作区快照大小超限")
                    with path.open("xb") as output:
                        while chunk := os.read(handle, min(65536, remaining + 1)):
                            remaining -= len(chunk)
                            if remaining < 0:
                                raise SandboxError("工作区快照大小超限")
                            output.write(chunk)
                    path.chmod(0o600 | (opened.st_mode & 0o100))
                finally:
                    os.close(handle)

    directory = (
        os.open(".", dir_flags, dir_fd=root) if isinstance(root, int) else os.open(root, dir_flags)
    )
    try:
        copy_dir(directory, destination, os.fstat(directory).st_dev, 0)
    finally:
        os.close(directory)


@contextmanager
def source_snapshot(root: Path | int) -> Iterator[Path]:
    """Copy bounded source without following links or including protected files."""
    with TemporaryDirectory(prefix="devsquad-source-") as directory:
        snapshot = Path(directory) / "source"
        snapshot.mkdir()
        try:
            _copy_source(root, snapshot)
        except OSError as exc:
            raise SandboxError("无法安全创建工作区快照") from exc
        yield snapshot


@contextmanager
def isolated_command(root: Path, cwd: Path, argv: list[str]) -> Iterator[list[str]]:
    """Mount a read-only snapshot with an isolated network and private /tmp."""
    binary = _bubblewrap(root)
    relative_cwd = cwd.relative_to(root)
    with source_snapshot(root) as snapshot:
        if not (snapshot / relative_cwd).is_dir():
            raise SandboxError("cwd 不在隔离快照内")

        command = [
            str(binary),
            "--unshare-user", "--unshare-ipc", "--unshare-pid",
            "--unshare-net", "--unshare-uts", "--hostname", "devsquad",
            "--disable-userns", "--die-with-parent", "--cap-drop", "ALL",
            "--clearenv", "--setenv", "PATH", "/usr/bin:/bin",
            "--setenv", "HOME", "/tmp/home", "--setenv", "TMPDIR", "/tmp",
            "--setenv", "LANG", "C.UTF-8", "--setenv", "PYTHONDONTWRITEBYTECODE", "1",
            "--dev", "/dev", "--proc", "/proc",
            "--size", str(_MAX_BYTES), "--tmpfs", "/tmp", "--dir", "/tmp/home",
            "--ro-bind", str(snapshot), str(_WORKSPACE),
            "--chdir", str(_WORKSPACE / relative_cwd),
        ]
        for raw in _RUNTIME:
            path = Path(raw)
            if path.is_symlink():
                command.extend(("--symlink", os.readlink(path), raw))
            elif path.is_dir():
                command.extend(("--ro-bind", raw, raw))
        yield [*command, "--", *argv]
