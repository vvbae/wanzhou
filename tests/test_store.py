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


class TestWorkAuthors:
    def test_reuse_existing_and_create_new(self, tmp_path):
        conn = _conn(tmp_path)
        a1 = store.upsert_author(conn, {"name": "钱钟书"}, id="OL_QZS")
        wid = store.upsert_work(conn, {"title": "围城"}, id="w1", author_ids=[])
        # 复用已有作者(id) + 新建一个(name)
        store.set_work_authors(conn, wid, [{"id": a1, "name": "钱钟书"}, {"name": "杨绛"}])
        names = {a["name"] for a in store.get_work(conn, wid)["authors"]}
        assert names == {"钱钟书", "杨绛"}
        # 复用没新建：钱钟书 还是同一个 id（没造重复）
        ids = [a["id"] for a in store.get_work(conn, wid)["authors"] if a["name"] == "钱钟书"]
        assert ids == ["OL_QZS"]

    def test_existing_name_reused_not_duplicated(self, tmp_path):
        conn = _conn(tmp_path)
        store.upsert_author(conn, {"name": "鲁迅"}, id="OL_LX")
        wid = store.upsert_work(conn, {"title": "呐喊"}, id="w1", author_ids=[])
        store.set_work_authors(conn, wid, [{"name": "鲁迅"}])   # 只给名字，应复用已有
        assert conn.execute("SELECT count(*) FROM authors WHERE name='鲁迅'").fetchone()[0] == 1

    def test_via_approve(self, tmp_path):
        conn = _conn(tmp_path)
        a1 = store.upsert_author(conn, {"name": "A"}, id="a1")
        wid = store.upsert_work(conn, {"title": "x"}, id="w1", author_ids=[a1])
        cid = store.add_contribution(conn, target_type="work", target_id=wid, kind="edit",
                                     payload={"authors": [{"name": "B"}]})
        store.approve_contribution(conn, cid)
        assert [a["name"] for a in store.get_work(conn, wid)["authors"]] == ["B"]


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


class TestEnrichment:
    def test_pinyin_title_upgraded_to_chinese(self, tmp_path):
        conn = _conn(tmp_path)
        wid = store.upsert_work(conn, {"title": "Bai nian gu du"}, id="w1")
        store.upsert_edition(conn, "9787544253994", {"work_id": wid, "title": "Bai nian gu du"})
        changed = store.apply_enrichment(conn, "9787544253994", {
            "title": "百年孤独", "cover_url": "https://x/c.jpg", "description": "马孔多的百年"})
        assert "title" in changed
        e = store.get_edition(conn, "9787544253994")
        assert e["title"] == "百年孤独"
        assert e["sources"]["title"] == "google_books"
        assert e["cover_url"] == "https://x/c.jpg"
        assert e["enriched"]                                   # 标记已富化
        assert e["work"]["description"] == "马孔多的百年"
        assert store.search(conn, "百年孤独")[0] == 1           # 富化后中文可搜

    def test_does_not_overwrite_existing_chinese(self, tmp_path):
        conn = _conn(tmp_path)
        wid = store.upsert_work(conn, {"title": "三体"}, id="w2")
        store.upsert_edition(conn, "9787536692930", {"work_id": wid, "title": "三体"})
        changed = store.apply_enrichment(conn, "9787536692930", {"title": "Santi"})
        assert "title" not in changed                          # 已是中文，不覆盖
        assert store.get_edition(conn, "9787536692930")["title"] == "三体"

    def test_needs_enrichment_picks_pinyin(self, tmp_path):
        conn = _conn(tmp_path)
        w = store.upsert_work(conn, {"title": "x"}, id="w3")
        store.upsert_edition(conn, "9787544253994", {"work_id": w, "title": "Bai nian gu du"})
        store.upsert_edition(conn, "9787536692930", {"work_id": w, "title": "三体"})
        picks = store.needs_enrichment(conn)
        assert "9787544253994" in picks and "9787536692930" not in picks


class TestTags:
    def test_tags_dedup_and_browse(self, tmp_path):
        conn = _conn(tmp_path)
        w1 = store.upsert_work(conn, {"title": "三体"}, id="w1")
        w2 = store.upsert_work(conn, {"title": "球状闪电"}, id="w2")
        store.add_tags_to_work(conn, w1, ["科幻", "中国文学"])
        store.add_tags_to_work(conn, w2, ["科幻 ", "刘慈欣"])   # "科幻 " 归一到同一标签
        # 作品带标签
        assert {t["name"] for t in store.tags_for_work(conn, w1)} == {"科幻", "中国文学"}
        # 按标签浏览：科幻 → 两本
        total, name, results = store.works_by_tag(conn, "科幻")
        assert total == 2 and name == "科幻"
        assert {r["title"] for r in results} == {"三体", "球状闪电"}
        # 自动补全
        assert store.search_tags(conn, "科")[0]["name"] == "科幻"

    def test_get_work_includes_tags(self, tmp_path):
        conn = _conn(tmp_path)
        wid = store.upsert_work(conn, {"title": "x"}, id="w1")
        store.add_tags_to_work(conn, wid, ["历史"])
        assert store.get_work(conn, wid)["tags"][0]["slug"] == "历史"

    def test_build_from_subjects(self, tmp_path):
        conn = _conn(tmp_path)
        store.upsert_work(conn, {"title": "x", "subjects": ["小说", "经典"]}, id="w1")
        store.upsert_work(conn, {"title": "y", "subjects": ["小说"]}, id="w2")
        store.build_tags_from_subjects(conn)
        total, _, _ = store.works_by_tag(conn, "小说")
        assert total == 2


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
