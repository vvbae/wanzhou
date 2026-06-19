"""ISBN 规范化与校验。

库以 isbn_13 为主键。外部传进来的可能是 10 位或 13 位、带连字符/空格，
统一清洗成 13 位数字字符串；10 位的换算成 13 位。
"""

from __future__ import annotations


def _clean(raw: str) -> str:
    return raw.strip().replace("-", "").replace(" ", "").upper()


def is_valid_isbn13(isbn: str) -> bool:
    s = _clean(isbn)
    if len(s) != 13 or not s.isdigit():
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(s))
    return total % 10 == 0


def is_valid_isbn10(isbn: str) -> bool:
    s = _clean(isbn)
    if len(s) != 10:
        return False
    if not s[:9].isdigit():
        return False
    if not (s[9].isdigit() or s[9] == "X"):
        return False
    total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(s))
    return total % 11 == 0


def isbn10_to_13(isbn10: str) -> str:
    """10 位转 13 位（加 978 前缀、重算校验位）。"""
    s = _clean(isbn10)
    core = "978" + s[:9]
    check = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(core))
    check = (10 - check % 10) % 10
    return core + str(check)


def isbn13_to_10(isbn13: str) -> str | None:
    """13 位转 10 位（仅 978 段可转）。"""
    s = _clean(isbn13)
    if len(s) != 13 or not s.startswith("978"):
        return None
    core = s[3:12]
    total = sum((10 - i) * int(d) for i, d in enumerate(core))
    check = (11 - total % 11) % 11
    return core + ("X" if check == 10 else str(check))


def normalize(raw: str) -> str | None:
    """把任意形式的 ISBN 规范成 13 位主键；非法返回 None。"""
    s = _clean(raw)
    if is_valid_isbn13(s):
        return s
    if is_valid_isbn10(s):
        return isbn10_to_13(s)
    return None
