# Bounty Plaza Web 服务 — 赏金看板 + 自助兑换

## 启动

```bash
cd web
pip install -r requirements.txt

# 管理员令牌（创建赏金/授奖/审核兑换必需，生产环境必须设置）
export BOUNTY_ADMIN_TOKEN="换成强随机字符串"

python app.py
```

- 看板 UI：http://localhost:8080/
- Swagger UI：http://localhost:8080/docs
- 健康检查：http://localhost:8080/health

首次启动建议初始化账本（管理员注资 100000 积分 + 写入演示赏金）：

```bash
python scripts/seed.py
python scripts/coin.py seed-bounties
```

## 接口

### 赏金看板（公开）

| 接口 | 说明 |
|------|------|
| `GET /api/bounties?status=&tier=` | 赏金列表（status: open/claimed/paid/closed，tier: bronze/silver/gold/platinum/diamond） |
| `GET /api/bounties/{id}` | 赏金详情（含成果列表） |
| `POST /api/bounties/{id}/claim` | 认领赏金 `{"username"}`（原子，先到先得） |
| `POST /api/bounties/{id}/findings` | 提交成果 `{"username","title","details"}` |

### 管理员（需 `X-Admin-Token` 请求头）

| 接口 | 说明 |
|------|------|
| `POST /api/admin/bounties` | 创建赏金 `{"title","description","tier","reward_coins"}` |
| `POST /api/admin/bounties/{id}/close` | 关闭赏金 |
| `GET /api/admin/findings?status=submitted` | 待评审成果 |
| `POST /api/admin/findings/{id}/award` | 授奖：发放积分 + 賞金置为 paid（原子 + 幂等） |
| `GET /api/admin/redeems?status=pending` | 兑换请求列表 |
| `POST /api/admin/redeems/{id}/approve` | 批准兑换（原子扣款） |
| `POST /api/admin/redeems/{id}/reject` | 拒绝兑换 |
| `POST /api/admin/redeems/{id}/pay` | 标记已打款（原子防重） |

未设置 `BOUNTY_ADMIN_TOKEN` 时管理员接口返回 503。

### 兑换（公开）

| 接口 | 说明 |
|------|------|
| `GET /balance/{username}` | 查询余额和折合现金 |
| `POST /redeem` | 自助兑换（自动审批，原子防双花） |
| `GET /redeem/{id}` | 查询兑换状态 |
| `GET /history/{username}` | 兑换历史 |
| `GET /ledger` | 排行榜 |
| `GET /config` | 汇率 / 起兑额 / 等级列表 |

## 部署

> ⚠️ **Vercel 跑不了这个项目**：它是 Python + SQLite 常驻进程，Vercel 只支持无状态 Serverless 函数。请用下面的任一方案（都需要 Jay 拍板）。

### 方案 A：Render（推荐，最省事）

1. https://dashboard.render.com → New → Web Service → 选择本仓库
2. Build Command：`pip install -r web/requirements.txt`
3. Start Command：`python web/app.py`
4. Environment：`BOUNTY_ADMIN_TOKEN=<强随机串>`（Add Secret File 或 Env Var）
5. Disk：Add Disk → Mount Path `/opt/render/project/src/data`（1GB，SQLite 持久化必需，否则重启丢账本）
6. 部署后打开 `https://<你的服务>.onrender.com/` 即看板

### 方案 B：Railway

1. New Project → Deploy from GitHub → 选仓库
2. Variables：`BOUNTY_ADMIN_TOKEN`；Settings → 添加 Volume 挂载到 `/app/data`
3. Start Command：`python web/app.py`（Railway 自动注入 `PORT`，app.py 已读取）

### 方案 C：Fly.io

```bash
fly launch  # 按提示选 Python
fly volumes create bp_data --size 1  # SQLite 持久化
fly secrets set BOUNTY_ADMIN_TOKEN="强随机串"
fly deploy
```

Dockerfile 已就绪（`web/Dockerfile`），Fly 会直接用它构建。

### 方案 D：自有 VPS（docker compose）

```bash
git clone https://github.com/jaykk99/bounty-plaza.git && cd bounty-plaza
echo "BOUNTY_ADMIN_TOKEN=强随机串" > .env
docker compose up -d --build
# 首次：docker compose exec web python ../scripts/seed.py
```

服务跑在 `127.0.0.1:8080`，前面加 Nginx/Caddy 反代 + HTTPS。

### 部署后必做

1. 打开看板确认能加载 → 管理页填入 `BOUNTY_ADMIN_TOKEN` → 创建一条测试赏金走完认领/提交/授奖全流程
2. `data/coins.db` 定期备份（它是唯一的账本，丢了无法恢复）
