# Docker 部署说明

## 快速开始

### 方式一：docker-compose（推荐）

```bash
# 1. 克隆项目
git clone https://github.com/KiriChen-Wind/MiAir.git
cd MiAir

# 2. 创建配置目录
mkdir -p conf

# 3. 配置账号（也可启动后在 Web 界面配置）
export MI_USER="你的小米账号"
export MI_PASS="你的小米密码"
export MI_DID="你的设备DID"   # 可选，留空自动搜索

# 4. 构建并启动
docker-compose up -d

# 5. 查看日志
docker-compose logs -f
```

启动后访问 Web 管理界面：`http://局域网IP:8300`

---

### 方式二：docker run

```bash
docker build -t miair:latest .

docker run -d \
  --name miair \
  --network=host \
  -e MI_USER="你的小米账号" \
  -e MI_PASS="你的小米密码" \
  -e MI_DID="你的设备DID" \
  -v $(pwd)/conf:/app/conf \
  --restart unless-stopped \
  miair:latest
```

---

## 环境变量

| 变量名 | 说明 | 默认值 |
|--------|------|--------|
| `MI_USER` | 小米账号（手机号/邮箱） | 空 |
| `MI_PASS` | 小米密码 | 空 |
| `MI_DID` | 设备 DID，多个用逗号分隔 | 空（自动搜索） |
| `MIAIR_HOSTNAME` | 宿主机局域网 IP | 空（自动检测） |

> 账号信息也可以不通过环境变量设置，启动后在 Web 界面（端口 8300）完成配置。

---

## 端口说明

| 端口 | 用途 |
|------|------|
| `8200` | DLNA HTTP 服务 |
| `8300` | Web 管理界面 |

---

## 网络模式说明

AirPlay（Bonjour/mDNS）和 DLNA（SSDP）的设备发现均依赖局域网广播，**必须使用 `--network=host`**（即 host 网络模式）：

| 平台 | host 网络支持 | 说明 |
|------|--------------|------|
| Linux | ✅ 完全支持 | 推荐 |
| macOS | ✅ 支持 | 推荐 |
| Windows Docker Desktop | ❌ 不支持 | 建议使用 WSL2 |

---

## 数据持久化

`./conf` 目录会映射到容器内 `/app/conf`，包含：

- `config.json` — 应用配置
- `.mi.token` — 小米登录 Token（避免重复扫码）
- `miair.log` — 运行日志

---

## 常用命令

```bash
# 查看状态
docker-compose ps

# 查看日志
docker-compose logs -f

# 重启
docker-compose restart

# 停止
docker-compose down

# 更新
git pull
docker-compose up -d --build
```
