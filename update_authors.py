#!/usr/bin/env python3
"""只扫 authors dump，给库里已有作者补生卒年（birth_date/death_date）。

不动 editions/works，几分钟。用于加了 author 生卒年字段后回填。
    CNBIB_DB=./data/library.db uv run python update_authors.py /path/ol_authors.txt.gz
"""
from __future__ import annotations

import gzip
import json
import os
import sys
import time

from cnbib import store


def _lines(path):
    op = gzip.open(path, "rt", encoding="utf-8") if path.endswith(".gz") else open(path, encoding="utf-8")
    with op as f:
        yield from f


def main():
    path = sys.argv[1]
    db = os.environ.get("CNBIB_DB", store.DEFAULT_DB)
    conn = store.connect(db)
    store.init_db(conn)
    conn.execute("PRAGMA synchronous=OFF")

    have = {r[0] for r in conn.execute("SELECT id FROM authors")}
    print(f"库里作者 {len(have):,}，扫 authors dump 补生卒年…")
    scanned = updated = 0
    t0 = time.time()
    for line in _lines(path):
        scanned += 1
        parts = line.rstrip("\n").split("\t")
        if len(parts) < 5:
            continue
        try:
            au = json.loads(parts[4])
        except ValueError:
            continue
        key = au.get("key")
        if key not in have:
            continue
        b, d = au.get("birth_date"), au.get("death_date")
        if not b and not d:
            continue
        conn.execute("UPDATE authors SET birth_date=?, death_date=? WHERE id=?",
                     (b, d, key))
        updated += 1
        if updated % 5000 == 0:
            conn.commit()
            print(f"  扫 {scanned:,} 更新 {updated:,}", flush=True)
    conn.commit()
    print(f"完成：扫 {scanned:,}，补生卒年 {updated:,}，用时 {time.time()-t0:.0f}s")


if __name__ == "__main__":
    main()
