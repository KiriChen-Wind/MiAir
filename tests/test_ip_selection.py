"""测试局域网 IP 自动选择和 AirPlay 地址一致性。"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from miair.config import Config


def test_detect_local_ip_prefers_lan_over_default_route():
    candidates = [
        ("100.64.1.20", "default-route"),
        ("172.17.0.1", "docker0"),
        ("192.168.31.65", "Wi-Fi"),
        ("127.0.0.1", "loopback"),
    ]

    with patch.object(Config, "_collect_local_ipv4_candidates", return_value=candidates), \
            patch.object(Config, "_detect_default_route_ip", return_value="100.64.1.20"):
        assert Config._detect_local_ip() == "192.168.31.65"


def test_env_hostname_overrides_saved_hostname():
    old = os.environ.get("MIAIR_HOSTNAME")
    os.environ["MIAIR_HOSTNAME"] = "192.168.31.1"
    try:
        config = Config(hostname="100.64.1.20")
        assert config.hostname == "192.168.31.1"
    finally:
        if old is None:
            os.environ.pop("MIAIR_HOSTNAME", None)
        else:
            os.environ["MIAIR_HOSTNAME"] = old


def test_airplay_server_uses_constructor_hostname():
    with patch.dict(os.environ, {"MIAIR_HOSTNAME": "100.64.1.20"}):
        try:
            from miair.airplay.server import AirPlayServer
        except ModuleNotFoundError as exc:
            print(f"[SKIP] AirPlayServer dependency missing: {exc.name}")
            return

        server = AirPlayServer("192.168.31.65")
        assert server.ipv4 == "192.168.31.65"


def test_mdns_server_name_does_not_use_ip_hostname():
    try:
        from miair.airplay.mdns import AirPlayMDNS
    except ModuleNotFoundError as exc:
        print(f"[SKIP] AirPlayMDNS dependency missing: {exc.name}")
        return

    mdns = AirPlayMDNS("192.168.31.65", "客厅音箱", "AA:BB:CC:DD:EE:FF", 7000)
    assert mdns._get_ip() == "192.168.31.65"
    assert mdns._get_server_name() == "miair-speaker-aabbccddeeff.local."


def test_ssdp_uses_hostname_as_multicast_interface():
    from miair.dlna.ssdp import SSDPServer

    assert SSDPServer("192.168.31.65", 8200)._get_multicast_interface() == "192.168.31.65"
    assert SSDPServer("127.0.0.1", 8200)._get_multicast_interface() is None


def main():
    test_detect_local_ip_prefers_lan_over_default_route()
    test_env_hostname_overrides_saved_hostname()
    test_airplay_server_uses_constructor_hostname()
    test_mdns_server_name_does_not_use_ip_hostname()
    test_ssdp_uses_hostname_as_multicast_interface()
    print("[PASS] IP selection tests")


if __name__ == "__main__":
    main()
