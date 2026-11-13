"""对代码片段（chunk）做 BM25 检索。

状态：已实现。
测试：``backend/tests/test_bm25.py``
运行测试::

    pytest -m student --runxfail tests/test_bm25.py

测试会核对具体数字，而不只是排序，所以下面的公式是"合同"的一部分，必须照着写。

分词
    把文本转成小写，取出所有连续的 ``[a-z0-9]+`` 片段作为词。
    例如 ``Foo_Bar`` 会变成 ``foo`` 和 ``bar`` 两个词。

IDF（逆文档频率）
    使用 Lucene 的平滑形式::

        idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))

    ``N`` 是片段总数。``df(t)`` 是至少包含一次词 ``t`` 的片段数。

单个词的得分
    ::

        denom = tf + k1 * (1 - b + b * dl / avgdl)
        score += idf(t) * (tf * (k1 + 1) / denom)

    ``tf`` 是该词在这个片段里出现的次数。``dl`` 是片段长度（词数），
    ``avgdl`` 是所有片段的平均长度。
    查询里某个词如果在索引中从没出现过，它贡献 0 分。
    查询里重复的词只算一次（去重后保留第一次出现的顺序）。

搜索
    返回最多 ``top_k`` 个 ``(doc_id, score)``，只要 ``score > 0`` 的结果，
    先按分数从高到低排，分数相同再按 ``doc_id`` 从小到大排。
    查询为空、查询切不出任何词、或者索引为空时，返回 ``[]``。
    如果所有文档都切不出词（``avgdl == 0``），返回 ``[]``。
    ``top_k < 1`` 时抛出 ``ValueError``。

``add``
    遇到重复的 ``doc_id``（包括同一次调用里出现两份）时抛出 ``ValueError``，
    并且这时索引必须保持原样（这一批一个都不能加进去）。
    ``k1`` 默认 ``1.5``，``b`` 默认 ``0.75``。``k1 < 0`` 或者 ``b`` 不在
    ``[0, 1]`` 范围内时，``__init__`` 要抛出 ``ValueError``。
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

# 分词正则：连续的小写字母或数字算一个词（文本会先转小写）。
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    """转小写后取出所有 ``[a-z0-9]+`` 片段。``Foo_Bar`` -> ``["foo", "bar"]``。"""
    return _TOKEN_RE.findall(text.lower())


@dataclass(frozen=True)
class Chunk:
    """文件中一段可被检索的内容。``path`` 只是附带信息，不参与打分。"""

    doc_id: str
    text: str
    path: str | None = None


class BM25Index:
    """内存中的 BM25 索引。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        # 参数校验：k1 控制词频饱和速度，不能为负；b 控制长度归一化力度，只能在 [0, 1]。
        if k1 < 0:
            raise ValueError(f"k1 不能为负数，收到 {k1}")
        if not 0 <= b <= 1:
            raise ValueError(f"b 必须在 [0, 1] 之间，收到 {b}")
        self.k1 = k1
        self.b = b
        # 每篇文档的词频表：doc_id -> {词: 出现次数}
        self._tf: dict[str, Counter[str]] = {}
        # 每篇文档的长度（词数）：doc_id -> dl
        self._doc_len: dict[str, int] = {}
        # 文档频率：词 -> 有多少篇文档至少出现过一次这个词
        self._df: Counter[str] = Counter()
        # 所有文档的总词数，用来算平均长度 avgdl
        self._total_len = 0

    def add(self, chunks: list[Chunk]) -> None:
        """把 ``chunks`` 加入索引。``doc_id`` 重复时抛 ``ValueError`` 且索引保持不变。"""
        # 第一步：先整批检查，确认没有重复，再动索引。
        # 这样一旦报错，索引里不会留下"加了一半"的数据（相当于一个小事务）。
        seen: set[str] = set()
        for chunk in chunks:
            if chunk.doc_id in self._tf or chunk.doc_id in seen:
                raise ValueError(f"doc_id 重复：{chunk.doc_id!r}")
            seen.add(chunk.doc_id)

        # 第二步：检查通过后，逐篇分词并更新统计量。
        for chunk in chunks:
            tf = Counter(tokenize(chunk.text))
            self._tf[chunk.doc_id] = tf
            dl = sum(tf.values())
            self._doc_len[chunk.doc_id] = dl
            self._total_len += dl
            # df 按"篇"计数，所以对每篇文档的去重词表各加 1。
            self._df.update(tf.keys())

    def _idf(self, term: str) -> float:
        """Lucene 平滑 IDF：ln(1 + (N - df + 0.5) / (df + 0.5))，恒为正。"""
        n = len(self._tf)
        df = self._df[term]
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """返回得分最高的 ``top_k`` 个 ``(doc_id, score)``。"""
        if top_k < 1:
            raise ValueError(f"top_k 至少为 1，收到 {top_k}")

        # 查询词去重，保留第一次出现的顺序（dict.fromkeys 是有序去重的惯用写法）。
        terms = list(dict.fromkeys(tokenize(query)))
        n = len(self._tf)
        if not terms or n == 0 or self._total_len == 0:
            return []

        avgdl = self._total_len / n
        k1, b = self.k1, self.b
        scores: dict[str, float] = {}

        for term in terms:
            if self._df[term] == 0:
                continue  # 索引里没有的词贡献 0 分，直接跳过
            idf = self._idf(term)
            for doc_id, tf_table in self._tf.items():
                tf = tf_table.get(term, 0)
                if tf == 0:
                    continue
                dl = self._doc_len[doc_id]
                # 分母里的 (1 - b + b * dl / avgdl) 是长度归一化：文档越长，分数越被压低。
                denom = tf + k1 * (1 - b + b * dl / avgdl)
                # tf * (k1 + 1) / denom 随 tf 增长但有上限 (k1 + 1)，这就是"词频饱和"。
                scores[doc_id] = scores.get(doc_id, 0.0) + idf * (tf * (k1 + 1) / denom)

        # 只要正分；先按分数降序，同分按 doc_id 升序（取负号实现降序）。
        hits = [(doc_id, score) for doc_id, score in scores.items() if score > 0]
        hits.sort(key=lambda item: (-item[1], item[0]))
        return hits[:top_k]
