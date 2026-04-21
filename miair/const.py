"""MiAir 常量定义"""

# SSDP 相关
SSDP_ADDR = "239.255.255.250"
SSDP_PORT = 1900
SSDP_MX = 3
SSDP_ALIVE_INTERVAL = 30  # 秒

# UPnP URN - 声明为音频渲染器（音箱）
DEVICE_TYPE = "urn:schemas-upnp-org:device:MediaRenderer:1"
AVTRANSPORT_URN = "urn:schemas-upnp-org:service:AVTransport:1"
RENDERING_CONTROL_URN = "urn:schemas-upnp-org:service:RenderingControl:1"
CONNECTION_MANAGER_URN = "urn:schemas-upnp-org:service:ConnectionManager:1"

# Transport States
TRANSPORT_STATE_NO_MEDIA = "NO_MEDIA_PRESENT"
TRANSPORT_STATE_STOPPED = "STOPPED"
TRANSPORT_STATE_PLAYING = "PLAYING"
TRANSPORT_STATE_PAUSED = "PAUSED_PLAYBACK"
TRANSPORT_STATE_TRANSITIONING = "TRANSITIONING"

# Transport Status
TRANSPORT_STATUS_OK = "OK"
TRANSPORT_STATUS_ERROR = "ERROR_OCCURRED"

# Play Mode
PLAY_MODE_NORMAL = "NORMAL"

# UPnP Error Codes
UPNP_ERROR_INVALID_ACTION = 401
UPNP_ERROR_INVALID_ARGS = 402
UPNP_ERROR_ACTION_FAILED = 501
UPNP_ERROR_TRANSITION_NOT_AVAILABLE = 701
UPNP_ERROR_SEEK_MODE_NOT_SUPPORTED = 710

# 需要使用 play_by_music_url 接口的设备型号
NEED_USE_PLAY_MUSIC_API = [
    "X08C",
    "X08E",
    "X8F",
    "X4B",
    "LX05",
    "OH2",
    "OH2P",
    "X6A",
]

# 默认 audio_id (用于 play_by_music_url)
DEFAULT_AUDIO_ID = "1582971365183456177"

# 支持的协议信息 (ConnectionManager GetProtocolInfo) - 仅音频
SUPPORTED_PROTOCOLS = (
    "http-get:*:audio/mpeg:*,"
    "http-get:*:audio/mp3:*,"
    "http-get:*:audio/mp4:*,"
    "http-get:*:audio/ogg:*,"
    "http-get:*:audio/flac:*,"
    "http-get:*:audio/x-flac:*,"
    "http-get:*:audio/wav:*,"
    "http-get:*:audio/x-wav:*,"
    "http-get:*:audio/aac:*,"
    "http-get:*:audio/x-aac:*,"
    "http-get:*:audio/x-m4a:*,"
    "http-get:*:audio/x-ms-wma:*,"
    "http-get:*:audio/L16:*,"
    "http-get:*:audio/vnd.dlna.adts:*,"
    "http-get:*:audio/ape:*,"
    "http-get:*:audio/*:*"
)

# 小米对话历史 API (用于语音控制)
LATEST_ASK_API = (
    "https://userprofile.mina.mi.com/device_profile/v2/conversation"
    "?source=dialogu&hardware={hardware}&timestamp={timestamp}&limit=2"
)

# 需要通过 Mina 服务获取对话记录的硬件型号
GET_ASK_BY_MINA = {
    "LX04", "L05B", "L05C", "S12", "S12A",
    "LX5A", "L15A", "L16A", "X6A",
}


