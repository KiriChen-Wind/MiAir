"""小米账号认证管理"""

import logging
import os
import re

import aiohttp
from miservice import MiAccount, MiIOService, MiNAService

from miair.config import Config

log = logging.getLogger("miair")


def parse_cookie_string(cookie_str: str) -> dict:
    """解析 cookie 字符串，提取 userId 和 passToken"""
    result = {}
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            key, value = item.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key in ("userId", "passToken"):
                result[key] = value
    return result


class AuthManager:
    """管理小米账号认证和设备服务"""

    def __init__(self, config: Config):
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.account: MiAccount | None = None
        self.mina_service: MiNAService | None = None
        self.miio_service: MiIOService | None = None
        self._logged_in = False

    async def login(self):
        """登录小米账号并初始化服务"""
        os.makedirs(self.config.conf_path, exist_ok=True)

        # 创建 aiohttp session（必须设置超时，否则 miservice HTTP 调用可能无限挂起导致卡死）
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15, connect=5, sock_read=10)
            )

        token_store = self.config.mi_token_home

        # 如果有 cookie，使用 cookie 中的信息创建 MiAccount
        token_data = {}
        if self.config.cookie:
            token_data = parse_cookie_string(self.config.cookie)
        
        # 创建 MiAccount，如果使用 cookie 登录，传入空的账号密码
        if token_data.get("userId") and token_data.get("passToken"):
            # 使用 cookie 登录，传入空的账号密码，避免触发密码登录流程
            self.account = MiAccount(
                self.session,
                "",  # 空账号
                "",  # 空密码
                token_store=token_store,
            )
            # 设置 token，包含所有必要字段
            self.account.token = {
                "userId": token_data["userId"],
                "passToken": token_data["passToken"],
                "deviceId": "miair_device",
                "ssecurity": "",
                "serviceToken": "",
            }
            log.info("使用 cookie 登录")
        else:
            # 使用账号密码登录
            self.account = MiAccount(
                self.session,
                self.config.account,
                self.config.password,
                token_store=token_store,
            )

        # 显式调用 login
        # 如果使用 cookie 登录，跳过 login 调用，直接标记为已登录
        if token_data.get("userId") and token_data.get("passToken"):
            self._logged_in = True
            log.info("使用 cookie 登录成功")
        else:
            try:
                await self.account.login("micoapi")
                self._logged_in = True
                log.info("小米账号登录成功")
            except Exception as e:
                self._logged_in = False
                err_msg = str(e)
                err_code = self._extract_error_code(err_msg)
                if err_code == "87001" or "captcha" in err_msg.lower():
                    log.error(
                        "登录需要验证码! 请在浏览器访问 https://account.xiaomi.com 完成验证后重试，"
                        "或使用 cookie 方式登录"
                    )
                elif err_code == "70016":
                    log.error(
                        "登录验证失败! 可能原因：密码错误、需要关闭二次验证、"
                        "或需要在 https://www.mi.com 完成人机验证。"
                        "建议使用 cookie 方式登录。"
                    )
                elif "userId" in err_msg:
                    log.error(
                        "登录失败(缺少userId)! 小米账号可能需要额外验证。"
                        "请尝试以下方法：\n"
                        "  1. 在浏览器登录 https://account.xiaomi.com 完成验证\n"
                        "  2. 使用 cookie 方式登录（在设置中填入 cookie）\n"
                        "  3. 确保关闭了代理/VPN"
                    )
                else:
                    log.error(f"登录失败: {e}")

        # 无论是否登录成功，都设置 service (方便后续重试)
        self.mina_service = MiNAService(self.account)
        self.miio_service = MiIOService(self.account)

    async def ensure_login(self):
        """确保已登录，未登录则尝试登录"""
        if self.mina_service is None:
            await self.login()

    @staticmethod
    def _extract_error_code(err_msg: str) -> str:
        """从异常消息中提取数字错误码"""
        m = re.search(r'\b(\d{4,6})\b', err_msg)
        return m.group(1) if m else ""

    async def get_device_list(self) -> list[dict]:
        """获取账号下所有设备列表"""
        await self.ensure_login()
        if not self._logged_in:
            log.warning("未成功登录，无法获取设备列表")
            return []
        try:
            devices = await self.mina_service.device_list()
            return devices or []
        except Exception as e:
            log.warning(f"获取设备列表失败: {e}")
            # 可能 token 过期，尝试重新登录
            # 但如果使用 cookie 登录，不要重新调用 login（避免 KeyError）
            if self.config.cookie:
                log.error(f"Cookie 可能已过期，请重新获取: {e}")
                return []
            await self.close()
            await self.login()
            if not self._logged_in:
                return []
            try:
                devices = await self.mina_service.device_list()
                return devices or []
            except Exception as e2:
                log.error(f"重新登录后仍然失败: {e2}")
                return []

    async def update_speakers_info(self):
        """从云端获取设备信息，更新 speakers 配置"""
        devices = await self.get_device_list()
        did_list = self.config.get_did_list()

        for device in devices:
            miot_did = device.get("miotDID", "")
            if miot_did in did_list:
                speaker = self.config.get_speaker(miot_did)
                speaker.device_id = device.get("deviceID", "")
                speaker.hardware = device.get("hardware", "")
                if not speaker.name:
                    speaker.name = device.get("name", "")
                speaker.ensure_udn()
                log.info(
                    f"已更新设备信息: {speaker.name} "
                    f"(did={miot_did}, device_id={speaker.device_id}, "
                    f"hardware={speaker.hardware})"
                )

    def is_logged_in(self) -> bool:
        """是否已成功登录"""
        return self._logged_in

    async def close(self):
        """关闭 session"""
        if self.session and not self.session.closed:
            await self.session.close()
        self.session = None
        self.account = None
        self.mina_service = None
        self.miio_service = None
        self._logged_in = False
