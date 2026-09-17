"""检索层——中文二元组分词 + FTS5 全文索引。

设计要点（详见《开发计划.md》P0-2）：
- FTS5 默认分词器不切分中文，故在写入前自行做字符二元组预处理；
- 不使用 trigram 分词器（2 字中文人名会失配）；
- search_docs 保存原文与元数据，search_fts 只保存分词结果，两者 rowid 一一对应；
- 摘要只做索引，原文永不删（原文仍在 session_history 等业务表里）；
- P2 起提供 semantic=True 的稀疏向量召回路径（见 memory/vector.py）。
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

from ..memory.vector import MIN_SHARED_FEATURES, cosine, sparse_embed

# ─── 分词 ──────────────────────────────────────────────────────────────────


def _is_cjk(ch: str) -> bool:
    """是否为中日韩表意文字（需要二元组切分）。"""
    code = ord(ch)
    return (
        0x3400 <= code <= 0x4DBF        # 扩展 A
        or 0x4E00 <= code <= 0x9FFF     # 基本区
        or 0xF900 <= code <= 0xFAFF     # 兼容表意文字
        or 0x20000 <= code <= 0x2FA1F   # 扩展 B~
    )


def _tokenize_impl(text: str) -> list[str]:
    """分词实现：CJK 二元组 + ASCII 整词，按出现顺序返回（含重复）。"""
    tokens: list[str] = []
    buffer: list[str] = []
    ascii_word: list[str] = []

    def flush_cjk():
        if not buffer:
            return
        if len(buffer) == 1:
            tokens.append(buffer[0])  # 单字保留
        else:
            for i in range(len(buffer) - 1):
                tokens.append(buffer[i] + buffer[i + 1])
        buffer.clear()

    def flush_ascii():
        if ascii_word:
            tokens.append("".join(ascii_word).lower())
            ascii_word.clear()

    for ch in text:
        if _is_cjk(ch):
            flush_ascii()
            buffer.append(ch)
        elif ch.isascii() and ch.isalnum():
            flush_cjk()
            ascii_word.append(ch)
        else:
            flush_cjk()
            flush_ascii()
    flush_cjk()
    flush_ascii()
    return tokens


def tokenize_zh(text: str) -> list[str]:
    """CJK 连续串 → 二元组（长度为 1 时保留单字）；ASCII 词 → 小写整词；其余作分隔符。

    示例: "林越在服务器看到数据" → ['林越','越在','在服','服务','务器','器看','看到','到数','数据']
    """
    return _tokenize_impl(text or "")


def tokenize_to_query(text: str) -> str:
    """分词并去重，返回写入 FTS5 的 tokens 字符串（空格分隔）。"""
    seen: set[str] = set()
    ordered: list[str] = []
    for tok in _tokenize_impl(text or ""):
        if tok not in seen:
            seen.add(tok)
            ordered.append(tok)
    return " ".join(ordered)


def _quote(token: str) -> str:
    """把单个 token 包成 FTS5 字符串字面量，内部双引号翻倍转义。"""
    return '"' + token.replace('"', '""') + '"'


def _phrase_expr(tokens: list[str]) -> str:
    """FTS5 短语表达式：tokens 必须在文档里连续出现。

    注意 FTS5 语义：「"a b"」是短语，而「"a" "b"」只是 AND（可以隔着别的词）。
    §P0-2 原文写的是后者，实测会把"两词都出现但不相邻"的文档也当成命中，
    因此这里用真正的短语形式。
    """
    joined = " ".join(t.replace('"', '""') for t in tokens)
    return '"' + joined + '"'


def _and_expr(tokens: list[str]) -> str:
    """FTS5 AND 表达式（token 不必连续）。"""
    return " AND ".join(_quote(t) for t in tokens)


# ─── 检索结果 ──────────────────────────────────────────────────────────────


@dataclass
class SearchHit:
    """一条检索命中。snippet 为命中处 ±40 字的窗口。

    matched 记录这条命中来自哪一级检索：phrase（连续）| and（各词都出现）
    | or（任一命中）| like（单字回退）。§5 只要求前 8 个字段，这是附加信息，
    让 agent 能区分"精确命中"和"宽松召回的噪声"。
    """

    id: int
    source: str
    ref_id: str
    volume: int | None
    chapter: int | None
    pov: str | None
    text: str
    snippet: str
    score: float
    matched: str = "phrase"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source": self.source,
            "ref_id": self.ref_id,
            "volume": self.volume,
            "chapter": self.chapter,
            "pov": self.pov,
            "text": self.text,
            "snippet": self.snippet,
            "score": self.score,
            "matched": self.matched,
        }


# ─── 建表 ──────────────────────────────────────────────────────────────────

SEARCH_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS search_docs(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  ref_id TEXT NOT NULL DEFAULT '',
  volume INTEGER, chapter INTEGER, pov TEXT,
  text TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source, ref_id)
);
CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(tokens);
CREATE INDEX IF NOT EXISTS idx_search_docs_source ON search_docs(source);
CREATE INDEX IF NOT EXISTS idx_search_docs_volume ON search_docs(volume);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """幂等创建检索表结构。

    先探测表是否已存在：已存在时直接返回，避免每次 upsert 都跑一遍
    executescript——后者会隐式提交未完成的事务，把调用方的写入边界打乱。
    """
    if _table_exists(conn, "search_docs") and _table_exists(conn, "search_fts"):
        return
    conn.executescript(SEARCH_SCHEMA_SQL)
    if not conn.in_transaction:
        conn.commit()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


# ─── 写入 ──────────────────────────────────────────────────────────────────


def _hash_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _set_fts(conn: sqlite3.Connection, doc_id: int, text: str) -> None:
    """重建某一行的 FTS tokens（先删后插，保证 rowid 唯一对应）。"""
    conn.execute("DELETE FROM search_fts WHERE rowid = ?", (doc_id,))
    tokens = tokenize_to_query(text)
    if tokens:
        conn.execute(
            "INSERT INTO search_fts(rowid, tokens) VALUES (?, ?)", (doc_id, tokens)
        )


def upsert_doc(
    conn: sqlite3.Connection,
    *,
    source: str,
    ref_id: str,
    text: str,
    volume: int | None = None,
    chapter: int | None = None,
    pov: str | None = None,
) -> int:
    """写入/更新一条检索文档，返回 search_docs.id。

    幂等：content_hash 未变则直接返回既有 id，不改文本、不重建索引。
    """
    ensure_schema(conn)
    ref_id = "" if ref_id is None else str(ref_id)
    text = text or ""
    digest = _hash_text(text)

    row = conn.execute(
        "SELECT id, content_hash FROM search_docs WHERE source=? AND ref_id=?",
        (source, ref_id),
    ).fetchone()

    if row is not None and row["content_hash"] == digest:
        # 文本未变：仅在元数据有值时补齐 volume/chapter/pov
        conn.execute(
            """UPDATE search_docs
               SET volume = COALESCE(?, volume),
                   chapter = COALESCE(?, chapter),
                   pov = COALESCE(?, pov)
               WHERE id = ?""",
            (volume, chapter, pov, row["id"]),
        )
        if not conn.in_transaction:
            conn.commit()
        return int(row["id"])

    if row is not None:
        doc_id = int(row["id"])
        conn.execute(
            """UPDATE search_docs
               SET text=?, content_hash=?, volume=?, chapter=?, pov=?,
                   updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (text, digest, volume, chapter, pov, doc_id),
        )
    else:
        cur = conn.execute(
            """INSERT INTO search_docs(source, ref_id, volume, chapter, pov, text, content_hash)
               VALUES (?,?,?,?,?,?,?)""",
            (source, ref_id, volume, chapter, pov, text, digest),
        )
        doc_id = int(cur.lastrowid)

    _set_fts(conn, doc_id, text)
    # 无条件提交：sqlite3 的 Connection.close() 会回滚未提交事务，
    # 不能依赖 in_transaction 判断（FTS5 写入后该判断不可靠）。
    conn.commit()
    return doc_id


def index_session(
    conn: sqlite3.Connection,
    session_id: int,
    narrative: str,
    volume: int | None = None,
    chapter: int | None = None,
    pov: str | None = None,
) -> int:
    """把一行 session_history 的正文写入索引，ref_id = 该行主键。

    ref_id 必须用 session_history.id 而不是 round_number：§P0-2 把 ref_id 定义为
    "来源行主键"，且 rebuild_all 用行主键重建。若这里用轮次号，同一轮的多行会
    互相覆盖索引，且一次 reindex 后 ref_id 全部漂移。

    只索引正文本身；pov 作为元数据列存储（可过滤），不混进检索文本。
    """
    return upsert_doc(
        conn,
        source="narrative",
        ref_id=str(session_id),
        text=narrative,
        volume=volume,
        chapter=chapter,
        pov=pov,
    )


# ─── 检索 ──────────────────────────────────────────────────────────────────


def _make_snippet(text: str, window: int = 40, probes: list[str] | None = None) -> str:
    """取命中处 ±window 字的窗口；无命中时取开头一段。

    probes 是查询分词：优先用它们在原文里定位命中位置。定位失败时按
    「较长的 token 更可能是实词」的启发式回退（英文长词比中文二元组更可信）。
    """
    if not text:
        return ""
    if len(text) <= window * 2:
        return text

    pos = -1
    for probe in sorted(probes or [], key=len, reverse=True):
        if not probe:
            continue
        found = text.find(probe)
        if found >= 0:
            pos = found
            break
    if pos < 0:
        tokens = tokenize_zh(text)
        if not tokens:
            return text[: window * 2]
        probe = max(tokens, key=len)
        pos = text.find(probe)
    if pos < 0:
        return text[: window * 2]

    start = max(0, pos - window)
    end = min(len(text), pos + window)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + text[start:end] + suffix


def _run_match(
    conn: sqlite3.Connection,
    match_expr: str,
    *,
    k: int,
    source: str | None,
    volume: int | None,
) -> list[sqlite3.Row]:
    sql = [
        "SELECT d.id, d.source, d.ref_id, d.volume, d.chapter, d.pov, d.text,",
        "       bm25(search_fts) AS rank",
        "FROM search_fts JOIN search_docs d ON d.id = search_fts.rowid",
        "WHERE search_fts MATCH ?",
    ]
    params: list = [match_expr]
    if source:
        sql.append("AND d.source = ?")
        params.append(source)
    if volume is not None:
        sql.append("AND d.volume = ?")
        params.append(volume)
    sql.append("ORDER BY rank LIMIT ?")
    params.append(k)
    try:
        return conn.execute(" ".join(sql), params).fetchall()
    except sqlite3.OperationalError:
        # 查询串本身不合法（极端符号组合）时静默返回空
        return []


def _rows_to_hits(
    rows: list[sqlite3.Row], probes: list[str] | None = None, matched: str = "phrase"
) -> list[SearchHit]:
    hits = []
    for r in rows:
        text = r["text"] or ""
        hits.append(
            SearchHit(
                id=int(r["id"]),
                source=r["source"],
                ref_id=r["ref_id"],
                volume=r["volume"],
                chapter=r["chapter"],
                pov=r["pov"],
                text=text,
                snippet=_make_snippet(text, probes=probes),
                score=round(-float(r["rank"]), 6),
                matched=matched,
            )
        )
    return hits


def semantic_search(
    conn: sqlite3.Connection,
    query: str,
    *,
    k: int = 5,
    source: str | None = None,
    volume: int | None = None,
    min_score: float = 0.0,
) -> list[SearchHit]:
    """稀疏向量召回（P2）：按字符 n-gram 余弦相似度排序。

    与 FTS 路径互补：FTS 要求词形精确（二元组）命中，这条路能召回
    "共享词汇但写法不同"的片段（例如查询"机房的服务器"能命中"服务器机房"）。
    纯同义改写（"电脑"→"计算机"）仍召回不了，这是词形相似的固有边界。
    """
    ensure_schema(conn)
    if not query or not query.strip() or k <= 0:
        return []

    q_vec = sparse_embed(query)
    if not q_vec:
        return []

    sql = [
        "SELECT id, source, ref_id, volume, chapter, pov, text",
        "FROM search_docs WHERE 1=1",
    ]
    params: list = []
    if source:
        sql.append("AND source = ?")
        params.append(source)
    if volume is not None:
        sql.append("AND volume = ?")
        params.append(volume)

    scored: list[tuple[float, sqlite3.Row]] = []
    for row in conn.execute(" ".join(sql), params).fetchall():
        text = row["text"] or ""
        if not text:
            continue
        score = cosine(q_vec, sparse_embed(text), min_shared=MIN_SHARED_FEATURES)
        if score > min_score:
            scored.append((score, row))

    scored.sort(key=lambda x: (-x[0], x[1]["id"]))
    hits: list[SearchHit] = []
    for score, row in scored[:k]:
        hits.append(
            SearchHit(
                id=int(row["id"]),
                source=row["source"],
                ref_id=row["ref_id"],
                volume=row["volume"],
                chapter=row["chapter"],
                pov=row["pov"],
                text=row["text"],
                snippet=_make_snippet(row["text"], probes=tokenize_zh(query)),
                score=round(score, 6),
                matched="semantic",
            )
        )
    return hits


def search(
    conn: sqlite3.Connection,
    query: str,
    *,
    k: int = 5,
    source: str | None = None,
    volume: int | None = None,
    semantic: bool = False,
    min_score: float = 0.0,
) -> list[SearchHit]:
    """检索历史原文。

    semantic=True 时走稀疏向量召回（P2），否则走 FTS 路径。

    FTS 策略（顺序同 §P0-2，但每步用 FTS5 的正确写法）：
      1) 短语查询：二元组必须连续出现（"甲 乙"），最精确
      2) AND 查询：各二元组都出现即可（"甲" AND "乙"）
      3) OR 查询：任一命中即可（"甲" OR "乙"）
      4) 单个汉字 → LIKE 回退（FTS 只索引二元组，单字没有对应 token）
      5) bm25(search_fts) 排序

    注：semantic 路径不自动回退到 FTS——两者的排序不可比，混在一起
    会让 score 失去意义；调用方（CLI）出错或想对比时自己再调一次即可。
    """
    if semantic:
        return semantic_search(conn, query, k=k, source=source, volume=volume,
                               min_score=min_score)

    ensure_schema(conn)
    if not query or not query.strip() or k <= 0:
        return []

    tokens = tokenize_zh(query)
    if not tokens:
        return []

    # 1) 短语查询：二元组必须连续出现（最精确）
    rows = _run_match(conn, _phrase_expr(tokens), k=k, source=source, volume=volume)
    if rows:
        return _rows_to_hits(rows, probes=tokens, matched="phrase")

    if len(tokens) > 1:
        # 2) AND：各词都出现即可（短语未命中时放宽）
        rows = _run_match(conn, _and_expr(tokens), k=k, source=source, volume=volume)
        if rows:
            return _rows_to_hits(rows, probes=tokens, matched="and")

        # 3) OR：按"词"或，而不是按二元组或。
        #    中文一个词会切成多个二元组，若按二元组 OR，两个不相干的词会因为
        #    跨词的二元组（"AB"+"CD" → "BC"）互相命中，产生跨文档假阳性。
        words = [w.strip() for w in (query or "").split() if w.strip()] or [query]
        word_tokens = [tokenize_zh(w) for w in words]
        word_tokens = [t for t in word_tokens if t]
        if len(word_tokens) > 1:
            or_expr = " OR ".join(_phrase_expr(t) for t in word_tokens)
        else:
            or_expr = _phrase_expr(tokens)
        rows = _run_match(conn, or_expr, k=k, source=source, volume=volume)
        if rows:
            return _rows_to_hits(rows, probes=tokens, matched="or")

    # 4) 单字回退：FTS 只索引二元组，单字没有对应 token，改在原文上做子串匹配。
    #    单个汉字直接查；多个单字词（如"甲 乙"）按 OR 查原文
    if tokens and all(len(t) == 1 for t in tokens):
        return _search_like(
            conn,
            tokens[0],
            k=k,
            source=source,
            volume=volume,
            probes=tokens,
            matched="like",
            extra_needles=tokens[1:],
        )

    return []


def _search_like(
    conn: sqlite3.Connection,
    needle: str,
    *,
    k: int,
    source: str | None,
    volume: int | None,
    probes: list[str] | None = None,
    matched: str = "like",
    extra_needles: list[str] | None = None,
) -> list[SearchHit]:
    """单字回退：直接在原文上做 LIKE（FTS 不索引单字）。

    extra_needles 非空时按 OR 连接（查询本身是"多个单字词"的情况）。
    """

    def like_pattern(s: str) -> str:
        escaped = s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return "%" + escaped + "%"

    needles = [needle] + list(extra_needles or [])
    sql = [
        "SELECT id, source, ref_id, volume, chapter, pov, text, 0.0 AS rank",
        "FROM search_docs WHERE ("
        + " OR ".join(["text LIKE ? ESCAPE '\\'"] * len(needles))
        + ")",
    ]
    params: list = [like_pattern(n) for n in needles]
    if source:
        sql.append("AND source = ?")
        params.append(source)
    if volume is not None:
        sql.append("AND volume = ?")
        params.append(volume)
    sql.append("ORDER BY id LIMIT ?")
    params.append(k)
    rows = conn.execute(" ".join(sql), params).fetchall()
    return _rows_to_hits(rows, probes=probes, matched=matched)


# ─── 全量重建 ──────────────────────────────────────────────────────────────

_SOURCE_QUERIES: list[tuple[str, str]] = [
    (
        "npc",
        """SELECT CAST(id AS TEXT) AS ref_id,
                  name || ' ' || COALESCE(occupation,'') || ' ' || COALESCE(appearance,'')
                       || ' ' || COALESCE(personality,'') || ' ' || COALESCE(equipment,'')
                       || ' ' || COALESCE(inner_thought,'') || ' ' || COALESCE(intel,'')
                       || ' ' || COALESCE(status,'') AS text
           FROM npcs ORDER BY id""",
    ),
    (
        "item",
        """SELECT CAST(id AS TEXT) AS ref_id,
                  category || ' ' || name || ' ' || COALESCE(quality,'')
                       || ' ' || COALESCE(status,'') AS text
           FROM items ORDER BY id""",
    ),
    (
        "region",
        """SELECT CAST(id AS TEXT) AS ref_id,
                  name || ' ' || COALESCE(security,'') || ' ' || COALESCE(danger,'')
                       || ' ' || COALESCE(entry_requirement,'') || ' ' || COALESCE(landmarks,'') AS text
           FROM regions ORDER BY id""",
    ),
    (
        "relationship",
        """SELECT CAST(id AS TEXT) AS ref_id,
                  character_a || ' ' || character_b || ' ' || COALESCE(relation_type,'')
                       || ' ' || COALESCE(description,'') AS text
           FROM relationships ORDER BY id""",
    ),
    (
        "intel",
        """SELECT CAST(id AS TEXT) AS ref_id, content AS text
           FROM important_intel ORDER BY id""",
    ),
    (
        "perm_memory",
        """SELECT CAST(id AS TEXT) AS ref_id,
                  COALESCE(datetime_range,'') || ' ' || COALESCE(scene,'')
                       || ' ' || COALESCE(summary,'') AS text
           FROM perm_memories ORDER BY id""",
    ),
    (
        "plot_summary",
        """SELECT '' AS ref_id, content AS text FROM plot_summary WHERE id=1""",
    ),
]


# 每个来源依赖的业务表——缺表时跳过而不是抛异常（半迁移/空库也能安全重建）
_SOURCE_TABLES: dict[str, str] = {
    "narrative": "session_history",
    "npc": "npcs",
    "item": "items",
    "region": "regions",
    "relationship": "relationships",
    "intel": "important_intel",
    "perm_memory": "perm_memories",
    "plot_summary": "plot_summary",
}


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")}


def _narrative_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """读 session_history 供重建索引用。

    volume/chapter/pov 是 P0 迁移后才有列，这里按实际列动态拼 SQL——
    否则半迁移库会在 "no such column: volume" 上直接炸掉。
    """
    cols = _columns(conn, "session_history")
    meta = [c for c in ("volume", "chapter", "pov") if c in cols]
    select_meta = "".join(", " + c for c in meta)
    sql = (
        "SELECT CAST(id AS TEXT) AS ref_id, narrative AS text"
        + select_meta
        + " FROM session_history ORDER BY id"
    )
    return conn.execute(sql).fetchall()


def rebuild_all(conn: sqlite3.Connection) -> dict[str, int]:
    """从各业务表全量重建检索索引，返回每个来源写入的文档数。

    业务表缺失时该来源计 0 并跳过（不抛异常），便于空库/半迁移库调用。
    """
    ensure_schema(conn)
    conn.execute("DELETE FROM search_fts")
    conn.execute("DELETE FROM search_docs")

    counts: dict[str, int] = {}

    # narrative 单独处理：列可能不齐（旧库），且需要带上卷/章/POV 元数据
    n = 0
    if _table_exists(conn, "session_history"):
        cols = _columns(conn, "session_history")
        for row in _narrative_rows(conn):
            text = (row["text"] or "").strip()
            if not text:
                continue
            kwargs = {c: row[c] for c in ("volume", "chapter", "pov") if c in cols}
            upsert_doc(conn, source="narrative", ref_id=row["ref_id"], text=text, **kwargs)
            n += 1
    counts["narrative"] = n

    for source, sql in _SOURCE_QUERIES:
        if not _table_exists(conn, _SOURCE_TABLES[source]):
            counts[source] = 0
            continue
        n = 0
        for row in conn.execute(sql).fetchall():
            text = (row["text"] or "").strip()
            if not text:
                continue
            upsert_doc(conn, source=source, ref_id=row["ref_id"], text=text)
            n += 1
        counts[source] = n

    # 实体别名（P1 建表后可用）：把别名并入对应实体文档，便于精确子串匹配
    if _table_exists(conn, "entity_aliases"):
        for row in conn.execute(
            "SELECT entity_type, entity_id, alias FROM entity_aliases ORDER BY id"
        ).fetchall():
            existing = conn.execute(
                "SELECT id, text FROM search_docs WHERE source=? AND ref_id=?",
                (row["entity_type"], str(row["entity_id"])),
            ).fetchone()
            if existing:
                upsert_doc(
                    conn,
                    source=row["entity_type"],
                    ref_id=str(row["entity_id"]),
                    text=existing["text"] + " " + row["alias"],
                )

    if not conn.in_transaction:
        conn.commit()
    return counts
