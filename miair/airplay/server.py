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
import subprocess
import sys
import threading
import time
import uuid
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

@dataclass
class PacketData:
    """解码队列中的音频包"""
    seq: int          # 序列号；-1 = 静音帧
    timestamp: int    # RTP 时间戳 (32-bit)；0 = 无时间戳
    payload: bytes    # 加密音频数据或静音帧


class JitterBuffer:
    """RTP 包环形缓冲区

    512 条目 (~4.1 秒 @ 44100Hz/352spf)，自动淘汰最旧条目。
    替代 dict-based jitter_buffer，提供更大的抖动容忍窗口。
    """

    BUFFER_SIZE = 512

    def __init__(self, max_size: int = BUFFER_SIZE):
        self._max_size = max_size
        self._packets: dict[int, tuple[int, bytes]] = {}  # seq -> (timestamp, payload)
        self._order: deque[int] = deque()  # 插入顺序，用于淘汰

    def insert(self, seq: int, rtp_timestamp: int, payload: bytes) -> None:
        """插入包。满时淘汰最旧条目。重复 seq 覆盖。"""
        if seq in self._packets:
            self._packets[seq] = (rtp_timestamp, payload)
            return
        while len(self._packets) >= self._max_size:
            old_seq = self._order.popleft()
            self._packets.pop(old_seq, None)
        self._packets[seq] = (rtp_timestamp, payload)
        self._order.append(seq)

    def has(self, seq: int) -> bool:
        return seq in self._packets

    def pop(self, seq: int) -> tuple[int, bytes] | None:
        """取出并删除指定 seq 的包。"""
        pkt = self._packets.pop(seq, None)
        return pkt

    def drain(self, start_seq: int) -> list[tuple[int, int, bytes]]:
        """从 start_seq 开始按序取出连续的包，遇到缺口停止。
        返回 [(seq, timestamp, payload), ...]"""
        result = []
        seq = start_seq
        while seq in self._packets:
            ts, payload = self._packets.pop(seq)
            result.append((seq, ts, payload))
            seq = (seq + 1) & 0xFFFF
        return result

    def gap_missing(self, next_seq: int) -> list[int]:
        """返回 next_seq 到下一个可用包之间的缺失 seq 列表。最多扫描 32 个位置。"""
        missing = []
        seq = next_seq
        for _ in range(32):
            if seq in self._packets:
                break
            missing.append(seq)
            seq = (seq + 1) & 0xFFFF
        else:
            # 扫描了 32 个位置都没找到，只返回前几个
            missing = missing[:8]
        return missing

    def next_available_after(self, seq: int) -> int | None:
        """找 seq 之后最近的可用 seq。"""
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

    def __contains__(self, seq: int) -> bool:
        return seq in self._packets


class PlaybackPacer:
    """帧释放调度器 — 将 RTP 时间戳映射到本地时钟，控制解码线程的帧释放时机

    时间锚点由 D4 sync 包 (RTCP TIME_ANNOUNCE) 提供：
      playAtRtpTimestamp 对应 NTP 时间 → 映射到本地 perf_counter。
    无锚点时退化为启动缓冲模式（累积 32 帧后立即释放）。
    """

    def __init__(self, sample_rate: int = 44100):
        self._sample_rate = sample_rate
        # 锚点: RTP 时间戳 → 本地 perf_counter 时间
        self._anchor_rtp_ts: int | None = None
        self._anchor_perf: float = 0.0
        self._lock = threading.Lock()
        # 漂移校正: 实际/期望时间比的 EMA
        self._drift_rate: float = 1.0
        # 目标延迟: 帧释放提前量，补偿 HTTP 缓冲 + 网络 + 音箱缓冲
        self._target_latency_sec: float = 0.200
        # 启动缓冲
        self._startup_count: int = 0
        self._startup_target: int = 32  # ~256ms
        self._started: bool = False

    @property
    def has_anchor(self) -> bool:
        return self._anchor_rtp_ts is not None

    def update_anchor(self, sender_rtp_ts: int, ntp_time: float,
                      play_at_rtp_ts: int) -> None:
        """从 D4 sync 包更新时间锚点。

        D4 包含义: sender 在 NTP 时间 ntp_time 时，RTP 时钟为 sender_rtp_ts，
        且 play_at_rtp_ts 对应的音频应该被播放。
        我们用 play_at_rtp_ts 作为锚点，因为它直接告诉我们「这个 RTP 时间戳
        应该在什么时刻播放」。
        """
        now_perf = time.perf_counter()
        with self._lock:
            if self._anchor_rtp_ts is None:
                # 首次同步: 建立锚点
                self._anchor_rtp_ts = play_at_rtp_ts
                self._anchor_perf = now_perf + self._target_latency_sec
                self._started = False
                self._startup_count = 0
            else:
                # 后续同步: 计算漂移率
                audio_elapsed = (play_at_rtp_ts - self._anchor_rtp_ts) / self._sample_rate
                real_elapsed = now_perf - self._anchor_perf
                if audio_elapsed > 0.5:
                    measured_rate = real_elapsed / audio_elapsed
                    # EMA 更新 (alpha=0.05 温和收敛)
                    self._drift_rate += 0.05 * (measured_rate - self._drift_rate)
                    # 定期重锚点防止累积误差
                    self._anchor_rtp_ts = play_at_rtp_ts
                    self._anchor_perf = now_perf + self._target_latency_sec

    def wait_for_frame(self, rtp_timestamp: int) -> bool:
        """解码线程调用。等到帧应该释放的时刻。
        返回 True = 播放，False = 太晚了跳过。
        """
        if rtp_timestamp == 0:
            return True  # 静音帧直接播放

        with self._lock:
            if self._anchor_rtp_ts is None:
                # 无锚点: 启动缓冲模式
                self._startup_count += 1
                if self._startup_count >= self._startup_target:
                    self._started = True
                return True

            # 计算此帧应该释放的时刻
            audio_offset = (rtp_timestamp - self._anchor_rtp_ts) / self._sample_rate
            target_perf = self._anchor_perf + audio_offset * self._drift_rate

        now = time.perf_counter()
        wait_time = target_perf - now

        if wait_time > 0.005:  # 超过 5ms 才 sleep
            time.sleep(wait_time)
            return True
        elif wait_time < -0.100:  # 超过 100ms 过期
            return False  # 跳过
        else:
            return True  # 稍微迟到但可接受

    def reset(self) -> None:
        """FLUSH 时重置。"""
        with self._lock:
            self._anchor_rtp_ts = None
            self._anchor_perf = 0.0
            self._drift_rate = 1.0
            self._startup_count = 0
            self._started = False


class NTPClockSync:
    """NTP 时钟同步 — 跟踪本机与 iPhone 的网络延迟"""

    NTP_EPOCH_OFFSET = 2208988800.0  # 1900-01-01 到 1970-01-01 的秒数

    def __init__(self):
        self._latency_ms: float = 50.0  # 估计的单向延迟 (ms)
        self._rtt_samples: deque[float] = deque(maxlen=20)
        self._lock = threading.Lock()

    @property
    def latency_ms(self) -> float:
        return self._latency_ms

    def update_latency(self, request_sent_mono: float, response_recv_mono: float) -> None:
        """从 timing exchange 的往返时间更新延迟估计。"""
        rtt = response_recv_mono - request_sent_mono
        if rtt <= 0 or rtt > 1.0:  # 丢弃异常值
            return
        with self._lock:
            self._rtt_samples.append(rtt)
            if len(self._rtt_samples) >= 3:
                sorted_rtts = sorted(self._rtt_samples)
                median_rtt = sorted_rtts[len(sorted_rtts) // 2]
                self._latency_ms = (median_rtt / 2.0) * 1000.0


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

        # 时间同步（新增）
        self._timing_pacer: PlaybackPacer | None = None
        self._clock_sync: NTPClockSync | None = None
        self._timing_client_addr: tuple | None = None  # iPhone timing 端口地址
        self._timing_socket: socket.socket | None = None
        self._timing_request_seq: int = 0
        self._rtsp_client_addr: tuple | None = None  # RTSP 客户端 IP

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

        客户端发送的 Transport 头包含客户端的 control_port 和 timing_port。
        服务端需要创建自己的三个 UDP socket:
        - server_port: 接收 RTP 音频数据
        - control_port: 接收/发送 RTCP 控制包
        - timing_port: 接收/发送 NTP timing 包
        """
        transport = headers.get("Transport", "")

        # 解析 iPhone 的 control_port 和 timing_port
        client_timing_port = 0
        client_control_port = 0
        for part in transport.split(";"):
            part = part.strip()
            if "timing_port" in part:
                try: client_timing_port = int(part.split("=")[1])
                except (ValueError, IndexError): pass
            elif "control_port" in part:
                try: client_control_port = int(part.split("=")[1])
                except (ValueError, IndexError): pass

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

        # 存储 socket 引用，供 RTCP 重传使用
        self._rtcp_control_socket = control_socket
        self._rtp_data_socket = rtp_socket

        # 创建 timing socket (NTP 时间同步)
        timing_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        timing_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        timing_socket.settimeout(1.0)  # 短超时，timing_loop 内部管理发送间隔
        timing_socket.bind(("0.0.0.0", 0))
        timing_port = timing_socket.getsockname()[1]

        # 初始化时间同步对象
        self._timing_socket = timing_socket
        self._clock_sync = NTPClockSync()
        self._timing_pacer = PlaybackPacer(self._sample_rate)
        if client_timing_port > 0 and self._rtsp_client_addr:
            self._timing_client_addr = (self._rtsp_client_addr[0], client_timing_port)
        if client_control_port > 0 and self._rtsp_client_addr:
            self._rtcp_control_addr = (self._rtsp_client_addr[0], client_control_port)

        timing_thread = threading.Thread(
            target=self._timing_loop,
            args=(timing_socket,),
            daemon=True,
        )
        timing_thread.start()

        # 启动 RTCP (control) 接收线程
        rtcp_thread = threading.Thread(
            target=self._rtcp_loop,
            args=(control_socket,),
            daemon=True,
        )
        rtcp_thread.start()

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

    def _rtcp_loop(self, rtcp_socket: socket.socket):
        """RTCP 控制包接收循环

        处理:
        - TIME_ANNOUNCE (0xd4/212): NTP 时间同步
        - REXMIT_RESPONSE (0xd6/214): iPhone 返回的重传包，注入 RTP 数据流
        """
        log.info("RTCP 线程启动")
        try:
            while self._running:
                try:
                    data, addr = rtcp_socket.recvfrom(1500)
                    if not data or len(data) < 4:
                        continue

                    # 记录 iPhone 控制端口地址（用于发送重传请求）
                    if not self._rtcp_control_addr:
                        self._rtcp_control_addr = addr

                    ptype = data[1]
                    if ptype == 212:  # TIME_ANNOUNCE_NTP (D4 sync 包)
                        if len(data) >= 20:
                            sender_rtp_ts = int.from_bytes(data[4:8], 'big')
                            ntp_sec = int.from_bytes(data[8:12], 'big')
                            ntp_frac = int.from_bytes(data[12:16], 'big')
                            ntp_time = ntp_sec + (ntp_frac * 2**-32)
                            play_at_rtp_ts = int.from_bytes(data[16:20], 'big')
                            if self._timing_pacer:
                                self._timing_pacer.update_anchor(
                                    sender_rtp_ts, ntp_time, play_at_rtp_ts)
                    elif ptype == 214:  # REXMIT_RESPONSE — iPhone 重传的 RTP 包
                        # data[4:] 是完整的 RTP 包，注入 RTP 数据 socket
                        rtp_data = data[4:]
                        if len(rtp_data) >= 12 and self._rtp_data_socket:
                            try:
                                rtp_port = self._rtp_data_socket.getsockname()[1]
                                self._rtp_data_socket.sendto(rtp_data, ('127.0.0.1', rtp_port))
                            except Exception as e:
                                log.debug(f"RTCP 重传包注入失败: {e}")
                except socket.timeout:
                    continue
                except OSError:
                    break
        except Exception as e:
            pass
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

    def _timing_loop(self, timing_socket: socket.socket):
        """双向 NTP 时间同步循环

        - 响应 iPhone 的 0x52 timing 请求（原有逻辑）
        - 主动向 iPhone 发送 0x52 timing 请求（每 3 秒一次，前 3 次 300ms 间隔）
        - 从 0x53 响应计算 RTT，更新 NTPClockSync 延迟估计
        """
        NTP_EPOCH = NTPClockSync.NTP_EPOCH_OFFSET
        # 发送间隔: 前 3 次 300ms（快速收敛），之后 3 秒
        _fast_pings_left = 3
        _fast_interval = 0.3
        _normal_interval = 3.0
        _last_send_time = 0.0
        _pending_seq: int | None = None
        _send_mono: float = 0.0

        try:
            while self._running:
                # --- 主动发送 timing request ---
                now = time.time()
                client_addr = self._timing_client_addr
                interval = _fast_interval if _fast_pings_left > 0 else _normal_interval
                if client_addr and (now - _last_send_time) >= interval:
                    self._timing_request_seq = (self._timing_request_seq + 1) & 0xFFFF
                    _pending_seq = self._timing_request_seq
                    _send_mono = time.perf_counter()

                    req = bytearray(32)
                    req[0] = 0x80
                    req[1] = 0x52  # TIME_REQUEST
                    req[2:4] = self._timing_request_seq.to_bytes(2, 'big')
                    # bytes 24-31: our send time as NTP timestamp
                    ntp_now = now + NTP_EPOCH
                    ntp_sec = int(ntp_now)
                    ntp_frac = int((ntp_now - ntp_sec) * (2**32))
                    req[24:28] = ntp_sec.to_bytes(4, 'big')
                    req[28:32] = ntp_frac.to_bytes(4, 'big')

                    try:
                        timing_socket.sendto(bytes(req), client_addr)
                        if _fast_pings_left > 0:
                            _fast_pings_left -= 1
                    except Exception:
                        pass
                    _last_send_time = now

                # --- 接收并处理包 ---
                try:
                    data, addr = timing_socket.recvfrom(256)
                except socket.timeout:
                    continue
                if not data or len(data) < 32:
                    continue

                ptype = data[1] & 0x7f

                if ptype == 0x52:
                    # iPhone 发来 timing request → 回复 response
                    recv_now = time.time()
                    ntp_recv = recv_now + NTP_EPOCH
                    ntp_sec = int(ntp_recv)
                    ntp_frac = int((ntp_recv - ntp_sec) * (2**32))

                    response = bytearray(32)
                    response[0] = 0x80
                    response[1] = 0xd3  # timing response (0x53 | marker)
                    response[2:4] = data[2:4]  # 复制 sequence number
                    response[4:12] = data[24:32]  # 复制 reference send time
                    response[12:16] = ntp_sec.to_bytes(4, 'big')
                    response[16:20] = ntp_frac.to_bytes(4, 'big')
                    send_now = time.time() + NTP_EPOCH
                    send_sec = int(send_now)
                    send_frac = int((send_now - send_sec) * (2**32))
                    response[20:24] = send_sec.to_bytes(4, 'big')
                    response[24:28] = send_frac.to_bytes(4, 'big')

                    timing_socket.sendto(bytes(response), addr)

                elif ptype == 0x53:
                    # iPhone 回复 timing response → 计算 RTT
                    resp_seq = (data[2] << 8) | data[3]
                    if _pending_seq is not None and resp_seq == _pending_seq:
                        recv_mono = time.perf_counter()
                        if self._clock_sync:
                            self._clock_sync.update_latency(_send_mono, recv_mono)
                        _pending_seq = None

        except Exception:
            pass
        finally:
            timing_socket.close()

    def _rtp_receive_loop(self, rtp_socket: socket.socket):
        """RTP 音频数据接收循环 — 两阶段管道 + 时间调度

        Stage 1 (receiver): recvfrom → 环形缓冲区 → 按序发送到解码队列
        Stage 2 (decoder):  解码队列 → PlaybackPacer 调度 → 解密 → ALAC 解码 → write_pcm

        接收线程只做 UDP 读取和轻量操作，永远不被解码阻塞。
        """
        log.info("RTP 接收线程启动")

        # 等待流媒体激活
        wait_count = 0
        while self._running and not self._stream_server._active and wait_count < 50:
            time.sleep(0.1)
            wait_count += 1

        if not self._stream_server._active:
            log.warning("RTP: 流媒体未激活，退出接收线程")
            rtp_socket.close()
            return

        log.info("RTP: 开始接收音频数据")

        # 预计算常用值
        _session_key = self._session_key
        _session_iv16 = self._session_iv16
        _write_pcm = self._stream_server.write_pcm
        _decode_audio = self._decode_audio
        _silence_frame = self._silence_frame
        _recv_buf = bytearray(2048)

        # 解码队列
        decode_queue: queue.Queue[PacketData | None] = queue.Queue(maxsize=200)
        running = True

        # ---- Stage 2: 解码线程（带 pacer 调度） ----
        def _decoder_worker():
            _pacer = self._timing_pacer
            try:
                while running:
                    try:
                        item = decode_queue.get(timeout=0.5)
                    except queue.Empty:
                        continue
                    if item is None:
                        break

                    # 时间调度: 等到正确时刻再释放帧
                    if _pacer and item.timestamp > 0:
                        if not _pacer.wait_for_frame(item.timestamp):
                            continue  # 太晚了，跳过

                    # 静音帧直接写入
                    if item.seq == -1:
                        _write_pcm(item.payload)
                        continue

                    # 解密
                    payload = item.payload
                    if _session_key and _session_iv16:
                        try:
                            plen = len(payload)
                            decrypt_len = plen & ~0xF
                            if decrypt_len > 0:
                                cipher = AES.new(_session_key, AES.MODE_CBC, _session_iv16)
                                decrypted = cipher.decrypt(payload[:decrypt_len])
                                if decrypt_len < plen:
                                    decrypted = decrypted + bytes(memoryview(payload)[decrypt_len:])
                                payload = decrypted
                        except Exception:
                            _write_pcm(_silence_frame)
                            continue

                    # 解码并输出
                    decoded = _decode_audio(payload)
                    if decoded:
                        _write_pcm(decoded)
            except Exception as e:
                log.error(f"RTP 解码线程异常: {e}")

        decoder_thread = threading.Thread(target=_decoder_worker, daemon=True)
        decoder_thread.start()

        # ---- Stage 1: 接收 + 环形缓冲 + 指数退避重传 ----
        try:
            jb = JitterBuffer(max_size=512)
            next_seq = -1
            _pacer = self._timing_pacer

            # 启动缓冲
            STARTUP_BUFFER_TARGET = 32  # ~256ms @ 44100Hz/352spf
            startup_buffered = False

            # 重传状态: seq -> (首次请求 perf_counter 时间, 重试次数)
            _retransmit_state: dict[int, tuple[float, int]] = {}
            _RETRANSMIT_BASE_INTERVAL = 0.040  # 40ms
            _RETRANSMIT_MAX_INTERVAL = 1.000
            _RETRANSMIT_GIVE_UP_TIME = 2.0  # 2秒后放弃

            while self._running:
                try:
                    nbytes, addr = rtp_socket.recvfrom_into(_recv_buf)
                    if nbytes < 12:
                        continue

                    seq = (_recv_buf[2] << 8) | _recv_buf[3]
                    rtp_timestamp = (_recv_buf[4] << 24) | (_recv_buf[5] << 16) | \
                                   (_recv_buf[6] << 8) | _recv_buf[7]
                    payload = bytes(_recv_buf[12:nbytes])

                    # 跟踪最新 RTP 状态（供 FLUSH/RECORD 响应使用）
                    self._last_rtp_seq = seq
                    self._last_rtp_timestamp = rtp_timestamp

                    if next_seq == -1:
                        next_seq = seq

                    # 插入环形缓冲区
                    jb.insert(seq, rtp_timestamp, payload)

                    # 启动预缓冲: 等待足够帧数
                    if not startup_buffered:
                        if len(jb) < STARTUP_BUFFER_TARGET:
                            continue
                        # 等待 pacer 锚点（如果还没收到 D4 sync）
                        if _pacer and not _pacer.has_anchor:
                            if len(jb) < STARTUP_BUFFER_TARGET * 2:
                                continue
                        startup_buffered = True

                    # 检查 FLUSH 请求
                    if self._flush_flag.is_set():
                        self._flush_flag.clear()
                        jb.clear()
                        next_seq = seq
                        _retransmit_state.clear()
                        if _pacer:
                            _pacer.reset()
                        continue

                    # 按序 drain 到解码队列
                    drained = jb.drain(next_seq)
                    for d_seq, d_ts, d_payload in drained:
                        try:
                            decode_queue.put_nowait(PacketData(d_seq, d_ts, d_payload))
                        except queue.Full:
                            pass
                    if drained:
                        next_seq = (drained[-1][0] + 1) & 0xFFFF
                        for d_seq, _, _ in drained:
                            _retransmit_state.pop(d_seq, None)

                    # 丢包检测 + 指数退避重传
                    if len(jb) > 8:
                        missing_seqs = jb.gap_missing(next_seq)
                        now_mono = time.perf_counter()

                        for missing_seq in missing_seqs:
                            if missing_seq in _retransmit_state:
                                first_time, retry_count = _retransmit_state[missing_seq]
                                # 放弃条件
                                if now_mono - first_time > _RETRANSMIT_GIVE_UP_TIME:
                                    _retransmit_state.pop(missing_seq, None)
                                    next_avail = jb.next_available_after(missing_seq)
                                    if next_avail is not None:
                                        gap = (next_avail - missing_seq) & 0xFFFF
                                        gap = min(gap, 64)
                                        for _ in range(gap):
                                            try:
                                                decode_queue.put_nowait(
                                                    PacketData(-1, 0, _silence_frame))
                                            except queue.Full:
                                                pass
                                        next_seq = next_avail
                                        # 清理跳过范围的重传状态
                                        for s in range(missing_seq, next_avail):
                                            _retransmit_state.pop(s & 0xFFFF, None)
                                    continue

                                # 指数退避重试
                                backoff = min(
                                    _RETRANSMIT_BASE_INTERVAL * (2 ** retry_count),
                                    _RETRANSMIT_MAX_INTERVAL
                                )
                                if now_mono - first_time >= backoff * (retry_count + 1):
                                    self._request_retransmit(missing_seq, 1)
                                    _retransmit_state[missing_seq] = (first_time, retry_count + 1)
                                    try:
                                        decode_queue.put_nowait(
                                            PacketData(-1, 0, _silence_frame))
                                    except queue.Full:
                                        pass
                            else:
                                # 首次检测: 请求重传
                                self._request_retransmit(missing_seq, 1)
                                _retransmit_state[missing_seq] = (now_mono, 1)
                                try:
                                    decode_queue.put_nowait(
                                        PacketData(-1, 0, _silence_frame))
                                except queue.Full:
                                    pass

                except socket.timeout:
                    continue
                except OSError:
                    break

        except Exception as e:
            log.error(f"RTP 接收错误: {e}")
        finally:
            running = False
            if self._timing_pacer:
                self._timing_pacer.reset()
            try:
                decode_queue.put_nowait(None)
            except queue.Full:
                pass
            rtp_socket.close()
            log.info("RTP 接收线程结束")

    def _decode_audio(self, data: bytes) -> bytes | None:
        """解码音频数据为 PCM"""
        if not self._codec_context:
            return data

        silence = self._silence_frame

        try:
            packet = av.packet.Packet(data)
            frames = self._codec_context.decode(packet)
            if not frames:
                return silence

            ch2 = self._channels * 2
            parts = []
            for frame in frames:
                resampled = self._resampler.resample(frame)
                if isinstance(resampled, list):
                    for f in resampled:
                        mv = memoryview(f.planes[0])
                        parts.append(bytes(mv[:f.samples * ch2]))
                else:
                    mv = memoryview(resampled.planes[0])
                    parts.append(bytes(mv[:resampled.samples * ch2]))
            return b"".join(parts) if parts else silence
        except Exception:
            return silence

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
