"""输出格式化器——将数据库中的信息卡渲染为读者可见的 HTML/Markdown 格式。"""

from __future__ import annotations

from ..db.repository import Repository
from ..models import StoryState, Player, NPC, TempMemory, PermanentMemory


class OutputFormatter:
    """将持久化数据渲染为标准的输出格式。"""

    def __init__(self, repo: Repository):
        self.repo = repo

    def format_chat_output(
        self,
        narrative: str,
        story_state: StoryState,
    ) -> str:
        """聊天输出：仅状态栏 + 正文叙事 + 结束语（不含信息卡）。"""
        parts = [
            self._format_status_bar(story_state),
            narrative,
            "",
            "【本次输出未截断，如截断请刷新消息】",
        ]
        return "\n\n".join(parts)

    def format_state_output(
        self,
        story_state: StoryState,
        player: Player,
        memory_state: dict,
    ) -> str:
        """状态查看输出：状态栏 + 全部信息卡（建议、玩家、NPC、道具、区域、记忆）。"""
        parts = [
            self._format_status_bar(story_state),
            "（查看当前状态，未生成新正文）",
            "",
            self._format_all_cards(player, memory_state),
        ]
        return "\n\n".join(parts)

    def format_full_output(
        self,
        narrative: str,
        story_state: StoryState,
        player: Player,
        memory_state: dict,
    ) -> str:
        """生成完整输出：顶部状态栏 + 正文 + 信息卡 + 结束语。"""
        parts = []

        # 1. 顶部状态栏
        parts.append(self._format_status_bar(story_state))

        # 2. 正文叙事
        parts.append(narrative)
        parts.append("")

        # 3. 信息卡总模块
        parts.append(self._format_all_cards(player, memory_state))

        # 4. 结束语
        parts.append("【本次输出未截断，如截断请刷新消息】")

        return "\n\n".join(parts)

    def _format_status_bar(self, state: StoryState) -> str:
        return (
            f"当前日期：{state.current_date}\n"
            f"当前时间：{state.current_time}\n"
            f"当前场景：{state.current_scene or '待定'}"
        )

    def _format_all_cards(self, player: Player, memory_state: dict) -> str:
        """渲染所有信息卡模块。"""
        parts = []

        # 建议模块
        parts.append(self._format_suggestions())

        # 玩家信息卡（核心模块）
        parts.append(self._format_player_card(player))

        # NPC 卡
        female_npcs = self.repo.get_npcs_by_gender("女")
        male_npcs = self.repo.get_npcs_by_gender("男")
        if female_npcs:
            parts.append(self._format_npc_section(female_npcs, "女性"))
        if male_npcs:
            parts.append(self._format_npc_section(male_npcs, "男性"))

        # 关系网络
        parts.append(self._format_relationships())

        # 城市/区域信息卡
        parts.append(self._format_regions())

        # 重要道具仓库
        parts.append(self._format_items())

        # 记忆区（核心模块）
        parts.append(self._format_memory(memory_state))

        return "\n\n".join(parts)

    def _format_suggestions(self) -> str:
        sug = self.repo.get_suggestions()
        return (
            "<details>\n"
            "<summary>【建议行动】</summary>\n\n"
            f"1. **保守策略**：{sug.conservative or '暂无'}\n\n"
            f"2. **激进策略**：{sug.aggressive or '暂无'}\n\n"
            f"3. **利用环境策略**：{sug.creative or '暂无'}\n\n"
            f"4. **自定义行动**：\n\n"
            "</details>"
        )

    def _format_player_card(self, player: Player) -> str:
        equipment_rows = []
        for eq in player.equipment:
            equipment_rows.append(
                f"| {eq.slot} | {eq.category} | {eq.name} | {eq.integrity}% |"
            )
        equip_table = "\n".join(equipment_rows)

        return (
            "<details>\n"
            f"<summary>【玩家信息 - {player.name}】</summary>\n\n"
            f"**身份/头衔**：{player.title or '暂无'}\n\n"
            f"**货币/资产**：{player.currency}\n\n"
            f"**数值属性**：\n"
            f"- 等级：Lv.{player.level}（{player.current_exp}/{player.max_exp}）\n"
            f"- 血量：{player.hp}/{player.max_hp}\n"
            f"- 攻击力：{player.atk}\n"
            f"- 精力：{player.en}/{player.max_en}\n\n"
            f"**当前状态**：{player.status.value}\n\n"
            f"**核心特性**：{player.core_trait}\n\n"
            f"**服装与装备**：\n"
            f"| 部位 | 种类 | 款式/名称 | 完整度 |\n"
            f"|------|------|-----------|--------|\n"
            f"{equip_table}\n\n"
            f"**外观评估**：{player.appearance_bonus:+d}%\n\n"
            "</details>"
        )

    def _format_npc_section(self, npcs: list[NPC], gender_label: str) -> str:
        cards = []
        for npc in npcs:
            cards.append(self._format_npc_card(npc))
        section_title = f"【人物 - CP-{gender_label}({len(npcs)})】"
        return section_title + "\n\n" + "\n\n".join(cards)

    def _format_npc_card(self, npc: NPC) -> str:
        occ = f"（{npc.occupation}）" if npc.occupation else ""
        return (
            "<details>\n"
            f"<summary>【{npc.name}】{occ}</summary>\n\n"
            f"1. **外貌/衣着**：{npc.appearance or '暂无'}\n\n"
            f"2. **性格**：{npc.personality or '暂无'}\n\n"
            f"3. **携带装备**：{npc.equipment or '无'}\n\n"
            f"4. **好感度**：Lv.{npc.affection_level}（exp：{npc.affection_exp}/{npc.affection_exp_max}）\n"
            f"   **与玩家做爱次数**：{npc.sex_count}\n\n"
            f"5. **内心想法**：{npc.inner_thought or '无法感知'}\n\n"
            f"6. **角色情报**：{npc.intel or '暂无'}\n\n"
            f"7. **人物状态**：{npc.status}\n\n"
            f"8. **是否在场**：{'是' if npc.is_present else '否'}\n\n"
            f"9. **喜好体位与性癖**：{npc.h_preferences or '尚未解锁'}\n\n"
            "</details>"
        )

    def _format_relationships(self) -> str:
        rels = self.repo.get_all_relationships()
        if not rels:
            return (
                "<details>\n"
                "<summary>【关系网络】</summary>\n\n"
                "暂无特殊关系网络记录。\n\n"
                "</details>"
            )

        lines = []
        for r in rels:
            lines.append(f"- {r.character_a} → {r.character_b}：{r.relation_type}，{r.description}")
        return (
            "<details>\n"
            "<summary>【关系网络】</summary>\n\n"
            + "\n".join(lines) +
            "\n\n</details>"
        )

    def _format_regions(self) -> str:
        regions = self.repo.get_all_regions()
        if not regions:
            return (
                "<details>\n"
                "<summary>【城市/区域信息卡】</summary>\n\n"
                "暂无数据\n\n"
                "</details>"
            )

        rows = [
            "| 层级区域 | 危险/治安等级 | 进入需求 | 核心情报 / 重点地标 |",
            "|-----------|---------------|----------|---------------------|",
        ]
        for r in regions:
            rows.append(
                f"| {r.name} | {r.danger.value} / {r.security.value} | {r.entry_requirement} | {r.landmarks} |"
            )
        return (
            "<details>\n"
            "<summary>【城市/区域信息卡】</summary>\n\n"
            + "\n".join(rows) +
            "\n\n</details>"
        )

    def _format_items(self) -> str:
        items = self.repo.get_all_items()
        if not items:
            return (
                "<details>\n"
                "<summary>【重要道具仓库】</summary>\n\n"
                "| 分类 | 名称 / 型号 | 品质 | 状态 |\n"
                "|------|-------------|------|------|\n"
                "| 载具 | 未检测到已登记载具 | - | - |\n"
                "| 道具 | 暂无道具记录 | - | - |\n"
                "| 密钥 | 无高权限访问密钥 | - | - |\n"
                "| 收藏 | 库存为空 | - | - |\n\n"
                "</details>"
            )

        # 按分类整理
        by_category = {"载具": [], "道具": [], "密钥": [], "收藏": []}
        for item in items:
            cat = item.category if item.category in by_category else "道具"
            by_category[cat].append(item)

        rows = [
            "| 分类 | 名称 / 型号 | 品质 | 状态 |",
            "|------|-------------|------|------|",
        ]
        for cat, cat_items in by_category.items():
            if cat_items:
                for item in cat_items:
                    rows.append(f"| {cat} | {item.name} | {item.quality.value} | {item.status} |")
            else:
                placeholder = {
                    "载具": "未检测到已登记载具",
                    "道具": "暂无道具记录",
                    "密钥": "无高权限访问密钥",
                    "收藏": "库存为空",
                }[cat]
                rows.append(f"| {cat} | {placeholder} | - | - |")

        return (
            "<details>\n"
            "<summary>【重要道具仓库】</summary>\n\n"
            + "\n".join(rows) +
            "\n\n</details>"
        )

    def _format_memory(self, memory_state: dict) -> str:
        temp_mems: list[TempMemory] = memory_state.get("temp_memories", [])
        perm_mems: list[PermanentMemory] = memory_state.get("perm_memories", [])
        temp_count = memory_state.get("temp_count", 0)
        perm_count = memory_state.get("perm_count", 0)
        plot_summary = memory_state.get("plot_summary", "")
        intel = memory_state.get("important_intel", [])

        lines = [
            "<details>",
            f"<summary>记忆区 (临时: {temp_count}/10 | 永久: {perm_count})</summary>",
            "",
            f"【临时记忆区】({temp_count}/10)",
        ]

        for m in temp_mems:
            lines.append(f"{m.seq}. [{m.datetime_str}] @ [{m.scene}] | 事件：{m.event} -> 结果：{m.result}")

        lines.extend(["", "---", "", f"【永久记忆区】({perm_count})"])

        if plot_summary:
            lines.append(f"剧情摘要：{plot_summary}")
            lines.append("")

        for m in perm_mems:
            lines.append(f"{m.seq}. [{m.datetime_range}] @ [{m.scene}] | {m.summary}")

        lines.extend(["", "---", "", "【重要情报】"])
        if intel:
            for item in intel:
                lines.append(f"- {item}")
        else:
            lines.append("（暂无记录）")

        lines.extend(["", "</details>"])
        return "\n".join(lines)
