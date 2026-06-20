#!/usr/bin/env python3
"""从 OpenLibrary 搜索 API 采集中文书 ISBN，生成一个列表文件。

解决"没有 ISBN 列表"的问题：OL 按语言能列出中文书，每条带 ISBN。
默认只保留 9787 开头（中国大陆出版号），过滤掉作品里混进的外文版 ISBN。
采集出来的列表喂给 import_isbns.py 就能灌库。

用法：
    uv run python harvest_isbns.py --pages 10 --out data/isbns_cn.txt
    uv run python harvest_isbns.py --query "subject:科幻 language:chi" --pages 5
    uv run python harvest_isbns.py --prefix 9789 --pages 5     # 港台等其它前缀
"""

from __future__ import annotations

import argparse
import time

import httpx

from cnbib.isbn import is_valid_isbn13

_API = "https://openlibrary.org/search.json"


def harvest(query: str, pages: int, prefix: str, delay: float) -> list[str]:
    got: set[str] = set()
    with httpx.Client(headers={"User-Agent": "cn-bib-harvester/0.1"}) as client:
        for page in range(1, pages + 1):
            try:
                r = client.get(
                    _API,
                    params={"q": query, "fields": "isbn", "limit": 100, "page": page},
                    timeout=30,
                )
                r.raise_for_status()
                docs = r.json().get("docs", [])
            except (httpx.HTTPError, ValueError) as e:
                print(f"  第 {page} 页失败：{e!r}，跳过")
                continue
            before = len(got)
            for doc in docs:
                for x in doc.get("isbn", []):
                    if x.startswith(prefix) and is_valid_isbn13(x):
                        got.add(x)
            print(f"  第 {page}/{pages} 页：累计 {len(got)}（+{len(got)-before}）", flush=True)
            if not docs:
                print("  没有更多结果了，停。")
                break
            time.sleep(delay)
    return sorted(got)


def main() -> None:
    p = argparse.ArgumentParser(description="从 OpenLibrary 采集中文书 ISBN")
    p.add_argument("--query", default="language:chi", help="OL 搜索式（默认中文语言）")
    p.add_argument("--pages", type=int, default=10, help="翻多少页（每页 100 条作品）")
    p.add_argument("--prefix", default="9787", help="只保留此前缀的 ISBN（默认大陆 9787）")
    p.add_argument("--out", default="data/isbns_cn.txt", help="输出文件")
    p.add_argument("--delay", type=float, default=0.5, help="翻页间隔秒数")
    args = p.parse_args()

    print(f"采集：q='{args.query}' 前缀={args.prefix} 共 {args.pages} 页")
    isbns = harvest(args.query, args.pages, args.prefix, args.delay)

    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(f"# 从 OpenLibrary 采集，q='{args.query}' 前缀={args.prefix}\n")
        for i in isbns:
            f.write(i + "\n")
    print(f"\n共 {len(isbns)} 个唯一 ISBN → {args.out}")


if __name__ == "__main__":
    main()
