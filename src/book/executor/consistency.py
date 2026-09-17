"""一致性检查——纯规则的三类检查（P2-2）。

对应《开发计划.md》P2-2。三类检查：

1. **时间线**：故事内日期/时间必须单调不减；倒挂即报（severity=high）
2. **状态**：`npcs.is_present` 与最近一轮正文是否吻合；`player.hp/en` 是否越界
3. **伏笔**：`status='未收'` 且埋设章号距今超过阈值（默认 30 章）→ 提醒回收

前两类纯规则、不需要 LLM。可选的 `--deep` 才会调用模型做深度比对（见 deep_check）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..db.repository import Repository

# 伏笔"埋太久没回收"的默认章数阈值
DEFAULT_FORESHADOW_STALE = 30

# §5 规定的问题枚举——外部输入（LLM）必须按这两个集合收敛
VALID_KINDS = {"timeline", "state", "foreshadow", "ability"}
VALID_SEVERITIES = {"high", "mid", "low"}

# 正文里至少要出现几次才认为"这个角色确实在场/确实不在场"，
# 避免只被提一次名字（比如回忆里）就误报
DEFAULT_PRESENCE_MIN_MENTIONS = 2


@dataclass
class Issue:
    """一条一致性问题，字段与 §5 的 check 输出一一对应。"""

    kind: str          # timeline | state | foreshadow | ability
    severity: str      # high | mid | low
    detail: str
    refs: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "detail": self.detail,
            "refs": self.refs,
        }


# ─── 日期时间解析 ──────────────────────────────────────────────────────────


_CN_DATE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_ISO_DATE = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_CN_TIME = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})")
_ISO_TIME = re.compile(r"[T ](\d{1,2}):(\d{2})")


def parse_story_time(date_str: str, time_str: str) -> tuple[int, int, int, int, int] | None:
    """把"2026年06月22日 星期一" + "上午 08:00" 解析成可比较的元组。

    解析不出来返回 None（该轮跳过时间线检查，而不是报一个假问题）。
    """
    date_str = (date_str or "").strip()
    time_str = (time_str or "").strip()

    date_part = None
    m = _CN_DATE.search(date_str) or _ISO_DATE.search(date_str)
    if m:
        date_part = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if date_part is None:
        return None

    # 时刻：先看 time_str，再看 date_str 里内嵌的（"2026-06-22 14:00" 这种）
    time_part = None
    for source in (time_str, date_str):
        if not source:
            continue
        m2 = _CN_TIME.search(source) or _ISO_TIME.search(source)
        if not m2:
            continue
        hour, minute = int(m2.group(1)), int(m2.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            time_part = (hour, minute)
            break
        # 找到了时刻但数值非法（如 "08:99"）：不猜，直接放弃这一轮，
        # 否则会被当成 00:00 造出"时间倒挂"的假阳性
        return None
    if time_part is None:
        time_part = (0, 0)

    return (*date_part, *time_part)


def _session_state(row: dict) -> dict:
    """session_history.story_state_json → dict（解析失败返回空 dict）。"""
    raw = row.get("story_state_json")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


# ─── 三类检查 ──────────────────────────────────────────────────────────────


def check_timeline(sessions: list[dict]) -> list[Issue]:
    """故事内时间必须单调不减；倒挂即报。

    计划写的是"同一卷内"。有 volume 列时按卷分组比较（跨卷时间回退是正常的：
    第二卷可能倒叙）；没有卷信息时退化为全局序。
    """
    groups: dict[object, list[dict]] = {}
    for row in sessions:
        groups.setdefault(row.get("volume"), []).append(row)

    issues: list[Issue] = []
    for group in groups.values():
        issues.extend(_check_timeline_group(group))
    return issues


def _check_timeline_group(sessions: list[dict]) -> list[Issue]:
    issues: list[Issue] = []
    prev: tuple[int, int, int, int, int] | None = None
    prev_row: dict | None = None

    for row in sessions:
        state = _session_state(row)
        current = parse_story_time(state.get("current_date", ""), state.get("current_time", ""))
        if current is None:
            continue
        if prev is not None and current < prev:
            text = lambda t: f"{t[0]:04d}-{t[1]:02d}-{t[2]:02d} {t[3]:02d}:{t[4]:02d}"  # noqa: E731
            issues.append(
                Issue(
                    kind="timeline",
                    severity="high",
                    detail=(
                        f"第 {prev_row['round_number']} 轮的时间是 {text(prev)}，"
                        f"第 {row['round_number']} 轮却回退到 {text(current)}"
                    ),
                    refs=[
                        {"table": "session_history", "id": prev_row["id"]},
                        {"table": "session_history", "id": row["id"]},
                    ],
                )
            )
        prev, prev_row = current, row

    return issues


def check_state(
    repo: Repository,
    sessions: list[dict],
    *,
    min_mentions: int = DEFAULT_PRESENCE_MIN_MENTIONS,
) -> list[Issue]:
    """npcs.is_present 与最近一轮正文是否吻合；player.hp/en 是否越界。"""
    issues: list[Issue] = []

    # 2a. 历史轮次存档里的数值越界（story_state_json 可扩展字段）。
    #     注意：生产路径的 StoryState.model_dump() 不含 hp/en，
    #     真正的越界判断在 2b（读 player 表）。
    for row in sessions:
        state = _session_state(row)
        if not state:
            continue
        hp, max_hp = state.get("hp"), state.get("max_hp")
        en, max_en = state.get("en"), state.get("max_en")
        if isinstance(hp, int) and isinstance(max_hp, int) and not (0 <= hp <= max_hp):
            issues.append(Issue(
                kind="state", severity="mid",
                detail=f"第 {row['round_number']} 轮 HP={hp} 超出 [0, {max_hp}]",
                refs=[{"table": "session_history", "id": row["id"]}],
            ))
        if isinstance(en, int) and isinstance(max_en, int) and not (0 <= en <= max_en):
            issues.append(Issue(
                kind="state", severity="mid",
                detail=f"第 {row['round_number']} 轮 EN={en} 超出 [0, {max_en}]",
                refs=[{"table": "session_history", "id": row["id"]}],
            ))

    # 2b. 玩家数值越界：**读 player 表**。
    #     P0/P1 的 StoryState 里没有 hp/en（生产写入的 model_dump 只有
    #     date/time/scene/chapter），所以只查 story_state_json 永远不会触发。
    try:
        player = repo.get_player()
        if not (0 <= player.hp <= player.max_hp):
            issues.append(Issue(
                kind="state", severity="mid",
                detail=f"玩家 HP={player.hp} 超出 [0, {player.max_hp}]",
                refs=[{"table": "player", "id": 1}],
            ))
        if not (0 <= player.en <= player.max_en):
            issues.append(Issue(
                kind="state", severity="mid",
                detail=f"玩家 EN={player.en} 超出 [0, {player.max_en}]",
                refs=[{"table": "player", "id": 1}],
            ))
    except Exception:  # noqa: BLE001 - player 表缺失等异常不应让 check 整体失败
        pass

    # 2c. 在场标记 vs 最近一轮正文
    if not sessions:
        return issues
    latest = sessions[-1]
    narrative = latest.get("narrative") or ""
    if not narrative.strip():
        return issues

    for npc in repo.get_all_npcs():
        name = (npc.name or "").strip()
        # 单字名/空名不做计数：中文里「林」会命中「林越」，必然假阳性；
        # 空串更糟（str.count("") 返回长度+1）。要用单字名请登记别名。
        if len(name) < 2:
            continue
        mentions = narrative.count(name)
        if npc.is_present and mentions == 0:
            issues.append(Issue(
                kind="state", severity="mid",
                detail=(
                    f"NPC「{name}」标记为在场（is_present=1），"
                    f"但第 {latest['round_number']} 轮正文里一次都没出现"
                ),
                refs=[{"table": "npcs", "id": npc.id},
                      {"table": "session_history", "id": latest["id"]}],
            ))
        elif not npc.is_present and mentions >= min_mentions:
            issues.append(Issue(
                kind="state", severity="mid",
                detail=(
                    f"NPC「{name}」标记为不在场（is_present=0），"
                    f"但第 {latest['round_number']} 轮正文里出现了 {mentions} 次"
                ),
                refs=[{"table": "npcs", "id": npc.id},
                      {"table": "session_history", "id": latest["id"]}],
            ))

    return issues


def check_foreshadows(
    repo: Repository,
    *,
    current_chapter: int | None,
    stale_after: int = DEFAULT_FORESHADOW_STALE,
) -> list[Issue]:
    """未收伏笔埋太久没回收 → 提醒。"""
    issues: list[Issue] = []
    if current_chapter is None:
        return issues

    for row in repo.list_foreshadows(status="未收"):
        planted = row.get("planted_chapter")
        if planted is None:
            continue
        gap = current_chapter - planted
        if gap > stale_after:
            issues.append(Issue(
                kind="foreshadow",
                severity="low",
                detail=(
                    f"伏笔「{row['name']}」在第 {planted} 章埋下，"
                    f"距今 {gap} 章仍未回收（阈值 {stale_after}）"
                ),
                refs=[{"table": "foreshadows", "id": row["id"]}],
            ))
    return issues


# ─── 主入口 ────────────────────────────────────────────────────────────────


def run_checks(
    repo: Repository,
    *,
    range_start: int | None = None,
    range_end: int | None = None,
    stale_after: int = DEFAULT_FORESHADOW_STALE,
    min_mentions: int = DEFAULT_PRESENCE_MIN_MENTIONS,
) -> list[Issue]:
    """跑完三类纯规则检查，返回问题列表（按严重度排序）。"""
    sessions = [
        row for row in repo.get_session_rounds(limit=0)
        if (range_start is None or row["round_number"] >= range_start)
        and (range_end is None or row["round_number"] <= range_end)
    ]

    issues: list[Issue] = []
    issues.extend(check_timeline(sessions))
    issues.extend(check_state(repo, sessions, min_mentions=min_mentions))

    current_chapter = repo.get_story_state().chapter
    issues.extend(check_foreshadows(repo, current_chapter=current_chapter,
                                    stale_after=stale_after))

    order = {"high": 0, "mid": 1, "low": 2}
    issues.sort(key=lambda i: (order.get(i.severity, 9), i.kind))
    return issues


def deep_check(repo: Repository, issues: list[Issue], llm_call=None) -> list[Issue]:
    """可选深度模式：用 LLM 对比"实体卡片 vs 章节摘要"找矛盾。

    没有 llm_call（未配置 Key）时原样返回，不做任何事——
    纯规则检查永远可用，AI 只是增强。
    """
    if llm_call is None:
        return issues

    cards = []
    for npc in repo.get_all_npcs():
        cards.append(f"- {npc.name}：{npc.status}｜{npc.occupation}｜{npc.intel}")
    for item in repo.get_all_items():
        cards.append(f"- [道具] {item.name}：{item.quality.value}｜{item.status}")
    if not cards:
        return issues

    summaries = [
        f"第{r['round_number']}轮：{(r.get('narrative') or '')[:200]}"
        for r in repo.get_session_rounds(limit=10)
    ]
    if not summaries:
        return issues

    prompt = (
        "你在做小说设定一致性检查。下面是【实体卡片】与【最近章节摘要】，"
        "请找出两者矛盾之处。只输出 JSON："
        '{"issues":[{"kind":"state|ability","severity":"high|mid|low",'
        '"detail":"...","refs":[]}]}。没有矛盾就输出 {"issues":[]}。\n\n'
        "【实体卡片】\n" + "\n".join(cards) + "\n\n"
        "【最近章节摘要】\n" + "\n".join(summaries)
    )

    try:
        raw = llm_call(prompt)
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            return issues
        data = json.loads(raw[start : end + 1])
    except Exception:  # noqa: BLE001 - 深度检查失败不应让 check 整体失败
        return issues

    for item in data.get("issues", []) or []:
        if not isinstance(item, dict) or not item.get("detail"):
            continue
        # 模型输出不可信：kind/severity 必须按 §5 枚举白名单收敛，
        # refs 必须是 list[dict]，否则会污染 CLI 契约
        kind = item.get("kind") if item.get("kind") in VALID_KINDS else "state"
        severity = item.get("severity") if item.get("severity") in VALID_SEVERITIES else "mid"
        refs = item.get("refs")
        if not isinstance(refs, list):
            refs = []
        refs = [r for r in refs if isinstance(r, dict)]
        issues.append(Issue(
            kind=kind,
            severity=severity,
            detail=str(item["detail"]),
            refs=refs,
        ))
    # 合并后重新排序，保持"高→低"的输出契约
    order = {"high": 0, "mid": 1, "low": 2}
    issues.sort(key=lambda i: (order.get(i.severity, 9), i.kind))
    return issues
