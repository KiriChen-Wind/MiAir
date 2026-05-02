"""DLNA 渲染器状态机"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Callable

from miair.const import (
    TRANSPORT_STATE_NO_MEDIA,
    TRANSPORT_STATE_PAUSED,
    TRANSPORT_STATE_PLAYING,
    TRANSPORT_STATE_STOPPED,
    TRANSPORT_STATE_TRANSITIONING,
    TRANSPORT_STATUS_OK,
)
from miair.speaker import SpeakerController

log = logging.getLogger("miair")


class DLNARenderer:
    """每个音箱对应一个 DLNA 渲染器实例，管理传输状态"""

    def __init__(self, udn: str, friendly_name: str, speaker: SpeakerController, default_volume: int = 50, config=None):
        self.udn = udn
        self.friendly_name = friendly_name
        self.speaker = speaker
        self.config = config
        # 保存did以便快速访问
        self.did = speaker.did
        self._lock = asyncio.Lock()

        # 传输状态
        self.transport_state = TRANSPORT_STATE_NO_MEDIA
        self.transport_status = TRANSPORT_STATUS_OK
        self.current_uri = ""
        self.current_uri_metadata = ""
        self.play_speed = "1"

        # 音量/静音
        self.volume = default_volume
        self.mute = False
        self._pre_mute_volume = default_volume

        # 事件管理器 (由 DeviceServer 注入)
        self.event_manager = None
        # 代理 URL 生成函数 (由 DeviceServer 注入)
        self.proxy_url_func = None
        # Seek 代理 URL 生成函数 (由 DeviceServer 注入)
        self.seek_url_func = None
        # 预缓冲函数 (由 DeviceServer 注入，SetAVTransportURI 时提前下载)
        self.pre_buffer_func = None
        # 代理中止回调 (由 DeviceServer 注入，替代双向引用)
        self.abort_proxy_func: Callable[[str], None] | None = None

        # 位置追踪 (基于定时器的近似值)
        self._play_start_time: float = 0.0
        self._accumulated_time: float = 0.0
        self._track_duration: float = 0.0

        # Next URI
        self.next_uri: str = ""
        self.next_uri_metadata: str = ""
        
        # 播放状态检查任务
        self._play_check_task: asyncio.Task | None = None
        # play() 后的宽限期，在此时间之前轮询不覆盖 PLAYING 状态
        self._play_grace_until: float = 0.0
        # 用户主动暂停/停止标志：True 表示用户手动暂停/停止，不应自动续播
        self._user_stopped: bool = False
        # 最后一次接收 DLNA 控制命令的时间（用于检测控制端断开）
        self._last_control_time: float = 0.0
        # 检测 "音箱已停而渲染器被强制保持PAUSED" 的起始时间
        # 当此模式持续超过阈值时，判定控制端已断开并重置为空闲
        self._stuck_paused_since: float = 0.0

    # 视频格式扩展名列表
    VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.flv', '.wmv', '.m4v', '.3gp', '.ts', '.mts', '.m2ts'}
    
    def _is_video_uri(self, uri: str) -> bool:
        """检查URI是否为视频文件"""
        uri_lower = uri.lower()
        # 检查常见的视频扩展名
        for ext in self.VIDEO_EXTENSIONS:
            if uri_lower.endswith(ext) or f"{ext}?" in uri_lower or f"{ext}&" in uri_lower:
                return True
        # 检查URL中的content-type提示
        if 'video/' in uri_lower:
            return True
        return False

    def _needs_transcode(self) -> bool:
        """检查当前音箱是否需要转码（不支持无损格式的硬件）"""
        if not self.speaker:
            return False
        speaker_cfg = getattr(self.speaker, 'speaker', None)
        if not speaker_cfg:
            return False
        return getattr(speaker_cfg, 'hardware', '') in (
            getattr(speaker_cfg, '_NON_LOSSLESS_HARDWARE', set())
        )
    
    async def set_av_transport_uri(self, uri: str, metadata: str = "") -> bool:
        """设置媒体 URI (DLNA SetAVTransportURI)"""
        # 检查是否为视频文件，如果是则拒绝
        if self._is_video_uri(uri):
            log.warning(f"[{self.friendly_name}] 拒绝视频文件: {uri[:80]}...")
            return False
            
        async with self._lock:
            self.current_uri = uri
            self.current_uri_metadata = metadata
            self.transport_state = TRANSPORT_STATE_STOPPED
            # 重置位置
            self._play_start_time = 0.0
            self._accumulated_time = 0.0
            self._track_duration = self._parse_duration_from_metadata(metadata)
            log.info(f"[{self.friendly_name}] SetAVTransportURI: {uri}")
        # 提前开始缓冲音频（在 Play 之前下载，减少等待）
        if self.pre_buffer_func:
            self.pre_buffer_func(uri)
        await self.notify_state_change()
        return True

    async def _check_play_status(self):
        """定期检查播放状态，当歌曲接近结束时主动触发下一曲"""
        near_end_count = 0  # 连续检测到接近结尾的次数
        while True:
            await asyncio.sleep(1)
            async with self._lock:
                if self.transport_state != TRANSPORT_STATE_PLAYING:
                    break
                
                # 计算当前播放位置
                current_position = self._get_elapsed_time()
                
                # 如果有曲长信息，且接近结束（剩余1秒以内）
                # 需要连续2次检测确认，避免位置计算偏差导致误触发
                if self._track_duration > 0 and (self._track_duration - current_position) < 1.0:
                    near_end_count += 1
                    if near_end_count >= 2:
                        log.info(f"[{self.friendly_name}] 歌曲即将结束，剩余 {self._track_duration - current_position:.1f} 秒")
                        # 主动触发下一曲
                        asyncio.get_running_loop().create_task(self.next_track())
                        break
                else:
                    near_end_count = 0

    async def play(self) -> bool:
        """开始播放 (DLNA Play)"""
        needs_transcode = self._needs_transcode()
        play_url = None

        async with self._lock:
            if not self.current_uri:
                log.warning(f"[{self.friendly_name}] Play 但没有设置 URI")
                return False
            if not self.speaker:
                log.error(f"[{self.friendly_name}] 无可用 speaker 控制器")
                return False

            # 取消之前的检查任务
            if self._play_check_task:
                self._play_check_task.cancel()
                self._play_check_task = None

            self.transport_state = TRANSPORT_STATE_TRANSITIONING
            self._user_stopped = False
            self._stuck_paused_since = 0.0
            log.info(f"[{self.friendly_name}] Play: {self.current_uri}")

            # 计算当前播放位置（用于从暂停位置继续播放）
            resume_position = self._accumulated_time
            if self._play_start_time > 0:
                resume_position += time.time() - self._play_start_time

            # 通过本地代理转发 URL (避免音箱直接拉取远端长 URL 失败)
            play_url = self.current_uri
            if self.proxy_url_func:
                play_url = self.proxy_url_func(self.current_uri, self.udn)
                log.info(f"[{self.friendly_name}] 代理 URL: {play_url}")

            # 如果有累计播放时间，使用seek功能从该位置继续播放
            if resume_position > 0 and self.seek_url_func:
                log.info(f"[{self.friendly_name}] 从 {self._format_time(resume_position)} 继续播放")
                track_duration = self._track_duration if self._track_duration > 0 else 3600.0
                seek_url = await self.seek_url_func(
                    self.current_uri, resume_position, track_duration, self.udn
                )
                if seek_url:
                    play_url = seek_url
                    log.info(f"[{self.friendly_name}] Seek URL: {play_url}")

            # 转码模式: 立即标记为 PLAYING 并设置宽限期
            # 让手机端先收到 PLAYING 通知，避免转码期间手机显示暂停
            # WAV 转码很快（1-2秒），8秒宽限期绰绰有余
            if needs_transcode:
                self.transport_state = TRANSPORT_STATE_PLAYING
                self._play_grace_until = time.time() + 8.0
                log.info(f"[{self.friendly_name}] 转码模式: 先返回 PLAYING 状态")

        # 转码模式: 释放锁后立即推送 PLAYING 给手机端
        if needs_transcode:
            await self.notify_state_change()

        # 发送实际播放指令
        async with self._lock:
            success = await self.speaker.play_url(play_url)
            if success:
                self.transport_state = TRANSPORT_STATE_PLAYING
                self._play_start_time = time.time()
                if not needs_transcode:
                    self._play_grace_until = time.time() + 8.0
                log.info(f"[{self.friendly_name}] 播放成功")
                self._play_check_task = asyncio.create_task(self._check_play_status())
                asyncio.create_task(self._apply_default_volume())
            else:
                self.transport_state = TRANSPORT_STATE_STOPPED
                self._play_grace_until = 0.0
                log.error(f"[{self.friendly_name}] 播放失败")
        await self.notify_state_change()
        return success

    async def _apply_default_volume(self):
        """播放开始后应用默认音量"""
        try:
            if getattr(self.config, 'follow_device_volume', False):
                return
            
            default_vol = getattr(self.config, 'default_volume', 50)
            if default_vol <= 0:
                return
                
            await asyncio.sleep(0.5)
            if self.speaker:
                await self.speaker.set_volume(default_vol)
                self.volume = default_vol
                log.info(f"[{self.friendly_name}] 已应用默认音量: {default_vol}%")
                await self.notify_state_change()
        except Exception as e:
            log.error(f"[{self.friendly_name}] 应用默认音量失败: {e}")

    async def pause(self) -> bool:
        """暂停播放 (DLNA Pause)"""
        async with self._lock:
            if not self.speaker:
                self.transport_state = TRANSPORT_STATE_PAUSED
                return True
            success = await self.speaker.pause()
            if success:
                # 累计播放时间
                if self._play_start_time > 0:
                    self._accumulated_time += time.time() - self._play_start_time
                    self._play_start_time = 0.0
                self.transport_state = TRANSPORT_STATE_PAUSED
                self._user_stopped = True
                # 取消播放状态检查任务
                if self._play_check_task:
                    self._play_check_task.cancel()
                    self._play_check_task = None
                log.info(f"[{self.friendly_name}] 已暂停")
        await self.notify_state_change()
        return success

    async def stop(self) -> bool:
        """停止播放 (DLNA Stop)"""
        # 立即中止所有活跃的媒体代理连接，防止音箱在断开后播放缓存残余
        if self.abort_proxy_func:
            self.abort_proxy_func(self.udn)
            
        async with self._lock:
            if not self.speaker:
                self.transport_state = TRANSPORT_STATE_STOPPED
                return True
            success = await self.speaker.stop()
            if success:
                self.transport_state = TRANSPORT_STATE_STOPPED
                self._accumulated_time = 0.0
                self._play_start_time = 0.0
                self._user_stopped = True
                # 取消播放状态检查任务
                if self._play_check_task:
                    self._play_check_task.cancel()
                    self._play_check_task = None
                log.info(f"[{self.friendly_name}] 已停止")
        await self.notify_state_change()
        return success

    async def reset_to_idle(self):
        """重置为空闲状态（控制端断开连接后调用）"""
        async with self._lock:
            if self.transport_state == TRANSPORT_STATE_NO_MEDIA:
                return
            old_state = self.transport_state
            self.transport_state = TRANSPORT_STATE_NO_MEDIA
            self.current_uri = ""
            self.current_uri_metadata = ""
            self.next_uri = ""
            self.next_uri_metadata = ""
            self._accumulated_time = 0.0
            self._play_start_time = 0.0
            self._track_duration = 0.0
            self._user_stopped = False
            self._stuck_paused_since = 0.0
            if self._play_check_task:
                self._play_check_task.cancel()
                self._play_check_task = None
            log.info(f"[{self.friendly_name}] 控制端断开，重置为空闲 ({old_state})")
        await self.notify_state_change()

    async def seek(self, unit: str, target: str) -> bool:
        """Seek - 生成格式正确的 seeked 音频并重新播放
        
        耗时的 seek_url_func 调用在锁外执行，避免长时间持有锁导致
        轮询任务阻塞和位置计算混乱。
        """
        if unit == "REL_TIME":
            seconds = self._parse_time(target)

            # 先在锁外生成 seek URL（耗时操作：等待缓冲+ffmpeg）
            seek_url = None
            current_uri = None
            duration = 0.0
            if self.seek_url_func and self.speaker and self.current_uri:
                async with self._lock:
                    duration = self._track_duration
                    current_uri = self.current_uri
                if duration > 0 and current_uri:
                    seek_url = await self.seek_url_func(
                        current_uri, seconds, duration, self.udn
                    )

            # 然后在锁内修改状态
            async with self._lock:
                if seek_url:
                    log.info(
                        f"[{self.friendly_name}] Seek to {target} "
                        f"({seconds:.1f}s/{self._track_duration:.1f}s)"
                    )
                    
                    # 保存当前状态，以便在暂停状态下恢复
                    was_playing = self.transport_state == TRANSPORT_STATE_PLAYING
                    was_paused = self.transport_state == TRANSPORT_STATE_PAUSED
                    
                    self.transport_state = TRANSPORT_STATE_TRANSITIONING
                    
                    # 如果当前正在播放，先停止
                    if was_playing:
                        await self.speaker.stop()
                    
                    success = await self.speaker.play_url(seek_url)
                    if success:
                        self._accumulated_time = seconds
                        
                        # 如果之前是暂停状态，seek后暂停在当前位置
                        if was_paused:
                            await self.speaker.pause()
                            self._play_start_time = 0.0
                            self.transport_state = TRANSPORT_STATE_PAUSED
                            log.info(f"[{self.friendly_name}] Seek 成功（保持暂停）")
                        else:
                            self._play_start_time = time.time()
                            self.transport_state = TRANSPORT_STATE_PLAYING
                            log.info(f"[{self.friendly_name}] Seek 成功")
                    else:
                        self.transport_state = TRANSPORT_STATE_STOPPED
                        log.error(f"[{self.friendly_name}] Seek 播放失败")
                    # 在锁外发送通知
                    asyncio.get_running_loop().create_task(self.notify_state_change())
                    return success

                # 回退: 软 Seek（仅更新内部位置追踪）
                self._accumulated_time = seconds
                if self.transport_state == TRANSPORT_STATE_PLAYING:
                    self._play_start_time = time.time()
                log.info(f"[{self.friendly_name}] Seek to {target} (soft)")
                return True
        elif unit == "TRACK_NR":
            log.info(f"[{self.friendly_name}] Seek TRACK_NR={target} (ignored)")
            return True
        return False

    async def next_track(self):
        """播放下一曲"""
        if self.next_uri:
            # 先停止当前播放
            if self.abort_proxy_func:
                self.abort_proxy_func(self.udn)
            if self.speaker:
                await self.speaker.stop()
                log.info(f"[{self.friendly_name}] 已停止当前播放，准备切换到下一曲")
                # 增加延迟到 1.0s，确保音箱完全停止播放并清空硬件缓存
                await asyncio.sleep(1.0)
            
            # 更新播放信息
            self.current_uri = self.next_uri
            self.current_uri_metadata = self.next_uri_metadata
            self.next_uri = ""
            self.next_uri_metadata = ""
            self._accumulated_time = 0.0
            self._track_duration = self._parse_duration_from_metadata(
                self.current_uri_metadata
            )
            
            if self.speaker:
                play_url = self.current_uri
                if self.proxy_url_func:
                    play_url = self.proxy_url_func(self.current_uri, self.udn)
                async with self._lock:
                    self.transport_state = TRANSPORT_STATE_TRANSITIONING
                success = await self.speaker.play_url(play_url)
                async with self._lock:
                    if success:
                        self.transport_state = TRANSPORT_STATE_PLAYING
                        self._play_start_time = time.time()
                        # 启动播放状态检查任务
                        if self._play_check_task:
                            self._play_check_task.cancel()
                            self._play_check_task = None
                        self._play_check_task = asyncio.create_task(self._check_play_status())
                    else:
                        self.transport_state = TRANSPORT_STATE_STOPPED
        else:
            # 没有预设下一首 — 模拟"自然播完"信号
            # 将位置设到曲末，让控制端判定为自然结束并自动推进播放列表
            # 先停止当前播放，确保不会卡在最后三秒
            if self.speaker:
                await self.speaker.stop()
                log.info(f"[{self.friendly_name}] 已停止当前播放，模拟自然播完")
                # 添加短暂延迟，确保音箱完全停止播放
                await asyncio.sleep(0.5)
            
            async with self._lock:
                if self._track_duration > 0:
                    self._accumulated_time = self._track_duration
                self._play_start_time = 0.0
                self.transport_state = TRANSPORT_STATE_STOPPED
                # 取消播放状态检查任务
                if self._play_check_task:
                    self._play_check_task.cancel()
                    self._play_check_task = None
            log.info(
                f"[{self.friendly_name}] 切歌: 无 next_uri，"
                f"模拟自然播完 (位置={self._format_time(self._accumulated_time)})"
            )
        await self.notify_state_change()

    async def previous_track(self):
        """上一曲 (重新播放当前曲)"""
        async with self._lock:
            self._accumulated_time = 0.0
            self._play_start_time = time.time()
        if self.speaker and self.current_uri:
            play_url = self.current_uri
            if self.proxy_url_func:
                play_url = self.proxy_url_func(self.current_uri, self.udn)
            await self.speaker.play_url(play_url)
        await self.notify_state_change()

    async def set_next_av_transport_uri(self, uri: str, metadata: str = ""):
        """设置下一首的 URI"""
        self.next_uri = uri
        self.next_uri_metadata = metadata
        log.info(f"[{self.friendly_name}] SetNextAVTransportURI: {uri}")

    def get_current_transport_actions(self) -> str:
        """根据当前状态返回可用动作列表"""
        if self.transport_state == TRANSPORT_STATE_PLAYING:
            return "Pause,Stop,Seek,Next"
        elif self.transport_state == TRANSPORT_STATE_PAUSED:
            return "Play,Stop,Seek,Next"
        elif self.transport_state == TRANSPORT_STATE_STOPPED:
            return "Play,Seek"
        elif self.transport_state == TRANSPORT_STATE_NO_MEDIA:
            return ""
        return "Play,Pause,Stop,Seek,Next"

    def get_transport_info(self) -> dict:
        """获取传输信息 (DLNA GetTransportInfo)"""
        return {
            "CurrentTransportState": self.transport_state,
            "CurrentTransportStatus": self.transport_status,
            "CurrentSpeed": self.play_speed,
        }

    def get_position_info(self) -> dict:
        """获取位置信息 (DLNA GetPositionInfo)"""
        rel_time = self._get_elapsed_time()
        duration = self._track_duration

        return {
            "Track": "1" if self.current_uri else "0",
            "TrackDuration": self._format_time(duration),
            "TrackMetaData": self.current_uri_metadata,
            "TrackURI": self.current_uri,
            "RelTime": self._format_time(rel_time),
            "AbsTime": self._format_time(rel_time),
            "RelCount": "0",
            "AbsCount": "0",
        }

    def get_media_info(self) -> dict:
        """获取媒体信息 (DLNA GetMediaInfo)"""
        return {
            "NrTracks": "1" if self.current_uri else "0",
            "MediaDuration": self._format_time(self._track_duration),
            "CurrentURI": self.current_uri,
            "CurrentURIMetaData": self.current_uri_metadata,
            "NextURI": self.next_uri,
            "NextURIMetaData": self.next_uri_metadata,
            "PlayMedium": "NETWORK",
            "RecordMedium": "NOT_IMPLEMENTED",
            "WriteStatus": "NOT_IMPLEMENTED",
        }

    def get_transport_settings(self) -> dict:
        """获取传输设置 (DLNA GetTransportSettings)"""
        return {
            "PlayMode": "NORMAL",
            "RecQualityMode": "NOT_IMPLEMENTED",
        }

    async def set_volume(self, volume: int) -> bool:
        """设置音量"""
        volume = max(0, min(100, volume))
        if not self.speaker:
            self.volume = volume
            return True
        success = await self.speaker.set_volume(volume)
        if success:
            self.volume = volume
            if volume > 0:
                self.mute = False
        return success

    async def get_volume(self) -> int:
        """获取音量"""
        if not self.speaker:
            return self.volume
        vol = await self.speaker.get_volume()
        self.volume = vol
        return vol

    async def set_mute(self, mute: bool) -> bool:
        """设置静音"""
        if mute and not self.mute:
            self._pre_mute_volume = self.volume
            if self.speaker:
                success = await self.speaker.set_volume(0)
            else:
                success = True
        elif not mute and self.mute:
            if self.speaker:
                success = await self.speaker.set_volume(self._pre_mute_volume)
            else:
                success = True
        else:
            success = True
        if success:
            self.mute = mute
        return success

    def get_mute(self) -> bool:
        """获取静音状态"""
        return self.mute

    async def notify_state_change(self):
        """发送状态变更事件通知"""
        if not self.event_manager:
            return
        try:
            from miair.dlna.eventing import build_last_change_event

            event_xml = build_last_change_event(
                transport_state=self.transport_state, volume=self.volume
            )
            await self.event_manager.notify_all(event_xml)
        except Exception as e:
            log.error(f"[{self.friendly_name}] 事件通知失败: {e}")

    def _get_elapsed_time(self) -> float:
        """获取当前播放已经过的秒数"""
        if self.transport_state == TRANSPORT_STATE_PLAYING and self._play_start_time > 0:
            return self._accumulated_time + (time.time() - self._play_start_time)
        return self._accumulated_time

    @staticmethod
    def _format_time(seconds: float) -> str:
        """将秒数转换为 HH:MM:SS 格式"""
        if seconds <= 0:
            return "00:00:00"
        total = int(seconds)
        h = total // 3600
        m = (total % 3600) // 60
        s = total % 60
        return f"{h:02d}:{m:02d}:{s:02d}"

    @staticmethod
    def _parse_time(time_str: str) -> float:
        """解析 HH:MM:SS 格式为秒数"""
        try:
            parts = time_str.split(":")
            if len(parts) == 3:
                h, m, s = parts
                return int(h) * 3600 + int(m) * 60 + float(s)
            elif len(parts) == 2:
                m, s = parts
                return int(m) * 60 + float(s)
        except (ValueError, IndexError):
            pass
        return 0.0

    @staticmethod
    def _parse_duration_from_metadata(metadata: str) -> float:
        """从 DIDL-Lite 元数据中解析 duration"""
        if not metadata:
            return 0.0
        try:
            # 查找 duration 属性: duration="HH:MM:SS" 或 duration="H:MM:SS.xxx"
            match = re.search(r'duration="([^"]+)"', metadata)
            if match:
                duration_str = match.group(1)
                # 去掉毫秒部分
                if "." in duration_str:
                    duration_str = duration_str.split(".")[0]
                return DLNARenderer._parse_time(duration_str)

            # 备选: 尝试解析 XML
            root = ET.fromstring(metadata)
            for elem in root.iter():
                duration = elem.get("duration")
                if duration:
                    if "." in duration:
                        duration = duration.split(".")[0]
                    return DLNARenderer._parse_time(duration)
        except Exception:
            pass
        return 0.0
