FROM python:3.12-slim

LABEL maintainer="MiAir"
LABEL description="DLNA/AirPlay receiver for Xiaomi AI Speaker"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libportaudio2 \
    dnsutils \
    git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    aiohttp \
    "zeroconf>=0.38.0" \
    "pycryptodome>=3.15.0" \
    miservice-fork \
    av \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY miair.py ./
COPY miair/ ./miair/

RUN mkdir -p /app/conf

EXPOSE 8200 8300

ENTRYPOINT ["python", "miair.py", "--conf-path", "/app/conf"]
