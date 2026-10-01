"""web/app.py API 测试"""
import pytest
from fastapi.testclient import TestClient

import coin
import app as webapp


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(coin, "DB_PATH", str(tmp_path / "test.db"))
    monkeypatch.setattr(webapp, "ADMIN_TOKEN", "test-admin-token")
    coin.init_db()
    conn = coin.get_db()
    conn.execute("INSERT INTO accounts (username, balance) VALUES ('admin', 100000)")
    conn.commit()
    conn.close()
    return TestClient(webapp.app)


def admin(h=None):
    hdrs = {"X-Admin-Token": "test-admin-token"}
    if h:
        hdrs.update(h)
    return hdrs


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_bounty_full_flow(client):
    # 未带 token 创建 → 403
    r = client.post("/api/admin/bounties", json={"title": "T", "tier": "gold", "reward_coins": 500})
    assert r.status_code == 403
    # 错误 token → 403
    r = client.post("/api/admin/bounties", json={"title": "T", "tier": "gold", "reward_coins": 500},
                    headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 403
    # 正确创建
    r = client.post("/api/admin/bounties",
                    json={"title": "修登录 Bug", "description": "desc", "tier": "gold", "reward_coins": 500},
                    headers=admin())
    assert r.status_code == 201
    bid = r.json()["bounty_id"]

    # 列表可见
    r = client.get("/api/bounties")
    assert r.status_code == 200 and len(r.json()) == 1
    assert r.json()[0]["reward_usd"] == round(500 * coin.RATE, 2)

    # 认领
    r = client.post(f"/api/bounties/{bid}/claim", json={"username": "alice"})
    assert r.status_code == 200
    # 重复认领 → 409
    r = client.post(f"/api/bounties/{bid}/claim", json={"username": "bob"})
    assert r.status_code == 409

    # 提交成果
    r = client.post(f"/api/bounties/{bid}/findings",
                    json={"username": "alice", "title": "修好了", "details": "PR #1"})
    assert r.status_code == 201
    fid = r.json()["finding_id"]

    # 详情含 findings
    r = client.get(f"/api/bounties/{bid}")
    assert len(r.json()["findings"]) == 1

    # 授奖
    r = client.post(f"/api/admin/findings/{fid}/award", headers=admin())
    assert r.status_code == 200
    assert r.json()["reward_coins"] == 500

    # 余额到账
    r = client.get("/balance/alice")
    assert r.json()["balance_coins"] == 500

    # 幂等：重复授奖
    r = client.post(f"/api/admin/findings/{fid}/award", headers=admin())
    assert r.status_code == 200 and r.json().get("idempotent") is True
    r = client.get("/balance/alice")
    assert r.json()["balance_coins"] == 500


def test_redeem_flow(client):
    # 先给 alice 发积分：走赏金授奖
    r = client.post("/api/admin/bounties", json={"title": "B", "tier": "bronze", "reward_coins": 100},
                    headers=admin())
    bid = r.json()["bounty_id"]
    fid = client.post(f"/api/bounties/{bid}/findings",
                      json={"username": "alice", "title": "done"}).json()["finding_id"]
    client.post(f"/api/admin/findings/{fid}/award", headers=admin())

    # 自助兑换
    r = client.post("/redeem", json={"username": "alice", "amount": 40, "address": "alice@paypal"})
    assert r.status_code == 200
    rid = r.json()["redeem_id"]
    assert r.json()["status"] == "approved"

    # 超额兑换 → 400
    r = client.post("/redeem", json={"username": "alice", "amount": 9999, "address": "x"})
    assert r.status_code == 400

    # 查询状态
    r = client.get(f"/redeem/{rid}")
    assert r.json()["status"] == "approved"

    # 管理员标记已打款
    r = client.post(f"/api/admin/redeems/{rid}/pay", headers=admin())
    assert r.status_code == 200
    # 重复标记 → 409
    r = client.post(f"/api/admin/redeems/{rid}/pay", headers=admin())
    assert r.status_code == 409


def test_redeem_validation(client):
    assert client.post("/redeem", json={"username": "alice", "amount": 0, "address": "x"}).status_code == 422
    assert client.post("/redeem", json={"username": "", "amount": 1, "address": "x"}).status_code == 422
    assert client.post("/redeem", json={"username": "alice", "amount": 1, "address": ""}).status_code == 422
    assert client.post("/redeem", json={"username": "alice", "amount": 1}).status_code == 422


def test_bounty_validation(client):
    # 非法 tier 被 pydantic 拦截
    r = client.post("/api/admin/bounties", json={"title": "T", "tier": "wood", "reward_coins": 10},
                    headers=admin())
    assert r.status_code == 422
    # 奖励为 0
    r = client.post("/api/admin/bounties", json={"title": "T", "tier": "bronze", "reward_coins": 0},
                    headers=admin())
    assert r.status_code == 422
    # 不存在的赏金
    assert client.get("/api/bounties/99999").status_code == 404
    assert client.post("/api/bounties/99999/claim", json={"username": "x"}).status_code == 409


def test_admin_redeem_approve_reject(client):
    conn = coin.get_db()
    conn.execute("INSERT INTO accounts (username, balance) VALUES ('carol', 50)")
    cur = conn.execute(
        "INSERT INTO redeem_requests (username, amount, coin_value, address, status) VALUES (?,?,?,?, 'pending')",
        ("carol", 20, 20 * coin.RATE, "c@x"),
    )
    rid = cur.lastrowid
    conn.commit()
    conn.close()

    r = client.post(f"/api/admin/redeems/{rid}/approve", headers=admin())
    assert r.status_code == 200
    r = client.get("/balance/carol")
    assert r.json()["balance_coins"] == 30

    # 再建一个 pending 并拒绝
    conn = coin.get_db()
    cur = conn.execute(
        "INSERT INTO redeem_requests (username, amount, coin_value, address, status) VALUES (?,?,?,?, 'pending')",
        ("carol", 5, 5 * coin.RATE, "c@x"),
    )
    rid2 = cur.lastrowid
    conn.commit()
    conn.close()
    r = client.post(f"/api/admin/redeems/{rid2}/reject", headers=admin())
    assert r.status_code == 200
    # 拒绝后不能再批准
    r = client.post(f"/api/admin/redeems/{rid2}/approve", headers=admin())
    assert r.status_code == 409


def test_ledger_and_config(client):
    r = client.get("/ledger")
    assert r.status_code == 200
    assert any(row["username"] == "admin" for row in r.json())
    r = client.get("/config")
    assert r.json()["tiers"] == ["bronze", "silver", "gold", "platinum", "diamond"]
