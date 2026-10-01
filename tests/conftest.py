"""pytest 配置：把 coin 的数据库指向临时目录，绝不碰仓库真实 data/coins.db"""
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "scripts"))
sys.path.insert(0, os.path.join(REPO, "web"))

import coin


@pytest.fixture()
def tdb(tmp_path, monkeypatch):
    """每个测试用例一个全新的空数据库"""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(coin, "DB_PATH", db_path)
    coin.init_db()
    conn = coin.get_db()
    yield conn
    conn.close()


@pytest.fixture()
def funded(tdb):
    """admin 有 100000 积分的库"""
    tdb.execute("INSERT INTO accounts (username, balance) VALUES ('admin', 100000)")
    tdb.commit()
    return tdb
