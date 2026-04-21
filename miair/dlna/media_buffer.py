"""服务端音频缓冲 - 下载完整音频到内存以支持 Range/Seek"""

import asyncio
import logging
import threading

import aiohttp

log = logging.getLogger("miair")

# 浏览器 UA，防止远端拒绝
_UA = (
    "Mozilla/5.0 (Linux; Android 12) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Mobile Safari/537.36"
)

# 多线程下载配置
_DOWNLOAD_THREADS = 8  # 并发下载线程数
_CHUNK_SIZE = 1024 * 1024  # 每个分块大小 1MB
_MIN_SIZE_FOR_MULTI_THREAD = 5 * 1024 * 1024  # 最小5MB才使用多线程


class MediaBuffer:
    """管理单个音频文件的下载缓冲（内存）"""

    def __init__(self, remote_url: str):
        self.remote_url = remote_url
        self.content_type: str = "audio/mpeg"
        self.total_size: int = 0  # Content-Length（0 表示未知）
        self.data = bytearray()
        self.download_complete: bool = False
        self.error: str | None = None
        self._headers_event = asyncio.Event()
        self._complete_event = asyncio.Event()
        self._download_task: asyncio.Task | None = None
        self._converting: bool = False  # 转换中标志
        self._converted: bool = False   # 已转换标志
        self._convert_event = asyncio.Event()  # 转换完成事件
        self._supports_range: bool = False  # 服务器是否支持Range请求

    @property
    def downloaded_size(self) -> int:
        return len(self.data)

    async def start_download(self, session: aiohttp.ClientSession):
        """启动后台下载任务"""
        self._download_task = asyncio.create_task(self._do_download(session))

    async def _do_download(self, session: aiohttp.ClientSession):
        """执行下载，写入内存 bytearray"""
        try:
            # 首先尝试获取文件信息
            headers = {"User-Agent": _UA}
            async with session.head(
                self.remote_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=30, connect=10),
                allow_redirects=True,
            ) as resp:
                if resp.status in (200, 206):
                    ct = resp.headers.get("Content-Type")
                    if ct:
                        self.content_type = ct.split(";")[0].strip()
                    
                    cl = resp.headers.get("Content-Length")
                    if cl:
                        try:
                            self.total_size = int(cl)
                        except ValueError:
                            pass
                    
                    # 检查是否支持Range请求
                    accept_ranges = resp.headers.get("Accept-Ranges", "")
                    self._supports_range = "bytes" in accept_ranges.lower()
                
                self._headers_event.set()
            
            # 如果HEAD请求没有明确支持Range，尝试发送一个测试Range请求
            if not self._supports_range and self.total_size > _MIN_SIZE_FOR_MULTI_THREAD:
                self._supports_range = await self._test_range_support(session)
            
            # 根据文件大小和服务器支持情况选择下载方式
            if (self._supports_range and 
                self.total_size > _MIN_SIZE_FOR_MULTI_THREAD):
                # 使用多线程下载
                log.info(f"使用多线程下载: {self.total_size} bytes, {_DOWNLOAD_THREADS} 线程")
                await self._do_multi_thread_download(session)
            else:
                # 使用单线程下载
                if self.total_size > _MIN_SIZE_FOR_MULTI_THREAD:
                    log.info(f"服务器不支持Range，使用单线程下载: {self.total_size} bytes")
                await self._do_single_thread_download(session)
                
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.error = str(e)
            log.error(f"音频缓冲下载失败: {e}")
        finally:
            self._headers_event.set()
            self._complete_event.set()
    
    async def _test_range_support(self, session: aiohttp.ClientSession) -> bool:
        """测试服务器是否实际支持Range请求"""
        try:
            # 尝试请求前1024字节
            headers = {
                "User-Agent": _UA,
                "Range": "bytes=0-1023"
            }
            async with session.get(
                self.remote_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10, connect=5),
                allow_redirects=True,
            ) as resp:
                if resp.status == 206:  # Partial Content
                    return True
                elif resp.status == 200:
                    # 有些服务器会返回200而不是206，但也支持Range
                    # 检查Content-Range头
                    content_range = resp.headers.get("Content-Range", "")
                    if content_range:
                        return True
        except Exception as e:
            pass
        return False

    async def _do_single_thread_download(self, session: aiohttp.ClientSession):
        """单线程下载"""
        headers = {"User-Agent": _UA}
        async with session.get(
            self.remote_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=300, connect=15),
            allow_redirects=True,
        ) as resp:
            if resp.status not in (200, 206):
                self.error = f"HTTP {resp.status}"
                return

            async for chunk in resp.content.iter_chunked(65536):
                self.data.extend(chunk)

        if not self.total_size:
            self.total_size = len(self.data)
        self.download_complete = True
        log.info(
            f"音频缓冲完成(单线程): {self.total_size} bytes, "
            f"{self.remote_url[:80]}..."
        )

    async def _do_multi_thread_download(self, session: aiohttp.ClientSession):
        """多线程分块下载（线程安全）"""
        total_size = self.total_size
        chunk_size = max(_CHUNK_SIZE, total_size // _DOWNLOAD_THREADS)
        
        # 创建分块列表
        chunks = []
        for i in range(0, total_size, chunk_size):
            end = min(i + chunk_size - 1, total_size - 1)
            chunks.append((i, end))
        
        # 初始化数据缓冲区
        self.data = bytearray(total_size)
        # 保护 bytearray 并发写入的锁
        write_lock = threading.Lock()
        
        # 并发下载所有分块
        semaphore = asyncio.Semaphore(_DOWNLOAD_THREADS)
        
        async def download_chunk(start: int, end: int):
            async with semaphore:
                headers = {
                    "User-Agent": _UA,
                    "Range": f"bytes={start}-{end}"
                }
                try:
                    async with session.get(
                        self.remote_url,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=120, connect=10),
                        allow_redirects=True,
                    ) as resp:
                        if resp.status not in (200, 206):
                            log.error(f"分块下载失败: HTTP {resp.status}")
                            return False
                        
                        # 读取数据并写入缓冲区（加锁保护）
                        chunk_data = await resp.read()
                        with write_lock:
                            self.data[start:start + len(chunk_data)] = chunk_data
                        return True
                except Exception as e:
                    log.error(f"分块下载失败 ({start}-{end}): {e}")
                    return False
        
        # 启动所有下载任务
        tasks = [download_chunk(start, end) for start, end in chunks]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        # 检查是否所有分块都下载成功
        success_count = sum(1 for r in results if r is True)
        if success_count == len(chunks):
            self.download_complete = True
            log.info(
                f"音频缓冲完成(多线程): {self.total_size} bytes, "
                f"{len(chunks)} 个分块, {self.remote_url[:80]}..."
            )
        else:
            # 有分块下载失败，尝试使用单线程重新下载
            log.warning(f"多线程下载部分失败 ({success_count}/{len(chunks)})，回退到单线程")
            self.data = bytearray()  # 清空缓冲区
            await self._do_single_thread_download(session)

    async def wait_for_headers(self, timeout: float = 30.0):
        """等待 HTTP 头就绪（content_type / total_size）"""
        try:
            await asyncio.wait_for(self._headers_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.error = "等待响应头超时"

    async def wait_for_completion(self, timeout: float = 120.0):
        """等待整个下载完成"""
        try:
            await asyncio.wait_for(self._complete_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self.error = "下载超时"

    def cleanup(self):
        """清理：取消下载任务、释放内存"""
        if self._download_task and not self._download_task.done():
            self._download_task.cancel()
        self.data = bytearray()
        self.download_complete = False

    async def convert_to_mp3(self) -> bool:
        """将音频转换为mp3格式（用于不支持无损格式的音箱）"""
        # 检查是否已经转换过或正在转换
        if self._converted:
            return True
        if self._converting:
            # 使用 Event 等待转换完成，最多60秒
            try:
                await asyncio.wait_for(self._convert_event.wait(), timeout=60)
            except asyncio.TimeoutError:
                pass
            return self._converted
        
        # 检查是否已经是mp3
        if self.content_type == "audio/mpeg" or self.content_type == "audio/mp3":
            self._converted = True
            return True
        
        self._converting = True
        input_path = None
        output_path = None
        
        try:
            import tempfile
            import os
            
            # 创建临时文件
            with tempfile.NamedTemporaryFile(suffix='.input', delete=False) as input_file:
                input_path = input_file.name
                input_file.write(self.data)
            
            output_path = input_path + '.mp3'
            
            # 使用ffmpeg转换 - 优化参数提高速度
            cmd = [
                'ffmpeg', '-y', 
                '-i', input_path,
                '-vn',  # 禁用视频
                '-codec:a', 'libmp3lame',
                '-q:a', '5',  # 降低质量以提高速度 (0=最高质量, 9=最低)
                '-ar', '44100',  # 标准采样率
                '-ac', '2',  # 立体声
                '-threads', '0',  # 使用所有可用线程
                output_path
            ]
            
            log.info(f"开始转换音频为mp3: {self.content_type} ({len(self.data)} bytes)")
            
            # 使用异步执行避免阻塞
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), 
                    timeout=60
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                log.error("ffmpeg转换超时")
                return False
            
            if process.returncode != 0:
                stderr_str = stderr.decode('utf-8', errors='ignore') if stderr else ""
                log.error(f"ffmpeg转换失败: {stderr_str[:200]}")
                return False
            
            # 读取转换后的文件
            with open(output_path, 'rb') as f:
                converted_data = f.read()
            
            # 更新数据
            original_size = len(self.data)
            self.data = bytearray(converted_data)
            self.total_size = len(self.data)
            self.content_type = "audio/mpeg"
            self._converted = True
            
            log.info(f"音频转换完成: {self.total_size} bytes (原 {original_size} bytes)")
            return True
            
        except Exception as e:
            log.error(f"音频转换失败: {e}")
            return False
        finally:
            # 统一清理临时文件和状态
            import os
            for p in (input_path, output_path):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
            self._converting = False
            self._convert_event.set()
