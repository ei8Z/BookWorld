"""主编排器——orchestrator 串联 Writer → Executor → Memory 流水线。"""

from __future__ import annotations
from pathlib import Path
import sys
import json

from .config import config
from .db.repository import Repository
from .memory.manager import MemoryManager
from .writer.generator import Writer, LLMClient
from .writer.prompt import (
    build_writer_system_prompt,
    build_writer_context,
    build_user_prompt,
)
from .executor.extractor import Extractor
from .executor.card_manager import CardManager
from .executor.formatter import OutputFormatter


class BookEngine:
    """叙事引擎主类——管理完整的一轮生成流水线。"""

    def __init__(self):
        # 初始化数据库
        self.repo = Repository(config.db_path)
        self.repo.init_db()

        # LLM 客户端（Writer 和 Extractor 共享，但用不同 temperature）
        self.llm = LLMClient()

        # 各子系统
        self.memory = MemoryManager(
            self.repo,
            llm_call=lambda p: self.llm.complete(p, temperature=0.3),
        )
        self.writer = Writer(self.llm)
        self.extractor = Extractor(self.llm)
        self.card_manager = CardManager(self.repo, self.memory)
        self.formatter = OutputFormatter(self.repo)

        # 加载故事设定并生成系统提示词
        self._refresh_writer_prompt()

    def _refresh_writer_prompt(self):
        """从数据库读取故事设定，生成注入设定后的 Writer 系统提示词。"""
        settings = self.repo.get_all_settings()
        self.writer_system_prompt = build_writer_system_prompt(settings)

    def _prepare_context(self, user_input: str) -> tuple:
        """准备 Writer 上下文，返回 (round_num, full_user_prompt)。"""
        round_num = self.repo.get_round_count() + 1
        story_state = self.repo.get_story_state()
        player = self.repo.get_player()
        memory_context = self.memory.get_context_for_writer()
        recent_history = self.repo.get_session_rounds(limit=3)

        all_npcs = self.repo.get_all_npcs()
        present_npcs = [n for n in all_npcs if n.is_present]
        npc_context = ""
        if present_npcs:
            npc_lines = []
            for n in present_npcs:
                detail = f"{n.name}（{n.occupation or '未知身份'}）：{n.appearance[:80]}"
                detail += f" | 好感度 Lv.{n.affection_level}"
                detail += f" | 状态: {n.status}"
                if n.inner_thought:
                    detail += f" | 内心: {n.inner_thought[:60]}"
                npc_lines.append(f"- {detail}")
            npc_context = "\n".join(npc_lines)

        # 道具
        items = self.repo.get_all_items()
        items_context = ""
        if items:
            item_lines = []
            for it in items:
                item_lines.append(f"- [{it.category}] {it.name} | {it.quality.value} | {it.status}")
            items_context = "\n".join(item_lines)

        # 区域
        regions = self.repo.get_all_regions()
        regions_context = ""
        if regions:
            region_lines = []
            for r in regions:
                region_lines.append(
                    f"- {r.name} | 危险等级: {r.danger.value} | 治安: {r.security.value} | {r.landmarks}"
                )
            regions_context = "\n".join(region_lines)

        # 关系
        relationships = self.repo.get_all_relationships()
        relationships_context = ""
        if relationships:
            rel_lines = []
            for r in relationships:
                rel_lines.append(f"- {r.character_a} → {r.character_b}：{r.relation_type}，{r.description}")
            relationships_context = "\n".join(rel_lines)

        writer_context = build_writer_context(
            story_state, player, memory_context, recent_history, npc_context,
            items_context=items_context,
            regions_context=regions_context,
            relationships_context=relationships_context,
        )
        user_msg = build_user_prompt(user_input)
        full_user = f"{writer_context}\n\n{user_msg}" if writer_context else user_msg

        return round_num, full_user

    def _finish_extraction(self, round_num: int, user_input: str, narrative: str) -> str:
        """完成抽取→更新→格式化，返回聊天输出。"""
        print("[抽取] 正在从正文提取结构化信息...", file=sys.stderr)
        extraction = self.extractor.extract(narrative)

        print("[更新] 正在更新信息卡...", file=sys.stderr)
        changes = self.card_manager.apply(extraction)

        updated_state = self.repo.get_story_state()
        self.repo.add_session(
            round_number=round_num,
            user_input=user_input,
            narrative=narrative,
            state=updated_state.model_dump(),
        )

        print(f"[完成] 变更: {json.dumps(changes, ensure_ascii=False, indent=2)}", file=sys.stderr)

        return self.formatter.format_chat_output(
            narrative=narrative,
            story_state=updated_state,
        )

    def run_round(self, user_input: str) -> str:
        """执行一轮完整流程：写 → 提取 → 更新 → 输出。"""
        round_num, full_user = self._prepare_context(user_input)

        # ─── 阶段 2：生成叙事 ──────────────────────────────────────────────
        print(f"\n[第 {round_num} 轮] 正在生成叙事...", file=sys.stderr)
        narrative = self.llm.chat(
            system_prompt=self.writer_system_prompt,
            user_prompt=full_user,
            temperature=0.85,
            max_tokens=4096,
        )

        if narrative.startswith("[未配置 API Key"):
            return narrative

        return self._finish_extraction(round_num, user_input, narrative)

    def run_round_stream(self, user_input: str):
        """流式执行一轮：yield SSE 事件字符串。"""
        round_num, full_user = self._prepare_context(user_input)

        print(f"\n[第 {round_num} 轮] 正在生成叙事（流式）...", file=sys.stderr)

        # ─── 阶段 2：流式生成叙事 ──────────────────────────────────────────
        full_text = ""
        for kind, chunk in self.llm.chat_stream(
            system_prompt=self.writer_system_prompt,
            user_prompt=full_user,
            temperature=0.85,
            max_tokens=16384,
        ):
            if kind == "thinking":
                yield f"data: {json.dumps({'type': 'thinking', 'content': chunk}, ensure_ascii=False)}\n\n"
            else:
                full_text += chunk
                yield f"data: {json.dumps({'type': 'chunk', 'content': chunk}, ensure_ascii=False)}\n\n"

        if full_text.startswith("[未配置 API Key"):
            yield f"data: {json.dumps({'type': 'done', 'output': full_text}, ensure_ascii=False)}\n\n"
            return

        # ─── 阶段 3-5：抽取 → 更新 → 格式化 ───────────────────────────────
        output = self._finish_extraction(round_num, user_input, full_text)
        yield f"data: {json.dumps({'type': 'done', 'output': output}, ensure_ascii=False)}\n\n"

    def show_state(self) -> str:
        """显示当前完整状态（不含叙事生成）。"""
        story_state = self.repo.get_story_state()
        player = self.repo.get_player()
        memory_state = self.memory.get_memory_state()
        return self.formatter.format_state_output(
            story_state=story_state,
            player=player,
            memory_state=memory_state,
        )

    def set_api_key(self, key: str):
        """设置 API Key。"""
        self.llm.api_key = key

    def close(self):
        self.repo.close()


def main():
    """CLI 入口。"""
    import argparse

    parser = argparse.ArgumentParser(description="Book 叙事引擎")
    parser.add_argument("action", nargs="?", default="interactive",
                        choices=["run", "state", "init", "interactive"],
                        help="操作：run=执行一轮, state=查看状态, init=初始化数据库, interactive=交互模式")
    parser.add_argument("--input", "-i", help="玩家行动输入")
    parser.add_argument("--output", "-o", help="输出文件路径（默认打印到 stdout）")

    args = parser.parse_args()

    engine = BookEngine()

    # API Key: 优先环境变量，交互模式下才提示输入
    if not engine.llm.api_key and args.action in ("run", "interactive"):
        import os
        key = os.environ.get("DEEPSEEK_API_KEY", "")
        if not key and sys.stdin.isatty():
            from getpass import getpass
            key = getpass("未检测到 DEEPSEEK_API_KEY。请粘贴 API Key: ").strip()
        if key:
            engine.set_api_key(key)

    try:
        if args.action == "init":
            print("数据库已初始化。")
            engine.show_state()
            print("\n初始化完成。")

        elif args.action == "state":
            output = engine.show_state()
            print(output)

        elif args.action == "run":
            if not args.input:
                print("错误：--input 参数不能为空", file=sys.stderr)
                sys.exit(1)
            output = engine.run_round(args.input)
            if args.output:
                Path(args.output).write_text(output, encoding="utf-8")
                print(f"输出已保存到 {args.output}")
            else:
                print(output)

        elif args.action == "interactive":
            print("=" * 60)
            print("  Book 叙事引擎 - 交互模式")
            print("  输入玩家行动，输入 'state' 查看状态，输入 'quit' 退出")
            print("=" * 60)
            while True:
                try:
                    user_input = input("\n>>> ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\n再见。")
                    break

                if not user_input:
                    continue
                if user_input.lower() in ("quit", "exit", "q"):
                    break
                if user_input.lower() == "state":
                    print(engine.show_state())
                    continue

                output = engine.run_round(user_input)
                print("\n" + output)

    finally:
        engine.close()


if __name__ == "__main__":
    main()
