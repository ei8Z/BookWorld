"""Writer Prompt——为作家身份生成干净的创作提示词。

与原始提示词的关键区别：
- 不包含系统执行者指令（格式、信息卡、记忆区）
- 不包含输出结构要求
- 只关注叙事本身的质量
- 基础设定从数据库 story_settings 动态注入
"""

from ..models import StoryState, Player


def build_writer_system_prompt(settings: dict[str, str] | None = None) -> str:
    """作家身份的系统提示词——精简、聚焦、不被执行器污染。
    
    settings 中可包含: player_profile, player_abilities, world_setting, initial_setup
    """
    s = settings or {}

    parts = [
        "你是一个网络小说作者，正在赶稿。你状态投入但有点疲惫，脑子里只有这个故事及其各种设定，没精力去想怎么写才正确。写就是了。",
        "",
    ]

    # ── 注入故事设定 ──
    if s.get("player_profile"):
        parts.append("## 主角设定（你必须严格遵守）")
        parts.append(s["player_profile"])
        parts.append("")

    if s.get("player_abilities"):
        parts.append("## 主角能力（你必须严格遵守这些规则，不得自行添加或修改能力）")
        parts.append(s["player_abilities"])
        parts.append("")

    if s.get("world_setting"):
        parts.append("## 世界背景（故事舞台的硬性规则）")
        parts.append(s["world_setting"])
        parts.append("")

    if s.get("initial_setup"):
        parts.append("## 故事起点（当前故事的开端设定）")
        parts.append(s["initial_setup"])
        parts.append("")

    # ── 写作要求 ──
    parts.extend([
        "## 写作要求",
        "",
        "**字数指引**",
        "- 软下限 800 字：低于此字数说明剧情推进不足",
        "- 无硬性上限：剧情推到位了自然收笔，不需要凑字数",
        "- 注入保护：如果你在用相似的句式反复描写同一个状态——停下来，直接跳到下一个剧情节点",
        "",
        "**续写原则**",
        "1. 从上一轮的最后一个进展点直接续写",
        "2. 进行全新的叙述，绝不概括、重述或复述上一轮的内容",
        "3. 确保剧情向前推进，不原地打转",
        "",
        "**叙事风格**",
        "- 第一人称",
        "- 沉浸式，注重感官细节（视觉、听觉、触觉、嗅觉）",
        "- 心理活动自然融入叙事",
        "- 对话推动剧情",
        "",
        "**时间感知**",
        "- 根据互动内容合理推进时间",
        "- 注意昼夜变化、场景转换的描写",
    ])

    return "\n".join(parts)


def build_writer_context(
    story_state: StoryState,
    player: Player,
    memory_context: str,
    recent_history: list[dict],
    npc_context: str,
    items_context: str = "",
    regions_context: str = "",
    relationships_context: str = "",
) -> str:
    """构建作家所需的上下文——精简、只给写作需要的信息。"""
    parts = []

    # 当前时间和场景
    parts.append("## 当前状态")
    parts.append(f"日期：{story_state.current_date}")
    parts.append(f"时间：{story_state.current_time}")
    parts.append(f"地点：{story_state.current_scene or '待定'}")
    parts.append("")

    # 玩家角色（含数值和装备）
    parts.append("## 主角")
    parts.append(f"姓名：{player.name}，{player.age}岁，身高{player.height_cm}cm")
    parts.append(f"身份：{player.title or '待展开'}")
    parts.append(f"等级：Lv.{player.level}（{player.current_exp}/{player.max_exp}）| "
                 f"HP: {player.hp}/{player.max_hp} | ATK: {player.atk} | EN: {player.en}/{player.max_en}")
    parts.append(f"当前状态：{player.status.value}")
    parts.append(f"能力：{player.core_trait}")
    if player.equipment:
        eq_parts = []
        for eq in player.equipment:
            eq_parts.append(f"{eq.name}({eq.slot}, {eq.integrity}%)")
        parts.append(f"装备：{' | '.join(eq_parts)}")
    parts.append(f"货币：{player.currency}元")
    parts.append("")

    # 记忆上下文（由 Memory Manager 提供）
    if memory_context:
        parts.append("## 剧情背景")
        parts.append(memory_context)
        parts.append("")

    # 最近几轮的叙事（用于续写连贯性）
    if recent_history:
        parts.append("## 上一轮剧情")
        last = recent_history[-1]
        # 只取最后 500 字的摘要，避免上下文过长
        narrative = last.get("narrative", "")
        if len(narrative) > 500:
            narrative = narrative[-500:] + "\n...(上文省略)"
        parts.append(narrative)
        parts.append("")

    # 在场 NPC（含好感度/状态/内心想法）
    if npc_context:
        parts.append("## 在场人物")
        parts.append(npc_context)
        parts.append("")

    # 持有道具
    if items_context:
        parts.append("## 持有道具")
        parts.append(items_context)
        parts.append("")

    # 当前区域
    if regions_context:
        parts.append("## 已知区域")
        parts.append(regions_context)
        parts.append("")

    # 人物关系
    if relationships_context:
        parts.append("## 人物关系")
        parts.append(relationships_context)
        parts.append("")

    return "\n".join(parts)


def build_user_prompt(user_input: str) -> str:
    """构建用户输入提示词——把玩家的选择转为写作指令。"""
    return f"""【玩家行动】
{user_input}

请以上述行动为引导，续写下一段故事。自然衔接上一轮剧情，推进事件发展。"""
