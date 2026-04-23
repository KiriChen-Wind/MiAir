#!/bin/bash
# MiAir Docker 部署脚本
# 支持 Linux / OpenWrt / iStoreOS / macOS / ARM 等平台

set -e

echo "=========================================="
echo "  MiAir Docker 部署脚本"
echo "=========================================="
echo ""

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# 配置变量
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
CONTAINER_NAME="miair"
IMAGE_NAME="miair:latest"

# 进入脚本所在目录
cd "$APP_DIR"

# 检测架构
detect_arch() {
    ARCH=$(uname -m)
    case "$ARCH" in
        x86_64)  PLATFORM="amd64"; echo -e "${CYAN}架构: x86_64 (amd64)${NC}" ;;
        aarch64) PLATFORM="arm64"; echo -e "${CYAN}架构: aarch64 (ARM64)${NC}" ;;
        armv7l)  PLATFORM="armv7"; echo -e "${CYAN}架构: armv7l (ARMv7)${NC}" ;;
        *)       echo -e "${RED}不支持的架构: $ARCH${NC}"; exit 1 ;;
    esac
}

# 检测是否以 root 运行（OpenWrt/iStoreOS 不需要）
detect_root() {
    if [ "$EUID" -ne 0 ] && [ ! -f "/etc/openwrt_release" ] && [ ! -f "/etc/iStoreOS_version" ]; then
        echo -e "${RED}错误: 请使用 root 权限运行此脚本${NC}"
        echo "运行: sudo ./deploy.sh"
        exit 1
    fi
}

# ============================================
# 步骤 1: 检测环境
# ============================================
echo -e "${GREEN}[1/8] 检测系统环境...${NC}"
detect_arch
detect_root

# ============================================
# 步骤 2: 检查 Docker
# ============================================
echo -e "${GREEN}[2/8] 检查 Docker 环境...${NC}"

if command -v docker &> /dev/null && docker info &> /dev/null; then
    echo -e "${GREEN}✓ Docker 已安装: $(docker --version)${NC}"
else
    echo -e "${YELLOW}Docker 未安装，正在安装...${NC}"

    # OpenWrt / iStoreOS
    if [ -f "/etc/openwrt_release" ] || [ -f "/etc/iStoreOS_version" ]; then
        opkg update
        opkg install dockerd docker-compose-plugin || opkg install dockerd docker-compose
        /etc/init.d/dockerd start
        /etc/init.d/dockerd enable
    # Debian / Ubuntu
    elif command -v apt-get &> /dev/null; then
        apt-get update && apt-get install -y docker.io docker-compose
        systemctl start docker || service docker start
        systemctl enable docker || service docker enable
    # Fedora
    elif command -v dnf &> /dev/null; then
        dnf install -y docker docker-compose
        systemctl start docker
        systemctl enable docker
    # Arch
    elif command -v pacman &> /dev/null; then
        pacman -Sy --noconfirm docker docker-compose
        systemctl start docker
        systemctl enable docker
    else
        echo -e "${RED}无法自动安装 Docker，请手动安装${NC}"
        exit 1
    fi
    echo -e "${GREEN}✓ Docker 安装完成${NC}"
fi

# 等待 Docker 服务就绪
echo "等待 Docker 服务启动..."
for i in {1..30}; do
    if docker info &> /dev/null; then
        echo -e "${GREEN}✓ Docker 服务运行正常${NC}"
        break
    fi
    [ $i -eq 30 ] && echo -e "${RED}✗ Docker 服务启动超时${NC}" && exit 1
    sleep 1
done

# ============================================
# 步骤 3: 获取局域网 IP
# ============================================
echo -e "${GREEN}[3/8] 获取局域网 IP...${NC}"

get_lan_ip() {
    local ip=""

    # macOS
    if [ "$(uname)" = "Darwin" ]; then
        ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null)
        [ -n "$ip" ] && echo "$ip" && return
    fi

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
# 步骤 4: 检查/下载 MiAir 代码
# ============================================
echo -e "${GREEN}[4/8] 检查 MiAir 代码...${NC}"

if [ -f "miair.py" ] && [ -f "pyproject.toml" ] && [ -d "miair" ]; then
    echo -e "${GREEN}✓ MiAir 代码已存在${NC}"
else
    echo -e "${YELLOW}未找到 MiAir 代码，开始从 GitHub 下载...${NC}"

    if command -v wget &> /dev/null; then
        wget -O miair.tar.gz https://github.com/KiriChen-Wind/MiAir/archive/refs/heads/main.tar.gz
    elif command -v curl &> /dev/null; then
        curl -L https://github.com/KiriChen-Wind/MiAir/archive/refs/heads/main.tar.gz -o miair.tar.gz
    elif command -v opkg &> /dev/null; then
        opkg install wget
        wget -O miair.tar.gz https://github.com/KiriChen-Wind/MiAir/archive/refs/heads/main.tar.gz
    else
        echo -e "${RED}错误: 未找到 wget 或 curl${NC}"
        exit 1
    fi

    echo "解压文件..."
    tar -xzf miair.tar.gz
    cp -r MiAir-main/* MiAir-main/.* . 2>/dev/null || true
    rm -rf MiAir-main miair.tar.gz

    if [ -f "miair.py" ] && [ -f "pyproject.toml" ] && [ -d "miair" ]; then
        echo -e "${GREEN}✓ MiAir 代码下载并准备完成${NC}"
    else
        echo -e "${RED}✗ MiAir 代码下载失败${NC}"
        exit 1
    fi
fi

# ============================================
# 步骤 5: 加载配置
# ============================================
echo -e "${GREEN}[5/8] 加载配置文件...${NC}"

if [ -f ".env" ]; then
    echo "加载 .env 配置文件..."
    set -a
    source .env
    set +a
fi

# ============================================
# 步骤 6: 创建 Dockerfile
# ============================================
echo -e "${GREEN}[6/8] 创建 Dockerfile...${NC}"

cd "$APP_DIR"

cat > Dockerfile << 'DOCKERFILE_EOF'
FROM python:3.12-slim

LABEL maintainer="MiAir"
LABEL description="DLNA/AirPlay receiver for Xiaomi AI Speaker"

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libportaudio2 \
    dnsutils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir . --root-user-action=ignore \
    -i https://pypi.tuna.tsinghua.edu.cn/simple

COPY miair.py ./
COPY miair/ ./miair/
RUN mkdir -p /app/conf

EXPOSE 8200 8300
ENTRYPOINT ["python", "miair.py", "--conf-path", "/app/conf"]
DOCKERFILE_EOF

echo -e "${GREEN}✓ Dockerfile 创建完成${NC}"

# ============================================
# 步骤 7: 构建镜像
# ============================================
echo -e "${GREEN}[7/8] 构建 Docker 镜像 (可能需要几分钟)...${NC}"

docker build -t "$IMAGE_NAME" .

if [ $? -eq 0 ]; then
    echo -e "${GREEN}✓ 镜像构建成功${NC}"
else
    echo -e "${RED}✗ 镜像构建失败${NC}"
    exit 1
fi

# ============================================
# 步骤 8: 启动容器
# ============================================
echo -e "${GREEN}[8/8] 清理旧容器并启动...${NC}"

docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

# 创建配置目录
mkdir -p "$APP_DIR/conf"

# 构建环境变量参数
ENV_VARS="-e TZ=Asia/Shanghai -e MIAIR_HOSTNAME=$HOST_IP"
[ -n "$MI_USER" ] && ENV_VARS="$ENV_VARS -e MI_USER=$MI_USER"
[ -n "$MI_PASS" ] && ENV_VARS="$ENV_VARS -e MI_PASS=$MI_PASS"
[ -n "$MI_DID" ] && ENV_VARS="$ENV_VARS -e MI_DID=$MI_DID"

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
    echo -e "Web 管理界面: ${GREEN}http://$HOST_IP:8300${NC}"
    echo -e "DLNA 端口: ${GREEN}8200${NC}"
    echo ""
    echo "管理命令:"
    echo "  ./manage.sh logs     # 查看日志"
    echo "  ./manage.sh restart  # 重启"
    echo "  ./manage.sh stop     # 停止"
    echo ""
    echo "最近日志:"
    echo "----------------------------------------"
    docker logs miair 2>&1 | tail -15
    echo "----------------------------------------"
else
    echo -e "${RED}✗ 容器启动失败，请查看日志${NC}"
    echo "docker logs miair"
    exit 1
fi
