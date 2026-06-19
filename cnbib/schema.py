"""Pydantic 模型 —— 统一对外数据结构。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class Book(BaseModel):
    isbn_13: str
    isbn_10: str | None = None
    title: str | None = None
    subtitle: str | None = None
    authors: list[str] = Field(default_factory=list)
    translators: list[str] = Field(default_factory=list)
    original_title: str | None = None
    original_authors: list[str] = Field(default_factory=list)
    publisher: str | None = None
    publish_date: str | None = None
    publish_year: int | None = None
    description: str | None = None
    cover_url: str | None = None
    page_count: int | None = None
    language: str | None = None
    series: str | None = None
    subjects: list[str] = Field(default_factory=list)
    clc: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class BookResponse(Book):
    """单本返回：在 Book 基础上附每字段来源。"""

    sources: dict[str, str] = Field(
        default_factory=dict,
        description="字段名 → 来源（crowdsource / openlibrary / google_books）",
    )
    needs_chinese: list[str] = Field(
        default_factory=list,
        description="有值但仍是拼音、待补中文的字段（众包优先补这些）",
    )


class SearchHit(BaseModel):
    isbn_13: str
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    publisher: str | None = None
    publish_year: int | None = None
    cover_url: str | None = None


class SearchResponse(BaseModel):
    query: str
    page: int
    page_size: int
    total: int
    results: list[SearchHit]


class ContributeRequest(BaseModel):
    isbn: str
    fields: dict[str, object] = Field(
        description="要补全/纠错的字段，如 {'translators': '范晔', 'title': '百年孤独'}"
    )
    contributor_hint: str | None = Field(
        default=None, description="可选的匿名标识；不填则用请求 IP"
    )


class ContributeResponse(BaseModel):
    isbn_13: str
    applied: list[str] = Field(description="实际发生改动的字段")
    book: BookResponse


class SourceCount(BaseModel):
    source: str
    count: int


class StatsResponse(BaseModel):
    total_books: int
    field_value_count: int
    by_source: list[SourceCount]
    recent_isbns: list[str]
