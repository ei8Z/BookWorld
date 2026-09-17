"""提及驱动上下文构建——按优先级与字符预算装配写作上下文。

对应《开发计划.md》P1-2。核心原则：**不再无条件拼接全量卡片**。
只有「POV 主角」「被提及的实体」「在场的实体」才会进入上下文，其余靠检索补充。

优先级（从高到低，累加到 budget_chars 为止）：
  1. POV 主角卡（player 或 npcs 中该角色）
  2. 被提及实体（entity_aliases 别名 + 各表 name 的精确子串匹配）
  3. 在场 NPC（npcs.is_present = 1）
  4. 最近 3 轮叙事尾段（每轮取尾 300 字）
  5. 剧情摘要 + 最近 5 条重要情报
  6. 未收伏笔（foreshadows.status='未收'）
  7. 检索补充：search() 取 3 条相关历史片段
超预算时丢弃最低优先级项，并在 ContextPack.trimmed 里列出被丢弃项名称。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..db import search as search_mod
from ..db.repository import Repository

# 各分区的名称——同时也是 ContextPack.trimmed 里报告的丢弃项名称
SECTION_SCENE = "scene"
SECTION_CHAPTER = "chapter"
SECTION_SCENES = "chapter_scenes"
SECTION_ENTITIES = "entities"
SECTION_PRESENT_NPCS = "present_npcs"
SECTION_RECENT = "recent_narratives"
SECTION_MEMORY = "memory"
SECTION_FORESHADOWS = "foreshadows"
SECTION_RETRIEVED = "retrieved"

RECENT_ROUNDS = 3
RECENT_TAIL_CHARS = 300
INTEL_LIMIT = 5
RETRIEVED_LIMIT = 3
CARD_CLIP = 400
# 关系卡数量上限：枢纽角色的几十条关系会把 POV/在场卡挤出预算
RELATIONSHIP_LIMIT = 8


@dataclass
class ContextPack:
    """受预算约束的上下文包，字段与 §5 的 context 输出一一对应。"""

    pov: str
    scene: str
    chapter: int | None
    budget_chars: int
    prompt_chars: int = 0
    entities: list[dict] = field(default_factory=list)
    present_npcs: list[str] = field(default_factory=list)
    memory: dict = field(default_factory=dict)
    foreshadows: list[dict] = field(default_factory=list)
    retrieved: list[dict] = field(default_factory=list)
    trimmed: list[str] = field(default_factory=list)
    # P1 章节层级（--chapter / story_state.chapter 驱动）
    chapter_title: str = ""
    chapter_pov: str = ""
    chapter_goal: str = ""
    chapter_scenes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "pov": self.pov,
            "scene": self.scene,
            "chapter": self.chapter,
            "prompt_chars": self.prompt_chars,
            "entities": self.entities,
            "present_npcs": self.present_npcs,
            "memory": self.memory,
            "foreshadows": self.foreshadows,
            "retrieved": self.retrieved,
            "trimmed": self.trimmed,
            # 附加字段：§5 规定的 10 个字段一个没少，这里只是补充章节信息
            "chapter_title": self.chapter_title,
            "chapter_pov": self.chapter_pov,
            "chapter_goal": self.chapter_goal,
            "chapter_scenes": self.chapter_scenes,
        }

    def render_parts(self) -> list[str]:
        """渲染用到的每一行。render() 只是把它 join 起来。

        预算装配也用这个方法算尺寸，保证"算的"与"输出的"是同一份文本。
        """
        parts: list[str] = []
        line = chapter_render_line(self)
        if line:
            parts.append(line)
        if self.chapter_scenes:
            parts.append(f"【本章场景】{'、'.join(self.chapter_scenes)}")
        if self.scene:
            parts.append(f"【当前场景】{self.scene}")
        if self.entities:
            parts.append("【相关人物与设定】")
            for e in self.entities:
                parts.append(entity_line(e))
        if self.present_npcs:
            parts.append(f"【在场】{'、'.join(self.present_npcs)}")
        if self.memory.get("plot_summary"):
            parts.append(f"【剧情摘要】{self.memory['plot_summary']}")
        for item in self.memory.get("intel", []):
            parts.append(f"【情报】{item}")
        if self.foreshadows:
            parts.append(f"【未收伏笔】{foreshadow_line(self.foreshadows)}")
        recent = self.memory.get("recent") or []
        if recent:
            parts.append("【最近剧情】")
            parts.extend(t for t in recent if t)
        if self.retrieved:
            parts.append("【相关历史片段】")
            for r in self.retrieved:
                parts.append(retrieved_line(r))
        return parts

    def render(self) -> str:
        """渲染成可直接拼进 Writer user prompt 的文本块。"""
        return "\n".join(self.render_parts())


# ─── 与 render 共用的行渲染（预算按这些行算，保证记账=输出） ────────────────


def chapter_render_line(pack: "ContextPack") -> str:
    """【本章】那一行；没有章节信息时返回空串。render 与预算共用。"""
    if pack.chapter is None:
        return ""
    bits = [f"第{pack.chapter}章"]
    if pack.chapter_title:
        bits.append(pack.chapter_title)
    if pack.chapter_pov:
        bits.append(f"POV：{pack.chapter_pov}")
    if pack.chapter_goal:
        bits.append(f"目标：{pack.chapter_goal}")
    return "【本章】" + " | ".join(bits)


def entity_line(e: dict) -> str:
    """实体卡在 render 里的那一行（注意：card 里通常已含名字，这里只加前缀）。"""
    return f"- {e['name']}：{e['card']}"


def foreshadow_line(foreshadows: list[dict]) -> str:
    return "、".join(
        f"{x['name']}（第{x['planted_chapter']}章埋）"
        if x.get("planted_chapter") is not None
        else x["name"]
        for x in foreshadows
    )


def retrieved_line(r: dict) -> str:
    chapter = r.get("chapter")
    prefix = f"第{chapter}章 " if chapter is not None else ""
    return f"- {prefix}{r.get('snippet', '')}"


# ─── 卡片渲染 ──────────────────────────────────────────────────────────────


def _clip(text: str, limit: int = CARD_CLIP) -> str:
    """压平换行并按上限截断。"""
    flat = " ".join((text or "").split())
    return flat[:limit]


def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _columns(conn, table: str) -> set[str]:
    return {r[1] for r in conn.execute("PRAGMA table_info(" + table + ")")}


def _player_card(repo: Repository) -> tuple[str, str]:
    p = repo.get_player()
    bits = [f"姓名：{p.name}", f"年龄：{p.age}", f"核心特性：{p.core_trait}"]
    bits.append(f"状态：{p.status.value} HP {p.hp}/{p.max_hp} EN {p.en}/{p.max_en}")
    if p.title:
        bits.append(f"身份：{p.title}")
    eq = "、".join(
        f"{e.slot}:{e.name}" for e in p.equipment if e.name and e.name != "空"
    )
    if eq:
        bits.append(f"装备：{eq}")
    return p.name, " | ".join(bits)


def _npc_card(npc, detailed: bool = True) -> str:
    bits = [f"{npc.name}（{npc.occupation or '未知身份'}）"]
    bits.append(f"状态：{npc.status}")
    if detailed:
        bits.append(f"好感度 Lv.{npc.affection_level}")
        if npc.appearance:
            bits.append(f"外貌：{_clip(npc.appearance, 120)}")
        if npc.personality:
            bits.append(f"性格：{_clip(npc.personality, 80)}")
        if npc.inner_thought:
            bits.append(f"内心：{_clip(npc.inner_thought, 80)}")
    return " | ".join(bits)


def _item_card(item) -> str:
    return f"[{item.category}] {item.name} | {item.quality.value} | {item.status}"


def _region_card(region) -> str:
    return (
        f"{region.name} | 危险: {region.danger.value} | 治安: {region.security.value}"
        f" | {_clip(region.landmarks, 80)}"
    )


def _relationship_card(rel) -> str:
    return f"{rel.character_a} → {rel.character_b}：{rel.relation_type}，{_clip(rel.description, 80)}"


# ─── 提及匹配 ──────────────────────────────────────────────────────────────


def _load_aliases(repo: Repository) -> dict[str, list[tuple[str, str]]]:
    """返回 {实体标识: [(别名, 实体类型)…]}。实体标识通常就是实体名字。

    类型必须一起带上：否则 item 的别名会误命中同名 NPC，
    而 player 类型的别名（主角的化名/外号）则完全用不上。
    """
    conn = repo.conn
    if not _table_exists(conn, "entity_aliases"):
        return {}
    out: dict[str, list[tuple[str, str]]] = {}
    try:
        for r in conn.execute(
            "SELECT entity_id, entity_type, alias FROM entity_aliases ORDER BY id"
        ):
            out.setdefault(str(r["entity_id"]), []).append(
                (str(r["alias"]), str(r["entity_type"]))
            )
    except Exception:  # noqa: BLE001 - 别名只是增强，缺了不影响主流程
        return {}
    return out


def _mentioned(
    name: str, haystack: str, aliases: dict[str, list[tuple[str, str]]], entity_type: str
) -> bool:
    """实体是否"被提及"：**名字本身**或**该类型下的已登记别名**在文本里精确出现。

    - 别名只在 entity_type 匹配时生效（player/npc/item/region 不互相串味）
    - 单字名不做裸子串匹配：NPC「林」会被「林越」误判为被提及，
      只有登记过别名时才认它
    """
    if not name:
        return False
    for alias, atype in aliases.get(name, []):
        if alias and atype == entity_type and alias in haystack:
            return True
    if len(name) >= 2:
        return name in haystack
    return False


# ─── 各分区数据 ────────────────────────────────────────────────────────────


def _collect_entities(
    repo: Repository, pov: str, haystack: str, include_present: bool = True
) -> list[dict]:
    """POV 主角卡 + 被提及实体 + 在场实体（去重）。"""
    entities: list[dict] = []
    seen: set[str] = set()

    def add(kind: str, name: str, card: str) -> None:
        if name in seen:
            return
        seen.add(name)
        entities.append({"type": kind, "name": name, "card": card})

    player_name, player_card = _player_card(repo)
    target = (pov or "").strip()

    aliases = _load_aliases(repo)

    # 1) POV 主角卡：POV 是玩家本人，或被提到的主角化名/外号
    if target and (
        target == player_name
        or _mentioned(player_name, haystack, aliases, "player")
    ):
        add("player", player_name, player_card)

    npcs = repo.get_all_npcs()

    # 2) POV 主角卡未命中时，回退到 npcs 里的同名角色。
    #    必须在"在场 NPC"之前处理：否则 POV 角色 is_present=1 时会先被精简卡占位，
    #    优先级 1 的主角卡就被降级了。用完整卡。
    if target and target not in seen:
        for npc in npcs:
            if npc.name == target:
                add("npc", npc.name, _npc_card(npc))
                break

    # 3) 被提及实体（名字或已登记别名的精确子串匹配）
    for npc in npcs:
        if npc.name == target or npc.name in seen:
            continue
        if _mentioned(npc.name, haystack, aliases, "npc"):
            add("npc", npc.name, _npc_card(npc))

    for item in repo.get_all_items():
        if item.name not in seen and _mentioned(item.name, haystack, aliases, "item"):
            add("item", item.name, _item_card(item))

    for region in repo.get_all_regions():
        if region.name not in seen and _mentioned(region.name, haystack, aliases, "region"):
            add("region", region.name, _region_card(region))

    # 4) 在场 NPC：算"被提及"的一种（无需名字出现在文本里）。
    #    计划里只有"被提及实体"要求完整卡片，在场者给精简卡，避免几个在场
    #    NPC 就把预算吃光、把 POV 主角卡挤掉。
    #    必须在关系筛选**之前**收集：否则"两个都在场的人之间的关系"永远选不中。
    if include_present:
        for npc in npcs:
            if npc.is_present and npc.name not in seen:
                add("npc", npc.name, _npc_card(npc, detailed=False))

    # 5) 与已入选实体相关的关系，只在其中一端已入选时才给（不再无条件全量注入）。
    #    加数量上限：枢纽角色的几十条关系会把优先级更高的卡片全挤出预算。
    if entities:
        names = {e["name"] for e in entities}
        related = [
            rel for rel in repo.get_all_relationships()
            if rel.character_a in names or rel.character_b in names
        ]
        if len(related) > RELATIONSHIP_LIMIT:
            related = related[:RELATIONSHIP_LIMIT]
        for rel in related:
            add("relationship", f"{rel.character_a}→{rel.character_b}", _relationship_card(rel))

    return entities


def _recent_narratives(repo: Repository) -> list[dict]:
    rounds = repo.get_session_rounds(limit=RECENT_ROUNDS)
    out = []
    for row in rounds:
        narrative = (row.get("narrative") or "").strip()
        if not narrative:
            continue
        out.append(
            {
                "round": row.get("round_number"),
                "chapter": row.get("chapter"),
                "text": narrative[-RECENT_TAIL_CHARS:],
            }
        )
    return out


def _foreshadows(repo: Repository, chapter: int | None = None) -> list[dict]:
    """未收伏笔。

    chapter 给定时只保留"埋设章号 <= 当前章"的：还没埋下的伏笔不应该出现在
    当前章的上下文中（否则等于剧透未来的钩子）。章号缺失的伏笔始终保留。
    """
    conn = repo.conn
    if not _table_exists(conn, "foreshadows"):
        return []
    cols = _columns(conn, "foreshadows")
    name_col = "name" if "name" in cols else "title"
    rows = conn.execute(
        "SELECT * FROM foreshadows WHERE status='未收' ORDER BY id"
    ).fetchall()
    out = []
    for row in rows:
        planted = row["planted_chapter"] if "planted_chapter" in cols else None
        if chapter is not None and planted is not None and planted > chapter:
            continue
        out.append(
            {
                "name": row[name_col],
                "planted_chapter": planted,
                "status": row["status"],
            }
        )
    return out


def _chapter_context(repo: Repository, chapter: int | None) -> dict:
    """读取当前章/场景的结构化信息（P1 层级真正参与上下文）。

    这些表是 P1 新建的，缺失时安全返回空。
    """
    if chapter is None:
        return {"title": "", "pov": "", "goal": "", "locations": []}

    conn = repo.conn
    info = {"title": "", "pov": "", "goal": "", "locations": []}

    if _table_exists(conn, "chapters"):
        row = repo.get_chapter(chapter)
        if row:
            info["title"] = row.get("title") or ""
            info["pov"] = row.get("pov") or ""
            info["goal"] = row.get("goal") or ""

    if _table_exists(conn, "scenes"):
        for s in repo.list_scenes(chapter):
            loc = (s.get("location") or "").strip()
            if loc and loc not in info["locations"]:
                info["locations"].append(loc)

    return info


# ─── 主入口 ────────────────────────────────────────────────────────────────


def build_context(
    repo: Repository,
    *,
    pov: str,
    scene: str = "",
    chapter: int | None = None,
    budget_chars: int = 6000,
    extra_text: str = "",
) -> ContextPack:
    """按优先级装配上下文，累加到 budget_chars 为止，超预算丢弃最低优先级项。

    extra_text 用于把"本轮用户输入"也算进提及匹配范围（互动模式用）。
    """
    pack = ContextPack(
        pov=pov,
        scene=scene,
        chapter=chapter,
        budget_chars=budget_chars,
    )

    story_state = repo.get_story_state()
    # 章号优先用显式传入的；否则回落到 story_state.chapter。
    # 只有"显式传章号"时才启用伏笔的章号过滤——否则未登记章号的调用方
    # 会莫名其妙丢掉所有伏笔（默认 story_state.chapter=1）。
    chapter_explicit = chapter is not None
    if chapter is None:
        chapter = story_state.chapter
        pack.chapter = chapter
    chapter_info = _chapter_context(repo, chapter)

    # 本章登记的场景地点也参与提及匹配：写到"机房"这一章时，
    # 场景表里登记的"服务器机房"应该能把对应区域卡片带进来
    haystack = " ".join(
        x for x in (
            pov, scene, story_state.current_scene, extra_text,
            chapter_info["title"], chapter_info["goal"],
            *chapter_info["locations"],
        ) if x
    )

    # 优先级 1、2、3：POV 主角卡 / 被提及实体 / 在场实体
    entities = _collect_entities(repo, pov, haystack, include_present=True)
    # 逐条裁剪是按列表顺序贪心的，所以必须保证 POV 主角卡排在最前面，
    # 否则小预算下可能被别的卡片抢先占满而丢掉主角。
    entities.sort(key=lambda e: 0 if e["type"] == "player" else 1)
    present = [n.name for n in repo.get_all_npcs() if n.is_present]

    # 优先级 4：最近 3 轮叙事尾段
    recent = _recent_narratives(repo)

    # 优先级 5：剧情摘要 + 最近 5 条重要情报
    memory = {
        "plot_summary": repo.get_plot_summary(),
        "intel": repo.get_important_intel()[-INTEL_LIMIT:],
        "recent": [],  # 由预算装配阶段按 SECTION_RECENT 是否入选决定
    }

    # 优先级 6：未收伏笔。显式给了章号时只收"已经埋下"的（不剧透未来钩子）；
    # 没给章号则不做过滤，保持既有的"列出全部未收伏笔"语义。
    foreshadows = _foreshadows(repo, chapter if chapter_explicit else None)

    # 优先级 7：检索补充（把 pov/scene/chapter 目标当查询词）
    query = " ".join(x for x in (pov, scene) if x).strip()
    retrieved: list[dict] = []
    if query:
        for hit in search_mod.search(repo.conn, query, k=RETRIEVED_LIMIT):
            retrieved.append(
                {
                    "source": hit.source,
                    "chapter": hit.chapter,
                    "snippet": hit.snippet,
                    "score": hit.score,
                }
            )

    # ─── 预算装配 ─────────────────────────────────────────────────────────
    # 规则：除检索补充外，每个分区都要能**完整**装下才保留，否则整块丢弃并记进
    # trimmed；实体卡是分区内部按条贪心（顺序即优先级：POV → 被提及 → 在场）。
    #
    # 预算比较的是"渲染出来的字符数"，所以每个分区的尺寸都按 render() 的实际
    # 输出计算（含标题前缀），最后 prompt_chars = len(render())。
    # 这样 prompt_chars 与实际注入内容永远一致，不会出现"计了账却没输出"。
    pack.chapter_title = chapter_info["title"]
    pack.chapter_pov = chapter_info["pov"]
    pack.chapter_goal = chapter_info["goal"]
    pack.chapter_scenes = chapter_info["locations"]

    used = 0
    dropped_entities: list[str] = []

    def _entity_size(e: dict) -> int:
        # 按 render 里的真实行计算（+1 是行尾换行），避免"公式"与"输出"不一致
        return len(entity_line(e)) + 1

    # 优先级 0：章节头 + 场景行 + 场景行也必须在预算内，否则调用方传一个
    # 超长 chapter 标题/scene 就能绕过预算把 prompt 撑爆。
    chapter_line = chapter_render_line(pack)
    if chapter_line:
        size = len(chapter_line) + 1
        if used + size <= budget_chars:
            used += size
        else:
            pack.trimmed.append(SECTION_CHAPTER)
            pack.chapter_title = pack.chapter_pov = pack.chapter_goal = ""
            pack.chapter = None
            chapter_line = ""
    scenes_line = f"【本章场景】{'、'.join(pack.chapter_scenes)}" if pack.chapter_scenes else ""
    if scenes_line:
        size = len(scenes_line) + 1
        if used + size <= budget_chars:
            used += size
        else:
            pack.trimmed.append(SECTION_SCENES)
            pack.chapter_scenes = []
            scenes_line = ""

    if pack.scene:
        scene_size = len("【当前场景】") + len(pack.scene) + 1
        if used + scene_size <= budget_chars:
            used += scene_size
        else:
            pack.trimmed.append(SECTION_SCENE)
            pack.scene = ""

    # 1+2+3：实体卡逐条装。卡片装不下时连 POV 卡也会被丢（并记进 trimmed）——
    # 这是预算硬约束的正常表现；默认 6000 字远大于单张卡片。
    if entities:
        used += len("【相关人物与设定】") + 1
    for ent in entities:
        size = _entity_size(ent)
        if used + size <= budget_chars:
            pack.entities.append(ent)
            used += size
        else:
            dropped_entities.append(ent["name"])
    if dropped_entities:
        pack.trimmed.append(SECTION_ENTITIES)

    kept_names = {e["name"] for e in pack.entities}
    pack.present_npcs = [n for n in present if n in kept_names]
    if pack.present_npcs:
        present_size = len("【在场】") + len("、".join(pack.present_npcs)) + 1
        if used + present_size <= budget_chars:
            used += present_size
        else:
            pack.trimmed.append(SECTION_PRESENT_NPCS)
            pack.present_npcs = []

    # 4/5/6/7：整分区取舍。注意 recent 与 memory 是**两个独立分区**：
    # 之前共用一个 pack.memory 字段，导致 recent 装得下、memory 装不下时
    # recent 被记账却不输出。
    fs_size = 0
    if foreshadows:
        fs_size = len("【未收伏笔】") + len(foreshadow_line(foreshadows)) + 1
    sections = [
        (SECTION_RECENT, len("【最近剧情】") + 1 + sum(len(r["text"]) + 1 for r in recent)),
        (SECTION_MEMORY,
         (len("【剧情摘要】") + len(memory["plot_summary"]) + 1 if memory["plot_summary"] else 0)
         + sum(len("【情报】") + len(i) + 1 for i in memory["intel"])),
        (SECTION_FORESHADOWS, fs_size),
        (SECTION_RETRIEVED, len("【相关历史片段】") + 1
         + sum(len(retrieved_line(r)) + 1 for r in retrieved)),
    ]
    kept: set[str] = set()
    for name, size in sections:
        if used + size <= budget_chars:
            used += size
            kept.add(name)
        else:
            pack.trimmed.append(name)

    if SECTION_RECENT in kept and recent:
        memory["recent"] = [r["text"] for r in recent]
    else:
        memory["recent"] = []
    if SECTION_MEMORY in kept:
        pack.memory = memory
    if SECTION_FORESHADOWS in kept:
        pack.foreshadows = foreshadows
    if SECTION_RETRIEVED in kept:
        pack.retrieved = retrieved

    # prompt_chars 一律等于实际渲染长度，保证与注入内容一致
    pack.prompt_chars = len(pack.render())
    return pack
