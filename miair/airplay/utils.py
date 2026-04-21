import re
import socket
import logging
import logging.config
import platform
import subprocess


# AirPlay 子系统日志配置
# 所有日志统一输出到控制台（由主应用 miair logger 管理文件日志），
# 不再生成独立的 events.log / audio.debug.log / control.log 文件。
logging.config.dictConfig({
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'standard': {
            'format': '[%(name)s]: %(message)s'
        },
    },
    'handlers': {
        'console': {
            'level': 'NOTSET',
            'class': 'logging.StreamHandler',
            'formatter': 'standard',
        },
    },
    'loggers': {
        'ap2.playfair': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'Audio.debug': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'Audio': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'AudioBuffered': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'AudioRealtime': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'Control*': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'events': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'HAP': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'Receiver': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'Receiver*': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        'RTPBuffer': {
            'handlers': ['console'],
            'level': 'DEBUG',
            'propagate': False,
        },
        '': {
            'handlers': ['console'],
            'level': 'NOTSET',
            'propagate': False
        },
    }
})


if platform.system() == "Windows":
    try:
        from pycaw.pycaw import AudioUtilities, ISimpleAudioVolume
    except ImportError:
        AudioUtilities = None
        ISimpleAudioVolume = None
        print('[!] Pycaw is not installed - volume control will be unavailable', )


def get_file_logger(name, level="INFO"):
    """获取日志记录器（仅控制台输出，不生成独立日志文件）"""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    return logger


def get_screen_logger(name, level="INFO"):
    logger = logging.getLogger(name)
    logger.setLevel(level)
    # logger.propagate = False
    if level == 'DEBUG':
        print(f'[{name}] logging level: {level}')
    return logger


def get_free_port():
    free_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    free_socket.bind(('0.0.0.0', 0))
    free_socket.listen(5)
    port = free_socket.getsockname()[1]
    free_socket.close()
    return port


def get_free_socket(addr=None, tcp=False):
    v4 = True
    stype = socket.SOCK_STREAM if tcp else socket.SOCK_DGRAM
    free_socket = None

    if addr:
        if len(addr.split(".")) == 4:
            free_socket = socket.socket(socket.AF_INET, stype)
        else:
            free_socket = socket.socket(socket.AF_INET6, stype)
            v4 = False
        free_socket.bind((addr, 0))
    else:
        if v4:
            free_socket = socket.socket(socket.AF_INET, stype)
            free_socket.bind(('0.0.0.0', 0))
        else:
            free_socket = socket.socket(socket.AF_INET6, stype)
            free_socket.bind(('::', 0))
    if tcp:
        free_socket.listen(5)
    return free_socket


def interpolate(value, from_min, from_max, to_min, to_max):
    from_span = from_max - from_min
    to_span = to_max - to_min

    value_scale = float(value - from_min) / float(from_span)

    return to_min + (value_scale * to_span)


audio_pid = 0


def set_volume_pid(pid):
    global audio_pid
    audio_pid = pid


def get_pycaw_volume_session():
    if platform.system() != 'Windows' or AudioUtilities is None:
        return
    session = None
    for s in AudioUtilities.GetAllSessions():
        try:
            if s.Process.pid == audio_pid:
                session = s._ctl.QueryInterface(ISimpleAudioVolume)
                break
        except AttributeError:
            pass
    return session


def get_volume():
    subsys = platform.system()
    if subsys == "Darwin":
        resp = subprocess.check_output(["osascript", "-e", "output volume of (get volume settings)"]).rstrip()
        if resp == b'missing value':
            pct = 25
        else:
            try:
                pct = int(resp)
            except ValueError:
                pct = 0
        vol = interpolate(pct, 0, 100, -30, 0)
    elif subsys == "Linux":
        line_pct = subprocess.check_output(["amixer", "get", "Master"]).splitlines()[-1]
        m = re.search(b"\[([0-9]+)%\]", line_pct)
        if m:
            pct = int(m.group(1))
            if pct < 45:
                pct = 45
        else:
            pct = 50
        vol = interpolate(pct, 45, 100, -30, 0)
    elif subsys == "Windows":
        volume_session = get_pycaw_volume_session()
        if not volume_session:
            vol = -15
        else:
            vol = interpolate(volume_session.GetMasterVolume(), 0, 1, -30, 0)
    else:
        # This system is not supported, whatever it is.
        vol = 50
    if vol == -30:
        return -144
    return vol


def set_volume(vol):
    if vol == -144:
        vol = -30

    subsys = platform.system()
    if subsys == "Darwin":
        pct = int(interpolate(vol, -30, 0, 0, 100))
        subprocess.run(["osascript", "-e", f"set volume output volume {pct}"])
    elif subsys == "Linux":
        pct = int(interpolate(vol, -30, 0, 45, 100))

        subprocess.run(["amixer", "set", "Master", f"{pct}%"])
    elif subsys == "Windows":
        volume_session = get_pycaw_volume_session()
        if volume_session:
            pct = interpolate(vol, -30, 0, 0, 1)
            volume_session.SetMasterVolume(pct, None)
