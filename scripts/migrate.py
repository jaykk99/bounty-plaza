#!/usr/bin/env python3
"""
migrate.py — 数据库迁移工具

替代 coin.py init_db() 中的 CREATE TABLE IF NOT EXISTS，
提供可追溯的版本化迁移。

用法:
    python scripts/migrate.py          # 运行所有待执行的迁移
    python scripts/migrate.py --check   # 查看当前版本和待执行迁移
"""

import argparse
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(REPO_ROOT, "data", "coins.db")

MIGRATIONS = [
    # v1: 初始 schema
    """CREATE TABLE IF NOT EXISTS accounts (
        username    TEXT PRIMARY KEY,
        balance     INTEGER NOT NULL DEFAULT 0,
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
    CREATE INDEX IF NOT EXISTS idx_tx_created ON transactions(created_at);""",

    # v2: 回填旧库中缺失的 created_at / updated_at / coin_value（旧 schema 无 DEFAULT）
    """UPDATE transactions SET created_at = datetime('now') WHERE created_at IS NULL;
    UPDATE accounts SET created_at = datetime('now') WHERE created_at IS NULL;
    UPDATE accounts SET updated_at = datetime('now') WHERE updated_at IS NULL;
    UPDATE redeem_requests SET created_at = datetime('now') WHERE created_at IS NULL;
    UPDATE redeem_requests SET updated_at = datetime('now') WHERE updated_at IS NULL;
    UPDATE redeem_requests SET coin_value = amount * 0.72 WHERE coin_value IS NULL;""",

    # v3: 用带 DEFAULT / CHECK / FK 的正确 schema 重建三张表（旧库表结构缺少约束，
    #     CREATE TABLE IF NOT EXISTS 无法升级已存在的表）。标准 SQLite 重建流程。
    """PRAGMA foreign_keys=OFF;

    CREATE TABLE accounts_new (
        username    TEXT PRIMARY KEY,
        balance     INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    INSERT INTO accounts_new (username, balance, created_at, updated_at)
        SELECT username, balance,
               COALESCE(created_at, datetime('now')),
               COALESCE(updated_at, datetime('now'))
        FROM accounts;
    DROP TABLE accounts;
    ALTER TABLE accounts_new RENAME TO accounts;

    CREATE TABLE transactions_new (
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
    INSERT INTO transactions_new (id, tx_type, from_user, to_user, amount, reason, ref_id, prev_hash, hash, status, created_at)
        SELECT id, COALESCE(tx_type, 'transfer'), from_user, to_user, amount, reason, ref_id,
               COALESCE(prev_hash, '0'), COALESCE(hash, '0'),
               COALESCE(status, 'completed'),
               COALESCE(created_at, datetime('now'))
        FROM transactions;
    DROP TABLE transactions;
    ALTER TABLE transactions_new RENAME TO transactions;

    DROP TABLE IF EXISTS accounts_new;
    DROP TABLE IF EXISTS transactions_new;
    DROP TABLE IF EXISTS redeem_requests_new;

    CREATE TABLE redeem_requests_new (
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
    INSERT INTO redeem_requests_new (id, username, amount, coin_value, address, note, status, created_at, updated_at)
        SELECT id, username, amount, COALESCE(coin_value, amount * 0.72), address,
               COALESCE(note, ''), COALESCE(status, 'pending'),
               COALESCE(created_at, datetime('now')),
               COALESCE(updated_at, datetime('now'))
        FROM redeem_requests;
    DROP TABLE redeem_requests;
    ALTER TABLE redeem_requests_new RENAME TO redeem_requests;

    CREATE INDEX IF NOT EXISTS idx_tx_user ON transactions(from_user, to_user);
    CREATE INDEX IF NOT EXISTS idx_redeem_user ON redeem_requests(username);
    CREATE INDEX IF NOT EXISTS idx_redeem_status ON redeem_requests(status);
    CREATE INDEX IF NOT EXISTS idx_tx_status ON transactions(status);
    CREATE INDEX IF NOT EXISTS idx_tx_created ON transactions(created_at);
    PRAGMA foreign_keys=ON;""",
]


def get_current_version(conn) -> int:
    try:
        cur = conn.execute("SELECT MAX(version) FROM schema_version")
        return cur.fetchone()[0] or 0
    except sqlite3.OperationalError:
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY, applied_at TEXT)")
        return 0


def run_migrations(conn, target: int = None):
    current = get_current_version(conn)
    if target is None:
        target = len(MIGRATIONS)

    for i in range(current, target):
        version = i + 1
        print(f"  Applying migration v{version}...")
        conn.executescript(MIGRATIONS[i])
        from datetime import datetime, timezone
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        print(f"  ✅ v{version} applied")


def main():
    parser = argparse.ArgumentParser(description="Database migration tool")
    parser.add_argument("--check", action="store_true", help="Check current version")
    parser.add_argument("--target", type=int, default=None, help="Target version")
    args = parser.parse_args()

    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    current = get_current_version(conn)

    if args.check:
        print(f"Current version: v{current}")
        print(f"Latest version:  v{len(MIGRATIONS)}")
        pending = len(MIGRATIONS) - current
        if pending > 0:
            print(f"Pending: {pending} migration(s)")
        else:
            print("Up to date")
        return 0

    run_migrations(conn, args.target)
    print(f"Done. Current version: v{get_current_version(conn)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
