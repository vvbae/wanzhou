"""三层数据层（作者 / 作品 / 版本）+ 字段来源 + 贡献审核。

取代 v0.1 的扁平 db.py（见 docs/design.md v0.2）。迁移期两者并存，
读侧切换到本模块后退役 db.py。

结构：
    author ──< work_authors >── work ──< edition（主键 isbn_13）
译者在版本层；作者/原作名/简介在作品层；作者简介/原名在作者层。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from cnbib.cleaning import has_cjk

DEFAULT_DB = "cnbib.db"

# 各实体的可写列（不含主键/时间戳）
AUTHOR_FIELDS = ("name", "name_original", "aliases", "bio", "birth_date", "death_date", "ol_key")
WORK_FIELDS = ("title", "title_original", "description", "subjects", "first_publish_year", "ol_key")
EDITION_FIELDS = (
    "work_id", "title", "isbn_10", "subtitle", "translators", "publisher", "publish_date",
    "publish_year", "cover_url", "page_count", "language", "series", "format", "clc", "ol_key",
)
_INT_COLS = {"first_publish_year", "publish_year", "page_count"}
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
            bio TEXT, birth_date TEXT, death_date TEXT, ol_key TEXT,
            created_at TEXT, updated_at TEXT
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
            clc TEXT, ol_key TEXT, enriched TEXT, created_at TEXT, updated_at TEXT
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
        CREATE TABLE IF NOT EXISTS users (
            username TEXT PRIMARY KEY, password_hash TEXT,
            role TEXT DEFAULT 'user', created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token TEXT PRIMARY KEY, username TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS invites (
            token TEXT PRIMARY KEY, role TEXT, created_by TEXT,
            used_by TEXT, created_at TEXT, used_at TEXT
        );
        CREATE TABLE IF NOT EXISTS tags (
            slug TEXT PRIMARY KEY, name TEXT, created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS work_tags (
            work_id TEXT NOT NULL, tag_slug TEXT NOT NULL,
            PRIMARY KEY (work_id, tag_slug)
        );
        CREATE INDEX IF NOT EXISTS idx_ed_work ON editions(work_id);
        CREATE INDEX IF NOT EXISTS idx_wa_author ON work_authors(author_id);
        CREATE INDEX IF NOT EXISTS idx_contrib_status ON contributions(status);
        CREATE INDEX IF NOT EXISTS idx_wt_tag ON work_tags(tag_slug);
        """
    )
    _ensure_columns(conn)
    tok = _fts_ok(conn)
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS works_fts USING fts5("
        f"work_id UNINDEXED, title, authors, publisher, tokenize='{tok}')"
    )
    conn.commit()


def _ensure_columns(conn) -> None:
    """给已存在的表补齐新增列（轻量迁移），不丢旧数据。"""
    for entity in ("author", "work", "edition"):
        table = _TABLE[entity]
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col in _FIELDS[entity]:
            if col not in have:
                typ = "INTEGER" if col in _INT_COLS else "TEXT"
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    # 不在 _FIELDS 里的附加列
    if "enriched" not in {r["name"] for r in conn.execute("PRAGMA table_info(editions)")}:
        conn.execute("ALTER TABLE editions ADD COLUMN enriched TEXT")
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(contributions)")}
    if "user_id" not in cols:
        conn.execute("ALTER TABLE contributions ADD COLUMN user_id TEXT")
    if "field_name" not in cols:
        conn.execute("ALTER TABLE contributions ADD COLUMN field_name TEXT")
    if "email" not in {r["name"] for r in conn.execute("PRAGMA table_info(users)")}:
        conn.execute("ALTER TABLE users ADD COLUMN email TEXT")


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
        {"id": r["id"], "title": _display_title(conn, r["id"], r["title"]),
         "first_publish_year": r["first_publish_year"],
         "isbn_13": _work_rep(conn, r["id"])[0], "cover_url": _work_rep(conn, r["id"])[1]}
        for r in conn.execute(
            "SELECT w.id, w.title, w.first_publish_year FROM work_authors wa "
            "JOIN works w ON w.id=wa.work_id WHERE wa.author_id=? ORDER BY w.first_publish_year DESC",
            (author_id,),
        ).fetchall()
    ]
    a["sources"] = get_sources(conn, "author", author_id)
    a["last_edit"] = last_edit(conn, "author", author_id)
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
    w["cover_url"] = _work_cover(conn, work_id)
    w["tags"] = tags_for_work(conn, work_id)
    w["sources"] = get_sources(conn, "work", work_id)
    w["last_edit"] = last_edit(conn, "work", work_id)
    return w


def get_edition(conn, isbn_13: str) -> dict | None:
    row = conn.execute("SELECT * FROM editions WHERE isbn_13=?", (isbn_13,)).fetchone()
    if not row:
        return None
    e = _load_row("edition", row)
    e["sources"] = get_sources(conn, "edition", isbn_13)
    e["last_edit"] = last_edit(conn, "edition", isbn_13)
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


def last_edit(conn, entity_type: str, entity_id: str) -> dict | None:
    """该实体最近一次通过审核的众包编辑：谁、何时。给详情页显示'最后编辑'。"""
    r = conn.execute(
        "SELECT user_id, reviewed_by, reviewed_at FROM contributions "
        "WHERE status='approved' AND target_type=? AND IFNULL(target_id,'')=IFNULL(?,'') "
        "ORDER BY reviewed_at DESC LIMIT 1", (entity_type, entity_id),
    ).fetchone()
    if not r:
        return None
    return {"by": r["user_id"] or "匿名", "at": (r["reviewed_at"] or "")[:10]}


def _work_rep(conn, work_id: str) -> tuple[str | None, str | None]:
    """作品的代表版本：(isbn, cover)，优先有封面的那版。卡片直接链到这本。"""
    r = conn.execute(
        "SELECT isbn_13, cover_url FROM editions WHERE work_id=? "
        "ORDER BY (cover_url IS NULL) LIMIT 1", (work_id,)
    ).fetchone()
    return (r["isbn_13"], r["cover_url"]) if r else (None, None)


def _work_cover(conn, work_id: str) -> str | None:
    return _work_rep(conn, work_id)[1]


def _work_hit(conn, row: sqlite3.Row) -> dict:
    isbn, cover = _work_rep(conn, row["id"])
    return {
        "id": row["id"],
        "isbn_13": isbn,                                          # 代表版本，卡片直接进这本
        "title": _display_title(conn, row["id"], row["title"]),   # 优先中文
        "first_publish_year": row["first_publish_year"],
        "authors": _author_names(conn, row["id"]),
        "cover_url": cover,
    }


def apply_enrichment(conn, isbn: str, record: dict, *, source: str = "google_books",
                     confidence: int = 10, commit: bool = True) -> list[str]:
    """用外部源数据补一条版本：拼音标题→中文、补封面、补作品简介。

    record 是聚合后的扁平 dict（含中文 title / description / cover_url）。
    只补"缺或拼音"的字段，标来源（默认 google_books，可信，不走审核）。返回改了的字段。
    """
    e = conn.execute(
        "SELECT title, cover_url, work_id FROM editions WHERE isbn_13=?", (isbn,)
    ).fetchone()
    if not e:
        return []
    changed, ef = [], {}
    if record.get("title") and has_cjk(record["title"]) and not has_cjk(e["title"]):
        ef["title"] = record["title"]; changed.append("title")
    if record.get("cover_url") and not e["cover_url"]:
        ef["cover_url"] = record["cover_url"]; changed.append("cover_url")
    if ef:
        _upsert_partial(conn, "edition", isbn, ef)
        set_sources(conn, "edition", isbn, [_FS(k, source, confidence) for k in ef], commit=False)

    wid = e["work_id"]
    if wid and record.get("description"):
        w = conn.execute("SELECT description FROM works WHERE id=?", (wid,)).fetchone()
        if w and not w["description"]:
            _upsert_partial(conn, "work", wid, {"description": record["description"]})
            set_sources(conn, "work", wid, [_FS("description", source, confidence)], commit=False)
            changed.append("description")

    conn.execute("UPDATE editions SET enriched=? WHERE isbn_13=?", (_now(), isbn))
    if "title" in ef and wid:
        _reindex_work(conn, wid)
    if commit:
        conn.commit()
    return changed


def needs_enrichment(conn, limit: int = 500) -> list[str]:
    """挑还没富化、且标题是拼音/拉丁的版本 ISBN（给批量富化用）。"""
    # 优先补有封面的（更可能是真书/有人看），同样配额效果更明显
    rows = conn.execute(
        "SELECT isbn_13, title FROM editions WHERE enriched IS NULL AND title IS NOT NULL "
        "ORDER BY (cover_url IS NULL) LIMIT ?", (limit * 4,)
    ).fetchall()
    return [r["isbn_13"] for r in rows if not has_cjk(r["title"])][:limit]


# ── 账号 / 会话 ────────────────────────────────────────────────────
def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200_000).hex()
    return f"pbkdf2$200000${salt}${h}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _algo, iters, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(iters)).hex()
        return secrets.compare_digest(calc, h)
    except Exception:  # noqa: BLE001
        return False


def create_user(conn, username: str, password: str, role: str = "user",
                email: str | None = None) -> dict:
    u = username.strip()
    if not u or len(password) < 6:
        raise ValueError("用户名不能为空，密码至少 6 位")
    if conn.execute("SELECT 1 FROM users WHERE username=?", (u,)).fetchone():
        raise ValueError("用户名已被占用")
    conn.execute("INSERT INTO users (username, password_hash, role, email, created_at) VALUES (?,?,?,?,?)",
                 (u, hash_password(password), role, (email or "").strip() or None, _now()))
    conn.commit()
    return {"username": u, "role": role}


def reviewer_emails(conn) -> list[str]:
    """有邮箱的审核员/管理员邮箱，用于待审通知。"""
    rows = conn.execute(
        "SELECT email FROM users WHERE role IN ('reviewer','admin') AND email IS NOT NULL AND email<>''"
    ).fetchall()
    return [r["email"] for r in rows]


def set_role(conn, username: str, role: str) -> bool:
    cur = conn.execute("UPDATE users SET role=? WHERE username=?", (role, username))
    conn.commit()
    return cur.rowcount > 0


def verify_credentials(conn, username: str, password: str) -> dict | None:
    row = conn.execute("SELECT username, password_hash, role FROM users WHERE username=?",
                       (username.strip(),)).fetchone()
    if row and verify_password(password, row["password_hash"]):
        return {"username": row["username"], "role": row["role"]}
    return None


def create_session(conn, username: str) -> str:
    token = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO sessions (token, username, created_at) VALUES (?,?,?)",
                 (token, username, _now()))
    conn.commit()
    return token


def get_session_user(conn, token: str | None) -> dict | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT u.username, u.role FROM sessions s JOIN users u ON u.username=s.username "
        "WHERE s.token=?", (token,)
    ).fetchone()
    return {"username": row["username"], "role": row["role"]} if row else None


def delete_session(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()


def create_invite(conn, role: str, created_by: str) -> str:
    """生成一个邀请码：用它注册即获得 reviewer/admin 角色。"""
    if role not in ("reviewer", "admin"):
        raise ValueError("邀请角色只能是 reviewer 或 admin")
    token = secrets.token_urlsafe(24)
    conn.execute("INSERT INTO invites (token, role, created_by, created_at) VALUES (?,?,?,?)",
                 (token, role, created_by, _now()))
    conn.commit()
    return token


def invite_role(conn, token: str) -> str | None:
    """返回未使用邀请的角色；无效/已用返回 None。"""
    r = conn.execute("SELECT role, used_by FROM invites WHERE token=?", (token,)).fetchone()
    return r["role"] if r and not r["used_by"] else None


def use_invite(conn, token: str, used_by: str) -> None:
    conn.execute("UPDATE invites SET used_by=?, used_at=? WHERE token=?",
                 (used_by, _now(), token))
    conn.commit()


# ── 标签（实体 + 多对多，可按标签浏览）──────────────────────────────
def tag_slug(name: str) -> str:
    """归一成 slug：去首尾空格、空白折叠、小写（中文不受影响）。用于去重与 URL。"""
    return re.sub(r"\s+", " ", str(name).strip()).lower()


def add_tags_to_work(conn, work_id: str, names, *, commit: bool = True) -> None:
    now = _now()
    for name in names or []:
        nm = re.sub(r"\s+", " ", str(name).strip())
        if not nm:
            continue
        slug = tag_slug(nm)
        conn.execute("INSERT OR IGNORE INTO tags (slug, name, created_at) VALUES (?,?,?)",
                     (slug, nm, now))
        conn.execute("INSERT OR IGNORE INTO work_tags (work_id, tag_slug) VALUES (?,?)",
                     (work_id, slug))
    if commit:
        conn.commit()


def tags_for_work(conn, work_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT t.slug, t.name FROM work_tags wt JOIN tags t ON t.slug=wt.tag_slug "
        "WHERE wt.work_id=? ORDER BY t.name", (work_id,)
    ).fetchall()
    return [{"slug": r["slug"], "name": r["name"]} for r in rows]


def search_tags(conn, q: str, limit: int = 10) -> list[dict]:
    """标签自动补全：按名字匹配，按使用量排序。"""
    q = q.strip()
    if not q:
        return []
    rows = conn.execute(
        "SELECT t.slug, t.name, count(wt.work_id) c FROM tags t "
        "JOIN work_tags wt ON wt.tag_slug=t.slug WHERE t.name LIKE ? "
        "GROUP BY t.slug ORDER BY c DESC LIMIT ?", (f"%{q}%", limit)
    ).fetchall()
    return [{"slug": r["slug"], "name": r["name"], "count": r["c"]} for r in rows]


# 人工挑的中文主题 → 真实英文 slug（OL 主题词是英文，给大众一个中文入口）
_CURATED_TOPICS = [
    ("历史", "history"), ("小说", "fiction"), ("传记", "biography"),
    ("中国文学", "chinese literature"), ("诗歌", "chinese poetry"),
    ("经济", "economic conditions"), ("政治", "politics and government"),
    ("哲学", "philosophy"), ("语言", "chinese language"),
    ("社会风俗", "social life and customs"), ("艺术", "art"), ("教育", "education"),
    ("宗教", "religion"), ("科学", "science"), ("文学评论", "criticism and interpretation"),
]


def top_tags(conn, n: int = 12) -> list[dict]:
    """首页"按主题浏览"：中文主题标签（映射到真实 slug，只保留库里有的）。"""
    out = []
    for name, slug in _CURATED_TOPICS:
        if conn.execute("SELECT 1 FROM work_tags WHERE tag_slug=? LIMIT 1", (slug,)).fetchone():
            out.append({"name": name, "slug": slug})
        if len(out) >= n:
            break
    return out


def works_by_tag(conn, slug: str, page: int = 1, page_size: int = 20) -> tuple[int, str, list[dict]]:
    slug = tag_slug(slug)
    name_row = conn.execute("SELECT name FROM tags WHERE slug=?", (slug,)).fetchone()
    if not name_row:
        return 0, slug, []
    total = conn.execute("SELECT count(*) FROM work_tags WHERE tag_slug=?", (slug,)).fetchone()[0]
    offset = (max(1, page) - 1) * page_size
    rows = conn.execute(
        "SELECT w.* FROM work_tags wt JOIN works w ON w.id=wt.work_id "
        "WHERE wt.tag_slug=? LIMIT ? OFFSET ?", (slug, page_size, offset)
    ).fetchall()
    return total, name_row["name"], [_work_hit(conn, r) for r in rows]


def set_work_authors(conn, work_id: str, entries, *, source: str = "crowdsource",
                     confidence: int = 100, commit: bool = True) -> None:
    """重设作品的作者（OL 式）。entries 每项是 {"id": 已有作者} 或 {"name": 新名}：
    有 id 且存在 → 复用；否则按名找现成的，再没有才新建。避免造重复。"""
    conn.execute("DELETE FROM work_authors WHERE work_id=?", (work_id,))
    for e in entries or []:
        aid = None
        if isinstance(e, dict) and e.get("id"):
            if conn.execute("SELECT 1 FROM authors WHERE id=?", (e["id"],)).fetchone():
                aid = e["id"]
        if not aid:
            nm = re.sub(r"\s+", " ",
                        (e.get("name") if isinstance(e, dict) else str(e)).strip())
            if not nm:
                continue
            row = conn.execute("SELECT id FROM authors WHERE name=? LIMIT 1", (nm,)).fetchone()
            if row:
                aid = row["id"]
            else:
                aid = new_id("a")
                upsert_author(conn, {"name": nm}, id=aid, commit=False)
                set_sources(conn, "author", aid, [_FS("name", source, confidence)], commit=False)
        conn.execute("INSERT OR IGNORE INTO work_authors (work_id, author_id) VALUES (?,?)",
                     (work_id, aid))
    _reindex_work(conn, work_id)
    if commit:
        conn.commit()


def build_tags_from_subjects(conn, *, batch: int = 5000) -> int:
    """迁移：把所有 works.subjects 拆成 tags + work_tags。返回处理的作品数。"""
    conn.execute("PRAGMA synchronous=OFF")
    n = 0
    for row in conn.execute("SELECT id, subjects FROM works WHERE subjects IS NOT NULL"):
        subs = json.loads(row["subjects"]) if row["subjects"] else []
        if subs:
            add_tags_to_work(conn, row["id"], subs, commit=False)
        n += 1
        if n % batch == 0:
            conn.commit()
    conn.commit()
    return n


def search_authors(conn, q: str, limit: int = 6) -> list[dict]:
    """按姓名搜作者（搜"曹雪芹"应先出作者本人，而不是书）。"""
    q = q.strip()
    if not q:
        return []
    rows = conn.execute(
        "SELECT id, name, birth_date, death_date FROM authors "
        "WHERE name LIKE ? ORDER BY length(name) LIMIT ?", (f"%{q}%", limit)
    ).fetchall()
    out = []
    for r in rows:
        cnt = conn.execute(
            "SELECT count(*) FROM work_authors WHERE author_id=?", (r["id"],)
        ).fetchone()[0]
        out.append({"id": r["id"], "name": r["name"], "birth_date": r["birth_date"],
                    "death_date": r["death_date"], "work_count": cnt})
    return out


def random_showcase(conn, n: int = 8) -> list[dict]:
    """首页展示用：随机若干本（有封面、优先中文标题），返回作品卡片（去重到作品）。"""
    rows = conn.execute(
        "SELECT isbn_13, work_id, title, cover_url FROM editions "
        "WHERE cover_url IS NOT NULL AND work_id IS NOT NULL AND title IS NOT NULL "
        "ORDER BY RANDOM() LIMIT ?", (n * 25,)
    ).fetchall()
    cjk, rest, seen = [], [], set()
    for r in rows:
        wid = r["work_id"]
        if wid in seen:
            continue
        seen.add(wid)
        wt = conn.execute("SELECT title FROM works WHERE id=?", (wid,)).fetchone()
        title = _display_title(conn, wid, wt["title"] if wt else r["title"]) or r["title"]
        card = {"work_id": wid, "isbn_13": r["isbn_13"], "title": title,
                "cover_url": r["cover_url"], "authors": _author_names(conn, wid)}
        (cjk if has_cjk(title) else rest).append(card)
        if len(cjk) >= n:
            break
    return (cjk + rest)[:n]   # 中文标题优先，不够再用拼音补


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
def create_book(conn, payload: dict, *, source: str = "crowdsource",
                confidence: int = 100, commit: bool = True) -> str:
    """从一条扁平 payload 落一整本书：作者 + 作品 + 版本，并标来源。

    payload 关键字段：isbn_13(必填), title, authors(名字列表), translators,
    publisher, publish_date/year, title_original, description, subjects, work_id(可选关联)。
    供"加书"审核通过、以及将来从源站可信导入复用。
    """
    isbn = payload["isbn_13"]
    # 查重：版本已存在 → 复用其作品，不重建作者/作品（避免重复 work / 覆盖原作者）
    existing = conn.execute("SELECT work_id FROM editions WHERE isbn_13=?", (isbn,)).fetchone()
    if existing and existing["work_id"]:
        wid = existing["work_id"]
    else:
        author_ids = []
        for name in payload.get("authors") or []:
            nm = str(name).strip()
            if not nm:
                continue
            aid = new_id("a")
            upsert_author(conn, {"name": nm}, id=aid, commit=False)
            set_sources(conn, "author", aid, [_FS("name", source, confidence)], commit=False)
            author_ids.append(aid)
        wid = payload.get("work_id") or new_id("w")
        wf = {k: payload[k] for k in ("title", "title_original", "description", "subjects",
                                      "first_publish_year") if payload.get(k) not in (None, "", [])}
        upsert_work(conn, wf, id=wid, author_ids=author_ids or None, commit=False, reindex=False)
        set_sources(conn, "work", wid, [_FS(k, source, confidence) for k in wf], commit=False)
        if payload.get("subjects"):
            add_tags_to_work(conn, wid, payload["subjects"], commit=False)

    ef = {k: payload[k] for k in ("title", "subtitle", "translators", "publisher",
          "publish_date", "publish_year", "cover_url", "page_count", "language",
          "series", "format", "isbn_10") if payload.get(k) not in (None, "", [])}
    ef["work_id"] = wid
    upsert_edition(conn, isbn, ef, commit=False)
    set_sources(conn, "edition", isbn,
                [_FS(k, source, confidence) for k in ef if k != "work_id"], commit=False)
    _reindex_work(conn, wid)
    if commit:
        conn.commit()
    return isbn


def add_contribution(conn, *, target_type: str, kind: str, payload: dict,
                     target_id: str | None = None, field_name: str | None = None,
                     contributor_hint: str | None = None, user_id: str | None = None) -> int:
    # 规范化 payload（键排序）以便去重
    payload_str = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    dup = conn.execute(
        "SELECT id FROM contributions WHERE status='pending' AND target_type=? "
        "AND IFNULL(target_id,'')=IFNULL(?,'') AND kind=? AND payload=?",
        (target_type, target_id, kind, payload_str),
    ).fetchone()
    if dup:                       # 完全相同的待审贡献，不重复建
        return dup["id"]
    cur = conn.execute(
        "INSERT INTO contributions (status, target_type, target_id, kind, payload, "
        "field_name, contributor_hint, user_id, created_at) VALUES ('pending', ?,?,?,?,?,?,?,?)",
        (target_type, target_id, kind, payload_str, field_name, contributor_hint, user_id, _now()),
    )
    conn.commit()
    return cur.lastrowid


def list_contributions_by_user(conn, username: str, limit: int = 200) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM contributions WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
        (username, limit),
    ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"]) if d.get("payload") else {}
        out.append(d)
    return out


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

    if kind == "add" and etype == "book":
        # 整本新书：create_book 内部已建作者/作品/版本并标来源
        tid = create_book(conn, payload, commit=False)
    elif kind == "add":
        if etype == "edition":
            tid = payload["isbn_13"]
            upsert_edition(conn, tid, payload, commit=False)
        elif etype == "work":
            tid = upsert_work(conn, payload, commit=False)
        elif etype == "author":
            tid = upsert_author(conn, payload, commit=False)
        set_sources(conn, etype, tid, [_FS(f) for f in payload if f in _FIELDS[etype]], commit=False)
        if etype == "edition" and payload.get("work_id"):
            _reindex_work(conn, payload["work_id"])
    else:  # edit
        _upsert_partial(conn, etype, tid, payload)
        set_sources(conn, etype, tid, [_FS(f) for f in payload if f in _FIELDS.get(etype, ())], commit=False)
        if etype == "work" and "subjects" in payload:   # 同步标签
            conn.execute("DELETE FROM work_tags WHERE work_id=?", (tid,))
            add_tags_to_work(conn, tid, payload.get("subjects") or [], commit=False)
        if etype == "work" and "authors" in payload:     # 重设作者（OL 式复用）
            set_work_authors(conn, tid, payload.get("authors") or [], commit=False)
    conn.execute(
        "UPDATE contributions SET status='approved', reviewed_by=?, review_note=?, reviewed_at=?, target_id=? WHERE id=?",
        (reviewer, note, _now(), tid, contrib_id),
    )
    # 众包冲突解决：采纳了某字段的一个提议 → 同目标同字段的其它待审自动驳回
    if c.get("field_name"):
        conn.execute(
            "UPDATE contributions SET status='rejected', reviewed_by=?, "
            "review_note='已采纳其它提议', reviewed_at=? "
            "WHERE status='pending' AND target_type=? AND IFNULL(target_id,'')=IFNULL(?,'') "
            "AND field_name=? AND id<>?",
            (reviewer, _now(), c["target_type"], c["target_id"], c["field_name"], contrib_id),
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
