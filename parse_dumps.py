#!/usr/bin/env python3
"""把 OpenLibrary 三个 dump（editions + works + authors）解析成三层库。

范围：版本 ISBN 以 9787 开头（大陆）。零 OL API——dump 自带 work/author 关系，
归并直接继承。三遍流式：
  1) editions：过滤 9787，写版本，记下需要哪些 work。
  2) works：只取被引用的 work，写作品 + 作者链接，记下需要哪些 author。
  3) authors：只取被引用的 author，写作者（名/简介）。
最后统一重建 FTS。

用法：
    uv run python parse_dumps.py --editions ed.gz --works wk.gz --authors au.gz \
        --db ./data/cnbib.db
    # 调试只扫前 N 行 editions：--limit 100000
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from typing import Any, Iterator

from cnbib import store
from cnbib.cleaning import (
    extract_original_title,
    normalize_language,
    parse_by_statement,
    parse_year,
)

PREFIX = "9787"
SOURCE, CONF = "openlibrary", 20
COVER = "https://covers.openlibrary.org/b/id/{}-L.jpg"
BATCH = 5000


def _lines(path: str) -> Iterator[str]:
    if path == "-":
        yield from sys.stdin
    elif path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            yield from f
    else:
        with open(path, encoding="utf-8") as f:
            yield from f


def _json5(line: str) -> dict | None:
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 5:
        return None
    try:
        return json.loads(parts[4])
    except ValueError:
        return None


def _s(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def _text(v: Any) -> str | None:
    if isinstance(v, dict):
        v = v.get("value")
    return _s(v)


def _src(fields: list[str]):
    return [store._FS(f, SOURCE, CONF) for f in fields]


# ── Pass 1: editions ──────────────────────────────────────────────
def pass_editions(conn, path, limit):
    needed_works: set[str] = set()
    fallback_title: dict[str, str] = {}
    work_orig: dict[str, str] = {}
    scanned = written = 0
    t0 = time.time()
    for line in _lines(path):
        scanned += 1
        if limit and scanned > limit:
            break
        ed = _json5(line)
        if not ed:
            continue
        isbns = [x for x in (ed.get("isbn_13") or []) if isinstance(x, str)]
        isbn = next((x for x in isbns if x.startswith(PREFIX) and len(x) == 13), None)
        title = _s(ed.get("title"))
        if not isbn or not title:
            continue

        wkey = (ed.get("works") or [{}])[0].get("key") or f"wisbn_{isbn}"
        needed_works.add(wkey)
        fallback_title.setdefault(wkey, title)
        orig = _s(ed.get("translation_of")) or extract_original_title(ed.get("notes"))
        if orig:
            work_orig.setdefault(wkey, orig)

        _, translators = parse_by_statement(ed.get("by_statement"))
        covers = [c for c in (ed.get("covers") or []) if isinstance(c, int) and c > 0]
        langs = ed.get("languages") or []
        code = langs[0]["key"].split("/")[-1] if langs and langs[0].get("key") else None
        pc = ed.get("number_of_pages")
        rec = {
            "work_id": wkey,
            "title": title,                # 版本自己的标题（中文常在这里，别丢）
            "isbn_10": next(iter(ed.get("isbn_10") or []), None),
            "subtitle": _s(ed.get("subtitle")),
            "translators": translators or None,
            "publisher": ", ".join(p for p in (ed.get("publishers") or []) if isinstance(p, str)) or None,
            "publish_date": _s(ed.get("publish_date")),
            "publish_year": parse_year(ed.get("publish_date")),
            "cover_url": COVER.format(covers[0]) if covers else None,
            "page_count": pc if isinstance(pc, int) and pc > 0 else None,
            "language": normalize_language(code, title) or "zh",
            "series": _s((ed.get("series") or [None])[0]),
            "format": _s(ed.get("physical_format")),
            "ol_key": ed.get("key"),
        }
        store.upsert_edition(conn, isbn, rec, commit=False)
        store.set_sources(conn, "edition", isbn,
                          _src([k for k, v in rec.items() if v not in (None, [], "")]), commit=False)
        written += 1
        if written % BATCH == 0:
            conn.commit()
            print(f"  [editions] 扫 {scanned:,} 写 {written:,} "
                  f"({scanned/max(1e-9,time.time()-t0):,.0f} 行/秒)", flush=True)
    conn.commit()
    print(f"  [editions] 完成：扫 {scanned:,}，写版本 {written:,}，需作品 {len(needed_works):,}")
    return needed_works, fallback_title, work_orig


# ── Pass 2: works ─────────────────────────────────────────────────
def pass_works(conn, path, needed, fallback_title, work_orig):
    seen: set[str] = set()
    needed_authors: set[str] = set()
    scanned = written = 0
    for line in _lines(path):
        scanned += 1
        wk = _json5(line)
        if not wk:
            continue
        key = wk.get("key")
        if key not in needed:
            continue
        akeys = [a.get("author", {}).get("key") for a in wk.get("authors", []) if a.get("author")]
        akeys = [k for k in akeys if k]
        rec = {
            "title": _s(wk.get("title")) or fallback_title.get(key),
            "title_original": work_orig.get(key),
            "description": _text(wk.get("description")),
            "subjects": [s for s in (wk.get("subjects") or []) if isinstance(s, str)] or None,
            "first_publish_year": parse_year(wk.get("first_publish_date")),
            "ol_key": key,
        }
        store.upsert_work(conn, rec, id=key, author_ids=akeys, commit=False, reindex=False)
        store.set_sources(conn, "work", key,
                          _src([k for k, v in rec.items() if v not in (None, [], "")]), commit=False)
        needed_authors.update(akeys)
        seen.add(key)
        written += 1
        if written % BATCH == 0:
            conn.commit()
    conn.commit()

    # 没出现在 works dump 里的（含我们生成的 wisbn_）→ 用版本标题补一条最小作品
    missing = needed - seen
    for key in missing:
        rec = {"title": fallback_title.get(key), "title_original": work_orig.get(key), "ol_key": key}
        store.upsert_work(conn, rec, id=key, author_ids=[], commit=False, reindex=False)
        store.set_sources(conn, "work", key,
                          _src([k for k, v in rec.items() if v not in (None, [], "")]), commit=False)
    conn.commit()
    print(f"  [works] 命中 {written:,}，补建 {len(missing):,}，需作者 {len(needed_authors):,}")
    return needed_authors


# ── Pass 3: authors ───────────────────────────────────────────────
def pass_authors(conn, path, needed):
    scanned = written = 0
    for line in _lines(path):
        scanned += 1
        au = _json5(line)
        if not au:
            continue
        key = au.get("key")
        if key not in needed:
            continue
        rec = {
            "name": _s(au.get("name")) or _s(au.get("personal_name")),
            "bio": _text(au.get("bio")),
            "ol_key": key,
        }
        store.upsert_author(conn, rec, id=key, commit=False)
        store.set_sources(conn, "author", key,
                          _src([k for k, v in rec.items() if v not in (None, [], "")]), commit=False)
        written += 1
        if written % BATCH == 0:
            conn.commit()
    conn.commit()
    print(f"  [authors] 写作者 {written:,}")
    return written


def main() -> None:
    p = argparse.ArgumentParser(description="解析 OL 三 dump 进三层库（大陆 9787）")
    p.add_argument("--editions", required=True)
    p.add_argument("--works", required=True)
    p.add_argument("--authors", required=True)
    p.add_argument("--db", default=store.DEFAULT_DB)
    p.add_argument("--limit", type=int, default=0, help="只扫前 N 行 editions（调试）")
    args = p.parse_args()
    if args.db == store.DEFAULT_DB and os.environ.get("CNBIB_DB"):
        args.db = os.environ["CNBIB_DB"]

    conn = store.connect(args.db)
    store.init_db(conn)
    conn.execute("PRAGMA synchronous=OFF")
    t0 = time.time()

    print("Pass 1/3 editions …")
    needed, fb_title, w_orig = pass_editions(conn, args.editions, args.limit)
    print("Pass 2/3 works …")
    needed_authors = pass_works(conn, args.works, needed, fb_title, w_orig)
    print("Pass 3/3 authors …")
    pass_authors(conn, args.authors, needed_authors)
    print("重建 FTS …")
    store.rebuild_fts(conn)

    s = store.stats(conn)
    print("\n" + "=" * 56)
    print(f"完成：作者 {s['authors']:,} · 作品 {s['works']:,} · 版本 {s['editions']:,}")
    print(f"用时 {time.time()-t0:.0f}s")
    print("=" * 56)


if __name__ == "__main__":
    main()
