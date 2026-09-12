"""记忆管理器——记忆生命周期管理：添加、整合、压缩。"""

from __future__ import annotations
from typing import Callable, Optional

from ..db.repository import Repository
from ..models import TempMemory, PermanentMemory
from .integrator import (
    build_integration_prompt,
    build_compress_prompt,
    build_summary_prompt,
    parse_memory_response,
    parse_compressed_memories,
)


class MemoryManager:
    """管理临时记忆→永久记忆→剧情摘要的完整生命周期。

    llm_call: 可注入的 LLM 调用函数，签名 (prompt: str) -> str
    """

    def __init__(self, repo: Repository, llm_call: Optional[Callable[[str], str]] = None):
        self.repo = repo
        self.llm_call = llm_call or (lambda p: "")  # 无 LLM 时静默返回空

    def add_temp_memory(self, datetime_str: str, scene: str, event: str, result: str) -> dict:
        """添加一条临时记忆，自动检查触发整合。返回变更摘要。"""
        current_count = self.repo.temp_memory_count()
        new_seq = current_count + 1

        mem = TempMemory(
            seq=new_seq,
            datetime_str=datetime_str,
            scene=scene,
            event=event[:40],  # 硬截断：不超过40字
            result=result[:40],
        )
        self.repo.add_temp_memory(mem)

        result_log = {"action": "added", "temp_count": new_seq, "triggered_integration": False}

        # 检查是否触发整合
        if new_seq >= 10:
            self._integrate_temp_to_perm()
            result_log["triggered_integration"] = True
            result_log["perm_count"] = self.repo.perm_memory_count()

        return result_log

    def _integrate_temp_to_perm(self):
        """将 10 条临时记忆整合为 1 条永久记忆。"""
        temp_mems = self.repo.get_temp_memories()
        if not temp_mems:
            return

        # 用 LLM 整合（如果 LLM 不可用，使用简单拼接作为降级方案）
        prompt = build_integration_prompt(temp_mems)
        response = self.llm_call(prompt)

        if response:
            summary_text = parse_memory_response(response)
        else:
            # 降级：取第一条和最后一条拼接
            summary_text = (
                f"{temp_mems[0].datetime_str}至{temp_mems[-1].datetime_str} "
                f"@ {temp_mems[0].scene} | {temp_mems[0].event}"
            )

        # 确定日期范围
        date_range = temp_mems[0].datetime_str.split()[0] if " " in temp_mems[0].datetime_str else temp_mems[0].datetime_str

        perm_count = self.repo.perm_memory_count()
        perm_mem = PermanentMemory(
            seq=perm_count + 1,
            datetime_range=date_range,
            scene=temp_mems[0].scene,
            summary=summary_text[:100],
        )

        # 在事务中完成：添加永久记忆 → 清空临时记忆 → 检查压缩 → 更新摘要
        with self.repo.transaction():
            self.repo.add_perm_memory(perm_mem)
            self.repo.clear_temp_memories()

        # 检查永久记忆是否需要压缩
        new_perm_count = self.repo.perm_memory_count()
        if new_perm_count >= 6:
            self._compress_perm_memories()

        # 更新剧情摘要
        self._update_plot_summary()

    def _compress_perm_memories(self):
        """将 6 条永久记忆压缩为 3 条。"""
        perm_mems = self.repo.get_perm_memories()
        if len(perm_mems) < 6:
            return

        prompt = build_compress_prompt(perm_mems)
        response = self.llm_call(prompt)

        if response:
            parts = parse_compressed_memories(response)
            if len(parts) >= 3:
                new_mems = []
                for i, text in enumerate(parts[:3], 1):
                    # 简单解析日期和场景
                    perm_mem = PermanentMemory(
                        seq=i,
                        datetime_range=perm_mems[0].datetime_range,
                        scene="综合",
                        summary=text[:100],
                    )
                    new_mems.append(perm_mem)
                self.repo.replace_perm_memories(new_mems)
                return

        # 降级：保留前3条并合并后3条
        if len(perm_mems) >= 6:
            merged_summary = "; ".join(m.summary[:60] for m in perm_mems[3:6])
            new_mems = [
                perm_mems[0],
                perm_mems[1],
                PermanentMemory(
                    seq=3,
                    datetime_range=perm_mems[0].datetime_range,
                    scene="综合",
                    summary=merged_summary[:100],
                ),
            ]
            self.repo.replace_perm_memories(new_mems)

    def _update_plot_summary(self):
        """更新剧情摘要。"""
        perm_mems = self.repo.get_perm_memories()
        if not perm_mems:
            return

        existing = self.repo.get_plot_summary()
        prompt = build_summary_prompt(perm_mems, existing)
        response = self.llm_call(prompt)

        if response:
            self.repo.update_plot_summary(response.strip()[:200])
        elif not existing:
            # 降级：用第一条永久记忆作为摘要
            self.repo.update_plot_summary(perm_mems[0].summary)

    def add_important_intel(self, content: str):
        """添加重要情报。"""
        self.repo.add_important_intel(content)

    def get_context_for_writer(self) -> str:
        """生成给 Writer 模块的上下文（精简版，避免污染）。"""
        parts = []

        summary = self.repo.get_plot_summary()
        if summary:
            parts.append(f"【剧情摘要】{summary}")

        intel = self.repo.get_important_intel()
        if intel:
            parts.append("【重要情报】")
            for item in intel[-5:]:  # 最多5条
                parts.append(f"- {item}")

        # 最近 3 条永久记忆
        perm_mems = self.repo.get_perm_memories()
        if perm_mems:
            parts.append("【近期关键事件】")
            for m in perm_mems[-3:]:
                parts.append(f"- [{m.datetime_range}] @ {m.scene}: {m.summary}")

        return "\n".join(parts) if parts else "（新故事开始，暂无历史记录）"

    def get_memory_state(self) -> dict:
        """获取完整记忆状态，供输出格式化使用。"""
        return {
            "temp_memories": self.repo.get_temp_memories(),
            "temp_count": self.repo.temp_memory_count(),
            "perm_memories": self.repo.get_perm_memories(),
            "perm_count": self.repo.perm_memory_count(),
            "plot_summary": self.repo.get_plot_summary(),
            "important_intel": self.repo.get_important_intel(),
        }
