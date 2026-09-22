FROM python:3.14-slim

ENV TZ=Asia/Shanghai

RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ uv
ENV UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY config ./config

RUN uv sync --frozen --no-dev

EXPOSE 5000

CMD ["./.venv/bin/stocking-sheet-sync", "serve"]
