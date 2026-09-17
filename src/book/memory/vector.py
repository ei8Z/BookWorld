"""零依赖"类语义"检索——字符 n-gram 哈希稀疏向量。

对应《开发计划.md》P2-1。设计取舍：

- 用 FNV-1a 把字符二元组/三元组哈希到固定维度（默认 4096），累积成稀疏向量；
- 余弦相似度做召回，**不引入任何第三方依赖**（计划 §7 禁止默认依赖）；
- 明确边界：稀疏向量对**共享词汇**的改写有效（"服务器机房"↔"机房的服务器"），
  对**纯同义改写**（"电脑"↔"计算机"）召回有限——那不是词形相似能解决的问题。

同时提供 Embedder 协议，为将来接本地 dense 模型（bge-small-zh /
sentence-transformers，作为可选 extra）预留接口。本次只留接口不实现。
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

# 稀疏向量维度（哈希空间大小）。
# 最初取 4096，但实测会撞出严重假阳性：'信息' 与 '动作' 同桶 → 两字文档相似度 1.0。
# 取 2^16 后单对短词的碰撞概率降到 ~1.5e-5。
DIM = 65536

# 至少要有这么多个特征桶命中才认相似度。
# 只命中 1 个桶的"相似"基本是碰撞或单字偶然重合，不足以支撑召回。
MIN_SHARED_FEATURES = 2

# FNV-1a 64 位参数
_FNV_OFFSET = 0xCBF29CE484222325
_FNV_PRIME = 0x100000001B3
_MASK64 = 0xFFFFFFFFFFFFFFFF


def _fnv1a(text: str) -> int:
    """FNV-1a 哈希（64 位）。"""
    h = _FNV_OFFSET
    for byte in text.encode("utf-8"):
        h ^= byte
        h = (h * _FNV_PRIME) & _MASK64
    return h


def _ngrams(text: str) -> list[str]:
    """字符二元组 + 三元组（不足 3 字时只出二元组；单字出单字）。

    先把空白/标点归一掉：检索关心的是词形，不关心排版。
    """
    cleaned = "".join(ch for ch in (text or "") if ch.isalnum())
    if not cleaned:
        return []
    if len(cleaned) == 1:
        return [cleaned]
    grams: list[str] = []
    for n in (2, 3):
        if len(cleaned) >= n:
            for i in range(len(cleaned) - n + 1):
                grams.append(cleaned[i : i + n])
    return grams


def sparse_embed(text: str, dim: int = DIM) -> dict[int, float]:
    """文本 → 稀疏向量 {维度: 权重}。

    权重用 tf 的平方根做次线性缩放：长文本里高频 n-gram 不该线性压倒内容。
    """
    grams = _ngrams(text)
    if not grams:
        return {}
    counts: dict[int, int] = {}
    for g in grams:
        idx = _fnv1a(g) % dim
        counts[idx] = counts.get(idx, 0) + 1
    return {idx: math.sqrt(c) for idx, c in counts.items()}


def cosine(a: dict[int, float], b: dict[int, float], min_shared: int = 0) -> float:
    """稀疏向量余弦相似度；任一为空返回 0.0。

    min_shared > 0 时，共享特征桶少于该数量直接返回 0.0——用于掐掉
    "单个 n-gram 偶然同桶"造成的假阳性（例如查询'信息'命中无关文档'动作'）。
    """
    if not a or not b:
        return 0.0
    # 先走小的一侧，减少查表次数
    if len(a) > len(b):
        a, b = b, a
    dot = 0.0
    shared = 0
    for idx, va in a.items():
        vb = b.get(idx)
        if vb:
            dot += va * vb
            shared += 1
    if dot == 0.0 or shared < min_shared:
        return 0.0
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0.0 or nb == 0.0:
        return 0.0
    # 夹紧到 [0, 1]：浮点累加会让"自身相似度"偶发变成 1.0000000000000002
    return min(1.0, max(0.0, dot / (na * nb)))


@runtime_checkable
class Embedder(Protocol):
    """dense 向量接口（本次不实现，仅预留）。

    接本地模型的方式（可选 extra，不装也能跑）：

        # pip install "book-project[dense]"
        class BgeEmbedder:
            def __init__(self) -> None:
                from sentence_transformers import SentenceTransformer
                self._m = SentenceTransformer("BAAI/bge-small-zh-v1.5")

            def embed(self, texts: list[str]) -> list[list[float]]:
                return self._m.encode(texts, normalize_embeddings=True).tolist()

    接进来之后，`search(..., embedder=...) ` 可以走 dense 召回，
    与现有稀疏路径并存；两者都不可用时回落到 FTS。
    """

    def embed(self, texts: list[str]) -> list[list[float]]:
        ...
