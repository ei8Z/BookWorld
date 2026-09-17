"""信息抽取器——从叙事正文中提取结构化数据。

这是系统执行者的核心：读完正文 → 提取变更 → 更新数据库。
不再依赖 LLM 在写作时"同时记得更新信息卡"。
"""

from __future__ import annotations

import sys
import json
from typing import Optional

from ..writer.generator import LLMClient, LLM_FAILURE_MARKER


EXTRACT_PROMPT = """你是一个精确的信息抽取系统。请从以下小说叙事正文中提取结构化信息，以 JSON 格式返回。

## 提取规则

### 1. story_state_update (故事状态更新)
检测正文中是否出现了新的时间、地点：
- new_date: 如果正文明确提到了新的日期（如"第二天"、"6月23日"），返回新日期字符串；否则 null
- new_time: 如果正文明确提到了新的时间（如"下午3点"、"深夜"），返回新时间字符串；否则 null
- new_scene: 如果场景发生了变化（从A地到了B地），返回新场景名称；否则 null

### 2. temp_memory (临时记忆)
只提取1条最关键的事件，包含：
- datetime_str: 事件发生的日期时间（如"2026/06/22 14:00"）
- scene: 事件发生的场景
- event: 核心事件，只记录具体信息（人名、地名、物品、事件结果），禁止记录氛围/心情/态度，不超过40字
- result: 导致的结果，不超过40字

### 3. player_changes (玩家变化)
如果正文中玩家的状态发生变化：
- currency_change: 货币变化（正数为获得，负数为失去）
- hp_change: 血量变化
- en_change: 精力变化
- new_status: 新状态（如果变化）
- new_title: 新身份/头衔（如果获得）
- exp_gain: 获得经验值

### 4. npc_updates (NPC更新)
列出正文中出现或状态变化的 NPC：
- name: 角色名
- gender: "男"或"女"
- occupation: 职业
- appearance: 外貌/衣着描述（如有新描述）
- personality: 性格特征（如有揭示）
- inner_thought: 该角色在当前情境下的内心想法（如果在场）
- is_present: 是否在当前场景
- status: 状态变化描述（如"受伤"、"情绪低落"等）
- affection_change: 好感度变化（正数为增加，负数为减少）
- intel: 新揭示的角色情报
- sex_count_change: 做爱次数增量（正数为新增）

### 5. new_items (新道具)
正文中获得的物品：
- category: "载具"/"道具"/"密钥"/"收藏"
- name: 物品名称
- quality: "传说"/"史诗"/"精良"/"普通"/"破损"
- status: 状态描述

### 6. new_regions (新区域)
正文中首次出现的区域：
- name: 区域名
- security: "安全区"/"普通区"/"灰色区"/"无法地带"
- danger: "无威胁"/"低危"/"中危"/"高危"/"致命"
- entry_requirement: 进入需求描述
- landmarks: 重要地标

### 7. relationships (关系变化)
正文中揭示或变化的角色关系：
- character_a: 角色A
- character_b: 角色B
- relation_type: "敌对"/"恋爱"/"血缘"/"上下级"/"恩人"/"仇人"/"其他"
- description: 简述关系由来

### 8. suggestions (建议行动)
根据当前剧情，为玩家提供 3 个行动建议：
- conservative: 保守策略
- aggressive: 激进/风险策略
- creative: 利用环境/技能策略

### 9. important_intel (重要情报)
正文中揭示的会影响作品走向的重要信息（世界观规则、角色重大成长等）。每条不超过50字。

---

请只输出 JSON，不要其他文字。格式如下：
{
  "story_state_update": {
    "new_date": null,
    "new_time": null,
    "new_scene": null
  },
  "temp_memory": {
    "datetime_str": "",
    "scene": "",
    "event": "",
    "result": ""
  },
  "player_changes": {
    "currency_change": 0,
    "hp_change": 0,
    "en_change": 0,
    "new_status": null,
    "new_title": null,
    "exp_gain": 0
  },
  "npc_updates": [],
  "new_items": [],
  "new_regions": [],
  "relationships": [],
  "suggestions": {
    "conservative": "",
    "aggressive": "",
    "creative": ""
  },
  "important_intel": []
}

正文：
{content}"""


class Extractor:
    """从叙事正文中抽取结构化信息。"""

    def __init__(self, llm: Optional[LLMClient] = None):
        self.llm = llm or LLMClient()
        # 最近一次抽取失败的原因（None 表示没失败）。
        # _empty_result() 与"模型确实没抽到东西"无法区分，调用方需要靠这个字段判断。
        self.last_error: Optional[str] = None

    def extract(self, narrative: str) -> dict:
        """
        从叙事正文中提取所有结构化变更。

        Returns:
            结构化的提取结果字典
        """
        prompt = EXTRACT_PROMPT.replace("{content}", narrative)
        self.last_error = None

        try:
            response = self.llm.complete(
                prompt, temperature=0.2, max_tokens=2048,
                response_format={"type": "json_object"},
            )
            # 降级响应（网络失败 / 无 Key）不是"零变更"，必须让调用方知道抽取没成功
            if response.lstrip().startswith(LLM_FAILURE_MARKER):
                raise RuntimeError("LLM 未返回可用内容（调用失败或未配置 API Key）")
            # 尝试从响应中提取 JSON
            result = self._parse_json(response)
            if not result:
                self.last_error = "LLM 响应无法解析为 JSON"
            return result
        except Exception as e:
            self.last_error = f"{type(e).__name__}: {e}"
            print(f"[Extractor] 提取失败: {e}", file=sys.stderr)
            return self._empty_result()

    @staticmethod
    def _parse_json(text: str) -> dict:
        """从 LLM 响应中提取 JSON。"""
        text = text.strip()
        # 移除可能的 markdown 代码块标记
        if text.startswith("```"):
            lines = text.split("\n")
            # 去掉第一行和最后一行
            text = "\n".join(lines[1:]) if len(lines) > 1 else text
            if text.endswith("```"):
                text = text[:-3]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # 尝试找到 JSON 边界
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end > start:
                try:
                    return json.loads(text[start:end + 1])
                except json.JSONDecodeError:
                    pass
            # 只报长度与首行摘要，避免把整段 prompt 倒进诊断通道
            head = text.splitlines()[0][:60] if text else ""
            print(
                f"[Extractor] JSON 解析失败（响应 {len(text)} 字符）: {head}",
                file=sys.stderr,
            )
            return Extractor._empty_result()

    @staticmethod
    def _empty_result() -> dict:
        return {
            "story_state_update": {"new_date": None, "new_time": None, "new_scene": None},
            "temp_memory": {"datetime_str": "", "scene": "", "event": "", "result": ""},
            "player_changes": {"currency_change": 0, "hp_change": 0, "en_change": 0,
                               "new_status": None, "new_title": None, "exp_gain": 0},
            "npc_updates": [],
            "new_items": [],
            "new_regions": [],
            "relationships": [],
            "suggestions": {"conservative": "", "aggressive": "", "creative": ""},
            "important_intel": [],
        }
