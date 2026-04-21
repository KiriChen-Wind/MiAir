"""语音命令轮询模块

采用 xiaomusic 的对话记录拉取逻辑，支持两种方式：
1. 通过小爱 API 直接获取
2. 通过 Mina 服务获取（适用于特定硬件）
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Callable

import aiohttp
from aiohttp import ClientSession, ClientTimeout

from miair.const import LATEST_ASK_API, GET_ASK_BY_MINA

if TYPE_CHECKING:
    from miair.auth import AuthManager
    from miair.config import Config
    from miair.dlna.renderer import DLNARenderer

log = logging.getLogger("miair")

# 关键词 -> 动作映射 (参照 xiaomusic 的 key_word_dict)
KEYWORD_ACTIONS: dict[str, str] = {
    "暂停": "pause",
    "停止": "stop",
    "停止播放": "stop",
    "关机": "stop",
    "下一首": "next",
    "下一曲": "next",
    "上一首": "previous",
    "上一曲": "previous",
    "继续播放": "resume",
    "继续": "resume",
    "播放": "resume",
}

# 匹配优先级 (长的优先匹配，避免 "停止播放" 只匹配到 "播放")
KEYWORD_MATCH_ORDER: list[str] = sorted(KEYWORD_ACTIONS.keys(), key=len, reverse=True)


class ConversationPoller:
    """对话记录轮询器，采用 xiaomusic 的实现方式"""

    def __init__(
        self,
        config: Config,
        auth: AuthManager,
        get_renderers: Callable[[], dict[str, DLNARenderer]],
    ):
        """初始化对话轮询器

        Args:
            config: 配置对象
            auth: 认证管理器实例
            get_renderers: 获取渲染器的回调函数
        """
        self.config = config
        self.auth = auth
        self._get_renderers = get_renderers
        self._last_timestamp: dict[str, int] = {}  # device_id -> 上次记录的时间戳
        self._task: asyncio.Task | None = None
        self._session: ClientSession | None = None  # 独立的轮询 session

        # 内部事件管理
        self.polling_event = asyncio.Event()
        self.new_record_event = asyncio.Event()

        # 存储最新的对话记录
        self.last_record = None

    async def start(self):
        """启动轮询"""
        self._task = asyncio.create_task(self.run_conversation_loop())
        log.info("语音控制轮询已启动")

    async def stop(self):
        """停止轮询"""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
        log.info("语音控制轮询已停止")

    async def run_conversation_loop(self):
        """运行对话循环

        持续运行的主循环，负责：
        1. 启动对话轮询任务
        2. 等待新对话记录
        3. 处理对话命令
        """
        # 启动轮询任务
        async with ClientSession() as session:
            self._session = session
            task = asyncio.create_task(self.poll_latest_ask(session))
            
            try:
                while True:
                    self.polling_event.set()
                    await self.new_record_event.wait()
                    self.new_record_event.clear()
                    new_record = self.last_record
                    self.polling_event.clear()  # 处理命令时停止轮询

                    query = new_record.get("query", "").strip()
                    device_id = new_record.get("device_id", "").strip()
                    await self._handle_command(device_id, query, new_record.get("time", 0))

            except asyncio.CancelledError:
                log.info("Conversation loop cancelled, cleaning up...")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                raise

    async def poll_latest_ask(self, session: ClientSession):
        """轮询最新对话记录

        持续运行的协程，定期从所有设备拉取最新对话记录。
        根据硬件类型选择合适的获取方式。

        Args:
            session: aiohttp客户端会话
        """
        try:
            while True:
                if not self.config.enable_voice_control:
                    await asyncio.sleep(5)
                    continue

                tasks = []
                renderers = self._get_renderers()
                for udn, renderer in renderers.items():
                    if not renderer.speaker:
                        continue
                    device_id = renderer.speaker.device_id
                    if not device_id:
                        continue

                    # 首次用当前时间初始化
                    if device_id not in self._last_timestamp:
                        self._last_timestamp[device_id] = int(time.time() * 1000)

                    hardware = getattr(
                        getattr(renderer.speaker, 'speaker', None),
                        'hardware', ''
                    )
                    if not hardware:
                        continue
                    # 根据硬件类型选择获取方式
                    if hardware in GET_ASK_BY_MINA:
                        tasks.append(self.get_latest_ask_by_mina(device_id))
                    else:
                        tasks.append(
                            self.get_latest_ask_from_xiaoai(session, device_id, hardware)
                        )
                
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)

                # 控制轮询间隔
                start = time.perf_counter()
                await self.polling_event.wait()
                if self.config.voice_poll_interval <= 1:
                    if (d := time.perf_counter() - start) < 1:
                        await asyncio.sleep(1 - d)
                else:
                    sleep_sec = 0
                    while sleep_sec < self.config.voice_poll_interval:
                        await asyncio.sleep(1)
                        sleep_sec += 1
        except asyncio.CancelledError:
            log.info("Polling task cancelled")
            raise

    async def get_latest_ask_from_xiaoai(self, session: ClientSession, device_id: str, hardware: str):
        """从小爱API获取最新对话

        通过HTTP请求小爱API获取指定设备的最新对话记录。
        包含重试机制和错误处理。

        Args:
            session: aiohttp客户端会话
            device_id: 设备ID
            hardware: 硬件类型

        Returns:
            None - 通过 _check_last_query 更新内部状态
        """
        cookies = self._get_api_cookies(device_id)
        if not cookies:
            return

        retries = 3
        for i in range(retries):
            try:
                timeout = ClientTimeout(total=5)  # 减少超时时间，提高响应速度
                url = LATEST_ASK_API.format(
                    hardware=hardware,
                    timestamp=str(int(time.time() * 1000)),
                )
                r = await session.get(url, timeout=timeout, cookies=cookies)

                # 检查响应状态码
                if r.status != 200:
                    # 401 时触发重新登录
                    if i == 2 and r.status == 401:
                        log.info("对话 API 返回 401，尝试重新登录")
                        await self.auth.login()
                    continue

            except asyncio.CancelledError:
                log.warning("Task was cancelled.")
                return
            except Exception as e:
                continue

            try:
                data = await r.json()
            except Exception as e:
                if i == 2:
                    log.info("多次解析失败，尝试重新登录")
                    await self.auth.login()
            else:
                return self._parse_conversation(device_id, data)

    async def get_latest_ask_by_mina(self, device_id: str):
        """通过Mina服务获取最新对话

        使用Mina服务API获取对话记录，适用于特定硬件类型。

        Args:
            device_id: 设备ID

        Returns:
            None - 通过 _check_last_query 更新内部状态
        """
        try:
            # 动态获取最新的 mina_service
            if self.auth.mina_service is None:
                log.warning(f"mina_service is None, skip get_latest_ask_by_mina for device {device_id}")
                return
            messages = await self.auth.mina_service.get_latest_ask(device_id)
            for message in messages:
                query = message.response.answer[0].question
                answer = message.response.answer[0].content
                last_record = {
                    "time": message.timestamp_ms,
                    "device_id": device_id,
                    "query": query,
                    "answer": answer,
                }
                self._check_last_query(last_record)
        except Exception as e:
            pass
        return

    def _get_api_cookies(self, device_id: str) -> dict | None:
        """构造云端对话 API 所需的完整认证 cookie

        API 需要三个 cookie: userId, serviceToken, deviceId
        """
        account = self.auth.account
        if not account or not account.token:
            return None

        token = account.token
        user_id = token.get("userId")
        micoapi = token.get("micoapi")
        if not user_id or not micoapi:
            return None

        service_token = micoapi[1] if isinstance(micoapi, (list, tuple)) else None
        if not service_token:
            return None

        return {
            "userId": str(user_id),
            "serviceToken": service_token,
            "deviceId": device_id,
        }

    def _parse_conversation(self, device_id: str, data: dict):
        """从API响应数据中提取最后一条对话

        解析小爱API返回的JSON数据，提取最新的对话记录。

        Args:
            device_id: 设备ID
            data: API响应数据

        Returns:
            None - 通过 _check_last_query 更新内部状态
        """
        if d := data.get("data"):
            try:
                if isinstance(d, str):
                    records_data = json.loads(d)
                else:
                    records_data = d
                records = records_data.get("records")
                if not records:
                    return
                last_record = records[0]
                last_record["device_id"] = device_id
                answers = last_record.get("answers", [{}])
                if answers:
                    answer = answers[0].get("tts", {}).get("text", "").strip()
                    last_record["answer"] = answer
                self._check_last_query(last_record)
            except Exception as e:
                pass

    def _check_last_query(self, last_record):
        """检查并更新最后一条对话记录

        验证对话记录的时间戳，如果是新记录则更新并触发事件。

        Args:
            last_record: 对话记录字典，包含 device_id、time、query、answer 等字段
        """
        device_id = last_record["device_id"]
        timestamp = last_record.get("time")
        query = last_record.get("query", "").strip()

        if timestamp > self._last_timestamp[device_id]:
            self._last_timestamp[device_id] = timestamp
            self.last_record = last_record
            self.new_record_event.set()

    async def _handle_command(self, device_id: str, query: str, command_time: int = 0):
        """匹配语音命令并执行对应的 DLNA 动作"""
        action = None
        for keyword in KEYWORD_MATCH_ORDER:
            if keyword in query:
                action = KEYWORD_ACTIONS[keyword]
                break

        if not action:
            return

        delay_str = ""
        if command_time > 0:
            delay = time.time() - command_time / 1000
            delay_str = f" (延迟 {delay:.1f}s)"

        # 查找对应的渲染器
        renderers = self._get_renderers()
        target_renderer = None
        for udn, renderer in renderers.items():
            if renderer.speaker and renderer.speaker.device_id == device_id:
                target_renderer = renderer
                break

        if not target_renderer:
            log.warning(f"未找到设备 {device_id} 对应的渲染器")
            return

        log.info(f"[{target_renderer.friendly_name}] 语音命令: '{query}' -> {action}{delay_str}")

        try:
            if action == "pause":
                await target_renderer.pause()
            elif action == "stop":
                await target_renderer.stop()
            elif action == "resume":
                await target_renderer.play()
            elif action == "next":
                await target_renderer.next_track()
            elif action == "previous":
                await target_renderer.previous_track()
        except Exception as e:
            log.error(f"[{target_renderer.friendly_name}] 执行语音命令失败: {e}")
