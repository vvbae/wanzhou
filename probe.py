#!/usr/bin/env python3
"""probe.py — Phase 0 数据可行性探针。

给一个 ISBN，并发查询 Google Books 和 OpenLibrary，把两个源返回的字段
并排打印，标注每个字段哪个源有、哪个源没有。再对一批 ISBN 批量跑一遍，
最后输出一张字段覆盖率统计表。

这是探针，不是产品：不写数据库、不起 API、不做字段合并清洗。目的只有一个——
看清楚两个源对真实中文书的字段覆盖到底如何（尤其译者 / 原作名 / 冷门书）。

用法：
    uv run probe.py                # 跑内置 15 个 ISBN
    uv run probe.py 9787536692930  # 只跑单个 ISBN（详细并排对比）

依赖：httpx
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from typing import Any

import httpx

# Google Books 匿名配额极低（会 429）。带 key 后免费 ~1000 次/天。
# 不写进代码：export GOOGLE_BOOKS_API_KEY=xxx
GOOGLE_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()

# ── 探针关心的字段（对应设计文档 books 表的核心列）──────────────────────
# 顺序即并排打印 / 覆盖率表的列顺序。
FIELDS = [
    "title",
    "subtitle",
    "authors",
    "translators",
    "original_title",
    "publisher",
    "publish_date",
    "description",
    "cover_url",
    "page_count",
    "language",
    "subjects",
]

# ── 测试用 ISBN ────────────────────────────────────────────────────
# 标签来源：上一轮 OpenLibrary 实际返回、号与书对得上的条目（已核验为真实书目）。
# 不再凭记忆编标签。带 ⚠ 的是上一轮 OL 查无、身份待 Google 核验的号——
# 保留是为了测"OL 查不到的书 Google 能不能补"，但它们的 ISBN-13 校验位
# 会在运行时检查，编错的号会被标出来并排除出覆盖率分母。
ISBNS: list[tuple[str, str]] = [
    ("9787536692930", "畅销·科幻 三体 (刘慈欣)"),
    ("9787208061644", "译作 追风筝的人"),
    ("9787544253994", "译作 百年孤独"),
    ("9787544291170", "译作 百年孤独(精装另一版)"),
    ("9787020024759", "经典 围城 (钱钟书)"),
    ("9787108009821", "学术·历史 万历十五年 (黄仁宇)"),
    ("9787530216781", "经典 平凡的世界 (路遥)"),
    ("9787549529322", "纪实 看见 (柴静)"),
    ("9787513320474", "绘本 博恩熊情境教育绘本"),
    # ↓ 上一轮 OL 查无，身份待核验（可能编错或确实冷门）
    ("9787100020558", "⚠待核验"),
    ("9787301156865", "⚠待核验"),
    ("9787540471644", "⚠待核验"),
    ("9787508660752", "⚠待核验"),
    ("9787559610387", "⚠待核验"),
    ("9787221152978", "⚠待核验"),
]


def valid_isbn13(isbn: str) -> bool:
    """ISBN-13 校验位检查：编错/拼错的号在此暴露。"""
    s = isbn.strip().replace("-", "")
    if len(s) != 13 or not s.isdigit():
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(s))
    return total % 10 == 0


# ── 数据源 adapter（探针版，极简）─────────────────────────────────────
# 每个源把原始响应抽成 {field: value or None} 的扁平 dict。
# 注意：这里只做最朴素的提取，不做译者拆分等清洗——那是 aggregator 的活，
# 探针要看的恰恰是"未清洗时原始覆盖长什么样"。


async def _get_with_retry(
    client: httpx.AsyncClient, url: str, *, tries: int = 4
) -> httpx.Response:
    """对 429 / 5xx 做指数退避重试。Google Books 对密集匿名查询很容易 429。"""
    delay = 1.0
    last: Exception | None = None
    for _ in range(tries):
        try:
            r = await client.get(url, timeout=15)
            if r.status_code in (429, 500, 502, 503):
                last = httpx.HTTPStatusError(
                    f"{r.status_code}", request=r.request, response=r
                )
                await asyncio.sleep(delay)
                delay *= 2
                continue
            r.raise_for_status()
            return r
        except (httpx.TransportError, httpx.HTTPStatusError) as e:
            last = e
            await asyncio.sleep(delay)
            delay *= 2
    raise last if last else RuntimeError("unreachable")


async def fetch_google(client: httpx.AsyncClient, isbn: str) -> dict[str, Any]:
    url = f"https://www.googleapis.com/books/v1/volumes?q=isbn:{isbn}&country=US"
    if GOOGLE_KEY:
        url += f"&key={GOOGLE_KEY}"
    try:
        r = await _get_with_retry(client, url)
        data = r.json()
    except Exception as e:  # noqa: BLE001 — 探针，任何失败都记为查不到
        return {"_error": repr(e)}

    if not data.get("items"):
        return {}  # 查到了但没结果

    vi = data["items"][0].get("volumeInfo", {})
    images = vi.get("imageLinks", {}) or {}
    return {
        "_raw": vi,  # 留原始 volumeInfo，给译者线索扫描/dump 用
        "title": vi.get("title"),
        "subtitle": vi.get("subtitle"),
        "authors": vi.get("authors"),  # Google 常把译者混在这里
        "translators": None,  # Google Books 无独立译者字段
        "original_title": None,  # Google Books 无原作名字段
        "publisher": vi.get("publisher"),
        "publish_date": vi.get("publishedDate"),
        "description": vi.get("description"),
        "cover_url": images.get("thumbnail") or images.get("smallThumbnail"),
        "page_count": vi.get("pageCount"),
        "language": vi.get("language"),
        "subjects": vi.get("categories"),
    }


async def fetch_openlibrary(client: httpx.AsyncClient, isbn: str) -> dict[str, Any]:
    url = (
        "https://openlibrary.org/api/books"
        f"?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
    )
    try:
        r = await client.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return {"_error": repr(e)}

    rec = data.get(f"ISBN:{isbn}")
    if not rec:
        return {}

    authors = [a.get("name") for a in rec.get("authors", []) if a.get("name")]
    publishers = [p.get("name") for p in rec.get("publishers", []) if p.get("name")]
    subjects = [s.get("name") for s in rec.get("subjects", []) if s.get("name")]
    cover = rec.get("cover", {}) or {}
    return {
        "_raw": rec,  # 留原始记录，给译者线索扫描/dump 用
        "title": rec.get("title"),
        "subtitle": rec.get("subtitle"),
        "authors": authors or None,
        "translators": None,  # OpenLibrary data API 不单列译者
        "original_title": None,
        "publisher": ", ".join(publishers) or None,
        "publish_date": rec.get("publish_date"),
        "description": (
            rec.get("description") if isinstance(rec.get("description"), str)
            else (rec.get("description") or {}).get("value")
            if isinstance(rec.get("description"), dict)
            else None
        ),
        "cover_url": cover.get("large") or cover.get("medium") or cover.get("small"),
        "page_count": rec.get("number_of_pages"),
        "language": None,  # jscmd=data 一般不含语言代码
        "subjects": subjects or None,
    }


# ── 工具：值是否算"有"───────────────────────────────────────────────
def has_value(v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    if isinstance(v, (list, dict)) and len(v) == 0:
        return False
    return True


def fmt_value(v: Any, width: int = 46) -> str:
    if not has_value(v):
        return "—"
    if isinstance(v, list):
        s = ", ".join(str(x) for x in v)
    else:
        s = str(v).replace("\n", " ")
    s = s.strip()
    return s if len(s) <= width else s[: width - 1] + "…"


# ── 译者线索扫描 ───────────────────────────────────────────────────
# "translators 字段 0%" ≠ 译者信息不存在。Google 常把译者塞进 authors 数组，
# 或写在 description 里。这里扫这些藏匿处，区分"字段缺失"和"信息不可得"。

# 译者信号：中文"译/譯/翻译"、英文 translated/translator、author 条目带"译"字
_TRANS_RE = re.compile(r"译者|譯者|翻译|翻譯|[一-鿿]\s*译|[一-鿿]\s*譯|translated by|translator", re.I)


def scan_translator_clues(g: dict, o: dict) -> dict[str, Any]:
    """返回译者线索：authors 是否疑似含译者、description 是否提到译者。"""
    g_authors = g.get("authors") or []
    o_authors = o.get("authors") or []
    descs = [d for d in (g.get("description"), o.get("description")) if isinstance(d, str)]

    # authors 里直接带"译"字的条目（最强信号——译者被混进 authors）
    author_hits = [a for a in (g_authors + o_authors) if isinstance(a, str) and _TRANS_RE.search(a)]
    # description 里的译者提及，留一段上下文供肉眼核
    desc_snip = None
    for d in descs:
        m = _TRANS_RE.search(d)
        if m:
            s = max(0, m.start() - 12)
            desc_snip = d[s : m.end() + 14].replace("\n", " ").strip()
            break

    # authors 多于 1 人：可能含译者，也可能就是多作者，标为"待查"
    multi = max(len(g_authors), len(o_authors)) >= 2

    recoverable = bool(author_hits or desc_snip)
    return {
        "recoverable": recoverable,
        "author_hits": author_hits,
        "desc_snip": desc_snip,
        "multi_author": multi,
        "g_authors": g_authors,
        "o_authors": o_authors,
    }


def print_translator_scan(results: list[tuple[str, str, dict, dict]]) -> None:
    print(f"\n\n{'#'*78}")
    print("译者线索扫描  —— 字段缺失 ≠ 信息不可得")
    print(f"{'#'*78}")
    n = len(results)
    recoverable = 0
    for isbn, label, g, o in results:
        if not has_value(g.get("title")) and not has_value(o.get("title")):
            continue  # 整本查无，跳过
        c = scan_translator_clues(g, o)
        if c["recoverable"]:
            recoverable += 1
        flag = "★可解析" if c["recoverable"] else ("?多作者待查" if c["multi_author"] else "·无线索")
        print(f"\n{isbn} [{label}]  {flag}")
        print(f"  authors(G): {fmt_value(c['g_authors'], 60)}")
        print(f"  authors(OL): {fmt_value(c['o_authors'], 60)}")
        if c["author_hits"]:
            print(f"  ⮑ authors 里带'译'字: {c['author_hits']}")
        if c["desc_snip"]:
            print(f"  ⮑ 简介提到译者: …{c['desc_snip']}…")
    print(f"\n{'-'*78}")
    print(f"可从 authors/description 解析救回译者的: {recoverable} 本（共 {n} 本）")
    print("→ translators 真实可得率远高于结构化字段的 0%，值得写解析而非纯靠人工。")
    print(f"{'#'*78}")


def dump_raw(results: list[tuple[str, str, dict, dict]], path: str) -> None:
    """把每本的原始 google/ol payload 存盘，供回看原始 JSON。"""
    out = [
        {"isbn": isbn, "label": label,
         "google_raw": g.get("_raw"), "openlibrary_raw": o.get("_raw"),
         "google_error": g.get("_error"), "openlibrary_error": o.get("_error")}
        for isbn, label, g, o in results
    ]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n[原始 JSON 已 dump → {path}]  共 {len(out)} 本，可回看 authors/description 全文")


# ── 单本：并排打印 ─────────────────────────────────────────────────
def print_side_by_side(isbn: str, label: str, g: dict, o: dict) -> None:
    print(f"\n{'='*78}")
    print(f"ISBN {isbn}  [{label}]")
    if "_error" in g:
        print(f"  ⚠ Google Books 请求失败: {g['_error']}")
    if "_error" in o:
        print(f"  ⚠ OpenLibrary 请求失败: {o['_error']}")
    print(f"{'-'*78}")
    print(f"{'字段':<16}{'Google':<2} {'OL':<2}  {'值（优先非空源）':<40}")
    print(f"{'-'*78}")
    for f in FIELDS:
        gv, ov = g.get(f), o.get(f)
        gmark = "✓" if has_value(gv) else "·"
        omark = "✓" if has_value(ov) else "·"
        shown = gv if has_value(gv) else ov
        print(f"{f:<16}{gmark:^7}{omark:^5} {fmt_value(shown)}")


# ── 批量：查一组 ISBN，累计覆盖率 ──────────────────────────────────
async def probe_one(
    client: httpx.AsyncClient, isbn: str
) -> tuple[dict, dict]:
    # 并发查两个源
    g, o = await asyncio.gather(
        fetch_google(client, isbn),
        fetch_openlibrary(client, isbn),
    )
    return g, o


def print_coverage_table(
    results: list[tuple[str, str, dict, dict]],
) -> None:
    n = len(results)
    print(f"\n\n{'#'*78}")
    print(f"字段覆盖率统计表  (共 {n} 个 ISBN)")
    print(f"{'#'*78}")
    print(f"{'字段':<16}{'Google':>9}{'OpenLib':>10}{'至少一个':>11}")
    print(f"{'-'*78}")

    for f in FIELDS:
        g_hit = sum(1 for _, _, g, _ in results if has_value(g.get(f)))
        o_hit = sum(1 for _, _, _, o in results if has_value(o.get(f)))
        either = sum(
            1 for _, _, g, o in results
            if has_value(g.get(f)) or has_value(o.get(f))
        )
        print(
            f"{f:<16}"
            f"{g_hit:>4}/{n} {100*g_hit//n:>3}%"
            f"{o_hit:>4}/{n} {100*o_hit//n:>3}%"
            f"{either:>5}/{n} {100*either//n:>3}%"
        )

    # 命中率：整本书在某源完全查不到（title 都没有）
    print(f"{'-'*78}")
    g_found = sum(1 for _, _, g, _ in results if has_value(g.get("title")))
    o_found = sum(1 for _, _, _, o in results if has_value(o.get("title")))
    either_found = sum(
        1 for _, _, g, o in results
        if has_value(g.get("title")) or has_value(o.get("title"))
    )
    print(
        f"{'整本可查(有title)':<16}"
        f"{g_found:>4}/{n} {100*g_found//n:>3}%"
        f"{o_found:>4}/{n} {100*o_found//n:>3}%"
        f"{either_found:>5}/{n} {100*either_found//n:>3}%"
    )
    print(f"{'#'*78}\n")


async def run_batch(isbns: list[tuple[str, str]]) -> None:
    # 先把编错的号挑出来——不让它们污染覆盖率分母
    bad = [(i, lbl) for i, lbl in isbns if not valid_isbn13(i)]
    good = [(i, lbl) for i, lbl in isbns if valid_isbn13(i)]
    print(f"{'#'*78}")
    print(f"探针启动：{len(isbns)} 个 ISBN，{len(good)} 个校验位合法，{len(bad)} 个非法")
    print(f"Google Books API key: {'已配置 ✓' if GOOGLE_KEY else '未配置（预期 429）✗'}")
    if bad:
        print("非法 ISBN（校验位不过，已排除出覆盖率分母）：")
        for i, lbl in bad:
            print(f"  ✗ {i}  [{lbl}]")
    print(f"{'#'*78}")

    results: list[tuple[str, str, dict, dict]] = []
    # 共享一个 client；并发数适度，别把外部 API 打急了
    limits = httpx.Limits(max_connections=8)
    async with httpx.AsyncClient(
        limits=limits, headers={"User-Agent": "cn-bib-probe/0.1"}
    ) as client:
        sem = asyncio.Semaphore(3)

        async def guarded(isbn: str, label: str):
            async with sem:
                g, o = await probe_one(client, isbn)
            return isbn, label, g, o

        tasks = [guarded(isbn, label) for isbn, label in good]
        for coro in asyncio.as_completed(tasks):
            isbn, label, g, o = await coro
            results.append((isbn, label, g, o))

    # 并排打印按原始顺序，读起来稳定
    order = {isbn: i for i, (isbn, _) in enumerate(isbns)}
    results.sort(key=lambda r: order[r[0]])
    for isbn, label, g, o in results:
        print_side_by_side(isbn, label, g, o)

    print_coverage_table(results)
    print_translator_scan(results)
    dump_raw(results, "probe_raw.json")


async def run_single(isbn: str) -> None:
    async with httpx.AsyncClient(
        headers={"User-Agent": "cn-bib-probe/0.1"}
    ) as client:
        g, o = await probe_one(client, isbn)
    print_side_by_side(isbn, "single", g, o)


def main() -> None:
    if len(sys.argv) > 1:
        asyncio.run(run_single(sys.argv[1].strip()))
    else:
        asyncio.run(run_batch(ISBNS))


if __name__ == "__main__":
    main()
