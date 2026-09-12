"""记忆整合器——确定性算法，不依赖 LLM 记忆能力。

核心原理：
- 临时记忆 10 条满 → 用 LLM 提炼为 1 条永久记忆
- 永久记忆 6 条满 → 用 LLM 压缩为 3 条
- 剧情摘要每次永久记忆变更时重写
- 所有状态持久化在 SQLite，不依赖上下文窗口
"""

from ..models import TempMemory, PermanentMemory


def build_integration_prompt(temp_memories: list[TempMemory]) -> str:
    """构建整合提示词：将 10 条临时记忆浓缩为 1 条永久记忆。"""
    items = "\n".join(
        f"{m.seq}. [{m.datetime_str}] @ {m.scene} | 事件：{m.event} -> 结果：{m.result}"
        for m in temp_memories
    )
    return f"""将以下 10 条临时记忆整合为 1 条永久记忆。

规则：
1. 只保留关键事件（人名、地名、重要物品、事件结果），删除氛围描写和心情描述
2. 不超过 50 字
3. 格式：[日期时间段] @ [核心场景] | [核心事件概述及长远影响]

临时记忆：
{items}

请只输出永久记忆内容，不要多余文字。"""


def build_compress_prompt(perm_memories: list[PermanentMemory]) -> str:
    """构建压缩提示词：将 6 条永久记忆压缩为 3 条。"""
    items = "\n".join(
        f"{m.seq}. [{m.datetime_range}] @ {m.scene} | {m.summary}"
        for m in perm_memories
    )
    return f"""将以下 6 条永久记忆压缩为 3 条。

规则：
1. 不得丢失任何重要信息（人名、地名、关键事件、重要物品）
2. 每条不超过 100 字
3. 合并相关事件，删除冗余

原始记忆：
{items}

请输出 3 条压缩后的永久记忆，格式为：
1. [日期] @ [场景] | [内容]
2. [日期] @ [场景] | [内容]
3. [日期] @ [场景] | [内容]"""


def build_summary_prompt(perm_memories: list[PermanentMemory], existing_summary: str) -> str:
    """构建剧情摘要生成提示词。"""
    items = "\n".join(
        f"- [{m.datetime_range}] @ {m.scene}: {m.summary}"
        for m in perm_memories
    )
    return f"""根据以下永久记忆，用不超过三句话概括所有内容的核心。

现有摘要：{existing_summary or '无'}

永久记忆：
{items}

请输出新的剧情摘要（仅输出摘要内容）："""


def parse_memory_response(text: str) -> str:
    """从 LLM 响应中提取纯净的记忆文本。"""
    text = text.strip().strip('"').strip("'")
    # 常见的前缀清理
    for prefix in ["永久记忆：", "整合后：", "记忆：", "输出："]:
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    return text


def parse_compressed_memories(text: str) -> list[str]:
    """解析压缩后的多条记忆。"""
    import re
    # 匹配 "1. xxx" 或 "1）xxx" 格式
    parts = re.split(r'\n(?=\d+[\.\)）])', text.strip())
    if len(parts) < 2:
        # 尝试按数字+标点分割
        parts = re.split(r'(?<=\n)(?=\d+[\.\)）])', text.strip())
    results = []
    for part in parts:
        cleaned = re.sub(r'^\d+[\.\)）]\s*', '', part.strip())
        if cleaned:
            results.append(cleaned)
    return results
