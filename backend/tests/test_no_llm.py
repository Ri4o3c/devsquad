"""Tripwires for this PR: no model SDK, and no shell=True in the package."""

from __future__ import annotations

import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

_BANNED = re.compile(
    r"shell\s*=\s*True|import\s+openai|from\s+openai|import\s+anthropic|"
    r"import\s+langgraph|from\s+langgraph|import\s+langchain|import\s+litellm|"
    r"api\.openai\.com|api\.anthropic\.com"
)


def test_package_source_has_no_shell_true_or_model_sdk() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if _BANNED.search(text):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []
