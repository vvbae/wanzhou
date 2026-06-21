#!/usr/bin/env python3
"""Phase D 批量富化：用 Google Books 把拼音标题的版本补成中文（+ 封面/简介）。

懒补(A)由网站后台在被访问时触发；这个脚本是"少量预补(B)"：上线前先把一批
拼音书过一遍 Google，让首页/搜索好看。配额 1000/天，用 --limit 控制。

    GOOGLE_BOOKS_API_KEY=xxx CNBIB_DB=./data/library.db \
        uv run python enrich.py --limit 800
"""
from __future__ import annotations

import argparse
import asyncio
import os

import httpx

from cnbib import store
from cnbib.aggregator import aggregate
from cnbib.sources import GoogleBooksSource, OpenLibrarySource

SOURCES = [OpenLibrarySource(), GoogleBooksSource()]


async def run(args):
    db = os.environ.get("CNBIB_DB", store.DEFAULT_DB)
    if not os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip():
        print("⚠ 没设 GOOGLE_BOOKS_API_KEY —— 只有 OL，补不出中文。先设 key。")
    conn = store.connect(db)
    store.init_db(conn)
    isbns = store.needs_enrichment(conn, args.limit)
    print(f"待富化(拼音标题、未富化)：{len(isbns)} 本")

    enriched = 0
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(headers={"User-Agent": "cn-bib-enrich/0.1"}) as client:
        async def one(isbn):
            async with sem:
                if args.delay:
                    await asyncio.sleep(args.delay)
                try:
                    rec = (await aggregate(isbn, SOURCES, client=client)).record
                    return isbn, rec
                except Exception:  # noqa: BLE001
                    return isbn, None

        done = 0
        for coro in asyncio.as_completed([one(i) for i in isbns]):
            isbn, rec = await coro
            done += 1
            if rec:
                changed = store.apply_enrichment(conn, isbn, rec)
                if "title" in changed:
                    enriched += 1
            else:
                conn.execute("UPDATE editions SET enriched=? WHERE isbn_13=?",
                             (store._now(), isbn))  # 标记试过，避免反复打
                conn.commit()
            if done % 20 == 0 or done == len(isbns):
                print(f"  {done}/{len(isbns)} · 标题转中文 {enriched}", flush=True)
    print(f"完成：标题转中文 {enriched} / 处理 {len(isbns)}")


def main():
    p = argparse.ArgumentParser(description="Google 批量富化拼音标题")
    p.add_argument("--limit", type=int, default=500, help="本次处理多少本（看配额）")
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--delay", type=float, default=0.2)
    asyncio.run(run(p.parse_args()))


if __name__ == "__main__":
    main()
