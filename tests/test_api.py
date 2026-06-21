"""读侧 API（三层）端点 + 页面测试。用 store 预置数据，无网络。"""

import warnings

from fastapi.testclient import TestClient

import cnbib.api as apimod
from cnbib import store

warnings.filterwarnings("ignore")


def _setup(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    conn = store.connect(path)
    store.init_db(conn)
    aid = store.upsert_author(conn, {"name": "卡勒德·胡赛尼", "name_original": "Khaled Hosseini",
                                     "bio": "阿富汗裔美国作家"}, id="OL1412764A")
    wid = store.upsert_work(conn, {"title": "The Kite Runner", "title_original": "The Kite Runner"},
                            id="OL5781992W", author_ids=[aid])
    store.upsert_edition(conn, "9787208061644", {
        "work_id": wid, "title": "追风筝的人", "translators": ["李继宏"],
        "publisher": "上海人民出版社", "publish_year": 2006,
        "cover_url": "https://covers.openlibrary.org/b/id/9248248-L.jpg"})
    store.set_sources(conn, "edition", "9787208061644", [store._FS("translators")])
    store.rebuild_fts(conn)
    conn.close()
    monkeypatch.setattr(apimod, "DB_PATH", path)
    return aid, wid


class TestJSON:
    def test_edition_with_work_and_author(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            d = c.get("/books/9787208061644").json()
            assert d["title"] == "追风筝的人"          # 版本中文标题
            assert d["translators"] == ["李继宏"]
            assert d["sources"]["translators"] == "crowdsource"
            assert d["work"]["display_title"] == "追风筝的人"   # 优先中文
            assert d["work"]["authors"][0]["name"] == "卡勒德·胡赛尼"

    def test_work_lists_editions(self, tmp_path, monkeypatch):
        _, wid = _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            d = c.get("/works", params={"id": wid}).json()
            assert d["editions"][0]["isbn_13"] == "9787208061644"
            assert d["display_title"] == "追风筝的人"

    def test_author_lists_works(self, tmp_path, monkeypatch):
        aid, _ = _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            d = c.get("/authors", params={"id": aid}).json()
            assert d["bio"] == "阿富汗裔美国作家"
            assert d["works"][0]["title"] == "The Kite Runner"

    def test_search_finds_chinese_title(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            d = c.get("/search", params={"q": "追风筝的人"}).json()
            assert d["total"] == 1
            assert d["results"][0]["title"] == "追风筝的人"

    def test_random_books_showcase(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            d = c.get("/random_books", params={"n": 8}).json()
            assert len(d["results"]) == 1            # 库里就一本带封面
            b = d["results"][0]
            assert b["title"] == "追风筝的人" and b["cover_url"] and b["work_id"]

    def test_stats(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            s = c.get("/stats").json()
            assert s["authors"] == 1 and s["works"] == 1 and s["editions"] == 1

    def test_404(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            assert c.get("/books/9780000000000").status_code == 404
            assert c.get("/works", params={"id": "nope"}).status_code == 404


class TestPages:
    def test_pages_served(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            assert "中文开放书目" in c.get("/").text
            for p in ["/work", "/book", "/author", "/add", "/edit", "/admin"]:
                assert c.get(p).status_code == 200, p
