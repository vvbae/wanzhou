"""三层数据层（作者 / 作品 / 版本）+ 字段来源 + 贡献审核。

取代 v0.1 的扁平 db.py（见 docs/design.md v0.2）。迁移期两者并存，
读侧切换到本模块后退役 db.py。

结构：
    author ──< work_authors >── work ──< edition（主键 isbn_13）
译者在版本层；作者/原作名/简介在作品层；作者简介/原名在作者层。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from cnbib.cleaning import has_cjk

DEFAULT_DB = "cnbib.db"

# 各实体的可写列（不含主键/时间戳）
AUTHOR_FIELDS = ("name", "name_original", "aliases", "bio", "ol_key")
WORK_FIELDS = ("title", "title_original", "description", "subjects", "first_publish_year", "ol_key")
EDITION_FIELDS = (
    "work_id", "title", "isbn_10", "subtitle", "translators", "publisher", "publish_date",
    "publish_year", "cover_url", "page_count", "language", "series", "format", "ol_key",
)
# JSON list 列
LIST_FIELDS = {"aliases", "subjects", "translators"}

_TABLE = {"author": "authors", "work": "works", "edition": "editions"}
_PK = {"author": "id", "work": "id", "edition": "isbn_13"}
_FIELDS = {"author": AUTHOR_FIELDS, "work": WORK_FIELDS, "edition": EDITION_FIELDS}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def connect(path: str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _fts_ok(conn: sqlite3.Connection) -> str:
    try:
        conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS _p USING fts5(x, tokenize='trigram')")
        conn.execute("DROP TABLE IF EXISTS _p")
        return "trigram"
    except sqlite3.OperationalError:
        return "unicode61"


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS authors (
            id TEXT PRIMARY KEY, name TEXT, name_original TEXT, aliases TEXT,
            bio TEXT, ol_key TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS works (
            id TEXT PRIMARY KEY, title TEXT, title_original TEXT, description TEXT,
            subjects TEXT, first_publish_year INTEGER, ol_key TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS work_authors (
            work_id TEXT NOT NULL, author_id TEXT NOT NULL, role TEXT DEFAULT 'author',
            PRIMARY KEY (work_id, author_id)
        );
        CREATE TABLE IF NOT EXISTS editions (
            isbn_13 TEXT PRIMARY KEY, work_id TEXT, title TEXT, isbn_10 TEXT, subtitle TEXT,
            translators TEXT, publisher TEXT, publish_date TEXT, publish_year INTEGER,
            cover_url TEXT, page_count INTEGER, language TEXT, series TEXT, format TEXT,
            ol_key TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS field_sources (
            entity_type TEXT NOT NULL, entity_id TEXT NOT NULL, field_name TEXT NOT NULL,
            source TEXT NOT NULL, confidence INTEGER, updated_at TEXT,
            PRIMARY KEY (entity_type, entity_id, field_name)
        );
        CREATE TABLE IF NOT EXISTS contributions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            status TEXT NOT NULL DEFAULT 'pending',
            target_type TEXT NOT NULL, target_id TEXT, kind TEXT NOT NULL,
            payload TEXT, contributor_hint TEXT,
            reviewed_by TEXT, review_note TEXT, created_at TEXT, reviewed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_ed_work ON editions(work_id);
        CREATE INDEX IF NOT EXISTS idx_wa_author ON work_authors(author_id);
        CREATE INDEX IF NOT EXISTS idx_contrib_status ON contributions(status);
        """
    )
    tok = _fts_ok(conn)
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS works_fts USING fts5("
        f"work_id UNINDEXED, title, authors, publisher, tokenize='{tok}')"
    )
    conn.commit()


# ── JSON 助手 ──────────────────────────────────────────────────────
def _dump(field: str, v: Any) -> Any:
    if field in LIST_FIELDS:
        return json.dumps(v, ensure_ascii=False) if v else None
    return v


def _load_row(entity: str, row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for f in LIST_FIELDS:
        if f in d:
            d[f] = json.loads(d[f]) if d.get(f) else []
    return d


# ── upsert 实体 ────────────────────────────────────────────────────
def _upsert(conn, entity: str, pk_val: str, data: dict, commit: bool) -> str:
    table, pk, fields = _TABLE[entity], _PK[entity], _FIELDS[entity]
    now = _now()
    row = conn.execute(f"SELECT created_at FROM {table} WHERE {pk}=?", (pk_val,)).fetchone()
    created = row["created_at"] if row else now
    cols = [pk] + list(fields) + ["created_at", "updated_at"]
    vals = {pk: pk_val, "created_at": created, "updated_at": now}
    for f in fields:
        vals[f] = _dump(f, data.get(f))
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != pk)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT({pk}) DO UPDATE SET {updates}",
        vals,
    )
    if commit:
        conn.commit()
    return pk_val


def upsert_author(conn, author: dict, *, id: str | None = None, commit: bool = True) -> str:
    aid = id or author.get("id") or new_id("a")
    return _upsert(conn, "author", aid, author, commit)


def upsert_work(conn, work: dict, *, id: str | None = None,
                author_ids: list[str] | None = None, commit: bool = True,
                reindex: bool = True) -> str:
    wid = id or work.get("id") or new_id("w")
    _upsert(conn, "work", wid, work, commit=False)
    if author_ids is not None:
        conn.execute("DELETE FROM work_authors WHERE work_id=?", (wid,))
        for aid in author_ids:
            conn.execute(
                "INSERT OR IGNORE INTO work_authors (work_id, author_id) VALUES (?,?)",
                (wid, aid),
            )
    if reindex:                       # 批量导入时关掉，最后统一 rebuild_fts
        _reindex_work(conn, wid)
    if commit:
        conn.commit()
    return wid


def rebuild_fts(conn) -> None:
    """全量重建 works_fts（批量导入后调用，确保作者名也进索引）。"""
    conn.execute("DELETE FROM works_fts")
    conn.execute(
        "INSERT INTO works_fts (work_id, title, authors, publisher) "
        "SELECT w.id, "
        "  TRIM(COALESCE(w.title,'') || ' ' || "
        "    COALESCE((SELECT group_concat(e.title,' ') FROM editions e "
        "              WHERE e.work_id=w.id AND e.title IS NOT NULL), '')), "
        "  COALESCE((SELECT group_concat(a.name, ' ') FROM work_authors wa "
        "            JOIN authors a ON a.id=wa.author_id WHERE wa.work_id=w.id), ''), "
        "  COALESCE((SELECT publisher FROM editions e WHERE e.work_id=w.id "
        "            AND e.publisher IS NOT NULL LIMIT 1), '') "
        "FROM works w"
    )
    conn.commit()


def upsert_edition(conn, isbn_13: str, edition: dict, *, commit: bool = True) -> str:
    _upsert(conn, "edition", isbn_13, edition, commit=commit)
    return isbn_13


def _display_title(conn, work_id: str, work_title: str | None) -> str | None:
    """展示用标题：优先中文。work 标题常是原文/拼音，中文标题在版本上。"""
    if has_cjk(work_title):
        return work_title
    rows = conn.execute(
        "SELECT title FROM editions WHERE work_id=? AND title IS NOT NULL", (work_id,)
    ).fetchall()
    for r in rows:
        if has_cjk(r["title"]):
            return r["title"]
    return work_title or (rows[0]["title"] if rows else None)


def _author_names(conn, work_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT a.name FROM work_authors wa JOIN authors a ON a.id=wa.author_id "
        "WHERE wa.work_id=? AND a.name IS NOT NULL", (work_id,)
    ).fetchall()
    return [r["name"] for r in rows]


def _reindex_work(conn, work_id: str) -> None:
    w = conn.execute("SELECT title FROM works WHERE id=?", (work_id,)).fetchone()
    if not w:
        return
    ed = conn.execute(
        "SELECT group_concat(title, ' ') t, "
        "(SELECT publisher FROM editions WHERE work_id=? AND publisher IS NOT NULL LIMIT 1) p "
        "FROM editions WHERE work_id=? AND title IS NOT NULL", (work_id, work_id)
    ).fetchone()
    # 索引同时含 work 标题 + 各版本（中文）标题，搜中文才搜得到
    title_idx = " ".join(x for x in [w["title"], ed["t"] if ed else None] if x)
    conn.execute("DELETE FROM works_fts WHERE work_id=?", (work_id,))
    conn.execute(
        "INSERT INTO works_fts (work_id, title, authors, publisher) VALUES (?,?,?,?)",
        (work_id, title_idx, " ".join(_author_names(conn, work_id)), ed["p"] if ed else ""),
    )


# ── field_sources ─────────────────────────────────────────────────
def set_sources(conn, entity_type: str, entity_id: str, sources: list, *, commit: bool = True) -> None:
    now = _now()
    for fs in sources:
        conn.execute(
            "INSERT INTO field_sources (entity_type, entity_id, field_name, source, confidence, updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(entity_type, entity_id, field_name) DO UPDATE SET "
            "source=excluded.source, confidence=excluded.confidence, updated_at=excluded.updated_at",
            (entity_type, entity_id, fs.field_name, fs.source, fs.confidence, now),
        )
    if commit:
        conn.commit()


def get_sources(conn, entity_type: str, entity_id: str) -> dict[str, str]:
    rows = conn.execute(
        "SELECT field_name, source FROM field_sources WHERE entity_type=? AND entity_id=?",
        (entity_type, entity_id),
    ).fetchall()
    return {r["field_name"]: r["source"] for r in rows}


# ── 读 ────────────────────────────────────────────────────────────
def get_author(conn, author_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM authors WHERE id=?", (author_id,)).fetchone()
    if not row:
        return None
    a = _load_row("author", row)
    a["works"] = [
        {"id": r["id"], "title": r["title"], "first_publish_year": r["first_publish_year"]}
        for r in conn.execute(
            "SELECT w.id, w.title, w.first_publish_year FROM work_authors wa "
            "JOIN works w ON w.id=wa.work_id WHERE wa.author_id=? ORDER BY w.first_publish_year DESC",
            (author_id,),
        ).fetchall()
    ]
    a["sources"] = get_sources(conn, "author", author_id)
    return a


def get_work(conn, work_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM works WHERE id=?", (work_id,)).fetchone()
    if not row:
        return None
    w = _load_row("work", row)
    w["authors"] = [
        {"id": r["id"], "name": r["name"]}
        for r in conn.execute(
            "SELECT a.id, a.name FROM work_authors wa JOIN authors a ON a.id=wa.author_id WHERE wa.work_id=?",
            (work_id,),
        ).fetchall()
    ]
    w["editions"] = [
        _load_row("edition", r)
        for r in conn.execute(
            "SELECT * FROM editions WHERE work_id=? ORDER BY publish_year DESC", (work_id,)
        ).fetchall()
    ]
    w["display_title"] = _display_title(conn, work_id, w.get("title"))
    w["sources"] = get_sources(conn, "work", work_id)
    return w


def get_edition(conn, isbn_13: str) -> dict | None:
    row = conn.execute("SELECT * FROM editions WHERE isbn_13=?", (isbn_13,)).fetchone()
    if not row:
        return None
    e = _load_row("edition", row)
    e["sources"] = get_sources(conn, "edition", isbn_13)
    e["work"] = get_work(conn, e["work_id"]) if e.get("work_id") else None
    return e


def _fts_q(q: str) -> str:
    return '"' + q.replace('"', '""') + '"'


def search(conn, q: str, page: int = 1, page_size: int = 20) -> tuple[int, list[dict]]:
    q = q.strip()
    if not q:
        return 0, []
    offset = (max(1, page) - 1) * page_size
    use_like = len(q) < 3
    if not use_like:
        try:
            total = conn.execute(
                "SELECT count(*) c FROM works_fts WHERE works_fts MATCH ?", (_fts_q(q),)
            ).fetchone()["c"]
            rows = conn.execute(
                "SELECT w.* FROM works_fts f JOIN works w ON w.id=f.work_id "
                "WHERE works_fts MATCH ? ORDER BY rank LIMIT ? OFFSET ?",
                (_fts_q(q), page_size, offset),
            ).fetchall()
            return total, [_work_hit(conn, r) for r in rows]
        except sqlite3.OperationalError:
            use_like = True
    like = f"%{q}%"
    total = conn.execute(
        "SELECT count(*) c FROM works_fts WHERE title LIKE ? OR authors LIKE ? OR publisher LIKE ?",
        (like, like, like),
    ).fetchone()["c"]
    rows = conn.execute(
        "SELECT w.* FROM works_fts f JOIN works w ON w.id=f.work_id "
        "WHERE f.title LIKE ? OR f.authors LIKE ? OR f.publisher LIKE ? LIMIT ? OFFSET ?",
        (like, like, like, page_size, offset),
    ).fetchall()
    return total, [_work_hit(conn, r) for r in rows]


def _work_hit(conn, row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "title": _display_title(conn, row["id"], row["title"]),   # 优先中文
        "first_publish_year": row["first_publish_year"],
        "authors": _author_names(conn, row["id"]),
    }


def random_edition(conn) -> dict | None:
    row = conn.execute("SELECT isbn_13 FROM editions ORDER BY RANDOM() LIMIT 1").fetchone()
    return get_edition(conn, row["isbn_13"]) if row else None


def stats(conn) -> dict[str, Any]:
    def c(sql):
        return conn.execute(sql).fetchone()[0]
    return {
        "authors": c("SELECT count(*) FROM authors"),
        "works": c("SELECT count(*) FROM works"),
        "editions": c("SELECT count(*) FROM editions"),
        "pending_contributions": c("SELECT count(*) FROM contributions WHERE status='pending'"),
        "by_source": [
            {"source": r["source"], "count": r["c"]}
            for r in conn.execute(
                "SELECT source, count(*) c FROM field_sources GROUP BY source ORDER BY c DESC"
            ).fetchall()
        ],
    }


# ── 贡献 / 审核 ────────────────────────────────────────────────────
def add_contribution(conn, *, target_type: str, kind: str, payload: dict,
                     target_id: str | None = None, contributor_hint: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO contributions (status, target_type, target_id, kind, payload, contributor_hint, created_at) "
        "VALUES ('pending', ?, ?, ?, ?, ?, ?)",
        (target_type, target_id, kind, json.dumps(payload, ensure_ascii=False), contributor_hint, _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_contributions(conn, status: str = "pending", limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM contributions WHERE status=? ORDER BY created_at DESC LIMIT ?",
        (status, limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"]) if d.get("payload") else {}
        out.append(d)
    return out


class _FS:
    __slots__ = ("field_name", "source", "confidence")

    def __init__(self, field_name, source="crowdsource", confidence=100):
        self.field_name, self.source, self.confidence = field_name, source, confidence


def approve_contribution(conn, contrib_id: int, *, reviewer: str = "admin", note: str = "") -> bool:
    """通过一条贡献：应用到实体 + 字段来源标 crowdsource。"""
    row = conn.execute("SELECT * FROM contributions WHERE id=? AND status='pending'", (contrib_id,)).fetchone()
    if not row:
        return False
    c = dict(row)
    payload = json.loads(c["payload"]) if c.get("payload") else {}
    etype, tid, kind = c["target_type"], c["target_id"], c["kind"]

    if kind == "add":
        if etype == "edition":
            tid = payload["isbn_13"]
            upsert_edition(conn, tid, payload, commit=False)
        elif etype == "work":
            tid = upsert_work(conn, payload, commit=False)
        elif etype == "author":
            tid = upsert_author(conn, payload, commit=False)
        fields = [f for f in payload if f in _FIELDS[etype]]
    else:  # edit
        _upsert_partial(conn, etype, tid, payload)
        fields = list(payload.keys())

    set_sources(conn, etype, tid, [_FS(f) for f in fields], commit=False)
    if etype == "edition" and payload.get("work_id"):
        _reindex_work(conn, payload["work_id"])
    conn.execute(
        "UPDATE contributions SET status='approved', reviewed_by=?, review_note=?, reviewed_at=?, target_id=? WHERE id=?",
        (reviewer, note, _now(), tid, contrib_id),
    )
    conn.commit()
    return True


def _upsert_partial(conn, entity: str, pk_val: str, payload: dict) -> None:
    """只更新 payload 里的列（edit 用）。"""
    table, pk, fields = _TABLE[entity], _PK[entity], _FIELDS[entity]
    sets = {f: _dump(f, v) for f, v in payload.items() if f in fields}
    if not sets:
        return
    assign = ", ".join(f"{k}=?" for k in sets) + ", updated_at=?"
    conn.execute(
        f"UPDATE {table} SET {assign} WHERE {pk}=?",
        (*sets.values(), _now(), pk_val),
    )
    if entity == "work":
        _reindex_work(conn, pk_val)


def reject_contribution(conn, contrib_id: int, *, reviewer: str = "admin", note: str = "") -> bool:
    cur = conn.execute(
        "UPDATE contributions SET status='rejected', reviewed_by=?, review_note=?, reviewed_at=? "
        "WHERE id=? AND status='pending'",
        (reviewer, note, _now(), contrib_id),
    )
    conn.commit()
    return cur.rowcount > 0
