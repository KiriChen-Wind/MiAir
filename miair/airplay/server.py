"""AirPlay 接收服务主模块

基于 airplay2-receiver 的核心逻辑，简化为仅接收音频并输出到 HTTP 流。
支持 AirPlay 1 (RAOP) 协议。
"""

import asyncio
import base64
import logging
import os
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

log = logging.getLogger("miair")

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
        self._audio_format = 0
        self._sample_rate = 44100
        self._channels = 2
        self._fmtp_params: list[str] = []  # SDP fmtp 参数

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
        """获取本机 IPv4 地址"""
        hostname = os.getenv("MIAIR_HOSTNAME", "")
        if hostname and hostname != "127.0.0.1":
            return hostname
        
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

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
        session_active = False
        rtp_socket = None
        rtp_thread = None
        control_socket = None
        timing_socket = None
        teardown_done = False  # 避免 TEARDOWN 和 finally 双重触发回调

        # 设置客户端 socket 超时，防止无限阻塞导致线程卡死
        sock.settimeout(30.0)

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
                log.info(f"RTSP {method} {path} CSeq={cseq} body={len(body)} bytes")

                if method == "OPTIONS":
                    response_headers = {
                        "Public": "ANNOUNCE, SETUP, RECORD, PAUSE, FLUSH, FLUSHBUFFERED, TEARDOWN, OPTIONS, POST, GET, PUT, SETPEERSX, SETMAGICCOOKIE, GET_PARAMETER, SET_PARAMETER",
                        "Apple-Jack-Status": "connected; type=analog",
                    }
                    # AirPlay 1 认证: 响应 Apple-Challenge
                    apple_challenge = headers.get("Apple-Challenge")
                    if apple_challenge:
                        log.info(f"Apple-Challenge: {apple_challenge}")
                        apple_response = AP1Security.compute_apple_response(
                            apple_challenge,
                            self.ipv4_bin,
                            self.device_id_bin,
                        )
                        log.info(f"Apple-Response: {apple_response[:50]}...")
                        response_headers["Apple-Response"] = apple_response
                    else:
                        log.info("OPTIONS: 无 Apple-Challenge")
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
                    log.info("RTSP FLUSH: 清空音频缓冲区")
                    # 仅清空队列，不停止流服务器，避免断开客户端
                    self._stream_server.start_streaming() 
                    self._send_rtsp_response(sock, 200, cseq)

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
                    log.info(f"SET_PARAMETER: Content-Type={content_type}, body size={len(body)}")
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
        log.info(f"ANNOUNCE SDP:\n{sdp}")
        log.info(f"ANNOUNCE headers: {headers}")

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
                # 例如: a=rtpmap:96 AppleLossless
                parts = line.split()
                log.info(f"Found rtpmap: {parts}")
                if len(parts) >= 2:
                    fmt = parts[1]
                    if "AppleLossless" in fmt:
                        self._audio_format = 0x2  # ALAC
                        log.info(f"识别到 ALAC 格式")
                    elif "mpeg4-generic" in fmt:
                        self._audio_format = 0x4  # AAC
                        log.info(f"识别到 AAC 格式")
                    elif "L16" in fmt or "PCM" in fmt:
                        self._audio_format = 0x1  # PCM
                        log.info(f"识别到 PCM 格式")
            elif line.startswith("a=fmtp:"):
                # ALAC fmtp: a=fmtp:96 352 0 16 40 10 14 2 255 0 0 44100
                parts = line.split()
                log.info(f"Found fmtp: {parts}")
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

        log.info(f"解析结果: audio_format={self._audio_format}, sr={self._sample_rate}, ch={self._channels}")

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
        else:
            self._session_key = None
            self._session_iv = None
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
        log.info(f"SETUP Transport: {transport}")

        # 创建 RTP 接收 socket (server_port - 音频数据)
        rtp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # 增大内核 UDP 接收缓冲区，防止高频小包场景下内核丢包
        rtp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 524288)
        rtp_socket.settimeout(1.0)
        rtp_socket.bind(("0.0.0.0", 0))
        server_port = rtp_socket.getsockname()[1]

        # 创建 RTCP 控制 socket (control_port)
        control_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        control_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        control_socket.settimeout(2.0)
        control_socket.bind(("0.0.0.0", 0))
        control_port = control_socket.getsockname()[1]

        # 创建 timing socket (NTP 时间同步)
        timing_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        timing_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        timing_socket.settimeout(2.0)
        timing_socket.bind(("0.0.0.0", 0))
        timing_port = timing_socket.getsockname()[1]

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

    def _rtcp_loop(self, rtcp_socket: socket.socket):
        """RTCP 控制包接收循环"""
        log.info("RTCP 线程启动")
        try:
            while self._running:
                try:
                    data, addr = rtcp_socket.recvfrom(256)
                    if not data or len(data) < 4:
                        continue
                    # RTCP 包处理 - 主要用于时间同步
                    # AirPlay 1 使用 RTCP 类型 212 (0xd4) 发送时间信息
                    if len(data) >= 8:
                        ptype = data[1]
                        if ptype == 212:  # TIME_ANNOUNCE_NTP
                            # 提取 sender RTP timestamp 和 playAt timestamp
                            sender_ts = int.from_bytes(data[4:8], 'big')
                            play_at_ts = int.from_bytes(data[16:20], 'big') if len(data) >= 20 else 0
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
        })

    def _timing_loop(self, timing_socket: socket.socket):
        """RAOP NTP 时间同步响应循环

        AirPlay 1 timing 使用 RTP 格式:
        请求: 0x80 0x52 (type=0x52=82 即 TIME_REQUEST) + seq(2) + zero(8) + ref_time(8) + recv_time(8)
        响应: 0x80 0x53 (type=0x53=83 即 TIME_RESPONSE) + seq(2) + ref_time(8) + recv_time(8) + send_time(8)
        共 32 字节
        """
        log.info("Timing 线程启动")
        try:
            while self._running:
                try:
                    data, addr = timing_socket.recvfrom(256)
                    if not data or len(data) < 32:
                        continue

                    # 检查是否为 timing request (type byte = 0x52 or 0xd2)
                    ptype = data[1] & 0x7f  # 去掉 marker bit
                    if ptype != 0x52:
                        continue

                    now = time.time()
                    # NTP 时间戳 (从 1900-01-01 开始的秒数)
                    ntp_now = now + 2208988800.0
                    ntp_sec = int(ntp_now)
                    ntp_frac = int((ntp_now - ntp_sec) * (2**32))

                    response = bytearray(32)
                    response[0] = 0x80  # RTP version 2
                    response[1] = 0xd3  # timing response type (0x53 | 0x80 marker)
                    response[2:4] = data[2:4]  # 复制 sequence number

                    # bytes 4-11: 复制请求中的 reference send time (来自请求的 bytes 24-31)
                    if len(data) >= 32:
                        response[4:12] = data[24:32]
                    # bytes 12-19: receive timestamp (我们收到请求的时间)
                    response[12:16] = ntp_sec.to_bytes(4, 'big')
                    response[16:20] = ntp_frac.to_bytes(4, 'big')
                    # bytes 20-27: send timestamp (我们发送响应的时间)
                    send_now = time.time() + 2208988800.0
                    send_sec = int(send_now)
                    send_frac = int((send_now - send_sec) * (2**32))
                    response[20:24] = send_sec.to_bytes(4, 'big')
                    response[24:28] = send_frac.to_bytes(4, 'big')

                    timing_socket.sendto(bytes(response), addr)
                except socket.timeout:
                    continue
                except OSError:
                    break
        except Exception as e:
            pass
        finally:
            timing_socket.close()

    def _rtp_receive_loop(self, rtp_socket: socket.socket):
        """RTP 音频数据接收循环"""
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
        packet_count = 0
        error_count = 0
        last_seq = 0

        try:
            jitter_buffer = {}  # seq -> pcm_data
            next_seq = -1
            buffer_threshold = 2  # 最小缓冲阈值以降低延迟 (约 16ms)
            # 缓冲区最大上限，防止极端情况下内存无限增长
            max_jitter_size = 100

            while self._running:
                try:
                    data, addr = rtp_socket.recvfrom(2048)
                    if not data or len(data) < 12:
                        continue

                    # RTP 头解析
                    seq = int.from_bytes(data[2:4], 'big')
                    payload_type = data[1] & 0x7f
                    payload = data[12:]

                    # 初始对齐 next_seq
                    if next_seq == -1:
                        next_seq = seq
                        log.info(f"RTP: 初始序列号 {next_seq}")

                    # 将原始 payload 放入抖动缓冲区
                    jitter_buffer[seq] = payload
                    packet_count += 1

                    # 缓冲区过大时强制清理最老的包，防止内存泄漏
                    if len(jitter_buffer) > max_jitter_size:
                        # 丢弃最老的包，跳转到最新的包
                        oldest = min(jitter_buffer.keys(), key=lambda x: (x - next_seq) & 0xFFFF)
                        while len(jitter_buffer) > buffer_threshold and oldest != next_seq:
                            jitter_buffer.pop(oldest, None)
                            oldest = min(jitter_buffer.keys(), key=lambda x: (x - next_seq) & 0xFFFF) if jitter_buffer else next_seq
                        next_seq = min(jitter_buffer.keys(), key=lambda x: (x - next_seq) & 0xFFFF) if jitter_buffer else next_seq

                    # 当缓冲区达到一定大小或已收到下一个期望的包时，开始输出
                    while True:
                        if next_seq in jitter_buffer:
                            ordered_payload = jitter_buffer.pop(next_seq)
                            
                            # 解密 — IV 每包相同，但 CBC 要求每次新建 cipher
                            if self._session_key and self._session_iv:
                                try:
                                    cipher = AES.new(self._session_key, AES.MODE_CBC, self._session_iv[:16])
                                    plen = len(ordered_payload)
                                    decrypt_len = plen & ~0xF
                                    if decrypt_len > 0:
                                        decrypted = cipher.decrypt(ordered_payload[:decrypt_len])
                                        if decrypt_len < plen:
                                            # 用 memoryview 避免尾部切片拷贝
                                            decrypted = decrypted + bytes(memoryview(ordered_payload)[decrypt_len:])
                                        ordered_payload = decrypted
                                    # else: 不足 16 字节无需解密
                                except Exception as e:
                                    next_seq = (next_seq + 1) & 0xFFFF
                                    continue

                            # 解码音频
                            pcm_data = self._decode_audio(ordered_payload)
                            if pcm_data:
                                self._stream_server.write_pcm(pcm_data)
                            else:
                                error_count += 1
                                if error_count > 100:
                                    log.warning(f"RTP: 连续解码失败 {error_count} 次")
                                    error_count = 0

                            last_seq = next_seq
                            next_seq = (next_seq + 1) & 0xFFFF
                            
                        elif len(jitter_buffer) > buffer_threshold:
                            # 缓冲区过大，说明中间丢包了，跳过丢失的包
                            missing_seq = next_seq
                            next_seq = min(jitter_buffer.keys(), key=lambda x: (x - missing_seq) & 0xFFFF)
                            if self._codec_context:
                                try:
                                    self._codec_context.flush_buffers()
                                except Exception as e:
                                    pass
                            continue
                        else:
                            break

                    if packet_count % 500 == 0:
                        log.info(f"RTP: 已接收 {packet_count} 个音频包")

                except socket.timeout:
                    continue
                except OSError:
                    break

        except Exception as e:
            log.error(f"RTP 接收错误: {e}")
            import traceback
            log.error(traceback.format_exc())
        finally:
            rtp_socket.close()
            log.info(f"RTP 接收线程结束，共接收 {packet_count} 个包，最后 seq={last_seq}")

    def _decode_audio(self, data: bytes) -> bytes | None:
        """解码音频数据为 PCM"""
        if not self._codec_context:
            # PCM 模式直接返回 (假设是 s16le)
            return data

        try:
            packet = av.packet.Packet(data)
            frames = self._codec_context.decode(packet)
            if not frames:
                return None

            # 用 memoryview 零拷贝截取有效音频数据
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
            return b"".join(parts) if parts else None
        except Exception as e:
            # 解码失败时返回静音数据，避免音频流中断
            # 返回 10ms 静音数据
            silence_len = self._sample_rate * self._channels * 2 // 100
            return b'\x00' * silence_len

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
