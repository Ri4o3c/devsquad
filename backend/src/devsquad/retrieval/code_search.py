"""第一期 ripgrep + BM25 代码检索；向量与 RRF 留到第二期。

中文需求应先改写成英文关键词，再通过 keywords 传入。本模块不调用模型。
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from devsquad.retrieval.bm25 import BM25Index, Chunk, tokenize
from devsquad.sandbox.pathguard import is_secret_filename


@dataclass(frozen=True)
class SearchHit:
    """相关代码原文，行号从 1 起且包含两端。"""

    path: str
    start_line: int
    end_line: int
    score: float
    snippet: str


def _expanded(text: str) -> str:
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).replace("_", " ")


def _glob_regex(pattern: str) -> str:
    """把 gitignore 通配符转成不跨目录的正则。"""
    result = ""
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern[i : i + 3] == "**/":
            result += "(?:.*/)?"
            i += 3
            continue
        if pattern[i : i + 2] == "**":
            result += ".*"
            i += 2
            continue
        result += "[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char)
        i += 1
    return result


class GitIgnore:
    """一个目录的有序忽略规则；父目录被忽略后不再遍历。"""

    def __init__(self, text: str, base: str = "") -> None:
        self.base = base
        self.rules: list[tuple[bool, bool, re.Pattern[str]]] = []
        for line in text.splitlines():
            line = line.rstrip()
            if not line or line.startswith("#"):
                continue
            negative = line.startswith("!")
            if negative:
                line = line[1:]
            directory = line.endswith("/")
            line = line.rstrip("/")
            anchored = "/" in line or line.startswith("/")
            line = line.lstrip("/")
            prefix = "" if anchored else "(?:.*/)?"
            self.rules.append((negative, directory, re.compile(prefix + _glob_regex(line))))

    def ignored(self, path: str, is_dir: bool, previous: bool = False) -> bool:
        """path 使用仓库相对路径，返回应用本层规则后的状态。"""
        if self.base:
            if not path.startswith(self.base + "/"):
                return previous
            path = path[len(self.base) + 1 :]
        for negative, directory, pattern in self.rules:
            if (not directory or is_dir) and pattern.fullmatch(path):
                previous = not negative
        return previous


def _blocked(path: Path) -> bool:
    return (
        path.is_symlink()
        or (os.name == "nt" and path.resolve() != path.absolute())
        or path.name.lower() in {".git", ".npmrc", ".pypirc"}
        or is_secret_filename(path.name)
    )


class _PathFilter:
    """共用的路径与内容过滤；每个目录的 gitignore 只读取一次。"""

    def __init__(self, root: Path, limit: int) -> None:
        self.root = root
        self.limit = limit
        self.cache: dict[Path, GitIgnore] = {}

    def _rules(self, directory: Path) -> GitIgnore:
        if directory not in self.cache:
            ignore = directory / ".gitignore"
            text = ""
            try:
                if (
                    not _blocked(ignore)
                    and ignore.is_file()
                    and ignore.stat().st_size <= self.limit
                ):
                    text = ignore.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
            base = "" if directory == self.root else directory.relative_to(self.root).as_posix()
            self.cache[directory] = GitIgnore(text, base)
        return self.cache[directory]

    def allowed(self, path: Path, is_dir: bool = False) -> bool:
        """逐级检查路径，父目录被拒绝时不再读取其忽略规则。"""
        try:
            parts = path.relative_to(self.root).parts
        except ValueError:
            return False
        if not parts or ".." in parts:
            return False
        parent = self.root
        rules = []
        for index, part in enumerate(parts):
            current = parent / part
            if _blocked(current):
                return False
            rules.append(self._rules(parent))
            directory = index < len(parts) - 1 or is_dir
            ignored = False
            for rule in rules:
                ignored = rule.ignored(
                    current.relative_to(self.root).as_posix(), directory, ignored
                )
            if ignored:
                return False
            parent = current
        return True

    def read(self, path: Path) -> bytes | None:
        """只读合规的候选文件，同时检查大小和二进制头。"""
        if not self.allowed(path):
            return None
        try:
            if not path.is_file() or path.stat().st_size > self.limit:
                return None
            with path.open("rb") as stream:
                data = stream.read(self.limit + 1)
        except OSError:
            return None
        if len(data) > self.limit or b"\0" in data[:8192]:
            return None
        return data


def _files(root: Path, filters: _PathFilter):
    """Python 后端遍历仓库，提前剪掉被拒绝的目录。"""
    for directory, dirs, files in os.walk(root, followlinks=False):
        parent = Path(directory)
        dirs[:] = sorted(name for name in dirs if filters.allowed(parent / name, True))
        for name in sorted(files):
            yield parent / name


def _chunks(path: str, text: str):
    lines = text.splitlines(keepends=True)
    ranges = []
    if path.lower().endswith(".py"):
        try:
            tree = ast.parse(text)
            cursor = 1
            for node in tree.body:
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                    start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                    ranges.extend([(cursor, start - 1), (start, node.end_lineno)])
                    cursor = node.end_lineno + 1
            ranges.append((cursor, len(lines)))
        except (SyntaxError, ValueError):
            pass
    if not ranges:
        ranges = [(i + 1, min(i + 60, len(lines))) for i in range(0, len(lines), 50)]
    for start, end in ranges:
        snippet = "".join(lines[start - 1 : end])
        if snippet.strip():
            yield start, end, snippet


def search_code(
    root: str | Path,
    query: str,
    *,
    keywords: list[str] | None = None,
    top_k: int = 8,
    backend: str = "auto",
    max_file_bytes: int = 1_000_000,
) -> list[SearchHit]:
    """召回文件后对完整代码块做 BM25；中文需求请先改写 keywords。"""
    if top_k < 1:
        raise ValueError("top_k 至少为 1")
    if backend not in {"auto", "rg", "python"}:
        raise ValueError("backend 必须是 auto、rg 或 python")
    executable = shutil.which("rg") if backend != "python" else None
    if backend == "rg" and executable is None:
        raise RuntimeError("未找到 ripgrep（rg），请安装或使用 python 后端")
    words = keywords if keywords is not None else tokenize(query) + tokenize(_expanded(query))
    words = list(dict.fromkeys(word.strip() for word in words if len(word.strip()) >= 2))
    if not words:
        return []
    root = Path(root).resolve(strict=True)
    filters = _PathFilter(root, max_file_bytes)
    if executable:
        # rg 自行遍历，只启用仓库内的 .gitignore；返回路径仍需经过共用过滤。
        argv = [
            str(Path(executable).resolve()),
            "--files-with-matches",
            "--null",
            "--ignore-case",
            "--fixed-strings",
            "--hidden",
            "--no-require-git",
            "--no-config",
            "--no-ignore-global",
            "--no-ignore-exclude",
            "--no-ignore-parent",
            "--no-ignore-dot",
            "--glob",
            "!.git",
            "--no-messages",
        ]
        for word in words:
            argv.extend(["-e", word])
        result = subprocess.run(
            argv + ["--", str(root)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(f"ripgrep 检索失败：{os.fsdecode(result.stderr)}")
        candidates = {Path(os.fsdecode(p)) for p in result.stdout.split(b"\0") if p}
    else:
        candidates = _files(root, filters)
    index = BM25Index()
    hits = {}
    for path in sorted(candidates):
        data = filters.read(path)
        if data is None:
            continue
        text = data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
        if not executable and not any(word.lower() in text.lower() for word in words):
            continue
        relative = path.relative_to(root).as_posix()
        for start, end, snippet in _chunks(relative, text):
            doc_id = f"{relative}:{start}-{end}"
            hits[doc_id] = (relative, start, end, snippet)
            index.add(
                [
                    Chunk(
                        doc_id,
                        snippet
                        + "\n"
                        + _expanded(snippet)
                        + "\n"
                        + relative
                        + " "
                        + _expanded(relative),
                        relative,
                    )
                ]
            )
    ranking = index.search(
        query + " " + _expanded(query) + " " + " ".join(words) + " " + _expanded(" ".join(words)),
        top_k,
    )
    return [
        SearchHit(hits[key][0], hits[key][1], hits[key][2], score, hits[key][3])
        for key, score in ranking
    ]
