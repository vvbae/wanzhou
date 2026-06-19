"""aggregator 字段级合并的单元测试。纯逻辑 + 用假源测并发抓取，无网络。"""

from typing import Any

import pytest

from cnbib.aggregator import (
    CROWD_CONFIDENCE,
    CROWD_SOURCE,
    aggregate,
    derive_original_authors,
    merge_records,
    romanized_fields,
)
from cnbib.sources.base import Source, empty_record


def rec(**kw: Any) -> dict[str, Any]:
    r = empty_record()
    r.update(kw)
    return r


class TestMergeRecords:
    def test_complement_missing_fields(self):
        # google 有简介，openlibrary 有出版社，互补
        google = ("google_books", 10, rec(description="简介", title="书"))
        ol = ("openlibrary", 20, rec(publisher="出版社", title="书"))
        result = merge_records([google, ol])
        assert result.record["description"] == "简介"
        assert result.record["publisher"] == "出版社"

    def test_conflict_higher_priority_wins(self):
        # 同字段冲突：openlibrary(20) 覆盖 google_books(10)
        google = ("google_books", 10, rec(title="Google 书名"))
        ol = ("openlibrary", 20, rec(title="OpenLibrary 书名"))
        result = merge_records([google, ol])
        assert result.record["title"] == "OpenLibrary 书名"

    def test_crowd_beats_everything(self):
        google = ("google_books", 10, rec(title="G"))
        ol = ("openlibrary", 20, rec(title="OL"))
        crowd = (CROWD_SOURCE, CROWD_CONFIDENCE, rec(title="人工确认"))
        result = merge_records([google, ol, crowd])
        assert result.record["title"] == "人工确认"

    def test_empty_list_treated_as_missing(self):
        # openlibrary 的 authors 是空列表，应让位给 google 的非空
        google = ("google_books", 10, rec(authors=["刘慈欣"]))
        ol = ("openlibrary", 20, rec(authors=[]))
        result = merge_records([google, ol])
        assert result.record["authors"] == ["刘慈欣"]

    def test_field_sources_recorded(self):
        google = ("google_books", 10, rec(description="简介"))
        ol = ("openlibrary", 20, rec(title="书", publisher="社"))
        result = merge_records([google, ol])
        by_field = {fs.field_name: fs.source for fs in result.field_sources}
        assert by_field["description"] == "google_books"
        assert by_field["title"] == "openlibrary"
        assert by_field["publisher"] == "openlibrary"
        # 没有值的字段不应记来源
        assert "series" not in by_field

    def test_no_candidates(self):
        result = merge_records([])
        assert all(v is None for v in result.record.values())
        assert result.field_sources == []


class TestPreferChinese:
    def test_title_picks_chinese_over_pinyin_ignoring_priority(self):
        # OL(20) 给拼音书名，Google(10) 给中文 —— 应选中文，尽管 OL 优先级更高
        ol = ("openlibrary", 20, rec(title="Bai nian gu du"))
        google = ("google_books", 10, rec(title="百年孤独"))
        result = merge_records([ol, google])
        assert result.record["title"] == "百年孤独"
        by_field = {fs.field_name: fs.source for fs in result.field_sources}
        assert by_field["title"] == "google_books"

    def test_publisher_picks_chinese_from_lower_priority(self):
        # 反方向：OL 给中文出版社，Google 给拼音 —— 选 OL 的中文
        ol = ("openlibrary", 20, rec(publisher="上海人民"))
        google = ("google_books", 10, rec(publisher="Shang Hai Ren Min"))
        result = merge_records([ol, google])
        assert result.record["publisher"] == "上海人民"

    def test_authors_list_prefers_chinese(self):
        ol = ("openlibrary", 20, rec(authors=["Qian, Zhongshu"]))
        google = ("google_books", 10, rec(authors=["钱钟书"]))
        result = merge_records([ol, google])
        assert result.record["authors"] == ["钱钟书"]

    def test_falls_back_to_pinyin_when_no_chinese(self):
        # 译者只有拼音可得 —— 保留拼音（避免不了）
        ol = ("openlibrary", 20, rec(translators=["Fan Ye"]))
        result = merge_records([ol])
        assert result.record["translators"] == ["Fan Ye"]

    def test_original_title_not_forced_chinese(self):
        # 原作名本就是外文，不应被"优先中文"影响
        ol = ("openlibrary", 20, rec(original_title="The Kite Runner"))
        result = merge_records([ol])
        assert result.record["original_title"] == "The Kite Runner"


class TestRomanizedFields:
    def test_flags_pinyin_title_and_translator(self):
        record = rec(title="Bai nian gu du", translators=["Fan Ye"], publisher="上海人民")
        flagged = romanized_fields(record)
        assert "title" in flagged
        assert "translators" in flagged
        assert "publisher" not in flagged  # 中文，不标

    def test_no_flag_when_all_chinese(self):
        record = rec(title="百年孤独", publisher="南海出版公司")
        assert romanized_fields(record) == []

    def test_latin_authors_flagged(self):
        # 规则改后：authors 存中文译名，拉丁作者名 = 待补译名
        assert "authors" in romanized_fields(rec(authors=["Khaled Hosseini"]))
        # 中文译名不标
        assert "authors" not in romanized_fields(rec(authors=["卡勒德·胡赛尼"]))


class TestDeriveOriginalAuthors:
    def test_translation_copies_foreign_author(self):
        # 译作（有译者）+ 拉丁作者名 → 原文名进 original_authors
        record = rec(authors=["Gabriel García Márquez"], translators=["范晔"])
        derive_original_authors(record)
        assert record["original_authors"] == ["Gabriel García Márquez"]

    def test_triggered_by_original_title(self):
        record = rec(authors=["Khaled Hosseini"], original_title="The Kite Runner")
        derive_original_authors(record)
        assert record["original_authors"] == ["Khaled Hosseini"]

    def test_non_translation_not_derived(self):
        # 原创中文书：不是译作，不动 original_authors
        record = rec(authors=["刘慈欣"])
        derive_original_authors(record)
        assert not record["original_authors"]

    def test_chinese_translated_name_not_copied(self):
        # 作者已是中文译名 → 没有原文名可抽，original_authors 留空
        record = rec(authors=["加西亚·马尔克斯"], translators=["范晔"])
        derive_original_authors(record)
        assert not record["original_authors"]

    def test_existing_value_not_overwritten(self):
        record = rec(
            authors=["Gabriel García Márquez"],
            translators=["范晔"],
            original_authors=["加西亚·马尔克斯(原名)"],
        )
        derive_original_authors(record)
        assert record["original_authors"] == ["加西亚·马尔克斯(原名)"]


# ── 用假源测 aggregate() 的并发抓取 + 容错 ──────────────────────────

class _FakeSource(Source):
    def __init__(self, name, confidence, record=None, *, fail=False):
        self.name = name
        self.confidence = confidence
        self._record = record
        self._fail = fail

    async def fetch(self, client, isbn):
        if self._fail:
            raise RuntimeError("源挂了")
        return self._record


@pytest.mark.asyncio
async def test_aggregate_merges_two_sources():
    sources = [
        _FakeSource("google_books", 10, rec(description="简介", title="G书名")),
        _FakeSource("openlibrary", 20, rec(title="OL书名", publisher="社")),
    ]
    result = await aggregate("9787536692930", sources)
    assert result.record["title"] == "OL书名"        # 高优先级覆盖
    assert result.record["description"] == "简介"      # 互补
    assert result.record["publisher"] == "社"


@pytest.mark.asyncio
async def test_aggregate_survives_source_failure():
    # 一个源抛异常，另一个正常，聚合不应失败
    sources = [
        _FakeSource("openlibrary", 20, fail=True),
        _FakeSource("google_books", 10, rec(title="只剩 Google")),
    ]
    result = await aggregate("9787536692930", sources)
    assert result.record["title"] == "只剩 Google"


@pytest.mark.asyncio
async def test_aggregate_all_empty():
    sources = [
        _FakeSource("openlibrary", 20, None),
        _FakeSource("google_books", 10, None),
    ]
    result = await aggregate("9787536692930", sources)
    assert result.record["title"] is None


@pytest.mark.asyncio
async def test_aggregate_with_crowd():
    sources = [_FakeSource("google_books", 10, rec(title="G"))]
    result = await aggregate("9787536692930", sources, crowd=rec(title="人工"))
    assert result.record["title"] == "人工"
