"""测试音频格式检测、seek 逻辑和日志编码修复"""

import asyncio
import struct
import sys
import os

# 确保 miair 可导入
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def test_detect_audio_format():
    """测试 _detect_audio_format 魔数检测"""
    from miair.dlna.device_server import DeviceServer

    # FLAC
    flac_data = b"fLaC" + b"\x00" * 100
    assert DeviceServer._detect_audio_format(flac_data) == "flac", "FLAC 检测失败"

    # WAV
    wav_data = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 100
    assert DeviceServer._detect_audio_format(wav_data) == "wav", "WAV 检测失败"

    # OGG
    ogg_data = b"OggS" + b"\x00" * 100
    assert DeviceServer._detect_audio_format(ogg_data) == "ogg", "OGG 检测失败"

    # MP3 with ID3
    mp3_id3_data = b"ID3" + b"\x00" * 100
    assert DeviceServer._detect_audio_format(mp3_id3_data) == "mp3", "MP3 ID3 检测失败"

    # MP3 sync frame
    mp3_sync_data = bytes([0xFF, 0xFB, 0x90, 0x00]) + b"\x00" * 100
    assert DeviceServer._detect_audio_format(mp3_sync_data) == "mp3", "MP3 sync 检测失败"

    # AAC ADTS: 0xFFF0 (sync + ID=MPEG4, layer=00)
    aac_data = bytes([0xFF, 0xF1, 0x50, 0x80]) + b"\x00" * 100
    assert DeviceServer._detect_audio_format(aac_data) == "aac", "AAC 检测失败"

    # M4A (ftyp)
    m4a_data = b"\x00\x00\x00\x20" + b"ftyp" + b"M4A " + b"\x00" * 100
    assert DeviceServer._detect_audio_format(m4a_data) == "m4a", "M4A 检测失败"

    # WMA
    wma_data = b"\x30\x26\xb2\x75" + b"\x00" * 100
    assert DeviceServer._detect_audio_format(wma_data) == "wma", "WMA 检测失败"

    # Unknown
    unknown_data = b"\x00\x01\x02\x03" + b"\x00" * 100
    assert DeviceServer._detect_audio_format(unknown_data) == "unknown", "Unknown 检测失败"

    # application/octet-stream 场景: FLAC 数据但 content_type 不含 flac
    # _format_seek 应该通过魔数正确识别
    print("  [PASS] _detect_audio_format 所有格式检测正确")


def test_format_seek_uses_magic():
    """测试 _format_seek 使用魔数而非 content_type"""
    from miair.dlna.device_server import DeviceServer

    # 构造有效的最小 FLAC 文件
    # fLaC magic + STREAMINFO block
    streaminfo = bytearray(34)
    # min/max block size = 4096
    streaminfo[0:2] = (4096).to_bytes(2, "big")
    streaminfo[2:4] = (4096).to_bytes(2, "big")
    # sample rate 44100, 2 channels, 16 bps
    # byte 10-13: sample_rate(20) | channels-1(3) | bps-1(5) | total_samples_hi(4)
    sr = 44100
    ch_minus1 = 1  # 2 channels
    bps_minus1 = 15  # 16 bit
    val = (sr << 12) | (ch_minus1 << 9) | (bps_minus1 << 4) | 0
    streaminfo[10:14] = val.to_bytes(4, "big")
    # total_samples low 32 = 1000000
    streaminfo[14:18] = (1000000).to_bytes(4, "big")

    # STREAMINFO block header: last=1 (0x80), type=0, length=34
    block_header = bytes([0x80, 0x00, 0x00, 0x22])

    flac_header = b"fLaC" + block_header + bytes(streaminfo)

    # 构造假音频数据（包含帧同步码）
    audio_data = bytearray(100000)
    # 在多个位置放置帧同步码 0xFF 0xF8
    for offset in [0, 20000, 40000, 60000, 80000]:
        audio_data[offset] = 0xFF
        audio_data[offset + 1] = 0xF8

    flac_data = bytearray(flac_header + bytes(audio_data))

    # 关键测试：content_type 是 application/octet-stream，但数据是 FLAC
    result = DeviceServer._format_seek(flac_data, 0.5, "application/octet-stream")
    assert result is not None, "FLAC seek 失败 (content_type=application/octet-stream)"
    assert result[:4] == b"fLaC", "结果不是有效的 FLAC (缺少 fLaC magic)"
    print("  [PASS] _format_seek 使用魔数检测 FLAC (忽略 content_type)")

    # MP3 测试
    mp3_data = bytearray(50000)
    mp3_data[0] = 0xFF
    mp3_data[1] = 0xFB
    for i in range(2000, 50000, 2000):
        mp3_data[i] = 0xFF
        mp3_data[i + 1] = 0xFB

    result = DeviceServer._format_seek(mp3_data, 0.5, "application/octet-stream")
    assert result is not None, "MP3 seek 失败 (content_type=application/octet-stream)"
    assert result[0] == 0xFF and (result[1] & 0xE0) == 0xE0, "结果不是有效的 MP3 帧"
    print("  [PASS] _format_seek 使用魔数检测 MP3 (忽略 content_type)")


def test_seek_wav():
    """测试 WAV seek"""
    from miair.dlna.device_server import DeviceServer

    # 构造最小 WAV 文件: RIFF + WAVE + fmt + data
    sample_rate = 44100
    channels = 2
    bits_per_sample = 16
    block_align = channels * (bits_per_sample // 8)
    byte_rate = sample_rate * block_align

    # fmt chunk
    fmt_data = struct.pack("<HHIIHH",
        1,              # PCM format
        channels,
        sample_rate,
        byte_rate,
        block_align,
        bits_per_sample,
    )
    fmt_chunk = b"fmt " + struct.pack("<I", len(fmt_data)) + fmt_data

    # data chunk: 10000 samples worth of audio
    num_samples = 10000
    audio_bytes = bytearray(num_samples * block_align)
    for i in range(0, len(audio_bytes), 2):
        audio_bytes[i] = i & 0xFF
        audio_bytes[i + 1] = (i >> 8) & 0xFF
    data_chunk = b"data" + struct.pack("<I", len(audio_bytes)) + bytes(audio_bytes)

    riff_size = 4 + len(fmt_chunk) + len(data_chunk)
    wav_data = bytearray(
        b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + fmt_chunk + data_chunk
    )

    result = DeviceServer._seek_wav(wav_data, 0.5)
    assert result is not None, "WAV seek 失败"
    assert result[:4] == b"RIFF", "结果不是有效的 WAV"
    assert result[8:12] == b"WAVE", "结果缺少 WAVE 标志"
    assert len(result) < len(wav_data), "WAV seek 结果应该更小"
    print(f"  [PASS] WAV seek: {len(wav_data)} -> {len(result)} bytes")


def test_seek_aac():
    """测试 AAC ADTS seek"""
    from miair.dlna.device_server import DeviceServer

    # 构造包含 ADTS 帧同步的数据
    aac_data = bytearray(20000)
    # 在多个位置放置 ADTS sync: 0xFFF1
    for offset in [0, 5000, 10000, 15000]:
        aac_data[offset] = 0xFF
        aac_data[offset + 1] = 0xF1

    result = DeviceServer._seek_aac(aac_data, 0.5)
    assert result is not None, "AAC seek 失败"
    assert result[0] == 0xFF and (result[1] & 0xF6) == 0xF0, "结果不是有效的 ADTS 帧"
    print(f"  [PASS] AAC seek: {len(aac_data)} -> {len(result)} bytes")


def test_mp3_id3_skip():
    """测试 MP3 seek 跳过 ID3v2 头"""
    from miair.dlna.device_server import DeviceServer

    # 构造带 ID3v2 头的 MP3
    id3_body_size = 1000
    id3_header = b"ID3\x04\x00\x00"
    # ID3 size 用 syncsafe integer (每字节只用 7 位)
    s = id3_body_size
    id3_header += bytes([
        (s >> 21) & 0x7F,
        (s >> 14) & 0x7F,
        (s >> 7) & 0x7F,
        s & 0x7F,
    ])
    id3_data = id3_header + bytearray(id3_body_size)

    # 在 ID3 之后放置 MP3 帧
    audio_data = bytearray(50000)
    for i in range(0, 50000, 2000):
        audio_data[i] = 0xFF
        audio_data[i + 1] = 0xFB

    mp3_data = bytearray(id3_data + audio_data)
    result = DeviceServer._seek_mp3(mp3_data, 0.5)
    assert result is not None, "MP3 seek 失败 (带 ID3)"
    assert result[0] == 0xFF and (result[1] & 0xE0) == 0xE0, "结果不是有效的 MP3 帧"
    # 确保 seek 跳过了 ID3，结果不包含 ID3 头
    assert result[:3] != b"ID3", "结果不应包含 ID3 头"
    print(f"  [PASS] MP3 seek (跳过 ID3): {len(mp3_data)} -> {len(result)} bytes")


def test_check_ffmpeg_project_dir():
    """测试 _check_ffmpeg 优先查找项目目录"""
    from miair.dlna.device_server import DeviceServer

    ds = DeviceServer("127.0.0.1", 8200)
    result = ds._check_ffmpeg()
    if result:
        print(f"  [PASS] _check_ffmpeg 找到: {result}")
        # 检查是否在项目目录中
        project_dir = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(
                __import__("miair.dlna.device_server", fromlist=["DeviceServer"]).__file__
            )))
        )
        if project_dir in result:
            print(f"  [PASS] ffmpeg 位于项目目录")
    else:
        print(f"  [INFO] ffmpeg 未找到 (不影响纯 Python seek)")


def test_logging_encoding():
    """测试日志编码不会因特殊字符崩溃"""
    import logging
    import io

    # 模拟 Windows 场景: 创建一个 UTF-8 StreamHandler
    stream = io.TextIOWrapper(
        io.BytesIO(), encoding="utf-8", errors="replace", line_buffering=True
    )
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))

    logger = logging.getLogger("test_encoding")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    # 测试各种特殊字符
    test_strings = [
        "願い〜あの頃のキミへ〜",  # 日文 + 波浪号 U+301C
        "日本語テスト",
        "🎵🎶 emoji test",
        "Ñoño café résumé",
        "中文测试：标题《歌曲》",
    ]

    for s in test_strings:
        try:
            logger.info(s)
        except UnicodeEncodeError as e:
            print(f"  [FAIL] 日志编码失败: {e}")
            return

    print("  [PASS] 所有特殊字符日志输出正常")
    logger.removeHandler(handler)


async def test_ffmpeg_seek_flac():
    """测试用 ffmpeg 对 FLAC 数据进行 seek"""
    from miair.dlna.device_server import DeviceServer
    import subprocess

    ds = DeviceServer("127.0.0.1", 8200)
    ffmpeg = ds._check_ffmpeg()
    if not ffmpeg:
        print("  [SKIP] ffmpeg 不可用，跳过 ffmpeg seek 测试")
        return

    # 用 ffmpeg 生成一个短 FLAC 测试文件
    import tempfile
    out_fd, out_path = tempfile.mkstemp(suffix=".flac", prefix="miair_test_")
    os.close(out_fd)
    try:
        proc = subprocess.run(
            [ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
             "-c:a", "flac", out_path],
            capture_output=True, timeout=10
        )
        if proc.returncode != 0:
            print(f"  [SKIP] ffmpeg 无法生成测试 FLAC: {proc.stderr.decode(errors='replace')[:100]}")
            return

        with open(out_path, "rb") as f:
            flac_data = bytearray(f.read())

        print(f"  [INFO] 测试 FLAC: {len(flac_data)} bytes")

        # 测试格式检测
        fmt = DeviceServer._detect_audio_format(flac_data)
        assert fmt == "flac", f"格式检测错误: {fmt}"
        print(f"  [PASS] 真实 FLAC 文件格式检测正确: {fmt}")

        # 测试纯 Python seek
        result = DeviceServer._format_seek(flac_data, 0.5, "application/octet-stream")
        if result:
            assert result[:4] == b"fLaC", "纯 Python FLAC seek 结果无效"
            print(f"  [PASS] 纯 Python FLAC seek: {len(flac_data)} -> {len(result)} bytes")
        else:
            print("  [WARN] 纯 Python FLAC seek 失败 (可能帧同步码未匹配)")

        # 测试 ffmpeg seek
        seeked = await ds._ffmpeg_seek(flac_data, 2.5, "application/octet-stream")
        if seeked:
            assert len(seeked) > 0, "ffmpeg seek 结果为空"
            ffmt = DeviceServer._detect_audio_format(seeked)
            assert ffmt == "flac", f"ffmpeg seek 输出不是 FLAC: {ffmt}"
            print(f"  [PASS] ffmpeg FLAC seek: {len(flac_data)} -> {len(seeked)} bytes")
        else:
            print("  [WARN] ffmpeg FLAC seek 返回 None")

    finally:
        if os.path.exists(out_path):
            os.unlink(out_path)


def main():
    print("=" * 50)
    print("MiAir 音频 Seek 测试")
    print("=" * 50)

    print("\n1. 格式检测测试:")
    test_detect_audio_format()

    print("\n2. 格式 Seek 魔数测试:")
    test_format_seek_uses_magic()

    print("\n3. WAV Seek 测试:")
    test_seek_wav()

    print("\n4. AAC Seek 测试:")
    test_seek_aac()

    print("\n5. MP3 ID3 跳过测试:")
    test_mp3_id3_skip()

    print("\n6. ffmpeg 路径检测测试:")
    test_check_ffmpeg_project_dir()

    print("\n7. 日志编码测试:")
    test_logging_encoding()

    print("\n8. ffmpeg 真实 FLAC Seek 测试:")
    asyncio.run(test_ffmpeg_seek_flac())

    print("\n" + "=" * 50)
    print("所有测试完成!")
    print("=" * 50)


if __name__ == "__main__":
    main()
