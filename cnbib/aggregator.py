"""字段级聚合 —— 项目核心逻辑，单独可测。

并发查所有源，按字段优先级合并（CLAUDE.md 规则）：
  缺失互补，冲突按优先级覆盖；每个采用值记一条 field_sources。
优先级（confidence 越大越优先）：
  crowdsource(人工确认) > openlibrary > google_books

合并是纯函数 merge_records()，不碰网络，方便单测。
aggregate() 负责并发抓取再调用它。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx

from cnbib.cleaning import has_cjk
from cnbib.sources.base import LIST_FIELDS, SOURCE_FIELDS, Source

# 众包确认值的优先级，高于任何外部源
CROWD_CONFIDENCE = 100
CROWD_SOURCE = "crowdsource"

# 这些字段"应该是中文"：合并时在非空候选里优先选带汉字的值，
# 不管它来自哪个源（书名 Google 常给中文、出版社 OL 常给中文，方向不固定）。
# 全力避免拼音 —— 只有所有源都没有中文版本时，才退而用拼音。
# 注意：original_title 不在内（外文原作名本就该是拉丁字母）。
CJK_PREFERRED: frozenset[str] = frozenset(
    {"title", "subtitle", "authors", "translators", "publisher", "series", "description"}
)

# 这些字段若最终值仍是拼音/拉丁（无汉字），标记为"待补中文"（needs_chinese）。
# authors 在内：按项目规则 authors 存中文译名，外国作者的原文名归 original_authors，
# 所以 authors 里出现拉丁字母 = 译名待补。original_authors 不在内（它本就该是原文）。
FLAG_IF_ROMANIZED: frozenset[str] = frozenset(
    {"title", "authors", "publisher", "series", "translators"}
)


@dataclass
class FieldSource:
    field_name: str
    source: str
    confidence: int


@dataclass
class AggregateResult:
    record: dict[str, Any]
    field_sources: list[FieldSource] = field(default_factory=list)


def _has_value(field_name: str, v: Any) -> bool:
    if v is None:
        return False
    if isinstance(v, str) and not v.strip():
        return False
    if field_name in LIST_FIELDS and isinstance(v, (list, tuple)) and not v:
        return False
    return True


def _is_chinese(field_name: str, v: Any) -> bool:
    """该值是否含汉字（list 字段任一元素含汉字即可）。"""
    if field_name in LIST_FIELDS and isinstance(v, (list, tuple)):
        return any(has_cjk(str(x)) for x in v)
    return has_cjk(str(v))


def derive_original_authors(record: dict[str, Any]) -> None:
    """译作里，API 给的作者名其实是原文名 —— 复制一份到 original_authors。

    规则：authors 存中文译名、original_authors 存原文名。外部源只给得出原文名
    （Gabriel García Márquez），中文译名要靠众包补。所以聚合时：若这是译作
    （有译者或原作名）且 authors 里有非中文项，把这些原文名填进 original_authors，
    authors 原样保留（暂时是原文，会被标 needs_chinese 提示补译名）。
    就地修改 record。
    """
    if record.get("original_authors"):
        return  # 已有（如众包提供），不覆盖
    is_translation = bool(record.get("translators") or record.get("original_title"))
    if not is_translation:
        return
    foreign = [a for a in (record.get("authors") or []) if not has_cjk(str(a))]
    if foreign:
        record["original_authors"] = foreign


def romanized_fields(record: dict[str, Any]) -> list[str]:
    """有值、但应是中文却仍是拼音的字段，列出来供前端提示 / 众包优先补。"""
    out = []
    for f in FLAG_IF_ROMANIZED:
        v = record.get(f)
        if _has_value(f, v) and not _is_chinese(f, v):
            out.append(f)
    return sorted(out)


def merge_records(
    candidates: list[tuple[str, int, dict[str, Any]]],
) -> AggregateResult:
    """按字段取最优值。

    candidates: [(source_name, confidence, record_dict), ...]
    - 普通字段：confidence 最大的非空值胜出。
    - CJK_PREFERRED 字段：先在非空候选里挑"带汉字"的（按优先级），
      全都没汉字才退用最高优先级的拼音值 —— 全力避免拼音。
    """
    merged = {f: None for f in SOURCE_FIELDS}
    sources: list[FieldSource] = []

    ordered = sorted(candidates, key=lambda c: c[1], reverse=True)
    for fname in SOURCE_FIELDS:
        valued = [
            (name, conf, rec.get(fname))
            for name, conf, rec in ordered
            if _has_value(fname, rec.get(fname))
        ]
        if not valued:
            continue

        chosen = None
        if fname in CJK_PREFERRED:
            chosen = next((c for c in valued if _is_chinese(fname, c[2])), None)
        if chosen is None:
            chosen = valued[0]  # 普通字段，或无中文候选时退用最高优先级

        name, conf, val = chosen
        merged[fname] = val
        sources.append(FieldSource(fname, name, conf))

    return AggregateResult(record=merged, field_sources=sources)


async def aggregate(
    isbn: str,
    sources: list[Source],
    *,
    client: httpx.AsyncClient | None = None,
    crowd: dict[str, Any] | None = None,
) -> AggregateResult:
    """并发查所有源 + 可选众包确认值，合并成一条记录。

    crowd：已人工确认的字段 dict（Phase 2 从 edits/books 来），优先级最高。
    """
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(headers={"User-Agent": "cn-bib-api/0.1"})
    try:
        results = await asyncio.gather(
            *(s.fetch(client, isbn) for s in sources),
            return_exceptions=True,
        )
    finally:
        if owns_client:
            await client.aclose()

    candidates: list[tuple[str, int, dict[str, Any]]] = []
    if crowd:
        candidates.append((CROWD_SOURCE, CROWD_CONFIDENCE, crowd))
    for src, res in zip(sources, results):
        if isinstance(res, dict):
            candidates.append((src.name, src.confidence, res))
        # 异常或 None：该源无贡献，跳过（聚合不因单源失败而失败）

    result = merge_records(candidates)
    derive_original_authors(result.record)
    return result
