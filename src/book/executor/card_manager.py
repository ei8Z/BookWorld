"""Card Manager——将抽取的结构化信息应用到数据库中。"""

from __future__ import annotations

from ..db.repository import Repository
from ..models import NPC, Item, Quality, Region, SecurityLevel, DangerLevel, Relationship, PlayerStatus
from ..memory.manager import MemoryManager


class CardManager:
    """系统执行者的核心：接收提取结果，更新所有信息卡。"""

    def __init__(self, repo: Repository, memory: MemoryManager):
        self.repo = repo
        self.memory = memory

    def apply(self, extraction: dict) -> dict:
        """应用提取结果，返回变更摘要。"""
        changes = {}

        # 1. 更新故事状态
        state_update = extraction.get("story_state_update", {})
        state_changes = self._apply_story_state(state_update)
        if state_changes:
            changes["story_state"] = state_changes

        # 2. 添加临时记忆
        temp_mem = extraction.get("temp_memory", {})
        if temp_mem.get("event"):
            mem_result = self.memory.add_temp_memory(
                datetime_str=temp_mem.get("datetime_str", ""),
                scene=temp_mem.get("scene", ""),
                event=temp_mem.get("event", ""),
                result=temp_mem.get("result", ""),
            )
            changes["memory"] = mem_result

        # 3. 更新玩家
        player_changes = extraction.get("player_changes", {})
        player_result = self._apply_player_changes(player_changes)
        if player_result:
            changes["player"] = player_result

        # 4. 更新 NPC
        npc_updates = extraction.get("npc_updates", [])
        if npc_updates:
            changes["npcs"] = self._apply_npc_updates(npc_updates)

        # 5. 添加道具
        new_items = extraction.get("new_items", [])
        if new_items:
            changes["items"] = self._apply_items(new_items)

        # 6. 添加区域
        new_regions = extraction.get("new_regions", [])
        if new_regions:
            changes["regions"] = self._apply_regions(new_regions)

        # 7. 更新关系
        relationships = extraction.get("relationships", [])
        if relationships:
            changes["relationships"] = self._apply_relationships(relationships)

        # 8. 更新建议
        suggestions = extraction.get("suggestions", {})
        if any(suggestions.values()):
            self.repo.update_suggestions(
                conservative=suggestions.get("conservative", ""),
                aggressive=suggestions.get("aggressive", ""),
                creative=suggestions.get("creative", ""),
            )
            changes["suggestions"] = suggestions

        # 9. 重要情报
        intel_list = extraction.get("important_intel", [])
        for intel in intel_list:
            if intel:
                self.memory.add_important_intel(intel)
        if intel_list:
            changes["important_intel"] = intel_list

        return changes

    def _apply_story_state(self, update: dict) -> dict:
        applied = {}
        if update.get("new_date"):
            self.repo.update_story_state(current_date=update["new_date"])
            applied["date"] = update["new_date"]
        if update.get("new_time"):
            self.repo.update_story_state(current_time=update["new_time"])
            applied["time"] = update["new_time"]
        if update.get("new_scene"):
            self.repo.update_story_state(current_scene=update["new_scene"])
            applied["scene"] = update["new_scene"]
        return applied

    def _apply_player_changes(self, changes: dict) -> dict:
        applied = {}
        player = self.repo.get_player()

        updates = {}
        if changes.get("currency_change"):
            new_currency = player.currency + changes["currency_change"]
            updates["currency"] = max(0, new_currency)
            applied["currency"] = new_currency
        if changes.get("hp_change"):
            new_hp = player.hp + changes["hp_change"]
            updates["hp"] = max(0, min(player.max_hp, new_hp))
            applied["hp"] = new_hp
        if changes.get("en_change"):
            new_en = player.en + changes["en_change"]
            updates["en"] = max(0, min(player.max_en, new_en))
            applied["en"] = new_en
        if changes.get("new_status"):
            raw_status = changes["new_status"]
            try:
                PlayerStatus(raw_status)
                updates["status"] = raw_status
                applied["status"] = raw_status
            except ValueError:
                pass  # 忽略 LLM 返回的无效状态值
        if changes.get("new_title"):
            updates["title"] = changes["new_title"]
            applied["title"] = changes["new_title"]
        if changes.get("exp_gain"):
            new_exp = player.current_exp + changes["exp_gain"]
            level_ups = 0
            while new_exp >= player.max_exp:
                new_exp -= player.max_exp
                level_ups += 1
            updates["current_exp"] = new_exp
            if level_ups > 0:
                updates["level"] = player.level + level_ups
                applied["level_up"] = level_ups
            applied["exp"] = new_exp

        if updates:
            self.repo.update_player(**updates)
        return applied

    def _apply_npc_updates(self, npc_list: list[dict]) -> list[str]:
        updated_names = []
        for data in npc_list:
            name = data.get("name", "")
            if not name:
                continue
            existing = self.repo.get_npc_by_name(name)
            if existing:
                # 更新现有 NPC
                updates = {}
                for field in ["occupation", "appearance", "personality", "equipment",
                              "inner_thought", "intel", "status", "h_preferences"]:
                    if data.get(field):
                        updates[field] = data[field]
                if "is_present" in data:
                    updates["is_present"] = bool(data["is_present"])
                if data.get("affection_change"):
                    new_exp = existing.affection_exp + data["affection_change"]
                    level_ups = 0
                    while new_exp >= existing.affection_exp_max:
                        new_exp -= existing.affection_exp_max
                        level_ups += 1
                    updates["affection_exp"] = new_exp
                    if level_ups > 0:
                        updates["affection_level"] = existing.affection_level + level_ups
                if data.get("sex_count_change"):
                    updates["sex_count"] = existing.sex_count + data["sex_count_change"]

                # 使用 upsert 更新
                for k, v in updates.items():
                    setattr(existing, k, v)
                self.repo.upsert_npc(existing)
            else:
                # 创建新 NPC（or 用于处理 LLM 返回 null 的情况）
                npc = NPC(
                    name=name,
                    gender=data.get("gender") or "男",
                    occupation=data.get("occupation") or "",
                    appearance=data.get("appearance") or "",
                    personality=data.get("personality") or "",
                    equipment=data.get("equipment") or "无",
                    inner_thought=data.get("inner_thought") or "",
                    intel=data.get("intel") or "",
                    status=data.get("status") or "正常",
                    is_present=bool(data.get("is_present", False)),
                    h_preferences=data.get("h_preferences") or "",
                )
                # 新 NPC 也应用数值变更
                if data.get("affection_change"):
                    npc.affection_exp = max(0, data["affection_change"])
                if data.get("sex_count_change"):
                    npc.sex_count = max(0, data["sex_count_change"])
                self.repo.upsert_npc(npc)
            updated_names.append(name)
        return updated_names

    def _apply_items(self, items_data: list[dict]) -> list[str]:
        names = []
        for data in items_data:
            item = Item(
                category=data.get("category") or "道具",
                name=data.get("name") or "未知道具",
                quality=Quality(data.get("quality") or "普通"),
                status=data.get("status") or "完好",
            )
            self.repo.upsert_item(item)
            names.append(item.name)
        return names

    def _apply_regions(self, regions_data: list[dict]) -> list[str]:
        names = []
        for data in regions_data:
            region = Region(
                name=data.get("name") or "未知区域",
                security=SecurityLevel(data.get("security") or "普通区"),
                danger=DangerLevel(data.get("danger") or "无威胁"),
                entry_requirement=data.get("entry_requirement") or "无",
                landmarks=data.get("landmarks") or "",
            )
            self.repo.upsert_region(region)
            names.append(region.name)
        return names

    def _apply_relationships(self, rels_data: list[dict]) -> list[str]:
        summaries = []
        for data in rels_data:
            rel = Relationship(
                character_a=data.get("character_a") or "",
                character_b=data.get("character_b") or "",
                relation_type=data.get("relation_type") or "",
                description=data.get("description") or "",
            )
            self.repo.upsert_relationship(rel)
            summaries.append(f"{rel.character_a}→{rel.character_b}: {rel.relation_type}")
        return summaries
