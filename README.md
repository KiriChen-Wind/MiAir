# MiAir - 为小爱音箱添加 DLNA 与 AirPlay 支持

## 引用以下开源项目代码

由衷感谢 **[XiaoMusic](https://github.com/hanxi/xiaomusic "XiaoMusic")**   **[AirPlay2 Receiver](https://github.com/openairplay/airplay2-receiver "AirPlay2 Receiver")**   **[MaCast](https://github.com/xfangfang/Macast "Macast)")**

---

## Docker 部署（推荐）

支持 Linux / OpenWrt / iStoreOS / ImmortalWrt / macOS 等平台。

### 一键安装

```bash
# 克隆项目
git clone https://github.com/SyunSS/MiAir.git
cd MiAir

# 运行安装脚本，按提示输入小米账号密码即可
chmod +x deploy.sh manage.sh
./deploy.sh
```

安装完成后访问 `http://容器宿主机IP:8300` 打开 Web 管理界面。

### 安装脚本参数（可选）

| 参数 | 说明 |
|------|------|
| `-u` 或 `--user` | 小米账号（手机号/邮箱） |
| `-p` 或 `--pass` | 小米密码 |
| `-d` 或 `--did` | 设备 DID（可选，留空自动搜索） |

示例：

```bash
./deploy.sh -u 你的手机号 -p 你的密码 -d 设备DID
```

### 网络模式说明

AirPlay（Bonjour/mDNS）和 DLNA（SSDP）的设备发现依赖局域网广播，**必须使用 host 网络模式**：

| 平台 | host 网络支持 | 说明 |
|------|:------------:|------|
| Linux | ✅ 完全支持 | 推荐 |
| macOS | ✅ 支持 | 推荐 |
| Windows Docker Desktop | ❌ 不支持 | 建议使用 WSL2 或 Linux 虚拟机 |

### 常用命令

```bash
# 查看日志
docker logs -f miair

# 停止/启动/重启
docker stop miair
docker start miair
docker restart miair

# 更新（重新运行安装脚本即可）
./deploy.sh

# 卸载
docker rm -f miair
docker rmi miair:latest
```

---

## 快速开始（源码运行）

*确保设备已安装 Python 3.12+*

进入项目目录，使用终端执行

```bash
python miair.py
```

程序将自动安装相关依赖库，请确保网络畅通

---

## 我们

**[需要帮助&交流&测试版本发布](https://qun.qq.com/universal-share/share?ac=1&authKey=1zXhx2zxgw9GG2mkecypT9clD7q0B3W3l4K0D4fQirmpDWakz0Oy2BI3ocDrgzbh&busi_data=eyJncm91cENvZGUiOiI3NDEyNjcyOTgiLCJ0b2tlbiI6InYwbitXQTF5cE9MaUJCR0hMUk03OWV0WkFoMThxbjJRaWI4dHVlbUpGdW5OdEZBVEpXMXF0T1dQUnRmRXRzYVgiLCJ1aW4iOiIxODQxOTM4MDQwIn0%3D&data=_OrA-eASJMwYwx-Uj-BReC1Xh3zGAdkn8CQskbEsQ5S66bhqvvO6dJ-QrSlRl-Ks00l5XDw1FANE8Um0w5yB8Q&svctype=4&tempid=h5_group_info "需要帮助&交流&测试版本发布")**

## 后续

可能 添加的功能

- ~~支持 Docker 部署~~ ✅ 已支持
- 支持 OpenWrt 部署
- ~~支持 MacOS 部署~~
- ......

[![preview](https://raw.githubusercontent.com/KiriChen-Wind/MiAir/main/preview.png "preview")](https://raw.githubusercontent.com/KiriChen-Wind/MiAir/main/preview.png "preview")
