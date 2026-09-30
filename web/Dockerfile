# web/Dockerfile — Bounty Plaza 兑换 API 服务镜像
# 构建: docker compose build
# 运行: docker compose up -d
FROM python:3.11-slim

WORKDIR /app

# 先装依赖（利用层缓存）
COPY web/requirements.txt ./web/requirements.txt
RUN pip install --no-cache-dir -r web/requirements.txt

# 应用代码 + 账本脚本 + 数据目录
COPY web/app.py ./web/app.py
COPY scripts/coin.py ./scripts/coin.py
COPY BOUNTY_CONFIG.json ./BOUNTY_CONFIG.json

# data 目录通过 volume 挂载（见 docker-compose.yml），此处仅保证目录存在
RUN mkdir -p /app/data

EXPOSE 8080

WORKDIR /app/web
CMD ["python", "app.py"]
