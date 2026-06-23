#!/usr/bin/env python3
"""回填 works.has_cjk_title：作品标题或任一版本标题含中文 → 1。
主题浏览/排序靠它优先中文。一次性，可重跑。
    CNBIB_DB=./data/library.db uv run python recompute_cjk.py
"""
import os
import time

from cnbib import store
from cnbib.cleaning import has_cjk

conn = store.connect(os.environ.get("CNBIB_DB", store.DEFAULT_DB))
store.init_db(conn)
conn.execute("PRAGMA synchronous=OFF")
t0 = time.time()

cjk = set()
for r in conn.execute("SELECT work_id, title FROM editions WHERE title IS NOT NULL"):
    if r["work_id"] and has_cjk(r["title"]):
        cjk.add(r["work_id"])
for r in conn.execute("SELECT id, title FROM works WHERE title IS NOT NULL"):
    if has_cjk(r["title"]):
        cjk.add(r["id"])

conn.execute("UPDATE works SET has_cjk_title=0")
conn.executemany("UPDATE works SET has_cjk_title=1 WHERE id=?", [(w,) for w in cjk])
conn.commit()
total = conn.execute("SELECT count(*) FROM works").fetchone()[0]
print(f"中文标题作品 {len(cjk):,} / {total:,}（{100*len(cjk)//total}%），用时 {time.time()-t0:.0f}s")
