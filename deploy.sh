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
PARENT_DIR="$(dirname "$APP_DIR")"
CONTAINER_NAME="miair"
IMAGE_NAME="miair:latest"

# 检测设备架构
ARCH=$(uname -m)
echo -e "${GREEN}检测到设备架构: $ARCH${NC}"

# 根据架构设置基础镜像
case "$ARCH" in
    arm*|aarch64)
        BASE_IMAGE="python:3.12-slim"
        ;;    
    x86_64|amd64)
        BASE_IMAGE="python:3.12-slim"
        ;;
    *)
        echo -e "${YELLOW}警告: 未知架构 $ARCH，使用默认基础镜像${NC}"
        BASE_IMAGE="python:3.12-slim"
        ;;
esac
echo -e "${GREEN}使用基础镜像: $BASE_IMAGE${NC}"

# 询问用户配置目录位置
echo -e "${YELLOW}配置文件目录设置${NC}"
echo "建议使用持久化存储路径，如: /root/miair_conf 或 /opt/miair_conf"
read -p "请输入配置目录路径（直接回车使用默认: /tmp/miair_conf）: " CONFIG_DIR
if [ -z "$CONFIG_DIR" ]; then
    CONFIG_DIR="/tmp/miair_conf"
fi

# 确保配置目录存在
mkdir -p "$CONFIG_DIR"
echo -e "${GREEN}✓ 配置目录: $CONFIG_DIR${NC}"

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

# 获取 LAN 口 IP 的函数：优先枚举局域网私有地址，默认出网地址只作为最后兜底
get_lan_ip() {
    local ip

    # 1. 优先从非虚拟接口中选择私有 LAN 地址，避免多网卡时误选 WAN/default route
    ip=$(ip -o -4 addr show scope global 2>/dev/null | awk '
        $2 !~ /^(lo|docker[0-9]*|tailscale[0-9]*|veth.*|br-[a-f0-9]+|wg[0-9]*|tun[0-9]*|tap[0-9]*|ppp[0-9]*)$/ {
            split($4, a, "/"); candidate=a[1]
            if (candidate ~ /^192\.168\./) { best=candidate; found=1; exit }
            if (candidate ~ /^10\./ && best == "") best=candidate
            if (candidate ~ /^172\.(1[6-9]|2[0-9]|3[0-1])\./ && best == "") best=candidate
            if (fallback == "") fallback=candidate
        }
        END { if (best != "") print best; else if (fallback != "") print fallback }
    ')
    [ -n "$ip" ] && echo "$ip" && return

    # 2. 如果 ip addr 不可用，再尝试 hostname -I 中的私有地址
    if command -v hostname &> /dev/null; then
        ip=$(hostname -I 2>/dev/null | awk '{
            for (i=1; i<=NF; i++) {
                if ($i ~ /^192\.168\./) { print $i; exit }
                if ($i ~ /^10\./ && best == "") best=$i
                if ($i ~ /^172\.(1[6-9]|2[0-9]|3[0-1])\./ && best == "") best=$i
            }
            if (best != "") print best
        }')
        [ -n "$ip" ] && echo "$ip" && return
    fi

    # 3. 最后才使用默认出网 IP，它可能是 WAN 地址，后续仍会要求用户确认
    ip=$(ip -4 route get 8.8.8.8 2>/dev/null | awk '/src/ {for (i=1; i<=NF; i++) if ($i == "src") {print $(i+1); exit}}')
    [ -n "$ip" ] && echo "$ip" && return

    # 兜底
    echo "127.0.0.1"
}

HOST_IP=$(get_lan_ip)

if [ -z "$HOST_IP" ] || [ "$HOST_IP" = "127.0.0.1" ]; then
    echo -e "${YELLOW}警告: 无法自动获取到有效的局域网 IP。${NC}"
    read -p "请输入宿主机的局域网 IP 地址: " HOST_IP
    while [ -z "$HOST_IP" ] || [ "$HOST_IP" = "127.0.0.1" ]; do
        echo -e "${RED}IP 地址无效，请重新输入${NC}"
        read -p "请输入宿主机的局域网 IP 地址: " HOST_IP
    done
else
    echo -e "${GREEN}获取到的局域网 IP 地址: ${YELLOW}$HOST_IP${NC}"
    echo "这个局域网 IP 地址是正确的吗？"
    read -p "如果此局域网 IP 地址正确，请按下回车以确认使用。否则请输入正确的局域网 IP 地址并按下回车： " USER_IP
    if [ -n "$USER_IP" ]; then
        HOST_IP="$USER_IP"
    fi
fi
echo -e "${GREEN}✓ 最终确认宿主机 IP: $HOST_IP${NC}"

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
cat > Dockerfile << DOCKERFILE_EOF
FROM $BASE_IMAGE

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

# 询问用户是否保留之前的配置
echo -e "${YELLOW}是否保留之前的配置文件？${NC}"
echo "1) 保留配置"
echo "2) 重置配置"
read -p "请选择 (1/2): " KEEP_CONFIG

# 根据用户选择处理配置
if [ "$KEEP_CONFIG" = "1" ]; then
    # 备份配置
    echo -e "${GREEN}保留配置，备份现有配置...${NC}"
    TEMP_CONF="/tmp/miair_conf_backup_$$"
    if [ -d "$CONFIG_DIR" ] && [ -n "$(ls -A "$CONFIG_DIR" 2>/dev/null)" ]; then
        cp -r "$CONFIG_DIR" "$TEMP_CONF"
        echo -e "${GREEN}✓ 配置已备份${NC}"
    else
        TEMP_CONF=""
        echo -e "${YELLOW}未找到现有配置，将使用默认配置${NC}"
    fi
else
    echo -e "${YELLOW}重置配置文件...${NC}"
    rm -rf "$CONFIG_DIR" 2>/dev/null || true
    TEMP_CONF=""
fi

# 清理旧容器
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

# 确保配置目录存在
mkdir -p "$CONFIG_DIR"
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
    -v "$CONFIG_DIR:/app/conf" \
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

    # 恢复备份的配置
    if [ -n "$TEMP_CONF" ] && [ -d "$TEMP_CONF" ]; then
        echo -e "${GREEN}恢复配置...${NC}"
        cp -r "$TEMP_CONF"/* "$CONFIG_DIR/" 2>/dev/null || true
        rm -rf "$TEMP_CONF"
        echo -e "${GREEN}✓ 配置已恢复${NC}"
    fi

    echo -e "配置文件目录: ${GREEN}$CONFIG_DIR${NC}"
    echo -e "Web 管理界面: ${GREEN}http://$HOST_IP:8300${NC}"
    echo -e "DLNA 服务端口: ${GREEN}8200${NC}"
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
