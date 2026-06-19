"""统一的数据源接口与内部记录结构。

新增一个源 = 加一个继承 Source 的文件，把它的原始响应映射成 SOURCE_FIELDS
里的扁平 dict，主流程（aggregator）不用改。
"""

from __future__ import annotations

import abc
from typing import Any

import httpx

# aggregator / db 共用的字段全集（对应 books 表，去掉时间戳与主键）。
# 顺序无关紧要，但集中定义一处，避免各源各写一套。
SOURCE_FIELDS: tuple[str, ...] = (
    "isbn_10",
    "title",
    "subtitle",
    "authors",          # list[str] —— 中文译名（外国作者也存译名，原名进 original_authors）
    "translators",      # list[str]
    "original_title",
    "original_authors",  # list[str] —— 外国作者的原文名
    "publisher",
    "publish_date",
    "publish_year",  # int
    "description",
    "cover_url",
    "page_count",    # int
    "language",
    "series",
    "subjects",      # list[str]
    "clc",
)

# 这些字段是 list，空列表视为"无值"
LIST_FIELDS: frozenset[str] = frozenset(
    {"authors", "translators", "original_authors", "subjects"}
)


def empty_record() -> dict[str, Any]:
    """一个所有字段为 None 的内部记录骨架。"""
    return {f: None for f in SOURCE_FIELDS}


class Source(abc.ABC):
    """数据源 adapter 基类。

    子类实现 `fetch(client, isbn)`，返回映射好、清洗过的内部 dict
    （键是 SOURCE_FIELDS 的子集），查不到返回 None。
    """

    #: 源标识，写进 field_sources.source
    name: str
    #: 合并优先级分数（越大越优先），见 CLAUDE.md 字段合并优先级
    confidence: int

    @abc.abstractmethod
    async def fetch(self, client: httpx.AsyncClient, isbn: str) -> dict[str, Any] | None:
        ...
