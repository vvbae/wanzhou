"""FastAPI 读侧（三层：作者/作品/版本）。薄层，数据在 store.py。

设计 v0.2：搜索只查本地目录，不实时聚合外部。"加书 / 改 / 审核"是写侧（Phase C）。
JSON 端点对外 CC0；页面在 static/。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request, Response,
)
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from cnbib import store
from cnbib.aggregator import aggregate
from cnbib.cleaning import has_cjk
from cnbib.isbn import normalize
from cnbib.sources import GoogleBooksSource, OpenLibrarySource

# 加书源预填 + 懒富化用：实时查 OpenLibrary + Google Books
_LOOKUP_SOURCES = [OpenLibrarySource(), GoogleBooksSource()]


async def _lazy_enrich(isbn: str):
    """懒富化(A)：被访问的拼音书，后台查 Google 补成中文，下次访问就好了。"""
    try:
        rec = (await aggregate(isbn, _LOOKUP_SOURCES)).record
        conn = store.connect(DB_PATH)
        try:
            store.apply_enrichment(conn, isbn, rec)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — 后台尽力而为，失败不影响请求
        pass

DB_PATH = os.environ.get("CNBIB_DB", store.DEFAULT_DB)
# admin 审核口令（环境变量；不设则审核接口一律拒绝——不是用户系统，就一个密钥）
ADMIN_TOKEN = os.environ.get("CNBIB_ADMIN_TOKEN", "").strip()
_STATIC = Path(__file__).parent / "static"


def get_conn():
    conn = store.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def current_user(request: Request, conn=Depends(get_conn)) -> dict | None:
    return store.get_session_user(conn, request.cookies.get("sid"))


def require_reviewer(request: Request, x_admin_token: str = Header(default=""),
                     conn=Depends(get_conn)) -> dict:
    """审核权限：登录且角色 reviewer/admin；或带 bootstrap 口令（建首个 admin 用）。"""
    if ADMIN_TOKEN and x_admin_token == ADMIN_TOKEN:
        return {"username": "admin", "role": "admin"}
    u = store.get_session_user(conn, request.cookies.get("sid"))
    if u and u["role"] in ("reviewer", "admin"):
        return u
    raise HTTPException(403, "需要审核员登录")


class Creds(BaseModel):
    username: str
    password: str


class ContributionIn(BaseModel):
    target_type: str            # book / work / edition / author
    kind: str                   # add / edit
    payload: dict
    target_id: str | None = None
    contributor_hint: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = store.connect(DB_PATH)
    store.init_db(conn)
    conn.close()
    yield


app = FastAPI(
    title="万轴 · 中文开放图书馆",
    version="0.2.0",
    description="社区共建的开放中文书目数据库。作者/作品/版本三层。数据 CC0。",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")


# ── JSON 端点 ──────────────────────────────────────────────────────
@app.get("/books/{isbn}")
def get_edition(isbn: str, background: BackgroundTasks, conn=Depends(get_conn)):
    e = store.get_edition(conn, isbn.strip())
    if not e:
        raise HTTPException(404, f"库里没有这本（版本）：{isbn}。可去【加书】添加。")
    # 懒富化：标题还是拼音、且没富化过 → 后台查 Google 补中文（本次先返回现状）
    if not e.get("enriched") and not has_cjk(e.get("title")):
        background.add_task(_lazy_enrich, e["isbn_13"])
    return e


# 用查询参数（作品/作者 id 是 OL key，含斜杠，不能做路径段）
@app.get("/works")
def get_work(id: str = Query(...), conn=Depends(get_conn)):
    w = store.get_work(conn, id)
    if not w:
        raise HTTPException(404, f"没有这个作品：{id}")
    return w


@app.get("/authors")
def get_author(id: str = Query(...), conn=Depends(get_conn)):
    a = store.get_author(conn, id)
    if not a:
        raise HTTPException(404, f"没有这个作者：{id}")
    return a


@app.get("/search")
def search(
    q: str = Query(..., min_length=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    conn=Depends(get_conn),
):
    total, results = store.search(conn, q, page, page_size)
    authors = store.search_authors(conn, q) if page == 1 else []
    return {"query": q, "page": page, "page_size": page_size, "total": total,
            "results": results, "authors": authors}


@app.get("/authors_search")
def authors_search(q: str = Query(..., min_length=1), conn=Depends(get_conn)):
    return {"results": store.search_authors(conn, q)}


@app.get("/tags")
def tags_autocomplete(q: str = Query(..., min_length=1), conn=Depends(get_conn)):
    return {"results": store.search_tags(conn, q)}


@app.get("/top_tags")
def top_tags(n: int = Query(12, ge=1, le=40), conn=Depends(get_conn)):
    return {"results": store.top_tags(conn, n)}


@app.get("/tag_works")
def tag_works(tag: str = Query(...), page: int = Query(1, ge=1),
              page_size: int = Query(20, ge=1, le=100), conn=Depends(get_conn)):
    total, name, results = store.works_by_tag(conn, tag, page, page_size)
    return {"tag": tag, "name": name, "total": total, "results": results}


@app.get("/random")
def random_edition(conn=Depends(get_conn)):
    e = store.random_edition(conn)
    if not e:
        raise HTTPException(404, "库里还没有书")
    return e


@app.get("/random_books")
def random_books(n: int = Query(8, ge=1, le=24), conn=Depends(get_conn)):
    return {"results": store.random_showcase(conn, n)}


@app.get("/stats")
def get_stats(conn=Depends(get_conn)):
    return store.stats(conn)


# ── 加书源预填：实时查外部源，不写库（搜不到才让用户手填）──────────
@app.get("/lookup/{isbn}")
async def lookup(isbn: str):
    isbn13 = normalize(isbn)
    if not isbn13:
        raise HTTPException(400, f"不是合法 ISBN：{isbn}")
    r = (await aggregate(isbn13, _LOOKUP_SOURCES)).record
    if not r.get("title"):
        return {"found": False, "isbn_13": isbn13}
    return {
        "found": True, "isbn_13": isbn13, "title": r.get("title"),
        "authors": r.get("authors") or [], "translators": r.get("translators") or [],
        "title_original": r.get("original_title"), "publisher": r.get("publisher"),
        "publish_year": r.get("publish_year"), "description": r.get("description"),
        "subjects": r.get("subjects") or [], "cover_url": r.get("cover_url"),
        "language": r.get("language"),
    }


# ── 账号 / 会话 ────────────────────────────────────────────────────
_COOKIE = dict(httponly=True, samesite="lax", max_age=60 * 60 * 24 * 30)


@app.post("/auth/register")
def register(c: Creds, response: Response, conn=Depends(get_conn)):
    try:
        store.create_user(conn, c.username, c.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    response.set_cookie("sid", store.create_session(conn, c.username.strip()), **_COOKIE)
    return {"username": c.username.strip(), "role": "user"}


@app.post("/auth/login")
def login(c: Creds, response: Response, conn=Depends(get_conn)):
    u = store.verify_credentials(conn, c.username, c.password)
    if not u:
        raise HTTPException(401, "用户名或密码错误")
    response.set_cookie("sid", store.create_session(conn, u["username"]), **_COOKIE)
    return u


@app.post("/auth/logout")
def logout(request: Request, response: Response, conn=Depends(get_conn)):
    store.delete_session(conn, request.cookies.get("sid"))
    response.delete_cookie("sid")
    return {"ok": True}


@app.get("/auth/me")
def whoami(user=Depends(current_user)):
    return user or {}


# ── 写侧：贡献（进待审）+ 审核 ─────────────────────────────────────
@app.post("/contribute")
def contribute(c: ContributionIn, request: Request, conn=Depends(get_conn)):
    if c.kind not in ("add", "edit"):
        raise HTTPException(400, "kind 只能是 add / edit")
    if c.target_type not in ("book", "work", "edition", "author"):
        raise HTTPException(400, "target_type 非法")
    if not c.payload:
        raise HTTPException(400, "payload 为空")
    if c.kind == "add" and c.target_type == "book":
        isbn = normalize(str(c.payload.get("isbn_13", "")))
        if not isbn:
            raise HTTPException(400, "加书需要合法 ISBN")
        if store.get_edition(conn, isbn):       # 查重：已收录 → 走纠错，别重复加
            raise HTTPException(409, "这本书已收录，请在它的页面用『补全·纠错』")
        c.payload["isbn_13"] = isbn
    if c.kind == "edit" and not c.target_id:
        raise HTTPException(400, "改字段需要 target_id")
    user = store.get_session_user(conn, request.cookies.get("sid"))
    uid = user["username"] if user else None
    hint = (c.contributor_hint or "").strip() or (request.client.host if request.client else None)
    if c.kind == "edit":
        # 按字段拆条：同一字段的多个提议天然成组，便于冲突解决
        ids = [store.add_contribution(conn, target_type=c.target_type, kind="edit",
               payload={f: v}, target_id=c.target_id, field_name=f,
               contributor_hint=hint, user_id=uid) for f, v in c.payload.items()]
    else:
        ids = [store.add_contribution(conn, target_type=c.target_type, kind="add",
               payload=c.payload, target_id=c.target_id, contributor_hint=hint, user_id=uid)]
    return {"id": ids[0] if ids else None, "ids": ids, "status": "pending",
            "message": "已提交，等审核"}


@app.get("/admin/contributions")
def admin_list(status: str = "pending", reviewer=Depends(require_reviewer), conn=Depends(get_conn)):
    return store.list_contributions(conn, status)


@app.post("/admin/contributions/{cid}/approve")
def admin_approve(cid: int, reviewer=Depends(require_reviewer), conn=Depends(get_conn)):
    if not store.approve_contribution(conn, cid, reviewer=reviewer["username"]):
        raise HTTPException(404, "没有这条待审贡献")
    return {"ok": True}


@app.post("/admin/contributions/{cid}/reject")
def admin_reject(cid: int, note: str = "", reviewer=Depends(require_reviewer), conn=Depends(get_conn)):
    if not store.reject_contribution(conn, cid, reviewer=reviewer["username"], note=note):
        raise HTTPException(404, "没有这条待审贡献")
    return {"ok": True}


# ── 页面 ──────────────────────────────────────────────────────────
def _page(name: str) -> FileResponse:
    return FileResponse(_STATIC / name)


@app.get("/", include_in_schema=False)
def home():
    return _page("search.html")


@app.get("/work", include_in_schema=False)
def work_page():
    return _page("work.html")


@app.get("/book", include_in_schema=False)
def book_page():
    return _page("book.html")


@app.get("/author", include_in_schema=False)
def author_page():
    return _page("author.html")


@app.get("/add", include_in_schema=False)
def add_page():
    return _page("add.html")


@app.get("/edit", include_in_schema=False)
def edit_page():
    return _page("edit.html")


@app.get("/admin", include_in_schema=False)
def admin_page():
    return _page("admin.html")


@app.get("/login", include_in_schema=False)
def login_page():
    return _page("login.html")


@app.get("/tag", include_in_schema=False)
def tag_page():
    return _page("tag.html")


@app.get("/guide", include_in_schema=False)
def guide_page():
    return _page("guide.html")


@app.get("/about", include_in_schema=False)
def about_page():
    return _page("about.html")


@app.get("/contact", include_in_schema=False)
def contact_page():
    return _page("contact.html")


@app.get("/privacy", include_in_schema=False)
def privacy_page():
    return _page("privacy.html")
