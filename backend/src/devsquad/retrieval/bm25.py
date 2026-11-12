"""BM25 over code chunks.

STATUS: interface only. The project owner implements ``BM25Index.add`` and
``BM25Index.search``.
Tests: ``backend/tests/test_bm25.py``
Run the failing tests with::

    pytest -m student --runxfail tests/test_bm25.py

The tests check numbers, not only ranking, so the formula below is part of
the contract.

Tokenization
    Lowercase the text and take every ``[a-z0-9]+`` run. ``Foo_Bar`` becomes
    ``foo`` and ``bar``.

IDF
    Lucene's smoothed form::

        idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))

    ``N`` is the number of chunks. ``df(t)`` is how many chunks contain ``t``
    at least once.

Per-term score
    ::

        denom = tf + k1 * (1 - b + b * dl / avgdl)
        score += idf(t) * (tf * (k1 + 1) / denom)

    ``dl`` is the chunk length in tokens. ``avgdl`` is the mean chunk length.
    A query term that never appears in the index contributes nothing.
    Repeated query terms count once (unique terms, first-seen order).

Search
    Return up to ``top_k`` pairs ``(doc_id, score)`` with ``score > 0``,
    sorted by score descending and then ``doc_id`` ascending.
    An empty query, a query with no tokens, or an empty index returns ``[]``.
    If every document tokenizes to nothing (``avgdl == 0``), return ``[]``.
    ``top_k < 1`` raises ``ValueError``.

``add``
    Reject a duplicate ``doc_id`` (including two copies in one call) with
    ``ValueError``, and leave the index unchanged when that happens.
    ``k1`` defaults to ``1.5`` and ``b`` defaults to ``0.75``. ``k1 < 0`` or
    ``b`` outside ``[0, 1]`` raises ``ValueError`` from ``__init__``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Chunk:
    """One retrievable piece of a file. ``path`` is metadata, not a ranking signal."""

    doc_id: str
    text: str
    path: str | None = None


class BM25Index:
    """In-memory BM25 index. Both mutators are unimplemented."""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b

    def add(self, chunks: list[Chunk]) -> None:
        """Index ``chunks``.

        TODO(student): tokenize, remember per-document term frequencies, and
        reject duplicate ``doc_id`` values with ``ValueError`` without
        keeping a partial batch. See the module docstring.

        Raises:
            NotImplementedError: always, until this body is replaced.
            ValueError: once implemented, when a ``doc_id`` is repeated.
        """
        raise NotImplementedError(
            "student TODO: implement BM25Index.add. "
            "See this module docstring and tests/test_bm25.py. "
            "Run: pytest -m student --runxfail tests/test_bm25.py"
        )

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Return the top ``top_k`` ``(doc_id, score)`` hits.

        TODO(student): apply the IDF and BM25 formula in the module
        docstring. Do not call a model and do not read the filesystem; the
        corpus is whatever ``add`` was given.

        Raises:
            NotImplementedError: always, until this body is replaced.
            ValueError: once implemented, when ``top_k < 1``.
        """
        raise NotImplementedError(
            "student TODO: implement BM25Index.search. "
            "See this module docstring and tests/test_bm25.py. "
            "Run: pytest -m student --runxfail tests/test_bm25.py"
        )
