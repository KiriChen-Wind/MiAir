FROM python:3.12-slim

LABEL maintainer="MiAir"
LABEL description="DLNA/AirPlay receiver for Xiaomi AI Speaker"

# 安装系统依赖（FFmpeg 用于音频流处理）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libportaudio2 \
    dnsutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先安装依赖（利用 Docker 缓存层）
COPY pyproject.toml .
RUN pip install --no-cache-dir . --root-user-action=ignore

# 复制应用代码
COPY miair.py ./
COPY miair/ ./miair/

# 创建配置目录
RUN mkdir -p /app/conf

# DLNA 端口 8200，Web 管理界面端口 8300
EXPOSE 8200 8300

ENTRYPOINT ["python", "miair.py", "--conf-path", "/app/conf"]
