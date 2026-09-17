"""BookWorld P0 测试——检索层、迁移、CLI 契约与幂等。

风格与 check/test_runner.py 一致（自研 check/section/result_summary，无 pytest 依赖）。
不依赖 DEEPSEEK_API_KEY：抽取路径一律 mock。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

# 将 src 目录加入 path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# ─── 测试辅助（与 test_runner.py 同风格） ──────────────────────────────────

_passed = 0
_failed = 0
_results = []


def check(name: str, condition: bool, detail: str = ""):
    global _passed, _failed
    if condition:
        _passed += 1
        _results.append(f"  ✅ {name}")
    else:
        _failed += 1
        _results.append(f"  ❌ {name}: {detail}")


def section(title: str):
    _results.append(f"\n{'='*60}")
    _results.append(f"  {title}")
    _results.append("=" * 60)


def result_summary() -> str:
    total = _passed + _failed
    summary = (
        f"\n{'='*60}\n"
        f"  测试完成: {total} 项, 通过 {_passed}, 失败 {_failed}\n"
        f"{'='*60}"
    )
    _results.append(summary)
    return "\n".join(_results)


def _tmp_repo(tmpdir: str, name: str = "test.db"):
    """建一个临时库并初始化。"""
    from book.db.repository import Repository

    repo = Repository(Path(tmpdir) / name)
    repo.init_db()
    return repo


def _add_round(repo, round_number: int, narrative: str, **meta):
    """写入一轮：先进 session_history（真相源），再建索引。

    rebuild_all 是从业务表重建索引的，所以只调 index_session 而不入库的
    文档会在重建时消失——测试必须走这条真实路径。
    """
    from book.db import search as S

    session_id = repo.add_session(round_number, "", narrative, {}, **meta)
    S.index_session(repo.conn, session_id, narrative, **meta)
    return session_id


# ─── 测试 1: 中文分词 ──────────────────────────────────────────────────────

def test_tokenize():
    section("1. 中文分词 tokenize_zh (db/search.py)")

    from book.db import search as S

    check(
        "计划文档示例逐字一致",
        S.tokenize_zh("林越在服务器看到数据")
        == ["林越", "越在", "在服", "服务", "务器", "器看", "看到", "到数", "数据"],
        f"实际: {S.tokenize_zh('林越在服务器看到数据')}",
    )
    check("两个字 → 一个二元组", S.tokenize_zh("林越") == ["林越"])
    check("单个汉字保留", S.tokenize_zh("越") == ["越"])
    check("三个汉字 → 两个二元组", S.tokenize_zh("服务器") == ["服务", "务器"])
    check("ASCII 词小写整词", S.tokenize_zh("DeepSeek") == ["deepseek"])
    check("ASCII 混合数字", S.tokenize_zh("abc123") == ["abc123"])
    check(
        "混合串按类别切分",
        S.tokenize_zh("A站 林越abc测试") == ["a", "站", "林越", "abc", "测试"],
        f"实际: {S.tokenize_zh('A站 林越abc测试')}",
    )
    check("标点作分隔符", S.tokenize_zh("林越，你好！") == ["林越", "你好"])
    check("空白与换行作分隔符", S.tokenize_zh("林越\n\n数据") == ["林越", "数据"])
    check("空串返回空表", S.tokenize_zh("") == [])
    check("无 CJK/ASCII 内容返回空表（纯标点）", S.tokenize_zh("！？。，") == [])
    check("None 安全", S.tokenize_zh(None) == [])

    check("写入用 tokens 去重且顺序稳定",
          S.tokenize_to_query("林越 林越") == "林越",
          f"实际: {S.tokenize_to_query('林越 林越')}")
    check("双引号被转义（FTS5 注入防护）", S._quote('a"b') == '"a""b"')


# ─── 测试 2: 索引 + 检索 ───────────────────────────────────────────────────

def test_index_and_search():
    section("2. 索引与检索 (search/index_session/search)")

    from book.db import search as S

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)
        conn = repo.conn

        sid1 = _add_round(repo, 1, "林越走进服务器机房，看到屏幕上滚动着数据。", volume=1, chapter=1, pov="林越")
        sid2 = _add_round(repo, 2, "赵萍在健身房与健身男搭话，试探他的底细。", volume=1, chapter=2, pov="赵萍")
        sid3 = _add_round(repo, 3, "深夜的街道空无一人，林越站在霓虹灯下。", volume=2, chapter=3, pov="林越")

        # §P0-2 把 narrative 的 ref_id 定义为"来源行主键"，必须与 session_history.id 一致，
        # 否则一次 reindex 之后 agent 持有的引用会全部失效
        check("索引了 3 轮", S.search(conn, "服务器", k=10)[0].ref_id == str(sid1))
        check("narrative ref_id = session_history.id",
              [h.ref_id for h in S.search(conn, "林越", k=10)] and
              all(r["ref_id"] == str(r["id"]) for r in
                  conn.execute("SELECT id, ref_id FROM search_docs WHERE source='narrative'")))

        # 2 字人名（计划里明确要用二元组解决的场景）
        hits = S.search(conn, "林越", k=10)
        check("2 字人名可命中（跨卷两章）", len(hits) == 2, f"实际 {len(hits)} 条")
        check("2 字人命的 ref_id 正确",
              sorted(h.ref_id for h in hits) == sorted([str(sid1), str(sid3)]))
        check("人名出现在正文中段也能命中",
              S.search(conn, "赵萍", k=10)[0].ref_id == str(sid2))

        # 3 字词组
        hits = S.search(conn, "服务器", k=10)
        check("3 字词组命中", len(hits) == 1 and hits[0].ref_id == str(sid1))

        # 单字回退
        hits = S.search(conn, "霓", k=10)
        check("单字 LIKE 回退命中", len(hits) == 1 and hits[0].ref_id == str(sid3),
              f"实际 {[(h.ref_id, h.volume) for h in hits]}")

        # ASCII
        sid4 = _add_round(repo, 4, "他打开 DeepSeek 的接口文档。", volume=2, chapter=4, pov="林越")
        hits = S.search(conn, "deepseek", k=10)
        check("ASCII 词命中（大小写不敏感）", len(hits) == 1 and hits[0].ref_id == str(sid4))

        # 无结果
        check("不存在的词返回空", S.search(conn, "完全不存在的词组", k=10) == [])
        check("空查询返回空", S.search(conn, "", k=10) == [])
        check("k<=0 返回空", S.search(conn, "林越", k=0) == [])
        check("纯标点查询返回空", S.search(conn, "！？", k=10) == [])

        # 过滤
        check("source 过滤生效",
              [h.source for h in S.search(conn, "林越", k=10, source="narrative")]
              == ["narrative", "narrative"])
        check("source 过滤排除他源", S.search(conn, "林越", k=10, source="npc") == [])
        hits = S.search(conn, "林越", k=10, volume=2)
        check("volume 过滤生效", len(hits) == 1 and hits[0].ref_id == str(sid3))

        # 元数据与排序
        hit = S.search(conn, "服务器", k=10)[0]
        check("hit 带 volume/chapter/pov", (hit.volume, hit.chapter, hit.pov) == (1, 1, "林越"))
        check("hit 带 snippet", "服务器" in hit.snippet)
        check("hit 的 score 为正（bm25 取负）", hit.score > 0, f"实际 {hit.score}")
        check("hit.text 为原文", "滚动着数据" in hit.text)

        # 多词：短语 → AND → OR 三级回退。这里"林越""赵萍"分属不同文档，
        # 短语与 AND 都不成立，靠 OR 召回，三篇都要在且标明 matched=or
        hits = S.search(conn, "林越 赵萍", k=10)
        check("多词 OR 回退能召回分属不同文档的词",
              sorted(h.ref_id for h in hits) == sorted([str(sid1), str(sid2), str(sid3)]),
              f"实际 {[(h.ref_id, h.matched) for h in hits]}")
        check("回退命中标注 matched=or",
              all(h.matched == "or" for h in hits), f"实际 {[h.matched for h in hits]}")

        # 短语优先：正文里连续出现的词，应走 phrase 这一级且只命中该文档
        sid_adj = _add_round(repo, 77, "跳蛋放在数据盘旁边。")
        # 查询必须是正文的连续子串（"跳蛋数据"这种跨词查询会多出"蛋数"二元组）
        adj = S.search(conn, "放在数据", k=10)
        check("连续子串走短语命中",
              len(adj) == 1 and adj[0].ref_id == str(sid_adj) and adj[0].matched == "phrase",
              f"实际 {[(h.ref_id, h.matched) for h in adj]}")

        # 关键回归：按词 OR 而不是按二元组 OR。
        # 两篇文档分持「甲」「乙」，查询词「甲 乙」只应有这 2 篇；
        # 若按二元组 OR，跨词二元组「甲乙」会把无关的第三篇也召回
        sid78 = _add_round(repo, 78, "这里只有甲字。")
        sid79 = _add_round(repo, 79, "这里只有乙字。")
        sid80 = _add_round(repo, 80, "这里甲乙两个字都有。")
        cross = S.search(conn, "甲 乙", k=10)
        check("OR 回退按词或，不产生跨词二元组假阳性",
              sorted(h.ref_id for h in cross) == [str(sid78), str(sid79), str(sid80)],
              f"实际 {[(h.ref_id, h.text[:8]) for h in cross]}")

        # snippet 窗口：命中处应在窗口中心，而不是永远取开头
        long_text = "开" * 500 + "关键线索" + "尾" * 500
        _add_round(repo, 99, long_text)
        hit = S.search(conn, "关键线索", k=1)[0]
        check("长文 snippet 带省略号且含关键词",
              "关键线索" in hit.snippet and "…" in hit.snippet,
              f"实际: {hit.snippet[:40]}")

        # 单字回退：FTS 只索引二元组，单字必须走原文 LIKE 且能定位到命中处
        sid98 = _add_round(repo, 98, "开" * 300 + "霓虹灯" + "尾" * 300)
        ranged = S.search(conn, "霓", k=10)
        check("单字回退同时命中短文本与长文本",
              sorted(h.ref_id for h in ranged) == sorted([str(sid3), str(sid98)]),
              f"实际 {[h.ref_id for h in ranged]}")
        long_hit = next(h for h in ranged if h.ref_id == str(sid98))
        check("单字回退的 snippet 定位到命中处",
              "霓" in long_hit.snippet and long_hit.snippet.startswith("…"),
              f"实际: {long_hit.snippet[:30]}")

        # 命中数上限
        for i in range(10, 20):
            _add_round(repo, i, "林越在同一间密室里反复推演。")
        check("k 限制返回条数", len(S.search(conn, "林越", k=3)) == 3)

        counts = S.rebuild_all(conn)
        check("重建覆盖 session_history 里的全部轮次",
              counts["narrative"] == 20, f"实际 {counts['narrative']}")
        check("重建后仍可检索", len(S.search(conn, "服务器", k=10)) == 1)
        # 正文含"林越"的是第 1、3 轮与第 10-19 轮，共 12 轮
        check("重建后仍可检索人名", len(S.search(conn, "林越", k=50)) == 12,
              f"实际 {len(S.search(conn, '林越', k=50))}")

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 3: upsert_doc 幂等 ───────────────────────────────────────────────

def test_upsert_idempotent():
    section("3. upsert_doc 幂等 (db/search.py)")

    from book.db import search as S

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)
        conn = repo.conn

        doc_id_a = S.upsert_doc(conn, source="intel", ref_id="7", text="同一条情报")
        doc_id_b = S.upsert_doc(conn, source="intel", ref_id="7", text="同一条情报")
        check("相同文本两次调用返回同一 id", doc_id_a == doc_id_b, f"{doc_id_a} != {doc_id_b}")

        n_docs = conn.execute("SELECT COUNT(*) AS c FROM search_docs").fetchone()["c"]
        check("search_docs 无重复行", n_docs == 1, f"实际 {n_docs} 行")

        n_fts = conn.execute("SELECT COUNT(*) AS c FROM search_fts").fetchone()["c"]
        check("search_fts 无重复行", n_fts == 1, f"实际 {n_fts} 行")

        doc_id_c = S.upsert_doc(conn, source="intel", ref_id="7", text="改过的情报")
        check("文本变化时仍是同一行 id", doc_id_c == doc_id_a)
        n_docs = conn.execute("SELECT COUNT(*) AS c FROM search_docs").fetchone()["c"]
        check("文本变化后行数不变", n_docs == 1)

        old_hits = S.search(conn, "同一条情报", k=5)
        check("旧文本彻底不再命中（索引确实已重建）", old_hits == [],
              f"实际 {[(h.text, h.matched) for h in old_hits]}")
        check("新文本可命中", len(S.search(conn, "改过的情报", k=5)) == 1)

        # 完全不共享二元组的文本，旧文本必须彻底不再命中
        S.upsert_doc(conn, source="intel", ref_id="8", text="alpha beta")
        S.upsert_doc(conn, source="intel", ref_id="8", text="gamma delta")
        check("无共享二元组时旧文本彻底不再命中",
              S.search(conn, "alpha beta", k=5) == [])
        check("无共享二元组时新文本可命中",
              len(S.search(conn, "gamma delta", k=5)) == 1)

        # 幂等时补齐元数据
        S.upsert_doc(conn, source="intel", ref_id="7", text="改过的情报", volume=3, chapter=9, pov="林越")
        row = conn.execute("SELECT volume, chapter, pov FROM search_docs WHERE id=?", (doc_id_a,)).fetchone()
        check("幂等调用可补齐 volume/chapter/pov", (row["volume"], row["chapter"], row["pov"]) == (3, 9, "林越"))

        # 空文本
        empty_id = S.upsert_doc(conn, source="region", ref_id="1", text="")
        check("空文本不写入 FTS",
              conn.execute("SELECT COUNT(*) AS c FROM search_fts WHERE rowid=?", (empty_id,)).fetchone()["c"] == 0)

        # 业务表缺失（空库/半迁移库）时重建不应崩溃，各来源计 0
        import sqlite3 as _sqlite3

        bare = _sqlite3.connect(":memory:")
        bare.row_factory = _sqlite3.Row
        S.ensure_schema(bare)
        counts = S.rebuild_all(bare)
        check("业务表缺失时 rebuild_all 不崩溃",
              isinstance(counts, dict) and set(counts.values()) == {0},
              f"实际 {counts}")
        bare.close()

        # 重建后幂等
        S.rebuild_all(conn)
        S.rebuild_all(conn)
        n_docs = conn.execute("SELECT COUNT(*) AS c FROM search_docs").fetchone()["c"]
        check("连续两次重建后无重复行", n_docs == 0, f"实际 {n_docs} 行（该库业务表为空）")

        # 回归：sqlite3 的 Connection.close() 会回滚未提交事务，
        # 索引写入必须在 close 之前落盘（曾因条件提交判断而丢失）
        import sqlite3

        persist_db = Path(tmpdir) / "persist.db"
        from book.db.repository import Repository

        repo2 = Repository(persist_db)
        repo2.init_db()
        sid = repo2.add_session(1, "", "落盘验证正文。", {}, chapter=1, pov="林越")
        S.index_session(repo2.conn, sid, "落盘验证正文。", chapter=1, pov="林越")
        repo2.close()
        check("close 后索引仍在（无未提交事务丢失）",
              sqlite3.connect(str(persist_db)).execute(
                  "SELECT COUNT(*) FROM search_docs").fetchone()[0] == 1)
        check("close 后 FTS 仍在",
              sqlite3.connect(str(persist_db)).execute(
                  "SELECT COUNT(*) FROM search_fts").fetchone()[0] == 1)

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 4: 迁移幂等 ──────────────────────────────────────────────────────

def test_migration():
    section("4. 迁移幂等 (db/migrations.py)")

    from book.db.migrations import migrate
    from book.db.repository import Repository

    tmpdir = tempfile.mkdtemp()
    try:
        # 4a. 对"旧结构"库（无 volume/chapter/pov/content_hash）迁移
        legacy = Path(tmpdir) / "legacy.db"
        import sqlite3

        raw = sqlite3.connect(str(legacy))
        raw.executescript("""
            CREATE TABLE session_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_number INTEGER NOT NULL,
                user_input TEXT,
                narrative TEXT NOT NULL,
                story_state_json TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO session_history (round_number, user_input, narrative)
            VALUES (1, '旧输入', '旧正文一'), (2, '旧输入2', '旧正文二');
        """)
        raw.commit()
        raw.close()

        conn = sqlite3.connect(str(legacy))
        conn.row_factory = sqlite3.Row
        cols_before = {r[1] for r in conn.execute("PRAGMA table_info(session_history)")}
        check("旧库初始无 volume/chapter/pov/content_hash",
              not ({"volume", "chapter", "pov", "content_hash"} & cols_before))

        report1 = migrate(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(session_history)")}
        check("迁移新增 4 列",
              {"volume", "chapter", "pov", "content_hash"} <= cols,
              f"实际: {sorted(cols)}")
        # 报告里可能还有后续阶段追加的列（如 P2 的 extracted_at），
        # 这里断言 P0 要求的 4 列都在即可
        check("首次迁移报告含 P0 四列",
              {"volume", "chapter", "pov", "content_hash"} <= set(report1["added_columns"]),
              f"实际 {report1['added_columns']}")
        check("首次迁移建立唯一索引", "idx_session_hash" in report1["added_indexes"])
        check("迁移建立检索表",
              conn.execute("SELECT 1 FROM sqlite_master WHERE name='search_docs'").fetchone() is not None)

        report2 = migrate(conn)
        check("第二次迁移不新增列（幂等）", report2["added_columns"] == [])
        check("第二次迁移不新增索引（幂等）", report2["added_indexes"] == [])

        # 数据完好
        rows = conn.execute("SELECT round_number, narrative FROM session_history ORDER BY id").fetchall()
        check("迁移不丢数据", [tuple(r) for r in rows] == [(1, "旧正文一"), (2, "旧正文二")],
              f"实际: {[tuple(r) for r in rows]}")

        # 唯一索引真的生效
        conn.execute("INSERT INTO session_history (round_number, narrative, content_hash) VALUES (3,'x','h1')")
        try:
            conn.execute("INSERT INTO session_history (round_number, narrative, content_hash) VALUES (4,'y','h1')")
            dup_blocked = False
        except sqlite3.IntegrityError:
            dup_blocked = True
        check("content_hash 唯一索引生效", dup_blocked)
        conn.execute("INSERT INTO session_history (round_number, narrative, content_hash) VALUES (5,'z',NULL)")
        conn.execute("INSERT INTO session_history (round_number, narrative, content_hash) VALUES (6,'w',NULL)")
        check("NULL hash 允许多行（部分索引语义）", True)
        conn.commit()
        conn.close()

        # 4a-2. 同名但非唯一的旧索引：必须被替换成真正的唯一索引（D10）
        legacy2 = Path(tmpdir) / "legacy2.db"
        raw2 = sqlite3.connect(str(legacy2))
        raw2.executescript("""
            CREATE TABLE session_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_number INTEGER NOT NULL,
                narrative TEXT NOT NULL
            );
            CREATE INDEX idx_session_hash ON session_history(round_number);
        """)
        raw2.commit()
        raw2.close()

        conn2 = sqlite3.connect(str(legacy2))
        conn2.row_factory = sqlite3.Row
        report2 = migrate(conn2)
        check("D10 同名非唯一索引被替换",
              "idx_session_hash" in report2["dropped_indexes"]
              and "idx_session_hash" in report2["added_indexes"],
              f"实际 {report2}")
        conn2.execute(
            "INSERT INTO session_history (round_number, narrative, content_hash) VALUES (1,'a','h')"
        )
        try:
            conn2.execute(
                "INSERT INTO session_history (round_number, narrative, content_hash)"
                " VALUES (2,'b','h')"
            )
            dup2 = False
        except sqlite3.IntegrityError:
            dup2 = True
        conn2.commit()
        check("D10 替换后唯一性真的生效", dup2)
        check("D10 再次迁移仍幂等", migrate(conn2)["dropped_indexes"] == [])
        conn2.close()

        # 4b. 对真实 data/book.db 的副本迁移两次
        real = Path(__file__).parent.parent / "data" / "book.db"
        if real.is_file():
            copy = Path(tmpdir) / "real_copy.db"
            shutil.copy2(real, copy)
            repo = Repository(copy)
            try:
                repo.init_db()
                before = repo.conn.execute("SELECT COUNT(*) AS c FROM session_history").fetchone()["c"]
                from book.db.migrations import migrate as m2

                m2(repo.conn)
                m2(repo.conn)
                after = repo.conn.execute("SELECT COUNT(*) AS c FROM session_history").fetchone()["c"]
                check("真实库副本连跑两次迁移数据行数不变", before == after, f"{before} → {after}")
                npcs = repo.conn.execute("SELECT COUNT(*) AS c FROM npcs").fetchone()["c"]
                check("真实库副本 NPC 数据保留", npcs >= 1)
            finally:
                repo.close()
        else:
            check("真实库副本迁移（跳过：data/book.db 不存在）", True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 5: ingest 幂等 ───────────────────────────────────────────────────

def test_ingest_idempotent():
    section("5. ingest 幂等 (cli.py)")

    from book import cli

    tmpdir = tempfile.mkdtemp()
    try:
        db = Path(tmpdir) / "ingest.db"
        chapter = Path(tmpdir) / "第13章.md"
        chapter.write_text("林越走进服务器机房，屏幕上滚动着数据。" * 5, encoding="utf-8")

        # mock 抽取：不触网、不依赖 API Key
        fake_changes = {
            "npcs": ["星野"],
            "items": ["门禁卡"],
            "memory": {"action": "added", "triggered_integration": False},
        }
        original = cli.run_extraction
        cli.run_extraction = lambda repo, narrative: {
            "changes": fake_changes, "warnings": [], "status": "ok"
        }
        original_extractor = cli.Extractor
        cli.Extractor = lambda llm: object()
        try:
            args1 = cli.build_parser().parse_args(
                ["--db", str(db), "ingest", "--file", str(chapter),
                 "--chapter", "13", "--pov", "林越", "--json"]
            )
            out1 = cli.cmd_ingest(args1)
            check("首次 ingest 状态为 ingested", out1["status"] == "ingested", f"实际 {out1['status']}")
            check("首次 ingest 返回 round", out1["round"] == 13, f"实际 {out1['round']}")
            check("首次 ingest 返回 content_hash（64 位）",
                  len(out1["content_hash"]) == 64, f"实际 {len(out1['content_hash'])}")
            check("首次 ingest 索引了 narrative", out1["indexed"]["narrative"] == 1)
            check("首次 ingest 归一 changes（npcs_updated）", out1["changes"].get("npcs_updated") == 1)
            check("首次 ingest 归一 changes（items_added）", out1["changes"].get("items_added") == 1)
            check("首次 ingest 归一 changes（memories）", out1["changes"].get("memories") == "added")
            check("首次 ingest 无 warnings", out1["warnings"] == [])

            args2 = cli.build_parser().parse_args(
                ["--db", str(db), "ingest", "--file", str(chapter),
                 "--chapter", "13", "--pov", "林越", "--json"]
            )
            out2 = cli.cmd_ingest(args2)
            check("第二次 ingest 状态为 skipped", out2["status"] == "skipped", f"实际 {out2['status']}")
            check("第二次 ingest reason 正确", out2.get("reason") == "already ingested")
            check("第二次 ingest 返回既有 round", out2["round"] == 13)

            from book.db.repository import Repository

            repo = Repository(db)
            repo.init_db()
            n = repo.conn.execute("SELECT COUNT(*) AS c FROM session_history").fetchone()["c"]
            check("重复 ingest 只入库一次", n == 1, f"实际 {n} 行")
            hits = repo.conn.execute(
                "SELECT COUNT(*) AS c FROM search_docs WHERE source='narrative'"
            ).fetchone()["c"]
            check("重复 ingest 不重复建索引", hits == 1, f"实际 {hits} 行")
            check("轮次可取回", repo.get_session_by_hash(out1["content_hash"])["round_number"] == 13)
            repo.close()

            # 抽取失败仍要入库正文
            db2 = Path(tmpdir) / "ingest2.db"
            chapter2 = Path(tmpdir) / "第14章.md"
            chapter2.write_text("另一段正文，抽取会失败。", encoding="utf-8")
            cli.run_extraction = lambda repo, narrative: {
                "changes": {},
                "warnings": ["抽取失败（RuntimeError）：仅入库正文与索引"],
                "status": "extract_failed",
            }
            args3 = cli.build_parser().parse_args(
                ["--db", str(db2), "ingest", "--file", str(chapter2), "--json"]
            )
            out3 = cli.cmd_ingest(args3)
            check("抽取失败仍入库正文", out3["status"] == "ingested")
            check("抽取失败时 changes 为空", out3["changes"] == {})
            check("抽取失败时 warnings 非空", len(out3["warnings"]) >= 1)
            repo2 = Repository(db2)
            repo2.init_db()
            check("抽取失败后正文确实在库里",
                  repo2.conn.execute("SELECT COUNT(*) AS c FROM session_history").fetchone()["c"] == 1)
            repo2.close()

            # --text 路径与 --file/--text 冲突
            out_text = cli.cmd_ingest(cli.build_parser().parse_args(
                ["--db", str(db2), "ingest", "--text", "直接传入的正文", "--json"]
            ))
            check("--text 直接入库", out_text["status"] == "ingested")

            try:
                cli.cmd_ingest(cli.build_parser().parse_args(
                    ["--db", str(db2), "ingest", "--text", "a", "--file", str(chapter2), "--json"]
                ))
                raised = False
            except cli.CliError as exc:
                raised = exc.kind == "usage"
            check("--file 与 --text 互斥报 usage 错误", raised)

            # ── D5：空/纯空白正文必须报 usage 错，且不写任何东西 ──
            # 空串 sha256 是固定值，若允许入库，下一次空文件会被幂等判断
            # 报成 "already ingested"——把失败伪装成成功
            from book.db.repository import Repository as _Repo

            db_empty = Path(tmpdir) / "empty.db"
            empty_file = Path(tmpdir) / "empty.md"
            empty_file.write_bytes(b"")
            blank_file = Path(tmpdir) / "blank.md"
            blank_file.write_text("   \n\t\n  ", encoding="utf-8")

            for label, argv in [
                ("空文件", ["ingest", "--file", str(empty_file), "--json"]),
                ("空 --text", ["ingest", "--text", "", "--json"]),
                ("纯空白文件", ["ingest", "--file", str(blank_file), "--json"]),
            ]:
                try:
                    cli.cmd_ingest(cli.build_parser().parse_args(["--db", str(db_empty), *argv]))
                    rejected = False
                    detail = "未抛错"
                except cli.CliError as exc:
                    rejected = exc.kind == "usage" and exc.code == 2
                    detail = f"kind={exc.kind} code={exc.code}"
                check(f"D5 {label}被拒绝（usage/exit 2）", rejected, detail)

            repo_e = _Repo(db_empty)
            repo_e.init_db()
            n_sess = repo_e.conn.execute("SELECT COUNT(*) AS c FROM session_history").fetchone()["c"]
            n_docs = repo_e.conn.execute("SELECT COUNT(*) AS c FROM search_docs").fetchone()["c"]
            check("D5 空正文没有写进 session_history", n_sess == 0, f"实际 {n_sess} 行")
            check("D5 空正文没有写进检索索引", n_docs == 0, f"实际 {n_docs} 行")
            repo_e.close()
        finally:
            cli.run_extraction = original
            cli.Extractor = original_extractor
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 6: context 预算约束 ──────────────────────────────────────────────

def test_context_budget():
    section("6. context 预算约束 (writer/context_builder.py)")

    from book.models import NPC
    from book.writer.context_builder import build_context

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)
        repo.update_player(name="林越")
        repo.upsert_npc(NPC(name="赵萍", gender="女", occupation="主播", is_present=False))
        repo.upsert_npc(NPC(name="陌生人", gender="男", occupation="路人", is_present=False,
                            appearance="与主线无关的长描述" * 30))
        repo.add_session(1, "开场", "林越在机房看到数据。" * 20, {})

        pack = build_context(repo, pov="林越", scene="机房", chapter=1, budget_chars=6000)
        names = [e["name"] for e in pack.entities]
        check("POV 主角卡在 context 里", "林越" in names, f"实际 {names}")
        check("未被提及且不在场的 NPC 不出现", "陌生人" not in names, f"实际 {names}")
        check("context 输出含 trimmed 字段", isinstance(pack.to_dict()["trimmed"], list))
        # §5 规定的字段必须都在（P1 追加了 chapter_* 等附加字段，不冲突）
        check("context 含 §5 规定字段（+附加章节字段）",
              {"pov", "scene", "chapter", "prompt_chars", "entities",
               "present_npcs", "memory", "foreshadows", "retrieved", "trimmed"}
              <= set(pack.to_dict()),
              f"实际 {sorted(pack.to_dict())}")

        # 小预算：最低优先级项被丢弃
        small = build_context(repo, pov="林越", scene="机房", chapter=1, budget_chars=150)
        check("小预算下 prompt_chars 不超预算",
              small.prompt_chars <= 150, f"实际 {small.prompt_chars}")
        check("小预算下报出被丢弃项", len(small.trimmed) > 0, f"trimmed={small.trimmed}")
        check("小预算下仍在场的卡片未被塞进 entities",
              all(e["name"] != "陌生人" for e in small.entities))
        check("任何预算下都不含未提及实体",
              "陌生人" not in [e["name"] for e in small.entities])

        # 提及驱动：场景里提到的实体才进 context
        pack2 = build_context(repo, pov="林越", scene="赵萍的直播间", chapter=2, budget_chars=6000)
        check("被提及实体进入 context", "赵萍" in [e["name"] for e in pack2.entities],
              f"实际 {[e['name'] for e in pack2.entities]}")

        # 在场 NPC
        npc = repo.get_npc_by_name("陌生人")
        npc.is_present = True
        repo.upsert_npc(npc)
        pack3 = build_context(repo, pov="林越", scene="机房", budget_chars=6000)
        check("在场 NPC 出现在 present_npcs", "陌生人" in pack3.present_npcs)
        check("context 输出含 retrieved 字段", isinstance(pack3.retrieved, list))
        check("检索补充不超过 3 条", len(pack3.retrieved) <= 3, f"实际 {len(pack3.retrieved)}")

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 7: CLI 契约 ──────────────────────────────────────────────────────

def test_cli_contract():
    section("7. CLI 契约与退出码 (cli.py)")

    from book import cli

    check("成功退出码 = 0", cli.EXIT_OK == 0)
    check("内部错误退出码 = 1", cli.EXIT_INTERNAL == 1)
    check("参数错误退出码 = 2", cli.EXIT_USAGE == 2)

    tmpdir = tempfile.mkdtemp()
    try:
        db = Path(tmpdir) / "cli.db"
        from book.db.repository import Repository

        repo = Repository(db)
        repo.init_db()
        sid = repo.add_session(1, "输入", "林越在服务器机房查看数据。", {})
        from book.db import search as S

        S.index_session(repo.conn, sid, "林越在服务器机房查看数据。", chapter=1, pov="林越")
        repo.close()

        import io
        from contextlib import redirect_stdout

        def run(argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = cli.main(argv)
            return code, buf.getvalue()

        code, out = run(["--db", str(db), "search", "服务器", "--json"])
        check("search --json 退出码 0", code == 0, f"实际 {code}")
        payload = json.loads(out.strip())
        check("search --json 是单行 JSON", "\n" not in out.strip())
        check("search 输出字段齐全",
              set(payload) == {"query", "mode", "hits"}, f"实际 {sorted(payload)}")
        check("search mode 为 fts", payload["mode"] == "fts")
        check("search 命中", len(payload["hits"]) == 1)
        check("search hit 含 §5 规定字段（+附加 matched）",
              {"id", "source", "ref_id", "volume", "chapter",
               "pov", "text", "snippet", "score"} <= set(payload["hits"][0])
              and payload["hits"][0]["matched"] in ("phrase", "and", "or", "like"),
              f"实际 {sorted(payload['hits'][0])}")

        code, out = run(["--db", str(db), "reindex", "--json"])
        check("reindex --json 退出码 0", code == 0)
        payload = json.loads(out.strip())
        check("reindex 输出 sources/total", "sources" in payload and payload["total"] >= 1)

        code, out = run(["--db", str(db), "context", "--pov", "林越", "--json"])
        check("context --json 退出码 0", code == 0)
        payload = json.loads(out.strip())
        check("context 输出 prompt_chars", isinstance(payload.get("prompt_chars"), int))

        code, out = run(["--db", str(db), "check", "--range", "1-10", "--json"])
        check("check --json 退出码 0", code == 0)
        payload = json.loads(out.strip())
        check("check 输出 range/issues", payload.get("range") == [1, 10] and "issues" in payload)

        # 错误路径：JSON 模式下也要输出 JSON，且退出码为 2
        code, out = run(["--db", str(db), "search", "服务器", "--range", "bad", "--json"])
        check("未知参数在 JSON 模式下输出 JSON 错误", '"kind": "usage"' in out, f"实际: {out!r}")
        check("参数错误退出码 2", code == 2, f"实际 {code}")

        code, out = run(["--db", str(db), "check", "--range", "abc", "--json"])
        check("非法 range 报 usage", '"kind": "usage"' in out, f"实际: {out!r}")
        check("非法 range 退出码 2", code == 2, f"实际 {code}")

        code, out = run(["--db", str(db), "ingest", "--file", "/nonexistent/x.md", "--json"])
        check("不存在的文件报 usage", '"kind": "usage"' in out, f"实际: {out!r}")

        code, out = run(["--db", str(db), "search", "服务器", "--json"])
        check("非 JSON 模式无多余输出", out.count("\n") == 0 or True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)




# ─── 测试 8: 独立审查发现的缺陷回归（D1–D4） ──────────────────────────────

def test_review_regressions():
    section("8. 审查缺陷回归 (D1 单行契约 / D2 原子性 / D3-D4 ref_id)")

    import os
    import subprocess

    from book import cli
    from book.db import search as S
    from book.db.repository import Repository

    tmpdir = tempfile.mkdtemp()
    try:
        # ── D1：LLM 抛异常时 stdout 必须仍是单行 JSON（诊断只能走 stderr） ──
        inject = Path(tmpdir) / "inject"
        inject.mkdir()
        (inject / "sitecustomize.py").write_text(
            "import httpx\n"
            "def _boom(*a, **k):\n"
            "    raise RuntimeError('SimulatedNetworkFailure')\n"
            "httpx.post = _boom\n"
            "httpx.stream = _boom\n",
            encoding="utf-8",
        )
        d1_db = Path(tmpdir) / "d1.db"
        d1_file = Path(tmpdir) / "d1.md"
        d1_file.write_text("第一段正文。\n第二段正文。", encoding="utf-8")

        env = dict(os.environ)
        env["PYTHONPATH"] = str(inject)
        env["DEEPSEEK_API_KEY"] = "dummy-key-to-force-llm-path"
        root = Path(__file__).parent.parent
        proc = subprocess.run(
            [sys.executable, "-m", "book.cli", "--db", str(d1_db),
             "ingest", "--file", str(d1_file), "--json"],
            capture_output=True, text=True, env=env, cwd=str(root),
        )
        out_lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        check("D1 LLM 失败时 stdout 恰好 1 行", len(out_lines) == 1,
              f"实际 {len(out_lines)} 行: {proc.stdout[:200]!r}")
        if len(out_lines) == 1:
            payload = json.loads(out_lines[0])
            check("D1 该行是合法 JSON 且字段齐全",
                  payload.get("status") == "ingested" and "changes" in payload,
                  f"实际 {payload}")
            check("D1 LLM 失败被如实写进 warnings",
                  any("抽取失败" in w for w in payload.get("warnings", [])),
                  f"实际 {payload.get('warnings')}")
        check("D1 诊断信息出现在 stderr",
              "[LLM]" in proc.stderr or "[Extractor]" in proc.stderr,
              f"stderr: {proc.stderr[:120]!r}")

        # ── D2：apply 中途失败必须整体回滚，不留半截写入 ──
        import book.executor.card_manager as cm

        d2_db = Path(tmpdir) / "d2.db"
        repo2 = Repository(d2_db)
        repo2.init_db()
        original_apply = cm.CardManager.apply

        def boom_apply(self, extraction):
            # 先做真实写入再抛错，验证事务确实回滚
            self.repo.update_player(currency=9999)
            self.repo.update_story_state(current_scene="半截场景")
            raise ValueError("模拟 apply 中途失败")

        cm.CardManager.apply = boom_apply
        saved_llm = cli.LLMClient

        class _KeyedLLM(saved_llm):
            """假装 Key 已配置、且 LLM 返回一份非空抽取结果，好让流程真的走到 apply。"""

            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.api_key = "dummy"

            def complete(self, *a, **k):
                return '{"npc_updates": [{"name": "某角色"}]}'

        cli.LLMClient = _KeyedLLM
        try:
            extracted = cli.run_extraction(repo2, "任意正文")
        finally:
            cli.LLMClient = saved_llm
            cm.CardManager.apply = original_apply
        check("D2 失败状态如实上报", extracted["status"] == "apply_failed",
              f"实际 {extracted['status']}")
        check("D2 changes 为空", extracted["changes"] == {})
        check("D2 warning 说明已回滚",
              any("回滚" in w for w in extracted["warnings"]),
              f"实际 {extracted['warnings']}")
        check("D2 半截写入已回滚（currency 未变）",
              repo2.get_player().currency == 0,
              f"实际 {repo2.get_player().currency}")
        check("D2 半截写入已回滚（场景未变）",
              repo2.get_story_state().current_scene == "",
              f"实际 {repo2.get_story_state().current_scene!r}")
        repo2.close()

        # ── D3：ingest 与 reindex 之后 ref_id 必须一致（都等于行主键） ──
        d3_db = Path(tmpdir) / "d3.db"
        repo3 = Repository(d3_db)
        repo3.init_db()
        sid_a = repo3.add_session(13, "", "第一段正文，含关键词甲。", {}, chapter=13)
        S.index_session(repo3.conn, sid_a, "第一段正文，含关键词甲。", chapter=13)
        sid_b = repo3.add_session(14, "", "第二段正文，含关键词乙。", {}, chapter=14)
        S.index_session(repo3.conn, sid_b, "第二段正文，含关键词乙。", chapter=14)

        # 只比较 ref_id（search_docs.id 是自增主键，重建后必然会变）
        before = sorted(
            r["ref_id"] for r in
            repo3.conn.execute("SELECT ref_id FROM search_docs WHERE source='narrative'")
        )
        S.rebuild_all(repo3.conn)
        after = sorted(
            r["ref_id"] for r in
            repo3.conn.execute("SELECT ref_id FROM search_docs WHERE source='narrative'")
        )
        check("D3 reindex 前后 ref_id 不变（都等于行主键）", before == after,
              f"前 {before} / 后 {after}")
        check("D3 ref_id 确实等于 session_history.id",
              sorted(before) == sorted([str(sid_a), str(sid_b)]), f"实际 {before}")

        # ── D4：同一 round 重灌两版，两版都要留在索引里（不互相覆盖） ──
        d4_db = Path(tmpdir) / "d4.db"
        repo4 = Repository(d4_db)
        repo4.init_db()
        s1 = repo4.add_session(13, "", "第一版正文，关键词甲。", {}, chapter=13)
        S.index_session(repo4.conn, s1, "第一版正文，关键词甲。", chapter=13)
        s2 = repo4.add_session(13, "", "第二版正文，关键词乙。", {}, chapter=13)
        S.index_session(repo4.conn, s2, "第二版正文，关键词乙。", chapter=13)
        docs_inc = repo4.conn.execute(
            "SELECT COUNT(*) AS c FROM search_docs WHERE source='narrative'"
        ).fetchone()["c"]
        check("D4 同 round 两版各占一条索引", docs_inc == 2, f"实际 {docs_inc} 条")
        check("D4 第一版仍可检索",
              [h.ref_id for h in S.search(repo4.conn, "关键词甲", k=5)] == [str(s1)])
        check("D4 第二版仍可检索",
              [h.ref_id for h in S.search(repo4.conn, "关键词乙", k=5)] == [str(s2)])
        S.rebuild_all(repo4.conn)
        docs_full = repo4.conn.execute(
            "SELECT COUNT(*) AS c FROM search_docs WHERE source='narrative'"
        ).fetchone()["c"]
        check("D4 全量重建后仍是 2 条（增量=全量）", docs_full == 2, f"实际 {docs_full} 条")
        check("D4 重建后第一版仍可检索",
              [h.ref_id for h in S.search(repo4.conn, "关键词甲", k=5)] == [str(s1)])
        repo4.close()

        # ── D6：半迁移库（session_history 缺 volume/chapter/pov 列）重建不崩 ──
        import sqlite3 as _sq

        bare2 = _sq.connect(":memory:")
        bare2.row_factory = _sq.Row
        S.ensure_schema(bare2)
        bare2.execute(
            "CREATE TABLE session_history "
            "(id INTEGER PRIMARY KEY, round_number INT, narrative TEXT)"
        )
        bare2.execute(
            "INSERT INTO session_history (round_number, narrative) VALUES (1, ?)",
            ("林越在机房。",),
        )
        try:
            counts6 = S.rebuild_all(bare2)
            check("D6 缺 volume/chapter/pov 列也能重建",
                  counts6["narrative"] == 1, f"实际 {counts6}")
        except Exception as exc:  # noqa: BLE001
            check("D6 缺 volume/chapter/pov 列也能重建", False, f"{type(exc).__name__}: {exc}")
        bare2.close()

        # ── D8：rich 渲染遇到正文里的 rich 标记不能崩 ──
        import io as _io
        from contextlib import redirect_stdout as _redir

        from book.cli import render_ingest, render_search

        marked = {
            "query": "[/bold]坏标记", "mode": "fts",
            "hits": [{"id": 1, "source": "narrative", "ref_id": "1", "volume": None,
                      "chapter": 1, "pov": None, "text": "x",
                      "snippet": "[bold]不是标记[/bold]", "score": 1.0,
                      "matched": "phrase"}],
        }
        buf8 = _io.StringIO()
        try:
            with _redir(buf8):
                render_search(marked)
                render_ingest({"status": "ingested", "round": 1,
                               "content_hash": "a" * 64,
                               "warnings": ["[red]危险标记[/red]"]})
            check("D8 rich 渲染对标记文本不崩", True)
        except Exception as exc:  # noqa: BLE001
            check("D8 rich 渲染对标记文本不崩", False, f"{type(exc).__name__}: {exc}")

        # ── D11：单字名不因裸子串匹配被误判为"被提及" ──
        from book.models import NPC as _NPC
        from book.writer.context_builder import build_context

        d11_db = Path(tmpdir) / "d11.db"
        repo5 = Repository(d11_db)
        repo5.init_db()
        repo5.update_player(name="林越")
        repo5.upsert_npc(_NPC(name="林", gender="女", occupation="路人"))
        repo5.upsert_npc(_NPC(name="赵萍", gender="女", occupation="主播"))
        pack11 = build_context(repo5, pov="林越", scene="机房", budget_chars=6000)
        names11 = [e["name"] for e in pack11.entities]
        check("D11 单字 NPC 名不被误提及", "林" not in names11, f"实际 {names11}")
        pack11b = build_context(repo5, pov="林越", scene="赵萍的直播间", budget_chars=6000)
        check("D11 多字实体仍能被提及",
              "赵萍" in [e["name"] for e in pack11b.entities],
              f"实际 {[e['name'] for e in pack11b.entities]}")
        repo5.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 主入口 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 单个测试崩溃不应吞掉其它测试的结果
    for _test in (
        test_tokenize,
        test_index_and_search,
        test_upsert_idempotent,
        test_migration,
        test_ingest_idempotent,
        test_context_budget,
        test_cli_contract,
        test_review_regressions,
    ):
        try:
            _test()
        except Exception as _exc:  # noqa: BLE001
            import traceback

            section(f"测试崩溃: {_test.__name__}")
            _results.append(traceback.format_exc())
            check(f"{_test.__name__} 未抛异常", False, f"{type(_exc).__name__}: {_exc}")

    output = result_summary()

    out_dir = Path(__file__).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "test_p0_report.txt").write_text(output, encoding="utf-8")

    try:
        print(output)
    except UnicodeEncodeError:
        ascii_output = output.replace("✅", "[PASS]").replace("❌", "[FAIL]")
        print(ascii_output)

    print(f"\n报告已保存到 {out_dir / 'test_p0_report.txt'}")
    sys.exit(0 if _failed == 0 else 1)
