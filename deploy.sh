#!/bin/bash
# MiAir Docker 部署脚本
# 支持 Linux / OpenWrt / iStoreOS / macOS 等平台

set -e

echo "=========================================="
echo "  MiAir Docker 部署脚本"
echo "=========================================="
echo ""

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 配置变量
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
CONTAINER_NAME="miair"
IMAGE_NAME="miair:latest"

# 进入脚本所在目录
cd "$APP_DIR"

# 检测是否以 root 运行
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}错误: 请使用 root 权限运行此脚本${NC}"
    echo "运行: sudo ./deploy.sh"
    exit 1
fi

# ============================================
# 步骤 1: 检查 Docker 是否安装
# ============================================
echo -e "${GREEN}[1/8] 检查 Docker 环境...${NC}"

if command -v docker &> /dev/null; then
    echo -e "${GREEN}✓ Docker 已安装: $(docker --version)${NC}"
else
    echo -e "${YELLOW}Docker 未安装，正在安装...${NC}"
    opkg update
    opkg install dockerd docker-compose
    /etc/init.d/dockerd start
    /etc/init.d/dockerd enable
    echo -e "${GREEN}✓ Docker 安装完成${NC}"
fi

# 等待 Docker 服务就绪
echo "等待 Docker 服务启动..."
for i in {1..30}; do
    if docker info &> /dev/null; then
        echo -e "${GREEN}✓ Docker 服务运行正常${NC}"
        break
    fi
    if [ $i -eq 30 ]; then
        echo -e "${RED}✗ Docker 服务启动超时${NC}"
        exit 1
    fi
    sleep 1
done

# ============================================
# 步骤 2: 获取宿主机 IP
# ============================================
echo -e "${GREEN}[2/8] 获取局域网 IP...${NC}"

# 获取 LAN 口 IP 的函数：按优先级依次尝试各种方式
get_lan_ip() {
    local ip=""

    # 1. 优先尝试常见 LAN 桥接口名
    for iface in br-lan br0 eth0 eth1 lan; do
        ip=$(ip addr show "$iface" 2>/dev/null \
             | grep -oP 'inet \K[\d.]+' \
             | grep -v '^127\.' | head -1)
        [ -n "$ip" ] && echo "$ip" && return
    done

    # 2. 找所有以 br- 开头的桥接口
    for iface in $(ip link show 2>/dev/null | awk -F': ' '/^[0-9]+: br-/{print $2}'); do
        ip=$(ip addr show "$iface" 2>/dev/null \
             | grep -oP 'inet \K[\d.]+' \
             | grep -v '^127\.' | head -1)
        [ -n "$ip" ] && echo "$ip" && return
    done

    # 3. 取默认路由出口接口的 IP
    local gw_iface
    gw_iface=$(ip route 2>/dev/null | awk '/^default/{print $5; exit}')
    if [ -n "$gw_iface" ]; then
        ip=$(ip addr show "$gw_iface" 2>/dev/null \
             | grep -oP 'inet \K[\d.]+' \
             | grep -v '^127\.' | head -1)
        [ -n "$ip" ] && echo "$ip" && return
    fi

    # 4. 兜底：取第一个非 lo、非 docker 的内网 IP
    ip=$(ip addr 2>/dev/null \
         | grep -oP 'inet \K(192\.168|10\.|172\.(1[6-9]|2[0-9]|3[01]))[\d.]+' \
         | head -1)
    [ -n "$ip" ] && echo "$ip" && return

    echo "127.0.0.1"
}

HOST_IP=$(get_lan_ip)
echo -e "${GREEN}✓ 宿主机 IP: $HOST_IP${NC}"

# ============================================
# 步骤 3: 检查并下载 MiAir 代码
# ============================================
echo -e "${GREEN}[3/8] 检查 MiAir 代码...${NC}"

# 检查必要文件是否存在
if [ -f "miair.py" ] && [ -f "pyproject.toml" ] && [ -d "miair" ]; then
    echo -e "${GREEN}✓ MiAir 代码已存在${NC}"
else
    echo -e "${YELLOW}未找到 MiAir 代码，开始从 GitHub 下载...${NC}"
    
    # 下载代码
    echo "下载 MiAir 源代码..."
    if command -v wget &> /dev/null; then
        wget -O miair.tar.gz https://github.com/KiriChen-Wind/MiAir/archive/refs/heads/main.tar.gz
    elif command -v curl &> /dev/null; then
        curl -L https://github.com/KiriChen-Wind/MiAir/archive/refs/heads/main.tar.gz -o miair.tar.gz
    else
        opkg install wget
        wget -O miair.tar.gz https://github.com/KiriChen-Wind/MiAir/archive/refs/heads/main.tar.gz
    fi
    
    # 解压并整理
    echo "解压文件..."
    tar -xzf miair.tar.gz
    cp -r MiAir-main/* . 2>/dev/null || true
    cp MiAir-main/.* . 2>/dev/null || true
    rm -rf MiAir-main miair.tar.gz
    
    # 验证下载
    if [ -f "miair.py" ] && [ -f "pyproject.toml" ] && [ -d "miair" ]; then
        echo -e "${GREEN}✓ MiAir 代码下载并准备完成${NC}"
    else
        echo -e "${RED}✗ MiAir 代码下载失败${NC}"
        echo "请手动下载: https://github.com/KiriChen-Wind/MiAir"
        exit 1
    fi
fi

# ============================================
# 步骤 4: 配置参数
# ============================================

# 加载 .env 文件（如果存在）
if [ -f ".env" ]; then
    echo "加载 .env 配置文件..."
    set -a
    source .env
    set +a
fi

# ============================================
# 步骤 5: 创建 Dockerfile
# ============================================
echo -e "${GREEN}[5/8] 创建 Dockerfile...${NC}"

# 确保在正确目录
cd "$APP_DIR"

# 检查 pyproject.toml 是否存在
if [ ! -f "pyproject.toml" ]; then
    echo -e "${RED}错误: pyproject.toml 不存在${NC}"
    echo "请确保 MiAir 代码已正确放置"
    exit 1
fi

# 创建 Dockerfile
cat > Dockerfile << 'DOCKERFILE_EOF'
FROM python:3.12-slim

LABEL maintainer="MiAir"
LABEL description="DLNA/AirPlay receiver for Xiaomi AI Speaker"

# 安装系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libportaudio2 \
    dnsutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 安装 Python 依赖
COPY pyproject.toml .
RUN pip install --no-cache-dir .

# 复制应用代码
COPY miair.py ./
COPY miair/ ./miair/

# 创建配置目录
RUN mkdir -p /app/conf

# 暴露端口
EXPOSE 8200 8300

ENTRYPOINT ["python", "miair.py", "--conf-path", "/app/conf"]
DOCKERFILE_EOF

echo -e "${GREEN}✓ Dockerfile 创建完成${NC}"

# ============================================
# 步骤 6: 构建 Docker 镜像
# ============================================
echo -e "${GREEN}[6/8] 构建 Docker 镜像 (可能需要几分钟)...${NC}"

docker build -t miair:latest .

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ 镜像构建成功${NC}"
else
    echo -e "${RED}✗ 镜像构建失败${NC}"
    exit 1
fi

# ============================================
# 步骤 7: 停止并删除旧容器（如存在）
# ============================================
echo -e "${GREEN}[7/8] 清理旧容器...${NC}"

docker rm -f "$CONTAINER_NAME" 2>/dev/null || true
echo -e "${GREEN}✓ 清理完成${NC}"

# ============================================
# 步骤 8: 启动容器
# ============================================
echo -e "${GREEN}[8/8] 启动 MiAir 容器...${NC}"

# 构建环境变量参数
ENV_VARS="-e TZ=Asia/Shanghai -e MIAIR_HOSTNAME=$HOST_IP"
[ -n "$MI_USER" ] && ENV_VARS="$ENV_VARS -e MI_USER=$MI_USER"
[ -n "$MI_PASS" ] && ENV_VARS="$ENV_VARS -e MI_PASS=$MI_PASS"
[ -n "$MI_DID" ] && ENV_VARS="$ENV_VARS -e MI_DID=$MI_DID"

# 启动命令
docker run -d \
    --name "$CONTAINER_NAME" \
    --network=host \
    $ENV_VARS \
    -v "$APP_DIR/conf:/app/conf" \
    --restart unless-stopped \
    --cap-add=NET_ADMIN \
    --cap-add=NET_BIND_SERVICE \
    --cap-add=NET_BROADCAST \
    "$IMAGE_NAME"

if [ $? -eq 0 ]; then
    echo ""
    echo "=========================================="
    echo -e "${GREEN}  🎉 MiAir 部署成功！${NC}"
    echo "=========================================="
    echo ""
    echo -e "Web 管理界面: ${GREEN}YourHostIP:8300${NC}"
    echo -e "DLNA 端口: ${GREEN}8200${NC}"
    echo ""
    echo "查看日志命令:"
    echo "  docker logs -f miair"
    echo ""
    echo "停止服务:"
    echo "  docker stop miair"
    echo ""
    
    # 显示日志前 20 行
    echo "最近日志:"
    echo "----------------------------------------"
    docker logs miair 2>&1 | tail -20
    echo "----------------------------------------"
else
    echo -e "${RED}✗ 容器启动失败，请查看日志${NC}"
    echo "docker logs miair"
    exit 1
fi
