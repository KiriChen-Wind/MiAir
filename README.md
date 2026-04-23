# MiAir - 为小爱音箱添加 DLNA 与 AirPlay 支持

## 引用以下开源项目代码 由衷感谢

**[XiaoMusic](https://github.com/hanxi/xiaomusic "XiaoMusic")** &ensp; **[AirPlay2 Receiver](https://github.com/openairplay/airplay2-receiver "AirPlay2 Receiver")** &ensp; **[MaCast](https://github.com/xfangfang/Macast "MaCast")**

## 快速开始
## Windows
*确保设备已安装 Python 3.12+*

前往Release页面下载最新版本\
进入项目目录，使用终端执行
```python
python miair.py
```
程序将自动安装相关依赖库，请确保网络畅通\
安装完成后访问 `http://容器宿主机IP:8300` 即可打开 Web 管理界面。

## Docker (Thanks @SyunSS)

支持平台：Linux / OpenWrt / macOS

### 使用脚本部署
```bash
# 安装 Git
opkg update
opkg install git
opkg install git-http

# 克隆项目
git clone -b docker https://github.com/KiriChen-Wind/MiAir.git
cd MiAir

# 赋予权限并运行安装脚本
chmod +x deploy.sh manage.sh
./deploy.sh
```

安装完成后访问 `http://容器宿主机IP:8300` 即可打开 Web 管理界面。
请确保容器网络为Host。\
部分情况下，修改配置后容器可能无法自动重启，请手动重启容器。

### 在线镜像部署
```bash
docker run -d \
  --name miair \
  --network=host \
  -e MIAIR_HOSTNAME=你的局域网IP \
  ghcr.io/syunss/miair:latest
```

> **示例：** `MIAIR_HOSTNAME=192.168.31.1`

### 本地构建部署

```bash
git clone -b docker https://github.com/SyunSS/MiAir.git
cd MiAir
docker build -t miair .
docker run -d --name miair --network=host -e MIAIR_HOSTNAME=你的IP miair
```

### Docker 相关命令
```bash
docker logs -f miair     # 查看日志
docker stop miair        # 停止
docker start miair       # 启动
docker restart miair     # 重启
```


## 我们
**[需要帮助&交流&测试版本发布](https://qun.qq.com/universal-share/share?ac=1&authKey=1zXhx2zxgw9GG2mkecypT9clD7q0B3W3l4K0D4fQirmpDWakz0Oy2BI3ocDrgzbh&busi_data=eyJncm91cENvZGUiOiI3NDEyNjcyOTgiLCJ0b2tlbiI6InYwbitXQTF5cE9MaUJCR0hMUk03OWV0WkFoMThxbjJRaWI4dHVlbUpGdW5OdEZBVEpXMXF0T1dQUnRmRXRzYVgiLCJ1aW4iOiIxODQxOTM4MDQwIn0%3D&data=_OrA-eASJMwYwx-Uj-BReC1Xh3zGAdkn8CQskbEsQ5S66bhqvvO6dJ-QrSlRl-Ks00l5XDw1FANE8Um0w5yB8Q&svctype=4&tempid=h5_group_info "需要帮助&交流&测试版本发布")**

## 后续 可能 添加的功能

- ~~支持 Docker 部署~~ ✅ 已支持
- 支持 OpenWrt 部署
- ~~支持 MacOS 部署~~
- ......


[![preview](https://raw.githubusercontent.com/KiriChen-Wind/MiAir/main/preview.png "preview")](https://raw.githubusercontent.com/KiriChen-Wind/MiAir/main/preview.png "preview")
