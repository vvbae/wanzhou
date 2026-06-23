# 中文开放书目 API —— 容器镜像
FROM python:3.12-slim

# uv（包管理）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# 先装依赖，利用层缓存
COPY pyproject.toml uv.lock ./
COPY cnbib ./cnbib
COPY make_admin.py enrich.py recompute_cjk.py ./
RUN uv sync --frozen --no-dev

# 数据库放持久卷挂载点（容器内的 /data，由平台挂卷进来）
ENV CNBIB_DB=/data/cnbib.db
RUN mkdir -p /data

EXPOSE 8080
# 监听 0.0.0.0 才能从容器外访问；端口与平台 internal_port 一致
CMD ["uv", "run", "--no-dev", "uvicorn", "cnbib.api:app", "--host", "0.0.0.0", "--port", "8080"]
