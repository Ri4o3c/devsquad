"""验证 Planner 检索与故障降级。"""

from unittest.mock import Mock

import pytest

from devsquad.agent import planner
from devsquad.agent.loop import ModelTurn
from devsquad.retrieval.code_search import SearchHit


def make_model():
    return Mock(complete=Mock(return_value=ModelTurn(cost_usd=0, final_text="实施步骤")))


def test_hits(tmp_path):
    (tmp_path / "search.py").write_text("def search_code():\n    return []\n")
    model = make_model()
    result = planner.plan(root=tmp_path, requirement="修改 search_code 检索", model=model)
    messages = model.complete.call_args.args[0]
    assert "def search_code" in messages[-1]["content"]
    assert "search.py:1-2" in result
    assert "来自检索 #1" in result
    assert "查询=" in result and "关键词=" in result
    assert "不许读取或输出任何密钥文件或环境变量" in messages[0]["content"]


def test_no_hits(tmp_path):
    model = make_model()
    result = planner.plan(root=tmp_path, requirement="unknown", model=model)
    assert "实施步骤" in result and "未检索到相关代码" in result
    model.complete.assert_called_once()


def test_error(tmp_path, monkeypatch):
    monkeypatch.setattr(planner, "search_code", Mock(side_effect=RuntimeError("内部错误详情")))
    model = make_model()
    result = planner.plan(root=tmp_path, requirement="search", model=model)
    assert "实施步骤" in result and "未检索到相关代码" in result
    assert "内部错误详情" not in result + str(model.complete.call_args)


def test_limits(tmp_path, monkeypatch):
    hits = [SearchHit(f"file{i}.py", 1, 4, 1.0, "abcdefghij") for i in range(4)]
    search = Mock(return_value=hits)
    monkeypatch.setattr(planner, "search_code", search)
    model = make_model()
    result = planner.plan(
        root=tmp_path, requirement="search code files", model=model,
        config=planner.PlannerConfig(
            search_top_k=2, snippet_max_chars=4, max_keywords=2, query_max_chars=7
        ),
    )
    context = model.complete.call_args.args[0][-1]["content"]
    assert "file1.py" in result and "file2.py" not in context + result
    assert context.count("abcd") == 2 and "abcde" not in context
    assert search.call_args.kwargs["top_k"] == 2
    assert search.call_args.kwargs["keywords"] == ["search", "code"]
    assert len(search.call_args.args[1]) == 7


def test_keywords():
    assert planner.extract_keywords("修改 searchCode 检索") == ["search", "修改", "code", "检索"]
    assert planner.extract_keywords("search search", 1) == ["search"]


@pytest.mark.parametrize(
    "field", ["search_top_k", "snippet_max_chars", "max_keywords", "query_max_chars"]
)
def test_invalid_limits(field):
    with pytest.raises(ValueError, match="正数"):
        planner.PlannerConfig(**{field: 0})
