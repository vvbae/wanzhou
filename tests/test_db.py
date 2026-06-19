"""db 层冒烟测试：建表、写回、读取、搜索、stats。"""

from cnbib import db
from cnbib.aggregator import FieldSource
from cnbib.sources.base import empty_record


def _conn(tmp_path):
    conn = db.connect(str(tmp_path / "test.db"))
    db.init_db(conn)
    return conn


def _santi():
    r = empty_record()
    r.update(
        title="三体",
        authors=["刘慈欣"],
        publisher="重庆出版社",
        publish_year=2008,
        language="zh-Hans",
        subjects=["科幻"],
    )
    return r


def test_upsert_and_get(tmp_path):
    conn = _conn(tmp_path)
    fs = [FieldSource("title", "openlibrary", 20), FieldSource("authors", "google_books", 10)]
    db.upsert_book(conn, "9787536692930", _santi(), fs)

    book = db.get_book(conn, "9787536692930")
    assert book is not None
    assert book["title"] == "三体"
    assert book["authors"] == ["刘慈欣"]          # JSON 往返成 list
    assert book["subjects"] == ["科幻"]
    assert book["sources"]["title"] == "openlibrary"
    assert book["sources"]["authors"] == "google_books"
    assert book["created_at"] and book["updated_at"]


def test_get_missing(tmp_path):
    conn = _conn(tmp_path)
    assert db.get_book(conn, "9780000000000") is None


def test_upsert_is_idempotent_keeps_created_at(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_book(conn, "9787536692930", _santi(), [])
    created1 = db.get_book(conn, "9787536692930")["created_at"]
    db.upsert_book(conn, "9787536692930", _santi(), [])
    book2 = db.get_book(conn, "9787536692930")
    assert book2["created_at"] == created1  # created_at 不被覆盖


def test_search_chinese(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_book(conn, "9787536692930", _santi(), [])
    total, rows = db.search(conn, "三体")
    assert total == 1
    assert rows[0]["isbn_13"] == "9787536692930"


def test_search_by_author(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_book(conn, "9787536692930", _santi(), [])
    total, rows = db.search(conn, "刘慈欣")
    assert total == 1


def test_search_empty_query(tmp_path):
    conn = _conn(tmp_path)
    assert db.search(conn, "   ") == (0, [])


def test_random_and_stats(tmp_path):
    conn = _conn(tmp_path)
    db.upsert_book(
        conn, "9787536692930", _santi(),
        [FieldSource("title", "openlibrary", 20)],
    )
    assert db.random_book(conn)["isbn_13"] == "9787536692930"

    s = db.stats(conn)
    assert s["total_books"] == 1
    assert s["field_value_count"] == 1
    assert s["by_source"] == [{"source": "openlibrary", "count": 1}]
    assert s["recent_isbns"] == ["9787536692930"]
