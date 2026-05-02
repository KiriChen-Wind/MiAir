"""UPnP 事件订阅管理"""

import asyncio
import logging
import time
import uuid

import aiohttp
from xml.sax.saxutils import escape

log = logging.getLogger("miair")


class Subscription:
    """一个事件订阅"""

    def __init__(self, sid: str, callback_url: str, timeout: int = 1800):
        self.sid = sid
        self.callback_url = callback_url
        self.timeout = timeout
        self.created_at = time.time()
        self.seq: int = 0

    @property
    def expired(self) -> bool:
        return (time.time() - self.created_at) > self.timeout

    def renew(self, timeout: int = 1800):
        self.timeout = timeout
        self.created_at = time.time()


class EventManager:
    """UPnP 事件订阅管理器 (参照 MaCast: 持久连接 + 非阻塞发送)"""

    def __init__(self):
        self._subscriptions: dict[str, Subscription] = {}  # sid -> Subscription
        self._cleanup_task: asyncio.Task | None = None
        self._session: aiohttp.ClientSession | None = None
        self._session_idle_since: float = 0.0  # session 空闲开始时间
        _SESSION_IDLE_TIMEOUT = 300  # session 空闲 5 分钟后关闭

    def _get_session(self) -> aiohttp.ClientSession:
        """获取或创建持久 HTTP session"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=5)
            )
        self._session_idle_since = 0.0  # 正在使用，重置空闲计时
        return self._session

    async def _close_idle_session(self):
        """关闭空闲的 session 以释放资源"""
        if (self._session and not self._session.closed
                and not self._subscriptions):
            await self._session.close()
            self._session = None

    def subscribe(self, callback_url: str, timeout: int = 1800) -> str:
        """创建新订阅"""
        sid = f"uuid:{uuid.uuid4()}"
        self._subscriptions[sid] = Subscription(sid, callback_url, timeout)
        log.info(f"新订阅: SID={sid} callback={callback_url} timeout={timeout}s")
        return sid

    def renew(self, sid: str, timeout: int = 1800) -> bool:
        """续订"""
        if sid in self._subscriptions:
            self._subscriptions[sid].renew(timeout)
            return True
        return False

    def unsubscribe(self, sid: str) -> bool:
        """取消订阅"""
        if sid in self._subscriptions:
            del self._subscriptions[sid]
            log.info(f"取消订阅: SID={sid}")
            return True
        return False

    def has_subscribers(self) -> bool:
        """检查是否有未过期的订阅者"""
        return any(not sub.expired for sub in self._subscriptions.values())

    async def notify_all(self, event_xml: str):
        """向所有活跃订阅者发送事件通知 (fire-and-forget，不等待慢订阅者)"""
        expired_sids = []
        for sid, sub in self._subscriptions.items():
            if sub.expired:
                expired_sids.append(sid)
                continue
            # fire-and-forget: 创建任务但不等待，避免慢订阅者阻塞事件通知
            task = asyncio.create_task(self._send_notify(sub, event_xml))
            task.add_done_callback(lambda t: None)  # 阻止未捕获异常警告

        for sid in expired_sids:
            del self._subscriptions[sid]

    async def _send_notify(self, sub: Subscription, event_xml: str):
        """发送 NOTIFY 到订阅者 (使用持久 session)"""
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "NT": "upnp:event",
            "NTS": "upnp:propchange",
            "SID": sub.sid,
            "SEQ": str(sub.seq),
        }
        sub.seq += 1
        try:
            session = self._get_session()
            async with session.request(
                "NOTIFY",
                sub.callback_url,
                headers=headers,
                data=event_xml,
            ):
                pass
        except Exception as e:
            pass

    def start_cleanup(self):
        """启动定期清理过期订阅的任务"""
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self):
        """定期清理过期订阅和空闲 session"""
        try:
            while True:
                await asyncio.sleep(60)  # 从 300 秒缩短到 60 秒，更及时释放资源
                expired = [
                    sid for sid, sub in self._subscriptions.items() if sub.expired
                ]
                for sid in expired:
                    del self._subscriptions[sid]
                # 没有订阅者时关闭空闲 session
                if not self._subscriptions:
                    await self._close_idle_session()
        except asyncio.CancelledError:
            pass

    async def stop(self):
        """停止事件管理器"""
        if self._cleanup_task:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
        if self._session and not self._session.closed:
            await self._session.close()


def build_last_change_event(transport_state: str = "", volume: int = -1) -> str:
    """构建 LastChange 事件 XML (参照 MaCast: 内部 Event XML 整体转义)"""
    event_parts = []
    if transport_state:
        event_parts.append(
            f'<TransportState val="{transport_state}"/>'
        )
    if volume >= 0:
        event_parts.append(f'<Volume channel="Master" val="{volume}"/>')

    inner = "".join(event_parts)
    event_xml = (
        '<Event xmlns="urn:schemas-upnp-org:metadata-1-0/AVT/">'
        f'<InstanceID val="0">{inner}</InstanceID>'
        '</Event>'
    )
    escaped_event = escape(event_xml)

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<e:propertyset xmlns:e="urn:schemas-upnp-org:event-1-0">\n'
        "  <e:property>\n"
        f"    <LastChange>{escaped_event}</LastChange>\n"
        "  </e:property>\n"
        "</e:propertyset>"
    )
