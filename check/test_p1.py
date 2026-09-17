"""BookWorld P1 测试——卷/章/场景/POV、提及驱动注入、伏笔与别名。

风格与 test_runner.py / test_p0.py 一致（自研 check/section/result_summary，无 pytest 依赖）。
不依赖 DEEPSEEK_API_KEY：所有 LLM 路径 mock 或绕过。
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# ─── 测试辅助（与既有套件同风格） ──────────────────────────────────────────

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


def _tmp_repo(tmpdir: str, name: str = "p1.db"):
    from book.db.repository import Repository

    repo = Repository(Path(tmpdir) / name)
    repo.init_db()
    return repo


# ─── 测试 1: 卷 / 章 / 场景 层级 ──────────────────────────────────────────

def test_structure():
    section("1. 卷/章/场景 层级 (schema + repository)")

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)
        conn = repo.conn

        # 表存在
        for t in ("volumes", "chapters", "scenes", "foreshadows", "entity_aliases"):
            check(f"表 '{t}' 存在",
                  conn.execute(
                      "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
                  ).fetchone() is not None)

        # story_state 增列
        ss_cols = {r[1] for r in conn.execute("PRAGMA table_info(story_state)")}
        check("story_state 有 current_volume", "current_volume" in ss_cols)
        check("story_state 有 current_pov", "current_pov" in ss_cols)

        # 卷
        v1 = repo.upsert_volume(1, "第一卷 都市", "林越隐藏身份")
        v1b = repo.upsert_volume(1, "第一卷 改名", "新简介")
        check("同卷号 upsert 返回同一行", v1 == v1b)
        check("卷标题被更新", repo.get_volume(1)["title"] == "第一卷 改名")
        repo.upsert_volume(2, "第二卷")
        check("列出全部卷按卷号排序", [v["idx"] for v in repo.get_all_volumes()] == [1, 2])

        # 章
        c1 = repo.upsert_chapter(1, volume_idx=1, title="第一章", pov="林越", goal="开场")
        c1b = repo.upsert_chapter(1, volume_idx=1, title="第一章 修订", pov="赵萍",
                                  status="完稿", word_count=3200)
        check("同 (卷,章) upsert 返回同一行", c1 == c1b)
        ch = repo.get_chapter(1, volume_idx=1)
        check("章标题被更新", ch["title"] == "第一章 修订")
        check("章 POV/状态/字数被更新",
              (ch["pov"], ch["status"], ch["word_count"]) == ("赵萍", "完稿", 3200))
        repo.upsert_chapter(2, volume_idx=1, title="第二章")
        repo.upsert_chapter(3, volume_idx=2, title="第三章")
        check("按卷过滤章", [c["idx"] for c in repo.list_chapters(volume_idx=1)] == [1, 2])
        check("列出全部章按卷章排序",
              [(c["volume_idx"], c["idx"]) for c in repo.list_chapters()] == [(1, 1), (1, 2), (2, 3)])
        check("同一章号在不同卷互不覆盖",
              repo.get_chapter(3, volume_idx=2)["title"] == "第三章")

        # 场景
        s1 = repo.upsert_scene(1, 1, location="出租屋", pov="林越", summary="开场")
        s1b = repo.upsert_scene(1, 1, location="出租屋-卧室", pov="林越", summary="修订")
        check("同 (章,场景) upsert 返回同一行", s1 == s1b)
        check("场景地点被更新", repo.list_scenes(1)[0]["location"] == "出租屋-卧室")
        repo.upsert_scene(1, 2, location="公司", pov="林越")
        check("按章列出场景", len(repo.list_scenes(1)) == 2)

        # story_state 读写 P1 列
        repo.update_story_state(current_volume=2, current_pov="赵萍")
        ss = repo.get_story_state()
        check("current_volume 可读写", ss.current_volume == 2)
        check("current_pov 可读写", ss.current_pov == "赵萍")
        check("story_state 序列化含 P1 字段",
              ss.model_dump().get("current_volume") == 2 and ss.model_dump().get("current_pov") == "赵萍")

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 2: 伏笔 ─────────────────────────────────────────────────────────

def test_foreshadows():
    section("2. 伏笔表 (foreshadows)")

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)

        fid = repo.add_foreshadow("青铜钥匙", detail="第八章出现", planted_volume=1,
                                  planted_chapter=8, planned_recovery="用它开地下室")
        check("新增伏笔返回 id", isinstance(fid, int))
        open_list = repo.list_foreshadows(status="未收")
        check("默认状态为未收", open_list[0]["status"] == "未收")
        check("伏笔字段完整",
              (open_list[0]["name"], open_list[0]["planted_chapter"],
               open_list[0]["planned_recovery"]) == ("青铜钥匙", 8, "用它开地下室"))

        repo.add_foreshadow("神秘短信", planted_chapter=12)
        check("可有多条未收伏笔", len(repo.list_foreshadows(status="未收")) == 2)
        check("不带过滤时列出全部", len(repo.list_foreshadows()) == 2)

        changed = repo.close_foreshadow("青铜钥匙", recovered_chapter=20)
        check("收线返回受影响行数 1", changed == 1)
        check("收线后不在未收列表",
              [f["name"] for f in repo.list_foreshadows(status="未收")] == ["神秘短信"])
        closed = [f for f in repo.list_foreshadows() if f["name"] == "青铜钥匙"][0]
        check("收线写入状态与回收章",
              closed["status"] == "已收" and closed["recovered_chapter"] == 20)
        check("重复收线返回 0（幂等）", repo.close_foreshadow("青铜钥匙") == 0)
        check("收不存在的伏笔返回 0", repo.close_foreshadow("根本没有") == 0)

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 3: 实体别名 ─────────────────────────────────────────────────────

def test_aliases():
    section("3. 实体别名 (entity_aliases)")

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)

        repo.add_alias("npc", "赵萍", "萍萍")
        repo.add_alias("npc", "赵萍", "赵姐")
        repo.add_alias("npc", "赵萍", "萍萍")  # 重复
        check("别名按实体归组", repo.get_aliases().get("赵萍") == ["萍萍", "赵姐"],
              f"实际 {repo.get_aliases()}")
        check("重复别名不产生重复行", len(repo.list_aliases()) == 2)
        check("别名带类型", all(a["entity_type"] == "npc" for a in repo.list_aliases()))

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 4: 提及驱动注入 ─────────────────────────────────────────────────

def test_mention_driven_context():
    section("4. 提及驱动注入 (writer/context_builder.py)")

    from book.models import NPC
    from book.writer.context_builder import build_context

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)
        repo.update_player(name="林越")
        repo.upsert_npc(NPC(name="赵萍", gender="女", occupation="主播", is_present=False,
                            appearance="长发", personality="精明"))
        repo.upsert_npc(NPC(name="王芳", gender="女", occupation="文员", is_present=True))
        repo.upsert_npc(NPC(name="路人甲", gender="男", occupation="路人", is_present=False,
                            appearance="与主线无关的描述" * 40))
        repo.upsert_npc(NPC(name="林", gender="女", occupation="单字名路人", is_present=False))
        repo.add_alias("npc", "赵萍", "萍萍")
        repo.upsert_item(__import__("book.models", fromlist=["Item"]).Item(
            category="道具", name="青铜钥匙"))
        repo.upsert_region(__import__("book.models", fromlist=["Region"]).Region(name="服务器机房"))

        # 4a. 未被提及且不在场的实体绝不出现
        pack = build_context(repo, pov="林越", scene="出租屋", budget_chars=6000)
        names = [e["name"] for e in pack.entities]
        check("POV 主角卡在 context 里", "林越" in names, f"实际 {names}")
        check("未被提及且不在场的 NPC 不出现", "路人甲" not in names, f"实际 {names}")
        check("未提及的道具不出现", "青铜钥匙" not in names, f"实际 {names}")
        check("未提及的区域不出现", "服务器机房" not in names, f"实际 {names}")

        # 4b. 在场 NPC 一定出现
        check("在场 NPC 出现", "王芳" in names, f"实际 {names}")
        check("present_npcs 只含在场的", pack.present_npcs == ["王芳"],
              f"实际 {pack.present_npcs}")

        # 4c. 被提及（名字）
        pack2 = build_context(repo, pov="林越", scene="赵萍的直播间", budget_chars=6000)
        check("被提及实体（名字）进入 context",
              "赵萍" in [e["name"] for e in pack2.entities],
              f"实际 {[e['name'] for e in pack2.entities]}")

        # 4d. 被提及（别名）
        pack3 = build_context(repo, pov="林越", scene="出租屋", extra_text="我去找萍萍",
                              budget_chars=6000)
        check("别名命中也能进入 context",
              "赵萍" in [e["name"] for e in pack3.entities],
              f"实际 {[e['name'] for e in pack3.entities]}")

        # 4e. 单字名不做裸子串匹配
        pack4 = build_context(repo, pov="林越", scene="出租屋", budget_chars=6000)
        check("单字 NPC 名不被误提及",
              "林" not in [e["name"] for e in pack4.entities],
              f"实际 {[e['name'] for e in pack4.entities]}")

        # 4f. 关系卡只在"至少一端入选"时出现
        from book.models import Relationship
        repo.upsert_relationship(Relationship(character_a="赵萍", character_b="王芳",
                                              relation_type="上下级", description="雇佣"))
        repo.upsert_relationship(Relationship(character_a="张三", character_b="李四",
                                              relation_type="敌对", description="与谁都不相干"))
        # 王芳在场 → 赵萍→王芳 这一端已入选，应当出现；
        # 张三→李四 两端都没入选，必须不出现
        pack5 = build_context(repo, pov="林越", scene="出租屋", budget_chars=6000)
        names5 = [e["name"] for e in pack5.entities]
        check("与已入选实体相关的关系出现", "赵萍→王芳" in names5, f"实际 {names5}")
        check("两端都未入选的关系不出现", "张三→李四" not in names5, f"实际 {names5}")
        # 两个都在场的 NPC 之间的关系也必须选得中（回归：筛选顺序错误曾导致漏选）
        repo2 = _tmp_repo(tmpdir, "d4.db")
        from book.models import NPC as _NPC2
        repo2.update_player(name="主角")
        repo2.upsert_npc(_NPC2(name="王芳", gender="女", is_present=True))
        repo2.upsert_npc(_NPC2(name="赵萍", gender="女", is_present=True))
        repo2.upsert_relationship(Relationship(character_a="王芳", character_b="赵萍",
                                                relation_type="上下级", description="雇佣"))
        pack_d4 = build_context(repo2, pov="主角", scene="机房", budget_chars=6000)
        check("两个在场 NPC 之间的关系能被选中",
              "王芳→赵萍" in [e["name"] for e in pack_d4.entities],
              f"实际 {[(e['type'], e['name']) for e in pack_d4.entities]}")
        repo2.close()
        pack6 = build_context(repo, pov="林越", scene="赵萍的直播间", budget_chars=6000)
        check("相关关系在有实体入选时出现",
              any(e["type"] == "relationship" for e in pack6.entities),
              f"实际 {[(e['type'], e['name']) for e in pack6.entities]}")

        # 4g. render() 产出可拼进 prompt 的文本
        text = pack2.render()
        check("render 含场景", "赵萍的直播间" in text)
        check("render 含实体卡", "赵萍" in text)
        check("render 是非空文本", len(text) > 20)

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 5: 伏笔进入 context ─────────────────────────────────────────────

def test_foreshadow_in_context():
    section("5. 未收伏笔进入 context (P1 验收项)")

    from book.writer.context_builder import build_context

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)
        repo.update_player(name="林越")
        repo.add_foreshadow("青铜钥匙", detail="第八章出现", planted_chapter=8)
        repo.add_foreshadow("神秘短信", planted_chapter=12)
        repo.add_foreshadow("已收的伏笔", planted_chapter=3)
        repo.close_foreshadow("已收的伏笔", recovered_chapter=9)

        pack = build_context(repo, pov="林越", scene="出租屋", budget_chars=6000)
        names = [f["name"] for f in pack.foreshadows]
        check("未收伏笔被 context 收录", "青铜钥匙" in names and "神秘短信" in names,
              f"实际 {names}")
        check("已收伏笔不进 context", "已收的伏笔" not in names, f"实际 {names}")
        check("伏笔项含 planted_chapter",
              all("planted_chapter" in f for f in pack.foreshadows))
        check("render 里出现未收伏笔", "青铜钥匙" in pack.render())

        # CLI 也能列出未收伏笔（验收命令 bookctl foreshadow list --json）
        from book import cli

        tmp_db = Path(tmpdir) / "cli.db"
        repo.close()
        repo2 = _tmp_repo(tmpdir, "cli.db")
        repo2.add_foreshadow("CLI伏笔", planted_chapter=5)
        repo2.close()
        args = cli.build_parser().parse_args(
            ["--db", str(tmp_db), "foreshadow", "list", "--status", "未收", "--json"]
        )
        payload = cli.cmd_foreshadow(args)
        check("CLI foreshadow list 列出未收伏笔",
              [f["name"] for f in payload["foreshadows"]] == ["CLI伏笔"],
              f"实际 {payload}")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 6: 预算约束（100 轮合成数据） ───────────────────────────────────

def test_budget_with_100_rounds():
    section("6. 100 轮数据下的预算约束 (P1 验收项)")

    from book.db import search as S
    from book.models import NPC
    from book.writer.context_builder import build_context

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)
        repo.update_player(name="林越")
        for i in range(8):
            repo.upsert_npc(NPC(name=f"角色{i}", gender="女", occupation=f"职业{i}",
                                is_present=(i < 2), appearance="外貌描述" * 20,
                                inner_thought="内心想法" * 10))

        # 合成 100 轮，每轮 200 字正文
        for rn in range(1, 101):
            text = f"第{rn}轮正文。" + "情节推进内容。" * 20
            sid = repo.add_session(rn, "", text, {}, chapter=rn, pov="林越")
            S.index_session(repo.conn, sid, text, chapter=rn, pov="林越")
        check("合成 100 轮入库", repo.get_round_count() == 100)

        for budget in (200, 1000, 3000, 6000, 20000):
            pack = build_context(repo, pov="林越", scene="出租屋", chapter=100,
                                 budget_chars=budget)
            check(f"预算 {budget} 下 prompt_chars ≤ budget",
                  pack.prompt_chars <= budget,
                  f"实际 {pack.prompt_chars}")
            check(f"预算 {budget} 下 trimmed 如实报告被丢弃项",
                  all(isinstance(t, str) for t in pack.trimmed))
            # prompt_chars 应等于被保留分区之和，而不是全部
            if pack.trimmed:
                check(f"预算 {budget} 下确有分区被裁剪", len(pack.trimmed) >= 1)

        # 小预算时高优先级仍需保留：实体卡按逐条裁剪，POV 卡必须活下来
        tiny = build_context(repo, pov="林越", scene="出租屋", budget_chars=200)
        check("小预算下 POV 主角卡仍保留",
              any(e["name"] == "林越" for e in tiny.entities),
              f"实际 {[(e['type'], e['name']) for e in tiny.entities]}")
        check("小预算下 prompt_chars 不超预算", tiny.prompt_chars <= 200,
              f"实际 {tiny.prompt_chars}")
        check("小预算下最低优先级被丢弃", len(tiny.trimmed) >= 1, f"实际 {tiny.trimmed}")

        # 逐条裁剪：预算越小，保留的实体卡越少（单调性）
        sizes = []
        for b in (120, 160, 200, 300, 600):
            p = build_context(repo, pov="林越", scene="出租屋", budget_chars=b)
            sizes.append(len(p.entities))
        check("实体卡数量随预算单调不减", sizes == sorted(sizes), f"实际 {sizes}")

        # 大预算时 7 个分区都装得下
        big = build_context(repo, pov="林越", scene="出租屋", chapter=100, budget_chars=60000)
        check("大预算下无裁剪", big.trimmed == [], f"实际 {big.trimmed}")
        check("大预算下含最近 3 轮", len(big.memory.get("recent", [])) == 3,
              f"实际 {len(big.memory.get('recent', []))}")
        check("每轮尾段不超过 300 字",
              all(len(t) <= 300 for t in big.memory.get("recent", [])))
        check("检索补充不超过 3 条", len(big.retrieved) <= 3)

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 7: 回归与兼容 ───────────────────────────────────────────────────

def test_regression():
    section("7. 兼容性回归 (build_writer_context / BookEngine)")

    from book.writer.prompt import build_writer_context, build_user_prompt
    from book.models import StoryState, Player

    ss = StoryState(current_scene="旧仓库")
    p = Player(title="冒险者")
    ctx = build_writer_context(ss, p, "测试记忆",
                               [{"narrative": "上一轮剧情"}], "- 助手")
    check("build_writer_context 仍可用（P1-3 要求保留）", len(ctx) > 0)
    check("旧接口仍含场景", "旧仓库" in ctx)
    check("build_user_prompt 仍可用", "续写" in build_user_prompt("探索"))

    # BookEngine._prepare_context 已改走 build_context，且受预算约束
    from book import main as book_main

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)
        repo.update_player(name="林越")
        repo.upsert_npc(__import__("book.models", fromlist=["NPC"]).NPC(
            name="在场者", gender="女", is_present=True))

        class _Engine(book_main.BookEngine):
            def __init__(self, repo):  # noqa: D107 - 测试替身，绕开真库与 LLM
                self.repo = repo
                self.writer_system_prompt = ""

        engine = _Engine(repo)
        round_num, full_user = engine._prepare_context("我去机房")
        check("_prepare_context 返回轮次号", round_num == 1, f"实际 {round_num}")
        check("user prompt 含玩家行动", "我去机房" in full_user)
        check("user prompt 含续写指令", "续写" in full_user)
        check("上下文含在场 NPC", "在场者" in full_user)
        check("上下文受预算约束（6000 字以内 + 指令）",
              len(full_user) <= book_main.CONTEXT_BUDGET_CHARS + 400,
              f"实际 {len(full_user)}")
        check("未提及且不在场实体不进 prompt",
              "不存在的人" not in full_user)
        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)




# ─── 测试 8: 预算一致性（独立审查 D1/D2 回归） ─────────────────────────────

def test_budget_accounting():
    section("8. 预算记账与实际注入一致 (D1/D2 回归)")

    from book.writer.context_builder import build_context

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)
        repo.update_player(name="林越")
        from book.models import NPC
        repo.upsert_npc(NPC(name="在场者", gender="女", is_present=True,
                            appearance="外貌" * 30))
        for rn in range(1, 4):
            repo.add_session(rn, "", f"第{rn}轮正文内容。", {})
        repo.update_plot_summary("摘要" * 200)          # 400 字摘要
        repo.add_foreshadow("伏笔甲", planted_chapter=3)
        repo.add_important_intel("情报一")

        # 覆盖"recent 装得下、memory 装不下"的边界组合
        for budget in (0, 1, 30, 69, 70, 100, 120, 150, 200, 400, 600, 2000):
            pack = build_context(repo, pov="林越", scene="出租屋",
                                 chapter=3, budget_chars=budget)
            rendered = pack.render()
            check(f"预算 {budget}：prompt_chars == len(render())",
                  pack.prompt_chars == len(rendered),
                  f"prompt_chars={pack.prompt_chars} len(render)={len(rendered)}")
            if budget >= 0:
                check(f"预算 {budget}：实际注入不超预算",
                      len(rendered) <= budget,
                      f"len(render)={len(rendered)}")

        # D1 关键场景：recent 与被裁的 memory 必须一致 —— 不能"记账却不输出"。
        # 逐一扫描预算，保证 trimmed 里出现的分区确实没进 render，
        # 且 render 里出现的分区都在 prompt_chars 里算过账。
        for budget in (100, 120, 150, 200, 400, 600, 700, 2000):
            pack = build_context(repo, pov="林越", scene="出租屋",
                                 chapter=3, budget_chars=budget)
            rendered = pack.render()
            check(f"预算 {budget}：prompt_chars == len(render())",
                  pack.prompt_chars == len(rendered),
                  f"{pack.prompt_chars} vs {len(rendered)}")
            if "recent_narratives" in pack.trimmed:
                check(f"预算 {budget}：recent 被裁时 memory.recent 为空且 render 无最近剧情",
                      not pack.memory.get("recent") and "【最近剧情】" not in rendered)
            elif pack.memory.get("recent"):
                check(f"预算 {budget}：recent 入选时 render 含最近剧情",
                      "【最近剧情】" in rendered)
            if "memory" in pack.trimmed:
                check(f"预算 {budget}：memory 被裁时 render 无摘要/情报",
                      "【剧情摘要】" not in rendered and "【情报】" not in rendered)
            if "scene" in pack.trimmed:
                check(f"预算 {budget}：scene 被裁时 render 无场景行",
                      "【当前场景】" not in rendered)

        # D2：超长 scene 也必须受预算约束
        huge_scene = "场" * 5000
        pack_huge = build_context(repo, pov="林越", scene=huge_scene, budget_chars=100)
        check("D2 超长 scene 不绕过预算",
              len(pack_huge.render()) <= 100,
              f"len(render)={len(pack_huge.render())}")
        check("D2 超长 scene 被丢弃并记录",
              pack_huge.scene == "" and "scene" in pack_huge.trimmed,
              f"scene 长度={len(pack_huge.scene)} trimmed={pack_huge.trimmed}")
        check("D2 prompt_chars 仍与实际一致",
              pack_huge.prompt_chars == len(pack_huge.render()))

        # 小 scene 正常保留
        pack_small = build_context(repo, pov="林越", scene="出租屋", budget_chars=6000)
        check("正常长度 scene 被保留", pack_small.scene == "出租屋")
        check("scene 出现在 render 里", "出租屋" in pack_small.render())

        # D5：关系卡扇出上限
        repo2 = _tmp_repo(tmpdir, "fanout.db")
        from book.models import NPC as _NPC, Relationship
        repo2.update_player(name="林越")
        repo2.upsert_npc(_NPC(name="枢纽", gender="女", is_present=True))
        for i in range(40):
            repo2.upsert_npc(_NPC(name=f"角色{i}", gender="男"))
            repo2.upsert_relationship(Relationship(character_a="枢纽", character_b=f"角色{i}",
                                                    relation_type="其他", description="x"))
        pack_fan = build_context(repo2, pov="林越", scene="x", budget_chars=6000)
        rels = [e for e in pack_fan.entities if e["type"] == "relationship"]
        check("D5 关系卡有数量上限", len(rels) <= 8, f"实际 {len(rels)}")
        check("D5 扇出不会挤掉在场 NPC", "枢纽" in pack_fan.present_npcs,
              f"实际 {pack_fan.present_npcs}")
        repo2.close()

        # D6：POV 角色 is_present=1 时仍拿完整卡
        repo3 = _tmp_repo(tmpdir, "pov.db")
        repo3.upsert_npc(_NPC(name="赵萍", gender="女", is_present=True,
                              appearance="长发及腰", personality="精明"))
        pack_pov = build_context(repo3, pov="赵萍", scene="机房", budget_chars=6000)
        card = pack_pov.entities[0]["card"]
        check("D6 POV 是在场 NPC 时仍给完整卡",
              "外貌" in card and "性格" in card, f"实际 {card}")
        repo3.close()

        # D7：别名类型隔离
        from book.models import Item
        repo4 = _tmp_repo(tmpdir, "alias.db")
        repo4.update_player(name="林越")
        repo4.upsert_npc(_NPC(name="赵萍", gender="女"))
        repo4.upsert_item(Item(category="道具", name="备用钥匙"))
        repo4.add_alias("npc", "赵萍", "spare_key_7")
        repo4.add_alias("player", "林越", "康康")
        pack_a = build_context(repo4, pov="林越", scene="x", extra_text="spare_key_7 出现了",
                               budget_chars=6000)
        names_a = [e["name"] for e in pack_a.entities]
        check("D7 npc 别名命中 NPC", "赵萍" in names_a, f"实际 {names_a}")
        check("D7 npc 别名不误命中同名道具", "备用钥匙" not in names_a, f"实际 {names_a}")
        pack_b = build_context(repo4, pov="赵萍", scene="x", extra_text="康康来了",
                               budget_chars=6000)
        check("D7 player 别名能把主角卡拉进来",
              any(e["type"] == "player" for e in pack_b.entities),
              f"实际 {[(e['type'], e['name']) for e in pack_b.entities]}")
        repo4.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 9: CLI 参数校验与迁移健壮性（D8/D9 回归） ────────────────────────

def test_cli_and_migration_robustness():
    section("9. CLI 参数校验 / migrate 健壮性 (D8/D9 回归)")

    import sqlite3

    from book import cli
    from book.db.migrations import migrate

    tmpdir = tempfile.mkdtemp()
    try:
        db = Path(tmpdir) / "robo.db"

        # D8：chapter add 缺 --chapter 必须是 usage 错误，且不留下副作用
        for argv, label in [
            (["chapter", "add", "--volume", "2", "--json"], "缺 --chapter"),
            (["alias", "add", "--entity-type", "怪兽", "--entity-id", "x",
              "--alias", "y", "--json"], "非法 --entity-type"),
        ]:
            try:
                cli.main(["--db", str(db), *argv])
                failed = False
            except SystemExit as exc:
                failed = exc.code != 2
            check(f"D8 {label} 报 usage 错误", not failed, "未按 usage 处理")

        args = cli.build_parser().parse_args(["--db", str(db), "chapter", "add", "--volume", "2", "--json"])
        try:
            cli.cmd_chapter(args)
            raised = False
        except cli.CliError as exc:
            raised = exc.kind == "usage" and exc.code == 2
        check("D8 chapter add 缺 --chapter 抛 CliError(usage)", raised)

        from book.db.repository import Repository as _Repo
        repo = _Repo(db)
        repo.init_db()
        check("D8 失败的 chapter add 没有留下空卷",
              repo.get_volume(2) is None, f"实际 {repo.get_all_volumes()}")
        repo.close()

        # D9/D3：裸 sqlite3 连接调 migrate 不崩，且会建出 P1 表
        repo2 = _Repo(Path(tmpdir) / "m.db")
        repo2.init_db()
        repo2.close()
        raw = sqlite3.connect(str(Path(tmpdir) / "m.db"))  # 无 row_factory
        first = migrate(raw)
        second = migrate(raw)
        check("D3 裸 sqlite3 连接调 migrate 不抛异常", isinstance(first, dict))
        check("D3 裸连接二次 migrate 幂等",
              second["added_columns"] == [] and second["added_indexes"] == []
              and second["dropped_indexes"] == [],
              f"实际 {second}")
        raw.close()

        empty = sqlite3.connect(":memory:")
        report = migrate(empty)
        tables = {r[0] for r in empty.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        check("D9 空库单独 migrate 会建出 P1 五表",
              {"volumes", "chapters", "scenes", "foreshadows", "entity_aliases"} <= tables,
              f"实际 {sorted(tables)}")
        check("D9 报告里标明建了 P1 结构", report.get("p1_schema") is True, f"实际 {report}")
        check("D9 关键表齐全（含 P0 与 P1）",
              {"session_history", "story_state"} <= tables,
              f"实际 {sorted(tables)}")
        empty.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)




# ─── 测试 10: --chapter 语义（P1 层级真正参与上下文） ─────────────────────

def test_chapter_drives_context():
    section("10. --chapter 驱动上下文 (卷章层级生效)")

    from book.models import Region, DangerLevel, SecurityLevel
    from book.writer.context_builder import build_context

    tmpdir = tempfile.mkdtemp()
    try:
        repo = _tmp_repo(tmpdir)
        repo.update_player(name="林越")
        repo.upsert_region(Region(name="服务器机房", security=SecurityLevel.GRAY,
                                  danger=DangerLevel.LOW))
        repo.upsert_region(Region(name="直播间"))

        repo.upsert_chapter(1, volume_idx=1, title="出租屋的早晨", pov="林越",
                            goal="交代现状")
        repo.upsert_scene(1, 1, location="出租屋", pov="林越")
        repo.upsert_chapter(13, volume_idx=2, title="机房里的日志", pov="林越",
                            goal="查到被删的日志")
        repo.upsert_scene(13, 1, location="服务器机房", pov="林越")
        repo.upsert_scene(13, 2, location="地下车库", pov="林越")

        repo.add_foreshadow("青铜钥匙", planted_chapter=8, planted_volume=2)
        repo.add_foreshadow("神秘短信", planted_chapter=12, planted_volume=2)
        repo.add_foreshadow("还没埋的钩子", planted_chapter=40)

        # 章头信息进入 context
        p13 = build_context(repo, pov="林越", chapter=13, budget_chars=6000)
        rendered13 = p13.render()
        check("章标题进入 context", "机房里的日志" in rendered13, rendered13[:120])
        check("章目标进入 context", "查到被删的日志" in rendered13)
        check("本章场景进入 context", "服务器机房" in rendered13 and "地下车库" in rendered13)
        check("chapter_title/goal/scenes 字段回传",
              p13.chapter_title == "机房里的日志"
              and p13.chapter_goal == "查到被删的日志"
              and p13.chapter_scenes == ["服务器机房", "地下车库"],
              f"实际 {p13.chapter_title!r} {p13.chapter_goal!r} {p13.chapter_scenes!r}")

        # 章号不同 → 输出必须不同（回归：--chapter 以前是空参）
        p1 = build_context(repo, pov="林越", chapter=1, budget_chars=6000)
        check("不同章号产生不同上下文", p1.render() != p13.render())

        # 场景表登记的"服务器机房"把同名区域卡带进来
        check("本章场景参与提及匹配（区域卡被带进来）",
              "服务器机房" in [e["name"] for e in p13.entities],
              f"实际 {[e['name'] for e in p13.entities]}")
        check("未在章场景里的区域不会被带入",
              "直播间" not in [e["name"] for e in p1.entities],
              f"实际 {[e['name'] for e in p1.entities]}")

        # 伏笔只收"已埋下"的
        names13 = [x["name"] for x in p13.foreshadows]
        check("第13章收录已埋伏笔", "青铜钥匙" in names13 and "神秘短信" in names13,
              f"实际 {names13}")
        check("第13章不收录未来钩子（第40章才埋）", "还没埋的钩子" not in names13,
              f"实际 {names13}")
        names1 = [x["name"] for x in p1.foreshadows]
        check("第1章不收录尚未埋下的伏笔", names1 == [], f"实际 {names1}")

        # 不显式给章号 → 不过滤伏笔（保持既有语义）
        p_none = build_context(repo, pov="林越", budget_chars=6000)
        check("不传章号时不过滤伏笔（保持既有语义）",
              len(p_none.foreshadows) == 3, f"实际 {[x['name'] for x in p_none.foreshadows]}")

        # 章节信息也受预算约束（超长标题不能绕过预算）
        repo.upsert_chapter(99, volume_idx=3, title="标" * 5000, pov="林越")
        pack99 = build_context(repo, pov="林越", chapter=99, budget_chars=200)
        check("超长章标题不绕过预算",
              len(pack99.render()) <= 200,
              f"len(render)={len(pack99.render())}")
        check("超长章标题被裁并记录",
              "chapter" in pack99.trimmed, f"实际 {pack99.trimmed}")
        check("prompt_chars 与 render 一致",
              pack99.prompt_chars == len(pack99.render()))

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 主入口 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    for _test in (
        test_structure,
        test_foreshadows,
        test_aliases,
        test_mention_driven_context,
        test_foreshadow_in_context,
        test_budget_with_100_rounds,
        test_regression,
        test_budget_accounting,
        test_cli_and_migration_robustness,
        test_chapter_drives_context,
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
    (out_dir / "test_p1_report.txt").write_text(output, encoding="utf-8")

    try:
        print(output)
    except UnicodeEncodeError:
        print(output.replace("✅", "[PASS]").replace("❌", "[FAIL]"))

    print(f"\n报告已保存到 {out_dir / 'test_p1_report.txt'}")
    sys.exit(0 if _failed == 0 else 1)
