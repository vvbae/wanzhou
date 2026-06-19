"""中文清洗逻辑的单元测试。用例多取自 probe 真实返回。"""

from cnbib.cleaning import (
    detect_script,
    extract_original_title,
    has_cjk,
    normalize_language,
    parse_by_statement,
    parse_year,
    split_translators_from_authors,
)


class TestParseByStatement:
    def test_romanized_author_and_translator(self):
        # 百年孤独：probe 真实 by_statement
        authors, translators = parse_by_statement("Jiaxiya Ma'erkesi zhu ; Fan Ye yi")
        assert authors == ["Jiaxiya Ma'erkesi"]
        assert translators == ["Fan Ye"]

    def test_with_nation_prefix(self):
        # 追风筝的人：带 [Mei] 国别前缀
        authors, translators = parse_by_statement("[Mei] Kalede Husaini zhu ; Li Jihong yi")
        assert authors == ["Kalede Husaini"]
        assert translators == ["Li Jihong"]

    def test_author_only_no_translator(self):
        # 围城：原创，只有 zhu，无译者
        authors, translators = parse_by_statement("Qian Zhongshu zhu.")
        assert authors == ["Qian Zhongshu"]
        assert translators == []

    def test_bare_name_no_role(self):
        # 看见：by_statement 只有名字
        authors, translators = parse_by_statement("Chai Jing")
        assert authors == ["Chai Jing"]
        assert translators == []

    def test_chinese_roles(self):
        authors, translators = parse_by_statement("加西亚·马尔克斯 著 ; 范晔 译")
        assert authors == ["加西亚·马尔克斯"]
        assert translators == ["范晔"]

    def test_multiple_translators(self):
        authors, translators = parse_by_statement("某某 著 ; 张三、李四 译")
        assert authors == ["某某"]
        assert translators == ["张三", "李四"]

    def test_empty(self):
        assert parse_by_statement(None) == ([], [])
        assert parse_by_statement("") == ([], [])


class TestSplitTranslatorsFromAuthors:
    def test_translator_mixed_in(self):
        authors, translators = split_translators_from_authors(["余华", "白睿文 译"])
        assert authors == ["余华"]
        assert translators == ["白睿文"]

    def test_translator_no_space(self):
        authors, translators = split_translators_from_authors(["李继宏译"])
        assert authors == []
        assert translators == ["李继宏"]

    def test_pure_authors_untouched(self):
        # 没有"译"标记的多作者，不臆测拆分
        authors, translators = split_translators_from_authors(["刘慈欣", "王晋康"])
        assert authors == ["刘慈欣", "王晋康"]
        assert translators == []

    def test_empty(self):
        assert split_translators_from_authors(None) == ([], [])
        assert split_translators_from_authors([]) == ([], [])


class TestExtractOriginalTitle:
    def test_simple(self):
        assert extract_original_title("Translation of: The kite runner.") == "The kite runner"

    def test_multiline_with_prefix(self):
        notes = "Xiao shuo.\n\nTranslation of: Cien años de soledad."
        assert extract_original_title(notes) == "Cien años de soledad"

    def test_dict_notes(self):
        assert extract_original_title({"value": "Translation of: Foo."}) == "Foo"

    def test_no_translation_note(self):
        assert extract_original_title("Essays.") is None
        assert extract_original_title(None) is None


class TestParseYear:
    def test_plain_year(self):
        assert parse_year("2008") == 2008

    def test_iso_date(self):
        assert parse_year("2017-01-01") == 2017

    def test_chinese_date(self):
        assert parse_year("1991年5月") == 1991

    def test_bracketed(self):
        assert parse_year("[2005]") == 2005

    def test_int_input(self):
        assert parse_year(1989) == 1989

    def test_out_of_range(self):
        assert parse_year(3000) is None
        assert parse_year("99") is None

    def test_none(self):
        assert parse_year(None) is None
        assert parse_year("no year here") is None


class TestNormalizeLanguage:
    def test_zh_cn_to_hans(self):
        assert normalize_language("zh-CN") == "zh-Hans"

    def test_zh_tw_to_hant(self):
        assert normalize_language("zh-TW") == "zh-Hant"

    def test_underscore_variant(self):
        assert normalize_language("zh_Hant") == "zh-Hant"

    def test_bare_zh_with_traditional_text(self):
        assert normalize_language("zh", "這是繁體書國語") == "zh-Hant"

    def test_bare_zh_with_simplified_text(self):
        assert normalize_language("zh", "这是简体书国语") == "zh-Hans"

    def test_bare_zh_undetectable(self):
        assert normalize_language("zh", "ABC 123") == "zh"

    def test_non_chinese(self):
        assert normalize_language("en") == "en"
        assert normalize_language("ja-JP") == "ja"

    def test_no_code_detect_from_text(self):
        assert normalize_language(None, "繁體國風") == "zh-Hant"
        assert normalize_language(None, None) is None


class TestHasCjk:
    def test_chinese(self):
        assert has_cjk("百年孤独")
        assert has_cjk("追风筝的人")

    def test_pinyin_and_latin(self):
        assert not has_cjk("Bai nian gu du")
        assert not has_cjk("Khaled Hosseini")
        assert not has_cjk("")
        assert not has_cjk(None)


class TestDetectScript:
    def test_traditional(self):
        assert detect_script("國風書愛") == "zh-Hant"

    def test_simplified(self):
        assert detect_script("国风书爱") == "zh-Hans"

    def test_ambiguous(self):
        assert detect_script("中文") is None
