#!/bin/bash
# MiAir Docker 本地构建部署脚本
# 支持 x86_64 / aarch64 / armv7

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}"
echo "=========================================="
echo "  MiAir Docker 本地构建部署"
echo "=========================================="
echo -e "${NC}"

# 检测架构
ARCH=$(uname -m)
case "$ARCH" in
    x86_64)  PLATFORM="amd64" ;;
    aarch64) PLATFORM="arm64" ;;
    armv7l)  PLATFORM="armv7" ;;
    *)       echo -e "${RED}不支持的架构: $ARCH${NC}"; exit 1 ;;
esac
echo -e "${GREEN}检测到架构: $PLATFORM ($ARCH)${NC}"

# 获取局域网 IP
get_lan_ip() {
    ip=$(ip route get 1.1.1.1 2>/dev/null | grep -oP 'src \K[\d.]+' | head -1)
    [ -z "$ip" ] && ip=$(hostname -I | awk '{print $1}')
    echo "${ip:-127.0.0.1}"
}

HOST_IP=$(get_lan_ip)
echo -e "${CYAN}局域网 IP: $HOST_IP${NC}"
echo ""

# 配置
read -p "小米账号 (手机号/邮箱): " MI_USER
read -sp "小米密码: " MI_PASS && echo ""
read -p "设备 DID (留空自动): " MI_DID

# 保存配置
mkdir -p ~/.miair
cat > ~/.miair/.env << EOF
MI_USER=$MI_USER
MI_PASS=$MI_PASS
MI_DID=$MI_DID
EOF

# 下载源码
echo -e "${GREEN}[1/3] 下载 MiAir 源码...${NC}"
TEMP_DIR="/tmp/miair-$$"
mkdir -p "$TEMP_DIR"
cd "$TEMP_DIR"

if command -v wget &> /dev/null; then
    wget -qO- https://github.com/KiriChen-Wind/MiAir/archive/refs/heads/main.tar.gz | tar xz
else
    curl -fsSL https://github.com/KiriChen-Wind/MiAir/archive/refs/heads/main.tar.gz | tar xz
fi

cd MiAir-main

# 创建 Dockerfile
cat > Dockerfile << 'EOF'
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libportaudio2 dnsutils && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir . --root-user-action=ignore \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY miair.py ./
COPY miair/ ./miair/
RUN mkdir -p /app/conf

EXPOSE 8200 8300
ENTRYPOINT ["python", "miair.py", "--conf-path", "/app/conf"]
EOF

# 构建
echo -e "${GREEN}[2/3] 构建 Docker 镜像 (架构: $PLATFORM)...${NC}"
docker build -t miair:latest .

# 启动
echo -e "${GREEN}[3/3] 启动容器...${NC}"
docker rm -f miair 2>/dev/null || true
mkdir -p ~/.miair/conf

docker run -d \
    --name miair \
    --network=host \
    -e MIAIR_HOSTNAME=$HOST_IP \
    -e MI_USER="$MI_USER" \
    -e MI_PASS="$MI_PASS" \
    ${MI_DID:+-e MI_DID="$MI_DID"} \
    -v ~/.miair/conf:/app/conf \
    --restart unless-stopped \
    --cap-add=NET_ADMIN --cap-add=NET_BIND_SERVICE --cap-add=NET_BROADCAST \
    miair:latest

cd /
rm -rf "$TEMP_DIR"

echo ""
echo "=========================================="
echo -e "${GREEN}  部署成功！${NC}"
echo "=========================================="
echo -e "Web 管理界面: ${GREEN}http://$HOST_IP:8300${NC}"
echo ""
echo "管理命令:"
echo "  docker logs -f miair"
echo "  docker restart miair"
echo "  docker stop miair"
