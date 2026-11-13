"""代码检索的召回、切分和安全边界。"""

import shutil
import subprocess

import pytest

from devsquad.retrieval import SearchHit, search_code
from devsquad.retrieval.code_search import GitIgnore


@pytest.mark.parametrize(
    ("rules", "path", "directory", "expected"),
    [
        ("# 注释\n\n*.log", "sub/a.log", False, True),
        ("*.log\n!keep.log", "sub/keep.log", False, False),
        ("cache/", "cache", True, True),
        ("cache/", "cache", False, False),
        ("/root.txt", "sub/root.txt", False, False),
        ("/root.txt", "root.txt", False, True),
        ("src/*.py", "src/a.py", False, True),
        ("src/*.py", "src/deep/a.py", False, False),
        ("a?.py", "ab.py", False, True),
        ("a?.py", "abc.py", False, False),
        ("**/draft*.md", "a/b/draft1.md", False, True),
        ("**/draft*.md", "draft.md", False, True),
        ("src/**/a.py", "src/a.py", False, True),
        ("src/**/a.py", "src/x/y/a.py", False, True),
    ],
)
def test_gitignore(rules, path, directory, expected):
    assert GitIgnore(rules).ignored(path, directory) is expected


def test_gitignore_base():
    matcher = GitIgnore("/a.py\n!keep.py", "sub")
    assert matcher.ignored("sub/a.py", False)
    assert not matcher.ignored("a.py", False)
    assert not matcher.ignored("sub/keep.py", False, True)


@pytest.fixture
def repo(tmp_path):
    files = {
        ".gitignore": "*.log\n!keep.log\nbuild/\n**/draft*.md\n",
        "service.py": "@decorate\nclass UserService:\n    def parse_HTTPResponse(self):\n"
        '        return "user service"\n',
        "other.py": 'def parse_value():\n    return "service"\n',
        "README.md": "service reference\n",
        "crlf.txt": "header\r\nservice\r\ntail\r\n",
        "sub/.gitignore": "*.txt\n!keep.txt\n/nested/*.py\n",
        "sub/keep.txt": "service\n",
        "sub/drop.txt": "service\n",
        "sub/nested/drop.py": "service\n",
        "sub/deep.py": "def service_helper():\n    pass\n",
        "sub/draft-note.md": "service\n",
        "keep.log": "service\n",
        "drop.log": "service\n",
        "build/keep.txt": "service\n",
        ".git/config": "service\n",
        ".env": "service\n",
        "key.pem": "service\n",
        "id_rsa": "service\n",
        ".npmrc": "service\n",
        ".pypirc": "service\n",
        ".env.private/file.py": "service\n",
        ".hidden.txt": "service\n",
        "huge.txt": "service " * 1000,
    }
    for name, content in files.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.encode())
    (tmp_path / "binary.bin").write_bytes(b"service\0binary")
    return tmp_path


def require_rg():
    if shutil.which("rg") is None:
        pytest.skip("本机没有安装 ripgrep（rg），只测 Python 后备")


@pytest.mark.parametrize("backend", ["python", "rg"])
def test_search(repo, backend):
    if backend == "rg":
        require_rg()
    hits = search_code(repo, "user service", backend=backend, top_k=100, max_file_bytes=1000)
    assert hits[0].path == "service.py"
    assert all(isinstance(hit, SearchHit) and hit.score > 0 for hit in hits)
    paths = {hit.path for hit in hits}
    assert paths == {
        "service.py",
        "other.py",
        "README.md",
        "crlf.txt",
        "sub/keep.txt",
        "sub/deep.py",
        "keep.log",
        ".hidden.txt",
    }
    assert all("\\" not in hit.path and "\r" not in hit.snippet for hit in hits)
    service = next(hit for hit in hits if hit.path == "service.py")
    assert (service.start_line, service.end_line) == (1, 4)
    crlf = next(hit for hit in hits if hit.path == "crlf.txt")
    assert (crlf.start_line, crlf.end_line) == (1, 3)
    assert crlf.snippet == "header\nservice\ntail\n"
    assert hits == search_code(
        repo, "user service", backend=backend, top_k=100, max_file_bytes=1000
    )


def test_backend_parity(repo):
    require_rg()
    args = dict(top_k=100, max_file_bytes=1000)
    assert search_code(repo, "user service", backend="rg", **args) == search_code(
        repo, "user service", backend="python", **args
    )


def test_auto_without_rg(repo, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert search_code(repo, "service") == search_code(repo, "service", backend="python")
    with pytest.raises(RuntimeError, match="ripgrep"):
        search_code(repo, "service", backend="rg")


def test_validation(repo):
    with pytest.raises(ValueError, match="top_k"):
        search_code(repo, "", top_k=0)
    with pytest.raises(ValueError, match="backend"):
        search_code(repo, "service", backend="invalid")
    for query in ["", "中文", "a !"]:
        assert search_code(repo, query, backend="python") == []
    assert search_code(repo, "中文", keywords=["UserService"], backend="python")
    assert search_code(repo, "", keywords=[], backend="python") == []


def test_identifiers_and_chunks(tmp_path):
    (tmp_path / "sample.py").write_text(
        "import os\n\n@decorate\nclass UserService:\n    pass\n\n"
        'VALUE = "parse"\n\nasync def parse_HTTPResponse():\n    return 1\n',
        encoding="utf-8",
    )
    hits = search_code(tmp_path, "user service", backend="python")
    assert hits[0].start_line == 3
    assert hits[0].end_line == 5
    assert hits[0].snippet.startswith("@decorate")
    hits = search_code(tmp_path, "parse http response", backend="python")
    assert hits[0].start_line == 9
    assert hits[0].end_line == 10


def test_windows_and_syntax_fallback(tmp_path):
    (tmp_path / "broken.py").write_bytes(b"bad syntax!\rservice\rtail")
    hit = search_code(tmp_path, "service", backend="python")[0]
    assert (hit.start_line, hit.end_line) == (1, 3)
    assert hit.snippet == "bad syntax!\nservice\ntail"
    (tmp_path / "long.txt").write_text("service\n" * 121, encoding="utf-8")
    hits = search_code(tmp_path, "service", backend="python", top_k=100)
    assert {(h.start_line, h.end_line) for h in hits if h.path == "long.txt"} == {
        (1, 60),
        (51, 110),
        (101, 121),
    }


def test_rg_errors(repo, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/fake/rg")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 2, stdout=b"", stderr=b"bad rg"),
    )
    with pytest.raises(RuntimeError, match="bad rg"):
        search_code(repo, "service", backend="rg")


def test_rg_no_matches(repo):
    require_rg()
    assert search_code(repo, "nonexistentterm", backend="rg") == []


def test_symlinks(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("service", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    assert [hit.path for hit in search_code(tmp_path, "service", backend="python")] == [
        "target.txt"
    ]


def test_rg_single_root_call(repo, monkeypatch):
    require_rg()
    original_run = subprocess.run
    calls = []

    def tracked_run(argv, **kwargs):
        calls.append(argv)
        return original_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", tracked_run)
    hits = search_code(repo, "user service", backend="rg", max_file_bytes=1000)
    assert hits[0].path == "service.py"
    assert len(calls) == 1
    argv = calls[0]
    assert "--no-require-git" in argv
    assert "--hidden" in argv
    assert argv[argv.index("--") + 1 :] == [str(repo.resolve())]
