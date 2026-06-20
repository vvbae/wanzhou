#!/usr/bin/env python3
"""从 OpenLibrary editions dump 直接把大陆中文书（ISBN 9787…）解析进本地库。

系统性来源：OL 每月公开全量 editions dump（~11.6GB .gz）。搜索 API 翻不深、拿不全，
dump 才是把"OL 所有中文书"一网打尽的唯一干净办法。dump 里本来就含 OL 字段，
所以**一次 OL API 都不打**，直接解析进库。Google 之后按配额选择性补。

范围（本脚本）：只收 isbn_13 以 9787 开头（中国大陆出版组）的版本。
作者名取自 by_statement（拼音，译作常有）；纯原创书可能无作者，留给 Google/众包补。

dump 格式：每行 5 列 Tab 分隔 → type, key, revision, last_modified, {edition JSON}

用法：
    # 先下载（一次性 ~11.6GB）
    curl -L -o ol_editions.txt.gz https://openlibrary.org/data/ol_dump_editions_latest.txt.gz
    CNBIB_DB=./data/cnbib.db uv run python parse_dump.py ol_editions.txt.gz

    # 或边下边解析，不落盘那 11.6GB：
    curl -L https://openlibrary.org/data/ol_dump_editions_latest.txt.gz | gunzip | \
        CNBIB_DB=./data/cnbib.db uv run python parse_dump.py -

    uv run python parse_dump.py ol_editions.txt.gz --limit 100000   # 调试：只扫前 N 行
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from typing import Any, Iterator

from cnbib import db
from cnbib.aggregator import derive_original_authors
from cnbib.cleaning import (
    extract_original_title,
    normalize_language,
    parse_by_statement,
    parse_year,
)
from cnbib.sources.base import LIST_FIELDS, SOURCE_FIELDS, empty_record

PREFIX = "9787"           # 大陆出版组
SOURCE = "openlibrary"
CONFIDENCE = 20
COVER = "https://covers.openlibrary.org/b/id/{}-L.jpg"
BATCH = 5000              # 每多少本提交一次


class _FS:
    """轻量 field_source（upsert_book 只用 .field_name/.source/.confidence）。"""
    __slots__ = ("field_name", "source", "confidence")

    def __init__(self, field_name: str):
        self.field_name = field_name
        self.source = SOURCE
        self.confidence = CONFIDENCE


def _str(v: Any) -> str | None:
    return v.strip() if isinstance(v, str) and v.strip() else None


def edition_to_record(ed: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """edition JSON → (isbn_13, record)。非 9787 / 无标题 → None。"""
    isbns = [x for x in (ed.get("isbn_13") or []) if isinstance(x, str)]
    isbn_13 = next((x for x in isbns if x.startswith(PREFIX) and len(x) == 13), None)
    if not isbn_13:
        return None
    title = _str(ed.get("title"))
    if not title:
        return None

    rec = empty_record()
    rec["isbn_10"] = next(iter(ed.get("isbn_10") or []), None)
    rec["title"] = title
    rec["subtitle"] = _str(ed.get("subtitle"))

    by_authors, translators = parse_by_statement(ed.get("by_statement"))
    rec["authors"] = by_authors or None        # dump 里 authors 是 key，名字取自 by_statement
    rec["translators"] = translators or None
    rec["original_title"] = _str(ed.get("translation_of")) or extract_original_title(ed.get("notes"))

    publishers = [p for p in (ed.get("publishers") or []) if isinstance(p, str)]
    rec["publisher"] = ", ".join(publishers) or None
    rec["publish_date"] = _str(ed.get("publish_date"))
    rec["publish_year"] = parse_year(ed.get("publish_date"))

    pc = ed.get("number_of_pages")
    rec["page_count"] = pc if isinstance(pc, int) and pc > 0 else None

    covers = [c for c in (ed.get("covers") or []) if isinstance(c, int) and c > 0]
    rec["cover_url"] = COVER.format(covers[0]) if covers else None

    langs = ed.get("languages") or []
    code = langs[0]["key"].split("/")[-1] if langs and langs[0].get("key") else None
    rec["language"] = normalize_language(code, title) or "zh"   # 9787 默认中文

    series = ed.get("series") or []
    rec["series"] = _str(series[0]) if series else None
    subjects = [s for s in (ed.get("subjects") or []) if isinstance(s, str)]
    rec["subjects"] = subjects or None

    derive_original_authors(rec)
    return isbn_13, rec


def _field_sources(rec: dict[str, Any]) -> list[_FS]:
    out = []
    for f in SOURCE_FIELDS:
        v = rec.get(f)
        if v is None or (isinstance(v, str) and not v.strip()):
            continue
        if f in LIST_FIELDS and not v:
            continue
        out.append(_FS(f))
    return out


def _lines(path: str) -> Iterator[str]:
    if path == "-":
        yield from sys.stdin
    elif path.endswith(".gz"):
        with gzip.open(path, "rt", encoding="utf-8") as f:
            yield from f
    else:
        with open(path, encoding="utf-8") as f:
            yield from f


def run(args) -> None:
    conn = db.connect(args.db)
    db.init_db(conn)
    # 批量导入提速（可重建的种子库，牺牲一点崩溃安全）
    conn.execute("PRAGMA synchronous=OFF")

    scanned = matched = written = bad = 0
    with_author = with_translator = with_cover = 0
    t0 = time.time()

    for line in _lines(args.file):
        scanned += 1
        if args.limit and scanned > args.limit:
            break
        # 第 5 列是 JSON
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 5:
            continue
        try:
            ed = json.loads(parts[4])
        except (ValueError, IndexError):
            bad += 1
            continue
        res = edition_to_record(ed)
        if not res:
            continue
        matched += 1
        isbn_13, rec = res
        db.upsert_book(conn, isbn_13, rec, _field_sources(rec), commit=False)
        written += 1
        if rec.get("authors"):
            with_author += 1
        if rec.get("translators"):
            with_translator += 1
        if rec.get("cover_url"):
            with_cover += 1
        if written % BATCH == 0:
            conn.commit()
            rate = scanned / max(1e-9, time.time() - t0)
            print(f"  扫描 {scanned:,} · 命中9787 {matched:,} · 写入 {written:,} "
                  f"· {rate:,.0f} 行/秒", flush=True)
    conn.commit()

    n = written or 1
    total = conn.execute("SELECT count(*) FROM books").fetchone()[0]
    print("\n" + "=" * 60)
    print(f"完成：扫描 {scanned:,} 行，命中 9787 {matched:,}，写入 {written:,}，坏行 {bad:,}")
    print(f"  有作者(by_statement): {with_author:,} ({100*with_author//n}%)")
    print(f"  有译者:               {with_translator:,} ({100*with_translator//n}%)")
    print(f"  有封面:               {with_cover:,} ({100*with_cover//n}%)")
    print(f"库内现共 {total:,} 本，用时 {time.time()-t0:.0f}s")
    print("=" * 60)


def main() -> None:
    p = argparse.ArgumentParser(description="从 OL editions dump 解析大陆中文书进库")
    p.add_argument("file", help="dump 文件（.gz 或文本）；'-' 读 stdin")
    p.add_argument("--db", default=db.DEFAULT_DB, help="数据库路径（或用 CNBIB_DB）")
    p.add_argument("--limit", type=int, default=0, help="只扫前 N 行（调试）")
    args = p.parse_args()
    if args.db == db.DEFAULT_DB and os.environ.get("CNBIB_DB"):
        args.db = os.environ["CNBIB_DB"]
    run(args)


if __name__ == "__main__":
    main()
