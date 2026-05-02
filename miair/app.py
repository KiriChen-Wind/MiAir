"""MiAir 主应用编排器"""

import asyncio
import logging
import os
import sys

from aiohttp import web

from miair.auth import AuthManager
from miair.config import Config
from miair.dlna.device_server import DeviceServer
from miair.dlna.renderer import DLNARenderer
from miair.dlna.ssdp import SSDPServer
from miair.speaker import SpeakerManager
from miair.web.api import create_web_app
from miair.airplay.speaker_airplay import AirPlayManager

log = logging.getLogger("miair")


class MiAir:
    """MiAir 主应用"""

    def __init__(self, config: Config):
        self.config = config
        self.auth = AuthManager(config)
        self.speaker_manager = SpeakerManager(config, self.auth)
        self.renderers: dict[str, DLNARenderer] = {}  # udn -> DLNARenderer
        self._did_to_udn: dict[str, str] = {}  # did -> udn
        self.ssdp_server: SSDPServer | None = None
        self.device_server: DeviceServer | None = None
        self._web_runner: web.AppRunner | None = None
        self.dlna_running = False
        self.airplay_manager: AirPlayManager | None = None

    def get_renderer_by_did(self, did: str) -> DLNARenderer | None:
        """根据 DID 获取渲染器"""
        udn = self._did_to_udn.get(did)
        if udn:
            return self.renderers.get(udn)
        return None

    async def get_all_devices(self) -> list[dict]:
        """获取小米账号下所有设备列表"""
        if not self.config.account and not self.config.cookie:
            return []
        try:
            await self.auth.ensure_login()
            devices = await self.auth.get_device_list()
            return devices
        except Exception as e:
            log.warning(f"获取设备列表失败: {e}")
            return []

    async def start(self):
        """启动所有服务"""
        self._setup_logging()

        log.info("MiAir 启动中...")
        log.info(f"主机名: {self.config.hostname}")
        log.info(f"DLNA 端口: {self.config.dlna_port}")
        log.info(f"Web 端口: {self.config.web_port}")

        # 1. 先启动 Web 管理界面 (始终启动)
        web_app = create_web_app(self.config, self)
        self._web_runner = web.AppRunner(web_app, access_log=None)
        await self._web_runner.setup()
        web_site = web.TCPSite(self._web_runner, "0.0.0.0", self.config.web_port)
        await web_site.start()
        log.info(f"Web 管理界面: http://{self.config.hostname}:{self.config.web_port}")

        # 2. 如果已有账号和设备配置，启动 DLNA 和 AirPlay 服务
        if (self.config.account or self.config.cookie) and self.config.mi_did:
            await self._start_dlna_services()
        else:
            if not self.config.account and not self.config.cookie:
                log.info("未配置小米账号，请打开 Web 管理界面进行配置")
            elif not self.config.mi_did:
                log.info("未选择音箱设备，请打开 Web 管理界面选择设备")
            log.info(f"请访问 http://{self.config.hostname}:{self.config.web_port} 进行配置")

    async def _start_dlna_services(self):
        """启动 DLNA 相关服务 (登录、初始化音箱、SSDP、HTTP)"""
        try:
            # 登录小米
            await self.auth.login()

            # 检查登录状态
            if not self.auth.is_logged_in():
                log.warning("登录失败，无法启动 DLNA 服务")
                # 清空渲染器和控制器，避免显示旧设备
                self.renderers.clear()
                self._did_to_udn.clear()
                if hasattr(self, 'speaker_manager'):
                    self.speaker_manager.controllers.clear()
                return

            # 获取设备列表，确保能正常获取新账号的设备
            device_list = await self.auth.get_device_list()
            if not device_list:
                log.warning("未获取到设备列表，无法启动 DLNA 服务")
                # 清空渲染器和控制器，避免显示旧设备
                self.renderers.clear()
                self._did_to_udn.clear()
                if hasattr(self, 'speaker_manager'):
                    self.speaker_manager.controllers.clear()
                return

            # 初始化音箱
            await self.speaker_manager.init_speakers()
            if not self.speaker_manager.controllers:
                log.warning("没有可用的音箱，请检查配置或重新选择设备")
                # 清空渲染器和控制器，避免显示旧设备
                self.renderers.clear()
                self._did_to_udn.clear()
                return

            # 为每个音箱创建 DLNA 渲染器
            self.ssdp_server = SSDPServer(self.config.hostname, self.config.dlna_port)
            self.device_server = DeviceServer(self.config.hostname, self.config.dlna_port, self.config)

            for did, controller in self.speaker_manager.controllers.items():
                speaker = controller.speaker
                udn = speaker.udn
                friendly_name = speaker.get_dlna_name()

                renderer = DLNARenderer(udn, friendly_name, controller, self.config.default_volume, config=self.config)
                self.renderers[udn] = renderer
                self._did_to_udn[did] = udn

                self.ssdp_server.register_renderer(udn, friendly_name)
                self.device_server.register_renderer(renderer)
                log.info(f"已创建渲染器: {friendly_name} (udn={udn})")

            # 启动 SSDP
            await self.ssdp_server.start()

            # 启动 DLNA HTTP 服务
            await self.device_server.start()

            self.dlna_running = True
            self.config.save()

            # 启动 AirPlay 服务 - 每个音箱一个
            await self._start_airplay_for_speakers()

            log.info(f"MiAir 服务启动完成! 共 {len(self.renderers)} 个音箱")
            log.info("手机 DLNA / AirPlay 现在应该能发现这些设备了")

        except Exception as e:
            log.error(f"启动 DLNA 服务失败: {e}")
            # 确保 dlna_running 为 False
            self.dlna_running = False
            # 清空渲染器和控制器，避免显示旧设备
            self.renderers.clear()
            self._did_to_udn.clear()
            if hasattr(self, 'speaker_manager'):
                self.speaker_manager.controllers.clear()

    async def _start_airplay_for_speakers(self):
        """为每个音箱启动独立的 AirPlay 接收服务"""
        try:
            if not self.speaker_manager.controllers:
                log.warning("没有可用的音箱，无法启动 AirPlay 服务")
                return

            self.airplay_manager = AirPlayManager(self.config.hostname, config=self.config)
            await self.airplay_manager.start_for_speakers(self.speaker_manager.controllers)
        except Exception as e:
            log.error(f"启动 AirPlay 服务失败: {e}")

    async def restart_dlna_services(self):
        """重启 DLNA 服务 (用户通过 Web 修改配置后调用)"""
        # 先停止现有服务
        await self._stop_dlna_services()
        # 关闭并重新初始化 auth，确保账号切换生效
        await self.auth.close()
        self.auth = AuthManager(self.config)
        # 重建 speaker manager
        self.speaker_manager = SpeakerManager(self.config, self.auth)
        # 启动
        await self._start_dlna_services()
        # 重启 AirPlay
        if self.airplay_manager:
            await self.airplay_manager.restart_for_speakers(self.speaker_manager.controllers)

    async def _stop_dlna_services(self):
        """停止 DLNA 服务"""
        if self.ssdp_server:
            await self.ssdp_server.stop()
            self.ssdp_server = None
        if self.device_server:
            await self.device_server.stop()
            self.device_server = None
        self.renderers.clear()
        self._did_to_udn.clear()
        self.dlna_running = False

    async def stop(self):
        """停止所有服务"""
        log.info("MiAir 正在关闭...")

        await self._stop_dlna_services()
        if self.airplay_manager:
            await self.airplay_manager.stop()
            self.airplay_manager = None
        if self._web_runner:
            # 添加超时，避免卡住
            try:
                await asyncio.wait_for(self._web_runner.cleanup(), timeout=3.0)
            except asyncio.TimeoutError:
                log.warning("Web 服务关闭超时")
        await self.auth.close()

        log.info("MiAir 已关闭")

    async def run_forever(self):
        """运行直到收到终止信号"""
        await self.start()
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    def _setup_logging(self):
        """配置日志"""
        logger = logging.getLogger("miair")
        logger.setLevel(logging.DEBUG if self.config.verbose else logging.INFO)

        # 抑制 asyncio / aiohttp 内部的连接断开噪音日志
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
        logging.getLogger("aiohttp.server").setLevel(logging.WARNING)

        if logger.handlers:
            return

        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d: %(message)s",
            datefmt="[%Y-%m-%d %H:%M:%S]",
        )

        # 控制台 - Windows 下用 UTF-8 流避免 GBK 编码异常
        if sys.platform == "win32":
            import io
            stream = io.TextIOWrapper(
                sys.stderr.buffer, encoding="utf-8", errors="replace",
                line_buffering=True,
            )
            console = logging.StreamHandler(stream)
        else:
            console = logging.StreamHandler()
        console.setFormatter(formatter)
        logger.addHandler(console)

        # 文件 — 每次启动清空，大小上限 500KB（超过自动清空重写）
        if self.config.log_file:
            log_dir = os.path.dirname(self.config.log_file)
            if log_dir:
                os.makedirs(log_dir, exist_ok=True)
            # 启动时清空旧日志
            try:
                open(self.config.log_file, "w", encoding="utf-8").close()
            except OSError:
                pass
            from logging.handlers import RotatingFileHandler
            file_handler = RotatingFileHandler(
                self.config.log_file,
                maxBytes=500 * 1024,   # 500KB
                backupCount=0,         # 不保留备份，超限直接清空重写
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
