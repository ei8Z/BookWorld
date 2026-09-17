"""BookWorld P2 测试——稀疏向量召回 + 一致性检查 + 成本优化。

风格与 test_runner.py / test_p0.py / test_p1.py 一致（自研 check 汇总，无 pytest 依赖）。
不依赖 DEEPSEEK_API_KEY：所有 LLM 路径 mock 或跳过。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

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


def _tmp_repo(tmpdir: str, name: str = "p2.db"):
    from book.db.repository import Repository

    repo = Repository(Path(tmpdir) / name)
    repo.init_db()
    return repo


def _add_round(repo, round_number: int, narrative: str, state: dict | None = None, **meta):
    from book.db import search as S

    sid = repo.add_session(round_number, "", narrative, state or {}, **meta)
    S.index_session(repo.conn, sid, narrative, **meta)
    return sid


# ─── 测试 1: 稀疏向量 ─────────────────────────────────────────────────────

def test_vector():
    section("1. 稀疏向量 (memory/vector.py)")

    from book.memory.vector import DIM, Embedder, cosine, sparse_embed

    v = sparse_embed("林越走进服务器机房")
    check("sparse_embed 返回稀疏 dict", isinstance(v, dict) and len(v) > 0)
    check("维度都在 [0, DIM)", all(0 <= k < DIM for k in v))
    check("权重都为正", all(x > 0 for x in v.values()))
    check("确定性（同文本两次结果相同）", sparse_embed("林越走进服务器机房") == v)
    check("空串 → 空向量", sparse_embed("") == {})
    check("None → 空向量", sparse_embed(None) == {})
    check("纯标点 → 空向量", sparse_embed("！？。，") == {})
    check("单字也有向量", len(sparse_embed("林")) == 1)

    check("自身余弦 = 1.0", abs(cosine(v, v) - 1.0) < 1e-9)
    check("空向量余弦 = 0.0", cosine(v, {}) == 0.0 and cosine({}, {}) == 0.0)

    a = sparse_embed("林越走进服务器机房查看日志")
    b = sparse_embed("机房的服务器里，林越在翻看日志")
    c = sparse_embed("赵萍在直播间和粉丝聊天")
    check("共享词汇的改写相似度明显高于无关文本",
          cosine(a, b) > cosine(a, c) * 3,
          f"{cosine(a, b):.4f} vs {cosine(a, c):.4f}")
    check("无关文本相似度很低", cosine(a, c) < 0.15, f"实际 {cosine(a, c):.4f}")
    check("余弦对称", abs(cosine(a, b) - cosine(b, a)) < 1e-12)

    # 计划要求的边界：纯同义改写召回有限（不是缺陷，是固有边界）
    check("纯同义改写（电脑/计算机）相似度为 0（记录该边界）",
          cosine(sparse_embed("电脑"), sparse_embed("计算机")) == 0.0)

    # Embedder 协议：只留接口
    check("Embedder 是 runtime_checkable Protocol",
          hasattr(Embedder, "__protocol_attrs__") or getattr(Embedder, "_is_protocol", False))
    check("Embedder 声明了 embed 方法", hasattr(Embedder, "embed"))

    # tf 次线性缩放：重复词不应线性放大权重
    once = sparse_embed("机房")
    many = sparse_embed("机房" * 10)
    check("重复 n-gram 权重次线性增长（sqrt）",
          max(many.values()) < max(once.values()) * 10,
          f"{max(once.values()):.2f} → {max(many.values()):.2f}")


# ─── 测试 2: 语义检索 ─────────────────────────────────────────────────────

def test_semantic_search():
    section("2. 语义检索 (search --semantic, P2 验收)")

    from book.db import search as S

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)
        _add_round(repo, 1, "林越走进服务器机房，查看屏幕上的数据。", chapter=1)
        _add_round(repo, 2, "机房的服务器闪烁着指示灯，林越翻看日志。", chapter=2)
        _add_round(repo, 3, "赵萍在直播间和粉丝聊天。", chapter=3)

        hits = S.search(repo.conn, "机房的服务器", k=5, semantic=True)
        check("语义检索有结果", len(hits) >= 2, f"实际 {len(hits)}")
        check("改写说法排在首位（共享词汇召回）",
              hits[0].ref_id == "2", f"实际首位 {hits[0].ref_id if hits else None}")
        check("semantic 路径标注 matched=semantic",
              all(h.matched == "semantic" for h in hits))
        check("score 在 (0, 1]", all(0 < h.score <= 1.0 for h in hits))
        check("相关度单调递减",
              all(hits[i].score >= hits[i + 1].score for i in range(len(hits) - 1)))

        check("无关查询不会命中无关文档",
              all(h.ref_id != "3" for h in S.search(repo.conn, "完全不相干的量子物理",
                                                    k=5, semantic=True)))
        check("semantic=False 走 FTS（matched 非 semantic）",
              all(h.matched != "semantic" for h in S.search(repo.conn, "机房的服务器", k=5)))

        # 过滤仍然生效
        hits_v = S.search(repo.conn, "机房服务器", k=5, semantic=True, volume=2)
        check("semantic 支持 volume 过滤", all(h.volume == 2 for h in hits_v))
        hits_s = S.search(repo.conn, "机房服务器", k=5, semantic=True, source="narrative")
        check("semantic 支持 source 过滤",
              all(h.source == "narrative" for h in hits_s))

        check("空查询返回空", S.search(repo.conn, "", semantic=True) == [])
        check("k<=0 返回空", S.search(repo.conn, "机房", k=0, semantic=True) == [])
        check("纯标点查询返回空", S.search(repo.conn, "！？", semantic=True) == [])

        # CLI 契约
        from book import cli

        payload = cli.cmd_search(cli.build_parser().parse_args(
            ["--db", str(Path(tmpdir) / "p2.db"), "search", "机房的服务器",
             "--semantic", "--json"]))
        check("CLI mode=semantic", payload["mode"] == "semantic", f"实际 {payload['mode']}")
        check("CLI hits 字段齐全",
              {"id", "source", "ref_id", "volume", "chapter", "pov",
               "text", "snippet", "score"} <= set(payload["hits"][0]))
        payload2 = cli.cmd_search(cli.build_parser().parse_args(
            ["--db", str(Path(tmpdir) / "p2.db"), "search", "机房", "--json"]))
        check("不带 --semantic 时 mode=fts", payload2["mode"] == "fts")
        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 3: 时间线检查 ───────────────────────────────────────────────────

def test_timeline():
    section("3. 时间线一致性 (check kind=timeline)")

    from book.executor.consistency import check_timeline, parse_story_time

    check("中文日期+时间可解析",
          parse_story_time("2026年06月22日 星期一", "上午 08:00") == (2026, 6, 22, 8, 0))
    check("ISO 日期可解析",
          parse_story_time("2026-06-23", "14:30") == (2026, 6, 23, 14, 30))
    check("解析不出返回 None", parse_story_time("乱七八糟", "") is None)
    check("只有日期没有时间时按 00:00",
          parse_story_time("2026年06月22日", "") == (2026, 6, 22, 0, 0))

    def sess(sid, rn, date, time):
        return {"id": sid, "round_number": rn,
                "story_state_json": json.dumps({"current_date": date, "current_time": time})}

    ok = [sess(1, 1, "2026年06月22日", "上午 08:00"),
          sess(2, 2, "2026年06月22日", "下午 14:00"),
          sess(3, 3, "2026年06月23日", "上午 09:00")]
    check("时间单调递增不报问题", check_timeline(ok) == [])

    same = [sess(1, 1, "2026年06月22日", "上午 08:00"),
            sess(2, 2, "2026年06月22日", "上午 08:00")]
    check("时间相同（原地）不报问题", check_timeline(same) == [])

    bad = [sess(1, 1, "2026年06月22日", "下午 14:00"),
           sess(2, 2, "2026年06月22日", "上午 08:00")]
    issues = check_timeline(bad)
    check("时间倒挂被报出", len(issues) == 1, f"实际 {len(issues)}")
    if issues:
        check("倒挂 kind=timeline", issues[0].kind == "timeline")
        check("倒挂 severity=high", issues[0].severity == "high")
        check("倒挂 refs 指向两轮",
              [r["id"] for r in issues[0].refs] == [1, 2],
              f"实际 {issues[0].refs}")

    cross = [sess(1, 1, "2026年06月22日", "上午 08:00"),
             sess(2, 2, "2026年05月01日", "上午 08:00")]
    check("跨月倒挂也能报出", len(check_timeline(cross)) == 1)
    unparsable = [sess(1, 1, "?", ""), sess(2, 2, "2026年06月22日", "上午 08:00")]
    check("无法解析的轮次被跳过而不是误报", check_timeline(unparsable) == [])


# ─── 测试 4: 状态检查 ─────────────────────────────────────────────────────

def test_state_check():
    section("4. 状态一致性 (check kind=state)")

    from book.executor.consistency import check_state
    from book.models import NPC

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)
        repo.upsert_npc(NPC(name="王芳", gender="女", is_present=True))
        repo.upsert_npc(NPC(name="赵萍", gender="女", is_present=False))

        # 正文里两人都没出现 → 王芳该报（在场却没露面）
        sid = repo.add_session(1, "", "林越一个人在屋里发呆。", {})
        sessions = [{"id": sid, "round_number": 1, "narrative": "林越一个人在屋里发呆。",
                     "story_state_json": "{}"}]
        issues = check_state(repo, sessions)
        kinds = [i.detail for i in issues]
        check("在场却没露面 → 报出", any("王芳" in d and "在场" in d for d in kinds), f"实际 {kinds}")
        check("不在场且未出现 → 不报", not any("赵萍" in d for d in kinds), f"实际 {kinds}")

        # 赵萍出现两次 → 该报（不在场却出现）
        sessions2 = [{"id": sid, "round_number": 1,
                      "narrative": "赵萍走了过来，赵萍笑着说你好。",
                      "story_state_json": "{}"}]
        issues2 = check_state(repo, sessions2)
        check("不在场却出现 ≥2 次 → 报出",
              any("赵萍" in i.detail for i in issues2),
              f"实际 {[i.detail for i in issues2]}")
        check("只出现一次（可能只是提及）→ 不报",
              not any("赵萍" in i.detail for i in
                      check_state(repo, [{"id": sid, "round_number": 1,
                                          "narrative": "有人提到了赵萍。",
                                          "story_state_json": "{}"}])))

        # 数值越界
        bad_state = json.dumps({"hp": 150, "max_hp": 100, "en": -5, "max_en": 100})
        issues3 = check_state(repo, [{"id": sid, "round_number": 1,
                                      "narrative": "无事发生。",
                                      "story_state_json": bad_state}])
        details3 = [i.detail for i in issues3]
        check("HP 越界被报出", any("HP=150" in d for d in details3), f"实际 {details3}")
        check("EN 越界被报出", any("EN=-5" in d for d in details3), f"实际 {details3}")
        check("越界 severity=mid", all(i.severity == "mid" for i in issues3))

        # 正常状态不误报
        good = json.dumps({"hp": 80, "max_hp": 100, "en": 50, "max_en": 100})
        check("正常数值不报",
              not any("HP" in i.detail or "EN" in i.detail for i in
                      check_state(repo, [{"id": sid, "round_number": 1,
                                          "narrative": "无事发生。",
                                          "story_state_json": good}])))
        check("空会话列表不崩", check_state(repo, []) == [])
        check("空正文不参与在场判断",
              check_state(repo, [{"id": sid, "round_number": 1, "narrative": "",
                                  "story_state_json": "{}"}]) == [])
        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 5: 伏笔检查 ─────────────────────────────────────────────────────

def test_foreshadow_check():
    section("5. 伏笔一致性 (check kind=foreshadow)")

    from book.executor.consistency import check_foreshadows

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)
        repo.add_foreshadow("早埋的", planted_chapter=1)
        repo.add_foreshadow("刚埋的", planted_chapter=48)
        repo.add_foreshadow("没写章号的")
        repo.add_foreshadow("已收的", planted_chapter=1)
        repo.close_foreshadow("已收的", recovered_chapter=10)

        issues = check_foreshadows(repo, current_chapter=50, stale_after=30)
        names = [i.detail for i in issues]
        check("埋太久的被报出", any("早埋的" in d for d in names), f"实际 {names}")
        check("刚埋的不报", not any("刚埋的" in d for d in names))
        check("没写章号的不报（无法判断）", not any("没写章号" in d for d in names))
        check("已收的不报", not any("已收的" in d for d in names))
        check("kind=foreshadow / severity=low",
              all(i.kind == "foreshadow" and i.severity == "low" for i in issues))
        check("detail 里含埋设章与间隔",
              any("第 1 章" in d and "49 章" in d for d in names), f"实际 {names}")

        check("阈值放宽后不报",
              check_foreshadows(repo, current_chapter=50, stale_after=100) == [])
        check("当前章号未知时不报",
              check_foreshadows(repo, current_chapter=None) == [])
        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 6: check 集成与 CLI ─────────────────────────────────────────────

def test_check_integration():
    section("6. check 集成与 CLI 契约 (P2 验收)")

    from book import cli

    tmpdir = tempfile.mkdtemp()
    try:
        db = Path(tmpdir) / "c.db"
        repo = _tmp_repo(tmpdir, "c.db")
        repo.upsert_npc(__import__("book.models", fromlist=["NPC"]).NPC(
            name="王芳", gender="女", is_present=True))
        repo.add_session(1, "", "林越在机房。",
                         {"current_date": "2026年06月22日", "current_time": "下午 14:00"})
        repo.add_session(2, "", "林越回屋。",
                         {"current_date": "2026年06月22日", "current_time": "上午 08:00"})
        repo.add_foreshadow("陈旧伏笔", planted_chapter=1)
        repo.update_story_state(chapter=99)
        repo.close()

        payload = cli.cmd_check(cli.build_parser().parse_args(
            ["--db", str(db), "check", "--json"]))
        kinds = {i["kind"] for i in payload["issues"]}
        check("三类检查全部报出", {"timeline", "state", "foreshadow"} <= kinds,
              f"实际 {sorted(kinds)}")
        check("range 字段回传", payload["range"] == [None, None])
        for issue in payload["issues"]:
            check(f"{issue['kind']} 条目字段齐全",
                  set(issue) == {"kind", "severity", "detail", "refs"},
                  f"实际 {sorted(issue)}")
            check(f"{issue['kind']} severity 合法",
                  issue["severity"] in ("high", "mid", "low"))
        severities = [i["severity"] for i in payload["issues"]]
        check("结果按严重度排序", severities == sorted(severities, key=lambda s: {"high": 0, "mid": 1, "low": 2}[s]),
              f"实际 {severities}")

        # range 过滤
        p_early = cli.cmd_check(cli.build_parser().parse_args(
            ["--db", str(db), "check", "--range", "1-1", "--json"]))
        check("--range 过滤生效（只剩第1轮不构成倒挂）",
              not any(i["kind"] == "timeline" for i in p_early["issues"]),
              f"实际 {[i['kind'] for i in p_early['issues']]}")
        check("--range 回传正确", p_early["range"] == [1, 1])

        # 阈值可调
        p_loose = cli.cmd_check(cli.build_parser().parse_args(
            ["--db", str(db), "check", "--foreshadow-stale", "500", "--json"]))
        check("--foreshadow-stale 生效",
              not any(i["kind"] == "foreshadow" for i in p_loose["issues"]))

        # --deep 无 Key：不崩，纯规则结果保留，并说明跳过
        p_deep = cli.cmd_check(cli.build_parser().parse_args(
            ["--db", str(db), "check", "--deep", "--json"]))
        check("--deep 无 Key 时不崩且保留纯规则结果",
              {"timeline", "state", "foreshadow"} <= {i["kind"] for i in p_deep["issues"]})
        check("--deep 无 Key 时给出说明",
              any("DEEPSEEK_API_KEY" in i["detail"] for i in p_deep["issues"]))

        # 干净库不报
        clean_dir = tempfile.mkdtemp()
        try:
            clean_db = Path(clean_dir) / "clean.db"
            r2 = _tmp_repo(clean_dir, "clean.db")
            r2.close()
            p_clean = cli.cmd_check(cli.build_parser().parse_args(
                ["--db", str(clean_db), "check", "--json"]))
            check("干净库 issues 为空", p_clean["issues"] == [],
                  f"实际 {p_clean['issues']}")
        finally:
            shutil.rmtree(clean_dir, ignore_errors=True)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 7: 成本优化 ─────────────────────────────────────────────────────

def test_cost_optimization():
    section("7. 成本优化 (ingest --no-extract / extract --range)")

    from book import cli
    from book.db.repository import Repository

    tmpdir = tempfile.mkdtemp()
    try:
        db = Path(tmpdir) / "nx.db"
        out = cli.cmd_ingest(cli.build_parser().parse_args(
            ["--db", str(db), "ingest", "--text", "林越在机房查看日志内容。",
             "--chapter", "1", "--no-extract", "--json"]))
        check("--no-extract 仍入库成功", out["status"] == "ingested")
        check("--no-extract 索引了正文", out["indexed"]["narrative"] == 1)
        check("--no-extract changes 为空", out["changes"] == {})
        check("--no-extract 在 warnings 里注明",
              any("no-extract" in w for w in out["warnings"]), f"实际 {out['warnings']}")

        repo = Repository(db)
        repo.init_db()
        n = repo.conn.execute("SELECT COUNT(*) AS c FROM session_history").fetchone()["c"]
        check("--no-extract 正文确实入库", n == 1, f"实际 {n}")
        repo.close()

        # 无 Key 时 extract 应报 usage（而不是静默什么都不做）
        try:
            cli.cmd_extract(cli.build_parser().parse_args(
                ["--db", str(db), "extract", "--range", "1-5", "--json"]))
            raised = False
        except cli.CliError as exc:
            raised = exc.kind == "usage" and exc.code == 2
        check("无 Key 时 extract 报 usage 错误", raised)

        # 有 Key（mock）时批量抽取并重建索引
        import book.executor.card_manager as cm

        saved_llm = cli.LLMClient
        saved_apply = cm.CardManager.apply

        class _KeyedLLM(saved_llm):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.api_key = "dummy"

            def complete(self, *a, **k):
                return '{"npc_updates": [{"name": "王芳"}]}'

        def _fake_apply(self, extraction):
            # 必须真的写库：extract 之后会 rebuild_all，它从业务表重建索引
            from book.models import NPC as _NPC
            self.repo.upsert_npc(_NPC(name="王芳", gender="女"))
            return {"npcs": ["王芳"]}

        cm.CardManager.apply = _fake_apply
        cli.LLMClient = _KeyedLLM
        try:
            res = cli.cmd_extract(cli.build_parser().parse_args(
                ["--db", str(db), "extract", "--range", "1-5", "--json"]))
        finally:
            cli.LLMClient = saved_llm
            cm.CardManager.apply = saved_apply
        check("extract 批量抽取成功", res["status"] == "ok" and res["extracted"] == 1,
              f"实际 {res}")
        check("extract 回传处理过的轮次", res["rounds"] == [1], f"实际 {res['rounds']}")

        repo2 = Repository(db)
        repo2.init_db()
        docs = repo2.conn.execute(
            "SELECT COUNT(*) AS c FROM search_docs WHERE source='npc'").fetchone()["c"]
        check("extract 后实体索引被重建", docs >= 1, f"实际 {docs}")
        repo2.close()

        # 空区间
        res_empty = cli.cmd_extract(cli.build_parser().parse_args(
            ["--db", str(db), "extract", "--range", "100-200", "--json"]))
        check("空区间返回 extracted=0 且给出提示",
              res_empty["extracted"] == 0 and len(res_empty["warnings"]) >= 1)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)




# ─── 测试 8: 独立审查缺陷回归（D1–D11） ──────────────────────────────────

def test_review_regressions():
    section("8. 审查缺陷回归 (extract 幂等/HP/子串/碰撞/解析)")

    from book import cli
    from book.db.repository import Repository
    from book.executor import consistency
    from book.memory.vector import DIM, MIN_SHARED_FEATURES, cosine, sparse_embed
    from book.models import NPC

    tmpdir = tempfile.mkdtemp()
    try:
        # ── D4：extract 必须幂等（只处理未抽取的轮次） ──
        db = Path(tmpdir) / "d4.db"
        import book.executor.card_manager as cm

        saved_llm = cli.LLMClient
        saved_apply = cm.CardManager.apply

        class _KeyedLLM(saved_llm):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.api_key = "dummy"

            def complete(self, *a, **k):
                return '{"npc_updates": [{"name": "王芳", "affection_change": 5}]}'

        calls = {"n": 0}

        def _counting_apply(self, extraction):
            calls["n"] += 1
            # CardManager 通过 memory 管理器写记忆（不是 repo.add_temp_memory）
            self.memory.add_temp_memory("2026/07/01", "A", "事件", "结果")
            return {"npcs": ["王芳"]}

        cli.LLMClient = _KeyedLLM
        cm.CardManager.apply = _counting_apply
        try:
            cli.cmd_ingest(cli.build_parser().parse_args(
                ["--db", str(db), "ingest", "--text", "第一轮正文内容。",
                 "--chapter", "1", "--no-extract", "--json"]))
            first = cli.cmd_extract(cli.build_parser().parse_args(
                ["--db", str(db), "extract", "--range", "1-5", "--json"]))
            second = cli.cmd_extract(cli.build_parser().parse_args(
                ["--db", str(db), "extract", "--range", "1-5", "--json"]))

            check("D4 首次 extract 处理了 1 轮", first["extracted"] == 1, f"实际 {first}")
            check("D4 二次 extract 不再重复抽取",
                  second["extracted"] == 0 and calls["n"] == 1,
                  f"实际 extracted={second['extracted']} apply调用={calls['n']}")
            check("D4 二次 extract 说明跳过了已抽取轮次",
                  any("已抽取" in w for w in second["warnings"]),
                  f"实际 {second['warnings']}")
            check("D4 二次 extract 回传 skipped 数", second.get("skipped") == 1,
                  f"实际 {second.get('skipped')}")

            forced = cli.cmd_extract(cli.build_parser().parse_args(
                ["--db", str(db), "extract", "--range", "1-5", "--force", "--json"]))
            check("D4 --force 才允许重抽", forced["extracted"] == 1 and calls["n"] == 2,
                  f"实际 {forced} calls={calls['n']}")
        finally:
            cli.LLMClient = saved_llm
            cm.CardManager.apply = saved_apply

        repo = Repository(db)
        repo.init_db()
        marked = repo.conn.execute(
            "SELECT COUNT(*) AS c FROM session_history WHERE extracted_at IS NOT NULL"
        ).fetchone()["c"]
        check("D4 抽取后写入 extracted_at 标记", marked == 1, f"实际 {marked}")
        repo.close()

        # ── D1：HP/EN 越界必须读 player 表 ──
        d1_dir = Path(tmpdir) / "d1"
        d1_dir.mkdir()
        r1 = _tmp_repo(str(d1_dir), "d1.db")
        r1.update_player(hp=999, en=-5)
        details = [i.detail for i in consistency.run_checks(r1)]
        check("D1 player 表 HP 越界被报出",
              any("HP=999" in d for d in details), f"实际 {details}")
        check("D1 player 表 EN 越界被报出",
              any("EN=-5" in d for d in details), f"实际 {details}")
        r1.update_player(hp=100, en=100)
        check("D1 正常值时不再报数值问题",
              not any("HP=" in i.detail or "EN=" in i.detail
                      for i in consistency.run_checks(r1)))
        r1.close()

        # ── D3：NPC 名字子串假阳性 ──
        r3 = _tmp_repo(tmpdir, "d3.db")
        r3.upsert_npc(NPC(name="林", gender="女", is_present=False))
        r3.upsert_npc(NPC(name="林越", gender="男", is_present=False))
        r3.upsert_npc(NPC(name="王芳", gender="女", is_present=True))
        sess = [{"id": 1, "round_number": 1,
                 "narrative": "林越和林越山都来了。", "story_state_json": "{}"}]
        d3 = [i.detail for i in consistency.check_state(r3, sess)]
        check("D3 单字名「林」不再被子串误命中",
              not any("「林」" in d for d in d3), f"实际 {d3}")
        check("D3 多字名「林越」仍正常计数",
              any("「林越」" in d for d in d3), f"实际 {d3}")
        r3.close()

        # ── D2：哈希碰撞 ──
        check("D2 维度已提升到 >= 65536", DIM >= 65536, f"实际 {DIM}")
        check("D2 要求至少 2 个共享特征", MIN_SHARED_FEATURES >= 2)
        for w1, w2 in [("信息", "动作"), ("对方", "母亲"), ("收入", "墙壁")]:
            check(f"D2 碰撞对 {w1}/{w2} 不再满分",
                  cosine(sparse_embed(w1), sparse_embed(w2)) == 0.0)
        check("D2 真实改写仍能召回",
              cosine(sparse_embed("机房的服务器"), sparse_embed("服务器机房")) > 0.3)
        s = sparse_embed("测试文本内容")
        check("D2 余弦自身相似度恒为 1.0（已夹紧）", cosine(s, s) == 1.0)
        check("D2 余弦值域被夹紧到 [0,1]",
              all(0.0 <= cosine(sparse_embed(a), sparse_embed(b)) <= 1.0
                  for a in ("机房", "林越") for b in ("机房", "林越山")))

        # ── D6：--deep 的模型输出必须按枚举收敛 ──
        r6 = _tmp_repo(tmpdir, "d6.db")
        r6.upsert_npc(NPC(name="某人", gender="女"))
        r6.add_session(1, "", "正文内容。", {})
        merged = consistency.deep_check(
            r6, [],
            llm_call=lambda p: '{"issues":[{"kind":"banana","severity":"critical",'
                                    '"detail":"模型胡说的","refs":"not-a-list"}]}',
        )
        check("D6 非法 kind 被收敛为 state",
              merged and merged[0].kind == "state", f"实际 {[i.kind for i in merged]}")
        check("D6 非法 severity 被收敛为 mid",
              merged and merged[0].severity == "mid", f"实际 {[i.severity for i in merged]}")
        check("D6 非法 refs 被收敛为空列表",
              merged and merged[0].refs == [], f"实际 {merged[0].refs if merged else None}")
        many = consistency.deep_check(
            r6, [consistency.Issue("state", "low", "低")],
            llm_call=lambda p: '{"issues":[{"kind":"state","severity":"high","detail":"高"}]}',
        )
        check("D6 合并后仍按严重度排序",
              [i.severity for i in many] == ["high", "low"],
              f"实际 {[i.severity for i in many]}")
        r6.close()

        # ── D7/D8：时间解析 ──
        check("D7 非法分钟（08:99）返回 None 而不是当 00:00",
              consistency.parse_story_time("2026年06月22日", "上午 08:99") is None)
        check("D7 非法小时（25:00）返回 None",
              consistency.parse_story_time("2026年06月22日", "25:00") is None)
        check("D8 日期串里内嵌的时刻被正确取用",
              consistency.parse_story_time("2026-06-22 14:00", "") == (2026, 6, 22, 14, 0))
        check("D8 内嵌时刻可检出同日倒退",
              len(consistency.check_timeline([
                  {"id": 1, "round_number": 1, "volume": None,
                   "story_state_json": '{"current_date":"2026-06-22 18:00","current_time":""}'},
                  {"id": 2, "round_number": 2, "volume": None,
                   "story_state_json": '{"current_date":"2026-06-22 09:00","current_time":""}'},
              ])) == 1)

        # ── D9：时间线按卷分组 ──
        def s(sid, rn, vol, date, time):
            return {"id": sid, "round_number": rn, "volume": vol,
                    "story_state_json": json.dumps(
                        {"current_date": date, "current_time": time})}

        cross_vol = [s(1, 1, 1, "2026年06月22日", "下午 14:00"),
                     s(2, 2, 2, "2026年05月01日", "上午 08:00")]
        check("D9 跨卷时间回退不再误报", consistency.check_timeline(cross_vol) == [],
              f"实际 {[i.detail for i in consistency.check_timeline(cross_vol)]}")
        same_vol = [s(1, 1, 1, "2026年06月22日", "下午 14:00"),
                    s(2, 2, 1, "2026年06月22日", "上午 08:00")]
        check("D9 同卷内倒挂仍然报出", len(consistency.check_timeline(same_vol)) == 1)

        # ── D10：参数校验 ──
        try:
            cli._parse_range("5-1")
            raised = False
        except cli.CliError as exc:
            raised = exc.kind == "usage"
        check("D10 反向 --range 报 usage 错误", raised)
        clean_dir = Path(tmpdir) / "d10"
        clean_dir.mkdir()
        try:
            cli.cmd_check(cli.build_parser().parse_args(
                ["--db", str(clean_dir / "x.db"), "check",
                 "--foreshadow-stale", "-1", "--json"]))
            raised = False
        except cli.CliError as exc:
            raised = exc.kind == "usage"
        check("D10 负数 --foreshadow-stale 报 usage 错误", raised)

        # ── D11：空名 NPC ──
        r11 = _tmp_repo(tmpdir, "d11.db")
        r11.conn.execute("INSERT INTO npcs (name, gender, is_present) VALUES ('', '女', 0)")
        r11.conn.commit()
        d11 = consistency.check_state(
            r11, [{"id": 1, "round_number": 1, "narrative": "一段正文。",
                   "story_state_json": "{}"}])
        check("D11 空名 NPC 不产生假问题",
              all("「」" not in i.detail for i in d11),
              f"实际 {[i.detail for i in d11]}")
        r11.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

# ─── 主入口 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    for _test in (
        test_vector,
        test_semantic_search,
        test_timeline,
        test_state_check,
        test_foreshadow_check,
        test_check_integration,
        test_cost_optimization,
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
    (out_dir / "test_p2_report.txt").write_text(output, encoding="utf-8")

    try:
        print(output)
    except UnicodeEncodeError:
        print(output.replace("✅", "[PASS]").replace("❌", "[FAIL]"))

    print(f"\n报告已保存到 {out_dir / 'test_p2_report.txt'}")
    sys.exit(0 if _failed == 0 else 1)
