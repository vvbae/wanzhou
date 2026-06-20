"""FastAPI 读侧（三层：作者/作品/版本）。薄层，数据在 store.py。

设计 v0.2：搜索只查本地目录，不实时聚合外部。"加书 / 改 / 审核"是写侧（Phase C）。
JSON 端点对外 CC0；页面在 static/。
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse

from cnbib import store

DB_PATH = os.environ.get("CNBIB_DB", store.DEFAULT_DB)
_STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = store.connect(DB_PATH)
    store.init_db(conn)
    conn.close()
    yield


app = FastAPI(
    title="中文开放书目",
    version="0.2.0",
    description="社区共建的开放中文书目数据库。作者/作品/版本三层。数据 CC0。",
    lifespan=lifespan,
)


def get_conn():
    conn = store.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


# ── JSON 端点 ──────────────────────────────────────────────────────
@app.get("/books/{isbn}")
def get_edition(isbn: str, conn=Depends(get_conn)):
    e = store.get_edition(conn, isbn.strip())
    if not e:
        raise HTTPException(404, f"库里没有这本（版本）：{isbn}。可去【加书】添加。")
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
    return {"query": q, "page": page, "page_size": page_size, "total": total, "results": results}


@app.get("/random")
def random_edition(conn=Depends(get_conn)):
    e = store.random_edition(conn)
    if not e:
        raise HTTPException(404, "库里还没有书")
    return e


@app.get("/stats")
def get_stats(conn=Depends(get_conn)):
    return store.stats(conn)


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
