"""AirPlay 音频链路核心逻辑测试

覆盖 JitterBuffer 的重排/缺口处理，以及 FairPlay 分包解密（曾经因为复用
pycryptodome 的有状态 cipher 对象导致「有数据流但没声音」）。

用法: python3 tests/test_airplay_stream.py
"""

import asyncio
import os
import sys
import threading
import time

# 确保 miair 可导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import aiohttp
from Crypto.Cipher import AES

from miair.airplay.audio_stream import AudioStreamServer
from miair.airplay.server import JitterBuffer, decrypt_packet

_failures = 0


def check(name: str, ok: bool) -> None:
    global _failures
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
    if not ok:
        _failures += 1


def test_jitter_buffer_order() -> None:
    jb = JitterBuffer()
    for seq in (100, 102, 101, 103):
        jb.insert(seq, bytes([seq]))
    check("乱序包按 seq 重排", jb.drain(100) == [bytes([100]), bytes([101]),
                                                bytes([102]), bytes([103])])
    check("取空后长度为 0", len(jb) == 0)


def test_jitter_buffer_gap() -> None:
    jb = JitterBuffer()
    for seq in (10, 11, 14, 15):
        jb.insert(seq, b"x")
    check("遇到缺口停止 drain", jb.drain(10) == [b"x", b"x"])
    check("缺口检测", jb.gap_missing(12) == [12, 13])
    check("缺口后最近的包", jb.next_available_after(12) == 14)


def test_jitter_buffer_wrap() -> None:
    jb = JitterBuffer()
    for seq in (65534, 65535, 0, 1):
        jb.insert(seq, b"w")
    check("seq 回绕时仍连续", len(jb.drain(65534)) == 4)


def test_jitter_buffer_capacity() -> None:
    jb = JitterBuffer(max_size=4)
    for seq in range(6):
        jb.insert(seq, bytes([seq]))
    check("超容量时淘汰最旧的包",
          jb.drain(0) == [] and jb.drain(2) == [bytes([2]), bytes([3]),
                                                bytes([4]), bytes([5])])


def test_decrypt_packet() -> None:
    key = bytes(range(16))
    iv = bytes([0x5A]) * 16
    plain = bytes(range(64))
    ciphertext = AES.new(key, AES.MODE_CBC, iv).encrypt(plain)

    check("整块数据可解回原文", decrypt_packet(key, iv, ciphertext) == plain)

    padded = ciphertext + b"\xde\xad"          # 非 16 字节对齐的尾巴原样保留
    check("尾部不足一块时原样拼接", decrypt_packet(key, iv, padded) == plain + b"\xde\xad")

    # 同一个包解密两次必须得到相同结果：pycryptodome 的 CBC 对象是有状态的，
    # 复用 cipher 对象会让第二包开始全部解错。
    check("每包独立起链（复用会解错）",
          decrypt_packet(key, iv, ciphertext) == decrypt_packet(key, iv, ciphertext) == plain)


def test_stream_server_loopback() -> None:
    """端到端冒烟：起服务 → 持续写 PCM → 客户端拉流

    守住「返回 200 却没有音频数据」这类回归（例如处理函数里抛 NameError，
    响应已 prepare 但流随即中断，音箱会直接报播放失败）。
    """
    async def run() -> tuple[int, bytes, str]:
        srv = AudioStreamServer("127.0.0.1", 0)
        await srv.start()
        srv.set_audio_params(44100, 2, 2)
        srv.start_streaming()
        stop = threading.Event()

        def produce() -> None:
            frame = bytes([0x11, 0x22] * 704)   # 1408 字节非静音 PCM
            while not stop.is_set():
                srv.write_pcm(frame)
                time.sleep(0.008)

        threading.Thread(target=produce, daemon=True).start()
        await asyncio.sleep(0.1)
        try:
            async with aiohttp.ClientSession() as sess:
                async with sess.get(srv.stream_url) as resp:
                    data = b""
                    async for chunk in resp.content.iter_any():
                        data += chunk
                        if len(data) > 64 * 1024:
                            break
                    return resp.status, data, srv.stats_line()
        finally:
            stop.set()
            await srv.stop()

    status, data, stats = asyncio.run(asyncio.wait_for(run(), timeout=20))
    check("WAV 流返回 200", status == 200)
    check("WAV 头正确", data[:4] == b"RIFF" and data[8:12] == b"WAVE")
    check("有 PCM 数据流出", len(data) > 44 + 1408, )
    check(f"PCM 非静音（{len(data)} 字节，{stats}）", any(data[44:]))


def main() -> int:
    test_jitter_buffer_order()
    test_jitter_buffer_gap()
    test_jitter_buffer_wrap()
    test_jitter_buffer_capacity()
    test_decrypt_packet()
    test_stream_server_loopback()
    print(f"\n{'全部通过' if _failures == 0 else f'{_failures} 项失败'}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
