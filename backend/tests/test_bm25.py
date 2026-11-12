"""Spec for ``BM25Index``.

Numbers below were computed from the formula in ``devsquad/retrieval/bm25.py``.
The tests fail until that module is implemented. ``xfail(strict=False)`` keeps
CI green.

    pytest -m student --runxfail tests/test_bm25.py

Corpus used by the ranking tests:

* d1 = "cat dog dog" (3 tokens)
* d2 = "cat bird" (2 tokens)
* d3 = "bird bird bird" (3 tokens)

With k1=1.5 and b=0.75, N=3, avgdl=8/3:

* idf("dog") = ln(1 + (3-1+0.5)/(1+0.5)) ≈ 0.9808292530
* idf("cat") = idf("bird") = ln(1 + (3-2+0.5)/(2+0.5)) ≈ 0.4700036292
* query "dog" → d1 ≈ 1.347061635
* query "cat" → d2 ≈ 0.5295815541, then d1 ≈ 0.4449738502
  (d2 wins because it is shorter)
* query "bird" → d3 ≈ 0.759601825, then d2 ≈ 0.5295815541

With b=0 the length penalty disappears, so "cat" scores idf for both d1 and
d2 and the tie breaks by doc_id: d1 then d2.
"""

from __future__ import annotations

import pytest

from devsquad.retrieval.bm25 import BM25Index, Chunk

pytestmark = [
    pytest.mark.student,
    pytest.mark.xfail(
        reason="student TODO: implement BM25Index.add and BM25Index.search",
        strict=False,
    ),
]


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


def test_search_dog_ranks_only_d1() -> None:
    hits = _index().search("dog")
    assert [doc_id for doc_id, _score in hits] == ["d1"]
    assert hits[0][1] == pytest.approx(1.347061635, rel=1e-6)


def test_search_cat_prefers_the_shorter_document() -> None:
    hits = _index().search("cat")
    assert [doc_id for doc_id, _score in hits] == ["d2", "d1"]
    assert hits[0][1] == pytest.approx(0.5295815541, rel=1e-6)
    assert hits[1][1] == pytest.approx(0.4449738502, rel=1e-6)


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


def test_query_is_case_insensitive_and_splits_identifiers() -> None:
    index = BM25Index()
    index.add([Chunk("snake", "foo_bar foo", path="snake.py"), Chunk("other", "zzz")])
    hits = index.search("BAR")
    assert [doc_id for doc_id, _score in hits] == ["snake"]


def test_top_k_limits_results() -> None:
    hits = _index().search("bird", top_k=1)
    assert len(hits) == 1
    assert hits[0][0] == "d3"


def test_tie_breaks_by_doc_id() -> None:
    index = BM25Index()
    index.add([Chunk("b", "alpha alpha"), Chunk("a", "alpha alpha")])
    hits = index.search("alpha")
    assert [doc_id for doc_id, _score in hits] == ["a", "b"]
    assert hits[0][1] == pytest.approx(hits[1][1])


def test_b_zero_removes_the_length_penalty() -> None:
    hits = _index(b=0).search("cat")
    assert [doc_id for doc_id, _score in hits] == ["d1", "d2"]
    assert hits[0][1] == pytest.approx(0.4700036292, rel=1e-6)
    assert hits[1][1] == pytest.approx(hits[0][1])


def test_empty_query_unknown_term_and_empty_index() -> None:
    index = _index()
    assert index.search("") == []
    assert index.search("   ") == []
    assert index.search("missing") == []
    assert BM25Index().search("cat") == []


def test_duplicate_doc_id_raises_and_does_not_keep_a_partial_batch() -> None:
    index = BM25Index()
    index.add([Chunk("a", "alpha")])
    with pytest.raises(ValueError):
        index.add([Chunk("a", "beta"), Chunk("b", "gamma")])
    assert index.search("gamma") == []
    assert index.search("alpha")[0][0] == "a"


def test_duplicate_inside_one_batch_raises() -> None:
    index = BM25Index()
    with pytest.raises(ValueError):
        index.add([Chunk("a", "one"), Chunk("a", "two")])
    assert index.search("one") == []


def test_invalid_parameters_and_top_k() -> None:
    with pytest.raises(ValueError):
        BM25Index(k1=-1)
    with pytest.raises(ValueError):
        BM25Index(b=1.5)
    index = _index()
    with pytest.raises(ValueError):
        index.search("cat", top_k=0)
