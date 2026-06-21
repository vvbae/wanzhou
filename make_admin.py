#!/usr/bin/env python3
"""创建/提升一个管理员账号（引导首个 admin）。
    CNBIB_DB=./data/library.db uv run python make_admin.py <用户名> <密码>
已存在则提升为 admin。"""
import os
import sys

from cnbib import store

u, pw = sys.argv[1], sys.argv[2]
conn = store.connect(os.environ.get("CNBIB_DB", store.DEFAULT_DB))
store.init_db(conn)
if conn.execute("SELECT 1 FROM users WHERE username=?", (u,)).fetchone():
    store.set_role(conn, u, "admin")
    print(f"已把 {u} 提升为 admin")
else:
    store.create_user(conn, u, pw, role="admin")
    print(f"已创建 admin：{u}")
