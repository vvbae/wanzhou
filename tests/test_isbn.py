"""ISBN 规范化测试。"""

from cnbib.isbn import (
    is_valid_isbn10,
    is_valid_isbn13,
    isbn10_to_13,
    normalize,
)


def test_valid_isbn13():
    assert is_valid_isbn13("9787536692930")
    assert not is_valid_isbn13("9787559610387")  # probe 抓到的非法号
    assert not is_valid_isbn13("123")


def test_valid_isbn10():
    assert is_valid_isbn10("7208061645")
    assert is_valid_isbn10("020161586X")  # X 校验位
    assert not is_valid_isbn10("0201615860")


def test_isbn10_to_13():
    assert isbn10_to_13("7208061645") == "9787208061644"


def test_normalize():
    assert normalize("978-7-5366-9293-0") == "9787536692930"  # 带连字符
    assert normalize("7208061645") == "9787208061644"          # 10 位转 13
    assert normalize("不是isbn") is None
