"""SSDP 组播发现服务"""

import asyncio
import logging
import random
import socket
import struct
import time

from miair.const import (
    AVTRANSPORT_URN,
    CONNECTION_MANAGER_URN,
    DEVICE_TYPE,
    RENDERING_CONTROL_URN,
    SSDP_ADDR,
    SSDP_ALIVE_INTERVAL,
    SSDP_PORT,
)

log = logging.getLogger("miair")


class SSDPServer:
    """SSDP 组播服务，用于设备发现"""

    def __init__(self, hostname: str, dlna_port: int):
        self.hostname = hostname
        self.dlna_port = dlna_port
        self.renderers: dict[str, str] = {}  # udn -> friendly_name
        self._transport = None
        self._alive_task = None
        self._sock = None

    def register_renderer(self, udn: str, friendly_name: str):
        """注册一个渲染器"""
        self.renderers[udn] = friendly_name
        log.info(f"SSDP 注册渲染器: {friendly_name} (uuid:{udn})")

    def _get_location(self, udn: str) -> str:
        """获取设备描述 URL"""
        return f"http://{self.hostname}:{self.dlna_port}/device/{udn}/description.xml"

    def _get_search_targets(self, udn: str) -> list[tuple[str, str]]:
        """获取所有需要通告的 ST 和 USN 对"""
        uuid_str = f"uuid:{udn}"
        return [
            ("upnp:rootdevice", f"{uuid_str}::upnp:rootdevice"),
            (uuid_str, uuid_str),
            (DEVICE_TYPE, f"{uuid_str}::{DEVICE_TYPE}"),
            (AVTRANSPORT_URN, f"{uuid_str}::{AVTRANSPORT_URN}"),
            (RENDERING_CONTROL_URN, f"{uuid_str}::{RENDERING_CONTROL_URN}"),
            (CONNECTION_MANAGER_URN, f"{uuid_str}::{CONNECTION_MANAGER_URN}"),
        ]

    def _build_msearch_response(self, st: str, usn: str, udn: str) -> bytes:
        """构建 M-SEARCH 响应"""
        location = self._get_location(udn)
        response = (
            "HTTP/1.1 200 OK\r\n"
            f"CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: {location}\r\n"
            f"SERVER: MiAir/1.0 UPnP/1.0\r\n"
            f"ST: {st}\r\n"
            f"USN: {usn}\r\n"
            f"EXT:\r\n"
            "\r\n"
        )
        return response.encode("utf-8")

    def _build_notify_alive(self, nt: str, usn: str, udn: str) -> bytes:
        """构建 NOTIFY alive 消息"""
        location = self._get_location(udn)
        notify = (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            f"CACHE-CONTROL: max-age=1800\r\n"
            f"LOCATION: {location}\r\n"
            f"NT: {nt}\r\n"
            f"NTS: ssdp:alive\r\n"
            f"SERVER: MiAir/1.0 UPnP/1.0\r\n"
            f"USN: {usn}\r\n"
            "\r\n"
        )
        return notify.encode("utf-8")

    def _build_notify_byebye(self, nt: str, usn: str) -> bytes:
        """构建 NOTIFY byebye 消息"""
        notify = (
            "NOTIFY * HTTP/1.1\r\n"
            f"HOST: {SSDP_ADDR}:{SSDP_PORT}\r\n"
            f"NT: {nt}\r\n"
            f"NTS: ssdp:byebye\r\n"
            f"USN: {usn}\r\n"
            "\r\n"
        )
        return notify.encode("utf-8")

    async def start(self):
        """启动 SSDP 服务"""
        loop = asyncio.get_running_loop()

        # 创建 UDP socket
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

        # Windows 兼容: SO_REUSEPORT 不存在
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass

        self._sock.bind(("", SSDP_PORT))

        # 加入组播组
        mreq = struct.pack(
            "4s4s",
            socket.inet_aton(SSDP_ADDR),
            socket.inet_aton("0.0.0.0"),  # 使用INADDR_ANY
        )
        self._sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        self._sock.setblocking(False)

        # 创建 asyncio datagram endpoint
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: SSDPProtocol(self),
            sock=self._sock,
        )

        # 发送初始 alive
        await self._send_alive()

        # 启动定期 alive 任务
        self._alive_task = asyncio.create_task(self._periodic_alive())
        log.info(f"SSDP 服务已启动 (监听 {SSDP_ADDR}:{SSDP_PORT})")

    async def stop(self):
        """停止 SSDP 服务"""
        # 发送 byebye（添加超时，避免卡住）
        try:
            await asyncio.wait_for(self._send_byebye(), timeout=2.0)
        except asyncio.TimeoutError:
            log.warning("SSDP byebye 发送超时")
        except Exception as e:
            pass

        if self._alive_task:
            self._alive_task.cancel()
            try:
                await asyncio.wait_for(self._alive_task, timeout=2.0)
            except asyncio.TimeoutError:
                log.warning("SSDP alive 任务取消超时")
            except asyncio.CancelledError:
                pass

        if self._transport:
            self._transport.close()
            self._transport = None

        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

        log.info("SSDP 服务已停止")

    async def _send_alive(self):
        """发送 NOTIFY alive"""
        if not self._transport:
            return
        for udn in self.renderers:
            for nt, usn in self._get_search_targets(udn):
                data = self._build_notify_alive(nt, usn, udn)
                self._transport.sendto(data, (SSDP_ADDR, SSDP_PORT))

    async def _send_byebye(self):
        """发送 NOTIFY byebye"""
        if not self._transport:
            return
        for udn in self.renderers:
            for nt, usn in self._get_search_targets(udn):
                data = self._build_notify_byebye(nt, usn)
                self._transport.sendto(data, (SSDP_ADDR, SSDP_PORT))

    async def _periodic_alive(self):
        """定期发送 alive 通告"""
        try:
            while True:
                await asyncio.sleep(SSDP_ALIVE_INTERVAL + random.uniform(-5, 5))
                await self._send_alive()
        except asyncio.CancelledError:
            pass

    def handle_msearch(self, data: bytes, addr: tuple):
        """处理 M-SEARCH 请求"""
        try:
            message = data.decode("utf-8")
        except UnicodeDecodeError:
            return

        if "M-SEARCH" not in message:
            return

        # 解析 ST (Search Target)
        st = ""
        mx = 3
        for line in message.split("\r\n"):
            lower = line.lower()
            if lower.startswith("st:"):
                st = line.split(":", 1)[1].strip()
            elif lower.startswith("mx:"):
                try:
                    mx = int(line.split(":", 1)[1].strip())
                except ValueError:
                    mx = 3

        if not st:
            return

        # 为每个匹配的渲染器发送响应
        for udn in self.renderers:
            targets = self._get_search_targets(udn)
            for target_st, target_usn in targets:
                if st == "ssdp:all" or st == target_st:
                    response = self._build_msearch_response(target_st, target_usn, udn)
                    # 延迟随机时间响应 (0 ~ MX 秒)
                    delay = random.uniform(0, min(mx, 3))
                    asyncio.get_running_loop().call_later(
                        delay,
                        self._transport.sendto,
                        response,
                        addr,
                    )


class SSDPProtocol(asyncio.DatagramProtocol):
    """SSDP UDP 协议处理"""

    def __init__(self, server: SSDPServer):
        self.server = server

    def datagram_received(self, data: bytes, addr: tuple):
        self.server.handle_msearch(data, addr)

    def error_received(self, exc):
        log.warning(f"SSDP 错误: {exc}")
