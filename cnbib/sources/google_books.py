"""Google Books adapter。

匿名查询配额极低（会 429）；设了环境变量 GOOGLE_BOOKS_API_KEY 就带上。
探针阶段的观察：Google 给干净的中文书名/简介，但译者常混在 authors 里，
且 description 偶尔是别的语言（需上层按 language 判断）。
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from cnbib.cleaning import normalize_language, parse_year, split_translators_from_authors
from cnbib.sources.base import Source, empty_record

_API = "https://www.googleapis.com/books/v1/volumes"
_KEY = os.environ.get("GOOGLE_BOOKS_API_KEY", "").strip()


class GoogleBooksSource(Source):
    name = "google_books"
    confidence = 10  # 见 CLAUDE.md：低于 openlibrary

    async def fetch(self, client: httpx.AsyncClient, isbn: str) -> dict[str, Any] | None:
        params = {"q": f"isbn:{isbn}", "country": "US"}
        if _KEY:
            params["key"] = _KEY
        try:
            r = await client.get(_API, params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
        except (httpx.HTTPError, ValueError):
            return None

        items = data.get("items")
        if not items:
            return None
        vi = items[0].get("volumeInfo", {}) or {}

        rec = empty_record()
        rec["isbn_10"] = _isbn10_from(vi)
        rec["title"] = _clean_str(vi.get("title"))
        rec["subtitle"] = _clean_str(vi.get("subtitle"))

        authors, translators = split_translators_from_authors(vi.get("authors"))
        rec["authors"] = authors or None
        rec["translators"] = translators or None

        rec["publisher"] = _clean_str(vi.get("publisher"))
        rec["publish_date"] = _clean_str(vi.get("publishedDate"))
        rec["publish_year"] = parse_year(vi.get("publishedDate"))
        rec["description"] = _clean_str(vi.get("description"))

        images = vi.get("imageLinks") or {}
        rec["cover_url"] = images.get("thumbnail") or images.get("smallThumbnail")

        pc = vi.get("pageCount")
        rec["page_count"] = pc if isinstance(pc, int) and pc > 0 else None

        # 语言用源给的 code，配合 title 细分繁简
        rec["language"] = normalize_language(vi.get("language"), rec["title"])
        rec["subjects"] = vi.get("categories") or None
        return rec


def _clean_str(v: Any) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def _isbn10_from(vi: dict[str, Any]) -> str | None:
    for ident in vi.get("industryIdentifiers") or []:
        if ident.get("type") == "ISBN_10":
            return ident.get("identifier")
    return None
