"""数据库迁移——向后兼容、幂等、可重复执行。

P0-3：为 session_history 增加卷/章/POV/内容哈希列，并建立内容哈希唯一索引。
所有操作都先检查现状，重复执行不报错、不丢数据（不重建表）。
"""

from __future__ import annotations

import sqlite3

from . import search
from .schema import BASE_SCHEMA_SQL, P1_SCHEMA_SQL

# 需要新增的列：列名 → 列定义
_SESSION_HISTORY_COLUMNS: dict[str, str] = {
    "volume": "INTEGER",
    "chapter": "INTEGER",
    "pov": "TEXT",
    "content_hash": "TEXT",
}

# P1：故事状态增加当前卷与当前 POV
_STORY_STATE_COLUMNS: dict[str, str] = {
    "current_volume": "INTEGER",
    "current_pov": "TEXT",
}

# P2：标记该轮是否已经做过结构化抽取。
# 没有这个标记，extract 重跑会把"相对增量"重复应用一次（永久性状态漂移）。
_SESSION_EXTRACT_COLUMNS: dict[str, str] = {
    "extracted_at": "TEXT",
}

# 唯一索引：content_hash 非空时唯一（幂等去重的数据库级保证）
_SESSION_HASH_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_session_hash
ON session_history(content_hash) WHERE content_hash IS NOT NULL
"""


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")}


def _add_missing_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> list[str]:
    """按 PRAGMA table_info 检查后 ALTER TABLE ADD COLUMN，返回实际新增的列名。"""
    if not _table_exists(conn, table):
        return []
    existing = _columns(conn, table)
    added = []
    for name, decl in columns.items():
        if name in existing:
            continue
        # 注：SQLite 的 ADD COLUMN 不支持 IF NOT EXISTS，故必须先 PRAGMA 检查
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
        added.append(name)
    return added


def migrate(conn: sqlite3.Connection) -> dict:
    """执行全部迁移，返回本次实际发生的变更摘要。

    幂等：对同一数据库连续调用两次，第二次返回的 added_columns 为空、
    added_indexes 为空，且不抛异常。
    """
    report: dict = {
        "added_columns": [],
        "added_indexes": [],
        "dropped_indexes": [],
        "core_schema": False,
        "p1_schema": False,
        "search_schema": False,
    }

    # 0) 核心表兜底：migrate 可能被单独调用在只有 P1/检索表的库或空库上。
    #    SCHEMA_SQL 全是 CREATE ... IF NOT EXISTS，对已有结构是 no-op；
    #    不执行 INIT_SQL（不塞默认数据），迁移只负责结构。
    if not _table_exists(conn, "session_history") or not _table_exists(conn, "story_state"):
        report["core_schema"] = True
        # 只补 P0 基础表：P1 表由下面第 3 步单独负责，这样 report["p1_schema"] 才如实
        conn.executescript(BASE_SCHEMA_SQL)

    # 1) session_history 增列（向后兼容旧库）
    added = _add_missing_columns(conn, "session_history", _SESSION_HISTORY_COLUMNS)
    if added:
        report["added_columns"] = added

    # 1b) P1：story_state 增列
    added_state = _add_missing_columns(conn, "story_state", _STORY_STATE_COLUMNS)
    if added_state:
        report["added_columns"] = report["added_columns"] + added_state

    # 1c) P2：session_history 增加抽取标记
    added_extract = _add_missing_columns(conn, "session_history", _SESSION_EXTRACT_COLUMNS)
    if added_extract:
        report["added_columns"] = report["added_columns"] + added_extract
        # 已经抽过的旧数据无法区分，一律视为"未抽取"由调用方决定是否补
    if _table_exists(conn, "session_history") and "extracted_at" in _columns(conn, "session_history"):
        before = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_session_extracted'"
        ).fetchone()
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_session_extracted ON session_history(extracted_at)"
        )
        if before is None:
            report["added_indexes"].append("idx_session_extracted")

    # 2) 内容哈希唯一索引
    if _table_exists(conn, "session_history") and "content_hash" in _columns(conn, "session_history"):
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_session_hash'"
        ).fetchone()
        # 不要用 row["sql"]：调用方可能没设 row_factory=Row（裸 sqlite3.connect）
        existing_sql = row[0] if row is not None else None
        if existing_sql is not None and "UNIQUE" not in (existing_sql or "").upper():
            # 同名索引已存在但不是 UNIQUE：CREATE UNIQUE INDEX IF NOT EXISTS 会静默跳过，
            # 唯一约束就永远建不上。删掉重建，保证幂等语义名副其实。
            conn.execute("DROP INDEX idx_session_hash")
            report["dropped_indexes"].append("idx_session_hash")
            existing_sql = None
        conn.execute(_SESSION_HASH_INDEX)
        if existing_sql is None:
            report["added_indexes"].append("idx_session_hash")

    # 3) P1 结构层级 / 伏笔 / 实体别名（migrate 被单独调用时也要建出来）
    if not _table_exists(conn, "volumes"):
        report["p1_schema"] = True
    conn.executescript(P1_SCHEMA_SQL)

    # 4) 检索层表结构
    if not _table_exists(conn, "search_docs"):
        report["search_schema"] = True
    search.ensure_schema(conn)

    if not conn.in_transaction:
        conn.commit()
    return report
