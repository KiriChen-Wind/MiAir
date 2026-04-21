"""每个音箱对应的 AirPlay 接收器

为每个小爱音箱创建一个独立的 AirPlay 接收服务，
手机连接后音频直接转发到对应音箱播放。
"""

import asyncio
import logging
import time

from zeroconf import Zeroconf, IPVersion

from miair.airplay.server import AirPlayServer
from miair.speaker import SpeakerController

log = logging.getLogger("miair")


class SpeakerAirPlay:
    """单个音箱的 AirPlay 接收器包装"""

    def __init__(self, hostname: str, controller: SpeakerController,
                 shared_zeroconf: Zeroconf | None = None, config=None):
        self.hostname = hostname
        self.controller = controller
        self.speaker = controller.speaker
        # 使用音箱名称作为 AirPlay 设备名
        self.device_name = self.speaker.get_dlna_name()
        self.shared_zeroconf = shared_zeroconf
        self.config = config
        self.airplay_server: AirPlayServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None  # 保存事件循环引用
        # AirPlay 状态轮询（打断续播）
        self._stream_url: str = ""  # 当前播放的 HTTP 流 URL
        self._airplay_active: bool = False  # AirPlay 是否活跃
        self._poll_task: asyncio.Task | None = None  # 状态轮询任务
        self._play_grace_until: float = 0.0  # play 后宽限期

    async def start(self):
        """启动该音箱的 AirPlay 服务"""
        try:
            # 保存当前事件循环，以便在 RTSP 线程中安全调用异步函数
            self._loop = asyncio.get_running_loop()

            self.airplay_server = AirPlayServer(
                self.hostname, self.device_name, self.shared_zeroconf,
                speaker_hardware=self.speaker.hardware
            )

            # 设置回调：直接播放到这个音箱
            self.airplay_server.on_play_start = self._on_play_start
            self.airplay_server.on_play_stop = self._on_play_stop
            self.airplay_server.on_volume_change = self._on_volume_change

            await self.airplay_server.start()
            log.info(f"音箱 {self.device_name} 的 AirPlay 服务已启动，端口: {self.airplay_server.rtsp_port}")
        except Exception as e:
            log.error(f"启动音箱 {self.device_name} 的 AirPlay 服务失败: {e}")
            raise

    async def stop(self):
        """停止该音箱的 AirPlay 服务"""
        self._airplay_active = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self.airplay_server:
            await self.airplay_server.stop()
            self.airplay_server = None
            log.info(f"音箱 {self.device_name} 的 AirPlay 服务已停止")

    def _on_play_start(self, stream_url: str):
        """AirPlay 开始播放 - 直接推送到这个音箱

        注意: 这个回调从 RTSP 线程调用，不在 asyncio 事件循环中。
        必须使用 run_coroutine_threadsafe 安全调度异步任务。
        """
        log.info(f"AirPlay 音频推送到 {self.device_name}: {stream_url}")
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._play_on_speaker(stream_url), self._loop)
        else:
            log.warning(f"AirPlay: 事件循环未运行，无法播放到 {self.device_name}")

    async def _play_on_speaker(self, stream_url: str):
        """在对应音箱上播放"""
        try:
            self._stream_url = stream_url
            self._airplay_active = True
            self._play_grace_until = time.time() + 10.0  # 10秒宽限期
            success = await self.controller.play_url(stream_url)
            if success:
                log.info(f"AirPlay 音频已在 {self.device_name} 开始播放")
                # 启动状态轮询（打断续播）
                self._start_poll()
            else:
                log.warning(f"AirPlay 音频在 {self.device_name} 播放失败")
        except Exception as e:
            log.error(f"AirPlay 播放到 {self.device_name} 失败: {e}")

    def _on_play_stop(self):
        """AirPlay 停止播放

        注意: 这个回调从 RTSP 线程调用，不在 asyncio 事件循环中。
        """
        log.info(f"AirPlay 停止播放到 {self.device_name}")
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._stop_speaker(), self._loop)
        else:
            log.warning(f"AirPlay: 事件循环未运行，无法停止 {self.device_name}")

    async def _stop_speaker(self):
        """停止音箱播放"""
        self._airplay_active = False
        self._stream_url = ""
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        try:
            await self.controller.stop()
        except Exception as e:
            pass

    def _start_poll(self):
        """启动 AirPlay 状态轮询任务"""
        if self._poll_task and not self._poll_task.done():
            return  # 已在运行
        self._poll_task = asyncio.create_task(self._poll_speaker_state())

    async def _poll_speaker_state(self):
        """轮询音箱状态，检测打断并自动续播

        当音箱被语音唤醒打断（status 从 playing 变成 stopped）时，
        只要 AirPlay 音频流仍然活跃，就自动重新 play_url 恢复播放。
        """
        try:
            while self._airplay_active and self._stream_url:
                await asyncio.sleep(3)  # 3秒轮询一次
                if not self._airplay_active or not self._stream_url:
                    break

                # 检查 AirPlay 音频流是否还在输出
                if self.airplay_server and not self.airplay_server.is_playing:
                    break

                # 宽限期内不轮询
                if time.time() < self._play_grace_until:
                    continue

                try:
                    status = await self.controller.get_status()
                    speaker_status = status.get("status", 0)
                    # status: 0=stopped, 1=playing, 2=paused
                    if speaker_status == 1:
                        continue  # 正在播放，一切正常

                    # 音箱不在播放状态，但 AirPlay 流还在 → 被打断了
                    log.info(
                        f"[{self.device_name}] AirPlay 检测到播放中断 "
                        f"(speaker_status={speaker_status})，"
                        f"等待后自动续播..."
                    )
                    # 等待打断结束（如语音回复完毕）
                    resume_delay = 5
                    if self.config:
                        resume_delay = getattr(self.config, 'resume_delay_seconds', 5)
                    await asyncio.sleep(resume_delay)

                    # 再次检查 AirPlay 是否仍然活跃
                    if not self._airplay_active or not self._stream_url:
                        break
                    if self.airplay_server and not self.airplay_server.is_playing:
                        break

                    # 重新播放（使用新 URL 防止音箱缓存旧响应）
                    base_url = self._stream_url.split('?')[0]
                    fresh_url = f"{base_url}?sid={int(time.time())}"
                    log.info(f"[{self.device_name}] AirPlay 自动续播: {fresh_url}")
                    self._play_grace_until = time.time() + 10.0
                    success = await self.controller.play_url(fresh_url)
                    if success:
                        log.info(f"[{self.device_name}] AirPlay 续播成功")
                    else:
                        log.warning(f"[{self.device_name}] AirPlay 续播失败")

                except Exception as e:
                    pass
        except asyncio.CancelledError:
            pass
        except Exception as e:
            pass

    def _on_volume_change(self, vol_db: float):
        """处理音量改变
        
        注意: 这个回调从 RTSP 线程调用，不在 asyncio 事件循环中。
        """
        # AirPlay 音量范围: -144 (静音) 到 0 (最大)
        if vol_db <= -144:
            volume = 0
        elif vol_db >= 0:
            volume = 100
        else:
            # 使用声压级对数映射 (10^(dB/20))，这更符合人耳听觉和 iOS 的滑动曲线
            # 0dB -> 1.0 (100%)
            # -20dB -> 0.1 (10%)
            # -40dB -> 0.01 (1%)
            volume = int(pow(10, vol_db / 20) * 100)
            
            # 确保即使在低分贝下也有基本的映射，避免由于 int() 导致的过早归零
            if volume == 0 and vol_db > -144:
                volume = 1

        log.info(f"AirPlay 音量同步到 {self.device_name}: {vol_db} dB -> {volume}%")
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self.controller.set_volume(volume), self._loop)


class AirPlayManager:
    """管理所有音箱的 AirPlay 接收器"""

    def __init__(self, hostname: str, config=None):
        self.hostname = hostname
        self.config = config
        self.speaker_airplays: dict[str, SpeakerAirPlay] = {}  # did -> SpeakerAirPlay
        self._shared_zeroconf: Zeroconf | None = None

    async def start_for_speakers(self, controllers: dict[str, SpeakerController]):
        """为所有音箱启动 AirPlay 服务"""
        # 创建一个共享的 Zeroconf 实例
        if not self._shared_zeroconf:
            self._shared_zeroconf = Zeroconf(ip_version=IPVersion.All)
            log.info("创建共享 Zeroconf 实例用于所有音箱")

        for did, controller in controllers.items():
            if did in self.speaker_airplays:
                # 已经存在，跳过
                continue

            try:
                speaker_airplay = SpeakerAirPlay(
                    self.hostname, controller, self._shared_zeroconf,
                    config=self.config
                )
                await speaker_airplay.start()
                self.speaker_airplays[did] = speaker_airplay
            except Exception as e:
                log.error(f"为音箱 {controller.speaker.get_dlna_name()} 启动 AirPlay 失败: {e}")

        log.info(f"共启动了 {len(self.speaker_airplays)} 个音箱的 AirPlay 服务")

    async def stop(self):
        """停止所有 AirPlay 服务"""
        for did, speaker_airplay in list(self.speaker_airplays.items()):
            try:
                await speaker_airplay.stop()
            except Exception as e:
                log.error(f"停止音箱 AirPlay 失败: {e}")
        self.speaker_airplays.clear()

        # 关闭共享的 zeroconf
        if self._shared_zeroconf:
            try:
                self._shared_zeroconf.close()
                log.info("共享 Zeroconf 已关闭")
            except Exception as e:
                log.error(f"关闭 Zeroconf 失败: {e}")
            self._shared_zeroconf = None

        log.info("所有 AirPlay 服务已停止")

    async def restart_for_speakers(self, controllers: dict[str, SpeakerController]):
        """重新为音箱启动 AirPlay 服务"""
        await self.stop()
        await self.start_for_speakers(controllers)
