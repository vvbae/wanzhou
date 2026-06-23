"""账号 / 会话 / 角色审核 测试。"""

import warnings

from fastapi.testclient import TestClient

import cnbib.api as apimod
from cnbib import store

warnings.filterwarnings("ignore")


def _setup(tmp_path, monkeypatch):
    path = str(tmp_path / "t.db")
    conn = store.connect(path)
    store.init_db(conn)
    wid = store.upsert_work(conn, {"title": "围城"}, id="OL_WC")
    store.upsert_edition(conn, "9787020024759", {"work_id": wid, "title": "围城"})
    conn.close()
    monkeypatch.setattr(apimod, "DB_PATH", path)
    monkeypatch.setattr(apimod, "ADMIN_TOKEN", "")   # 不用 bootstrap 口令，纯账号
    return path, wid


class TestAuth:
    def test_register_login_me_logout(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            assert c.post("/auth/register", json={"username": "alice", "password": "secret1"}).status_code == 200
            assert c.get("/auth/me").json()["username"] == "alice"
            c.post("/auth/logout")
            assert c.get("/auth/me").json() == {}
            assert c.post("/auth/login", json={"username": "alice", "password": "secret1"}).status_code == 200
            assert c.post("/auth/login", json={"username": "alice", "password": "wrong"}).status_code == 401

    def test_short_password_rejected(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            assert c.post("/auth/register", json={"username": "bob", "password": "123"}).status_code == 400

    def test_contribution_attributed_to_logged_in_user(self, tmp_path, monkeypatch):
        path, wid = _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            c.post("/auth/register", json={"username": "carol", "password": "secret1"})
            c.post("/contribute", json={"target_type": "work", "kind": "edit",
                   "target_id": wid, "payload": {"description": "钱钟书的小说"}})
        conn = store.connect(path)
        assert store.list_contributions(conn)[0]["user_id"] == "carol"

    def test_my_contributions_tracks_status(self, tmp_path, monkeypatch):
        path, wid = _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            # 未登录看自己的贡献 → 401
            assert c.get("/my/contributions").status_code == 401
            c.post("/auth/register", json={"username": "dave", "password": "secret1"})
            c.post("/contribute", json={"target_type": "work", "kind": "edit",
                   "target_id": wid, "payload": {"description": "x"}})
            mine = c.get("/my/contributions").json()
            assert len(mine) == 1 and mine[0]["status"] == "pending"
            assert mine[0]["field_name"] == "description"
        # 管理员通过后，用户能看到状态变 approved
        conn = store.connect(path); store.create_user(conn, "boss", "secret1", role="admin")
        cid = store.list_contributions(conn)[0]["id"]; conn.close()
        with TestClient(apimod.app) as c:
            c.post("/auth/login", json={"username": "boss", "password": "secret1"})
            c.post(f"/admin/contributions/{cid}/approve")
            c.post("/auth/logout")
            c.post("/auth/login", json={"username": "dave", "password": "secret1"})
            assert c.get("/my/contributions").json()[0]["status"] == "approved"


class TestInvites:
    def test_only_admin_creates_invite_and_register_gets_role(self, tmp_path, monkeypatch):
        path, _ = _setup(tmp_path, monkeypatch)
        conn = store.connect(path)
        store.create_user(conn, "boss", "secret1", role="admin")
        store.create_user(conn, "rev", "secret1", role="reviewer")
        conn.close()
        with TestClient(apimod.app) as c:
            # 普通/审核员都不能生成邀请
            c.post("/auth/login", json={"username": "rev", "password": "secret1"})
            assert c.post("/admin/invites", json={"role": "reviewer"}).status_code == 403
            c.post("/auth/logout")
            # 管理员生成邀请
            c.post("/auth/login", json={"username": "boss", "password": "secret1"})
            inv = c.post("/admin/invites", json={"role": "reviewer"}).json()
            assert inv["role"] == "reviewer" and inv["token"]
            c.post("/auth/logout")
        # 用邀请注册 → 角色 reviewer
        with TestClient(apimod.app) as c:
            r = c.post("/auth/register", json={"username": "newrev", "password": "secret1", "invite": inv["token"]})
            assert r.json()["role"] == "reviewer"
            assert c.get("/admin/contributions").status_code == 200   # 能审核
        # 邀请一次性：再用同一个 → 400
        with TestClient(apimod.app) as c:
            assert c.post("/auth/register", json={"username": "x2", "password": "secret1",
                          "invite": inv["token"]}).status_code == 400

    def test_plain_register_is_user(self, tmp_path, monkeypatch):
        _setup(tmp_path, monkeypatch)
        with TestClient(apimod.app) as c:
            assert c.post("/auth/register", json={"username": "joe", "password": "secret1"}).json()["role"] == "user"


class TestRoleGating:
    def test_normal_user_cannot_review_admin_can(self, tmp_path, monkeypatch):
        path, wid = _setup(tmp_path, monkeypatch)
        conn = store.connect(path)
        store.create_user(conn, "boss", "secret1", role="admin")
        conn.close()
        with TestClient(apimod.app) as c:
            # 普通用户：注册后访问审核接口 → 403
            c.post("/auth/register", json={"username": "joe", "password": "secret1"})
            assert c.get("/admin/contributions").status_code == 403
            c.post("/auth/logout")
            # 管理员登录 → 可审
            c.post("/auth/login", json={"username": "boss", "password": "secret1"})
            assert c.get("/admin/contributions").status_code == 200

    def test_approve_records_reviewer(self, tmp_path, monkeypatch):
        path, wid = _setup(tmp_path, monkeypatch)
        conn = store.connect(path)
        store.create_user(conn, "boss", "secret1", role="reviewer")
        cid = store.add_contribution(conn, target_type="work", kind="edit",
                                     target_id=wid, payload={"description": "x"})
        conn.close()
        with TestClient(apimod.app) as c:
            c.post("/auth/login", json={"username": "boss", "password": "secret1"})
            assert c.post(f"/admin/contributions/{cid}/approve").status_code == 200
        conn = store.connect(path)
        assert store.list_contributions(conn, "approved")[0]["reviewed_by"] == "boss"
