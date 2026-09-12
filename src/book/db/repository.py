"""数据库操作层——所有持久化读写。"""

from __future__ import annotations
import sqlite3
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..models import (
    Player, PlayerStatus, EquipmentSlot,
    NPC,
    Relationship,
    Item, Quality,
    Region, SecurityLevel, DangerLevel,
    TempMemory,
    PermanentMemory,
    StoryState,
    Suggestion,
)

# ─── SQL 列名白名单（防止通过 kwargs 注入） ───────────────────────────────

_VALID_COLUMNS = {
    "story_state": {"current_date", "current_time", "current_scene", "chapter"},
    "player": {
        "name", "age", "height_cm", "weight_kg", "title", "currency",
        "level", "current_exp", "max_exp", "hp", "max_hp", "atk",
        "en", "max_en", "status", "core_trait", "appearance_bonus",
    },
    "npcs": {
        "gender", "occupation", "appearance", "personality", "equipment",
        "affection_level", "affection_exp", "affection_exp_max", "sex_count",
        "inner_thought", "intel", "status", "is_present", "h_preferences",
    },
    "player_equipment": {"category", "name", "integrity"},
}


def _validate_columns(table: str, kwargs: dict) -> None:
    """验证 kwargs 的 key 都在对应表的白名单内，防止列名注入。"""
    allowed = _VALID_COLUMNS.get(table)
    if allowed is None:
        return  # 未定义白名单的表，放行（如 temp_memories）但需人工审查
    for key in kwargs:
        if key not in allowed:
            raise ValueError(f"非法列名 '{key}' 对于表 '{table}'，允许的列: {allowed}")


class Repository:
    """统一数据访问层，每个实例绑定一个 SQLite 连接。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._in_transaction = False

    def _commit(self):
        """内部提交——在事务中跳过，由外层 _Transaction 统一提交。"""
        if not self._in_transaction:
            self.conn.commit()

    def transaction(self):
        """返回事务上下文管理器，用于包裹多步写操作。"""
        return _Transaction(self)

    def init_db(self):
        """首次运行时初始化表结构与默认数据。"""
        from .schema import SCHEMA_SQL, INIT_SQL
        self.conn.executescript(SCHEMA_SQL)
        self.conn.executescript(INIT_SQL)
        self._commit()

    def close(self):
        self.conn.close()

    # ─── 故事状态 ─────────────────────────────────────────────────────────

    def get_story_state(self) -> StoryState:
        row = self.conn.execute("SELECT * FROM story_state WHERE id=1").fetchone()
        return StoryState(
            current_date=row["current_date"],
            current_time=row["current_time"],
            current_scene=row["current_scene"],
            chapter=row["chapter"],
        )

    def update_story_state(self, **kwargs):
        _validate_columns("story_state", kwargs)
        sets = ", ".join(f"{k}=?" for k in kwargs)
        values = list(kwargs.values())
        self.conn.execute(f"UPDATE story_state SET {sets} WHERE id=1", values)
        self._commit()

    # ─── 玩家 ─────────────────────────────────────────────────────────────

    def get_player(self) -> Player:
        row = self.conn.execute("SELECT * FROM player WHERE id=1").fetchone()
        eq_rows = self.conn.execute(
            "SELECT * FROM player_equipment ORDER BY id"
        ).fetchall()
        equipment = [
            EquipmentSlot(
                slot=r["slot"], category=r["category"],
                name=r["name"], integrity=r["integrity"]
            )
            for r in eq_rows
        ]
        try:
            status = PlayerStatus(row["status"])
        except ValueError:
            status = PlayerStatus.NORMAL

        return Player(
            name=row["name"], age=row["age"], height_cm=row["height_cm"],
            weight_kg=row["weight_kg"], title=row["title"], currency=row["currency"],
            level=row["level"], current_exp=row["current_exp"], max_exp=row["max_exp"],
            hp=row["hp"], max_hp=row["max_hp"], atk=row["atk"],
            en=row["en"], max_en=row["max_en"],
            status=status, core_trait=row["core_trait"],
            equipment=equipment, appearance_bonus=row["appearance_bonus"],
        )

    def update_player(self, **kwargs):
        """按字段更新玩家数据。"""
        _validate_columns("player", kwargs)
        if not kwargs:
            return
        sets = ", ".join(f"{k}=?" for k in kwargs)
        values = list(kwargs.values())
        self.conn.execute(f"UPDATE player SET {sets} WHERE id=1", values)
        self._commit()

    def update_player_equipment(self, slot: str, **kwargs):
        _validate_columns("player_equipment", kwargs)
        sets = ", ".join(f"{k}=?" for k in kwargs)
        values = list(kwargs.values()) + [slot]
        self.conn.execute(
            f"UPDATE player_equipment SET {sets} WHERE slot=?", values
        )
        self._commit()

    # ─── NPC ──────────────────────────────────────────────────────────────

    def get_all_npcs(self) -> list[NPC]:
        rows = self.conn.execute("SELECT * FROM npcs ORDER BY id").fetchall()
        return [_row_to_npc(r) for r in rows]

    def get_npcs_by_gender(self, gender: str) -> list[NPC]:
        rows = self.conn.execute(
            "SELECT * FROM npcs WHERE gender=? ORDER BY id", (gender,)
        ).fetchall()
        return [_row_to_npc(r) for r in rows]

    def get_npc_by_name(self, name: str) -> Optional[NPC]:
        row = self.conn.execute("SELECT * FROM npcs WHERE name=?", (name,)).fetchone()
        return _row_to_npc(row) if row else None

    def upsert_npc(self, npc: NPC) -> int:
        """插入或更新 NPC，返回 id。"""
        existing = self.get_npc_by_name(npc.name)
        if existing:
            fields = [
                "gender", "occupation", "appearance", "personality",
                "equipment", "affection_level", "affection_exp",
                "affection_exp_max", "sex_count", "inner_thought",
                "intel", "status", "is_present", "h_preferences"
            ]
            _validate_columns("npcs", {f: True for f in fields})
            sets = ", ".join(f"{f}=?" for f in fields)
            values = [getattr(npc, f) for f in fields] + [npc.name]
            self.conn.execute(f"UPDATE npcs SET {sets} WHERE name=?", values)
            self._commit()
            return existing.id
        else:
            cur = self.conn.execute(
                """INSERT INTO npcs (name,gender,occupation,appearance,personality,
                   equipment,affection_level,affection_exp,affection_exp_max,
                   sex_count,inner_thought,intel,status,is_present,h_preferences)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (npc.name, npc.gender, npc.occupation, npc.appearance,
                 npc.personality, npc.equipment, npc.affection_level,
                 npc.affection_exp, npc.affection_exp_max, npc.sex_count,
                 npc.inner_thought, npc.intel, npc.status,
                 1 if npc.is_present else 0, npc.h_preferences)
            )
            self._commit()
            return cur.lastrowid

    def remove_npc(self, name: str):
        self.conn.execute("DELETE FROM npcs WHERE name=?", (name,))
        self._commit()

    # ─── 关系网络 ─────────────────────────────────────────────────────────

    def get_all_relationships(self) -> list[Relationship]:
        rows = self.conn.execute("SELECT * FROM relationships ORDER BY id").fetchall()
        return [Relationship(
            id=r["id"], character_a=r["character_a"], character_b=r["character_b"],
            relation_type=r["relation_type"], description=r["description"]
        ) for r in rows]

    def upsert_relationship(self, rel: Relationship):
        existing = self.conn.execute(
            "SELECT id FROM relationships WHERE character_a=? AND character_b=? AND relation_type=?",
            (rel.character_a, rel.character_b, rel.relation_type)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE relationships SET description=? WHERE id=?",
                (rel.description, existing["id"])
            )
        else:
            self.conn.execute(
                "INSERT INTO relationships (character_a,character_b,relation_type,description) VALUES (?,?,?,?)",
                (rel.character_a, rel.character_b, rel.relation_type, rel.description)
            )
        self._commit()

    def clear_relationships(self):
        self.conn.execute("DELETE FROM relationships")
        self._commit()

    # ─── 道具 ─────────────────────────────────────────────────────────────

    def get_all_items(self) -> list[Item]:
        rows = self.conn.execute("SELECT * FROM items ORDER BY id").fetchall()
        return [Item(
            id=r["id"], category=r["category"], name=r["name"],
            quality=Quality(r["quality"]), status=r["status"]
        ) for r in rows]

    def upsert_item(self, item: Item):
        existing = self.conn.execute(
            "SELECT id FROM items WHERE category=? AND name=?", (item.category, item.name)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE items SET quality=?, status=? WHERE id=?",
                (item.quality.value, item.status, existing["id"])
            )
        else:
            self.conn.execute(
                "INSERT INTO items (category,name,quality,status) VALUES (?,?,?,?)",
                (item.category, item.name, item.quality.value, item.status)
            )
        self._commit()

    def remove_item(self, category: str, name: str):
        self.conn.execute("DELETE FROM items WHERE category=? AND name=?", (category, name))
        self._commit()

    # ─── 区域 ─────────────────────────────────────────────────────────────

    def get_all_regions(self) -> list[Region]:
        rows = self.conn.execute("SELECT * FROM regions ORDER BY id").fetchall()
        return [Region(
            id=r["id"], name=r["name"], security=SecurityLevel(r["security"]),
            danger=DangerLevel(r["danger"]), entry_requirement=r["entry_requirement"],
            landmarks=r["landmarks"]
        ) for r in rows]

    def upsert_region(self, region: Region):
        existing = self.conn.execute(
            "SELECT id FROM regions WHERE name=?", (region.name,)
        ).fetchone()
        if existing:
            self.conn.execute(
                "UPDATE regions SET security=?,danger=?,entry_requirement=?,landmarks=? WHERE id=?",
                (region.security.value, region.danger.value, region.entry_requirement,
                 region.landmarks, existing["id"])
            )
        else:
            self.conn.execute(
                "INSERT INTO regions (name,security,danger,entry_requirement,landmarks) VALUES (?,?,?,?,?)",
                (region.name, region.security.value, region.danger.value,
                 region.entry_requirement, region.landmarks)
            )
        self._commit()

    # ─── 记忆系统 ─────────────────────────────────────────────────────────

    def get_temp_memories(self) -> list[TempMemory]:
        rows = self.conn.execute(
            "SELECT * FROM temp_memories ORDER BY seq"
        ).fetchall()
        return [TempMemory(
            id=r["id"], seq=r["seq"], datetime_str=r["datetime_str"],
            scene=r["scene"], event=r["event"], result=r["result"],
            created_at=datetime.fromisoformat(r["created_at"])
        ) for r in rows]

    def temp_memory_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM temp_memories").fetchone()
        return row["cnt"]

    def add_temp_memory(self, mem: TempMemory):
        self.conn.execute(
            "INSERT INTO temp_memories (seq,datetime_str,scene,event,result) VALUES (?,?,?,?,?)",
            (mem.seq, mem.datetime_str, mem.scene, mem.event, mem.result)
        )
        self._commit()

    def clear_temp_memories(self):
        self.conn.execute("DELETE FROM temp_memories")
        self._commit()

    def get_perm_memories(self) -> list[PermanentMemory]:
        rows = self.conn.execute(
            "SELECT * FROM perm_memories ORDER BY seq"
        ).fetchall()
        return [PermanentMemory(
            id=r["id"], seq=r["seq"], datetime_range=r["datetime_range"],
            scene=r["scene"], summary=r["summary"],
            created_at=datetime.fromisoformat(r["created_at"])
        ) for r in rows]

    def perm_memory_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM perm_memories").fetchone()
        return row["cnt"]

    def add_perm_memory(self, mem: PermanentMemory):
        self.conn.execute(
            "INSERT INTO perm_memories (seq,datetime_range,scene,summary) VALUES (?,?,?,?)",
            (mem.seq, mem.datetime_range, mem.scene, mem.summary)
        )
        self._commit()

    def replace_perm_memories(self, memories: list[PermanentMemory]):
        """替换全部永久记忆（用于压缩整合）。在单个事务中完成。"""
        with self.transaction():
            self.conn.execute("DELETE FROM perm_memories")
            rows = [
                (i, m.datetime_range, m.scene, m.summary)
                for i, m in enumerate(memories, 1)
            ]
            self.conn.executemany(
                "INSERT INTO perm_memories (seq,datetime_range,scene,summary) VALUES (?,?,?,?)",
                rows,
            )

    def get_plot_summary(self) -> str:
        row = self.conn.execute("SELECT content FROM plot_summary WHERE id=1").fetchone()
        return row["content"] if row else ""

    def update_plot_summary(self, content: str):
        self.conn.execute("UPDATE plot_summary SET content=? WHERE id=1", (content,))
        self._commit()

    def get_important_intel(self) -> list[str]:
        rows = self.conn.execute("SELECT content FROM important_intel ORDER BY id").fetchall()
        return [r["content"] for r in rows]

    def add_important_intel(self, content: str):
        self.conn.execute("INSERT INTO important_intel (content) VALUES (?)", (content,))
        self._commit()

    # ─── 建议 ─────────────────────────────────────────────────────────────

    def get_suggestions(self) -> Suggestion:
        row = self.conn.execute("SELECT * FROM suggestions WHERE id=1").fetchone()
        return Suggestion(
            conservative=row["conservative"] or "",
            aggressive=row["aggressive"] or "",
            creative=row["creative"] or "",
        )

    def update_suggestions(self, conservative="", aggressive="", creative=""):
        self.conn.execute(
            "UPDATE suggestions SET conservative=?,aggressive=?,creative=? WHERE id=1",
            (conservative, aggressive, creative)
        )
        self._commit()

    # ─── 会话历史 ─────────────────────────────────────────────────────────

    def get_session_rounds(self, limit: int = 5) -> list[dict]:
        if limit <= 0:
            rows = self.conn.execute(
                "SELECT * FROM session_history ORDER BY id ASC"
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM session_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            rows = list(reversed(rows))
        return [dict(r) for r in rows]

    def add_session(self, round_number: int, user_input: str, narrative: str, state: dict):
        self.conn.execute(
            "INSERT INTO session_history (round_number,user_input,narrative,story_state_json) VALUES (?,?,?,?)",
            (round_number, user_input, narrative, json.dumps(state, ensure_ascii=False))
        )
        self._commit()

    def get_round_count(self) -> int:
        row = self.conn.execute("SELECT COUNT(*) as cnt FROM session_history").fetchone()
        return row["cnt"]

    # ─── 故事设定 ─────────────────────────────────────────────────────────

    def get_setting(self, key: str) -> str:
        """读取单个故事设定。"""
        row = self.conn.execute(
            "SELECT value FROM story_settings WHERE key=?", (key,)
        ).fetchone()
        return row["value"] if row else ""

    def get_all_settings(self) -> dict[str, str]:
        """读取所有故事设定。"""
        rows = self.conn.execute("SELECT key, value FROM story_settings").fetchall()
        return {r["key"]: r["value"] for r in rows}

    def set_setting(self, key: str, value: str):
        """写入或更新单个故事设定。"""
        self.conn.execute(
            "INSERT INTO story_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
        self._commit()

    def set_settings_batch(self, settings: dict[str, str]):
        """批量写入故事设定。"""
        with self.transaction():
            for key, value in settings.items():
                self.conn.execute(
                    "INSERT INTO story_settings (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value)
                )


def _row_to_npc(row: sqlite3.Row) -> NPC:
    return NPC(
        id=row["id"],
        name=row["name"],
        gender=row["gender"],
        occupation=row["occupation"] or "",
        appearance=row["appearance"] or "",
        personality=row["personality"] or "",
        equipment=row["equipment"] or "无",
        affection_level=row["affection_level"],
        affection_exp=row["affection_exp"],
        affection_exp_max=row["affection_exp_max"],
        sex_count=row["sex_count"],
        inner_thought=row["inner_thought"] or "",
        intel=row["intel"] or "",
        status=row["status"] or "正常",
        is_present=bool(row["is_present"]),
        h_preferences=row["h_preferences"] or "",
    )


class _Transaction:
    """SQLite 事务上下文管理器——自动 commit 或 rollback，并抑制内部 _commit 调用。"""

    def __init__(self, repo: Repository):
        self.repo = repo

    def __enter__(self):
        self.repo.conn.execute("BEGIN")
        self.repo._in_transaction = True
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.repo._in_transaction = False
        if exc_type is None:
            self.repo.conn.commit()
        else:
            self.repo.conn.rollback()
        return False  # 不吞异常
