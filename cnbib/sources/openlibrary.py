"""OpenLibrary adapter。

注意（2026-06 起的现实）：设计文档里写的 /api/books?jscmd=data 端点已被 OpenLibrary
弃用，现在恒返回 HTTP 500（"DEPRECATED ENDPOINT ... migrated to FastAPI"）。
本 adapter 改用现行的 JSON 端点：
  GET /isbn/{isbn}.json    —— 版本（edition）记录
  GET /works/{id}.json     —— 作品记录（edition 无 authors 时取这里）
  GET /authors/{id}.json   —— 把 author key 解析成人名

探针阶段的关键发现仍然成立，且新端点更直接：
- 译者：by_statement "X zhu ; Y yi" 解析（拼音）
- 原作名：edition 的 translation_of 字段（显式！）或 notes "Translation of: X."
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from cnbib.cleaning import (
    extract_original_title,
    normalize_language,
    parse_by_statement,
    parse_year,
)
from cnbib.sources.base import Source, empty_record

_BASE = "https://openlibrary.org"
_COVER = "https://covers.openlibrary.org/b/id/{}-L.jpg"
_MAX_AUTHORS = 6  # 限制作者解析的并发抓取数


class OpenLibrarySource(Source):
    name = "openlibrary"
    confidence = 20  # 见 CLAUDE.md：高于 google_books，低于 crowdsource

    async def fetch(self, client: httpx.AsyncClient, isbn: str) -> dict[str, Any] | None:
        ed = await _get_json(client, f"{_BASE}/isbn/{isbn}.json")
        if not ed or not ed.get("title"):
            return None

        rec = empty_record()
        rec["title"] = _clean_str(ed.get("title"))
        rec["subtitle"] = _clean_str(ed.get("subtitle"))

        by_authors, translators = parse_by_statement(ed.get("by_statement"))
        rec["translators"] = translators or None

        # 原作名：优先显式 translation_of，回退解析 notes
        rec["original_title"] = (
            _clean_str(ed.get("translation_of"))
            or extract_original_title(ed.get("notes"))
        )

        # 作者：edition.authors → work.authors，解析成人名；都没有才回退 by_statement
        resolved = await _resolve_authors(client, ed)
        rec["authors"] = resolved or by_authors or None

        publishers = [p for p in (ed.get("publishers") or []) if p]
        rec["publisher"] = ", ".join(publishers) or None
        rec["publish_date"] = _clean_str(ed.get("publish_date"))
        rec["publish_year"] = parse_year(ed.get("publish_date"))

        pc = ed.get("number_of_pages")
        rec["page_count"] = pc if isinstance(pc, int) and pc > 0 else None

        covers = [c for c in (ed.get("covers") or []) if isinstance(c, int) and c > 0]
        rec["cover_url"] = _COVER.format(covers[0]) if covers else None

        langs = ed.get("languages") or []
        lang_code = langs[0]["key"].split("/")[-1] if langs and langs[0].get("key") else None
        rec["language"] = normalize_language(lang_code, rec["title"])

        series = ed.get("series") or []
        rec["series"] = _clean_str(series[0]) if series else None
        return rec


async def _resolve_authors(
    client: httpx.AsyncClient, edition: dict[str, Any]
) -> list[str]:
    """把 edition / work 里的 author key 解析成人名列表。"""
    keys = [a["key"] for a in edition.get("authors", []) if a.get("key")]
    if not keys:
        works = edition.get("works") or []
        if works and works[0].get("key"):
            work = await _get_json(client, f"{_BASE}{works[0]['key']}.json")
            if work:
                keys = [
                    a["author"]["key"]
                    for a in work.get("authors", [])
                    if a.get("author", {}).get("key")
                ]
    keys = keys[:_MAX_AUTHORS]
    names = await asyncio.gather(*(_author_name(client, k) for k in keys))
    return [n for n in names if n]


async def _author_name(client: httpx.AsyncClient, key: str) -> str | None:
    data = await _get_json(client, f"{_BASE}{key}.json")
    return _clean_str(data.get("name")) if data else None


async def _get_json(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    try:
        r = await client.get(url, timeout=15, follow_redirects=True)
        r.raise_for_status()
        return r.json()
    except (httpx.HTTPError, ValueError):
        return None


def _clean_str(v: Any) -> str | None:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None
