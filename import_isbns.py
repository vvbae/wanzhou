#!/usr/bin/env python3
"""批量导入：读一个 ISBN 列表，逐个走现有聚合管线，灌进 SQLite。

纯拉数据——译名/拼音清洗交给后续众包，这里不管，源给什么存什么
（aggregate 已做基础清洗：译者拆分、年份、语言、原作名）。

特性：
- 断点续跑：已在库里的 ISBN 默认跳过（--force 强制重查）
- 限速：低并发 + 每请求小延迟，别把外部 API 打急（Google 免费 ~1000/天）
- 报告：新增/跳过/查无/出错 计数 + 字段覆盖率 + 拼音待补比例
- 备份：跑完用 SQLite .backup 写一份带时间戳的快照（--no-backup 关）

用法：
    uv run python import_isbns.py isbns.txt
    uv run python import_isbns.py isbns.txt --concurrency 3 --limit 200
    cat isbns.txt | uv run python import_isbns.py -            # 从 stdin 读
    CNBIB_DB=./data/cnbib.db GOOGLE_BOOKS_API_KEY=xxx uv run python import_isbns.py isbns.txt
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import sys
from datetime import datetime, timezone

import httpx

from cnbib import db
from cnbib.aggregator import aggregate, romanized_fields
from cnbib.isbn import normalize
from cnbib.sources import GoogleBooksSource, OpenLibrarySource
from cnbib.sources.base import LIST_FIELDS, SOURCE_FIELDS

SOURCES = [OpenLibrarySource(), GoogleBooksSource()]
# 报告里关注的字段
REPORT_FIELDS = [
    "title", "authors", "translators", "original_title", "original_authors",
    "publisher", "publish_year", "description", "cover_url", "language", "subjects",
]


def read_isbns(path: str) -> list[str]:
    """读 ISBN 列表：一行一个，# 注释和空行跳过。path='-' 读 stdin。"""
    f = sys.stdin if path == "-" else open(path, encoding="utf-8")
    try:
        out = []
        for line in f:
            s = line.strip()
            if s and not s.startswith("#"):
                out.append(s)
        return out
    finally:
        if f is not sys.stdin:
            f.close()


def _has(field: str, v) -> bool:
    if v is None:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    if field in LIST_FIELDS and isinstance(v, (list, tuple)) and not v:
        return False
    return True


def _normalize_list(raw: list[str]) -> tuple[list[str], int]:
    seen, valid, invalid = set(), [], 0
    for r in raw:
        n = normalize(r)
        if n is None:
            invalid += 1
        elif n not in seen:
            seen.add(n)
            valid.append(n)
    return valid, invalid


async def run_via_api(args) -> None:
    """远程模式：挨个 GET {api}/books/{isbn}，让线上服务器自己聚合+写库。

    用线上的 Google key、写线上的卷，本机不碰数据库。适合直接把公共实例喂饱。
    """
    valid, invalid = _normalize_list(read_isbns(args.file))
    todo = valid[: args.limit] if args.limit else valid
    base = args.api.rstrip("/")
    print(f"远程灌库 → {base}")
    print(f"合法去重 {len(valid)}，非法 {invalid}，本次打 {len(todo)} 本")

    tally = {"added": 0, "notfound": 0, "error": 0}
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=40) as client:
        async def one(isbn: str):
            async with sem:
                if args.delay:
                    await asyncio.sleep(args.delay)
                try:
                    r = await client.get(f"{base}/books/{isbn}")
                    return r.status_code
                except httpx.HTTPError:
                    return None

        done = 0
        for coro in asyncio.as_completed([one(i) for i in todo]):
            code = await coro
            done += 1
            if code == 200:
                tally["added"] += 1
            elif code == 404:
                tally["notfound"] += 1
            else:
                tally["error"] += 1
            if done % 20 == 0 or done == len(todo):
                print(f"  进度 {done}/{len(todo)} · 入库 {tally['added']} · "
                      f"查无 {tally['notfound']} · 出错 {tally['error']}", flush=True)
    print(f"\n完成：入库 {tally['added']} · 查无 {tally['notfound']} · 出错 {tally['error']}")
    try:
        s = (await httpx.AsyncClient().get(f"{base}/stats")).json()
        print(f"线上现共 {s['total_books']} 本")
    except Exception:  # noqa: BLE001
        pass


async def run(args) -> None:
    raw = read_isbns(args.file)
    valid, invalid = _normalize_list(raw)

    conn = db.connect(args.db)
    db.init_db(conn)
    existing = {row[0] for row in conn.execute("SELECT isbn_13 FROM books")}
    todo = valid if args.force else [i for i in valid if i not in existing]
    if args.limit:
        todo = todo[: args.limit]

    print(f"输入 {len(raw)} 行 → 合法去重 {len(valid)}，非法 {invalid}")
    print(f"库里已有 {len(existing)}，本次要查 {len(todo)} 本"
          f"（Google key: {'有' if GoogleBooksSource and _google_key() else '无→只 OL'}）")
    if not todo:
        print("没有要查的，结束。")
        return

    tally = {"added": 0, "notfound": 0, "error": 0}
    cover = {f: 0 for f in REPORT_FIELDS}
    pinyin = {}
    sem = asyncio.Semaphore(args.concurrency)

    async with httpx.AsyncClient(headers={"User-Agent": "cn-bib-importer/0.1"}) as client:
        async def fetch_one(isbn: str):
            async with sem:
                if args.delay:
                    await asyncio.sleep(args.delay)
                try:
                    return isbn, await aggregate(isbn, SOURCES, client=client), None
                except Exception as e:  # noqa: BLE001
                    return isbn, None, repr(e)

        done = 0
        for coro in asyncio.as_completed([fetch_one(i) for i in todo]):
            isbn, result, err = await coro
            done += 1
            if err:
                tally["error"] += 1
            elif result and result.record.get("title"):
                db.upsert_book(conn, isbn, result.record, result.field_sources)
                tally["added"] += 1
                for f in REPORT_FIELDS:
                    if _has(f, result.record.get(f)):
                        cover[f] += 1
                for f in romanized_fields(result.record):
                    pinyin[f] = pinyin.get(f, 0) + 1
            else:
                tally["notfound"] += 1
            if done % 20 == 0 or done == len(todo):
                print(f"  进度 {done}/{len(todo)} · 新增 {tally['added']} · "
                      f"查无 {tally['notfound']} · 出错 {tally['error']}", flush=True)

    _report(tally, cover, pinyin, conn, args)


def _report(tally, cover, pinyin, conn, args) -> None:
    n = tally["added"]
    print("\n" + "=" * 60)
    print(f"导入完成：新增 {n} · 查无 {tally['notfound']} · 出错 {tally['error']}")
    if n:
        print(f"\n字段覆盖率（{n} 本新增里有值的比例）：")
        for f in REPORT_FIELDS:
            print(f"  {f:16} {cover[f]:>4}/{n}  {100*cover[f]//n:>3}%")
        if pinyin:
            print("\n拼音/拉丁待补（needs_chinese，交众包）：")
            for f, c in sorted(pinyin.items(), key=lambda x: -x[1]):
                print(f"  {f:16} {c:>4}/{n}  {100*c//n:>3}%")
    total = conn.execute("SELECT count(*) FROM books").fetchone()[0]
    print(f"\n库内现共 {total} 本")

    if not args.no_backup and n:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        path = f"{args.db}.bak-{ts}"
        dest = sqlite3.connect(path)
        conn.backup(dest)
        dest.close()
        print(f"备份已写 → {path}")
    print("=" * 60)


def _google_key() -> bool:
    import os
    return bool(os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip())


def main() -> None:
    p = argparse.ArgumentParser(description="批量导入 ISBN 到本地书目库")
    p.add_argument("file", help="ISBN 列表文件（一行一个，# 注释）；'-' 读 stdin")
    p.add_argument("--db", default=db.DEFAULT_DB, help="数据库路径（或用 CNBIB_DB 环境变量）")
    p.add_argument("--concurrency", type=int, default=3, help="并发数（默认 3，别太高）")
    p.add_argument("--delay", type=float, default=0.2, help="每请求前的延迟秒数（礼貌限速）")
    p.add_argument("--limit", type=int, default=0, help="最多查多少本（0=不限，调试用）")
    p.add_argument("--force", action="store_true", help="已在库里的也重查覆盖")
    p.add_argument("--no-backup", action="store_true", help="跑完不写备份快照")
    p.add_argument("--api", default="", help="远程模式：打这个 URL 的 /books/{isbn}，"
                   "让线上服务器自己聚合写库（如 https://wanzhou.fly.dev）")
    args = p.parse_args()

    import os
    if args.db == db.DEFAULT_DB and os.environ.get("CNBIB_DB"):
        args.db = os.environ["CNBIB_DB"]

    asyncio.run(run_via_api(args) if args.api else run(args))


if __name__ == "__main__":
    main()
