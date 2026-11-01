"""AirPlay 接收服务主模块

基于 airplay2-receiver 的核心逻辑，简化为仅接收音频并输出到 HTTP 流。
支持 AirPlay 1 (RAOP) 协议。
"""

import asyncio
import base64
from collections import deque
from dataclasses import dataclass
import logging
import queue
import socket
import struct
import threading
import time
from typing import Callable

import av
from Crypto.Cipher import AES

from miair.airplay.audio_stream import AudioStreamServer
from miair.airplay.mdns import AirPlayMDNS
from miair.airplay.playfair import PlayFair
from miair.config import Config

log = logging.getLogger("miair")


# ============================================================
# 核心数据结构
# ============================================================

NTP_EPOCH_OFFSET = 2208988800.0  # 1900-01-01 → 1970-01-01 的秒数

# --- RTP 接收参数 ---
_RTP_HEADER_LEN = 12
_PREBUFFER_PACKETS = 32          # 开始输出前先攒 ~256ms，跨过开头的网络抖动
_GAP_DETECT_MIN = 8              # jitter buffer 里至少这么多包才做缺口检测
_RETRANSMIT_INTERVAL = 0.030     # 重传请求起始间隔（之后翻倍）
_RETRANSMIT_MAX_INTERVAL = 0.080
_RETRANSMIT_GIVE_UP = 0.150      # 超过该时长仍未补到 → 补静音跳过（AirPlay 重传通常 <50ms）
_MAX_GAP_FILL = 32               # 一次最多补多少帧静音
_DECODE_QUEUE_MAX = 200          # 解码队列 ≈ 1.6s
_STATS_EVERY_FRAMES = 1500       # 每输出多少帧打一条统计（≈12s）


class JitterBuffer:
    """RTP 乱序重排缓冲（按 seq 排序，容量约 4.6 秒 @ 44100Hz/352spf）"""

    def __init__(self, max_size: int = 512):
        self._max_size = max_size
        self._packets: dict[int, bytes] = {}
        self._order: deque[int] = deque()

    def insert(self, seq: int, payload: bytes) -> None:
        """插入音频包；重复 seq 覆盖，满载淘汰最旧的包"""
        if seq in self._packets:
            self._packets[seq] = payload
            return
        while len(self._packets) >= self._max_size:
            self._packets.pop(self._order.popleft(), None)
        self._packets[seq] = payload
        self._order.append(seq)

    def drain(self, next_seq: int) -> list[bytes]:
        """从 next_seq 起取出连续可播的包，遇到缺口停止"""
        payloads = []
        seq = next_seq
        while seq in self._packets:
            payloads.append(self._packets.pop(seq))
            seq = (seq + 1) & 0xFFFF
        return payloads

    def gap_missing(self, next_seq: int) -> list[int]:
        """next_seq 到下一个已到达包之间的缺失 seq（最多扫 32 个）"""
        missing = []
        seq = next_seq
        for _ in range(32):
            if seq in self._packets:
                return missing
            missing.append(seq)
            seq = (seq + 1) & 0xFFFF
        return missing[:8]

    def next_available_after(self, seq: int) -> int | None:
        """seq 之后最近的已到达 seq"""
        s = seq
        for _ in range(self._max_size):
            if s in self._packets:
                return s
            s = (s + 1) & 0xFFFF
        return None

    def clear(self) -> None:
        self._packets.clear()
        self._order.clear()

    def __len__(self) -> int:
        return len(self._packets)


@dataclass
class Retransmit:
    """单个缺失包的重传状态"""
    first_seen: float
    last_request: float
    attempts: int = 1


def decrypt_packet(key: bytes, iv: bytes, payload: bytes) -> bytes:
    """FairPlay 的 AES-128-CBC 解密（只解密 16 字节对齐的部分）

    注意：pycryptodome 的 CBC cipher 对象是「有状态」的，必须每包新建；
    复用会让第二个包之后全部解错，表现就是有数据流但完全没声音。
    """
    aligned = len(payload) & ~0xF
    if aligned == 0:
        return payload
    plain = AES.new(key, AES.MODE_CBC, iv).decrypt(payload[:aligned])
    return plain + payload[aligned:] if aligned < len(payload) else plain


def _as_frames(resampled) -> list:
    """PyAV 的 resample() 返回单帧或帧列表，统一成列表"""
    return resampled if isinstance(resampled, list) else [resampled]


# AirPort 私钥 (用于 AirPlay 1 RSA 认证)
AIRPORT_PRIVATE_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEpQIBAAKCAQEA59dE8qLieItsH1WgjrcFRKj6eUWqi+bGLOX1HL3U3GhC/j0Qg90u3sG/1CUt\n"
    "wC5vOYvfDmFI6oSFXi5ELabWJmT2dKHzBJKa3k9ok+8t9ucRqMd6DZHJ2YCCLlDRKSKv6kDqnw4U\n"
    "wPdpOMXziC/AMj3Z/lUVX1G7WSHCAWKf1zNS1eLvqr+boEjXuBOitnZ/bDzPHrTOZz0Dew0uowxf\n"
    "/+sG+NCK3eQJVxqcaJ/vEHKIVd2M+5qL71yJQ+87X6oV3eaYvt3zWZYD6z5vYTcrtij2VZ9Zmni/\n"
    "UAaHqn9JdsBWLUEpVviYnhimNVvYFZeCXg/IdTQ+x4IRdiXNv5hEewIDAQABAoIBAQDl8Axy9XfW\n"
    "BLmkzkEiqoSwF0PsmVrPzH9KsnwLGH+QZlvjWd8SWYGN7u1507HvhF5N3drJoVU3O14nDY4TFQAa\n"
    "LlJ9VM35AApXaLyY1ERrN7u9ALKd2LUwYhM7Km539O4yUFYikE2nIPscEsA5ltpxOgUGCY7b7ez5\n"
    "NtD6nL1ZKauw7aNXmVAvmJTcuPxWmoktF3gDJKK2wxZuNGcJE0uFQEG4Z3BrWP7yoNuSK3dii2jm\n"
    "lpPHr0O/KnPQtzI3eguhe0TwUem/eYSdyzMyVx/YpwkzwtYL3sR5k0o9rKQLtvLzfAqdBxBurciz\n"
    "aaA/L0HIgAmOit1GJA2saMxTVPNhAoGBAPfgv1oeZxgxmotiCcMXFEQEWflzhWYTsXrhUIuz5jFu\n"
    "a39GLS99ZEErhLdrwj8rDDViRVJ5skOp9zFvlYAHs0xh92ji1E7V/ysnKBfsMrPkk5KSKPrnjndM\n"
    "oPdevWnVkgJ5jxFuNgxkOLMuG9i53B4yMvDTCRiIPMQ++N2iLDaRAoGBAO9v//mU8eVkQaoANf0Z\n"
    "oMjW8CN4xwWA2cSEIHkd9AfFkftuv8oyLDCG3ZAf0vrhrrtkrfa7ef+AUb69DNggq4mHQAYBp7L+\n"
    "k5DKzJrKuO0r+R0YbY9pZD1+/g9dVt91d6LQNepUE/yY2PP5CNoFmjedpLHMOPFdVgqDzDFxU8hL\n"
    "AoGBANDrr7xAJbqBjHVwIzQ4To9pb4BNeqDndk5Qe7fT3+/H1njGaC0/rXE0Qb7q5ySgnsCb3DvA\n"
    "cJyRM9SJ7OKlGt0FMSdJD5KG0XPIpAVNwgpXXH5MDJg09KHeh0kXo+QA6viFBi21y340NonnEfdf\n"
    "54PX4ZGS/Xac1UK+pLkBB+zRAoGAf0AY3H3qKS2lMEI4bzEFoHeK3G895pDaK3TFBVmD7fV0Zhov\n"
    "17fegFPMwOII8MisYm9ZfT2Z0s5Ro3s5rkt+nvLAdfC/PYPKzTLalpGSwomSNYJcB9HNMlmhkGzc\n"
    "1JnLYT4iyUyx6pcZBmCd8bD0iwY/FzcgNDaUmbX9+XDvRA0CgYEAkE7pIPlE71qvfJQgoA9em0gI\n"
    "LAuE4Pu13aKiJnfft7hIjbK+5kyb3TysZvoyDnb3HOKvInK7vXbKuU4ISgxB2bB3HcYzQMGsz1qJ\n"
    "2gG0N5hvJpzwwhbhXqFKA4zaaSrw622wDniAK5MlIE0tIAKKP4yxNGjoD2QYjhBGuhvkWKY=\n"
    "-----END RSA PRIVATE KEY-----"
)


class AP1Security:
    """AirPlay 1 RSA 认证"""

    @staticmethod
    def _modinv(a, m):
        """计算模逆元"""
        def egcd(a, b):
            if a == 0:
                return (b, 0, 1)
            else:
                g, y, x = egcd(b % a, a)
                return (g, x - (b // a) * y, y)
        g, x, y = egcd(a, m)
        if g != 1:
            raise Exception('modular inverse does not exist')
        else:
            return x % m

    @staticmethod
    def compute_apple_response(apple_challenge: str, request_host: bytes, device_id: bytes) -> str:
        from Crypto.PublicKey import RSA

        RSA_KEYLEN = 256

        if apple_challenge[-2:] != "==":
            apple_challenge += "=="
        data = base64.b64decode(apple_challenge)
        data = data.ljust(32, b"\0")

        message = b"\x00\x01"
        message += b"\xFF" * (RSA_KEYLEN - 32 - 3)
        message += b"\x00"
        message += data
        message += request_host
        message += device_id

        message_bigint = int.from_bytes(message, "big")
        key = RSA.import_key(AIRPORT_PRIVATE_KEY)

        dP = key.d % (key.p - 1)
        dQ = key.d % (key.q - 1)
        qInv = AP1Security._modinv(key.q, key.p)
        m1 = pow(message_bigint, dP, key.p)
        m2 = pow(message_bigint, dQ, key.q)
        h = (qInv * (m1 - m2)) % key.p
        m = m2 + h * key.q
        mbin = m.to_bytes(RSA_KEYLEN, byteorder="big")
        m64 = base64.b64encode(mbin)
        if m64[-2:] == b"==":
            m64 = m64[:-2]
        return m64.decode("utf-8")


class AirPlayServer:
    """AirPlay 音频接收服务器

    实现 AirPlay 1 (RAOP) 协议接收音频，解码后输出到 HTTP 音频流。
    """

    def __init__(self, hostname: str, device_name: str = "MiAir", shared_zeroconf=None, speaker_hardware: str = ""):
        self.hostname = hostname
        self.device_name = device_name
        self.speaker_hardware = speaker_hardware
        self.device_id = self._generate_device_id()
        self.ipv4 = self._get_ipv4()

        # RTSP 服务器
        self.rtsp_port = 0
        self._rtsp_socket: socket.socket | None = None
        self._rtsp_thread: threading.Thread | None = None
        self._running = False

        # 音频流服务器 - 统一使用 WAV 输出（零编码延迟，不卡顿）
        self._stream_server = AudioStreamServer(hostname, 0, audio_format="wav")
        self.stream_port = 0

        # mDNS 广播
        self._mdns = AirPlayMDNS(hostname, device_name, self.device_id, 0, shared_zeroconf)

        # 音频解码
        self._codec_context = None
        self._resampler = None
        self._session_key: bytes | None = None
        self._session_iv: bytes | None = None
        self._session_iv16: bytes | None = None  # 预切片的 16 字节 IV，避免每包切片
        self._audio_format = 0
        self._sample_rate = 44100
        self._channels = 2
        self._fmtp_params: list[str] = []  # SDP fmtp 参数
        self._silence_frame: bytes = b'\x00' * 1408  # 默认 1 帧静音 (44100Hz/2ch/16bit/352spf)

        # 回调
        self.on_play_start: Callable | None = None
        self.on_play_stop: Callable | None = None
        self.on_volume_change: Callable[[float], None] | None = None

        # FairPlay
        self._playfair = PlayFair()
        self._fp_state = PlayFair.fairplay_s()
        self._fp_keymsg = None
        self._last_volume_db: float = -15.0  # 默认音量
        self._client_name: str = ""  # 连接的客户端设备名称
        self._is_playing: bool = False # 是否正在播放
        self._loop: asyncio.AbstractEventLoop | None = None  # 事件循环引用（用于跨线程回调）

        # RTP 状态跟踪（用于 FLUSH/RECORD 响应的 RTP-Info 头）
        self._last_rtp_seq: int = 0
        self._last_rtp_timestamp: int = 0
        # RTCP 重传请求
        self._rtcp_control_socket: socket.socket | None = None
        self._rtcp_control_addr: tuple | None = None  # iPhone 的 control 地址
        self._rtp_data_socket: socket.socket | None = None  # RTP 数据 socket（用于注入重传包）
        self._flush_flag = threading.Event()  # FLUSH 请求标志（跨线程通知）

        self._rtsp_client_addr: tuple | None = None  # RTSP 客户端 IP

        # RTP 侧诊断统计（丢包/重传/解码失败），卡顿时先看这条日志
        self._rtp_stats: dict[str, int] = {}
        self._reset_rtp_stats()

    def _generate_device_id(self) -> str:
        """生成设备 MAC 地址格式的 ID

        基于设备名生成唯一 ID，确保每个音箱有不同的 ID。
        """
        # 使用设备名的 hash 来生成伪 MAC 地址，确保每个设备名对应唯一的 ID
        import hashlib
        h = hashlib.md5(self.device_name.encode()).hexdigest()[:12]
        return ":".join(f"{h[i:i+2].upper()}" for i in range(0, 12, 2))

    @property
    def device_id_bin(self) -> bytes:
        """获取设备 ID 的二进制格式（6 字节）"""
        return int(self.device_id.replace(":", ""), base=16).to_bytes(6, "big")

    def _get_ipv4(self) -> str:
        """获取 AirPlay 认证使用的 IPv4，保持与服务发布地址一致。"""
        try:
            socket.inet_pton(socket.AF_INET, self.hostname)
            if self.hostname not in ("0.0.0.0", "127.0.0.1"):
                return self.hostname
        except OSError:
            pass
        return Config._detect_local_ip()

    @property
    def ipv4_bin(self) -> bytes:
        """获取 IPv4 地址的二进制格式（4 字节）"""
        return socket.inet_pton(socket.AF_INET, self.ipv4)

    @property
    def is_playing(self) -> bool:
        """是否正在播放"""
        return self._is_playing

    @property
    def client_name(self) -> str:
        """获取当前连接的客户端名称"""
        return self._client_name

    async def start(self):
        """启动 AirPlay 服务"""
        # 保存事件循环引用，供 RTSP 线程安全回调使用
        self._loop = asyncio.get_running_loop()

        # 启动音频流 HTTP 服务器
        await self._stream_server.start()
        self.stream_port = self._stream_server.port

        # 启动 RTSP 服务器
        self._rtsp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._rtsp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._rtsp_socket.bind(("0.0.0.0", 0))
        self._rtsp_socket.listen(5)
        self.rtsp_port = self._rtsp_socket.getsockname()[1]

        # 启动 mDNS 广播
        self._mdns.update_port(self.rtsp_port)
        self._mdns.start()

        self._running = True
        self._rtsp_thread = threading.Thread(target=self._rtsp_loop, daemon=True)
        self._rtsp_thread.start()

        log.info(f"AirPlay 服务已启动: {self.device_name}")
        log.info(f"  RTSP 端口: {self.rtsp_port}")
        log.info(f"  音频流: {self._stream_server.stream_url}")

    async def stop(self):
        """停止 AirPlay 服务"""
        self._running = False
        if self._rtsp_socket:
            self._rtsp_socket.close()
        self._mdns.stop()
        await self._stream_server.stop()
        log.info("AirPlay 服务已停止")

    def _rtsp_loop(self):
        """RTSP 主循环"""
        while self._running:
            try:
                client_sock, client_addr = self._rtsp_socket.accept()
                handler = threading.Thread(
                    target=self._handle_rtsp_client,
                    args=(client_sock, client_addr),
                    daemon=True,
                )
                handler.start()
            except OSError:
                break
            except Exception as e:
                log.error(f"RTSP accept error: {e}")

    def _safe_call_on_play_stop(self):
        """线程安全地调用 on_play_stop 回调
        
        从同步 RTSP 线程中安全地触发可能涉及异步操作的回调。
        使用 start() 时保存的事件循环引用，避免 Python 3.12 中
        asyncio.get_event_loop() 在非主线程不可靠的问题。
        """
        if not self.on_play_stop:
            return
        try:
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self.on_play_stop)
            else:
                self.on_play_stop()
        except Exception as e:
            log.error(f"on_play_stop error: {e}")

    def _handle_rtsp_client(self, sock: socket.socket, addr: tuple):
        """处理 RTSP 客户端连接"""
        log.info(f"AirPlay 客户端连接: {addr}")
        self._rtsp_client_addr = addr  # 存储客户端地址供 SETUP 解析端口使用
        session_active = False
        rtp_socket = None
        rtp_thread = None
        control_socket = None
        timing_socket = None
        teardown_done = False  # 避免 TEARDOWN 和 finally 双重触发回调

        # 设置客户端 socket 超时，防止无限阻塞导致线程卡死
        sock.settimeout(30.0)
        # 关闭 Nagle 算法，RTSP 响应立即发送
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        try:
            while self._running:
                # 读取 RTSP 请求头
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = sock.recv(4096)
                    if not chunk:
                        log.info(f"AirPlay 客户端关闭连接: {addr}")
                        return
                    data += chunk

                header_end = data.find(b"\r\n\r\n")
                header_lines = data[:header_end].decode("utf-8", errors="replace").split("\r\n")
                body = data[header_end + 4:]

                if not header_lines:
                    log.warning(f"RTSP 空请求头")
                    continue

                request_line = header_lines[0]
                parts = request_line.split()
                if len(parts) < 3:
                    log.warning(f"RTSP 无效请求行: {request_line}")
                    continue

                method = parts[0]
                path = parts[1]
                protocol = parts[2]

                headers = {}
                for line in header_lines[1:]:
                    if ":" in line:
                        key, value = line.split(":", 1)
                        headers[key.strip()] = value.strip()

                # 记录客户端名称 (通常在 X-Apple-Device-Name 或 User-Agent)
                if "X-Apple-Device-Name" in headers:
                    self._client_name = headers["X-Apple-Device-Name"]
                elif "User-Agent" in headers and not self._client_name:
                    ua = headers["User-Agent"]
                    if "/" in ua:
                        self._client_name = ua.split("/")[0]

                # 如果有 Content-Length，继续读取请求体
                content_length = int(headers.get("Content-Length", 0))
                if content_length > 0:
                    while len(body) < content_length:
                        chunk = sock.recv(4096)
                        if not chunk:
                            log.info(f"AirPlay 客户端关闭连接: {addr}")
                            return
                        body += chunk
                    body = body[:content_length]

                cseq = headers.get("CSeq", "0")

                if method == "OPTIONS":
                    response_headers = {
                        "Public": "ANNOUNCE, SETUP, RECORD, PAUSE, FLUSH, FLUSHBUFFERED, TEARDOWN, OPTIONS, POST, GET, PUT, SETPEERSX, SETMAGICCOOKIE, GET_PARAMETER, SET_PARAMETER",
                        "Apple-Jack-Status": "connected; type=analog",
                    }
                    # AirPlay 1 认证: 响应 Apple-Challenge
                    apple_challenge = headers.get("Apple-Challenge")
                    if apple_challenge:
                        apple_response = AP1Security.compute_apple_response(
                            apple_challenge,
                            self.ipv4_bin,
                            self.device_id_bin,
                        )
                        response_headers["Apple-Response"] = apple_response
                    self._send_rtsp_response(sock, 200, cseq, response_headers)

                elif method == "ANNOUNCE":
                    self._is_playing = True
                    self._handle_announce(sock, headers, body, cseq)

                elif method == "SETUP":
                    session_active, rtp_socket, control_socket, timing_socket = self._handle_setup(sock, headers, cseq)

                elif method == "RECORD":
                    self._handle_record(sock, cseq)
                    # 启动 RTP 接收线程
                    if rtp_socket and not rtp_thread:
                        rtp_thread = threading.Thread(
                            target=self._rtp_receive_loop,
                            args=(rtp_socket,),
                            daemon=True,
                        )
                        rtp_thread.start()

                elif method == "PAUSE":
                    self._stream_server.stop_streaming()
                    self._send_rtsp_response(sock, 200, cseq)

                elif method == "TEARDOWN":
                    self._is_playing = False
                    self._client_name = ""
                    self._stream_server.stop_streaming()
                    teardown_done = True
                    self._safe_call_on_play_stop()
                    self._send_rtsp_response(sock, 200, cseq)
                    break

                elif method == "FLUSH":
                    # FLUSH 中的 RTP-Info 头告知接收端：从此 seq/rtptime 开始新的播放
                    rtp_info = headers.get("RTP-Info", "")
                    flush_seq = 0
                    flush_rtptime = 0
                    if rtp_info:
                        for part in rtp_info.split(";"):
                            part = part.strip()
                            if part.startswith("seq="):
                                try: flush_seq = int(part[4:])
                                except ValueError: pass
                            elif part.startswith("rtptime="):
                                try: flush_rtptime = int(part[8:])
                                except ValueError: pass
                    log.info(f"RTSP FLUSH: seq={flush_seq} rtptime={flush_rtptime}")
                    # 清空音频缓冲区但不停止流服务器
                    self._stream_server.start_streaming()
                    # 通知 RTP 接收线程重置 jitter buffer
                    self._flush_flag.set()
                    # 响应中返回当前接收端的 RTP 状态
                    self._send_rtsp_response(sock, 200, cseq, {
                        "RTP-Info": f"seq={self._last_rtp_seq};rtptime={self._last_rtp_timestamp}",
                    })

                elif method == "FLUSHBUFFERED":
                    # FLUSHBUFFERED 用于 buffered 模式，包含 from_seq/until_seq 范围
                    rtp_info = headers.get("RTP-Info", "")
                    log.info(f"RTSP FLUSHBUFFERED: RTP-Info={rtp_info}")
                    self._stream_server.start_streaming()
                    self._flush_flag.set()
                    self._send_rtsp_response(sock, 200, cseq, {
                        "RTP-Info": f"seq={self._last_rtp_seq};rtptime={self._last_rtp_timestamp}",
                    })

                elif method == "GET_PARAMETER":
                    vol_body = f"volume: {self._last_volume_db:.2f}\r\n".encode()
                    self._send_rtsp_response(sock, 200, cseq, {
                        "Content-Type": "text/parameters",
                        "Content-Length": str(len(vol_body)),
                    })
                    sock.sendall(vol_body)

                elif method == "SET_VOLUME_NOTIFICATION":
                    self._send_rtsp_response(sock, 200, cseq)

                elif method == "SET_PARAMETER":
                    content_type = headers.get("Content-Type", "")
                    if not content_type.startswith("image/"):
                        body_str = body.decode("utf-8", errors="replace")
                        
                        # 解析音量: volume: -15.00
                        if "volume:" in body_str:
                            try:
                                vol_str = body_str.split("volume:")[1].strip().split("\r\n")[0]
                                vol_db = float(vol_str)
                                self._last_volume_db = vol_db
                                log.info(f"AirPlay 调节音量: {vol_db} dB")
                                if self.on_volume_change:
                                    self.on_volume_change(vol_db)
                            except Exception as e:
                                log.error(f"解析音量失败: {e}")
                    else:
                        pass
                    self._send_rtsp_response(sock, 200, cseq)

                elif method == "POST" and path == "/fp-setup":
                    self._handle_fp_setup(sock, body, cseq)

                elif method == "POST":
                    log.info(f"未处理的 POST 路径: {path}")
                    self._send_rtsp_response(sock, 200, cseq)

                else:
                    log.info(f"未处理的 RTSP 方法: {method} {path}")
                    self._send_rtsp_response(sock, 200, cseq)

        except socket.timeout:
            log.warning(f"RTSP 客户端超时: {addr}")
        except Exception as e:
            log.error(f"RTSP handler error: {e}")
        finally:
            # 无论正常 TEARDOWN 还是异常断开，都要重置播放状态
            self._is_playing = False
            self._client_name = ""
            # 关闭所有 socket（RTP、RTCP control、timing）
            for s in (rtp_socket, control_socket, timing_socket):
                if s:
                    try:
                        s.close()
                    except Exception:
                        pass
            sock.close()
            log.info(f"AirPlay 客户端断开: {addr}")
            # 异常断开时触发 on_play_stop 回调（TEARDOWN 已触发过则跳过）
            if not teardown_done:
                self._safe_call_on_play_stop()

    def _handle_fp_setup(self, sock: socket.socket, body: bytes, cseq: str):
        """处理 FairPlay 认证 (POST /fp-setup)

        iOS 会发送两轮 fp-setup 请求:
        - 第一轮 (seq=1): 16 字节请求，返回 142 字节响应
        - 第二轮 (seq=3): 164 字节请求，返回 32 字节响应
        """
        log.info(f"FairPlay setup: 收到 {len(body)} 字节")

        if len(body) < 16:
            log.warning(f"FairPlay 请求太短: {len(body)} 字节")
            self._send_rtsp_response(sock, 400, cseq)
            return
        if len(body) == 164:
            self._fp_keymsg = body
            log.info("保存 FairPlay keymsg (164 字节)")

        try:
            response = self._playfair.fairplay_setup(self._fp_state, body)
            if response:
                log.info(f"FairPlay setup 响应: {len(response)} 字节")
                # 发送带二进制内容的 RTSP 响应
                self._send_rtsp_binary_response(sock, 200, cseq, response,
                                                 "application/octet-stream")
            else:
                log.warning("FairPlay setup 未能生成响应")
                self._send_rtsp_response(sock, 200, cseq)
        except Exception as e:
            log.error(f"FairPlay setup 错误: {e}")
            import traceback
            log.error(traceback.format_exc())
            self._send_rtsp_response(sock, 500, cseq)

    def _send_rtsp_binary_response(self, sock: socket.socket, status: int,
                                    cseq: str, body: bytes,
                                    content_type: str = "application/octet-stream"):
        """发送包含二进制内容体的 RTSP 响应"""
        status_text = {200: "OK", 400: "Bad Request", 500: "Internal Server Error"}.get(status, "OK")
        response = f"RTSP/1.0 {status} {status_text}\r\n"
        response += f"CSeq: {cseq}\r\n"
        response += f"Server: AirTunes/105.1\r\n"
        response += f"Content-Type: {content_type}\r\n"
        response += f"Content-Length: {len(body)}\r\n"
        response += "\r\n"
        sock.sendall(response.encode("utf-8") + body)

    def _handle_announce(self, sock: socket.socket, headers: dict, body: bytes, cseq: str):
        """处理 ANNOUNCE 请求 - 解析 SDP"""
        sdp = body.decode("utf-8", errors="replace")

        # 解析 SDP 提取音频参数
        self._sample_rate = 44100
        self._channels = 2
        self._audio_format = 0
        aes_key = None
        aes_iv = None
        aes_key_type = None

        for line in sdp.split("\n"):
            line = line.strip()
            if not line:
                continue
            
            # i= 字段通常包含设备名称 (例如: i=Kiri的iPhone)
            if line.startswith("i="):
                name = line[2:].strip()
                if name:
                    self._client_name = name
                    log.info(f"从 SDP 中识别到客户端名称: {self._client_name}")

            if line.startswith("a=rtpmap:"):
                parts = line.split()
                if len(parts) >= 2:
                    fmt = parts[1]
                    if "AppleLossless" in fmt:
                        self._audio_format = 0x2
                    elif "mpeg4-generic" in fmt:
                        self._audio_format = 0x4
                    elif "L16" in fmt or "PCM" in fmt:
                        self._audio_format = 0x1
            elif line.startswith("a=fmtp:"):
                parts = line.split()
                self._fmtp_params = parts[1:]  # 保存完整 fmtp 参数（去掉 payload type）
                if len(parts) >= 12:
                    try:
                        self._sample_rate = int(parts[11])
                        self._channels = int(parts[7])
                    except (ValueError, IndexError):
                        pass
            elif line.startswith("a=rsaaeskey:"):
                key_data = line.split(":", 1)[1].strip()
                # base64 可能缺少 padding
                key_data += "=" * (4 - len(key_data) % 4) if len(key_data) % 4 else ""
                aes_key = base64.b64decode(key_data)
                aes_key_type = "rsa"
            elif line.startswith("a=fpaeskey:"):
                key_data = line.split(":", 1)[1].strip()
                key_data += "=" * (4 - len(key_data) % 4) if len(key_data) % 4 else ""
                aes_key = base64.b64decode(key_data)
                aes_key_type = "fairplay"
            elif line.startswith("a=aesiv:"):
                iv_data = line.split(":", 1)[1].strip()
                iv_data += "=" * (4 - len(iv_data) % 4) if len(iv_data) % 4 else ""
                aes_iv = base64.b64decode(iv_data)

        if aes_key and aes_iv:
            if aes_key_type == "rsa":
                # 解密 RSA AES 密钥
                self._session_key = self._decrypt_rsa_aes_key(aes_key)
                log.info(f"音频加密已启用 (RSA)")
            else:
                # 解密 FairPlay AES 密钥
                try:
                    from miair.airplay.playfair import FairPlayAES
                    fp_aes = FairPlayAES(fpaeskey=aes_key, aesiv=aes_iv, keymsg=self._fp_keymsg)
                    self._session_key = fp_aes.aeskey
                    log.info(f"音频加密已启用 (FairPlay)")
                except ImportError as e:
                    log.error(f"无法加载 FairPlay 解密模块 (ap2): {e}")
                    # 如果缺少 ap2，尝试使用 fp_decrypt 中的逻辑或其他 fallback
                    self._session_key = None
            
            self._session_iv = aes_iv
            self._session_iv16 = aes_iv[:16] if aes_iv else None
        else:
            self._session_key = None
            self._session_iv = None
            self._session_iv16 = None
            log.info(f"音频未加密")

        # 初始化音频解码器
        self._init_decoder()

        self._send_rtsp_response(sock, 200, cseq)

    def _decrypt_rsa_aes_key(self, encrypted_key: bytes) -> bytes:
        """使用 AirPort 私钥解密 AES 密钥"""
        from Crypto.PublicKey import RSA
        from Crypto.Cipher import PKCS1_v1_5

        key = RSA.import_key(AIRPORT_PRIVATE_KEY)
        cipher = PKCS1_v1_5.new(key)
        decrypted = cipher.decrypt(encrypted_key, None)
        return decrypted[:16] if decrypted else b"\x00" * 16

    def _init_decoder(self):
        """初始化音频解码器"""
        try:
            # 从 fmtp 参数中提取 bitdepth，默认 16
            bitdepth = 16
            p = self._fmtp_params
            if len(p) >= 3:
                try:
                    bitdepth = int(p[2])
                except (ValueError, IndexError):
                    pass

            if self._audio_format == 0x2:  # ALAC
                codec = av.codec.Codec("alac", "r")
                self._codec_context = av.codec.CodecContext.create(codec)
                self._codec_context.sample_rate = self._sample_rate
                self._codec_context.layout = "stereo" if self._channels >= 2 else "mono"
                
                # ALAC 解码器需要设置正确的采样格式
                if bitdepth == 24:
                    self._codec_context.format = av.AudioFormat("s32p")
                else:
                    self._codec_context.format = av.AudioFormat("s16p")
                
                # ALAC extradata ("magic cookie") - 36 bytes
                if len(p) >= 11:
                    try:
                        spf = int(p[0])
                        # 格式: size(4) + 'alac'(4) + version(4) + ALACSpecificConfig
                        extradata = struct.pack(
                            ">I4sIIBBBBBBHIII",
                            36, b"alac", 0,
                            spf,            # frameLength
                            int(p[1]),      # compatibleVersion
                            bitdepth,       # bitDepth
                            int(p[3]),      # historyMult
                            int(p[4]),      # initialHistory
                            int(p[5]),      # riceLimit
                            int(p[6]),      # numChannels
                            int(p[7]),      # maxRunLength
                            int(p[8]),      # maxFrameBytes
                            int(p[9]),      # avgBitRate
                            int(p[10]),     # sampleRate
                        )
                        self._codec_context.extradata = extradata
                    except (ValueError, IndexError, struct.error) as e:
                        log.warning(f"ALAC extradata 构建失败: {e}")
                        extradata = struct.pack(
                            ">I4sIIBBBBBBHIII",
                            36, b"alac", 0,
                            352, 0, 16, 40, 10, 14, 2, 255, 0, 0, 44100
                        )
                        self._codec_context.extradata = extradata
                else:
                    # 默认配置
                    extradata = struct.pack(
                        ">I4sIIBBBBBBHIII",
                        36, b"alac", 0,
                        352, 0, bitdepth, 40, 10, 14, self._channels, 255, 0, 0, self._sample_rate
                    )
                    self._codec_context.extradata = extradata
                
                # 打开解码器
                self._codec_context.open()
            elif self._audio_format == 0x4:  # AAC
                codec = av.codec.Codec("aac", "r")
                self._codec_context = av.codec.CodecContext.create(codec)
                self._codec_context.sample_rate = self._sample_rate
                self._codec_context.layout = "stereo" if self._channels >= 2 else "mono"
                self._codec_context.open()
            elif self._audio_format == 0x1:  # PCM
                self._codec_context = None

            # 重采样仅做格式转换 (planar→packed s16le)，不改变采样率
            # 之前 44100→48000 是冗余的，MP3 模式下 ffmpeg 还会再转回 44100
            self._resampler = av.AudioResampler(
                format=av.AudioFormat("s16").packed,
                layout="stereo" if self._channels >= 2 else "mono",
                rate=self._sample_rate,  # 保持原始采样率，避免冗余重采样
            )

            self._stream_server.set_audio_params(self._sample_rate, self._channels, 2)
            # 预计算单帧静音数据 (352 samples * ch * 2 bytes)，避免循环中反复分配
            self._silence_frame = b'\x00' * (self._sample_rate * self._channels * 2 * 352 // self._sample_rate)
            log.info(f"音频解码器初始化: fmt={self._audio_format}, sr={self._sample_rate}, ch={self._channels}, bits={bitdepth}")
        except Exception as e:
            log.error(f"解码器初始化失败: {e}")
            import traceback
            log.error(traceback.format_exc())

    def _handle_setup(self, sock: socket.socket, headers: dict, cseq: str) -> tuple:
        """处理 SETUP 请求

        客户端在 Transport 头里给出它的 control_port（用于发送重传请求）。
        服务端创建三个 UDP socket:
        - server_port: 接收 RTP 音频数据
        - control_port: 接收/发送 RTCP 控制包
        - timing_port: 接收/发送 NTP timing 包
        """
        transport = headers.get("Transport", "")

        # 解析 iPhone 的 control_port（用于发送重传请求）
        client_control_port = 0
        for part in transport.split(";"):
            if "control_port" in part:
                try:
                    client_control_port = int(part.split("=", 1)[1].strip())
                except (ValueError, IndexError):
                    pass

        # 创建 RTP 接收 socket (server_port - 音频数据)
        rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # 增大内核 UDP 接收缓冲区到 1MB，防止高频小包场景下内核丢包
        rtp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1048576)
        rtp_socket.settimeout(1.0)
        rtp_socket.bind(("0.0.0.0", 0))
        server_port = rtp_socket.getsockname()[1]

        # 创建 RTCP 控制 socket (control_port)
        control_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        control_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        control_socket.settimeout(2.0)
        control_socket.bind(("0.0.0.0", 0))
        control_port = control_socket.getsockname()[1]

        self._rtcp_control_socket = control_socket
        self._rtp_data_socket = rtp_socket
        if client_control_port > 0 and self._rtsp_client_addr:
            self._rtcp_control_addr = (self._rtsp_client_addr[0], client_control_port)

        # timing socket：只用来回复 iPhone 的 NTP timing 请求
        timing_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        timing_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        timing_socket.settimeout(1.0)
        timing_socket.bind(("0.0.0.0", 0))
        timing_port = timing_socket.getsockname()[1]

        # timing 应答线程 + RTCP 控制线程
        threading.Thread(target=self._timing_loop, args=(timing_socket,), daemon=True).start()
        threading.Thread(target=self._rtcp_loop, args=(control_socket,), daemon=True).start()

        # 构建 Transport 响应 - RAOP 格式
        # server_port = RTP 音频端口, control_port = RTCP 端口, timing_port = NTP 端口
        transport_response = (
            f"RTP/AVP/UDP;unicast;mode=record;"
            f"server_port={server_port};"
            f"control_port={control_port};"
            f"timing_port={timing_port}"
        )

        log.info(f"SETUP 响应: server_port={server_port}, control_port={control_port}, timing_port={timing_port}")

        self._send_rtsp_response(sock, 200, cseq, {
            "Transport": transport_response,
            "Session": "1",
            "Audio-Jack-Status": "connected; type=analog",
        })

        return True, rtp_socket, control_socket, timing_socket

    def _request_retransmit(self, start_seq: int, count: int):
        """向 iPhone 发送 RTCP REXMIT_REQUEST 请求重传丢失的 RTP 包

        RTCP type 0xd5 (213) 格式:
          byte 0:   0x80 (version=2)
          byte 1:   0xd5 (type)
          byte 2-3: length in 32-bit words
          byte 4-5: start sequence number
          byte 6-7: amount of following missing packets
        """
        sock = self._rtcp_control_socket
        addr = self._rtcp_control_addr
        if not sock or not addr:
            return
        try:
            req = bytearray(8)
            req[0] = 0x80
            req[1] = 0xd5  # REXMIT_REQUEST
            req[2:4] = (2).to_bytes(2, 'big')  # length = 8 bytes / 4 = 2 words
            req[4:6] = start_seq.to_bytes(2, 'big')
            req[6:8] = count.to_bytes(2, 'big')
            sock.sendto(bytes(req), addr)
        except Exception as e:
            log.debug(f"RTCP 重传请求失败: {e}")

    def _rtcp_loop(self, rtcp_socket: socket.socket) -> None:
        """接收 RTCP 控制包

        - 记录 iPhone 的控制端口（发送重传请求用）
        - REXMIT_RESPONSE (0xd6)：把 iPhone 补发的 RTP 包回灌给接收线程
        """
        log.info("RTCP 线程启动")
        try:
            while self._running:
                try:
                    data, addr = rtcp_socket.recvfrom(1500)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if len(data) < 4:
                    continue
                if self._rtcp_control_addr is None:
                    self._rtcp_control_addr = addr
                if data[1] != 214 or not self._rtp_data_socket:
                    continue
                packet = data[4:]
                if len(packet) < _RTP_HEADER_LEN:
                    continue
                try:
                    port = self._rtp_data_socket.getsockname()[1]
                    self._rtp_data_socket.sendto(packet, ("127.0.0.1", port))
                except OSError as e:
                    log.debug(f"重传包注入失败: {e}")
        finally:
            rtcp_socket.close()
            log.info("RTCP 线程已停止")

    def _handle_record(self, sock: socket.socket, cseq: str):
        """处理 RECORD 请求 - 开始播放"""
        self._stream_server.start_streaming()

        if self.on_play_start:
            try:
                self.on_play_start(self._stream_server.stream_url)
            except Exception as e:
                log.error(f"on_play_start error: {e}")

        self._send_rtsp_response(sock, 200, cseq, {
            "Audio-Latency": "0",
            "RTP-Info": f"seq={self._last_rtp_seq};rtptime={self._last_rtp_timestamp}",
        })

    def _timing_loop(self, timing_socket: socket.socket) -> None:
        """回复 iPhone 的 NTP timing 请求（0x52 → 0x53）

        RAOP 只要求接收端应答 timing。本机不做时钟对齐：音频按到达顺序解码
        输出，由音箱按实时速率拉流。
        """
        try:
            while self._running:
                try:
                    data, addr = timing_socket.recvfrom(256)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if len(data) < 32 or (data[1] & 0x7F) != 0x52:
                    continue

                ntp_now = time.time() + NTP_EPOCH_OFFSET
                ntp_sec = int(ntp_now)
                ntp_frac = int((ntp_now - ntp_sec) * (2 ** 32))

                reply = bytearray(32)
                reply[0] = 0x80
                reply[1] = 0xD3             # timing response
                reply[2:4] = data[2:4]      # 回显 sequence
                reply[4:12] = data[24:32]   # 回显请求方的发送时间
                reply[12:16] = ntp_sec.to_bytes(4, "big")   # 收到时间
                reply[16:20] = ntp_frac.to_bytes(4, "big")
                reply[20:24] = ntp_sec.to_bytes(4, "big")   # 回发时间
                reply[24:28] = ntp_frac.to_bytes(4, "big")
                try:
                    timing_socket.sendto(bytes(reply), addr)
                except OSError:
                    pass
        except Exception as e:
            log.debug(f"timing 线程异常: {e}")
        finally:
            timing_socket.close()

    def _reset_rtp_stats(self) -> None:
        self._rtp_stats = dict.fromkeys(
            ("lost", "retransmit", "decrypt_failed", "decode_failed", "queue_full"), 0
        )

    def _rtp_receive_loop(self, rtp_socket: socket.socket) -> None:
        """RTP 音频接收：收包/重排/补包 与 解密/解码 分两个线程

        接收线程只做 UDP 读取和顺序整理，永不等待解码；解码线程按顺序把 PCM
        写入 HTTP 音频流。整条链路不做墙上时钟调度 —— 音箱自己按实时速率拉流，
        这里只需要保证「不断流、不丢帧」。
        """
        log.info("RTP 接收线程启动")
        self._reset_rtp_stats()

        # 等 RECORD：流媒体激活后才开始收包
        for _ in range(50):
            if not self._running or self._stream_server._active:
                break
            time.sleep(0.1)
        if not self._stream_server._active:
            log.warning("RTP: 流媒体未激活，退出接收线程")
            rtp_socket.close()
            return

        log.info("RTP: 开始接收音频数据")

        session_key = self._session_key
        session_iv = self._session_iv16
        write_pcm = self._stream_server.write_pcm
        silence = self._silence_frame
        decode_audio = self._decode_audio
        stats = self._rtp_stats
        recv_buf = bytearray(2048)
        decode_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=_DECODE_QUEUE_MAX)
        running = True

        def _decoder() -> None:
            """解码线程：空 payload = 补一帧静音，None = 结束"""
            frames = 0
            try:
                while running:
                    try:
                        payload = decode_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if payload is None:
                        break
                    if not payload:
                        write_pcm(silence)
                        continue
                    if session_key and session_iv:
                        try:
                            payload = decrypt_packet(session_key, session_iv, payload)
                        except Exception:
                            stats["decrypt_failed"] += 1
                            write_pcm(silence)
                            continue
                    pcm = decode_audio(payload)
                    if pcm is None:
                        stats["decode_failed"] += 1
                        pcm = silence
                    write_pcm(pcm)

                    frames += 1
                    if frames >= _STATS_EVERY_FRAMES:
                        frames = 0
                        self._log_audio_stats(decode_queue)
            except Exception as e:
                log.error(f"RTP 解码线程异常: {e}")

        threading.Thread(target=_decoder, daemon=True).start()

        try:
            jb = JitterBuffer()
            pending: dict[int, Retransmit] = {}
            next_seq = -1
            prebuffered = False

            while self._running:
                try:
                    nbytes, _addr = rtp_socket.recvfrom_into(recv_buf)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if nbytes <= _RTP_HEADER_LEN:   # 只有头没有负载的包直接丢掉
                    continue

                seq = (recv_buf[2] << 8) | recv_buf[3]
                self._last_rtp_seq = seq
                self._last_rtp_timestamp = int.from_bytes(recv_buf[4:8], "big")
                if next_seq < 0:
                    next_seq = seq
                jb.insert(seq, bytes(recv_buf[_RTP_HEADER_LEN:nbytes]))

                if not prebuffered:
                    if len(jb) < _PREBUFFER_PACKETS:
                        continue
                    prebuffered = True

                if self._flush_flag.is_set():
                    # FLUSH：丢掉旧数据，从当前包重新开始
                    self._flush_flag.clear()
                    jb.clear()
                    pending.clear()
                    next_seq = seq
                    continue

                start = next_seq
                drained = jb.drain(start)
                if drained:
                    next_seq = (start + len(drained)) & 0xFFFF
                    for offset, payload in enumerate(drained):
                        pending.pop((start + offset) & 0xFFFF, None)
                        try:
                            decode_queue.put_nowait(payload)
                        except queue.Full:
                            stats["queue_full"] += 1

                next_seq = self._recover_gaps(jb, next_seq, pending, decode_queue)
        except Exception as e:
            log.error(f"RTP 接收错误: {e}")
        finally:
            running = False
            try:
                decode_queue.put_nowait(None)
            except queue.Full:
                pass
            rtp_socket.close()
            log.info("RTP 接收线程结束")

    def _recover_gaps(self, jb: JitterBuffer, next_seq: int,
                      pending: dict[int, Retransmit], decode_queue) -> int:
        """为缺失的包请求重传；重传窗口过后补静音并跳过（返回新的 next_seq）"""
        if len(jb) <= _GAP_DETECT_MIN:
            return next_seq

        stats = self._rtp_stats
        now = time.perf_counter()
        for seq in jb.gap_missing(next_seq):
            state = pending.get(seq)
            if state is None:
                pending[seq] = Retransmit(now, now)
                self._request_retransmit(seq, 1)
                stats["retransmit"] += 1
                continue

            if now - state.first_seen > _RETRANSMIT_GIVE_UP:
                # 补不回来了：用静音占位跳过空洞，否则后续音频会被整体堵住
                pending.pop(seq, None)
                following = jb.next_available_after(seq)
                if following is None:
                    continue
                missing = min((following - seq) & 0xFFFF, _MAX_GAP_FILL)
                for _ in range(missing):
                    try:
                        decode_queue.put_nowait(b"")
                    except queue.Full:
                        stats["queue_full"] += 1
                        break
                stats["lost"] += missing
                for skipped in range(seq, following):
                    pending.pop(skipped & 0xFFFF, None)
                return following

            interval = min(_RETRANSMIT_INTERVAL * (1 << state.attempts),
                           _RETRANSMIT_MAX_INTERVAL)
            if now - state.last_request >= interval:
                self._request_retransmit(seq, 1)
                state.last_request = now
                state.attempts += 1
                stats["retransmit"] += 1
        return next_seq

    def _log_audio_stats(self, decode_queue) -> None:
        s = self._rtp_stats
        log.info(
            f"[音频统计] {self._stream_server.stats_line()} "
            f"解码队列={decode_queue.qsize()} 丢包={s['lost']} 重传={s['retransmit']} "
            f"解密失败={s['decrypt_failed']} 解码失败={s['decode_failed']} "
            f"队列溢出={s['queue_full']}"
        )

    def _decode_audio(self, data: bytes) -> bytes | None:
        """ALAC/AAC → 16bit 立体声 PCM；解码失败返回 None"""
        if not self._codec_context:
            return data
        try:
            frames = self._codec_context.decode(av.packet.Packet(data))
        except Exception:
            return None
        if not frames:
            return None
        frame_bytes = self._channels * 2
        parts = [
            bytes(memoryview(frame.planes[0])[: frame.samples * frame_bytes])
            for chunk in frames
            for frame in _as_frames(self._resampler.resample(chunk))
        ]
        return b"".join(parts) if parts else None

    def _send_rtsp_response(self, sock: socket.socket, code: int, cseq: str, headers: dict | None = None):
        """发送 RTSP 响应"""
        messages = {
            200: "OK",
            400: "Bad Request",
            401: "Unauthorized",
            404: "Not Found",
            500: "Internal Server Error",
        }
        msg = messages.get(code, "Unknown")

        response = f"RTSP/1.0 {code} {msg}\r\n"
        response += f"CSeq: {cseq}\r\n"
        # AirPlay 1 使用 AirTunes/105.1，AirPlay 2 使用 366.0
        response += f"Server: AirTunes/105.1\r\n"

        if headers:
            for key, value in headers.items():
                response += f"{key}: {value}\r\n"

        response += "\r\n"
        sock.sendall(response.encode())
