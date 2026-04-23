# MiAir - 为小爱音箱添加 DLNA 与 AirPlay 支持

## 项目简介

MiAir 是一个开源项目，旨在为小米小爱音箱添加 DLNA 和 AirPlay 协议支持，使其可以像普通智能音箱一样被局域网内的设备发现和播放。

## 引用开源项目

| 项目 | 链接 |
|------|------|
| XiaoMusic | GitHub |
| AirPlay2 Receiver | GitHub |
| MaCast | GitHub |

---

## Docker 部署（推荐）

支持平台：Linux / OpenWrt / iStoreOS / ImmortalWrt / macOS

### 方式一：在线镜像（一键部署）

```bash
docker run -d \
  --name miair \
  --network=host \
  -e MIAIR_HOSTNAME=你的局域网IP \
  ghcr.io/syunss/miair:latest
```

> **示例：** `MIAIR_HOSTNAME=192.168.31.1`

### 方式二：本地构建

```bash
git clone -b docker https://github.com/SyunSS/MiAir.git
cd MiAir
docker build -t miair .
docker run -d --name miair --network=host -e MIAIR_HOSTNAME=你的IP miair
```

### 方式三：安装脚本

```bash
git clone https://github.com/SyunSS/MiAir.git
cd MiAir
chmod +x deploy.sh manage.sh
./deploy.sh
```

### 镜像说明

| 镜像地址 | 说明 |
|----------|------|
| `ghcr.io/syunss/miair:latest` | 在线预编译镜像，拉取后直接使用 |

### 网络模式说明

AirPlay（Bonjour/mDNS）和 DLNA（SSDP）的设备发现依赖局域网广播，**必须使用 host 网络模式**：

| 平台 | host 网络支持 | 说明 |
|------|:------------:|------|
| Linux | ✅ 完全支持 | 推荐 |
| macOS | ✅ 支持 | 推荐 |
| Windows Docker Desktop | ❌ 不支持 | 建议使用 WSL2 或 Linux 虚拟机 |

### 常用命令

```bash
docker logs -f miair     # 查看日志
docker stop miair        # 停止
docker start miair       # 启动
docker restart miair     # 重启
```

---

## 部署后

- **Web 管理界面**: `http://<MIAIR_HOSTNAME>:8300`
- **DLNA 端口**: 8200

---

## 快速开始（源码运行）

**前置要求：设备已安装 Python 3.12+**

```bash
cd MiAir
python miair.py
```

程序将自动安装相关依赖库，请确保网络畅通。

## 我们

**[需要帮助&交流&测试版本发布](https://qun.qq.com/universal-share/share?ac=1&authKey=1zXhx2zxgw9GG2mkecypT9clD7q0B3W3l4K0D4fQirmpDWakz0Oy2BI3ocDrgzbh&busi_data=eyJncm91cENvZGUiOiI3NDEyNjcyOTgiLCJ0b2tlbiI6InYwbitXQTF5cE9MaUJCR0hMUk03OWV0WkFoMThxbjJRaWI4dHVlbUpGdW5OdEZBVEpXMXF0T1dQUnRmRXRzYVgiLCJ1aW4iOiIxODQxOTM4MDQwIn0%3D&data=_OrA-eASJMwYwx-Uj-BReC1Xh3zGAdkn8CQskbEsQ5S66bhqvvO6dJ-QrSlRl-Ks00l5XDw1FANE8Um0w5yB8Q&svctype=4&tempid=h5_group_info "需要帮助&交流&测试版本发布")**

## 后续

可能 添加的功能

- ~~支持 Docker 部署~~ ✅ 已支持
- 支持 OpenWrt 部署
- ~~支持 MacOS 部署~~
- ......

[![preview](https://raw.githubusercontent.com/KiriChen-Wind/MiAir/main/preview.png "preview")](https://raw.githubusercontent.com/KiriChen-Wind/MiAir/main/preview.png "preview")
