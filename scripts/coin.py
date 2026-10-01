#!/usr/bin/env python3
"""
积分币系统 — Coin System
独立账本，SQLite 存储，完整审计日志。
每笔交易防篡改，每笔兑换可追溯。

用法:
    python scripts/coin.py balance <user>
    python scripts/coin.py transfer --from <sender> --to <receiver> --amount <n> --reason "<text>"
    python scripts/coin.py redeem --user <user> --amount <n> --address "<paypal/usdt>"
    python scripts/coin.py approve --id <redeem_id>
    python scripts/coin.py reject --id <redeem_id>
    python scripts/coin.py ledger
    python scripts/coin.py audit
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

DB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
DB_PATH = os.path.join(DB_DIR, "coins.db")

RATE = 0.72            # 1 积分 = 0.72 USD
MIN_REDEEM = 1          # 无最低限制（1积分即可兑换）

# ── 赏金看板 ──
TIERS = ("bronze", "silver", "gold", "platinum", "diamond")
BOUNTY_STATUSES = ("open", "claimed", "paid", "closed")
FINDING_STATUSES = ("submitted", "accepted", "rejected")


def get_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=8000")  # 并发竞争时等待锁而非立刻报错
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS accounts (
            username    TEXT PRIMARY KEY,
            balance     INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS transactions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tx_type     TEXT NOT NULL CHECK(tx_type IN ('transfer','redeem','approve','reject','cancel')),
            from_user   TEXT,
            to_user     TEXT,
            amount      INTEGER NOT NULL,
            reason      TEXT,
            ref_id      TEXT,
            prev_hash   TEXT NOT NULL,
            hash        TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'completed' CHECK(status IN ('completed','pending','approved','rejected','cancelled')),
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (from_user) REFERENCES accounts(username),
            FOREIGN KEY (to_user) REFERENCES accounts(username)
        );

        CREATE TABLE IF NOT EXISTS redeem_requests (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            username    TEXT NOT NULL,
            amount      INTEGER NOT NULL,
            coin_value  REAL NOT NULL,
            address     TEXT NOT NULL,
            note        TEXT DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected','paid')),
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (username) REFERENCES accounts(username)
        );

        CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(from_user, to_user);
        CREATE INDEX IF NOT EXISTS idx_redeem_user ON redeem_requests(username);
        CREATE INDEX IF NOT EXISTS idx_redeem_status ON redeem_requests(status);
        CREATE INDEX IF NOT EXISTS idx_tx_status ON transactions(status);
        CREATE INDEX IF NOT EXISTS idx_tx_created ON transactions(created_at);

        CREATE TABLE IF NOT EXISTS bounties (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            title        TEXT NOT NULL,
            description  TEXT NOT NULL DEFAULT '',
            tier         TEXT NOT NULL CHECK(tier IN ('bronze','silver','gold','platinum','diamond')),
            reward_coins INTEGER NOT NULL CHECK(reward_coins > 0),
            status       TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','claimed','paid','closed')),
            claimed_by   TEXT,
            claimed_at   TEXT,
            created_at   TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS findings (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            bounty_id   INTEGER NOT NULL REFERENCES bounties(id),
            username    TEXT NOT NULL,
            title       TEXT NOT NULL,
            details     TEXT NOT NULL DEFAULT '',
            status      TEXT NOT NULL DEFAULT 'submitted' CHECK(status IN ('submitted','accepted','rejected')),
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            FOREIGN KEY (username) REFERENCES accounts(username)
        );

        CREATE INDEX IF NOT EXISTS idx_bounty_status ON bounties(status);
        CREATE INDEX IF NOT EXISTS idx_bounty_tier ON bounties(tier);
        CREATE INDEX IF NOT EXISTS idx_finding_bounty ON findings(bounty_id);
        CREATE INDEX IF NOT EXISTS idx_finding_status ON findings(status);
    """)
    conn.commit()
    conn.close()


def compute_hash(row: dict) -> str:
    raw = f"{str(row.get('prev_hash') or '')}|{str(row.get('tx_type') or '')}|{str(row.get('from_user') or '')}|{str(row.get('to_user') or '')}|{row.get('amount',0)}|{str(row.get('reason') or '')}"
    return hashlib.sha256(raw.encode()).hexdigest()


def get_last_hash(conn) -> str:
    cur = conn.execute("SELECT hash FROM transactions ORDER BY id DESC LIMIT 1")
    row = cur.fetchone()
    return row["hash"] if row else "0" * 64


def get_balance(conn, username: str) -> int:
    cur = conn.execute("SELECT balance FROM accounts WHERE username = ?", (username,))
    row = cur.fetchone()
    return row["balance"] if row else 0


def ensure_account(conn, username: str):
    cur = conn.execute("SELECT 1 FROM accounts WHERE username = ?", (username,))
    if not cur.fetchone():
        conn.execute("INSERT INTO accounts (username, balance) VALUES (?, 0)", (username,))

def _with_lock_retry(fn, retries=8):
    """SQLite 写竞争导致 database is locked 时指数退避重试。"""
    for i in range(retries):
        try:
            return fn()
        except sqlite3.OperationalError as e:
            if "locked" in str(e).lower() and i < retries - 1:
                time.sleep(0.02 * (2 ** i))
                continue
            raise


# ═══════════════ 赏金看板 ═══════════════

def create_bounty(conn, title: str, description: str = "", tier: str = "bronze",
                  reward_coins: int = 0) -> int:
    title = (title or "").strip()
    if not title:
        raise ValueError("标题不能为空")
    if len(title) > 200:
        raise ValueError("标题过长（最多 200 字符）")
    if tier not in TIERS:
        raise ValueError(f"tier 非法: {tier}，可选 {TIERS}")
    if not isinstance(reward_coins, int) or reward_coins <= 0:
        raise ValueError("reward_coins 必须为正整数")
    cur = conn.execute(
        "INSERT INTO bounties (title, description, tier, reward_coins) VALUES (?,?,?,?)",
        (title, description or "", tier, reward_coins),
    )
    conn.commit()
    return cur.lastrowid


def list_bounties(conn, status: str = None, tier: str = None):
    q = "SELECT * FROM bounties"
    conds, params = [], []
    if status:
        if status not in BOUNTY_STATUSES:
            raise ValueError(f"status 非法: {status}")
        conds.append("status = ?")
        params.append(status)
    if tier:
        if tier not in TIERS:
            raise ValueError(f"tier 非法: {tier}")
        conds.append("tier = ?")
        params.append(tier)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC"
    return conn.execute(q, params).fetchall()


def get_bounty(conn, bounty_id: int):
    return conn.execute("SELECT * FROM bounties WHERE id = ?", (bounty_id,)).fetchone()


def claim_bounty(conn, bounty_id: int, username: str):
    """认领赏金。原子操作：只有 open 的能被认领。返回 (ok, reason)。"""
    username = (username or "").strip()
    if not username:
        return (False, "用户名不能为空")

    def _do():
        conn.execute("BEGIN IMMEDIATE")
        try:
            ensure_account(conn, username)
            cur = conn.execute(
                "UPDATE bounties SET status='claimed', claimed_by=?, claimed_at=datetime('now'),"
                " updated_at=datetime('now') WHERE id=? AND status='open'",
                (username, bounty_id),
            )
            if cur.rowcount == 0:
                conn.rollback()
                b = get_bounty(conn, bounty_id)
                if not b:
                    return (False, "赏金不存在")
                return (False, f"赏金当前状态为 {b['status']}，不可认领")
            conn.commit()
            return (True, "")
        except Exception:
            conn.rollback()
            raise

    return _with_lock_retry(_do)


def close_bounty(conn, bounty_id: int) -> bool:
    """管理员关闭赏金（open/claimed → closed）。"""
    cur = conn.execute(
        "UPDATE bounties SET status='closed', updated_at=datetime('now')"
        " WHERE id=? AND status IN ('open','claimed')",
        (bounty_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def submit_finding(conn, bounty_id: int, username: str, title: str, details: str = "") -> int:
    """提交成果。bounty 必须存在且处于 open/claimed。"""
    username = (username or "").strip()
    title = (title or "").strip()
    if not username:
        raise ValueError("用户名不能为空")
    if not title:
        raise ValueError("成果标题不能为空")
    if len(title) > 200:
        raise ValueError("成果标题过长（最多 200 字符）")
    b = get_bounty(conn, bounty_id)
    if not b:
        raise ValueError("赏金不存在")
    if b["status"] not in ("open", "claimed"):
        raise ValueError(f"赏金已 {b['status']}，不再接受提交")
    ensure_account(conn, username)
    cur = conn.execute(
        "INSERT INTO findings (bounty_id, username, title, details) VALUES (?,?,?,?)",
        (bounty_id, username, title, details or ""),
    )
    conn.commit()
    return cur.lastrowid


def list_findings(conn, bounty_id: int = None, status: str = None):
    q = "SELECT * FROM findings"
    conds, params = [], []
    if bounty_id is not None:
        conds.append("bounty_id = ?")
        params.append(bounty_id)
    if status:
        if status not in FINDING_STATUSES:
            raise ValueError(f"status 非法: {status}")
        conds.append("status = ?")
        params.append(status)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    q += " ORDER BY id DESC"
    return conn.execute(q, params).fetchall()


def get_finding(conn, finding_id: int):
    return conn.execute("SELECT * FROM findings WHERE id = ?", (finding_id,)).fetchone()


def award_finding(conn, finding_id: int):
    """授奖：原子 + 幂等。

    接受一条 finding → 同 bounty 其他 submitted 全部拒绝 →
    从 admin 账户原子扣款并记账（hash 链）→ bounty 置为 paid。
    返回 (True, info)；info == "already_awarded" 表示重复调用（幂等）。
    """
    def _do():
        conn.execute("BEGIN IMMEDIATE")
        try:
            f = get_finding(conn, finding_id)
            if not f:
                conn.rollback()
                return (False, "成果不存在")
            if f["status"] == "accepted":
                conn.rollback()
                return (True, "already_awarded")
            if f["status"] != "submitted":
                conn.rollback()
                return (False, f"成果状态为 {f['status']}，不可授奖")
            b = get_bounty(conn, f["bounty_id"])
            if not b or b["status"] not in ("open", "claimed"):
                conn.rollback()
                return (False, "赏金不存在或不可授奖")

            # 原子翻转：只有 submitted 的行能被接受（防并发双授奖）
            cur = conn.execute(
                "UPDATE findings SET status='accepted' WHERE id=? AND status='submitted'",
                (finding_id,),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return (False, "并发冲突：该成果已被处理")
            conn.execute(
                "UPDATE findings SET status='rejected' WHERE bounty_id=? AND status='submitted' AND id!=?",
                (b["id"], finding_id),
            )

            ensure_account(conn, "admin")
            ensure_account(conn, f["username"])
            # 从平台（admin）原子扣款：余额不足则整笔回滚
            cur = conn.execute(
                "UPDATE accounts SET balance = balance - ? WHERE username='admin' AND balance >= ?",
                (b["reward_coins"], b["reward_coins"]),
            )
            if cur.rowcount == 0:
                conn.rollback()
                return (False, "平台（admin）余额不足，无法发放奖励")

            prev_hash = get_last_hash(conn)
            tx_data = {
                "tx_type": "approve", "from_user": "admin", "to_user": f["username"],
                "amount": b["reward_coins"],
                "reason": f"赏金奖励 #{b['id']}: {b['title'][:60]}",
                "prev_hash": prev_hash,
            }
            tx_data["hash"] = compute_hash(tx_data)
            conn.execute(
                "INSERT INTO transactions (tx_type, from_user, to_user, amount, reason, prev_hash, hash)"
                " VALUES (?,?,?,?,?,?,?)",
                (tx_data["tx_type"], tx_data["from_user"], tx_data["to_user"],
                 tx_data["amount"], tx_data["reason"], tx_data["prev_hash"], tx_data["hash"]),
            )
            conn.execute(
                "UPDATE accounts SET balance = balance + ?, updated_at=datetime('now') WHERE username=?",
                (b["reward_coins"], f["username"]),
            )
            conn.execute(
                "UPDATE bounties SET status='paid', updated_at=datetime('now') WHERE id=?",
                (b["id"],),
            )
            conn.commit()
            return (True, {"username": f["username"], "reward_coins": b["reward_coins"],
                           "bounty_id": b["id"]})
        except Exception:
            conn.rollback()
            raise

    return _with_lock_retry(_do)


def mark_redeem_paid(conn, redeem_id: int):
    """原子标记已打款（防并发双标）。返回 (ok, info/reason)。"""
    def _do():
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute(
                "UPDATE redeem_requests SET status='paid', updated_at=datetime('now')"
                " WHERE id=? AND status='approved'",
                (redeem_id,),
            )
            if cur.rowcount == 0:
                conn.rollback()
                r = conn.execute("SELECT status FROM redeem_requests WHERE id=?", (redeem_id,)).fetchone()
                if not r:
                    return (False, "兑换请求不存在")
                return (False, f"状态为 {r['status']}，不可标记已支付")
            req = conn.execute("SELECT * FROM redeem_requests WHERE id=?", (redeem_id,)).fetchone()
            tx = conn.execute(
                "SELECT id FROM transactions WHERE from_user=? AND amount=? AND tx_type='redeem'"
                " AND status='approved' ORDER BY id DESC LIMIT 1",
                (req["username"], req["amount"]),
            ).fetchone()
            if tx:
                conn.execute("UPDATE transactions SET status='completed' WHERE id=?", (tx["id"],))
            conn.commit()
            return (True, {"username": req["username"], "amount": req["amount"],
                           "cash_value": req["coin_value"]})
        except Exception:
            conn.rollback()
            raise

    return _with_lock_retry(_do)


def seed_demo_bounties(conn) -> int:
    """演示赏金：仅当 bounties 表为空时写入。返回写入条数。"""
    if conn.execute("SELECT COUNT(*) FROM bounties").fetchone()[0] > 0:
        return 0
    demos = [
        ("修复登录页移动端错位", "登录表单在 <480px 宽度下按钮溢出，需 CSS 修复并附截图。", "bronze", 50),
        ("为 coin.py 添加并发兑换测试", "用线程模拟 20 并发兑换，验证无双花。PR 需含测试代码。", "silver", 150),
        ("实现赏金看板深色模式", "全站深色主题，含 localStorage 记忆。", "silver", 200),
        ("auto_payout 增加 Prometheus 指标", "/metrics 暴露待打款数、成功数、失败数。", "gold", 400),
        ("设计赏金广场品牌 Logo", "SVG + PNG 导出，附三版配色方案。", "gold", 350),
        ("审计全库 SQL 注入面", "输出审计报告并修复发现的问题，PoC 必备。", "platinum", 800),
    ]
    n = 0
    for title, desc, tier, reward in demos:
        create_bounty(conn, title, desc, tier, reward)
        n += 1
    return n



def cmd_balance(args):
    conn = get_db()
    if args.init:
        ensure_account(conn, "admin")
        cur = conn.execute("SELECT balance FROM accounts WHERE username = 'admin'")
        bal = cur.fetchone()[0]
        if bal == 0:
            conn.execute("UPDATE accounts SET balance = 100000, updated_at = datetime('now') WHERE username = 'admin'")
            conn.commit()
            bal = 100000
            print("✅ Admin account initialized with 100,000 coins")
        else:
            print(f"Admin account ready (balance: {bal} coins)")
    balance = get_balance(conn, args.user)
    conn.close()
    cash = balance * RATE
    print(f"{args.user}: {balance} 积分币 = ${cash:.2f}")
    return 0


def cmd_transfer(args):
    conn = get_db()
    try:
        ensure_account(conn, args.from_user)
        ensure_account(conn, args.to_user)

        # 原子扣款：balance >= amount 时才扣，避免竞态双花
        cur = conn.execute(
            "UPDATE accounts SET balance = balance - ? WHERE username = ? AND balance >= ?",
            (args.amount, args.from_user, args.amount)
        )
        if cur.rowcount == 0:
            print(f"ERROR: {args.from_user} 余额不足或并发冲突")
            return 1

        prev_hash = get_last_hash(conn)
        tx_data = {
            "tx_type": "transfer", "from_user": args.from_user,
            "to_user": args.to_user, "amount": args.amount,
            "reason": args.reason, "prev_hash": prev_hash,
        }
        tx_data["hash"] = compute_hash(tx_data)

        conn.execute(
            "INSERT INTO transactions (tx_type, from_user, to_user, amount, reason, prev_hash, hash) VALUES (?,?,?,?,?,?,?)",
            (tx_data["tx_type"], tx_data["from_user"], tx_data["to_user"],
             tx_data["amount"], tx_data["reason"], tx_data["prev_hash"], tx_data["hash"])
        )
        conn.execute("UPDATE accounts SET balance = balance + ? WHERE username = ?", (args.amount, args.to_user))
        conn.commit()
        print(f"✅ 转账成功: {args.from_user} → {args.to_user} 共 {args.amount} 积分币")
        print(f"   原因: {args.reason}")
    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}")
        return 1
    finally:
        conn.close()
    return 0


def cmd_redeem(args):
    conn = get_db()
    try:
        balance = get_balance(conn, args.user)
        if balance < args.amount:
            print(f"ERROR: {args.user} 余额不足（{balance} < {args.amount}）")
            return 1
        if args.amount < 1:  # 无最低限制
            print(f"ERROR: 金额必须大于 0")
            return 1

        cash_value = args.amount * RATE
        ensure_account(conn, args.user)
        cur = conn.execute(
            "INSERT INTO redeem_requests (username, amount, coin_value, address, status) VALUES (?,?,?,?,'pending')",
            (args.user, args.amount, cash_value, args.address)
        )
        req_id = cur.lastrowid

        if args.auto:
            prev_hash = get_last_hash(conn)
            tx_data = {"tx_type": "redeem", "from_user": args.user, "to_user": None,
                       "amount": args.amount, "reason": f"自助兑换 #{req_id}", "prev_hash": prev_hash}
            tx_data["hash"] = compute_hash(tx_data)
            conn.execute(
                "INSERT INTO transactions (tx_type, from_user, amount, reason, prev_hash, hash, status) VALUES (?,?,?,?,?,?,'approved')",
                ("redeem", args.user, args.amount, f"自助兑换 #{req_id}", tx_data["prev_hash"], tx_data["hash"])
            )
            conn.execute("UPDATE accounts SET balance = balance - ? WHERE username = ? AND balance >= ?", (args.amount, args.user, args.amount))
            conn.execute("UPDATE redeem_requests SET status = 'approved', updated_at = datetime('now') WHERE id = ?", (req_id,))
            conn.commit()
            if args.json:
                import json as _json
                print(_json.dumps({"ok": True, "id": req_id, "username": args.user, "amount": args.amount, "cash_value": round(cash_value, 2), "address": args.address, "status": "approved", "balance_remaining": balance - args.amount}))
            else:
                print(f"✅ 自助兑换成功: {args.user} 兑换 {args.amount} 积分币 = ${cash_value:.2f}")
                print(f"   收款地址: {args.address}")
                rid_label = f"兑换号: #{req_id}"; print(f"   {rid_label}，已自动批准，管理员请尽快打款")
        else:
            conn.commit()
            print(f"✅ 兑换申请已提交: {args.user} 兑换 {args.amount} 积分币 = ${cash_value:.2f}")
            print(f"   收款地址: {args.address}")
            print(f"   等待管理员审核（ID: {req_id}）")
    except Exception as e:
        conn.rollback()
        print(f"ERROR: {e}")
        return 1
    finally:
        conn.close()
    return 0

def approve_redeem(conn, redeem_id: int):
    """批准兑换请求：先原子扣款再落账。返回 (ok, info/reason)。

    info 成功时为 {"username","amount","cash_value","address"}。
    """
    def _do():
        conn.execute("BEGIN IMMEDIATE")
        try:
            cur = conn.execute("SELECT * FROM redeem_requests WHERE id = ?", (redeem_id,))
            req = cur.fetchone()
            if not req or req["status"] != "pending":
                conn.rollback()
                return (False, "兑换请求不存在或已被处理")
            username, amount = req["username"], req["amount"]
            cur2 = conn.execute(
                "UPDATE accounts SET balance = balance - ? WHERE username = ? AND balance >= ?",
                (amount, username, amount),
            )
            if cur2.rowcount == 0:
                conn.rollback()
                return (False, f"{username} 余额不足")
            prev_hash = get_last_hash(conn)
            tx_data = {
                "tx_type": "redeem", "from_user": username, "to_user": None,
                "amount": amount, "reason": f"兑换请求 #{redeem_id}", "prev_hash": prev_hash,
            }
            tx_data["hash"] = compute_hash(tx_data)
            conn.execute(
                "INSERT INTO transactions (tx_type, from_user, amount, reason, prev_hash, hash, status)"
                " VALUES (?,?,?,?,?,?,'approved')",
                ("redeem", username, amount, f"兑换请求 #{redeem_id}",
                 tx_data["prev_hash"], tx_data["hash"]),
            )
            conn.execute(
                "UPDATE redeem_requests SET status = 'approved', updated_at = datetime('now') WHERE id = ?",
                (redeem_id,),
            )
            conn.commit()
            return (True, {"username": username, "amount": amount,
                           "cash_value": amount * RATE, "address": req["address"]})
        except Exception:
            conn.rollback()
            raise

    return _with_lock_retry(_do)


def reject_redeem(conn, redeem_id: int):
    """拒绝兑换请求（仅 pending）。返回 (ok, reason)。"""
    cur = conn.execute(
        "UPDATE redeem_requests SET status='rejected', updated_at=datetime('now')"
        " WHERE id=? AND status='pending'",
        (redeem_id,),
    )
    conn.commit()
    if cur.rowcount == 0:
        r = conn.execute("SELECT status FROM redeem_requests WHERE id=?", (redeem_id,)).fetchone()
        return (False, "兑换请求不存在" if not r else f"状态为 {r['status']}，不可拒绝")
    return (True, "")


def cmd_approve(args):
    conn = get_db()
    try:
        ok, info = approve_redeem(conn, args.id)
        if not ok:
            print(f"ERROR: {info}")
            return 1
        cash = info["cash_value"]
        print(f"✅ 兑换 #{args.id} 已批准: {info['username']} 获得 ${cash:.2f}")
        print(f"   打款地址: {info['address']}")
        print(f"   请尽快打款并在打款后标记为已支付: python scripts/coin.py pay --id {args.id}")
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        conn.close()
    return 0


def cmd_pay(args):
    conn = get_db()
    try:
        ok, info = mark_redeem_paid(conn, args.id)
        if not ok:
            if getattr(args, 'json', False):
                import json as _json
                print(_json.dumps({"ok": False, "error": info, "id": args.id}))
            else:
                print(f"ERROR: {info}")
            return 1
        if getattr(args, 'json', False):
            import json as _json
            print(_json.dumps({"ok": True, "id": args.id, "username": info["username"],
                               "amount": info["amount"], "cash_value": round(info["cash_value"], 2),
                               "status": "paid"}))
        else:
            print(f"✅ 兑换 #{args.id} 已标记为已支付: {info['username']} ${info['cash_value']:.2f}")
    except Exception as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        conn.close()
    return 0


def cmd_reject(args):
    conn = get_db()
    try:
        ok, reason = reject_redeem(conn, args.id)
    finally:
        conn.close()
    if not ok:
        print(f"ERROR: {reason}")
        return 1
    print(f"✅ 兑换 #{args.id} 已拒绝")
    return 0


def cmd_ledger(args):
    conn = get_db()
    cur = conn.execute("SELECT username, balance FROM accounts WHERE balance > 0 ORDER BY balance DESC")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print("暂无数据")
        return 0
    print(f"{'排名':>4} | {'用户名':<20} | {'积分币':>8} | {'现金价值':>8}")
    print("-" * 50)
    for i, row in enumerate(rows, 1):
        cash = row["balance"] * RATE
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, "")
        print(f"{medal}{i:>3} | {row['username']:<20} | {row['balance']:>8} | ${cash:<6.2f}")
    return 0


def cmd_audit(args):
    conn = get_db()
    cur = conn.execute("SELECT * FROM transactions ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print("暂无交易记录")
        return 0
    for row in rows:
        print(f"#{row['id']:>4} {row['tx_type']:<8} {row['from_user'] or '':<15} → {row['to_user'] or '':<15} {row['amount']:>8} [{row['status']:<9}] {(row['created_at'] or '')[:19]}")
        if args.verbose:
            print(f"      hash: {row['hash'][:20]}...  prev: {row['prev_hash'][:20]}...")
    return 0


def cmd_history(args):
    conn = get_db()
    cur = conn.execute("SELECT * FROM redeem_requests ORDER BY id DESC LIMIT 50")
    rows = cur.fetchall()
    conn.close()
    if not rows:
        print("暂无兑换记录")
        return 0
    for row in rows:
        print(f"#{row['id']:>4} {row['username']:<20} {row['amount']:>8}积分币 = ${(row['coin_value'] or 0):<6.2f} [{row['status']:<8}] {(row['address'] or '')[:30]:<30} {(row['created_at'] or '')[:19]}")
    return 0


def cmd_bounty_create(args):
    conn = get_db()
    try:
        bid = create_bounty(conn, args.title, args.desc or "", args.tier, args.reward)
        print(f"✅ 赏金 #{bid} 已创建: [{args.tier}] {args.title}（{args.reward} 积分币）")
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        conn.close()
    return 0


def cmd_bounty_list(args):
    conn = get_db()
    try:
        rows = list_bounties(conn, status=args.status, tier=args.tier)
    finally:
        conn.close()
    if not rows:
        print("暂无赏金")
        return 0
    print(f"{'ID':>4} | {'等级':<8} | {'状态':<8} | {'奖励':>6} | 标题")
    print("-" * 70)
    for r in rows:
        claimed = f"（{r['claimed_by']}）" if r["claimed_by"] else ""
        print(f"{r['id']:>4} | {r['tier']:<8} | {r['status']:<8} | {r['reward_coins']:>6} | {r['title']}{claimed}")
    return 0


def cmd_bounty_claim(args):
    conn = get_db()
    try:
        ok, reason = claim_bounty(conn, args.id, args.user)
    finally:
        conn.close()
    if not ok:
        print(f"ERROR: {reason}")
        return 1
    print(f"✅ {args.user} 已认领赏金 #{args.id}")
    return 0


def cmd_finding_submit(args):
    conn = get_db()
    try:
        fid = submit_finding(conn, args.bounty, args.user, args.title, args.details or "")
        print(f"✅ 成果 #{fid} 已提交到赏金 #{args.bounty}，等待评审")
    except ValueError as e:
        print(f"ERROR: {e}")
        return 1
    finally:
        conn.close()
    return 0


def cmd_finding_award(args):
    conn = get_db()
    try:
        ok, info = award_finding(conn, args.id)
    finally:
        conn.close()
    if not ok:
        print(f"ERROR: {info}")
        return 1
    if info == "already_awarded":
        print(f"ℹ️ 成果 #{args.id} 已授奖（幂等，无重复发放）")
    else:
        print(f"✅ 已授奖: {info['username']} 获得 {info['reward_coins']} 积分币（赏金 #{info['bounty_id']}）")
    return 0


def cmd_seed_bounties(args):
    conn = get_db()
    try:
        n = seed_demo_bounties(conn)
    finally:
        conn.close()
    print(f"✅ 已写入 {n} 条演示赏金" if n else "ℹ️ 赏金表非空，跳过")
    return 0


def main():
    init_db()

    parser = argparse.ArgumentParser(description="积分币系统 — Coin System")
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("balance", help="查询余额")
    p.add_argument("user")
    p.add_argument("--init", action="store_true", help="初始化 Admin 账户并注入初始积分")

    p = sub.add_parser("transfer", help="转账")
    p.add_argument("--from", dest="from_user", required=True)
    p.add_argument("--to", dest="to_user", required=True)
    p.add_argument("--amount", type=int, required=True)
    p.add_argument("--reason", default="")

    p = sub.add_parser("redeem", help="发起兑换")
    p.add_argument("--user", required=True)
    p.add_argument("--amount", type=int, required=True)
    p.add_argument("--address", required=True, help="PayPal 邮箱或 USDT 地址")
    p.add_argument("--auto", action="store_true", help="自助模式：自动批准兑换")
    p.add_argument("--json", action="store_true", help="JSON 格式输出")
    p.add_argument("--note", default="", help="备注")

    p = sub.add_parser("approve", help="批准兑换")
    p.add_argument("--id", type=int, required=True)

    p = sub.add_parser("pay", help="标记已打款")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--json", action="store_true", help="JSON 格式输出")
    p.add_argument("--note", default="", help="备注")

    p = sub.add_parser("reject", help="拒绝兑换")
    p.add_argument("--id", type=int, required=True)

    p = sub.add_parser("ledger", help="查看排行榜")
    p = sub.add_parser("audit", help="审计日志")
    p.add_argument("--verbose", action="store_true")
    p = sub.add_parser("history", help="兑换历史")

    p = sub.add_parser("bounty-create", help="创建赏金（管理员）")
    p.add_argument("--title", required=True)
    p.add_argument("--desc", default="")
    p.add_argument("--tier", default="bronze", choices=list(TIERS))
    p.add_argument("--reward", type=int, required=True)

    p = sub.add_parser("bounty-list", help="列出赏金")
    p.add_argument("--status", default=None, choices=list(BOUNTY_STATUSES))
    p.add_argument("--tier", default=None, choices=list(TIERS))

    p = sub.add_parser("bounty-claim", help="认领赏金")
    p.add_argument("--id", type=int, required=True)
    p.add_argument("--user", required=True)

    p = sub.add_parser("finding-submit", help="提交成果")
    p.add_argument("--bounty", type=int, required=True)
    p.add_argument("--user", required=True)
    p.add_argument("--title", required=True)
    p.add_argument("--details", default="")

    p = sub.add_parser("finding-award", help="授奖（管理员）")
    p.add_argument("--id", type=int, required=True)

    p = sub.add_parser("seed-bounties", help="写入演示赏金（仅空表时）")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 1

    commands = {
        "balance": cmd_balance,
        "transfer": cmd_transfer,
        "redeem": cmd_redeem,
        "approve": cmd_approve,
        "pay": cmd_pay,
        "reject": cmd_reject,
        "ledger": cmd_ledger,
        "audit": cmd_audit,
        "history": cmd_history,
        "bounty-create": cmd_bounty_create,
        "bounty-list": cmd_bounty_list,
        "bounty-claim": cmd_bounty_claim,
        "finding-submit": cmd_finding_submit,
        "finding-award": cmd_finding_award,
        "seed-bounties": cmd_seed_bounties,
    }
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
