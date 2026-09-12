"""数据模型——使用 Pydantic 定义所有核心数据结构。"""

from __future__ import annotations
from datetime import datetime
from enum import Enum
from typing import Optional
from pydantic import BaseModel, Field


# ─── 枚举 ────────────────────────────────────────────────────────────────

class Quality(str, Enum):
    LEGENDARY = "传说"
    EPIC = "史诗"
    REFINED = "精良"
    NORMAL = "普通"
    BROKEN = "破损"


class SecurityLevel(str, Enum):
    SAFE = "安全区"
    NORMAL = "普通区"
    GRAY = "灰色区"
    LAWLESS = "无法地带"


class DangerLevel(str, Enum):
    NONE = "无威胁"
    LOW = "低危"
    MEDIUM = "中危"
    HIGH = "高危"
    LETHAL = "致命"


class NPCStatus(str, Enum):
    NORMAL = "正常"
    INJURED = "受伤"
    EMOTIONAL = "情绪异常"


class PlayerStatus(str, Enum):
    NORMAL = "正常"
    INJURED = "受伤"
    POISONED = "中毒"
    FATIGUED = "疲劳"
    BUFFED = "增益"


# ─── 记忆系统 ─────────────────────────────────────────────────────────────

class TempMemory(BaseModel):
    id: Optional[int] = None
    seq: int  # 序号 1-10
    datetime_str: str  # "YYYY/MM/DD HH:MM"
    scene: str         # 场景地点
    event: str         # 核心事件（≤40字）
    result: str        # 导致结果（≤40字）
    created_at: datetime = Field(default_factory=datetime.now)


class PermanentMemory(BaseModel):
    id: Optional[int] = None
    seq: int
    datetime_range: str  # "YYYY/MM/DD"
    scene: str
    summary: str  # 核心事件概述及长远影响（≤100字）
    created_at: datetime = Field(default_factory=datetime.now)


# ─── 玩家 ─────────────────────────────────────────────────────────────────

class EquipmentSlot(BaseModel):
    slot: str        # 头部/躯干/左手/右手/腿部
    category: str    # 头盔/外套/武器/盾牌/裤子 或 "未穿戴"
    name: str        # 名称或"空"
    integrity: int   # 0-100


class Player(BaseModel):
    name: str = "林越"
    age: int = 24
    height_cm: int = 175
    weight_kg: float = 62.0
    title: str = ""
    currency: int = 0
    level: int = 1
    current_exp: int = 0
    max_exp: int = 100
    hp: int = 100
    max_hp: int = 100
    atk: int = 10
    en: int = 100
    max_en: int = 100
    status: PlayerStatus = PlayerStatus.NORMAL
    core_trait: str = "变形者"
    equipment: list[EquipmentSlot] = Field(default_factory=list)
    appearance_bonus: int = 0  # 外观评估百分比


# ─── NPC ──────────────────────────────────────────────────────────────────

class NPC(BaseModel):
    id: Optional[int] = None
    name: str
    gender: str  # "男" / "女"
    occupation: str = ""
    appearance: str = ""       # 外貌/衣着描述
    personality: str = ""      # 性格特征，如"温柔/坚强/腹黑"
    equipment: str = "无"      # 携带装备
    affection_level: int = 1
    affection_exp: int = 0
    affection_exp_max: int = 100
    sex_count: int = 0
    inner_thought: str = "当前不在场，无法感知"
    intel: str = ""            # 角色情报
    status: str = "正常"
    is_present: bool = False
    h_preferences: str = ""    # 喜好体位与性癖（尚未解锁则空）


# ─── 关系 ─────────────────────────────────────────────────────────────────

class Relationship(BaseModel):
    id: Optional[int] = None
    character_a: str
    character_b: str
    relation_type: str  # 敌对/恋爱/血缘/上下级/恩人/仇人/其他
    description: str = ""


# ─── 道具 ─────────────────────────────────────────────────────────────────

class Item(BaseModel):
    id: Optional[int] = None
    category: str   # 载具/道具/密钥/收藏
    name: str
    quality: Quality = Quality.NORMAL
    status: str = "完好"


# ─── 区域 ─────────────────────────────────────────────────────────────────

class Region(BaseModel):
    id: Optional[int] = None
    name: str
    security: SecurityLevel = SecurityLevel.NORMAL
    danger: DangerLevel = DangerLevel.NONE
    entry_requirement: str = "无"
    landmarks: str = ""  # • 地标1\n• 地标2


# ─── 故事状态 ─────────────────────────────────────────────────────────────

class TimeOfDay(str, Enum):
    DAWN = "清晨"
    MORNING = "上午"
    NOON = "中午"
    AFTERNOON = "下午"
    EVENING = "晚上"
    NIGHT = "深夜"


class StoryState(BaseModel):
    current_date: str = "2026年06月22日 星期一"  # YYYY年MM月DD日 星期X
    current_time: str = "上午 08:00"  # 时段 HH:MM
    current_scene: str = ""  # 具体地点
    chapter: int = 1


# ─── 建议行动 ─────────────────────────────────────────────────────────────

class Suggestion(BaseModel):
    conservative: str = ""
    aggressive: str = ""
    creative: str = ""


# ─── 执行结果 ─────────────────────────────────────────────────────────────

class ExecutionResult(BaseModel):
    """一轮完整执行的结果。"""
    narrative: str           # 生成的正文
    story_state: StoryState  # 更新后的故事状态
    updated_cards: dict      # 更新的卡片数据，用于格式化输出
    memory_changes: dict     # 记忆变更摘要
