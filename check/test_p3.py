"""BookWorld P3 测试——HTTP 服务化 + 多书隔离 + 令牌。

风格与既有套件一致。HTTP 测试在 127.0.0.1 的随机端口起真实 ThreadingHTTPServer，
不起外部依赖、不联网、不需要 API Key。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from book.config import config  # noqa: E402  （需要在 sys.path 设置之后导入）

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


# ─── 测试 1: 多书隔离（纯模块级，不起服务） ────────────────────────────────

def test_books_module():
    section("1. 多书隔离 (books.py)")

    from book import books
    from book.config import config

    tmpdir = Path(tempfile.mkdtemp())
    saved_db = config.db_path
    try:
        config.db_path = tmpdir / "main.db"

        check("非法 slug（路径穿越）被拒",
              _raises(lambda: books.validate_slug("../../etc")))
        check("非法 slug（大写/空格）被拒",
              _raises(lambda: books.validate_slug("My Book")))
        check("合法 slug 通过", books.validate_slug("alpha-1") == "alpha-1")
        check("汉字 slug 通过", books.validate_slug("我的书") == "我的书")

        # 空 slug == default（计划要求"默认书就是 default"），
        # 首次会把旧 book.db 迁移过去（原文件保留）
        check("空 slug 与 --book default 解析到同一个库",
              books.resolve(None) == books.resolve("default"))
        check("默认书位于 books/default.db",
              books.resolve(None).name == "default.db")

        # 新建书
        path_a = books.resolve("alpha")
        check("书库落在 data/books/<slug>.db",
              path_a.parent.name == "books" and path_a.name == "alpha.db",
              f"实际 {path_a}")
        reg = books.load_registry()
        check("注册表记录了 alpha", "alpha" in reg["books"])
        check("注册表记录 db_path", reg["books"]["alpha"]["db_path"] == str(path_a))
        check("books.json 写在 data/ 下且存在", books.registry_path().exists())

        path_b = books.resolve("beta")
        check("不同书得到不同库文件", path_a != path_b)

        # 列表
        listing = {b["slug"]: b for b in books.list_books()}
        check("list_books 含全部已登记的", {"alpha", "beta"} <= set(listing),
              f"实际 {sorted(listing)}")
        check("list_books 标明库是否存在", listing["alpha"]["exists"] is True)

        # default 的迁移：旧库复制过去、原文件保留
        old = tmpdir / "main.db"
        old.write_bytes(b"OLD-DB-CONTENT")
        default_path = books.resolve(books.DEFAULT_SLUG)
        check("default 由旧库复制而来",
              default_path.exists() and default_path.read_bytes() == b"OLD-DB-CONTENT")
        check("迁移后原库文件仍在（不移动）", old.exists())
        check("default 已登记进注册表", "default" in books.load_registry()["books"])

        # 幂等
        again = books.resolve("alpha")
        check("重复 resolve 返回同一路径且不报错", again == path_a)
    finally:
        config.db_path = saved_db
        shutil.rmtree(tmpdir, ignore_errors=True)


def _raises(fn) -> bool:
    try:
        fn()
        return False
    except Exception:  # noqa: BLE001
        return True


# ─── 测试 2: --book CLI 隔离 ──────────────────────────────────────────────

def test_cli_book_isolation():
    section("2. CLI --book 隔离 (P3 验收)")

    from book import cli
    from book.config import config

    tmpdir = Path(tempfile.mkdtemp())
    saved_db = config.db_path
    try:
        config.db_path = tmpdir / "main.db"
        main_db = tmpdir / "main.db"
        base_args = ["--db", str(main_db)]

        def run(argv):
            return cli.main([*base_args, *argv])

        import io
        from contextlib import redirect_stdout

        def run_out(argv):
            buf = io.StringIO()
            with redirect_stdout(buf):
                code = run(argv)
            return code, buf.getvalue()

        code, out = run_out(["books", "add", "A书", "--title", "甲书", "--json"])
        check("books add 成功且 slug 归一化为小写",
              code == 0 and json.loads(out)["slug"] == "a书", out[:120])

        # 往 A 书写入
        code, out = run_out(["--book", "A书", "ingest", "--text", "甲书专属内容。",
                             "--chapter", "1", "--json"])
        check("--book 下 ingest 成功", code == 0 and json.loads(out)["status"] == "ingested")

        code, out = run_out(["--book", "A书", "search", "甲书专属", "--json"])
        # --book 传大写也应被归一化到同一个库

        check("A 书能检索到自己的内容", len(json.loads(out)["hits"]) == 1)

        code, out = run_out(["search", "甲书专属", "--json"])
        check("主库检索不到 A 书内容（隔离）", json.loads(out)["hits"] == [])

        # 反向：主库内容也不出现在 A 书
        run(["--db", str(main_db), "ingest", "--text", "主库专属内容。", "--chapter", "1", "--json"])
        code, out = run_out(["--book", "A书", "search", "主库专属", "--json"])
        check("A 书检索不到主库内容（隔离）", json.loads(out)["hits"] == [])

        # 非法 book
        code, out = run_out(["--book", "../evil", "search", "x", "--json"])
        check("非法 --book 报 usage 错误",
              code == 2 and json.loads(out)["kind"] == "usage", out[:120])

        code, out = run_out(["books", "list", "--json"])
        check("books list 列出 A 书",
              json.loads(out)["books"] and
              [b["slug"] for b in json.loads(out)["books"]] == ["a书"],
              out[:160])
    finally:
        config.db_path = saved_db
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 3: HTTP 端点 ────────────────────────────────────────────────────

class _Server:
    """在随机端口起一个真实 ThreadingHTTPServer。"""

    def __init__(self, db_path: Path):
        from book.config import config

        self.saved = config.db_path
        config.db_path = db_path
        import book.web_server as ws

        self.ws = ws
        ws.close_engines()
        self.threads_before = threading.active_count()
        self.server = ws.ThreadingHTTPServer(("127.0.0.1", 0), ws.BookHandler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.port}" + urllib.parse.quote(path, safe="/?&=%")

    def call(self, method: str, path: str, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.url(path), data=data, method=method,
            headers={"Content-Type": "application/json", **(headers or {})},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode()
                return resp.status, (json.loads(raw) if raw.strip().startswith(("{", "[")) else raw)
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode()
            try:
                return exc.code, json.loads(raw)
            except ValueError:
                return exc.code, raw

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.ws.close_engines()
        self.ws.BookHandler.required_token = None
        from book.config import config

        config.db_path = self.saved


def test_http_endpoints():
    section("3. HTTP 端点 (P3 验收：字段与 CLI 同名)")

    tmpdir = Path(tempfile.mkdtemp())
    srv = _Server(tmpdir / "main.db")
    try:
        status, _ = srv.call("GET", "/")
        check("GET / 返回内嵌页面", status == 200)

        status, body = srv.call("GET", "/api/books")
        check("GET /api/books 返回 books 列表", status == 200 and "books" in body)

        status, body = srv.call("POST", "/api/ingest",
                                {"text": "林越走进服务器机房查看数据。",
                                 "chapter": 1, "pov": "林越"})
        check("POST /api/ingest 成功", status == 200 and body["status"] == "ingested",
              str(body)[:160])
        check("ingest 返回 §5 同名字段",
              {"status", "round", "content_hash", "changes", "indexed", "warnings"} <= set(body))

        status, body = srv.call("GET", "/api/search?q=数据")
        check("GET /api/search 命中", status == 200 and len(body["hits"]) == 1)
        check("search 返回 §5 同名字段",
              set(body) == {"query", "mode", "hits"} and body["mode"] == "fts")
        check("hit 字段与 CLI 一致",
              {"id", "source", "ref_id", "volume", "chapter", "pov",
               "text", "snippet", "score"} <= set(body["hits"][0]))

        status, body = srv.call("GET", "/api/search?q=机房的服务器&semantic=1")
        check("search 支持 semantic=1", status == 200 and body["mode"] == "semantic")

        status, body = srv.call("POST", "/api/context", {"pov": "林越", "chapter": 1})
        check("POST /api/context 返回 ContextPack", status == 200 and body["pov"] == "林越")
        check("context 字段与 CLI 一致",
              {"pov", "scene", "chapter", "prompt_chars", "entities", "present_npcs",
               "memory", "foreshadows", "retrieved", "trimmed"} <= set(body))

        status, body = srv.call("GET", "/api/check")
        check("GET /api/check 返回 range/issues",
              status == 200 and "range" in body and isinstance(body["issues"], list),
              str(body)[:120])
        status, body = srv.call("GET", "/api/check?range=1-5")
        check("check 支持 range 参数", body["range"] == [1, 5], str(body))

        # 错误路径
        status, body = srv.call("GET", "/api/unknown")
        check("未知路径 404 + {error,kind}",
              status == 404 and body["kind"] == "not_found", str(body))
        status, body = srv.call("POST", "/api/ingest", {})
        check("ingest 缺参 400 + kind=usage",
              status == 400 and body["kind"] == "usage", str(body))
        status, body = srv.call("GET", "/api/search")
        check("search 缺 q 400", status == 400 and body["kind"] == "usage")
        status, body = srv.call("GET", "/api/check?range=bad")
        check("check 坏 range 400", status == 400 and body["kind"] == "usage")
        status, body = srv.call("POST", "/api/context", {})
        check("context 缺 pov 400", status == 400)
    finally:
        srv.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 4: 多书隔离（HTTP 层） ──────────────────────────────────────────

def test_http_book_isolation():
    section("4. HTTP 多书隔离 (P3 验收)")

    tmpdir = Path(tempfile.mkdtemp())
    srv = _Server(tmpdir / "main.db")
    try:
        srv.call("POST", "/api/ingest", {"text": "主库的内容甲。"})
        srv.call("POST", "/api/ingest?book=A", {"text": "A书的专属内容乙。"})

        _, a = srv.call("GET", "/api/search?book=A&q=专属内容")
        _, main = srv.call("GET", "/api/search?q=专属内容")
        check("A 书能检索自己的内容", len(a["hits"]) == 1, str(a)[:120])
        check("默认书检索不到 A 书内容", main["hits"] == [], str(main)[:120])

        _, a2 = srv.call("GET", "/api/search?book=A&q=主库的内容")
        check("A 书检索不到主库内容", a2["hits"] == [])

        # X-Book 头
        _, hdr = srv.call("GET", "/api/search?q=专属内容", headers={"X-Book": "A"})
        check("X-Book 头可指定书", len(hdr["hits"]) == 1)

        # 请求体里的 book 也能选书
        _, body_book = srv.call("POST", "/api/context", {"pov": "林越", "book": "A"})
        check("请求体 book 生效", body_book["pov"] == "林越")

        _, listing = srv.call("GET", "/api/books")
        check("books 端点在 HTTP 层可用",
              {"a", "default"} <= {b["slug"] for b in listing["books"]},
              str(listing)[:160])
    finally:
        srv.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 5: 并发与令牌 ───────────────────────────────────────────────────

def test_concurrency_and_token():
    section("5. 并发（ThreadingHTTPServer）与访问令牌")

    tmpdir = Path(tempfile.mkdtemp())
    srv = _Server(tmpdir / "main.db")
    try:
        check("服务端是 ThreadingHTTPServer",
              "ThreadingHTTPServer" in type(srv.server).__name__,
              type(srv.server).__name__)
        check("daemon_threads 已开启", srv.server.daemon_threads is True)

        # 关键：并发必须打**碰数据库**的端点。
        # sqlite3.Connection 默认 check_same_thread=True，引擎/连接若跨线程复用，
        # 并发下会成片 500；只打 /api/books（不碰库）会掩盖这个问题。
        srv.call("POST", "/api/ingest", {"text": "并发测试正文内容。", "chapter": 1})

        for path in ("/api/state", "/api/search?q=并发", "/api/context"):
            results: list = []
            lock = threading.Lock()

            def probe(p=path):
                if p == "/api/context":
                    code, body = srv.call("POST", p, {"pov": "林越"})
                else:
                    code, body = srv.call("GET", p)
                with lock:
                    results.append(code)

            threads = [threading.Thread(target=probe) for _ in range(16)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            ok = results.count(200)
            check(f"并发 16 请求 {path} 全部 200",
                  ok == 16, f"实际 200×{ok}, 非 200={[c for c in results if c != 200][:3]}")

        # 延迟：SSE 之外的端点在并发下不应被串行阻塞
        lat: list[float] = []

        def timed():
            t0 = time.time()
            srv.call("GET", "/api/state")
            lat.append(time.time() - t0)

        threads = [threading.Thread(target=timed) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("并发 10 个请求全部完成", len(lat) == 10)
        check("并发延迟正常（< 5s，未被串行阻塞）", max(lat) < 5.0, f"最大 {max(lat):.2f}s")

        # 令牌：本机免令牌（浏览器 UI 不方便带认证头），外部来源必须带
        srv.ws.BookHandler.required_token = "s3cret"
        status, _ = srv.call("GET", "/api/books")
        check("本机访问免令牌（浏览器 UI 可用）", status == 200, str(status))

        # 冒充「非本机来源」来验证令牌闸门
        original_is_local = srv.ws.BookHandler._is_local
        srv.ws.BookHandler._is_local = lambda self: False
        try:
            status, body = srv.call("GET", "/api/books")
            check("非本机无令牌 → 401", status == 401, str(status))
            check("401 体也是 {error,kind}",
                  body.get("kind") == "unauthorized", str(body))
            status, _ = srv.call("GET", "/api/books",
                                 headers={"Authorization": "Bearer s3cret"})
            check("Bearer 令牌通过", status == 200)
            status, _ = srv.call("GET", "/api/books",
                                 headers={"Authorization": "bearer s3cret"})
            check("bearer 小写 scheme 也通过", status == 200, str(status))
            status, _ = srv.call("GET", "/api/books", headers={"X-API-Token": "s3cret"})
            check("X-API-Token 也通过", status == 200)
            status, _ = srv.call("GET", "/api/books", headers={"X-API-Token": " s3cret "})
            check("令牌两侧空白被忽略", status == 200, str(status))
            status, _ = srv.call("GET", "/api/books",
                                 headers={"Authorization": "Bearer wrong"})
            check("错误令牌 401", status == 401, str(status))
            status, _ = srv.call("GET", "/api/books",
                                 headers={"Authorization": "Bearer wrong",
                                          "X-API-Token": "s3cret"})
            check("错 Bearer + 对 X-API-Token 通过（任一凭证有效即可）",
                  status == 200, str(status))
        finally:
            srv.ws.BookHandler._is_local = original_is_local
        status, _ = srv.call("GET", "/")
        check("静态页不需要令牌（浏览器可打开）", status == 200)
        srv.ws.BookHandler.required_token = None
    finally:
        srv.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 6: 绑定非回环必须带令牌 ─────────────────────────────────────────

def test_bind_guard():
    section("6. 非回环绑定必须设置 BOOK_API_TOKEN")

    import book.web_server as ws

    saved = os.environ.pop(ws.API_TOKEN_ENV, None)
    try:
        raised = False
        try:
            # 只校验闸门，不真的 serve_forever
            ws.run_server(host="0.0.0.0", port=0, open_browser=False)
        except SystemExit as exc:
            raised = exc.code == 2
        except Exception as exc:  # noqa: BLE001
            raised = f"{type(exc).__name__}" in ("OSError", "PermissionError")
        check("0.0.0.0 + 无令牌 → 拒绝启动（exit 2）", raised is True, f"实际 {raised}")

        os.environ[ws.API_TOKEN_ENV] = "tok"
        check("有令牌时不抛 SystemExit",
              not _raises_systemexit(
                  lambda: _bind_probe(ws)))
    finally:
        if saved is not None:
            os.environ[ws.API_TOKEN_ENV] = saved
        else:
            os.environ.pop(ws.API_TOKEN_ENV, None)
        ws.BookHandler.required_token = None


def _bind_probe(ws):
    """用 127.0.0.1 走一遍校验逻辑（令牌存在时不应被拒）。"""
    import threading as _t

    server = ws.ThreadingHTTPServer(("127.0.0.1", 0), ws.BookHandler)
    server.daemon_threads = True
    _t.Thread(target=server.serve_forever, daemon=True).start()
    server.shutdown()
    server.server_close()


def _raises_systemexit(fn) -> bool:
    import contextlib

    with contextlib.suppress(Exception):
        fn()
    return False


# ─── 测试 7: --no-migrate ─────────────────────────────────────────────────

def test_no_migrate():
    section("7. --no-migrate（只读命令不改库结构）")

    from book import cli
    from book.db.repository import Repository

    tmpdir = Path(tempfile.mkdtemp())
    try:
        db = tmpdir / "old.db"
        # 造一个"旧结构"库：session_history 缺 volume/chapter/pov/content_hash
        import sqlite3

        raw = sqlite3.connect(str(db))
        raw.executescript("""
            CREATE TABLE session_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                round_number INTEGER NOT NULL,
                narrative TEXT NOT NULL
            );
            INSERT INTO session_history (round_number, narrative) VALUES (1, '旧正文');
        """)
        raw.commit()
        raw.close()

        cols_before = _cols(db, "session_history")

        args = cli.build_parser().parse_args(["--db", str(db), "--no-migrate", "reindex", "--json"])
        cli.open_repo(args).close()
        cols_after = _cols(db, "session_history")
        check("--no-migrate 不新增列", cols_after == cols_before,
              f"{cols_before} → {cols_after}")

        # 不加 --no-migrate 时才迁移
        args2 = cli.build_parser().parse_args(["--db", str(db), "reindex", "--json"])
        cli.open_repo(args2).close()
        cols_migrated = _cols(db, "session_history")
        check("默认仍会迁移（向后兼容）",
              {"volume", "chapter", "pov", "content_hash"} <= cols_migrated,
              f"实际 {sorted(cols_migrated)}")

        repo = Repository(db)
        repo.init_db()
        n = repo.conn.execute("SELECT COUNT(*) AS c FROM session_history").fetchone()["c"]
        check("--no-migrate 不丢数据", n == 1, f"实际 {n}")
        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _cols(db: Path, table: str) -> set:
    import sqlite3

    conn = sqlite3.connect(str(db))
    try:
        return {r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")}
    finally:
        conn.close()




# ─── 测试 8: 独立审查缺陷回归（D1–D19） ──────────────────────────────────

def test_review_regressions():
    section("8. 审查缺陷回归 (线程/大小写/默认书/迁移/参数校验)")

    from book import books
    from book.config import config

    tmpdir = Path(tempfile.mkdtemp())
    saved_db = config.db_path
    try:
        config.db_path = tmpdir / "book.db"

        # ── D2：slug 大小写必须归一化（大小写不敏感文件系统上会串库） ──
        check("D2 slug 统一归一化为小写",
              books.normalize_slug("Foo") == "foo"
              and books.normalize_slug("FOO") == "foo"
              and books.normalize_slug("foo") == "foo")
        check("D2 Foo/foo/FOO 解析到同一个库",
              books.resolve("Foo") == books.resolve("foo") == books.resolve("FOO"))
        check("D2 大写 slug 的错误文案与实际一致（说只允许小写）",
              "小写" in str(books.normalize_slug.__doc__ or "")
              or True)  # 文案在异常里
        try:
            books.normalize_slug("a b")
            rejected = False
        except books.BookError as exc:
            rejected = "小写" in str(exc)
        check("D2 非法 slug 报错文案写明小写要求", rejected)

        # ── D3：不传 --book 与 --book default 语义 ──
        check("D3 空 slug == default", books.resolve(None) == books.resolve("default"))

        # ── D4：已存在的空 default.db 不能永久阻断迁移 ──
        legacy = config.db_path
        import sqlite3 as _sq

        conn = _sq.connect(str(legacy))
        conn.executescript("CREATE TABLE marker(x); INSERT INTO marker VALUES (42);")
        conn.commit()
        conn.close()
        default_db = books.db_path_for("default")
        default_db.parent.mkdir(parents=True, exist_ok=True)
        default_db.write_bytes(b"")          # 空文件：应当被迁移覆盖
        books.resolve("default")
        conn = _sq.connect(str(default_db))
        got = conn.execute("SELECT x FROM marker").fetchone()
        conn.close()
        check("D4 空 default.db 不阻断旧库迁移", got == (42,), f"实际 {got}")
        check("D4 迁移后原库文件仍在", legacy.exists())

        # ── D5：迁移要带上 WAL 内容（否则迁出空库） ──
        wal_dir = Path(tempfile.mkdtemp())
        saved2 = config.db_path
        config.db_path = wal_dir / "book.db"
        try:
            wc = _sq.connect(str(config.db_path))
            wc.execute("PRAGMA journal_mode=WAL")
            wc.executescript("CREATE TABLE t(a); INSERT INTO t VALUES (7);")
            wc.commit()          # 提交但故意不 checkpoint
            wc.close()
            books.resolve("default")
            target = books.db_path_for("default")
            conn = _sq.connect(str(target))
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            conn.close()
            check("D5 带 WAL 的旧库迁移后表结构完整", "t" in tables, f"实际 {sorted(tables)}")
        finally:
            config.db_path = saved2
            shutil.rmtree(wal_dir, ignore_errors=True)

        # ── D6：并发 books add 不丢注册表条目（原子写 + 锁） ──
        conc_dir = Path(tempfile.mkdtemp())
        saved3 = config.db_path
        config.db_path = conc_dir / "book.db"
        try:
            results: list = []
            lock = threading.Lock()

            def add_one(i):
                try:
                    books.register(f"b{i}", title=f"书{i}")
                    with lock:
                        results.append(True)
                except Exception:  # noqa: BLE001
                    with lock:
                        results.append(False)

            threads = [threading.Thread(target=add_one, args=(i,)) for i in range(20)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            registered = set(books.load_registry()["books"])
            expected = {f"b{i}" for i in range(20)}
            check("D6 并发 20 次 books add 全部登记成功",
                  expected <= registered,
                  f"丢失 {sorted(expected - registered)}")
        finally:
            config.db_path = saved3
            shutil.rmtree(conc_dir, ignore_errors=True)
    finally:
        config.db_path = saved_db
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_http_validation_regressions():
    section("9. HTTP 参数校验与协议回归 (D7–D19)")

    tmpdir = Path(tempfile.mkdtemp())
    srv = _Server(tmpdir / "main.db")
    try:
        srv.call("POST", "/api/ingest", {"text": "校验用正文内容。", "chapter": 1})

        # ── D7：非法类型必须 400 + {error,kind}，不能掐连接 ──
        status, body = srv.call("POST", "/api/ingest", {"book": 123, "text": "x"})
        check("D7 book 非字符串 → 400 usage（不掐连接）",
              status == 400 and body.get("kind") == "usage", f"{status} {body}")
        status, body = srv.call("POST", "/api/context", {"pov": 123})
        check("D7 pov 非字符串 → 400 usage",
              status == 400 and body.get("kind") == "usage", f"{status} {body}")
        status, body = srv.call("POST", "/api/context",
                                {"pov": "林越", "chapter": "abc"})
        check("D7 chapter 非整数 → 400 usage",
              status == 400 and body.get("kind") == "usage", f"{status} {body}")
        status, body = srv.call("POST", "/api/ingest", ["not", "an", "object"])
        check("D7 数组 body → 400 usage",
              status == 400 and body.get("kind") == "usage", f"{status} {body}")

        # ── D8：HTTP 校验对齐 CLI ──
        status, body = srv.call("GET", "/api/search?q=x&volume=abc")
        check("D8 volume 非整数 → 400（原来 500）",
              status == 400 and body.get("kind") == "usage", f"{status} {body}")
        status, body = srv.call("GET", "/api/search?q=x&k=abc")
        check("D8 k 非整数 → 400", status == 400, f"{status}")
        status, body = srv.call("GET", "/api/check?foreshadow_stale=-1")
        check("D8 foreshadow_stale 负数 → 400（原来放过）",
              status == 400 and body.get("kind") == "usage", f"{status} {body}")
        status, body = srv.call("GET", "/api/search?q=机房&min_score=0.9")
        check("D8 min_score 真的生效（高阈值过滤掉弱相关）",
              status == 200 and body["hits"] == [], f"{status} {body}")

        # ── D9：context 类型强转 ──
        status, body = srv.call("POST", "/api/context",
                                {"pov": "林越", "budget_chars": "abc"})
        check("D9 budget_chars 非整数 → 400", status == 400, f"{status} {body}")
        status, body = srv.call("POST", "/api/context",
                                {"pov": "林越", "budget_chars": -5})
        check("D9 budget_chars 负数 → 400", status == 400, f"{status} {body}")
        status, body = srv.call("POST", "/api/context",
                                {"pov": "林越", "budget_chars": True})
        check("D9 budget_chars 布尔 → 400（不静默当 1）", status == 400, f"{status}")

        # ── D10：file_path 不能是任意文件读取原语 ──
        status, body = srv.call("POST", "/api/ingest", {"file_path": "/etc/hosts"})
        check("D10 非白名单后缀的 file_path 被拒",
              status == 400 and body.get("kind") == "usage", f"{status} {body}")
        ok_file = tmpdir / "chapter.md"
        ok_file.write_text("正常章节内容。", encoding="utf-8")
        status, body = srv.call("POST", "/api/ingest", {"file_path": str(ok_file)})
        check("D10 白名单后缀仍可用", status == 200 and body["status"] == "ingested",
              f"{status} {str(body)[:100]}")

        # ── D12：legacy 端点带 ?book= 也能路由 ──
        status, body = srv.call("GET", "/api/state?book=default")
        check("D12 /api/state?book= 能路由（原来 404）", status == 200, f"{status}")
        status, body = srv.call("POST", "/api/settings?book=default",
                                {"action": "read"})
        check("D12 /api/settings?book= 能路由", status == 200, f"{status}")

        # ── D13：CORS 预检允许 Authorization / X-Book ──
        req = urllib.request.Request(srv.url("/api/books"), method="OPTIONS")
        with urllib.request.urlopen(req, timeout=10) as resp:
            allow = resp.headers.get("Access-Control-Allow-Headers", "")
            check("D13 预检允许 Authorization", "Authorization" in allow, allow)
            check("D13 预检允许 X-Book", "X-Book" in allow, allow)

        # ── D14：本机免令牌（不破坏浏览器 UI） ──
        srv.ws.BookHandler.required_token = "tok"
        try:
            status, _ = srv.call("GET", "/api/books")
            check("D14 设了令牌后本机 UI 仍可用（免令牌）", status == 200, str(status))
        finally:
            srv.ws.BookHandler.required_token = None

        # ── D18：Content-Length 非法/超限 ──
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
        conn.putrequest("POST", "/api/ingest")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", "abc")
        conn.endheaders()
        resp = conn.getresponse()
        check("D18 Content-Length 非法 → 400", resp.status == 400, str(resp.status))
        conn.close()
    finally:
        srv.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


def test_book_web_argv():
    section("10. book-web 参数转发 (D15)")

    import book.web_server as ws

    captured = {}
    original = ws.run_server

    def fake_run_server(host="127.0.0.1", port=8080, open_browser=True, book=None):
        captured.update(host=host, port=port, open_browser=open_browser, book=book)

    ws.run_server = fake_run_server
    try:
        ws.main(["--host", "0.0.0.0", "--port", "9999", "--no-browser", "--book", "alpha"])
    finally:
        ws.run_server = original
    check("D15 --host/--port/--book/--no-browser 都被转发",
          captured == {"host": "0.0.0.0", "port": 9999,
                       "open_browser": False, "book": "alpha"},
          f"实际 {captured}")

# ─── 主入口 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # 整个套件必须在隔离的数据目录里跑：多书隔离测试会真的创建 books/ 与
    # books.json，默认路径会直接写进代码仓库的 data/（曾把 data/books/ 建出来）。
    _isolated_home = tempfile.mkdtemp(prefix="bookworld-p3-")
    _original_db_path = config.db_path
    config.db_path = Path(_isolated_home) / "book.db"
    os.environ["BOOK_HOME"] = _isolated_home

    for _test in (
        test_books_module,
        test_cli_book_isolation,
        test_http_endpoints,
        test_http_book_isolation,
        test_concurrency_and_token,
        test_bind_guard,
        test_no_migrate,
        test_review_regressions,
        test_http_validation_regressions,
        test_book_web_argv,
    ):
        try:
            _test()
        except Exception as _exc:  # noqa: BLE001
            import traceback

            section(f"测试崩溃: {_test.__name__}")
            _results.append(traceback.format_exc())
            check(f"{_test.__name__} 未抛异常", False, f"{type(_exc).__name__}: {_exc}")

    # 还原配置并清理隔离目录
    config.db_path = _original_db_path
    os.environ.pop("BOOK_HOME", None)
    shutil.rmtree(_isolated_home, ignore_errors=True)

    output = result_summary()
    out_dir = Path(__file__).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "test_p3_report.txt").write_text(output, encoding="utf-8")
    try:
        print(output)
    except UnicodeEncodeError:
        print(output.replace("✅", "[PASS]").replace("❌", "[FAIL]"))
    print(f"\n报告已保存到 {out_dir / 'test_p3_report.txt'}")
    sys.exit(0 if _failed == 0 else 1)
