#!/bin/bash
# MiAir 管理脚本
# 用法: ./manage.sh [start|stop|restart|logs|status|update|uninstall]

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
CONTAINER_NAME="miair"

case "$1" in
    start)
        echo "启动 MiAir..."
        docker start "$CONTAINER_NAME"
        ;;
    stop)
        echo "停止 MiAir..."
        docker stop "$CONTAINER_NAME"
        ;;
    restart)
        echo "重启 MiAir..."
        docker restart "$CONTAINER_NAME"
        ;;
    logs)
        if [ "$2" = "-f" ]; then
            docker logs -f "$CONTAINER_NAME"
        else
            docker logs "$CONTAINER_NAME"
        fi
        ;;
    status)
        docker ps -a | grep "$CONTAINER_NAME"
        ;;
    update)
        echo "更新 MiAir..."
        cd "$APP_DIR"
        # 备份配置
        cp -r conf conf.bak
        
        # 下载最新代码
        wget -O miair.tar.gz https://github.com/KiriChen-Wind/MiAir/archive/refs/heads/main.tar.gz
        tar -xzf miair.tar.gz
        cp -r MiAir-main/* .
        rm -rf MiAir-main miair.tar.gz
        
        # 重新构建
        docker build -t miair:latest .
        docker rm -f "$CONTAINER_NAME"
        ./deploy.sh
        ;;
    uninstall)
        echo "卸载 MiAir..."
        read -p "确定要删除容器和配置吗? (y/N): " confirm
        if [ "$confirm" = "y" ]; then
            docker rm -f "$CONTAINER_NAME"
            docker rmi miair:latest
            rm -rf "$APP_DIR"
            echo "卸载完成"
        fi
        ;;
    *)
        echo "用法: $0 {start|stop|restart|logs|status|update|uninstall}"
        echo ""
        echo "  start    - 启动服务"
        echo "  stop     - 停止服务"
        echo "  restart  - 重启服务"
        echo "  logs     - 查看日志"
        echo "  logs -f  - 实时查看日志"
        echo "  status   - 查看状态"
        echo "  update   - 更新到最新版本"
        echo "  uninstall - 卸载"
        exit 1
        ;;
esac
