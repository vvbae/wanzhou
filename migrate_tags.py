#!/usr/bin/env python3
"""把 works.subjects 迁移成 tags + work_tags（可按标签浏览）。一次性，可重跑。
    CNBIB_DB=./data/library.db uv run python migrate_tags.py
"""
import os
import time

from cnbib import store

conn = store.connect(os.environ.get("CNBIB_DB", store.DEFAULT_DB))
store.init_db(conn)
t0 = time.time()
n = store.build_tags_from_subjects(conn)
tags = conn.execute("SELECT count(*) FROM tags").fetchone()[0]
links = conn.execute("SELECT count(*) FROM work_tags").fetchone()[0]
print(f"处理作品 {n:,} · 去重标签 {tags:,} · 标签关联 {links:,} · 用时 {time.time()-t0:.0f}s")
