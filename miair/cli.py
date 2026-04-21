"""MiAir CLI 入口"""

import argparse
import asyncio
import signal
import sys

from miair.config import Config

# 强制 Windows 控制台使用 UTF-8 编码，防止中文和特殊字符乱码
if sys.platform == "win32":
    try:
        # Python 3.7+
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')
        else:
            # 较旧版本
            import io
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
    except Exception:
        pass


def parse_args():
    parser = argparse.ArgumentParser(description="MiAir - 让小爱音箱成为 DLNA 渲染器")
    parser.add_argument("--conf-path", default="conf", help="配置文件目录 (默认: conf)")
    parser.add_argument("--hostname", default="", help="本机 IP 地址 (留空自动检测)")
    parser.add_argument("--dlna-port", type=int, default=0, help="DLNA HTTP 端口 (默认: 8200)")
    parser.add_argument("--web-port", type=int, default=0, help="Web 管理端口 (默认: 8300)")
    parser.add_argument("--verbose", action="store_true", help="调试日志")
    parser.add_argument("--account", default="", help="小米账号")
    parser.add_argument("--password", default="", help="小米密码")
    parser.add_argument("--mi-did", default="", help="设备 DID (逗号分隔)")
    return parser.parse_args()


def main():
    args = parse_args()

    # 加载配置
    config = Config.load(args.conf_path)

    # CLI 参数覆盖配置文件
    if args.hostname:
        config.hostname = args.hostname
    if args.dlna_port:
        config.dlna_port = args.dlna_port
    if args.web_port:
        config.web_port = args.web_port
    if args.verbose:
        config.verbose = True
    if args.account:
        config.account = args.account
    if args.password:
        config.password = args.password
    if args.mi_did:
        config.mi_did = args.mi_did

    # 启动 (即使没有配置账号/设备也可以启动，用户通过 Web 界面配置)
    from miair.app import MiAir

    app = MiAir(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    main_task = loop.create_task(app.run_forever())

    # 信号处理
    def _shutdown():
        main_task.cancel()

    if sys.platform != "win32":
        loop.add_signal_handler(signal.SIGINT, _shutdown)
        loop.add_signal_handler(signal.SIGTERM, _shutdown)
    else:
        # Windows 不支持 add_signal_handler，使用键盘中断兜底
        import ctypes
        kernel32 = ctypes.WinDLL('kernel32')
        kernel32.SetConsoleCtrlHandler(None, False)  # 恢复默认 Ctrl+C 行为

    try:
        loop.run_until_complete(main_task)
    except (KeyboardInterrupt, asyncio.CancelledError):
        # 捕获取消或中断，开始优雅关闭
        pass
    finally:
        # 优雅停止所有服务
        if not loop.is_closed():
            try:
                # 给一定的超时时间进行关闭
                loop.run_until_complete(asyncio.wait_for(app.stop(), timeout=5.0))
            except Exception as e:
                # 打印到 stderr 而不是 logger，因为 logger 处理器可能已关闭
                sys.stderr.write(f"Shutdown error: {e}\n")
            finally:
                # 停止所有正在运行的任务
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    try:
                        loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                    except Exception:
                        pass
                loop.close()


if __name__ == "__main__":
    main()
