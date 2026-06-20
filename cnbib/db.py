"""SQLite 读写 + FTS5。

表：books（主键 isbn_13）、field_sources（字段来源追踪）、edits（众包日志，Phase 2 用）。
FTS5：books 上的全文索引，索引 title / authors / publisher。
中文无空格分词，用 trigram tokenizer 支持子串匹配；短查询退回 LIKE。
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import Any

from cnbib.aggregator import CROWD_CONFIDENCE, CROWD_SOURCE, FieldSource
from cnbib.sources.base import LIST_FIELDS, SOURCE_FIELDS

# 可众包编辑的字段：除主键/时间戳外的全部
EDITABLE_FIELDS: frozenset[str] = frozenset(SOURCE_FIELDS)
_INT_FIELDS = frozenset({"publish_year", "page_count"})
_LIST_SPLIT = re.compile(r"[，,、;\n]+")

DEFAULT_DB = "cnbib.db"

# books 表的全部列（含主键与时间戳）
_BOOK_COLUMNS = ("isbn_13",) + SOURCE_FIELDS + ("created_at", "updated_at")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    # check_same_thread=False：async 端点在事件循环线程，而 Depends 里的连接
    # 在 worker 线程创建，两者顺序（非并发）使用同一连接，需放开线程校验。
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _fts_tokenizer(conn: sqlite3.Connection) -> str:
    """优先 trigram（支持中文子串搜索），不支持则退 unicode61。"""
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS _fts_probe USING fts5(x, tokenize='trigram')"
        )
        conn.execute("DROP TABLE IF EXISTS _fts_probe")
        return "trigram"
    except sqlite3.OperationalError:
        return "unicode61"


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """给已存在的 books 表补齐 SOURCE_FIELDS 里新增的列（轻量迁移）。"""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(books)")}
    for col in SOURCE_FIELDS:
        if col not in have:
            typ = "INTEGER" if col in ("publish_year", "page_count") else "TEXT"
            conn.execute(f"ALTER TABLE books ADD COLUMN {col} {typ}")


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS books (
            isbn_13        TEXT PRIMARY KEY,
            isbn_10        TEXT,
            title          TEXT,
            subtitle       TEXT,
            authors         TEXT,   -- JSON array（中文译名）
            translators      TEXT,   -- JSON array
            original_title   TEXT,
            original_authors TEXT,   -- JSON array（外国作者原文名）
            publisher        TEXT,
            publish_date   TEXT,
            publish_year   INTEGER,
            description    TEXT,
            cover_url      TEXT,
            page_count     INTEGER,
            language       TEXT,
            series         TEXT,
            subjects       TEXT,   -- JSON array
            clc            TEXT,
            created_at     TEXT,
            updated_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS field_sources (
            isbn_13    TEXT NOT NULL,
            field_name TEXT NOT NULL,
            source     TEXT NOT NULL,
            confidence INTEGER,
            updated_at TEXT,
            PRIMARY KEY (isbn_13, field_name)
        );

        CREATE TABLE IF NOT EXISTS edits (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            isbn_13         TEXT NOT NULL,
            field_name      TEXT NOT NULL,
            old_value       TEXT,
            new_value       TEXT,
            contributor_hint TEXT,
            created_at      TEXT
        );
        """
    )
    # 先补齐可能缺的列（旧库迁移），再建依赖这些列的索引
    _ensure_columns(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_books_year ON books(publish_year)")
    tok = _fts_tokenizer(conn)
    conn.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS books_fts USING fts5(
            isbn_13 UNINDEXED,
            title,
            authors,
            publisher,
            tokenize='{tok}'
        )
        """
    )
    conn.commit()


# ── 写 ────────────────────────────────────────────────────────────

def _dump_list(v: Any) -> str | None:
    if not v:
        return None
    return json.dumps(v, ensure_ascii=False)


def upsert_book(
    conn: sqlite3.Connection,
    isbn_13: str,
    record: dict[str, Any],
    field_sources: list[Any] | None = None,
    commit: bool = True,
) -> None:
    """插入/更新一本书 + 它的字段来源。record 是 SOURCE_FIELDS 的 dict。

    commit=False 时不提交，给批量导入按批 commit（几十万行别每行提交）。
    """
    now = _now()
    existing = conn.execute(
        "SELECT created_at FROM books WHERE isbn_13=?", (isbn_13,)
    ).fetchone()
    created_at = existing["created_at"] if existing else now

    row: dict[str, Any] = {"isbn_13": isbn_13, "created_at": created_at, "updated_at": now}
    for f in SOURCE_FIELDS:
        val = record.get(f)
        row[f] = _dump_list(val) if f in LIST_FIELDS else val

    cols = ", ".join(_BOOK_COLUMNS)
    placeholders = ", ".join(f":{c}" for c in _BOOK_COLUMNS)
    updates = ", ".join(f"{c}=excluded.{c}" for c in _BOOK_COLUMNS if c != "isbn_13")
    conn.execute(
        f"INSERT INTO books ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(isbn_13) DO UPDATE SET {updates}",
        row,
    )

    # FTS：先删后插（standalone FTS，自己维护）
    conn.execute("DELETE FROM books_fts WHERE isbn_13=?", (isbn_13,))
    conn.execute(
        "INSERT INTO books_fts (isbn_13, title, authors, publisher) VALUES (?,?,?,?)",
        (
            isbn_13,
            record.get("title") or "",
            " ".join(record.get("authors") or []),
            record.get("publisher") or "",
        ),
    )

    if field_sources:
        for fs in field_sources:
            conn.execute(
                "INSERT INTO field_sources (isbn_13, field_name, source, confidence, updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(isbn_13, field_name) DO UPDATE SET "
                "source=excluded.source, confidence=excluded.confidence, updated_at=excluded.updated_at",
                (isbn_13, fs.field_name, fs.source, fs.confidence, now),
            )
    if commit:
        conn.commit()


# ── 读 ────────────────────────────────────────────────────────────

def _row_to_book(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for f in LIST_FIELDS:
        d[f] = json.loads(d[f]) if d.get(f) else []
    return d


def get_book(conn: sqlite3.Connection, isbn_13: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM books WHERE isbn_13=?", (isbn_13,)).fetchone()
    if not row:
        return None
    book = _row_to_book(row)
    book["sources"] = get_field_sources(conn, isbn_13)
    return book


def get_field_sources(conn: sqlite3.Connection, isbn_13: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT field_name, source FROM field_sources WHERE isbn_13=?", (isbn_13,)
    ).fetchall()
    return {r["field_name"]: r["source"] for r in rows}


def _fts_escape(q: str) -> str:
    # 包成 FTS5 字符串字面量，转义内部双引号
    return '"' + q.replace('"', '""') + '"'


def search(
    conn: sqlite3.Connection, q: str, page: int = 1, page_size: int = 20
) -> tuple[int, list[dict[str, Any]]]:
    q = q.strip()
    if not q:
        return 0, []
    page = max(1, page)
    offset = (page - 1) * page_size

    # trigram 需要 >=3 字符才能 MATCH；短查询退回 LIKE
    use_like = len(q) < 3
    if not use_like:
        try:
            total = conn.execute(
                "SELECT count(*) AS c FROM books_fts WHERE books_fts MATCH ?",
                (_fts_escape(q),),
            ).fetchone()["c"]
            rows = conn.execute(
                "SELECT b.* FROM books_fts f JOIN books b ON b.isbn_13=f.isbn_13 "
                "WHERE books_fts MATCH ? ORDER BY rank LIMIT ? OFFSET ?",
                (_fts_escape(q), page_size, offset),
            ).fetchall()
            return total, [_row_to_book(r) for r in rows]
        except sqlite3.OperationalError:
            use_like = True  # MATCH 语法异常时退回

    like = f"%{q}%"
    total = conn.execute(
        "SELECT count(*) AS c FROM books_fts WHERE title LIKE ? OR authors LIKE ? OR publisher LIKE ?",
        (like, like, like),
    ).fetchone()["c"]
    rows = conn.execute(
        "SELECT b.* FROM books_fts f JOIN books b ON b.isbn_13=f.isbn_13 "
        "WHERE f.title LIKE ? OR f.authors LIKE ? OR f.publisher LIKE ? "
        "ORDER BY b.publish_year DESC LIMIT ? OFFSET ?",
        (like, like, like, page_size, offset),
    ).fetchall()
    return total, [_row_to_book(r) for r in rows]


def random_book(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM books ORDER BY RANDOM() LIMIT 1"
    ).fetchone()
    if not row:
        return None
    book = _row_to_book(row)
    book["sources"] = get_field_sources(conn, book["isbn_13"])
    return book


def coerce_field(field: str, value: Any) -> Any:
    """把贡献提交的值规整成该字段的内部类型。"""
    if field in LIST_FIELDS:
        if isinstance(value, list):
            items = [str(x).strip() for x in value]
        elif isinstance(value, str):
            items = [p.strip() for p in _LIST_SPLIT.split(value)]
        else:
            items = []
        return [x for x in items if x]
    if field in _INT_FIELDS:
        try:
            return int(str(value).strip())
        except (ValueError, TypeError):
            return None
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _readable(field: str, v: Any) -> str | None:
    """edits 日志里存人类可读的旧值/新值。"""
    if v is None:
        return None
    if field in LIST_FIELDS and isinstance(v, (list, tuple)):
        return ", ".join(str(x) for x in v) or None
    s = str(v)
    return s or None


def apply_contribution(
    conn: sqlite3.Connection,
    isbn_13: str,
    fields: dict[str, Any],
    contributor_hint: str | None,
) -> list[str]:
    """写一次众包贡献：记 edits、更新 books、把改动字段的来源标成 crowdsource。

    返回实际发生改动的字段名列表（值没变的字段跳过）。
    库里没有这本书也接受（众包填补空白），按提交字段新建记录。
    """
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"不可编辑的字段: {sorted(unknown)}")

    existing = get_book(conn, isbn_13)
    base = {f: (existing.get(f) if existing else None) for f in SOURCE_FIELDS}

    now = _now()
    applied: list[str] = []
    changed_sources: list[FieldSource] = []
    for field, raw in fields.items():
        new_val = coerce_field(field, raw)
        old_val = base.get(field)
        if _readable(field, new_val) == _readable(field, old_val):
            continue  # 值没变，不记
        conn.execute(
            "INSERT INTO edits (isbn_13, field_name, old_value, new_value, contributor_hint, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (isbn_13, field, _readable(field, old_val), _readable(field, new_val), contributor_hint, now),
        )
        base[field] = new_val
        applied.append(field)
        changed_sources.append(FieldSource(field, CROWD_SOURCE, CROWD_CONFIDENCE))

    if applied:
        # upsert 全量写回 books（含未改字段）+ 仅改动字段的 field_sources，并重建 FTS
        upsert_book(conn, isbn_13, base, changed_sources)
    return applied


def list_edits(conn: sqlite3.Connection, isbn_13: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT field_name, old_value, new_value, contributor_hint, created_at "
        "FROM edits WHERE isbn_13=? ORDER BY created_at DESC, id DESC",
        (isbn_13,),
    ).fetchall()
    return [dict(r) for r in rows]


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    total = conn.execute("SELECT count(*) AS c FROM books").fetchone()["c"]
    field_count = conn.execute("SELECT count(*) AS c FROM field_sources").fetchone()["c"]
    by_source = [
        {"source": r["source"], "count": r["c"]}
        for r in conn.execute(
            "SELECT source, count(*) AS c FROM field_sources GROUP BY source ORDER BY c DESC"
        ).fetchall()
    ]
    recent = [
        r["isbn_13"]
        for r in conn.execute(
            "SELECT isbn_13 FROM books ORDER BY updated_at DESC LIMIT 10"
        ).fetchall()
    ]
    return {
        "total_books": total,
        "field_value_count": field_count,
        "by_source": by_source,
        "recent_isbns": recent,
    }
