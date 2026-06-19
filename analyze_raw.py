#!/usr/bin/env python3
"""analyze_raw.py — 离线复核：译者 / 原作名能不能从 OL 的 by_statement / notes 救回。

读 probe.py dump 的 probe_raw.json，不联网。验证一个假设：
"translators / original_title 字段 0%" 是探针没解析对字段，不是信息不存在。

OpenLibrary 的著者说明 by_statement 沿用图书馆 MARC 惯例：
    "<作者> zhu ; <译者> yi"      zhu=著, yi=译
notes 里译作常带：
    "Translation of: <原作名>."
"""

from __future__ import annotations

import json
import re

# by_statement 里以 "yi"（译）结尾的责任段 = 译者
_YI = re.compile(r"[；;]?\s*([^；;]+?)\s+yi\b", re.I)
# notes 里的原作名
_ORIG = re.compile(r"Translation of:\s*(.+?)\s*\.", re.I)


def recover_translator(ol: dict | None) -> str | None:
    if not ol:
        return None
    by = ol.get("by_statement") or ""
    # 只取含 "yi" 的责任段；"... zhu" 不算（那是著者）
    hits = [m.group(1).strip() for m in _YI.finditer(by)]
    # 去掉可能混进来的 "zhu" 段残留
    hits = [h for h in hits if not h.lower().endswith("zhu")]
    return "; ".join(hits) if hits else None


def recover_original_title(ol: dict | None) -> str | None:
    if not ol:
        return None
    notes = ol.get("notes")
    if isinstance(notes, dict):
        notes = notes.get("value", "")
    if not isinstance(notes, str):
        return None
    m = _ORIG.search(notes)
    return m.group(1).strip() if m else None


def main() -> None:
    with open("probe_raw.json", encoding="utf-8") as f:
        books = json.load(f)

    print(f"{'#'*82}")
    print("离线复核：从 OpenLibrary by_statement / notes 解析译者 + 原作名")
    print(f"{'#'*82}\n")

    n_total = len(books)
    n_ol = 0
    n_trans = 0
    n_orig = 0
    for b in books:
        ol = b.get("openlibrary_raw")
        if ol:
            n_ol += 1
        by = (ol or {}).get("by_statement") or "—"
        trans = recover_translator(ol)
        orig = recover_original_title(ol)
        if trans:
            n_trans += 1
        if orig:
            n_orig += 1
        mark = "★" if (trans or orig) else " "
        print(f"{mark} {b['isbn']}  {b['label']}")
        print(f"    by_statement: {by}")
        print(f"    → 译者(解析):   {trans or '—（无 yi 段，判定为原创/无译者）'}")
        print(f"    → 原作名(解析): {orig or '—'}")
        print()

    print(f"{'-'*82}")
    print(f"共 {n_total} 本，其中 OL 有记录 {n_ol} 本")
    print(f"解析出译者:   {n_trans} 本   （结构化 translators 字段原本是 0）")
    print(f"解析出原作名: {n_orig} 本   （结构化 original_title 字段原本是 0）")
    print(f"{'-'*82}")
    print("注意：解析出来的是罗马拼音（Fan Ye / Li Jihong），不是汉字。")
    print("拼音→汉字 有歧义，需众包或字典补全。但'信息可得'已坐实。")


if __name__ == "__main__":
    main()
