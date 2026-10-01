#!/usr/bin/env python3
"""
Bounty Plaza Web 服务 — 赏金看板 + 自助兑换

赏金流程: 浏览赏金 → 认领 → 提交成果 → 管理员授奖（积分自动到账）→ 积分兑换现金

启动:
    cd web && pip install -r requirements.txt && python app.py

浏览器打开 http://localhost:8080/ 使用看板，/docs 查看 API 文档。

环境变量:
    BOUNTY_ADMIN_TOKEN  管理员接口令牌（创建赏金/授奖/审核兑换必需）
    PORT               监听端口（默认 8080）
"""

import sys
import os
from contextlib import contextmanager
from typing import Optional

# 确保能找到 coin.py（上一级 scripts/ 目录）
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import coin  # 直接导入 coin.py 的模块

ADMIN_TOKEN = os.environ.get("BOUNTY_ADMIN_TOKEN", "")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

app = FastAPI(
    title="Bounty Plaza",
    description="赏金看板：浏览/认领赏金、提交成果；积分自助兑换现金",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── 数据库会话：保证连接一定关闭 ──

@contextmanager
def db():
    conn = coin.get_db()
    try:
        yield conn
    finally:
        conn.close()


def clean_username(username: str) -> str:
    username = (username or "").strip()
    if not username:
        raise HTTPException(status_code=422, detail="用户名不能为空")
    if len(username) > 64:
        raise HTTPException(status_code=422, detail="用户名过长（最多 64 字符）")
    return username


def require_admin(x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")):
    if not ADMIN_TOKEN:
        raise HTTPException(status_code=503, detail="未配置 BOUNTY_ADMIN_TOKEN，管理员接口不可用")
    if x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="管理员令牌无效")


def row_to_bounty(r) -> dict:
    return {
        "id": r["id"],
        "title": r["title"],
        "description": r["description"] or "",
        "tier": r["tier"],
        "reward_coins": r["reward_coins"],
        "reward_usd": round(r["reward_coins"] * coin.RATE, 2),
        "status": r["status"],
        "claimed_by": r["claimed_by"],
        "claimed_at": r["claimed_at"],
        "created_at": r["created_at"],
        "updated_at": r["updated_at"],
    }


def row_to_finding(r) -> dict:
    return {
        "id": r["id"],
        "bounty_id": r["bounty_id"],
        "username": r["username"],
        "title": r["title"],
        "details": r["details"] or "",
        "status": r["status"],
        "created_at": r["created_at"],
    }


# ── 数据模型 ──

class RedeemRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    amount: int = Field(ge=1, le=10_000_000)
    address: str = Field(min_length=1, max_length=256)


class ClaimRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class FindingSubmit(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=200)
    details: str = Field(default="", max_length=20000)


class BountyCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=20000)
    tier: str = Field(default="bronze", pattern="^(bronze|silver|gold|platinum|diamond)$")
    reward_coins: int = Field(gt=0, le=10_000_000)


# ── 页面 ──

@app.get("/", include_in_schema=False)
def index():
    idx = os.path.join(STATIC_DIR, "index.html")
    if not os.path.isfile(idx):
        raise HTTPException(status_code=404, detail="前端尚未构建")
    return FileResponse(idx)


if os.path.isdir(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/health")
def health():
    try:
        with db() as conn:
            conn.execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return {"ok": db_ok, "version": "2.0.0"}


# ── 赏金看板 ──

@app.get("/api/bounties")
def api_list_bounties(
    status: Optional[str] = Query(default=None, pattern="^(open|claimed|paid|closed)$"),
    tier: Optional[str] = Query(default=None, pattern="^(bronze|silver|gold|platinum|diamond)$"),
):
    """赏金列表（可按状态/等级过滤）"""
    try:
        with db() as conn:
            rows = coin.list_bounties(conn, status=status, tier=tier)
            return [row_to_bounty(r) for r in rows]
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


@app.get("/api/bounties/{bounty_id}")
def api_get_bounty(bounty_id: int):
    """赏金详情（含成果列表）"""
    with db() as conn:
        b = coin.get_bounty(conn, bounty_id)
        if not b:
            raise HTTPException(status_code=404, detail="赏金不存在")
        findings = coin.list_findings(conn, bounty_id=bounty_id)
    out = row_to_bounty(b)
    out["findings"] = [row_to_finding(f) for f in findings]
    return out


@app.post("/api/bounties/{bounty_id}/claim")
def api_claim_bounty(bounty_id: int, req: ClaimRequest):
    """认领赏金（原子操作，先到先得）"""
    username = clean_username(req.username)
    with db() as conn:
        ok, reason = coin.claim_bounty(conn, bounty_id, username)
    if not ok:
        raise HTTPException(status_code=409, detail=reason)
    return {"ok": True, "bounty_id": bounty_id, "claimed_by": username}


@app.post("/api/bounties/{bounty_id}/findings", status_code=201)
def api_submit_finding(bounty_id: int, req: FindingSubmit):
    """提交成果"""
    username = clean_username(req.username)
    with db() as conn:
        try:
            fid = coin.submit_finding(conn, bounty_id, username, req.title.strip(), req.details)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "finding_id": fid, "status": "submitted"}


# ── 管理员 ──

@app.post("/api/admin/bounties", status_code=201)
def api_create_bounty(req: BountyCreate, x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")):
    """创建赏金（管理员）"""
    require_admin(x_admin_token)
    with db() as conn:
        try:
            bid = coin.create_bounty(conn, req.title.strip(), req.description, req.tier, req.reward_coins)
        except ValueError as e:
            raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "bounty_id": bid}


@app.post("/api/admin/bounties/{bounty_id}/close")
def api_close_bounty(bounty_id: int, x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")):
    """关闭赏金（管理员）"""
    require_admin(x_admin_token)
    with db() as conn:
        ok = coin.close_bounty(conn, bounty_id)
    if not ok:
        raise HTTPException(status_code=409, detail="赏金不存在或已结束")
    return {"ok": True}


@app.get("/api/admin/findings")
def api_list_findings(
    status: Optional[str] = Query(default=None, pattern="^(submitted|accepted|rejected)$"),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """成果列表（管理员评审用）"""
    require_admin(x_admin_token)
    with db() as conn:
        rows = coin.list_findings(conn, status=status)
    return [row_to_finding(r) for r in rows]


@app.post("/api/admin/findings/{finding_id}/award")
def api_award_finding(finding_id: int, x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")):
    """授奖：接受成果、发放积分、赏金置为 paid（原子+幂等，管理员）"""
    require_admin(x_admin_token)
    with db() as conn:
        ok, info = coin.award_finding(conn, finding_id)
    if not ok:
        raise HTTPException(status_code=409, detail=info)
    if info == "already_awarded":
        return {"ok": True, "idempotent": True, "message": "已授奖（重复调用，无重复发放）"}
    return {"ok": True, "finding_id": finding_id, **info}


@app.get("/api/admin/redeems")
def api_list_redeems(
    status: Optional[str] = Query(default=None, pattern="^(pending|approved|rejected|paid)$"),
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
):
    """兑换请求列表（管理员）"""
    require_admin(x_admin_token)
    with db() as conn:
        q = "SELECT * FROM redeem_requests"
        params = []
        if status:
            q += " WHERE status = ?"
            params.append(status)
        q += " ORDER BY id DESC LIMIT 100"
        rows = conn.execute(q, params).fetchall()
    return [
        {
            "id": r["id"], "username": r["username"], "amount_coins": r["amount"],
            "cash_usd": round(r["coin_value"] or 0, 2), "address": r["address"],
            "status": r["status"], "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.post("/api/admin/redeems/{redeem_id}/approve")
def api_approve_redeem(redeem_id: int, x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")):
    """批准兑换（管理员，原子扣款）"""
    require_admin(x_admin_token)
    with db() as conn:
        ok, info = coin.approve_redeem(conn, redeem_id)
    if not ok:
        raise HTTPException(status_code=409, detail=info)
    return {"ok": True, "redeem_id": redeem_id, **info}


@app.post("/api/admin/redeems/{redeem_id}/reject")
def api_reject_redeem(redeem_id: int, x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")):
    """拒绝兑换（管理员）"""
    require_admin(x_admin_token)
    with db() as conn:
        ok, reason = coin.reject_redeem(conn, redeem_id)
    if not ok:
        raise HTTPException(status_code=409, detail=reason)
    return {"ok": True}


@app.post("/api/admin/redeems/{redeem_id}/pay")
def api_pay_redeem(redeem_id: int, x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token")):
    """标记已打款（管理员，原子防重）"""
    require_admin(x_admin_token)
    with db() as conn:
        ok, info = coin.mark_redeem_paid(conn, redeem_id)
    if not ok:
        raise HTTPException(status_code=409, detail=info)
    return {"ok": True, "redeem_id": redeem_id, **info}


# ── 兑换（用户自助） ──

@app.get("/balance/{username}")
def get_balance(username: str):
    """查询余额和折合现金"""
    username = clean_username(username)
    with db() as conn:
        balance = coin.get_balance(conn, username)
    return {
        "username": username,
        "balance_coins": balance,
        "cash_usd": round(balance * coin.RATE, 2),
        "rate": coin.RATE,
    }


@app.post("/redeem")
def create_redeem(req: RedeemRequest):
    """自助兑换：自动校验并批准（原子防双花）"""
    username = clean_username(req.username)
    address = req.address.strip()
    if not address:
        raise HTTPException(status_code=422, detail="收款地址不能为空")
    if req.amount < coin.MIN_REDEEM:
        raise HTTPException(status_code=400, detail=f"最低兑换 {coin.MIN_REDEEM} 积分币")

    with db() as conn:
        balance = coin.get_balance(conn, username)
        if balance < req.amount:
            raise HTTPException(status_code=400, detail=f"余额不足（{balance} < {req.amount}）")

        cash_value = req.amount * coin.RATE
        coin.ensure_account(conn, username)
        cur = conn.execute(
            "INSERT INTO redeem_requests (username, amount, coin_value, address, status) VALUES (?,?,?,?,'pending')",
            (username, req.amount, cash_value, address)
        )
        req_id = cur.lastrowid

        # 自动批准 & 原子扣余额（balance >= amount 守卫防双花）
        prev_hash = coin.get_last_hash(conn)
        tx_data = {
            "tx_type": "redeem", "from_user": username, "to_user": None,
            "amount": req.amount, "reason": f"自助兑换 #{req_id}", "prev_hash": prev_hash,
        }
        tx_data["hash"] = coin.compute_hash(tx_data)
        conn.execute(
            "INSERT INTO transactions (tx_type, from_user, amount, reason, prev_hash, hash, status) VALUES (?,?,?,?,?,?,'approved')",
            ("redeem", username, req.amount, f"自助兑换 #{req_id}", tx_data["prev_hash"], tx_data["hash"])
        )
        cur = conn.execute(
            "UPDATE accounts SET balance = balance - ? WHERE username = ? AND balance >= ?",
            (req.amount, username, req.amount)
        )
        if cur.rowcount == 0:
            conn.rollback()
            raise HTTPException(status_code=400, detail="余额不足（并发冲突）")
        conn.execute("UPDATE redeem_requests SET status = 'approved', updated_at = datetime('now') WHERE id = ?", (req_id,))
        conn.commit()

    return {
        "redeem_id": req_id,
        "username": username,
        "amount_coins": req.amount,
        "cash_usd": round(cash_value, 2),
        "address": address,
        "status": "approved",
        "message": f"兑换 #{req_id} 已自动批准，等待管理员打款",
    }


@app.get("/redeem/{redeem_id}")
def get_redeem_status(redeem_id: int):
    """查询兑换状态"""
    with db() as conn:
        row = conn.execute("SELECT * FROM redeem_requests WHERE id = ?", (redeem_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="兑换请求不存在")

    return {
        "id": row["id"],
        "username": row["username"],
        "amount_coins": row["amount"],
        "cash_usd": round(row["coin_value"] or 0, 2),
        "address": row["address"],
        "status": row["status"],
        "created_at": row["created_at"],
    }


@app.get("/history/{username}")
def get_history(username: str):
    """查询兑换历史"""
    username = clean_username(username)
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM redeem_requests WHERE username = ? ORDER BY id DESC LIMIT 20",
            (username,)
        ).fetchall()
    return [
        {
            "id": r["id"],
            "amount_coins": r["amount"],
            "cash_usd": round(r["coin_value"] or 0, 2),
            "address": r["address"],
            "status": r["status"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


@app.get("/ledger")
def get_ledger():
    """排行榜"""
    with db() as conn:
        rows = conn.execute(
            "SELECT username, balance FROM accounts WHERE balance > 0 ORDER BY balance DESC"
        ).fetchall()
    return [
        {
            "rank": i + 1,
            "username": r["username"],
            "balance_coins": r["balance"],
            "cash_usd": round(r["balance"] * coin.RATE, 2),
        }
        for i, r in enumerate(rows)
    ]


@app.get("/config")
def get_config():
    """系统配置和汇率"""
    return {
        "rate": coin.RATE,
        "rate_label": f"1 积分 = ${coin.RATE:.2f} USD",
        "min_redeem": coin.MIN_REDEEM,
        "min_redeem_label": f"最低兑换 {coin.MIN_REDEEM} 积分币",
        "tiers": list(coin.TIERS),
    }


if __name__ == "__main__":
    import uvicorn
    coin.init_db()
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(app, host="0.0.0.0", port=port)
