"""Web 管理界面 API"""

import json
import logging
import os
import sys

from aiohttp import web

import asyncio

from miair.config import Config

log = logging.getLogger("miair")


def _is_docker():
    """检测是否在 Docker 容器中运行"""
    # 1. 环境变量显式指定（最可靠）
    if os.environ.get("MIAIR_DOCKER"):
        return True
    # 2. Docker 会在容器根目录创建 .dockerenv 文件
    if os.path.exists("/.dockerenv"):
        return True
    # 3. 检查 cgroup（兼容 cgroup v1 和 v2）
    try:
        with open('/proc/1/cgroup', 'r') as f:
            content = f.read()
            return any(k in content for k in ('docker', 'containerd', 'kubepods'))
    except Exception:
        pass
    # 4. 检查 /proc/self/mountinfo 中的 overlay/docker 挂载
    try:
        with open('/proc/self/mountinfo', 'r') as f:
            content = f.read()
            return 'docker' in content or '/docker/' in content
    except Exception:
        pass
    return False

def _restart_process():
    """重启当前 Python 进程"""
    log.info(f"重启进程: {sys.executable} {sys.argv}")
    
    # 检测是否在 Docker 容器中
    if _is_docker():
        # Docker 环境下，直接退出进程
        # Docker 容器已设置 restart=unless-stopped，会自动重启
        log.info("在 Docker 环境中，退出进程，Docker 会自动重启容器")
        # 使用 exit code 0，unless-stopped 策略下任何退出都会重启
        os._exit(0)
    elif sys.platform == "win32":
        # Windows 上 os.execv 行为不同，使用 subprocess 重启
        import subprocess
        subprocess.Popen([sys.executable] + sys.argv)
        os._exit(0)
    else:
        os.execv(sys.executable, [sys.executable] + sys.argv)


def create_web_app(config: Config, app_instance) -> web.Application:
    """创建 Web 管理应用"""
    web_app = web.Application()

    async def handle_index(request):
        """主页"""
        import os
        static_dir = os.path.join(os.path.dirname(__file__), "static")
        index_path = os.path.join(static_dir, "index.html")
        if os.path.exists(index_path):
            return web.FileResponse(index_path)
        return web.Response(text="MiAir Web UI", content_type="text/html")

    async def handle_get_setting(request):
        """获取当前设置和设备列表 (类似 xiaomusic /getsetting)"""
        need_device_list = request.query.get("need_device_list", "false") == "true"

        data = {
            "hostname": config.hostname,
            "dlna_port": config.dlna_port,
            "web_port": config.web_port,
            "proxy_enabled": config.proxy_enabled,
            "auto_play_on_set_uri": config.auto_play_on_set_uri,
            "mi_did": config.mi_did,
            "has_account": bool(config.account or config.cookie),
            "cookie": config.cookie,
            "dlna_running": app_instance.dlna_running,
            "renderers_count": len(app_instance.renderers),
            # 实验性功能
            "auto_resume_on_interrupt": config.auto_resume_on_interrupt,
            "resume_delay_seconds": config.resume_delay_seconds,
            "default_volume": config.default_volume,
            "follow_device_volume": config.follow_device_volume,
            "auto_restart": config.auto_restart,
        }

        # 返回已配置的 speakers 信息
        speakers_info = {}
        for did in config.get_did_list():
            speaker = config.get_speaker(did)
            speakers_info[did] = {
                "did": did,
                "name": speaker.name,
                "dlna_name": speaker.get_dlna_name(),
                "hardware": speaker.hardware,
                "enabled": speaker.enabled,
            }
        data["speakers"] = speakers_info

        if need_device_list:
            device_list = await app_instance.get_all_devices()
            data["device_list"] = device_list

        return web.json_response(data)

    async def handle_save_setting(request):
        """保存设置 (账号、密码、cookie、选中的设备)"""
        data = await request.json()

        # 更新账号信息
        if "account" in data:
            config.account = data["account"]
        if "password" in data:
            config.password = data["password"]
        if "cookie" in data:
            config.cookie = data["cookie"]

        # 更新设备选择
        if "mi_did" in data:
            config.mi_did = data["mi_did"]

        # 更新其他配置
        if "auto_play_on_set_uri" in data:
            config.auto_play_on_set_uri = data["auto_play_on_set_uri"]

        # 更新端口配置
        if "dlna_port" in data:
            config.dlna_port = data["dlna_port"]
        if "web_port" in data:
            config.web_port = data["web_port"]

        # 更新实验性功能配置
        if "auto_resume_on_interrupt" in data:
            config.auto_resume_on_interrupt = data["auto_resume_on_interrupt"]
        if "resume_delay_seconds" in data:
            config.resume_delay_seconds = max(1, min(15, int(data["resume_delay_seconds"])))
        if "default_volume" in data:
            config.default_volume = max(1, min(100, int(data["default_volume"])))
        if "follow_device_volume" in data:
            config.follow_device_volume = data["follow_device_volume"]
        if "auto_restart" in data:
            config.auto_restart = data["auto_restart"]

        # 更新 speaker 名称
        if "speakers" in data:
            for did, speaker_data in data["speakers"].items():
                speaker = config.get_speaker(did)
                if "dlna_name" in speaker_data:
                    speaker.dlna_name = speaker_data["dlna_name"]

        config.save()

        # 先返回响应，然后重启进程
        resp = web.json_response({"ok": True, "message": "配置已保存，正在重启..."})
        await resp.prepare(request)
        await resp.write_eof()

        # 安排进程重启
        log.info("配置已保存，正在重启进程...")
        asyncio.get_running_loop().call_soon(_restart_process)
        return resp

    async def handle_get_devices(request):
        """获取小米账号下所有设备列表"""
        if not config.cookie:
            return web.json_response(
                {"error": "请先配置 Cookie"}, status=400
            )

        try:
            devices = await app_instance.get_all_devices()
            if not devices and not app_instance.auth.is_logged_in():
                return web.json_response({
                    "devices": [],
                    "error": "登录失败，请检查账号密码或尝试使用 Cookie 登录"
                })
            return web.json_response({"devices": devices})
        except Exception as e:
            return web.json_response(
                {"error": f"获取设备列表失败: {e}"}, status=500
            )

    async def handle_get_speakers(request):
        """获取当前运行中的渲染器状态"""
        speakers_info = []
        for did, controller in app_instance.speaker_manager.controllers.items():
            speaker = controller.speaker
            renderer = app_instance.get_renderer_by_did(did)
            # 获取 DLNA 状态
            transport_state = renderer.transport_state if renderer else "UNKNOWN"
            current_uri = renderer.current_uri if renderer else ""
            
            # 获取 AirPlay 状态
            airplay_active = False
            airplay_client = ""
            if app_instance.airplay_manager:
                sap = app_instance.airplay_manager.speaker_airplays.get(did)
                if sap and sap.airplay_server:
                    if sap.airplay_server.is_playing:
                        airplay_active = True
                        airplay_client = sap.airplay_server.client_name

            speakers_info.append({
                "did": did,
                "name": speaker.name,
                "dlna_name": speaker.get_dlna_name(),
                "hardware": speaker.hardware,
                "enabled": speaker.enabled,
                "udn": speaker.udn,
                "transport_state": transport_state,
                "current_uri": current_uri,
                "airplay_active": airplay_active,
                "airplay_client": airplay_client,
            })
        return web.json_response(speakers_info)

    async def handle_rename_speaker(request):
        """重命名音箱的 DLNA 名称"""
        did = request.match_info["did"]
        data = await request.json()
        new_name = data.get("dlna_name", "")
        if not new_name:
            return web.json_response({"error": "名称不能为空"}, status=400)

        speaker = config.get_speaker(did)
        speaker.dlna_name = new_name
        config.save()
        
        # 更新对应的DLNA渲染器名称
        for udn, renderer in app_instance.renderers.items():
            if renderer.did == did:
                renderer.friendly_name = new_name
                log.info(f"已更新渲染器名称: {new_name} (did={did})")
                break
        
        return web.json_response({"ok": True, "dlna_name": new_name})

    async def handle_status(request):
        """系统状态"""
        return web.json_response({
            "version": "0.1.0",
            "dlna_running": app_instance.dlna_running,
            "renderers_count": len(app_instance.renderers),
            "hostname": config.hostname,
            "dlna_port": config.dlna_port,
            "web_port": config.web_port,
        })

    # 注册路由
    web_app.router.add_get("/", handle_index)
    web_app.router.add_get("/api/setting", handle_get_setting)
    web_app.router.add_post("/api/setting", handle_save_setting)
    web_app.router.add_get("/api/devices", handle_get_devices)
    web_app.router.add_get("/api/speakers", handle_get_speakers)
    web_app.router.add_post("/api/speakers/{did}/rename", handle_rename_speaker)
    web_app.router.add_get("/api/status", handle_status)

    # 静态文件
    import os
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.exists(static_dir):
        web_app.router.add_static("/static", static_dir)

    return web_app
