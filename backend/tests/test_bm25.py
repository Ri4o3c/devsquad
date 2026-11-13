"""``BM25Index`` 的验收题目（测试规格）。

下面的数字都是按 ``devsquad/retrieval/bm25.py`` 开头的公式算出来的。
在你实现那个模块之前，这些测试都会失败。``xfail(strict=False)`` 的作用是
让 CI 在你写完之前仍然保持绿色。

    pytest -m student --runxfail tests/test_bm25.py

排序类测试用的语料：

* d1 = "cat dog dog"（3 个词）
* d2 = "cat bird"（2 个词）
* d3 = "bird bird bird"（3 个词）

取 k1=1.5、b=0.75，N=3，avgdl=8/3 时：

* idf("dog") = ln(1 + (3-1+0.5)/(1+0.5)) ≈ 0.9808292530
* idf("cat") = idf("bird") = ln(1 + (3-2+0.5)/(2+0.5)) ≈ 0.4700036292
* 查询 "dog" → d1 ≈ 1.347061635
* 查询 "cat" → d2 ≈ 0.5295815541，然后 d1 ≈ 0.4449738502
  （d2 更短，所以分数更高）
* 查询 "bird" → d3 ≈ 0.759601825，然后 d2 ≈ 0.5295815541

取 b=0 时长度惩罚消失，"cat" 在 d1 和 d2 上的得分都正好等于 idf，
分数相同就按 doc_id 排：先 d1 后 d2。
"""

from __future__ import annotations

import pytest

from devsquad.retrieval.bm25 import BM25Index, Chunk

pytestmark = [
    pytest.mark.student,
    pytest.mark.xfail(
        reason="学生待办：实现 BM25Index.add 和 BM25Index.search",
        strict=False,
    ),
]


# 辅助函数：用上面三篇文档 d1、d2、d3 建一个索引。
def _index(**kwargs) -> BM25Index:
    index = BM25Index(**kwargs)
    index.add(
        [
            Chunk("d1", "cat dog dog"),
            Chunk("d2", "cat bird"),
            Chunk("d3", "bird bird bird"),
        ]
    )
    return index


# 题 1：搜 "dog"，只有 d1 含这个词，所以只返回 d1，分数要对上。
def test_search_dog_ranks_only_d1() -> None:
    hits = _index().search("dog")
    assert [doc_id for doc_id, _score in hits] == ["d1"]
    assert hits[0][1] == pytest.approx(1.347061635, rel=1e-6)


# 题 2：搜 "cat"，d1、d2 都只出现一次，但 d2 更短，所以 d2 排第一。
def test_search_cat_prefers_the_shorter_document() -> None:
    hits = _index().search("cat")
    assert [doc_id for doc_id, _score in hits] == ["d2", "d1"]
    assert hits[0][1] == pytest.approx(0.5295815541, rel=1e-6)
    assert hits[1][1] == pytest.approx(0.4449738502, rel=1e-6)


# 题 3：搜 "bird" 得 d3、d2；查询写成 "bird bird" 结果必须一模一样（重复词只算一次）。
def test_search_bird_and_repeated_query_terms() -> None:
    index = _index()
    once = index.search("bird")
    twice = index.search("bird bird")
    assert [doc_id for doc_id, _score in once] == ["d3", "d2"]
    assert once[0][1] == pytest.approx(0.759601825, rel=1e-6)
    assert once[1][1] == pytest.approx(0.5295815541, rel=1e-6)
    assert [doc_id for doc_id, _score in twice] == ["d3", "d2"]
    assert twice[0][1] == pytest.approx(once[0][1])
    assert twice[1][1] == pytest.approx(once[1][1])


# 题 4：大小写不敏感，并且 foo_bar 要被切成 foo 和 bar，所以搜 "BAR" 能命中。
def test_query_is_case_insensitive_and_splits_identifiers() -> None:
    index = BM25Index()
    index.add([Chunk("snake", "foo_bar foo", path="snake.py"), Chunk("other", "zzz")])
    hits = index.search("BAR")
    assert [doc_id for doc_id, _score in hits] == ["snake"]


# 题 5：top_k=1 时只返回 1 条，而且是分数最高的 d3。
def test_top_k_limits_results() -> None:
    hits = _index().search("bird", top_k=1)
    assert len(hits) == 1
    assert hits[0][0] == "d3"


# 题 6：两篇文档分数完全相同时，按 doc_id 从小到大排，"a" 在 "b" 前面。
def test_tie_breaks_by_doc_id() -> None:
    index = BM25Index()
    index.add([Chunk("b", "alpha alpha"), Chunk("a", "alpha alpha")])
    hits = index.search("alpha")
    assert [doc_id for doc_id, _score in hits] == ["a", "b"]
    assert hits[0][1] == pytest.approx(hits[1][1])


# 题 7：b=0 时不考虑文档长度，d1、d2 搜 "cat" 同分，按 doc_id 排成 d1、d2。
def test_b_zero_removes_the_length_penalty() -> None:
    hits = _index(b=0).search("cat")
    assert [doc_id for doc_id, _score in hits] == ["d1", "d2"]
    assert hits[0][1] == pytest.approx(0.4700036292, rel=1e-6)
    assert hits[1][1] == pytest.approx(hits[0][1])


# 题 8：空查询、全是空格、索引里没有的词、空索引，都要返回 []。
def test_empty_query_unknown_term_and_empty_index() -> None:
    index = _index()
    assert index.search("") == []
    assert index.search("   ") == []
    assert index.search("missing") == []
    assert BM25Index().search("cat") == []


# 题 9：再次加入已存在的 doc_id 要抛 ValueError，而且这一批里的其他文档（b）也不能加进去。
def test_duplicate_doc_id_raises_and_does_not_keep_a_partial_batch() -> None:
    index = BM25Index()
    index.add([Chunk("a", "alpha")])
    with pytest.raises(ValueError):
        index.add([Chunk("a", "beta"), Chunk("b", "gamma")])
    assert index.search("gamma") == []
    assert index.search("alpha")[0][0] == "a"


# 题 10：同一批里出现两个相同的 doc_id 也要抛 ValueError，并且一篇都不加。
def test_duplicate_inside_one_batch_raises() -> None:
    index = BM25Index()
    with pytest.raises(ValueError):
        index.add([Chunk("a", "one"), Chunk("a", "two")])
    assert index.search("one") == []


# 题 11：k1 为负、b 超出 [0, 1]、top_k 小于 1，都要抛 ValueError。
def test_invalid_parameters_and_top_k() -> None:
    with pytest.raises(ValueError):
        BM25Index(k1=-1)
    with pytest.raises(ValueError):
        BM25Index(b=1.5)
    index = _index()
    with pytest.raises(ValueError):
        index.search("cat", top_k=0)
