# web/Dockerfile — Bounty Plaza 赏金看板 + 兑换 API 服务镜像
# 构建: docker compose build
# 运行: docker compose up -d
# 注意: Vercel 无法运行本项目（Python + SQLite 常驻进程），请用 Render / Railway / Fly.io / 自有 VPS（见 README 部署章节）
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# 先装依赖（利用层缓存）
COPY web/requirements.txt ./web/requirements.txt
RUN pip install --no-cache-dir -r web/requirements.txt

# 应用代码 + 账本脚本 + 静态前端
COPY web/app.py ./web/app.py
COPY web/static/ ./web/static/
COPY scripts/coin.py ./scripts/coin.py
COPY scripts/seed.py ./scripts/seed.py
COPY BOUNTY_CONFIG.json ./BOUNTY_CONFIG.json

# data 目录通过 volume 挂载（见 docker-compose.yml），此处仅保证目录存在
RUN mkdir -p /app/data && \
    useradd -m -u 10001 appuser && \
    chown -R appuser:appuser /app
USER appuser

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/health', timeout=4)"

WORKDIR /app/web
CMD ["python", "app.py"]
