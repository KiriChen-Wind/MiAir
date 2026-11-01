"""AirPlay 音频 HTTP 流输出

把解码后的 PCM 音频通过 HTTP 提供给小爱音箱播放：
- WAV：直接输出 PCM，零编码延迟（大多数音箱）
- MP3：ffmpeg 实时转码（不支持 WAV 的音箱）

音箱是「自己按实时速率拉流」的客户端，因此这里只做一件事：稳定供货。
"""

import asyncio
import logging
import os
import queue
import socket
import struct
import subprocess
import threading
import time

from aiohttp import web

log = logging.getLogger("miair")

# 单个 ALAC 帧 = 352 采样 ≈ 8ms @ 44.1kHz
_FRAME_SECONDS = 352 / 44100

_MAX_QUEUE_FRAMES = 192             # 缓冲硬上限 ≈ 1.5s，只有音箱卡住时才会触及
_BATCH_WINDOW = 0.016               # 攒包窗口：把 16ms 内的数据合并成一次写出
_BATCH_BYTES = 8192
_BATCH_FRAMES = 32
_KEEPALIVE_GAP = 1.0                # 断流超过 1s 就补 20ms 静音，避免音箱判定流结束
_KEEPALIVE_BPS_DIVISOR = 50         # 20ms 的 PCM 字节数 = bytes_per_second / 50


class AudioStreamServer:
    """把解码后的 PCM 通过 HTTP 流推给音箱"""

    def __init__(self, hostname: str, port: int = 0, audio_format: str = "wav"):
        self.hostname = hostname
        self.port = port
        self._audio_format = audio_format  # "mp3" or "wav"
        self._app = web.Application()
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

        self._audio_queue: queue.Queue[bytes] = queue.Queue(maxsize=_MAX_QUEUE_FRAMES)
        self._sample_rate = 44100
        self._channels = 2
        self._sample_width = 2  # 16-bit

        self._active = False       # 当前是否有 AirPlay 播放会话
        self._abort = False        # 通知当前的 HTTP 拉流线程退出
        self._session_id = int(time.time())

        # 运行期统计：入/出字节一对比就能看出有没有丢音（卡顿时先看这一行）
        # 注意 WAV 模式下两侧都是 PCM；MP3 模式下 出 是转码后的 MP3 字节，不可直接比较
        self._stats = {
            "in_bytes": 0,      # 写入队列的字节
            "out_bytes": 0,     # 已发给音箱的字节
            "dropped": 0,       # 队列溢出丢弃的帧数
            "keepalive": 0,     # 为保活补写的静音字节
        }

        self._setup_routes()

    def _setup_routes(self) -> None:
        if self._audio_format == "mp3":
            self._app.router.add_get("/airplay/stream.mp3", self._handle_stream_mp3)
        else:
            self._app.router.add_get("/airplay/stream.wav", self._handle_stream_wav)

    @property
    def stream_url(self) -> str:
        ext = "mp3" if self._audio_format == "mp3" else "wav"
        return f"http://{self.hostname}:{self.port}/airplay/stream.{ext}?sid={self._session_id}"

    @property
    def bytes_per_second(self) -> int:
        return self._sample_rate * self._channels * self._sample_width

    def buffered_seconds(self) -> float:
        """队列里待发送的音频时长（按 8ms/帧估算，用于观测）"""
        return self._audio_queue.qsize() * _FRAME_SECONDS

    def stats_line(self) -> str:
        s = self._stats
        return (
            f"缓冲={self.buffered_seconds():.2f}s "
            f"入={s['in_bytes'] / 1048576:.1f}MB "
            f"出={s['out_bytes'] / 1048576:.1f}MB "
            f"丢帧={s['dropped']} "
            f"补静音={s['keepalive'] / 1024:.0f}KB"
        )

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "0.0.0.0", self.port)
        await self._site.start()
        self.port = self._site._server.sockets[0].getsockname()[1]
        log.info(f"AirPlay 音频流服务器: http://{self.hostname}:{self.port} (格式: {self._audio_format})")

    async def stop(self) -> None:
        self._active = False
        self._abort = True
        if self._runner:
            await self._runner.cleanup()

    def set_audio_params(self, sample_rate: int, channels: int, sample_width: int = 2) -> None:
        self._sample_rate = sample_rate
        self._channels = channels
        self._sample_width = sample_width

    def start_streaming(self) -> None:
        """新的播放会话开始：清空残留数据并重置统计"""
        self._clear_queue()
        self._active = True
        self._abort = False
        self._session_id = int(time.time())
        self._stats = dict.fromkeys(self._stats, 0)
        log.info(f"音频流: 开始接收 PCM 数据 (格式: {self._audio_format})")

    def stop_streaming(self) -> None:
        self._active = False
        self._abort = True
        self._clear_queue()
        log.info(f"音频流: 停止接收 PCM 数据 ({self.stats_line()})")

    def _clear_queue(self) -> None:
        while True:
            try:
                self._audio_queue.get_nowait()
            except queue.Empty:
                return

    def write_pcm(self, data: bytes) -> None:
        """写入解码后的 PCM（非阻塞）。

        队列满意味着音箱没跟上（卡住/被打断）：丢最旧的一帧保低延迟，
        而不是整块丢弃 —— 整块丢弃会造成可听见的跳音。
        """
        if not self._active:
            return
        self._stats["in_bytes"] += len(data)
        while True:
            try:
                self._audio_queue.put_nowait(data)
                return
            except queue.Full:
                try:
                    self._audio_queue.get_nowait()
                    self._stats["dropped"] += 1
                except queue.Empty:
                    continue

    def _collect(self, first: bytes) -> bytes:
        """把紧随其后已就绪的 PCM 合并成一批，减少小包写出的开销"""
        parts = [first]
        total = len(first)
        deadline = time.perf_counter() + _BATCH_WINDOW
        while total < _BATCH_BYTES and len(parts) < _BATCH_FRAMES:
            if time.perf_counter() >= deadline:
                break
            try:
                extra = self._audio_queue.get_nowait()
            except queue.Empty:
                break
            parts.append(extra)
            total += len(extra)
        return b"".join(parts)

    def _reject_if_inactive(self) -> web.Response | None:
        """流已停止（TEARDOWN 后）返回 404 拒绝拉取，避免音箱把空响应当成 EOF
        反复重连该 URL 造成死循环（氛围灯闪烁、固件崩溃）。未停止返回 None。
        """
        if not self._active:
            log.info("AirPlay: 流已停止，拒绝拉取 (404)")
            return web.Response(status=404, headers={"Connection": "close"})
        return None

    def _build_wav_header(self, data_size: int = 0x7FFFFF00) -> bytes:
        byte_rate = self._sample_rate * self._channels * self._sample_width
        block_align = self._channels * self._sample_width
        return struct.pack(
            '<4sI4s4sIHHIIHH4sI',
            b'RIFF', data_size + 36, b'WAVE',
            b'fmt ', 16, 1, self._channels,
            self._sample_rate, byte_rate, block_align, self._sample_width * 8,
            b'data', data_size,
        )

    @staticmethod
    def _prepare_stream_response(request: web.Request, content_type: str) -> web.StreamResponse:
        """构建流式响应，并关闭 Nagle 让小包立刻发出"""
        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": content_type,
                "Cache-Control": "no-cache, no-store",
                "Pragma": "no-cache",
                "Connection": "close",
                "Transfer-Encoding": "chunked",
            },
        )
        transport = request.transport
        if transport is not None:
            sock = transport.get_extra_info("socket")
            if sock is not None:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return response

    # ============================================================
    # WAV 模式 — 直接输出 PCM，零编码延迟
    # ============================================================

    async def _handle_stream_wav(self, request: web.Request) -> web.StreamResponse:
        rejected = self._reject_if_inactive()
        if rejected is not None:
            return rejected

        response = self._prepare_stream_response(request, "audio/wav")
        await response.prepare(request)

        self._abort = False          # 允许续播（音箱重连时重新开始拉流）
        log.info("AirPlay: 音箱开始拉取 WAV 音频流 (零编码延迟)")

        loop = asyncio.get_running_loop()
        data_ready = asyncio.Event()
        pending: list[bytes] = []
        pending_lock = threading.Lock()
        reader_done = False

        def _reader() -> None:
            """从队列取 PCM 并通知事件循环写出（攒包 + 长时间空转时补静音保活）"""
            nonlocal reader_done
            idle_since: float | None = None
            keepalive_bytes = self.bytes_per_second // _KEEPALIVE_BPS_DIVISOR
            try:
                while self._active and not self._abort:
                    try:
                        chunk = self._audio_queue.get(timeout=0.02)
                    except queue.Empty:
                        if idle_since is None:
                            idle_since = time.perf_counter()
                        elif time.perf_counter() - idle_since >= _KEEPALIVE_GAP:
                            with pending_lock:
                                pending.append(b"\x00" * keepalive_bytes)
                            self._stats["keepalive"] += keepalive_bytes
                            loop.call_soon_threadsafe(data_ready.set)
                            idle_since = time.perf_counter()
                        continue

                    idle_since = None
                    with pending_lock:
                        pending.append(self._collect(chunk))
                    loop.call_soon_threadsafe(data_ready.set)
            except Exception as e:
                log.error(f"WAV reader 异常: {e}")
            finally:
                reader_done = True
                loop.call_soon_threadsafe(data_ready.set)

        threading.Thread(target=_reader, daemon=True).start()

        try:
            await response.write(self._build_wav_header())
            while not reader_done:
                await data_ready.wait()
                data_ready.clear()
                with pending_lock:
                    chunks, pending = pending, []
                if chunks:
                    blob = b"".join(chunks)
                    await response.write(blob)
                    self._stats["out_bytes"] += len(blob)
        except (ConnectionResetError, BrokenPipeError):
            log.info("AirPlay: 音箱断开 WAV 音频流连接")
        finally:
            self._abort = True
            try:
                await response.write_eof()
            except Exception:
                pass

        log.info("AirPlay: WAV 音频流结束")
        return response

    # ============================================================
    # MP3 模式 — ffmpeg 实时转码（用于不支持 WAV 的音箱）
    # ============================================================

    async def _handle_stream_mp3(self, request: web.Request) -> web.StreamResponse:
        rejected = self._reject_if_inactive()
        if rejected is not None:
            return rejected

        response = self._prepare_stream_response(request, "audio/mpeg")
        await response.prepare(request)

        self._abort = False
        log.info("AirPlay: 音箱开始拉取 MP3 音频流")

        ffmpeg_bin = "ffmpeg"
        for candidate in ("ffmpeg.exe", "bin/ffmpeg.exe"):
            if os.path.exists(candidate):
                ffmpeg_bin = os.path.abspath(candidate)
                break

        try:
            proc = subprocess.Popen(
                [
                    ffmpeg_bin,
                    "-loglevel", "error",
                    "-fflags", "+nobuffer+flush_packets",
                    "-flags", "low_delay",
                    "-f", "s16le",
                    "-ar", str(self._sample_rate),
                    "-ac", str(self._channels),
                    "-i", "pipe:0",
                    "-acodec", "libmp3lame",
                    "-compression_level", "0",   # 最快编码速度
                    "-ab", "128k",
                    "-ac", "2",
                    "-ar", "44100",
                    "-flush_packets", "1",
                    "-id3v2_version", "0",
                    "-f", "mp3",
                    "pipe:1",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except FileNotFoundError:
            log.error("ffmpeg 未找到，无法转码音频流")
            await response.write_eof()
            return response

        def _feed() -> None:
            """把 PCM 喂给 ffmpeg（攒包写管道，减少系统调用）"""
            idle_since: float | None = None
            keepalive_bytes = self.bytes_per_second // _KEEPALIVE_BPS_DIVISOR
            try:
                while self._active and not self._abort:
                    try:
                        chunk = self._audio_queue.get(timeout=0.05)
                    except queue.Empty:
                        if idle_since is None:
                            idle_since = time.perf_counter()
                        elif time.perf_counter() - idle_since >= _KEEPALIVE_GAP:
                            proc.stdin.write(b"\x00" * keepalive_bytes)
                            self._stats["keepalive"] += keepalive_bytes
                            idle_since = time.perf_counter()
                        continue
                    idle_since = None
                    proc.stdin.write(self._collect(chunk))
            except (BrokenPipeError, OSError, ValueError):
                pass
            finally:
                try:
                    proc.stdin.close()
                except Exception:
                    pass

        threading.Thread(target=_feed, daemon=True).start()

        try:
            while self._active and not self._abort:
                audio_data = await asyncio.to_thread(proc.stdout.read, 8192)
                if not audio_data:
                    break
                await response.write(audio_data)
                self._stats["out_bytes"] += len(audio_data)
        except (ConnectionResetError, BrokenPipeError):
            log.info("AirPlay: 音箱断开 MP3 音频流连接")
        finally:
            self._abort = True
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                proc.kill()
            try:
                await response.write_eof()
            except Exception:
                pass

        log.info("AirPlay: MP3 音频流结束")
        return response
