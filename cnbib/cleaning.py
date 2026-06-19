"""中文书目的清洗逻辑 —— 默认逻辑会错的地方都在这里，单独可测。

涵盖 CLAUDE.md "中文书的特殊处理"四条：
- 译者拆分：从 Google 的 authors（译者常混在里面）和 OpenLibrary 的 by_statement
  （MARC 惯例 "X zhu ; Y yi"）里把译者分离出来。
- 原作名：从 OpenLibrary notes 的 "Translation of: X" 提取。
- 语言代码：规范化 zh / zh-CN / zh-Hant 的混乱，繁简可分。
- publish_year：从各种日期字符串解析纯年份。
"""

from __future__ import annotations

import re

# CJK 统一表意文字（含扩展 A / 兼容区），用来判断一个值是不是中文（而非拼音）。
_CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿]")


def has_cjk(s: str | None) -> bool:
    """字符串里有没有汉字。用来在合并时优先选中文值、剔除拼音。"""
    return bool(s and _CJK.search(s))

# ── 译者拆分 ───────────────────────────────────────────────────────

# 责任者角色标记：罗马拼音 + 中文。著=author，译=translator。
_AUTHOR_MARK = re.compile(r"\b(zhu|zhuan|bianzhu|著|撰)\b|著$")
_TRANS_MARK = re.compile(r"\b(yi|bianyi)\b|译|譯|翻譯|翻译")
# 名字前常见的国别/语种前缀，如 "[Mei] ..."、"（美）..."、"[英]..."
_NATION_PREFIX = re.compile(r"^[\[\(（【][^\]\)）】]*[\]\)）】]\s*")
# 拆多个名字
_NAME_SPLIT = re.compile(r"[，,、;；/]|\sand\s|\s&\s")


def _strip_role(seg: str) -> str:
    """去掉责任段里的角色词和国别前缀，留下人名。"""
    s = _NATION_PREFIX.sub("", seg.strip())
    s = s.strip(" .;,，、。")  # 先去尾部标点，否则 "zhu." 的角色词去不掉
    # 去尾部角色词（中文紧贴、拼音空格分隔）
    s = re.sub(r"\s*(zhu|zhuan|bianzhu|yi|bianyi)\s*$", "", s, flags=re.I)
    s = re.sub(r"[\s]*[著撰译譯]\s*$", "", s)
    s = re.sub(r"(翻譯|翻译)\s*$", "", s)
    return s.strip(" .;,，、")


def _split_names(seg: str) -> list[str]:
    return [n for n in (p.strip(" .，,、") for p in _NAME_SPLIT.split(seg)) if n]


def parse_by_statement(by_statement: str | None) -> tuple[list[str], list[str]]:
    """解析 OpenLibrary by_statement → (authors, translators)。

    例 "Jiaxiya Ma'erkesi zhu ; Fan Ye yi" → (["Jiaxiya Ma'erkesi"], ["Fan Ye"])
    例 "Qian Zhongshu zhu." → (["Qian Zhongshu"], [])  # 无译者
    """
    authors: list[str] = []
    translators: list[str] = []
    if not by_statement:
        return authors, translators
    for seg in re.split(r"[;；]", by_statement):
        seg = seg.strip()
        if not seg:
            continue
        is_trans = bool(_TRANS_MARK.search(seg))
        is_author = bool(_AUTHOR_MARK.search(seg))
        name = _strip_role(seg)
        if not name:
            continue
        names = _split_names(name)
        # "译"优先判定（一个段一般只有一个角色；同时命中时按译处理更安全）
        if is_trans and not is_author:
            translators.extend(names)
        elif is_author and not is_trans:
            authors.extend(names)
        elif is_trans:
            translators.extend(names)
        else:
            authors.extend(names)
    return authors, translators


def split_translators_from_authors(
    authors: list[str] | None,
) -> tuple[list[str], list[str]]:
    """从 Google 的 authors 里把带"译/譯/翻译"标记的条目拆成 translators。

    例 ["余华", "白睿文 译"] → (["余华"], ["白睿文"])
    没有标记的（如纯多作者）保持在 authors，不臆测。
    """
    real_authors: list[str] = []
    translators: list[str] = []
    for a in authors or []:
        if not isinstance(a, str) or not a.strip():
            continue
        if _TRANS_MARK.search(a):
            name = _strip_role(a)
            if name:
                translators.append(name)
        else:
            real_authors.append(a.strip())
    return real_authors, translators


# ── 原作名 ─────────────────────────────────────────────────────────

_ORIG_TITLE = re.compile(r"Translation of:\s*(.+?)\s*[.。]", re.I | re.S)


def extract_original_title(notes: str | dict | None) -> str | None:
    """从 OpenLibrary notes 的 "Translation of: X." 提取原作名。"""
    if isinstance(notes, dict):
        notes = notes.get("value", "")
    if not isinstance(notes, str):
        return None
    m = _ORIG_TITLE.search(notes)
    if not m:
        return None
    return m.group(1).strip() or None


# ── publish_year ──────────────────────────────────────────────────

_YEAR = re.compile(r"(1[0-9]{3}|20[0-9]{2}|2100)")


def parse_year(date_str: str | int | None) -> int | None:
    """从任意日期字符串解析出 4 位年份。

    "2008" / "2017-01-01" / "1991年5月" / "民国27年" / "[2005]" 都尽量解析。
    取第一个落在 1400–2100 的 4 位数。
    """
    if date_str is None:
        return None
    if isinstance(date_str, int):
        return date_str if 1400 <= date_str <= 2100 else None
    for m in _YEAR.finditer(str(date_str)):
        y = int(m.group(1))
        if 1400 <= y <= 2100:
            return y
    return None


# ── 语言代码规范化 ─────────────────────────────────────────────────

# 繁体专用字 / 简体专用字 的小样本，用于 zh 无地区时粗判繁简。
_TRAD_ONLY = set("體國風雲書當愛聲學歲傳寫專樂藝營經費鄉誌譯")
_SIMP_ONLY = set("体国风云书当爱声学岁传写专乐艺营经费乡志译")

_LANG_MAP = {
    "zh-cn": "zh-Hans",
    "zh-hans": "zh-Hans",
    "zh-sg": "zh-Hans",
    "zh-my": "zh-Hans",
    "zh-tw": "zh-Hant",
    "zh-hk": "zh-Hant",
    "zh-mo": "zh-Hant",
    "zh-hant": "zh-Hant",
    "chi": "zh",
    "zho": "zh",
    "cmn": "zh",
}


def detect_script(text: str | None) -> str | None:
    """据文本里的繁/简专用字粗判 zh-Hant / zh-Hans；判不出返回 None。"""
    if not text:
        return None
    trad = sum(1 for c in text if c in _TRAD_ONLY)
    simp = sum(1 for c in text if c in _SIMP_ONLY)
    if trad > simp:
        return "zh-Hant"
    if simp > trad:
        return "zh-Hans"
    return None


def normalize_language(code: str | None, text: str | None = None) -> str | None:
    """规范化语言代码。zh 的繁简混乱统一到 zh-Hans / zh-Hant；判不出留 zh。

    非中文（en/ja/...）规范成小写主标签返回。
    """
    if code:
        c = code.strip().lower().replace("_", "-")
        if c in _LANG_MAP:
            mapped = _LANG_MAP[c]
            if mapped == "zh":  # 泛中文，尝试用文本细分
                return detect_script(text) or "zh"
            return mapped
        if c == "zh":
            return detect_script(text) or "zh"
        if c.startswith("zh"):
            return detect_script(text) or "zh"
        # 非中文语言：返回主标签
        return c.split("-")[0]
    # 没有 code，但有文本：靠脚本检测
    return detect_script(text)
