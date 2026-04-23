# MiAir Docker 部署包

## 适用环境

- Linux 主机 + Docker
- OpenWrt / iStoreOS / ImmortalWrt 等软路由 + Docker
- macOS + Docker
- 其他支持 Docker 的系统

## 文件说明

| 文件 | 说明 |
|------|------|
| `deploy.sh` | 主部署脚本（自动完成所有步骤） |
| `manage.sh` | 服务管理脚本（启停/日志/更新） |
| `.env.example` | 配置模板 |
| `Dockerfile` | Docker 镜像定义 |

---

## 快速部署

### 方式一：自动部署（推荐）

1. **上传文件到目标主机**
   ```
   Windows:
     scp -r .\miair-deploy\* root@目标主机IP:/mnt/docker/miair/

   macOS/Linux:
     scp -r ./miair-deploy/* root@目标主机IP:/mnt/docker/miair/
   ```

2. **SSH 登录目标主机**
   ```bash
   ssh root@目标主机IP
   ```

3. **进入目录并运行部署脚本**
   ```bash
   cd /mnt/docker/miair
   chmod +x deploy.sh manage.sh

   # 运行部署
   ./deploy.sh
   ```

4. **输入配置信息**
   - 小米账号（手机号/邮箱）
   - 小米密码
   - 设备 DID（可选）

5. **完成！访问 Web 管理界面**
   ```
   http://目标主机IP:8300
   ```

---

### 方式二：使用配置文件

1. **编辑 .env 文件**
   ```bash
   cd /mnt/docker/miair
   cp .env.example .env
   nano .env
   ```

   填入实际值：
   ```env
   MI_USER=你的手机号
   MI_PASS=你的密码
   MI_DID=设备DID（可选）
   ```

2. **运行部署**
   ```bash
   ./deploy.sh
   ```

---

## 管理命令

```bash
cd /mnt/docker/miair

# 查看状态
./manage.sh status

# 查看日志
./manage.sh logs

# 实时日志
./manage.sh logs -f

# 重启服务
./manage.sh restart

# 停止服务
./manage.sh stop

# 启动服务
./manage.sh start

# 更新到最新版本
./manage.sh update

# 卸载
./manage.sh uninstall
```

---

## 常见问题

### Q: 部署脚本报错 "Permission denied"
```bash
chmod +x deploy.sh manage.sh
```

### Q: AirPlay 找不到设备
确保使用 `--network=host`，检查日志确认服务正常：
```bash
./manage.sh logs
```

### Q: 小米登录失败
- 检查账号密码是否正确
- 尝试在 Web 界面重新登录
- 检查设备 DID 是否正确

### Q: 端口被占用
```bash
# 查看端口占用
netstat -tlnp | grep -E '8200|8300'
```

---

**祝你使用愉快！🎉**
