"""bookctl 命令行入口——给 agent 与人类共用的接口。

契约见《开发计划.md》§5（子命令名、参数名、JSON 字段名不可改）：
所有子命令支持 --json，输出单行 JSON；非 --json 时用 rich 友好输出。
退出码：成功 0；参数错误 2；内部错误 1。JSON 模式下错误也输出 JSON。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

from . import books
from .config import config
from .db import search as search_mod
from .db.repository import Repository
from .memory.manager import MemoryManager
from .executor.extractor import Extractor
from .executor.card_manager import CardManager
from .executor import consistency
from .writer.context_builder import build_context
from .writer.generator import LLMClient

EXIT_OK = 0
EXIT_INTERNAL = 1
EXIT_USAGE = 2

# ingest 后需要重建索引的来源
_INGEST_SOURCES = ["npc", "item", "region", "relationship", "intel", "perm_memory"]

# 允许登记的实体类型：必须与 search_docs.source / 提及匹配用的类型一致
_ALIAS_ENTITY_TYPES = {"player", "npc", "item", "region"}


class CliError(Exception):
    """带类型的 CLI 错误，用于统一输出 JSON 错误对象。"""

    def __init__(self, message: str, kind: str = "internal", code: int = EXIT_INTERNAL):
        super().__init__(message)
        self.kind = kind
        self.code = code


# ─── 输出层 ────────────────────────────────────────────────────────────────


def emit(payload: dict, as_json: bool, renderer=None) -> None:
    """按 --json 与否输出结果。JSON 必须单行。"""
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    elif renderer is not None:
        renderer(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def emit_error(message: str, kind: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": message, "kind": kind}, ensure_ascii=False))
    else:
        print(f"错误[{kind}]：{message}", file=sys.stderr)
    return EXIT_INTERNAL if kind == "internal" else EXIT_USAGE


# ─── 共享工具 ──────────────────────────────────────────────────────────────


def resolve_db(args) -> Path:
    """按 --book / --db 解析出数据库路径。

    --book 优先（多书隔离）：走 books 注册表得到 data/books/<slug>.db；
    否则用 --db（默认 config.db_path），保持 CLI 的既有行为。
    """
    book = getattr(args, "book", None)
    if book is not None:
        # --book "" 是误用：明确报错，不要静默写进共享主库
        if not str(book).strip():
            raise CliError("--book 不能为空（要默认书请用 --book default 或省略）",
                           kind="usage", code=EXIT_USAGE)
        try:
            return books.resolve(book)
        except books.BookError as exc:
            raise CliError(str(exc), kind="usage", code=EXIT_USAGE)
    # 不传 --book 时仍走 config.db_path（既有行为），
    # 但默认书 default 会由 books.resolve 走 books/default.db；
    # 为免"老数据看起来消失"，这里保持旧路径不变。
    return Path(getattr(args, "db", None) or config.db_path)


def open_repo(args) -> Repository:
    """打开数据库并确保结构/迁移到位（--no-migrate 时只建表不迁移）。"""
    repo = Repository(resolve_db(args))
    repo.init_db(migrate=not getattr(args, "no_migrate", False))
    return repo


def sha256_of_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_of_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_of_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_input_text(args) -> tuple[str, str]:
    """返回 (正文, content_hash)。--text 与 --file 二选一。"""
    if args.text is not None and args.file is not None:
        raise CliError("--file 与 --text 不能同时使用", kind="usage", code=EXIT_USAGE)
    if args.text is not None:
        raw = args.text.encode("utf-8")
        return _require_text(args.text, "--text"), sha256_of_bytes(raw)
    if args.file is not None:
        path = Path(args.file)
        if not path.is_file():
            raise CliError(f"文件不存在: {path}", kind="usage", code=EXIT_USAGE)
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise CliError(f"文件不是 UTF-8 编码: {path}", kind="usage", code=EXIT_USAGE)
        return _require_text(text, str(path)), sha256_of_bytes(raw)
    raise CliError("必须提供 --file 或 --text", kind="usage", code=EXIT_USAGE)


def _require_text(text: str, source: str) -> str:
    """拒绝空/纯空白正文。

    空正文不能算"入库成功"：它会插一行长度 0 的历史、占掉 context 的"最近一轮"
    名额，而且空串 sha256 是固定值（e3b0c442…），下一次空文件会被幂等判断
    报成 "already ingested"——把失败伪装成成功。这里直接按参数错误拒绝。
    """
    if not text.strip():
        raise CliError(
            f"正文为空（仅空白字符），拒绝入库: {source}",
            kind="usage",
            code=EXIT_USAGE,
        )
    return text


def _flatten_changes(changes: dict) -> dict:
    """把 CardManager.apply() 的嵌套结果归一为 §5 契约的扁平形状。"""
    flat = {k: v for k, v in changes.items() if k != "player"}
    if "npcs" in changes:
        names = changes["npcs"] or []
        flat["npcs_updated"] = len(names)
        flat["npc_names"] = names
    if "items" in changes:
        names = changes["items"] or []
        flat["items_added"] = len(names)
        flat["item_names"] = names
    if "regions" in changes:
        flat["regions_added"] = len(changes["regions"] or [])
    if "relationships" in changes:
        flat["relationships_updated"] = len(changes["relationships"] or [])
    if "important_intel" in changes:
        flat["intel_added"] = len(changes["important_intel"] or [])
    if "memory" in changes:
        mem = changes["memory"] or {}
        flat["memories"] = "integrated" if mem.get("triggered_integration") else "added"
    player = changes.get("player") or {}
    if player:
        if "currency" in player:
            flat["currency"] = player["currency"]
        if "hp" in player:
            flat["hp"] = player["hp"]
        if "en" in player:
            flat["en"] = player["en"]
        if "level_up" in player:
            flat["level_up"] = player["level_up"]
    return flat


def run_extraction(repo: Repository, narrative: str) -> dict:
    """对正文做一轮抽取并落库。

    返回 {"changes": dict, "warnings": list[str], "status": str}。
    status: ok（抽取并应用成功）| no_key | extract_failed | empty | apply_failed。

    抽取/应用失败都不抛异常——正文不能丢；但必须如实告知调用方，
    避免 changes={} 被误读成"这一轮什么都没变"。
    """
    warnings: list[str] = []
    llm = LLMClient()
    extractor = Extractor(llm)
    memory = MemoryManager(repo, llm_call=lambda p: llm.complete(p, temperature=0.3))
    card_manager = CardManager(repo, memory)

    def result(status: str, changes: dict | None = None) -> dict:
        return {"changes": changes or {}, "warnings": warnings, "status": status}

    if not llm.api_key:
        warnings.append("未配置 DEEPSEEK_API_KEY，已跳过结构化抽取（仅入库正文与索引）")
        return result("no_key")

    try:
        extraction = extractor.extract(narrative)
    except Exception as exc:  # noqa: BLE001 - 抽取失败不能丢正文
        warnings.append(f"抽取失败（{type(exc).__name__}）：仅入库正文与索引")
        return result("extract_failed")

    # 抽取器内部失败时会静默返回空结构，必须查 last_error 才不会把失败报成"零变更"
    if extractor.last_error:
        warnings.append(f"抽取失败（{extractor.last_error}）：仅入库正文与索引")
        return result("extract_failed")

    if not extraction:
        warnings.append("抽取结果为空：仅入库正文与索引")
        return result("empty")

    try:
        # 事务包裹：apply 会连续改多张表，中途失败若留下半截写入，
        # 业务表与检索索引就会不一致，且同内容重跑会被 content_hash 判为 skipped
        with repo.transaction():
            changes = card_manager.apply(extraction)
    except Exception as exc:  # noqa: BLE001
        warnings.append(
            f"应用抽取结果失败（{type(exc).__name__}：{exc}）；"
            "该轮结构化变更已整体回滚，仅入库正文与索引"
        )
        return result("apply_failed")
    return result("ok", changes)


# ─── 子命令实现 ────────────────────────────────────────────────────────────


def cmd_search(args) -> dict:
    repo = open_repo(args)
    try:
        hits = search_mod.search(
            repo.conn, args.query, k=args.k, source=args.source, volume=args.volume,
            semantic=args.semantic, min_score=args.min_score,
        )
        return {
            "query": args.query,
            "mode": "semantic" if args.semantic else "fts",
            "hits": [h.to_dict() for h in hits],
        }
    finally:
        repo.close()


def cmd_context(args) -> dict:
    repo = open_repo(args)
    try:
        pack = build_context(
            repo,
            pov=args.pov or "",
            scene=args.scene or "",
            chapter=args.chapter,
            budget_chars=args.budget,
        )
        return pack.to_dict()
    finally:
        repo.close()


def cmd_ingest(args) -> dict:
    text, content_hash = read_input_text(args)
    repo = open_repo(args)
    try:
        existing = repo.get_session_by_hash(content_hash)
        if existing:
            return {
                "status": "skipped",
                "reason": "already ingested",
                "round": existing["round_number"],
                "content_hash": content_hash,
                "changes": {},
                "indexed": {"narrative": 0, "entities": 0},
                "warnings": [],
            }

        if args.no_extract:
            extracted = {
                "changes": {},
                "warnings": ["--no-extract：已跳过结构化抽取（仅入库正文与索引）"],
                "status": "no_extract",
            }
        else:
            extracted = run_extraction(repo, text)
        changes = extracted["changes"]
        warnings = extracted["warnings"]

        # 轮次：显式 --round > --chapter > 库内下一轮
        round_number = args.round or args.chapter or (repo.get_max_round() + 1)

        state = repo.get_story_state()
        session_id = repo.add_session(
            round_number=round_number,
            user_input=args.input or "",
            narrative=text,
            state=state.model_dump(),
            volume=args.volume,
            chapter=args.chapter,
            pov=args.pov,
            content_hash=content_hash,
        )
        # 抽取成功（或无 Key/失败）都在此判定；只有真正 ok 才算"已抽取"，
        # 这样后续 extract 不会把增量再应用一遍
        if extracted["status"] == "ok":
            repo.mark_session_extracted(session_id)

        # ref_id 统一用 session_history.id（来源行主键），与 rebuild_all 口径一致；
        # 用 round_number 当 ref_id 会让同一轮的两行互相覆盖索引，reindex 后又变回行主键
        search_mod.index_session(
            repo.conn,
            session_id=session_id,
            narrative=text,
            volume=args.volume,
            chapter=args.chapter,
            pov=args.pov,
        )

        # 实体变化后重建对应来源的索引
        indexed_entities = 0
        if changes:
            search_mod.rebuild_all(repo.conn)
            rows = repo.conn.execute(
                "SELECT COUNT(*) AS c FROM search_docs WHERE source IN ("
                + ",".join("?" * len(_INGEST_SOURCES))
                + ")",
                _INGEST_SOURCES,
            ).fetchone()
            indexed_entities = int(rows["c"])
        elif extracted["status"] == "ok":
            # 抽取正常但确实没有结构化变更时才额外提示（避免与抽取失败混淆）
            warnings.append("未产生结构化变更，仅索引了正文")

        return {
            "status": "ingested",
            "round": round_number,
            "content_hash": content_hash,
            "changes": _flatten_changes(changes),
            "indexed": {"narrative": 1, "entities": indexed_entities},
            "warnings": warnings,
        }
    finally:
        repo.close()


def cmd_chapter(args) -> dict:
    repo = open_repo(args)
    try:
        if args.action == "list":
            rows = repo.list_chapters(volume_idx=args.volume)
            return {
                "chapters": [
                    {
                        "volume": r["volume_idx"],
                        "chapter": r["idx"],
                        "title": r["title"],
                        "pov": r["pov"],
                        "status": r["status"],
                        "word_count": r["word_count"],
                    }
                    for r in rows
                ]
            }
        # add：先校验参数，避免"先建了卷再失败"留下副作用
        if args.chapter is None:
            raise CliError("chapter add 必须提供 --chapter", kind="usage", code=EXIT_USAGE)
        if args.volume is None or args.volume < 1:
            raise CliError("--volume 必须是 >= 1 的整数", kind="usage", code=EXIT_USAGE)
        if repo.get_volume(args.volume) is None:
            repo.upsert_volume(args.volume, title=f"第{args.volume}卷")
        chapter_id = repo.upsert_chapter(
            idx=args.chapter,
            volume_idx=args.volume,
            title=args.title or "",
            pov=args.pov or "",
            goal=args.goal or "",
            status=args.status,
            word_count=args.word_count,
            file_path=args.file_path or "",
            summary=args.summary or "",
        )
        return {
            "status": "ok",
            "volume": args.volume,
            "chapter": args.chapter,
            "id": chapter_id,
        }
    finally:
        repo.close()


def cmd_foreshadow(args) -> dict:
    repo = open_repo(args)
    try:
        if args.action == "list":
            rows = repo.list_foreshadows(status=args.status)
            return {
                "foreshadows": [
                    {
                        "id": r["id"],
                        "name": r["name"],
                        "detail": r["detail"],
                        "planted_volume": r["planted_volume"],
                        "planted_chapter": r["planted_chapter"],
                        "status": r["status"],
                        "planned_recovery": r["planned_recovery"],
                        "recovered_chapter": r["recovered_chapter"],
                    }
                    for r in rows
                ]
            }
        if args.action == "add":
            if not (args.name or "").strip():
                raise CliError("--name 不能为空", kind="usage", code=EXIT_USAGE)
            fid = repo.add_foreshadow(
                name=args.name.strip(),
                detail=args.detail or "",
                planted_volume=args.volume,
                planted_chapter=args.chapter,
                planned_recovery=args.planned_recovery or "",
            )
            return {"status": "ok", "id": fid, "name": args.name.strip(), "state": "未收"}
        # close
        if not (args.name or "").strip():
            raise CliError("--name 不能为空", kind="usage", code=EXIT_USAGE)
        changed = repo.close_foreshadow(args.name.strip(), recovered_chapter=args.chapter)
        if changed == 0:
            # 幂等：已收或不存在都不算内部错误，但必须如实告知
            return {
                "status": "skipped",
                "reason": "该伏笔不存在或已收线",
                "name": args.name.strip(),
            }
        return {"status": "ok", "name": args.name.strip(), "state": "已收", "changed": changed}
    finally:
        repo.close()


def cmd_alias(args) -> dict:
    repo = open_repo(args)
    try:
        if args.action == "list":
            return {"aliases": repo.list_aliases()}
        if not (args.alias or "").strip():
            raise CliError("--alias 不能为空", kind="usage", code=EXIT_USAGE)
        if not (args.entity_id or "").strip():
            raise CliError("--entity-id 不能为空", kind="usage", code=EXIT_USAGE)
        if args.entity_type not in _ALIAS_ENTITY_TYPES:
            raise CliError(
                f"--entity-type 必须是 {'|'.join(sorted(_ALIAS_ENTITY_TYPES))} 之一，"
                f"收到 {args.entity_type}",
                kind="usage",
                code=EXIT_USAGE,
            )
        repo.add_alias(args.entity_type, args.entity_id.strip(), args.alias.strip())
        return {
            "status": "ok",
            "entity_type": args.entity_type,
            "entity_id": args.entity_id.strip(),
            "alias": args.alias.strip(),
        }
    finally:
        repo.close()


def cmd_check(args) -> dict:
    range_start, range_end = _parse_range(args.range)
    if args.foreshadow_stale < 0:
        raise CliError("--foreshadow-stale 不能为负数", kind="usage", code=EXIT_USAGE)
    repo = open_repo(args)
    try:
        issues = consistency.run_checks(
            repo,
            range_start=range_start,
            range_end=range_end,
            stale_after=args.foreshadow_stale,
        )
        if args.deep:
            # 深度模式需要 LLM；没有 Key 时纯规则结果照常返回，不报错
            llm = LLMClient()
            if llm.api_key:
                issues = consistency.deep_check(
                    repo, issues,
                    llm_call=lambda p: llm.complete(p, temperature=0.1, max_tokens=1500),
                )
            else:
                issues = list(issues)
                issues.append(consistency.Issue(
                    kind="ability", severity="low",
                    detail="未配置 DEEPSEEK_API_KEY，已跳过 --deep 的模型深度检查（纯规则结果仍有效）",
                    refs=[],
                ))
        return {"range": [range_start, range_end], "issues": [i.to_dict() for i in issues]}
    finally:
        repo.close()


def cmd_extract(args) -> dict:
    """批量补抽取（P2-3）。

    默认只处理**从未抽取过**的轮次（extracted_at IS NULL）——抽取结果是
    "相对增量"（好感 +N、经验 +N、记忆 +1 条），同一轮抽两次会把增量应用两遍，
    造成不可逆的状态漂移。--force 才会重抽已抽取过的轮次。
    """
    range_start, range_end = _parse_range(args.range)
    repo = open_repo(args)
    try:
        if args.force:
            candidates = repo.get_session_rounds(limit=0)
        else:
            candidates = repo.get_unextracted_sessions()

        sessions = [
            row for row in candidates
            if (range_start is None or row["round_number"] >= range_start)
            and (range_end is None or row["round_number"] <= range_end)
        ]
        skipped = len([r for r in repo.get_session_rounds(limit=0)
                       if r not in candidates
                       and (range_start is None or r["round_number"] >= range_start)
                       and (range_end is None or r["round_number"] <= range_end)])

        warnings: list[str] = []
        if skipped:
            warnings.append(
                f"跳过 {skipped} 个已抽取过的轮次（避免增量重复应用）；"
                "确实要重抽请加 --force"
            )
        if not sessions:
            return {"status": "ok", "extracted": 0, "rounds": [], "skipped": skipped,
                    "warnings": warnings + ["区间内没有待抽取的轮次"]}

        llm = LLMClient()
        if not llm.api_key:
            raise CliError(
                "未配置 DEEPSEEK_API_KEY，无法批量抽取",
                kind="usage", code=EXIT_USAGE,
            )

        done: list[int] = []
        for row in sessions:
            narrative = row.get("narrative") or ""
            if not narrative.strip():
                continue
            result = run_extraction(repo, narrative)
            warnings.extend(result["warnings"])
            # 只有真正抽取成功才打标记；失败/跳过的留待下次重试
            if result["status"] == "ok":
                repo.mark_session_extracted(row["id"])
                if result["changes"]:
                    done.append(row["round_number"])

        if done:
            search_mod.rebuild_all(repo.conn)
        return {
            "status": "ok",
            "extracted": len(done),
            "rounds": done,
            "skipped": skipped,
            "warnings": warnings,
        }
    finally:
        repo.close()


def cmd_books(args) -> dict:
    """列出/新建书。"""
    if args.action == "add":
        if not (args.slug or "").strip():
            raise CliError("books add 必须提供 slug", kind="usage", code=EXIT_USAGE)
        try:
            entry = books.register(args.slug.strip(), title=args.title or "")
        except books.BookError as exc:
            raise CliError(str(exc), kind="usage", code=EXIT_USAGE)
        # 新建的库要初始化结构
        repo = Repository(Path(entry["db_path"]))
        repo.init_db(migrate=not args.no_migrate)
        repo.close()
        return {"status": "ok", **entry}

    return {"books": books.list_books()}


def cmd_reindex(args) -> dict:
    repo = open_repo(args)
    try:
        counts = search_mod.rebuild_all(repo.conn)
        return {
            "status": "reindexed",
            "sources": counts,
            "total": sum(counts.values()),
        }
    finally:
        repo.close()


def _parse_range(raw: str | None) -> tuple[int | None, int | None]:
    if not raw:
        return None, None
    if "-" not in raw:
        raise CliError(f"--range 需要 A-B 形式，收到: {raw}", kind="usage", code=EXIT_USAGE)
    left, _, right = raw.partition("-")
    try:
        start, end = int(left), int(right)
    except ValueError:
        raise CliError(f"--range 需要整数区间，收到: {raw}", kind="usage", code=EXIT_USAGE)
    if start > end:
        raise CliError(f"--range 起点不能大于终点，收到: {raw}", kind="usage", code=EXIT_USAGE)
    return start, end


# ─── 非 JSON 的友好输出 ─────────────────────────────────────────────────────


def render_search(payload: dict) -> None:
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    console = Console()
    hits = payload.get("hits", [])
    if not hits:
        console.print("[yellow]无命中[/yellow]")
        return
    # 查询词来自用户，必须转义后再交给 rich，否则含 [/bold] 之类会抛 MarkupError
    title = f"检索：{payload.get('query')}（{payload.get('mode')}）"
    table = Table(title=escape(title))
    table.add_column("id", justify="right")
    table.add_column("source")
    table.add_column("章", justify="right")
    table.add_column("score", justify="right")
    table.add_column("snippet", overflow="fold")
    for hit in hits:
        chapter = hit.get("chapter")
        table.add_row(
            str(hit.get("id")),
            str(hit.get("source")),
            "-" if chapter is None else str(chapter),
            f"{hit.get('score', 0):.4f}",
            escape((hit.get("snippet") or "")[:80]),
        )
    console.print(table)


def render_context(payload: dict) -> None:
    from rich.console import Console
    from rich.markup import escape

    console = Console()
    console.print(
        f"POV=[bold]{escape(str(payload.get('pov')))}[/bold] "
        f"场景=[bold]{escape(str(payload.get('scene')))}[/bold] "
        f"章={payload.get('chapter')} 预算内 {payload.get('prompt_chars')} 字"
    )
    if payload.get("chapter_title") or payload.get("chapter_goal"):
        console.print(
            f"  本章：{escape(str(payload.get('chapter_title') or ''))}"
            + (f" | 目标：{escape(str(payload['chapter_goal']))}" if payload.get("chapter_goal") else "")
        )
    if payload.get("chapter_scenes"):
        console.print(f"  本章场景：{escape('、'.join(payload['chapter_scenes']))}")
    for ent in payload.get("entities", []):
        console.print(f"  · [{ent.get('type')}] {escape(str(ent.get('name')))}")
    if payload.get("foreshadows"):
        console.print(
            "  未收伏笔："
            + escape("、".join(str(f["name"]) for f in payload["foreshadows"]))
        )
    if payload.get("present_npcs"):
        console.print(f"  在场：{escape('、'.join(payload['present_npcs']))}")
    trimmed = payload.get("trimmed") or []
    if trimmed:
        console.print(f"  [dim]已裁剪：{escape('、'.join(trimmed))}[/dim]")


def render_ingest(payload: dict) -> None:
    from rich.console import Console
    from rich.markup import escape

    console = Console()
    console.print(
        f"[bold]{escape(str(payload.get('status')))}[/bold] round={payload.get('round')} "
        f"hash={(payload.get('content_hash') or '')[:12]}…"
    )
    for line in payload.get("warnings", []):
        console.print(f"  [yellow]! {escape(str(line))}[/yellow]")


def render_chapter(payload: dict) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    rows = payload.get("chapters")
    if rows is None:
        console.print(
            f"[green]{payload.get('status')}[/green] "
            f"卷{payload.get('volume')} 第{payload.get('chapter')}章"
        )
        return
    if not rows:
        console.print("[yellow]暂无章节[/yellow]")
        return
    table = Table(title="章节")
    for col in ("卷", "章", "标题", "POV", "状态", "字数"):
        table.add_column(col)
    for c in rows:
        table.add_row(str(c["volume"]), str(c["chapter"]), c["title"] or "-",
                      c["pov"] or "-", c["status"], str(c["word_count"]))
    console.print(table)


def render_foreshadow(payload: dict) -> None:
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    console = Console()
    rows = payload.get("foreshadows")
    if rows is None:
        console.print(f"[green]{payload.get('status')}[/green] {escape(str(payload.get('name', '')))}")
        return
    if not rows:
        console.print("[yellow]暂无伏笔[/yellow]")
        return
    table = Table(title="伏笔")
    for col in ("id", "名称", "埋设章", "状态", "计划回收"):
        table.add_column(col)
    for r in rows:
        table.add_row(str(r["id"]), escape(r["name"]),
                      "-" if r["planted_chapter"] is None else str(r["planted_chapter"]),
                      r["status"], escape(r["planned_recovery"] or "-"))
    console.print(table)


def render_books(payload: dict) -> None:
    from rich.console import Console
    from rich.table import Table

    console = Console()
    rows = payload.get("books")
    if rows is None:
        console.print(f"[green]{payload.get('status')}[/green] {payload.get('slug')} → {payload.get('db_path')}")
        return
    if not rows:
        console.print("[yellow]还没有任何书，用 bookctl books add <slug> 新建[/yellow]")
        return
    table = Table(title="书列表")
    for col in ("slug", "书名", "库文件", "存在"):
        table.add_column(col)
    for b in rows:
        table.add_row(b["slug"], b.get("title", ""), b.get("db_path", ""),
                      "是" if b.get("exists") else "否")
    console.print(table)


def render_check(payload: dict) -> None:
    from rich.console import Console
    from rich.markup import escape
    from rich.table import Table

    console = Console()
    issues = payload.get("issues", [])
    rng = payload.get("range") or [None, None]
    scope = "全部轮次" if rng[0] is None else f"轮次 {rng[0]}-{rng[1]}"
    if not issues:
        console.print(f"[green]一致性检查通过[/green]（{scope}）：未发现问题")
        return
    console.print(f"[yellow]发现 {len(issues)} 个问题[/yellow]（{scope}）")
    table = Table()
    for col in ("kind", "级别", "详情"):
        table.add_column(col, overflow="fold")
    colors = {"high": "red", "mid": "yellow", "low": "dim"}
    for i in issues:
        sev = str(i.get("severity"))
        table.add_row(
            str(i.get("kind")),
            f"[{colors.get(sev, 'white')}]{sev}[/{colors.get(sev, 'white')}]",
            escape(str(i.get("detail"))),
        )
    console.print(table)


def render_generic(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


# ─── 参数解析 ──────────────────────────────────────────────────────────────


def _default_db() -> Path:
    return Path(os.environ.get("BOOK_DB") or config.db_path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bookctl", description="BookWorld 叙事状态服务 CLI")
    parser.add_argument("--db", type=Path, default=_default_db(),
                        help="数据库路径（默认 data/book.db，可用环境变量 BOOK_DB 覆盖）")
    parser.add_argument("--book", default=None,
                        help="书名标识（多书隔离）：用 data/books/<slug>.db，优先于 --db")
    parser.add_argument("--no-migrate", action="store_true", dest="no_migrate",
                        help="只建缺失的表，不执行列级迁移（只读命令不想改库结构时用）")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("search", help="检索历史原文")
    p.add_argument("query", help="查询词")
    p.add_argument("--k", type=int, default=5, help="返回条数，默认 5")
    p.add_argument("--source", default=None, help="限定来源：narrative|npc|item|region|…")
    p.add_argument("--volume", type=int, default=None, help="限定卷号")
    p.add_argument("--semantic", action="store_true", help="稀疏向量召回（P2）")
    p.add_argument("--min-score", type=float, default=0.0, dest="min_score",
                   help="语义检索的最低相似度（默认 0.0；调高可过滤弱相关噪声）")
    p.add_argument("--json", action="store_true", help="输出单行 JSON")
    p.set_defaults(func=cmd_search, renderer=render_search)

    p = sub.add_parser("context", help="构建写作用上下文包（受预算约束）")
    p.add_argument("--pov", default="", help="视角角色名")
    p.add_argument("--scene", default="", help="当前场景")
    p.add_argument("--chapter", type=int, default=None, help="章号")
    p.add_argument("--budget", type=int, default=6000, help="正文字符预算，默认 6000")
    p.add_argument("--json", action="store_true", help="输出单行 JSON")
    p.set_defaults(func=cmd_context, renderer=render_context)

    p = sub.add_parser("ingest", help="把一章正文入库并抽取结构化信息（幂等）")
    p.add_argument("--file", default=None, help="正文文件路径")
    p.add_argument("--text", default=None, help="直接传入正文")
    p.add_argument("--volume", type=int, default=None, help="卷号")
    p.add_argument("--chapter", type=int, default=None, help="章号")
    p.add_argument("--round", type=int, default=None, help="轮次号（默认库内下一轮）")
    p.add_argument("--pov", default=None, help="视角角色名")
    p.add_argument("--input", default=None, help="该轮的玩家/作者指令（可选）")
    p.add_argument("--no-extract", action="store_true", dest="no_extract",
                   help="只入库+索引，跳过结构化抽取（之后可用 extract 批量补）")
    p.add_argument("--json", action="store_true", help="输出单行 JSON")
    p.set_defaults(func=cmd_ingest, renderer=render_ingest)

    p = sub.add_parser("chapter", help="卷/章管理")
    p.add_argument("action", choices=["list", "add"], help="list=列出 add=新增或更新")
    p.add_argument("--volume", type=int, default=1, help="卷号（默认 1）")
    p.add_argument("--chapter", type=int, default=None, help="章号（add 必填）")
    p.add_argument("--title", default=None, help="章标题")
    p.add_argument("--pov", default=None, help="本章视角角色")
    p.add_argument("--goal", default=None, help="本章目标")
    p.add_argument("--status", default="草稿", help="状态，默认 草稿")
    p.add_argument("--word-count", type=int, default=0, dest="word_count", help="字数")
    p.add_argument("--file-path", default=None, dest="file_path", help="正文文件路径")
    p.add_argument("--summary", default=None, help="本章摘要")
    p.add_argument("--json", action="store_true", help="输出单行 JSON")
    p.set_defaults(func=cmd_chapter, renderer=render_chapter)

    p = sub.add_parser("foreshadow", help="伏笔管理")
    p.add_argument("action", choices=["add", "list", "close"], help="操作")
    p.add_argument("--name", default=None, help="伏笔名称")
    p.add_argument("--detail", default=None, help="伏笔详情")
    p.add_argument("--volume", type=int, default=None, help="埋设卷号")
    p.add_argument("--chapter", type=int, default=None, help="埋设章号 / 回收章号")
    p.add_argument("--planned-recovery", default=None, dest="planned_recovery",
                   help="计划如何回收")
    p.add_argument("--status", default=None, help="list 时按状态过滤，如 未收")
    p.add_argument("--json", action="store_true", help="输出单行 JSON")
    p.set_defaults(func=cmd_foreshadow, renderer=render_foreshadow)

    p = sub.add_parser("alias", help="实体别名管理（供提及驱动注入）")
    p.add_argument("action", choices=["add", "list"], help="操作")
    p.add_argument("--entity-type", default="npc", dest="entity_type",
                   help="实体类型：npc|player|item|region")
    p.add_argument("--entity-id", default=None, dest="entity_id", help="实体标识（通常是名字）")
    p.add_argument("--alias", default=None, help="别名")
    p.add_argument("--json", action="store_true", help="输出单行 JSON")
    p.set_defaults(func=cmd_alias, renderer=render_generic)

    p = sub.add_parser("check", help="一致性检查（时间线/状态/伏笔）")
    p.add_argument("--range", default=None, help="轮次区间，如 10-20")
    p.add_argument("--deep", action="store_true", help="额外用 LLM 做深度一致性比对")
    p.add_argument("--foreshadow-stale", type=int, default=30, dest="foreshadow_stale",
                   help="伏笔埋下超过多少章仍未回收就提醒，默认 30")
    p.add_argument("--json", action="store_true", help="输出单行 JSON")
    p.set_defaults(func=cmd_check, renderer=render_check)

    p = sub.add_parser("books", help="多书隔离：列出/新建书")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add"])
    p.add_argument("slug", nargs="?", default=None, help="书名标识（add 必填）")
    p.add_argument("--title", default=None, help="书名")
    p.add_argument("--json", action="store_true", help="输出单行 JSON")
    p.set_defaults(func=cmd_books, renderer=render_books)

    p = sub.add_parser("extract", help="批量补抽取（配合 ingest --no-extract 使用）")
    p.add_argument("--range", default=None, help="轮次区间，如 10-20")
    p.add_argument("--force", action="store_true",
                   help="重抽已抽取过的轮次（会把增量再应用一遍，慎用）")
    p.add_argument("--json", action="store_true", help="输出单行 JSON")
    p.set_defaults(func=cmd_extract, renderer=render_generic)

    p = sub.add_parser("reindex", help="全量重建检索索引")
    p.add_argument("--json", action="store_true", help="输出单行 JSON")
    p.set_defaults(func=cmd_reindex, renderer=render_generic)

    return parser


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in argv

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            if as_json:
                print(json.dumps({"error": "参数错误", "kind": "usage"}, ensure_ascii=False))
            return EXIT_USAGE
        return EXIT_OK

    try:
        payload = args.func(args)
        emit(payload, args.json, getattr(args, "renderer", None))
        return EXIT_OK
    except CliError as exc:
        return emit_error(str(exc), exc.kind, as_json)
    except Exception as exc:  # noqa: BLE001 - 统一转成契约里的错误对象
        return emit_error(f"{type(exc).__name__}: {exc}", "internal", as_json)


if __name__ == "__main__":
    sys.exit(main())
