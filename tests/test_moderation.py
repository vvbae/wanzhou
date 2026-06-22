"""Phase C：贡献（加/改）→ 待审 → admin 审核 的测试。无网络。"""

import warnings

from fastapi.testclient import TestClient

import cnbib.api as apimod
from cnbib import store

warnings.filterwarnings("ignore")
TOKEN = "test-secret"


def _setup(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    conn = store.connect(path)
    store.init_db(conn)
    wid = store.upsert_work(conn, {"title": "围城"}, id="OL_WC", author_ids=[])
    store.upsert_edition(conn, "9787020024759", {"work_id": wid, "title": "围城"})
    conn.close()
    monkeypatch.setattr(apimod, "DB_PATH", path)
    monkeypatch.setattr(apimod, "ADMIN_TOKEN", TOKEN)
    return wid


class TestCreateBookStore:
    def test_create_book_builds_three_layers(self, tmp_path):
        conn = store.connect(str(tmp_path / "t.db"))
        store.init_db(conn)
        isbn = store.create_book(conn, {
            "isbn_13": "9787544291170", "title": "百年孤独", "authors": ["加西亚·马尔克斯"],
            "translators": ["范晔"], "publisher": "南海出版公司", "title_original": "Cien años de soledad",
        })
        e = store.get_edition(conn, isbn)
        assert e["title"] == "百年孤独" and e["translators"] == ["范晔"]
        assert e["work"]["title_original"] == "Cien años de soledad"
        assert e["work"]["authors"][0]["name"] == "加西亚·马尔克斯"
        assert e["sources"]["translators"] == "crowdsource"


class TestContributeFlow:
    def test_edit_needs_review_then_admin_approves(self, tmp_path, monkeypatch):
        wid = _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            r = c.post("/contribute", json={"target_type": "work", "kind": "edit",
                       "target_id": wid, "payload": {"description": "钱钟书的长篇小说"}})
            assert r.status_code == 200 and r.json()["status"] == "pending"
            # 待审期间不动实体
            assert c.get("/works", params={"id": wid}).json()["description"] is None
            # 无口令不能审
            assert c.get("/admin/contributions").status_code == 403
            # 有口令能看 + 审
            lst = c.get("/admin/contributions", headers={"X-Admin-Token": TOKEN}).json()
            assert len(lst) == 1
            cid = lst[0]["id"]
            assert c.post(f"/admin/contributions/{cid}/approve",
                          headers={"X-Admin-Token": TOKEN}).status_code == 200
            w = c.get("/works", params={"id": wid}).json()
            assert w["description"] == "钱钟书的长篇小说"
            assert w["sources"]["description"] == "crowdsource"

    def test_add_book_then_approve_creates_and_searchable(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            r = c.post("/contribute", json={"target_type": "book", "kind": "add", "payload": {
                "isbn_13": "9787549529322", "title": "看见", "authors": ["柴静"], "publisher": "广西师范大学出版社"}})
            assert r.status_code == 200
            cid = c.get("/admin/contributions", headers={"X-Admin-Token": TOKEN}).json()[0]["id"]
            # 没有这本书，直到通过
            assert c.get("/books/9787549529322").status_code == 404
            c.post(f"/admin/contributions/{cid}/approve", headers={"X-Admin-Token": TOKEN})
            e = c.get("/books/9787549529322").json()
            assert e["title"] == "看见" and e["work"]["authors"][0]["name"] == "柴静"
            assert c.get("/search", params={"q": "看见"}).json()["total"] == 1

    def test_reject(self, tmp_path, monkeypatch):
        wid = _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            c.post("/contribute", json={"target_type": "work", "kind": "edit",
                   "target_id": wid, "payload": {"title": "乱改"}})
            cid = c.get("/admin/contributions", headers={"X-Admin-Token": TOKEN}).json()[0]["id"]
            assert c.post(f"/admin/contributions/{cid}/reject",
                          headers={"X-Admin-Token": TOKEN}).status_code == 200
            assert c.get("/works", params={"id": wid}).json()["title"] == "围城"

    def test_add_book_bad_isbn_rejected(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            r = c.post("/contribute", json={"target_type": "book", "kind": "add",
                       "payload": {"isbn_13": "not-isbn", "title": "x"}})
            assert r.status_code == 400

    def test_edit_split_per_field(self, tmp_path, monkeypatch):
        wid = _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            r = c.post("/contribute", json={"target_type": "work", "kind": "edit",
                       "target_id": wid, "payload": {"title": "围城", "description": "钱钟书"}})
            assert len(r.json()["ids"]) == 2          # 两个字段 → 两条
            fields = {x["field_name"] for x in c.get("/admin/contributions",
                      headers={"X-Admin-Token": TOKEN}).json()}
            assert fields == {"title", "description"}

    def test_conflict_approve_one_rejects_others(self, tmp_path, monkeypatch):
        wid = _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            c.post("/contribute", json={"target_type": "work", "kind": "edit",
                   "target_id": wid, "payload": {"description": "甲的说法"}})
            c.post("/contribute", json={"target_type": "work", "kind": "edit",
                   "target_id": wid, "payload": {"description": "乙的说法"}})
            pend = c.get("/admin/contributions", headers={"X-Admin-Token": TOKEN}).json()
            assert len(pend) == 2                     # 同字段两个竞争提议
            # 采纳"甲" → "乙"自动驳回
            chosen = next(x for x in pend if x["payload"]["description"] == "甲的说法")
            c.post(f"/admin/contributions/{chosen['id']}/approve", headers={"X-Admin-Token": TOKEN})
            assert c.get("/admin/contributions", headers={"X-Admin-Token": TOKEN}).json() == []
            assert c.get("/works", params={"id": wid}).json()["description"] == "甲的说法"

    def test_add_existing_isbn_rejected_409(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)   # 已 seed 围城 9787020024759
        with TestClient(apimod.app) as c:
            r = c.post("/contribute", json={"target_type": "book", "kind": "add",
                       "payload": {"isbn_13": "9787020024759", "title": "围城"}})
            assert r.status_code == 409   # 查重：已收录 → 拒绝，去纠错
