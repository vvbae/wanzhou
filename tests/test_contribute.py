"""Phase 2：/contribute 端点 + edits 日志 + 众包覆盖 的测试（无网络，库内预置）。"""

import warnings

from fastapi.testclient import TestClient

import cnbib.api as apimod
from cnbib import db
from cnbib.aggregator import FieldSource
from cnbib.sources.base import empty_record

warnings.filterwarnings("ignore")


def _seed(path):
    """预置一本译者是拼音的书（百年孤独），模拟外部源聚合后的状态。"""
    conn = db.connect(path)
    db.init_db(conn)
    rec = empty_record()
    rec.update(
        title="Bai nian gu du",            # 拼音，待纠
        authors=["Gabriel García Márquez"],
        translators=["Fan Ye"],            # 拼音译者，待补中文
        publisher="南海出版公司",
    )
    db.upsert_book(
        conn, "9787544253994", rec,
        [FieldSource("title", "openlibrary", 20), FieldSource("translators", "openlibrary", 20)],
    )
    conn.close()


class TestApplyContributionDB:
    def test_records_edit_and_overrides_source(self, tmp_path):
        path = str(tmp_path / "t.db")
        _seed(path)
        conn = db.connect(path)
        applied = db.apply_contribution(
            conn, "9787544253994",
            {"title": "百年孤独", "translators": "范晔"},
            "1.2.3.4",
        )
        assert set(applied) == {"title", "translators"}

        book = db.get_book(conn, "9787544253994")
        assert book["title"] == "百年孤独"
        assert book["translators"] == ["范晔"]
        # 众包成为这两个字段的来源
        assert book["sources"]["title"] == "crowdsource"
        assert book["sources"]["translators"] == "crowdsource"
        # 没改的字段来源不动
        assert book["sources"].get("publisher") != "crowdsource" or "publisher" not in book["sources"]

        # edits 日志记下了旧值/新值/贡献者
        edits = db.list_edits(conn, "9787544253994")
        titles = {e["field_name"]: e for e in edits}
        assert titles["title"]["old_value"] == "Bai nian gu du"
        assert titles["title"]["new_value"] == "百年孤独"
        assert titles["translators"]["contributor_hint"] == "1.2.3.4"

    def test_unchanged_field_skipped(self, tmp_path):
        path = str(tmp_path / "t.db")
        _seed(path)
        conn = db.connect(path)
        applied = db.apply_contribution(
            conn, "9787544253994", {"publisher": "南海出版公司"}, "ip"
        )
        assert applied == []          # 值没变，不记
        assert db.list_edits(conn, "9787544253994") == []

    def test_unknown_field_rejected(self, tmp_path):
        path = str(tmp_path / "t.db")
        _seed(path)
        conn = db.connect(path)
        try:
            db.apply_contribution(conn, "9787544253994", {"bogus": "x"}, "ip")
            assert False, "应当拒绝未知字段"
        except ValueError:
            pass

    def test_list_field_split(self, tmp_path):
        path = str(tmp_path / "t.db")
        _seed(path)
        conn = db.connect(path)
        db.apply_contribution(conn, "9787544253994", {"authors": "马尔克斯、另一人"}, "ip")
        assert db.get_book(conn, "9787544253994")["authors"] == ["马尔克斯", "另一人"]


class TestContributeAPI:
    def test_contribute_updates_and_clears_flag(self, tmp_path, monkeypatch):
        path = str(tmp_path / "t.db")
        _seed(path)
        monkeypatch.setattr(apimod, "DB_PATH", path)
        with TestClient(apimod.app) as c:
            r = c.post(
                "/contribute",
                json={"isbn": "9787544253994", "fields": {"title": "百年孤独", "translators": "范晔"}},
            )
            assert r.status_code == 200, r.text
            d = r.json()
            assert set(d["applied"]) == {"title", "translators"}
            assert d["book"]["title"] == "百年孤独"
            assert d["book"]["translators"] == ["范晔"]
            assert d["book"]["sources"]["title"] == "crowdsource"
            # 补了中文后，这两个字段不再出现在 needs_chinese
            assert "title" not in d["book"]["needs_chinese"]
            assert "translators" not in d["book"]["needs_chinese"]

    def test_contribute_bad_isbn(self, tmp_path, monkeypatch):
        monkeypatch.setattr(apimod, "DB_PATH", str(tmp_path / "t.db"))
        with TestClient(apimod.app) as c:
            r = c.post("/contribute", json={"isbn": "notisbn", "fields": {"title": "x"}})
            assert r.status_code == 400

    def test_contribute_unknown_field(self, tmp_path, monkeypatch):
        path = str(tmp_path / "t.db")
        _seed(path)
        monkeypatch.setattr(apimod, "DB_PATH", path)
        with TestClient(apimod.app) as c:
            r = c.post("/contribute", json={"isbn": "9787544253994", "fields": {"bogus": "x"}})
            assert r.status_code == 400

    def test_pages_served(self, tmp_path, monkeypatch):
        monkeypatch.setattr(apimod, "DB_PATH", str(tmp_path / "t.db"))
        with TestClient(apimod.app) as c:
            home = c.get("/")
            assert home.status_code == 200 and "中文开放书目" in home.text
            book = c.get("/book")
            assert book.status_code == 200 and "返回搜索" in book.text
            edit = c.get("/edit")
            assert edit.status_code == 200 and "扫条码" in edit.text
