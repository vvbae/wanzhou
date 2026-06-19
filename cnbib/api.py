"""FastAPI 路由 —— 薄层。业务逻辑在 aggregator / db。

缓存策略：/books/{isbn} 先查本地 SQLite，没有再并发打外部源，结果写回库。
只读端点；/contribute 与 UI 是 Phase 2/3，这里不做。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse

from cnbib import db
from cnbib.aggregator import aggregate, romanized_fields
from cnbib.isbn import normalize
from cnbib.schema import (
    BookResponse,
    ContributeRequest,
    ContributeResponse,
    SearchHit,
    SearchResponse,
    StatsResponse,
)
from cnbib.sources import GoogleBooksSource, OpenLibrarySource

DB_PATH = os.environ.get("CNBIB_DB", db.DEFAULT_DB)
_STATIC = Path(__file__).parent / "static"

# 源实例（无状态，可复用）。新增源只在此追加。
SOURCES = [OpenLibrarySource(), GoogleBooksSource()]


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect(DB_PATH)
    db.init_db(conn)
    conn.close()
    yield


app = FastAPI(
    title="中文开放书目 API",
    version="0.1.0",
    description="给 ISBN，返回干净的中文书元数据。数据 CC0。",
    lifespan=lifespan,
)


def get_conn():
    conn = db.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def _to_book_response(book: dict) -> BookResponse:
    resp = BookResponse.model_validate(book)
    resp.needs_chinese = romanized_fields(book)
    return resp


@app.get("/books/random", response_model=BookResponse)
def get_random(conn=Depends(get_conn)):
    book = db.random_book(conn)
    if not book:
        raise HTTPException(404, "库里还没有任何书")
    return _to_book_response(book)


@app.get("/books/{isbn}", response_model=BookResponse)
async def get_book(isbn: str, conn=Depends(get_conn)):
    isbn_13 = normalize(isbn)
    if not isbn_13:
        raise HTTPException(400, f"不是合法的 ISBN：{isbn}")

    # 1) 先查本地库
    book = db.get_book(conn, isbn_13)
    if book:
        return _to_book_response(book)

    # 2) 库里没有 → 并发打外部源聚合
    result = await aggregate(isbn_13, SOURCES)
    if not result.record.get("title"):
        raise HTTPException(404, f"各数据源都查不到这本书：{isbn_13}")

    # 3) 写回库，再读出来（带上 sources）
    db.upsert_book(conn, isbn_13, result.record, result.field_sources)
    book = db.get_book(conn, isbn_13)
    return _to_book_response(book)


@app.get("/search", response_model=SearchResponse)
def search(
    q: str = Query(..., min_length=1, description="书名 / 作者 / 出版社关键词"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    conn=Depends(get_conn),
):
    total, rows = db.search(conn, q, page, page_size)
    hits = [
        SearchHit(
            isbn_13=r["isbn_13"],
            title=r.get("title"),
            authors=r.get("authors") or [],
            publisher=r.get("publisher"),
            publish_year=r.get("publish_year"),
            cover_url=r.get("cover_url"),
        )
        for r in rows
    ]
    return SearchResponse(
        query=q, page=page, page_size=page_size, total=total, results=hits
    )


@app.post("/contribute", response_model=ContributeResponse)
async def contribute(req: ContributeRequest, request: Request, conn=Depends(get_conn)):
    isbn_13 = normalize(req.isbn)
    if not isbn_13:
        raise HTTPException(400, f"不是合法的 ISBN：{req.isbn}")
    if not req.fields:
        raise HTTPException(400, "没有要提交的字段")

    # 库里没有这本书 → 先聚合外部源建底，让贡献有对照（old_value）
    if db.get_book(conn, isbn_13) is None:
        result = await aggregate(isbn_13, SOURCES)
        if result.record.get("title"):
            db.upsert_book(conn, isbn_13, result.record, result.field_sources)

    # contributor 标识：优先用提交的匿名标识，否则用请求 IP（不做登录）
    hint = (req.contributor_hint or "").strip() or (
        request.client.host if request.client else None
    )
    try:
        applied = db.apply_contribution(conn, isbn_13, dict(req.fields), hint)
    except ValueError as e:
        raise HTTPException(400, str(e))

    book = db.get_book(conn, isbn_13)
    if not book:
        raise HTTPException(404, f"无法创建记录：{isbn_13}")
    return ContributeResponse(
        isbn_13=isbn_13, applied=applied, book=_to_book_response(book)
    )


@app.get("/stats", response_model=StatsResponse)
def get_stats(conn=Depends(get_conn)):
    return StatsResponse(**db.stats(conn))


@app.get("/", include_in_schema=False)
def home_page():
    """首页 / 搜索页。"""
    return FileResponse(_STATIC / "search.html")


@app.get("/book", include_in_schema=False)
def book_page():
    """单本详情页（前端用 ?isbn= 拉 /books/{isbn}，展示每字段来源）。"""
    return FileResponse(_STATIC / "book.html")


@app.get("/edit", include_in_schema=False)
def edit_page():
    """极简贡献网页（手机可用，支持 ?isbn= 自动预填）。"""
    return FileResponse(_STATIC / "index.html")
