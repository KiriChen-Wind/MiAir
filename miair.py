#!/usr/bin/env python3
"""MiAir 入口点 — 自动检测并安装缺失依赖后启动"""

import subprocess
import sys

REQUIRED_PACKAGES = {
    # import 名 -> pip 包名
    "aiohttp": "aiohttp>=3.9.0",
    "miservice": "miservice-fork",
    "zeroconf": "zeroconf>=0.38.0",
    "Crypto": "pycryptodome>=3.15.0",
    "av": "av>=10.0.0",
}


def ensure_dependencies():
    """检测缺失的依赖并一次性安装"""
    missing = []
    for import_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            __import__(import_name)
        except ImportError:
            missing.append(pip_name)
    if missing:
        print(f"[MiAir] 正在安装缺失依赖: {', '.join(missing)}")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", *missing],
        )
        print("[MiAir] 依赖安装完成")


if __name__ == "__main__":
    ensure_dependencies()
    from miair.cli import main
    main()
