"""三层数据层 store.py 的测试：作者/作品/版本关系 + 搜索 + 贡献审核。"""

from cnbib import store
from cnbib.store import _FS


def _conn(tmp_path):
    conn = store.connect(str(tmp_path / "t.db"))
    store.init_db(conn)
    return conn


def _seed(conn):
    aid = store.upsert_author(conn, {"name": "加西亚·马尔克斯", "name_original": "Gabriel García Márquez",
                                     "bio": "哥伦比亚作家"}, id="OL27363A")
    wid = store.upsert_work(conn, {"title": "百年孤独", "title_original": "Cien años de soledad",
                                   "subjects": ["魔幻现实主义"]}, id="OL274505W", author_ids=[aid])
    store.upsert_edition(conn, "9787544253994", {
        "work_id": wid, "translators": ["范晔"], "publisher": "南海出版公司", "publish_year": 2011})
    store.upsert_edition(conn, "9787544291170", {
        "work_id": wid, "publisher": "南海出版公司", "publish_year": 2017, "format": "精装"})
    return aid, wid


class TestThreeLayer:
    def test_edition_links_to_work_and_authors(self, tmp_path):
        conn = _conn(tmp_path); _seed(conn)
        e = store.get_edition(conn, "9787544253994")
        assert e["translators"] == ["范晔"]              # 译者在版本层
        assert e["work"]["title"] == "百年孤独"           # 链到作品
        assert e["work"]["title_original"] == "Cien años de soledad"
        assert e["work"]["authors"][0]["name"] == "加西亚·马尔克斯"

    def test_work_lists_all_editions(self, tmp_path):
        conn = _conn(tmp_path); _, wid = _seed(conn)
        w = store.get_work(conn, wid)
        isbns = {e["isbn_13"] for e in w["editions"]}
        assert isbns == {"9787544253994", "9787544291170"}   # 一作品两版本

    def test_author_lists_works(self, tmp_path):
        conn = _conn(tmp_path); aid, _ = _seed(conn)
        a = store.get_author(conn, aid)
        assert a["bio"] == "哥伦比亚作家"
        assert a["name_original"] == "Gabriel García Márquez"
        assert [w["title"] for w in a["works"]] == ["百年孤独"]

    def test_missing(self, tmp_path):
        conn = _conn(tmp_path)
        assert store.get_edition(conn, "9780000000000") is None
        assert store.get_work(conn, "nope") is None


class TestChineseTitle:
    def test_display_prefers_chinese_edition_title(self, tmp_path):
        # work 标题是原文/拼音，中文标题在版本上 → 展示和搜索都该用中文
        conn = _conn(tmp_path)
        wid = store.upsert_work(conn, {"title": "The Kite Runner"}, id="OL5781992W")
        store.upsert_edition(conn, "9787208061644",
                             {"work_id": wid, "title": "追风筝的人", "publisher": "上海人民"})
        store.rebuild_fts(conn)
        w = store.get_work(conn, wid)
        assert w["title"] == "The Kite Runner"        # 原始 work 标题保留
        assert w["display_title"] == "追风筝的人"        # 展示用中文版本标题
        # 搜中文标题能命中
        total, hits = store.search(conn, "追风筝的人")
        assert total == 1 and hits[0]["title"] == "追风筝的人"


class TestSearch:
    def test_search_by_title(self, tmp_path):
        conn = _conn(tmp_path); _seed(conn)
        total, hits = store.search(conn, "百年孤独")
        assert total == 1
        assert hits[0]["authors"] == ["加西亚·马尔克斯"]

    def test_search_by_author(self, tmp_path):
        conn = _conn(tmp_path); _seed(conn)
        total, _ = store.search(conn, "马尔克斯")
        assert total == 1

    def test_search_empty(self, tmp_path):
        conn = _conn(tmp_path)
        assert store.search(conn, "  ") == (0, [])


class TestStats:
    def test_counts(self, tmp_path):
        conn = _conn(tmp_path); _seed(conn)
        s = store.stats(conn)
        assert s["authors"] == 1 and s["works"] == 1 and s["editions"] == 2
        assert s["pending_contributions"] == 0


class TestContributions:
    def test_edit_pending_then_approve_sets_crowdsource(self, tmp_path):
        conn = _conn(tmp_path); _, wid = _seed(conn)
        cid = store.add_contribution(conn, target_type="work", target_id=wid, kind="edit",
                                     payload={"description": "马孔多的百年兴衰"}, contributor_hint="1.2.3.4")
        # 待审期间不动实体
        assert store.get_work(conn, wid)["description"] is None
        assert store.stats(conn)["pending_contributions"] == 1

        assert store.approve_contribution(conn, cid) is True
        w = store.get_work(conn, wid)
        assert w["description"] == "马孔多的百年兴衰"
        assert w["sources"]["description"] == "crowdsource"   # 来源标人工
        assert store.stats(conn)["pending_contributions"] == 0

    def test_reject_leaves_entity_untouched(self, tmp_path):
        conn = _conn(tmp_path); _, wid = _seed(conn)
        cid = store.add_contribution(conn, target_type="work", target_id=wid, kind="edit",
                                     payload={"title": "乱改的标题"})
        assert store.reject_contribution(conn, cid, note="标题不对") is True
        assert store.get_work(conn, wid)["title"] == "百年孤独"   # 没被改
        assert [c["status"] for c in store.list_contributions(conn, "rejected")] == ["rejected"]

    def test_add_edition_via_contribution(self, tmp_path):
        conn = _conn(tmp_path); _, wid = _seed(conn)
        cid = store.add_contribution(conn, target_type="edition", kind="add",
                                     payload={"isbn_13": "9787020024758", "work_id": wid,
                                              "publisher": "人民文学出版社", "publish_year": 2020})
        store.approve_contribution(conn, cid)
        e = store.get_edition(conn, "9787020024758")
        assert e is not None and e["publisher"] == "人民文学出版社"
        assert e["sources"]["publisher"] == "crowdsource"
        # 新版本归到同一作品 → 作品现在三个版本
        assert len(store.get_work(conn, wid)["editions"]) == 3

    def test_approve_nonexistent(self, tmp_path):
        conn = _conn(tmp_path)
        assert store.approve_contribution(conn, 999) is False
