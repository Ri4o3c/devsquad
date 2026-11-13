"""Path guard tests.

These cover traversal out of the worktree, symlink escape, the ``/tmp/wt``
vs ``/tmp/wt-evil`` prefix trap, and writes into ``.git``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from devsquad.sandbox.pathguard import (
    GitWriteError,
    PathEscapeError,
    PathGuard,
    SecretPathError,
    is_secret_filename,
)
from devsquad.tasks import create_task
from tests.fsutil import symlink_or_skip
from tests.gitutil import git


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    root = tmp_path / "wt"
    root.mkdir()
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (root / ".git").write_text("gitdir: /somewhere/real.git\n", encoding="utf-8")
    return root


def test_reads_a_normal_file_inside_the_worktree(worktree: Path) -> None:
    guard = PathGuard(worktree)
    resolved = guard.check_read("src/app.py")
    assert resolved == (worktree / "src" / "app.py").resolve()
    assert resolved.read_text(encoding="utf-8").startswith("print")


def test_write_of_a_new_file_stays_inside(worktree: Path) -> None:
    guard = PathGuard(worktree)
    resolved = guard.check_write("src/new_file.py")
    assert resolved.is_relative_to(worktree.resolve())
    assert resolved.name == "new_file.py"


def test_can_read_git_metadata_but_not_write_it(worktree: Path) -> None:
    guard = PathGuard(worktree)
    resolved = guard.check_read(".git")
    assert "gitdir:" in resolved.read_text(encoding="utf-8")
    with pytest.raises(GitWriteError):
        guard.check_write(".git")


@pytest.mark.parametrize(
    "relative",
    [".git", ".git/config", "nested/.git/HEAD", "src/.git"],
)
def test_rejects_write_into_git(worktree: Path, relative: str) -> None:
    guard = PathGuard(worktree)
    with pytest.raises(GitWriteError):
        guard.check_write(relative)


def test_rejects_write_through_a_symlink_to_git(worktree: Path) -> None:
    symlink_or_skip(worktree / "gitlink", worktree / ".git")
    guard = PathGuard(worktree)
    with pytest.raises(GitWriteError):
        guard.check_write("gitlink")


def test_rejects_relative_traversal_outside_worktree(tmp_path: Path, worktree: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("nope\n", encoding="utf-8")
    guard = PathGuard(worktree)
    with pytest.raises(PathEscapeError):
        guard.check_read("../outside.txt")
    with pytest.raises(PathEscapeError):
        guard.check_write("../outside.txt")
    with pytest.raises(PathEscapeError):
        guard.check_read(worktree / "src" / ".." / ".." / "outside.txt")


def test_rejects_absolute_path_outside_worktree(tmp_path: Path, worktree: Path) -> None:
    outside = tmp_path / "absolute.txt"
    outside.write_text("nope\n", encoding="utf-8")
    guard = PathGuard(worktree)
    with pytest.raises(PathEscapeError):
        guard.check_read(outside)
    with pytest.raises(PathEscapeError):
        guard.check_write(outside)


def test_rejects_symlink_that_points_outside_worktree(tmp_path: Path, worktree: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("nope\n", encoding="utf-8")
    symlink_or_skip(worktree / "escape", secret)
    symlink_or_skip(worktree / "outdir", outside, target_is_directory=True)
    guard = PathGuard(worktree)

    with pytest.raises(PathEscapeError):
        guard.check_read("escape")
    with pytest.raises(PathEscapeError):
        guard.check_read("outdir/secret.txt")
    with pytest.raises(PathEscapeError):
        guard.check_write("outdir/pwned.txt")
    with pytest.raises(PathEscapeError):
        guard.check_write(worktree / "escape")


def test_rejects_junction_that_points_outside_on_windows(tmp_path: Path, worktree: Path) -> None:
    if os.name != "nt":
        pytest.skip("目录联接（junction）是 Windows 的重解析点，其他系统没有")
    outside = tmp_path / "junction-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("nope\n", encoding="utf-8")
    link = worktree / "jdir"
    completed = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or not link.exists():
        detail = (completed.stderr or completed.stdout).strip()
        pytest.skip(f"当前系统拒绝创建目录联接：{detail}")
    guard = PathGuard(worktree)
    with pytest.raises(PathEscapeError):
        guard.check_read(link / "secret.txt")
    with pytest.raises(PathEscapeError):
        guard.check_write(link / "pwned.txt")


def test_windows_git_metadata_is_case_insensitive(worktree: Path) -> None:
    if os.name != "nt":
        pytest.skip("只有 Windows 把 .GIT 和 .git 当成同一个目录")
    guard = PathGuard(worktree)
    with pytest.raises(GitWriteError):
        guard.check_write(".GIT/config")
    with pytest.raises(GitWriteError):
        guard.check_write("src/.Git/HEAD")


def test_rejects_sibling_directory_with_shared_prefix(tmp_path: Path) -> None:
    root = tmp_path / "wt"
    root.mkdir()
    evil = Path(str(root) + "-evil")
    evil.mkdir()
    target = evil / "x.txt"
    target.write_text("nope\n", encoding="utf-8")
    # This is the bug a startswith check would miss.
    assert str(target).startswith(str(root))
    guard = PathGuard(root)
    with pytest.raises(PathEscapeError):
        guard.check_read(target)
    with pytest.raises(PathEscapeError):
        guard.check_write(evil / "new.txt")


def test_allows_symlink_whose_target_stays_inside(worktree: Path) -> None:
    target = worktree / "src" / "app.py"
    link = worktree / "alias.py"
    symlink_or_skip(link, target)
    guard = PathGuard(worktree)
    assert guard.check_read("alias.py") == target.resolve()
    assert guard.check_write("alias.py") == target.resolve()


@pytest.mark.parametrize(
    "name",
    [".env", ".env.local", ".ENV", "id_rsa", "id_rsa.pub", "id_ed25519", "server.key", "cert.pem"],
)
def test_denies_reading_secret_filenames(worktree: Path, name: str) -> None:
    path = worktree / "src" / name
    path.write_text("secret\n", encoding="utf-8")
    guard = PathGuard(worktree)
    with pytest.raises(SecretPathError):
        guard.check_read(path)
    with pytest.raises(SecretPathError):
        guard.check_read(f"src/{name}")


def test_denies_reading_a_harmless_name_that_links_to_a_secret(worktree: Path) -> None:
    secret = worktree / "id_rsa"
    secret.write_text("key\n", encoding="utf-8")
    symlink_or_skip(worktree / "notes.txt", secret)
    guard = PathGuard(worktree)
    with pytest.raises(SecretPathError):
        guard.check_read("notes.txt")


def test_denies_reading_a_secret_name_that_links_to_a_normal_file(worktree: Path) -> None:
    symlink_or_skip(worktree / ".env", worktree / "src" / "app.py")
    guard = PathGuard(worktree)
    with pytest.raises(SecretPathError):
        guard.check_read(".env")


def test_allows_env_example_template(worktree: Path) -> None:
    template = worktree / ".env.example"
    template.write_text("TOKEN=\n", encoding="utf-8")
    guard = PathGuard(worktree)
    assert guard.check_read(".env.example") == template.resolve()
    assert guard.check_read(".env.example").read_text(encoding="utf-8") == "TOKEN=\n"


@pytest.mark.parametrize(
    ("name", "secret"),
    [
        (".env", True),
        (".env.local", True),
        (".env.example", False),
        (".env.sample", False),
        (".env.template", False),
        ("id_rsa", True),
        ("id_ed25519.pub", True),
        ("cert.pem", True),
        ("server.key", True),
        ("keys.p12", True),
        ("app.py", False),
        ("monkey", False),
        ("notes.txt", False),
    ],
)
def test_secret_filename_table(name: str, secret: bool) -> None:
    assert is_secret_filename(name) is secret


def test_write_to_real_linked_worktree_git_file(git_repo, tmp_path: Path) -> None:
    task = create_task(
        repo=git_repo,
        title="guard",
        workspaces_dir=tmp_path / "ws",
        db_path=tmp_path / "state.db",
    )
    worktree = Path(task.worktree_path)
    assert (worktree / ".git").is_file()
    assert git(git_repo, "branch", "--show-current").strip() == "main"
    guard = PathGuard(worktree)
    with pytest.raises(GitWriteError):
        guard.check_write(".git")
    with pytest.raises(GitWriteError):
        guard.check_write(".git/config")
    # Reading the pointer is allowed; it is not a secret name.
    assert "gitdir:" in guard.check_read(".git").read_text(encoding="utf-8")
