"""coin.py 核心逻辑测试：账本、兑换、赏金看板、并发安全"""
import threading

import coin


def _give(conn, user, amount):
    coin.ensure_account(conn, user)
    conn.execute("UPDATE accounts SET balance = balance + ? WHERE username = ?", (amount, user))
    conn.commit()


def test_transfer_atomic(funded):
    _give(funded, "alice", 0)
    coin.ensure_account(funded, "bob")
    funded.execute("UPDATE accounts SET balance=100 WHERE username='admin'")
    funded.commit()
    # 用 CLI 转账函数
    import argparse
    args = argparse.Namespace(from_user="admin", to_user="alice", amount=30, reason="test")
    assert coin.cmd_transfer(args) == 0
    assert coin.get_balance(funded, "admin") == 70
    assert coin.get_balance(funded, "alice") == 30


def test_transfer_insufficient(funded):
    import argparse
    args = argparse.Namespace(from_user="nobody", to_user="alice", amount=50, reason="x")
    assert coin.cmd_transfer(args) == 1
    assert coin.get_balance(funded, "alice") == 0


def test_redeem_auto_flow(funded):
    _give(funded, "alice", 200)
    cur = funded.execute(
        "INSERT INTO redeem_requests (username, amount, coin_value, address, status) VALUES (?,?,?,?, 'pending')",
        ("alice", 80, 80 * coin.RATE, "alice@paypal"),
    )
    rid = cur.lastrowid
    funded.commit()
    ok, info = coin.approve_redeem(funded, rid)
    assert ok, info
    assert coin.get_balance(funded, "alice") == 120
    st = funded.execute("SELECT status FROM redeem_requests WHERE id=?", (rid,)).fetchone()["status"]
    assert st == "approved"
    # 重复批准应失败（幂等保护）
    ok2, _ = coin.approve_redeem(funded, rid)
    assert not ok2


def test_approve_insufficient_balance_stays_pending(funded):
    _give(funded, "alice", 10)
    cur = funded.execute(
        "INSERT INTO redeem_requests (username, amount, coin_value, address, status) VALUES (?,?,?,?, 'pending')",
        ("alice", 9999, 1.0, "x"),
    )
    rid = cur.lastrowid
    funded.commit()
    ok, reason = coin.approve_redeem(funded, rid)
    assert not ok
    assert "余额不足" in reason
    assert coin.get_balance(funded, "alice") == 10
    st = funded.execute("SELECT status FROM redeem_requests WHERE id=?", (rid,)).fetchone()["status"]
    assert st == "pending"


def test_pay_idempotent(funded):
    _give(funded, "alice", 100)
    cur = funded.execute(
        "INSERT INTO redeem_requests (username, amount, coin_value, address, status) VALUES (?,?,?,?, 'approved')",
        ("alice", 40, 40 * coin.RATE, "x"),
    )
    rid = cur.lastrowid
    funded.commit()
    ok, info = coin.mark_redeem_paid(funded, rid)
    assert ok, info
    ok2, reason = coin.mark_redeem_paid(funded, rid)
    assert not ok2  # 第二次必须失败，不能重复标记


def test_pay_race(funded):
    """20 个线程同时标记已打款：只有一个成功"""
    _give(funded, "alice", 100)
    cur = funded.execute(
        "INSERT INTO redeem_requests (username, amount, coin_value, address, status) VALUES (?,?,?,?, 'approved')",
        ("alice", 40, 40 * coin.RATE, "x"),
    )
    rid = cur.lastrowid
    funded.commit()
    wins = []
    lock = threading.Lock()

    def worker():
        conn = coin.get_db()
        try:
            ok, _ = coin.mark_redeem_paid(conn, rid)
            if ok:
                with lock:
                    wins.append(1)
        finally:
            conn.close()

    ts = [threading.Thread(target=worker) for _ in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(wins) == 1, f"expected exactly 1 winner, got {len(wins)}"


def test_bounty_lifecycle(funded):
    conn = funded
    bid = coin.create_bounty(conn, "修 Bug", "desc", "gold", 500)
    bounties = coin.list_bounties(conn)
    assert len(bounties) == 1 and bounties[0]["status"] == "open"

    ok, reason = coin.claim_bounty(conn, bid, "alice")
    assert ok, reason
    # 重复认领失败
    ok2, _ = coin.claim_bounty(conn, bid, "bob")
    assert not ok2

    fid = coin.submit_finding(conn, bid, "alice", "修好了", "PR #1")
    fid2 = coin.submit_finding(conn, bid, "bob", "我也修了", "PR #2")

    ok, info = coin.award_finding(conn, fid)
    assert ok, info
    assert info["username"] == "alice" and info["reward_coins"] == 500
    assert coin.get_balance(conn, "alice") == 500
    # 平台扣款
    assert coin.get_balance(conn, "admin") == 100000 - 500
    # 另一个 finding 自动被拒绝
    assert coin.get_finding(conn, fid2)["status"] == "rejected"
    # bounty 已发放
    assert coin.get_bounty(conn, bid)["status"] == "paid"
    # 幂等：重复授奖不重复发钱
    ok, info = coin.award_finding(conn, fid)
    assert ok and info == "already_awarded"
    assert coin.get_balance(conn, "alice") == 500
    # 已发放的赏金不再接受提交
    try:
        coin.submit_finding(conn, bid, "carol", "晚了", "")
        assert False, "should have raised"
    except ValueError:
        pass


def test_claim_race(funded):
    """30 个线程抢认领同一赏金：只有一个成功"""
    conn = funded
    bid = coin.create_bounty(conn, "抢单", "", "silver", 100)
    wins = []
    lock = threading.Lock()

    def worker(i):
        c = coin.get_db()
        try:
            ok, _ = coin.claim_bounty(c, bid, f"user{i}")
            if ok:
                with lock:
                    wins.append(f"user{i}")
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(30)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(wins) == 1, f"expected 1 winner, got {wins}"
    b = coin.get_bounty(conn, bid)
    assert b["status"] == "claimed" and b["claimed_by"] == wins[0]


def test_award_race_single_payout(funded):
    """两个 finding 并发授奖：只有一个能发放，平台只扣一次"""
    conn = funded
    bid = coin.create_bounty(conn, "并发授奖", "", "gold", 300)
    f1 = coin.submit_finding(conn, bid, "alice", "A", "")
    f2 = coin.submit_finding(conn, bid, "bob", "B", "")
    results = []
    lock = threading.Lock()

    def worker(fid):
        c = coin.get_db()
        try:
            ok, info = coin.award_finding(c, fid)
            with lock:
                results.append((fid, ok, info))
        finally:
            c.close()

    ts = [threading.Thread(target=worker, args=(fid,)) for fid in (f1, f2)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok_count = sum(1 for _, ok, _ in results if ok)
    assert ok_count == 1, results
    # 平台总共只扣了 300
    assert coin.get_balance(conn, "admin") == 100000 - 300
    total_paid = coin.get_balance(conn, "alice") + coin.get_balance(conn, "bob")
    assert total_paid == 300


def test_award_insufficient_platform_balance(tdb):
    """平台余额不足时授奖失败且不产生半吊子状态"""
    bid = coin.create_bounty(tdb, "大额", "", "diamond", 999999)
    fid = coin.submit_finding(tdb, bid, "alice", "A", "")
    ok, reason = coin.award_finding(tdb, fid)
    assert not ok and "余额不足" in reason
    assert coin.get_finding(tdb, fid)["status"] == "submitted"
    assert coin.get_bounty(tdb, bid)["status"] in ("open", "claimed")
    assert coin.get_balance(tdb, "alice") == 0


def test_create_bounty_validation(funded):
    for bad in [dict(title="", reward_coins=10), dict(title="x", reward_coins=0),
                dict(title="x", reward_coins=-5)]:
        try:
            coin.create_bounty(funded, bad["title"], "", "bronze", bad["reward_coins"])
            assert False, f"should raise: {bad}"
        except ValueError:
            pass
    try:
        coin.create_bounty(funded, "x", "", "uranium", 10)
        assert False
    except ValueError:
        pass


def test_seed_demo_bounties_idempotent(funded):
    assert coin.seed_demo_bounties(funded) == 6
    assert coin.seed_demo_bounties(funded) == 0  # 第二次不重复
    assert len(coin.list_bounties(funded)) == 6
