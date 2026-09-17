"""多书隔离——每本书一个数据库文件 + 一份注册表。

对应《开发计划.md》P3-3。

约定：
- 每本书一个库：`data/books/<slug>.db`（与 DSH 侧 `story/<书名>/` 目录约定对齐）
- 注册表：`data/books.json`（slug、书名、创建时间、db 路径）
- 迁移：把旧的 `data/book.db` 复制成 `books/default.db`（**保留原文件**作为备份）
- **默认书就是 `default`**：不传 `--book` 与传 `--book default` 解析到同一个库。
  首次会先把旧的 `data/book.db` 迁移过去，避免老数据"看起来消失"。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from .config import config

# slug 只允许小写字母、数字、连字符、下划线、汉字。
# - 不允许 / \ 与 .，从根上堵住路径穿越（"../evil" 直接被拒）
# - **不允许大写**：macOS/Windows 的文件系统大小写不敏感，Foo 与 foo 会落到
#   同一个 inode，多书隔离会被击穿，所以统一先 lower() 再校验
_SLUG_RE = re.compile(r"^[a-z0-9_\-\u4e00-\u9fff]{1,64}$")

DEFAULT_SLUG = "default"

# 注册表读-改-写必须是原子的：books add 会被并发调用（CLI 多进程），
# 没有锁会互相覆盖，丢注册表条目。
_REGISTRY_LOCK = threading.Lock()
_LOCK_SUFFIX = ".lock"


class BookError(Exception):
    """书注册表相关的错误（非法 slug、找不到书等）。"""


def books_dir() -> Path:
    return config.db_path.parent / "books"


def registry_path() -> Path:
    return config.db_path.parent / "books.json"


def normalize_slug(slug: str) -> str:
    """归一化并校验 slug（统一小写，避免大小写不敏感文件系统上的串库）。"""
    slug = (slug or "").strip().lower()
    if not _SLUG_RE.match(slug):
        raise BookError(
            f"非法书名标识 {slug!r}：只允许小写字母、数字、连字符、下划线或汉字，长度 1-64"
        )
    return slug


# 兼容旧名字
def validate_slug(slug: str) -> str:
    return normalize_slug(slug)


@contextmanager
def _file_lock(path: Path):
    """跨进程文件锁（POSIX flock；不可用时退化为进程内锁）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _REGISTRY_LOCK:
        handle = None
        try:
            handle = open(path, "a+")
            try:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            except (ImportError, OSError):
                pass  # 非 POSIX 平台：至少还有进程内锁
            yield
        finally:
            if handle is not None:
                try:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except (ImportError, OSError):
                    pass
                handle.close()


def load_registry() -> dict:
    path = registry_path()
    if not path.exists():
        return {"books": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        # 不要把损坏静默当成"没有书"——那会让调用方以为数据没丢
        print(f"[books] 警告：注册表无法解析（{exc}），本次按空处理：{path}",
              file=__import__("sys").stderr)
        return {"books": {}}
    if not isinstance(data, dict) or not isinstance(data.get("books"), dict):
        print(f"[books] 警告：注册表结构异常，本次按空处理：{path}",
              file=__import__("sys").stderr)
        return {"books": {}}
    return data


def save_registry(data: dict) -> None:
    """原子写：先写临时文件再 replace，避免读到半截 JSON。"""
    path = registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def db_path_for(slug: str) -> Path:
    return books_dir() / f"{normalize_slug(slug)}.db"


def list_books() -> list[dict]:
    data = load_registry()
    out = []
    for slug, meta in sorted(data["books"].items()):
        entry = dict(meta or {})
        entry["slug"] = slug
        entry.setdefault("title", slug)
        # 路径按当前数据目录重新推导，避免搬到新 BOOK_HOME 后 exists 失真
        expected = db_path_for(slug)
        entry["db_path"] = str(expected)
        entry["exists"] = expected.exists()
        out.append(entry)
    return out


def _checkpoint(path: Path) -> None:
    """把 WAL 内容落盘，否则 copy 出来的 .db 可能是空的（D5）。"""
    if not path.exists():
        return
    try:
        conn = sqlite3.connect(str(path))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass


def _copy_db(src: Path, dst: Path) -> None:
    """复制数据库（先 checkpoint，再连同 -wal/-shm 一起复制）。"""
    _checkpoint(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        side = Path(str(src) + suffix)
        if side.exists():
            shutil.copy2(side, Path(str(dst) + suffix))


def _is_empty_db(path: Path) -> bool:
    """文件存在但没有任何表 → 视为空库（可以安全地重新迁移覆盖）。"""
    if not path.exists():
        return True
    if path.stat().st_size == 0:
        return True
    try:
        conn = sqlite3.connect(str(path))
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            ).fetchone()
            return not row or row[0] == 0
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _migrate_legacy(slug: str, target: Path) -> bool:
    """把旧的 config.db_path 迁移成 target；返回是否真的复制了。"""
    legacy = config.db_path
    if slug != DEFAULT_SLUG or not legacy.exists() or legacy == target:
        return False
    if target.exists() and not _is_empty_db(target):
        return False
    _copy_db(legacy, target)
    return True


def resolve(slug: str | None) -> Path:
    """把 slug 解析成数据库路径。

    - slug 为空 → 视作 `default`（**默认书就是 default**，与计划一致）
    - 首次解析 default 时，若 `books/default.db` 不存在或为空，而旧的
      `data/book.db` 存在 → 复制过去（原文件保留作备份）
    """
    slug = normalize_slug(slug or DEFAULT_SLUG)
    books_dir().mkdir(parents=True, exist_ok=True)
    target = db_path_for(slug)

    migrated = _migrate_legacy(slug, target)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch()

    with _file_lock(registry_path().with_suffix(".lock")):
        data = load_registry()
        if slug not in data["books"]:
            data["books"][slug] = {
                "title": slug,
                "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "db_path": str(target),
            }
            save_registry(data)
        elif migrated:
            data["books"][slug]["migrated_from"] = str(config.db_path)
            save_registry(data)

    return target


def register(slug: str, title: str = "") -> dict:
    slug = normalize_slug(slug)
    path = resolve(slug)
    with _file_lock(registry_path().with_suffix(".lock")):
        data = load_registry()
        entry = data["books"].setdefault(slug, {})
        entry["title"] = title or entry.get("title") or slug
        entry["db_path"] = str(path)
        entry.setdefault("created_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
        save_registry(data)
    return {"slug": slug, "title": entry["title"], "db_path": str(path)}
