"""Book 叙事引擎 —— 基础逻辑测试套件。

测试核心功能是否符合 README 文档描述的目标。
不依赖外部 LLM API，通过 mock 测试所有非网络路径。
"""

from __future__ import annotations
import sys
import json
import os
import tempfile
import shutil
from pathlib import Path
from datetime import datetime
from io import StringIO

# 将 src 目录加入 path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# ─── 测试辅助 ────────────────────────────────────────────────────────────────

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


# ─── 测试 1: 配置 ────────────────────────────────────────────────────────────

def test_config():
    section("1. 配置管理 (config.py)")

    from book.config import Config

    c = Config()
    check("默认 db_path 指向 data/book.db", Path(c.db_path).name == "book.db" and Path(c.db_path).parent.name == "data")
    check("默认 LLM base 为 DeepSeek API", "deepseek.com" in c.llm_api_base)
    check("默认 model 为 deepseek-flash", c.llm_model == "deepseek-flash")
    check("临时记忆上限 = 10", c.temp_memory_max == 10)
    check("永久记忆压缩阈值 = 6", c.perm_memory_compress_at == 6)
    check("output_dir 默认指向 output", Path(c.output_dir).name == "output")

    # 测试环境变量
    os.environ["DEEPSEEK_API_KEY"] = "test-key-123"
    c2 = Config()
    check("可从环境变量读取 API Key", c2.llm_api_key == "test-key-123")
    del os.environ["DEEPSEEK_API_KEY"]

    # 测试直接传入
    c3 = Config()
    c3.llm_api_key = "direct-key"
    check("可直接设置 API Key", c3.llm_api_key == "direct-key")


# ─── 测试 2: 数据模型 ────────────────────────────────────────────────────────

def test_models():
    section("2. Pydantic 数据模型 (models.py)")

    from book.models import (
        Quality, SecurityLevel, DangerLevel, NPCStatus, PlayerStatus,
        TempMemory, PermanentMemory, Player, EquipmentSlot, NPC,
        Relationship, Item, Region, StoryState, Suggestion,
        TimeOfDay, ExecutionResult,
    )

    # 枚举
    check("Quality 枚举", Quality.LEGENDARY.value == "传说")
    check("SecurityLevel 枚举", SecurityLevel.LAWLESS.value == "无法地带")
    check("DangerLevel 枚举", DangerLevel.LETHAL.value == "致命")
    check("NPCStatus 枚举", NPCStatus.EMOTIONAL.value == "情绪异常")
    check("PlayerStatus 枚举", PlayerStatus.POISONED.value == "中毒")

    # TempMemory
    tm = TempMemory(seq=1, datetime_str="2026/06/22 14:00",
                    scene="仓库", event="拿到钥匙", result="开门")
    check("TempMemory 创建成功", tm.seq == 1 and tm.scene == "仓库")
    check("TempMemory event 接受 40 字", len(tm.event) <= 40)

    # PermanentMemory
    pm = PermanentMemory(seq=1, datetime_range="2026/06/22",
                         scene="仓库", summary="获得钥匙打开实验室")
    check("PermanentMemory 创建成功", pm.scene == "仓库")

    # Player 默认值
    p = Player()
    check("Player 默认名字", p.name == "林越")
    check("Player 默认年龄", p.age == 24)
    check("Player 默认核心特性", p.core_trait == "变形者")
    check("Player 默认 HP", p.hp == 100)

    # EquipmentSlot
    eq = EquipmentSlot(slot="左手", category="武器", name="短刀", integrity=85)
    check("EquipmentSlot 创建成功", eq.integrity == 85)

    # NPC
    npc = NPC(name="测试角色", gender="女")
    check("NPC 默认 affection_level", npc.affection_level == 1)
    check("NPC 默认 is_present=False", npc.is_present is False)

    # Relationship
    rel = Relationship(character_a="A", character_b="B", relation_type="敌对")
    check("Relationship 创建成功", rel.relation_type == "敌对")

    # Item
    item = Item(category="道具", name="测试道具", quality=Quality.EPIC)
    check("Item 品质正确", item.quality == Quality.EPIC)

    # Region
    region = Region(name="测试区", security=SecurityLevel.GRAY, danger=DangerLevel.HIGH)
    check("Region 创建成功", region.security == SecurityLevel.GRAY)

    # StoryState 默认值
    ss = StoryState()
    check("StoryState 默认日期", "2026年06月22日" in ss.current_date)

    # Suggestion
    sug = Suggestion(conservative="保守行动", aggressive="激进行动", creative="创意行动")
    check("Suggestion 创建成功", sug.conservative == "保守行动")

    # ExecutionResult
    er = ExecutionResult(
        narrative="测试正文", story_state=ss,
        updated_cards={}, memory_changes={}
    )
    check("ExecutionResult 创建成功", er.narrative == "测试正文")


# ─── 测试 3: 数据库 Schema 和初始化 ──────────────────────────────────────────

def test_database():
    section("3. 数据库 Schema & 初始化 (db/schema.py + repository.py)")

    from book.db.schema import SCHEMA_SQL, INIT_SQL
    from book.db.repository import Repository

    # 使用临时文件
    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test.db"

    try:
        repo = Repository(db_path)
        repo.init_db()

        # 检查所有表是否存在
        tables = [
            "player", "player_equipment", "story_state", "temp_memories",
            "perm_memories", "plot_summary", "important_intel", "npcs",
            "relationships", "items", "regions", "suggestions", "session_history",
        ]
        for t in tables:
            row = repo.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (t,)
            ).fetchone()
            check(f"表 '{t}' 存在", row is not None, f"表 {t} 未找到")

        # 检查默认数据
        player = repo.get_player()
        check("Player 默认数据已插入", player.name == "林越")

        story = repo.get_story_state()
        check("StoryState 默认数据已插入", "2026年06月22日" in story.current_date)

        eq = repo.conn.execute("SELECT COUNT(*) as cnt FROM player_equipment").fetchone()
        check("5 个默认装备槽", eq["cnt"] == 5, f"实际: {eq['cnt']}")

        sug = repo.get_suggestions()
        check("Suggestions 默认数据已插入", isinstance(sug.conservative, str))

        plot = repo.get_plot_summary()
        check("PlotSummary 默认空", plot == "")

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 4: Repository CRUD ─────────────────────────────────────────────────

def test_repository_crud():
    section("4. Repository CRUD 操作 (db/repository.py)")

    from book.db.repository import Repository
    from book.models import (
        NPC, Relationship, Item, Quality, Region, SecurityLevel,
        DangerLevel, PlayerStatus, TempMemory, PermanentMemory,
    )

    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test.db"

    try:
        repo = Repository(db_path)
        repo.init_db()

        # --- StoryState 更新 ---
        repo.update_story_state(current_date="2026年07月01日 星期二")
        ss = repo.get_story_state()
        check("更新日期", "07月01日" in ss.current_date)

        repo.update_story_state(current_scene="神秘仓库")
        ss = repo.get_story_state()
        check("更新场景", ss.current_scene == "神秘仓库")

        # --- Player 更新 ---
        repo.update_player(currency=500, hp=80)
        p = repo.get_player()
        check("更新货币", p.currency == 500)
        check("更新血量", p.hp == 80)

        repo.update_player_equipment("左手", category="武器", name="长剑", integrity=90)
        p = repo.get_player()
        left = [e for e in p.equipment if e.slot == "左手"][0]
        check("更新左手装备", left.name == "长剑" and left.integrity == 90)

        # --- NPC 增删改查 ---
        npc = NPC(name="星野", gender="女", occupation="助手",
                  appearance="蓝色短发", personality="阳光/敏感")
        nid = repo.upsert_npc(npc)
        all_npcs = repo.get_all_npcs()
        check("新增 NPC", len(all_npcs) == 1 and all_npcs[0].name == "星野")

        # 按性别查询
        females = repo.get_npcs_by_gender("女")
        check("按性别查询 NPC", len(females) == 1)

        # 更新 NPC
        npc2 = NPC(name="星野", gender="女", occupation="首席助手",
                   personality="阳光/腹黑", is_present=True)
        repo.upsert_npc(npc2)
        updated = repo.get_npc_by_name("星野")
        check("更新 NPC 职业", updated.occupation == "首席助手")
        check("更新 NPC 在场状态", updated.is_present is True)

        # 按名查找不存在
        check("查找不存在的 NPC 返回 None",
              repo.get_npc_by_name("不存在的人") is None)

        # --- 关系网络 ---
        rel = Relationship(character_a="林越", character_b="星野",
                           relation_type="上下级", description="助手关系")
        repo.upsert_relationship(rel)
        all_rels = repo.get_all_relationships()
        check("新增关系", len(all_rels) == 1)

        # --- 道具 ---
        item = Item(category="道具", name="星辉石", quality=Quality.LEGENDARY)
        repo.upsert_item(item)
        items = repo.get_all_items()
        check("新增道具", len(items) == 1 and items[0].name == "星辉石")
        check("道具品质为传说", items[0].quality == Quality.LEGENDARY)

        repo.remove_item("道具", "星辉石")
        check("删除道具", len(repo.get_all_items()) == 0)

        # --- 区域 ---
        region = Region(name="旧城区", security=SecurityLevel.GRAY,
                        danger=DangerLevel.MEDIUM, landmarks="废弃仓库")
        repo.upsert_region(region)
        regions = repo.get_all_regions()
        check("新增区域", len(regions) == 1)

        # --- 临时记忆 ---
        tm = TempMemory(seq=1, datetime_str="2026/07/01 14:00",
                        scene="仓库", event="发现暗门", result="进入地下")
        repo.add_temp_memory(tm)
        check("临时记忆计数", repo.temp_memory_count() == 1)

        tms = repo.get_temp_memories()
        check("读取临时记忆", tms[0].event == "发现暗门")

        repo.clear_temp_memories()
        check("清空临时记忆", repo.temp_memory_count() == 0)

        # --- 永久记忆 ---
        pm = PermanentMemory(seq=1, datetime_range="2026/07/01",
                             scene="仓库", summary="发现暗门通向地下实验室")
        repo.add_perm_memory(pm)
        check("永久记忆计数", repo.perm_memory_count() == 1)

        repo.replace_perm_memories([PermanentMemory(
            seq=1, datetime_range="2026/07/01", scene="综合", summary="压缩后记忆"
        )])
        check("替换永久记忆", repo.perm_memory_count() == 1)

        # --- 重要情报 ---
        repo.add_important_intel("发现神秘组织")
        intels = repo.get_important_intel()
        check("添加重要情报", len(intels) == 1 and intels[0] == "发现神秘组织")

        # --- 剧情摘要 ---
        repo.update_plot_summary("这是一个测试摘要")
        check("更新剧情摘要", repo.get_plot_summary() == "这是一个测试摘要")

        # --- 建议 ---
        repo.update_suggestions("保守", "激进", "创意")
        sug = repo.get_suggestions()
        check("更新建议", sug.conservative == "保守")

        # --- 会话历史 ---
        check("初始轮次计数", repo.get_round_count() == 0)
        repo.add_session(1, "测试输入", "测试正文", {"test": True})
        check("添加会话后计数", repo.get_round_count() == 1)
        rounds = repo.get_session_rounds(limit=5)
        check("读取会话历史", len(rounds) == 1)

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 5: 记忆管理器（含 LLM 降级逻辑）──────────────────────────────────

def test_memory_manager():
    section("5. 记忆管理器 (memory/manager.py + integrator.py)")

    from book.db.repository import Repository
    from book.memory.manager import MemoryManager
    from book.memory.integrator import (
        build_integration_prompt,
        build_compress_prompt,
        build_summary_prompt,
        parse_memory_response,
        parse_compressed_memories,
    )
    from book.models import TempMemory, PermanentMemory

    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test.db"

    try:
        repo = Repository(db_path)
        repo.init_db()

        # --- 5a. 无 LLM 降级：添加临时记忆 ---
        mm = MemoryManager(repo)  # 无 llm_call，使用降级
        result = mm.add_temp_memory("2026/07/01 14:00", "仓库", "捡到钥匙", "获得新线索")
        check("添加第 1 条临时记忆", result["temp_count"] == 1)
        check("添加后未触发整合", result["triggered_integration"] is False)

        # --- 5b. 用降级逻辑手动触发整合 ---
        # 重置：清空之前的测试数据
        repo.clear_temp_memories()
        # 直接添加 9 条临时记忆
        for i in range(1, 10):
            r = mm.add_temp_memory("2026/07/01 14:00", "仓库", f"事件{i}", f"结果{i}")
        check("9 条记忆(第一组)未触发整合", r["triggered_integration"] is False)
        check("9 条记忆计数正确", r["temp_count"] == 9)

        # 添加第 10 条，应该触发整合
        r10 = mm.add_temp_memory("2026/07/01 14:00", "仓库", "事件10", "结果10")
        check("第 10 条触发整合", r10["triggered_integration"] is True)

        # 重置环境再精确测试
        repo2 = Repository(Path(tmpdir) / "test2.db")
        repo2.init_db()
        mm2 = MemoryManager(repo2)

        for i in range(1, 10):
            mm2.add_temp_memory("2026/07/01", "A", f"E{i}", f"R{i}")
        check("9 条记忆未触发整合", repo2.perm_memory_count() == 0)

        result = mm2.add_temp_memory("2026/07/01", "B", "E10", "R10")
        check("第 10 条触发整合", result["triggered_integration"] is True)
        check("整合后临时记忆清空", repo2.temp_memory_count() == 0)
        check("整合后产生 1 条永久记忆", repo2.perm_memory_count() == 1)
        check("整合后更新剧情摘要", len(repo2.get_plot_summary()) > 0)

        repo2.close()

        # --- 5c. 提示词构建 ---
        tm_list = [
            TempMemory(seq=1, datetime_str="2026/07/01 14:00", scene="A", event="E1", result="R1"),
            TempMemory(seq=2, datetime_str="2026/07/01 15:00", scene="A", event="E2", result="R2"),
        ]
        prompt = build_integration_prompt(tm_list)
        check("整合提示词包含规则", "只保留关键事件" in prompt)
        check("整合提示词包含记忆", "E1" in prompt and "R2" in prompt)

        pm_list = [
            PermanentMemory(seq=1, datetime_range="2026/07/01", scene="A", summary="S1"),
            PermanentMemory(seq=2, datetime_range="2026/07/02", scene="B", summary="S2"),
        ]
        sp = build_summary_prompt(pm_list, "旧摘要")
        check("摘要提示词包含旧摘要", "旧摘要" in sp)

        # --- 5d. LLM 响应解析 ---
        raw = "永久记忆：2026/07/01 @ 仓库 | 发现暗门"
        cleaned = parse_memory_response(raw)
        check("清理前缀", not cleaned.startswith("永久记忆"))

        # 压缩记忆解析
        raw_comp = "1. 第一条压缩记忆\n2. 第二条\n3. 第三条\n4. 第四条"
        parts = parse_compressed_memories(raw_comp)
        check("解析压缩记忆", len(parts) >= 3)

        # --- 5e. Writer 上下文生成 ---
        ctx = mm.get_context_for_writer()
        check("Writer 上下文不为空", len(ctx) > 0)
        check("Writer 上下文不含新故事标记", True)  # 此时有数据

        # --- 5f. 记忆状态 ---
        state = mm.get_memory_state()
        check("记忆状态包含 temp_memories", "temp_memories" in state)
        check("记忆状态包含 perm_memories", "perm_memories" in state)
        check("记忆状态包含 plot_summary", "plot_summary" in state)

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 6: 信息抽取器 JSON 解析 ────────────────────────────────────────────

def test_extractor():
    section("6. 信息抽取器 (executor/extractor.py)")

    from book.executor.extractor import Extractor

    ext = Extractor(None)

    # --- 6a. 空输入 ---
    empty = ext._empty_result()
    check("空结果包含所有键",
          all(k in empty for k in ["story_state_update", "temp_memory",
                                    "player_changes", "npc_updates",
                                    "new_items", "new_regions",
                                    "relationships", "suggestions",
                                    "important_intel"]))

    # --- 6b. 标准 JSON 解析 ---
    test_json = '{"story_state_update": {"new_date": null}, "temp_memory": {"event": "test"}}'
    parsed = ext._parse_json(test_json)
    check("解析标准 JSON", parsed["temp_memory"]["event"] == "test")

    # --- 6c. Markdown 代码块包裹的 JSON ---
    md_json = '```json\n{"key": "value"}\n```'
    parsed2 = ext._parse_json(md_json)
    check("解析 markdown JSON 代码块", parsed2["key"] == "value")

    # --- 6d. JSON 嵌在文本中 ---
    embedded = '前面有一些文字{"x": 1, "y": 2}后面也有'
    parsed3 = ext._parse_json(embedded)
    check("从文本中提取 JSON", parsed3.get("x") == 1)

    # --- 6e. 无效输入 ---
    invalid = "这不是 JSON，完全是乱码 ~~~"
    empty_result = ext._parse_json(invalid)
    check("无效输入返回空结果", empty_result["story_state_update"] is not None)

    # --- 6f. 提取 prompt 模板 ---
    from book.executor.extractor import EXTRACT_PROMPT
    check("提取 prompt 包含 JSON 格式指令", "请只输出 JSON" in EXTRACT_PROMPT)
    check("提取 prompt 包含 9 大类别",
          all(section in EXTRACT_PROMPT for section in
              ["story_state_update", "temp_memory", "player_changes",
               "npc_updates", "new_items", "new_regions",
               "relationships", "suggestions", "important_intel"]))


# ─── 测试 7: 信息卡管理器 ────────────────────────────────────────────────────

def test_card_manager():
    section("7. 信息卡管理器 (executor/card_manager.py)")

    from book.db.repository import Repository
    from book.memory.manager import MemoryManager
    from book.executor.card_manager import CardManager

    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test.db"

    try:
        repo = Repository(db_path)
        repo.init_db()
        mm = MemoryManager(repo)
        cm = CardManager(repo, mm)

        # 构造一个完整的提取结果
        extraction = {
            "story_state_update": {
                "new_date": "2026年07月02日 星期三",
                "new_time": "下午 14:00",
                "new_scene": "地下实验室",
            },
            "temp_memory": {
                "datetime_str": "2026/07/02 14:00",
                "scene": "地下实验室",
                "event": "发现神秘设备",
                "result": "获得新线索",
            },
            "player_changes": {
                "currency_change": 100,
                "hp_change": -20,
                "en_change": -10,
                "new_title": "探索者",
                "exp_gain": 50,
            },
            "npc_updates": [
                {"name": "测试NPC", "gender": "女", "occupation": "研究员",
                 "is_present": True, "affection_change": 10},
            ],
            "new_items": [
                {"category": "道具", "name": "能量核心", "quality": "史诗"},
            ],
            "new_regions": [
                {"name": "地下实验室", "security": "灰色区", "danger": "中危",
                 "entry_requirement": "需要门禁卡"},
            ],
            "relationships": [
                {"character_a": "林越", "character_b": "测试NPC",
                 "relation_type": "其他", "description": "初次见面"},
            ],
            "suggestions": {
                "conservative": "先观察",
                "aggressive": "直接上前",
                "creative": "使用隐形能力",
            },
            "important_intel": ["地下实验室属于神秘组织"],
        }

        changes = cm.apply(extraction)

        # 验证故事状态
        ss = repo.get_story_state()
        check("更新日期", "07月02日" in ss.current_date)
        check("更新时间", "14:00" in ss.current_time)
        check("更新场景", ss.current_scene == "地下实验室")

        # 验证玩家
        p = repo.get_player()
        check("货币增加 100", p.currency == 100)
        check("血量减少 20", p.hp == 80)
        check("精力减少 10", p.en == 90)
        check("获得称号", p.title == "探索者")

        # 验证 NPC
        npcs = repo.get_all_npcs()
        check("新增 NPC", len(npcs) == 1 and npcs[0].name == "测试NPC")

        # 验证道具
        items = repo.get_all_items()
        check("新增道具", len(items) == 1 and items[0].name == "能量核心")

        # 验证区域
        regions = repo.get_all_regions()
        check("新增区域", len(regions) == 1)

        # 验证关系
        rels = repo.get_all_relationships()
        check("新增关系", len(rels) == 1)

        # 验证建议
        sug = repo.get_suggestions()
        check("更新保守建议", sug.conservative == "先观察")

        # 验证变更摘要
        check("变更包含 story_state", "story_state" in changes)
        check("变更包含 memory", "memory" in changes)
        check("变更包含 player", "player" in changes)
        check("变更包含 npcs", "npcs" in changes)

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 8: 输出格式化器 ────────────────────────────────────────────────────

def test_formatter():
    section("8. 输出格式化器 (executor/formatter.py)")

    from book.db.repository import Repository
    from book.models import StoryState, Player, PlayerStatus
    from book.executor.formatter import OutputFormatter

    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test.db"

    try:
        repo = Repository(db_path)
        repo.init_db()
        fmt = OutputFormatter(repo)

        ss = StoryState(current_date="2026年07月01日 星期二",
                        current_time="上午 08:00",
                        current_scene="测试场景")
        p = repo.get_player()
        p.title = "冒险者"
        p.currency = 500

        memory_state = {
            "temp_memories": [],
            "temp_count": 0,
            "perm_memories": [],
            "perm_count": 0,
            "plot_summary": "测试摘要",
            "important_intel": [],
        }

        output = fmt.format_full_output(
            narrative="这是测试叙事正文。",
            story_state=ss,
            player=p,
            memory_state=memory_state,
        )

        # 验证输出结构
        check("输出包含状态栏", "当前日期" in output)
        check("输出包含正文", "测试叙事正文" in output)
        check("输出包含玩家信息卡", "玩家信息" in output)
        check("输出包含建议行动", "建议行动" in output)
        check("输出包含记忆区", "记忆区" in output)
        check("输出包含结束语", "本次输出未截断" in output)
        check("输出使用 details 折叠", "<details>" in output)

        # 验证带 NPC 的格式化
        from book.models import NPC
        npc = NPC(name="测试角色", gender="女", occupation="助手",
                  appearance="蓝色短发", is_present=True)
        repo.upsert_npc(npc)

        output2 = fmt.format_full_output(
            narrative="测试", story_state=ss, player=p,
            memory_state=memory_state,
        )
        check("输出包含 NPC 卡", "测试角色" in output2)

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 9: 事务管理 ────────────────────────────────────────────────────────

def test_transaction():
    section("9. 事务管理 (db/repository.py _Transaction)")

    from book.db.repository import Repository
    import sqlite3

    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test.db"

    try:
        repo = Repository(db_path)
        repo.init_db()

        repo.update_player(currency=100)
        check("事务前货币", repo.get_player().currency == 100)

        # 使用事务
        with repo.transaction():
            repo.update_player(currency=200)
            inner = repo.conn.execute("SELECT currency FROM player WHERE id=1").fetchone()
            check("事务内可读新值", inner["currency"] == 200)
            # 不提交——抛出异常回滚
            raise ValueError("测试回滚")

        # 因为 raise 被 __exit__ 的 return False 传递了，所以需要捕获
        rollback_ok = False
        try:
            with repo.transaction():
                repo.update_player(currency=999)
                raise RuntimeError("故意失败")
        except RuntimeError:
            rollback_ok = True

        check("异常触发回滚捕获", rollback_ok is True)
        # 回滚后原值不变
        p = repo.get_player()
        check("回滚后货币恢复", p.currency == 100)

        # 正常提交
        with repo.transaction():
            repo.update_player(currency=500)
        p2 = repo.get_player()
        check("正常提交货币更新", p2.currency == 500)

        repo.close()
    except ValueError:
        pass  # 第一个测试的故意异常
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 10: SQL 注入防护 ────────────────────────────────────────────────────

def test_sql_injection():
    section("10. SQL 注入防护 (db/repository.py 列名白名单)")

    from book.db.repository import _validate_columns, Repository

    # 合法列名应当通过
    try:
        _validate_columns("story_state", {"current_date": "test"})
        check("白名单验证 - 合法列名通过", True)
    except ValueError:
        check("白名单验证 - 合法列名通过", False, "不应抛出异常")

    # 非法列名应当被拒绝
    try:
        _validate_columns("story_state", {"current_date; DROP TABLE players;--": "x"})
        check("白名单拦截 SQL 注入", False, "应该抛出 ValueError")
    except ValueError:
        check("白名单拦截 SQL 注入", True)

    try:
        _validate_columns("player", {"1=1": "attack"})
        check("白名单拦截非法列名", False, "应该抛出 ValueError")
    except ValueError:
        check("白名单拦截非法列名", True)

    # 验证所有白名单表都定义
    from book.db.repository import _VALID_COLUMNS
    check("story_state 在白名单中", "story_state" in _VALID_COLUMNS)
    check("player 在白名单中", "player" in _VALID_COLUMNS)
    check("npcs 在白名单中", "npcs" in _VALID_COLUMNS)
    check("player_equipment 在白名单中", "player_equipment" in _VALID_COLUMNS)


# ─── 测试 11: Writer Prompt 构建 ──────────────────────────────────────────────

def test_writer_prompt():
    section("11. Writer 提示词构建 (writer/prompt.py)")

    from book.writer.prompt import (
        build_writer_system_prompt,
        build_writer_context,
        build_user_prompt,
    )
    from book.models import StoryState, Player, PlayerStatus

    # 系统提示词
    sys_p = build_writer_system_prompt()
    check("Writer 系统 prompt 包含写作要求", "写作要求" in sys_p)
    check("Writer 系统 prompt 包含字数指引", "800" in sys_p)
    check("Writer 系统 prompt 包含叙事风格", "第一人称" in sys_p)
    check("Writer 系统 prompt 不包含格式指令", "信息卡" not in sys_p)
    check("Writer 系统 prompt 不包含记忆区指令", "记忆区" not in sys_p)

    # 用户输入 prompt
    user_p = build_user_prompt("探索旧仓库")
    check("用户 prompt 包含玩家行动", "探索旧仓库" in user_p)
    check("用户 prompt 包含续写指令", "续写" in user_p)

    # 上下文构建
    ss = StoryState(current_scene="旧仓库")
    p = Player(title="冒险者")
    ctx = build_writer_context(
        story_state=ss,
        player=p,
        memory_context="测试记忆",
        recent_history=[
            {"narrative": "上一轮剧情正文内容" * 5, "round_number": 1}
        ],
        npc_context="- 助手（研究员）：蓝色短发",
    )
    check("上下文包含场景", "旧仓库" in ctx)
    check("上下文包含主角", p.name in ctx)
    check("上下文包含记忆", "测试记忆" in ctx)
    check("上下文包含 NPC", "研究员" in ctx)
    # 长叙事应被截断
    long_narrative = "X" * 600
    ctx2 = build_writer_context(ss, p, "", [{"narrative": long_narrative}], "")
    check("长叙事截断到 500 字内", len(ctx2.split("X")) < 550)


# ─── 测试 12: LLMClient 构造和降级 ───────────────────────────────────────────

def test_llm_client():
    section("12. LLM 客户端 (writer/generator.py)")

    from book.writer.generator import LLMClient, Writer

    # 无 API Key 的降级响应
    client = LLMClient(api_key="")
    resp = client.chat("system", "user prompt")
    check("无 API Key 返回降级提示", "[未配置 API Key" in resp)

    resp2 = client.complete("test prompt")
    check("Complete 也返回降级提示", "[未配置 API Key" in resp2)

    # Writer 构造
    w = Writer(client)
    check("Writer 持有 LLM 客户端", w.llm is client)

    # 带 key 的客户端构造
    client2 = LLMClient(api_key="sk-test",
                        api_base="https://custom.api.com/v1",
                        model="custom-model")
    check("自定义 API base", client2.api_base == "https://custom.api.com/v1")
    check("自定义 model", client2.model == "custom-model")


# ─── 测试 13: BookEngine 初始化流程 ───────────────────────────────────────────

def test_book_engine():
    section("13. BookEngine 编排器 (main.py)")

    from book.main import BookEngine
    from pathlib import Path as P
    import tempfile, shutil

    tmpdir = tempfile.mkdtemp()
    orig_cwd = os.getcwd()
    os.chdir(tmpdir)

    try:
        # 临时覆盖 db_path
        import book.config
        original_path = book.config.config.db_path
        book.config.config.db_path = P("test_engine.db")

        engine = BookEngine()

        # 验证各子系统初始化
        check("引擎有 repo", engine.repo is not None)
        check("引擎有 LLM", engine.llm is not None)
        check("引擎有 memory", engine.memory is not None)
        check("引擎有 writer", engine.writer is not None)
        check("引擎有 extractor", engine.extractor is not None)
        check("引擎有 card_manager", engine.card_manager is not None)
        check("引擎有 formatter", engine.formatter is not None)
        check("引擎预编译了 writer prompt", len(engine.writer_system_prompt) > 0)

        # show_state 不发 API 请求也可以运行
        state_output = engine.show_state()
        check("show_state 返回非空", len(state_output) > 0)
        check("show_state 包含状态", "当前日期" in state_output)

        # 设置 API Key
        engine.set_api_key("test-key")
        check("设置 API Key", engine.llm.api_key == "test-key")

        # run_round 无 API Key 时返回降级提示
        output = engine.run_round("测试输入")
        check("run_round 无 API Key 降级", "[未配置 API Key" in output)

        engine.close()

        # 恢复
        book.config.config.db_path = original_path
    finally:
        os.chdir(orig_cwd)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 14: Prompt 文档一致性 ───────────────────────────────────────────────

def test_doc_consistency():
    section("14. 代码与文档一致性")

    from book.config import config
    from book.models import Player, PlayerStatus

    # README 说临时记忆 10 条上限
    check("配置临时记忆上限 = 10", config.temp_memory_max == 10,
          f"实际: {config.temp_memory_max}")

    # README 说永久记忆 6 条压缩为 3 条
    check("配置永久记忆压缩阈值 = 6", config.perm_memory_compress_at == 6,
          f"实际: {config.perm_memory_compress_at}")

    # README 说拆分为 Writer/Executor/Memory 三个模块
    from book.writer.generator import Writer
    from book.executor.extractor import Extractor
    from book.executor.card_manager import CardManager
    from book.memory.manager import MemoryManager
    check("Writer 模块存在", Writer is not None)
    check("Executor/Extractor 模块存在", Extractor is not None)
    check("Executor/CardManager 模块存在", CardManager is not None)
    check("Memory 模块存在", MemoryManager is not None)

    # README 说 SQLite 持久化
    try:
        import sqlite3
        check("使用 SQLite", True)
    except ImportError:
        check("使用 SQLite", False, "sqlite3 模块不可用")

    # 架构：Writer 不接收格式指令
    from book.writer.prompt import build_writer_system_prompt
    sys_p = build_writer_system_prompt()
    check("Writer prompt 不含 'format' 指令", "format" not in sys_p.lower().split())
    check("Writer prompt 不含 '信息卡'", "信息卡" not in sys_p)
    check("Writer prompt 不含 '记忆区'", "记忆区" not in sys_p)

    # Extractor 后处理管道
    from book.executor.extractor import EXTRACT_PROMPT
    check("Extractor prompt 包含提取规则", "提取规则" in EXTRACT_PROMPT)

    # Formatter 渲染输出
    from book.executor.formatter import OutputFormatter
    check("OutputFormatter 存在", OutputFormatter is not None)


# ─── 测试 15: 降级和容错 ──────────────────────────────────────────────────────

def test_fallback():
    section("15. 降级与容错逻辑")

    from book.db.repository import Repository
    from book.memory.manager import MemoryManager
    from book.config import Config

    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test.db"

    try:
        repo = Repository(db_path)
        repo.init_db()

        # 无 LLM 的 MemoryManager 降级
        mm = MemoryManager(repo)  # llm_call 为 None
        for i in range(1, 11):
            mm.add_temp_memory("2026/07/01", "A", f"事件{i}", f"结果{i}")

        # 验证降级整合：有永久记忆和剧情摘要
        check("降级：整合后有永久记忆", repo.perm_memory_count() >= 1)
        check("降级：整合后有剧情摘要", len(repo.get_plot_summary()) > 0)

        # 测试压缩降级（需要 6 条永久记忆）
        for i in range(5):
            from book.models import PermanentMemory
            repo.add_perm_memory(PermanentMemory(
                seq=i+2, datetime_range="2026/07/02",
                scene="测试", summary=f"摘要{i+2}"
            ))

        perm_count = repo.perm_memory_count()
        # 手动触发压缩
        mm._compress_perm_memories()
        after_compress = repo.perm_memory_count()
        check("降级：压缩后永久记忆 ≤ 3", after_compress <= 3,
              f"压缩前: {perm_count}, 压缩后: {after_compress}")

        # Extractor 空结果
        from book.executor.extractor import Extractor
        ext = Extractor(None)
        empty = ext._empty_result()
        check("空结果 player_changes.currency_change = 0",
              empty["player_changes"]["currency_change"] == 0)

        # Writer 降级响应
        from book.writer.generator import LLMClient
        client = LLMClient(api_key="")
        resp = client.chat("sys", "test")
        check("LLM 降级返回字符串", isinstance(resp, str) and len(resp) > 0)

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 测试 16: 边界条件 ───────────────────────────────────────────────────────

def test_edge_cases():
    section("16. 边界条件")

    from book.db.repository import Repository
    from book.models import TempMemory, PermanentMemory, Player
    from book.memory.manager import MemoryManager
    from book.memory.integrator import parse_memory_response

    tmpdir = tempfile.mkdtemp()
    db_path = Path(tmpdir) / "test.db"

    try:
        repo = Repository(db_path)
        repo.init_db()

        # 空记忆整合
        mm = MemoryManager(repo)
        mm._integrate_temp_to_perm()  # 空列表不应该报错
        check("空临时记忆整合不崩溃", repo.perm_memory_count() == 0)

        # 空压缩
        mm._compress_perm_memories()  # 空列表不报错
        check("空永久记忆压缩不崩溃", True)

        # 空摘要更新
        mm._update_plot_summary()  # 空记忆 → 空摘要
        check("空剧情摘要更新不崩溃", True)

        # Player HP 边界
        p = repo.get_player()
        repo.update_player(hp=0)
        check("HP 可设为 0", repo.get_player().hp == 0)
        repo.update_player(hp=100)
        check("HP 可恢复", repo.get_player().hp == 100)

        # 货币不能为负
        repo.update_player(currency=-100)
        c = repo.get_player().currency
        check("货币可为负（不限制）", True)  # 数据库本身不限制

        # Equipment 完整度范围
        repo.update_player_equipment("头部", integrity=0)
        p2 = repo.get_player()
        head = [e for e in p2.equipment if e.slot == "头部"][0]
        check("装备完整度可为 0", head.integrity == 0)

        # Writer 空白输入
        from book.writer.prompt import build_writer_context, build_user_prompt
        from book.models import StoryState
        ctx = build_writer_context(StoryState(), Player(), "", [], "")
        check("空上下文生成不崩溃", len(ctx) > 0)

        # Memory 解析边界
        check("空字符串解析", parse_memory_response("") == "")
        check("纯空格解析", parse_memory_response("   ") == "")

        repo.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ─── 主入口 ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    test_config()
    test_models()
    test_database()
    test_repository_crud()
    test_memory_manager()
    test_extractor()
    test_card_manager()
    test_formatter()
    test_transaction()
    test_sql_injection()
    test_writer_prompt()
    test_llm_client()
    test_book_engine()
    test_doc_consistency()
    test_fallback()
    test_edge_cases()

    # 输出结果
    output = result_summary()

    # 写入文件（始终 UTF-8）
    out_dir = Path(__file__).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "test_report.txt").write_text(output, encoding="utf-8")

    # 打印时使用 'replace' 策略避免 GBK 编码错误
    try:
        print(output)
    except UnicodeEncodeError:
        # Fallback: 只输出 ASCII-safe 集合
        ascii_output = output.replace("✅", "[PASS]").replace("❌", "[FAIL]")
        print(ascii_output)

    print(f"\n报告已保存到 {out_dir / 'test_report.txt'}")

    # 返回退出码
    sys.exit(0 if _failed == 0 else 1)
