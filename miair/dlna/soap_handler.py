"""SOAP 动作解析与分发"""

import logging
import xml.etree.ElementTree as ET

from miair.const import (
    AVTRANSPORT_URN,
    CONNECTION_MANAGER_URN,
    RENDERING_CONTROL_URN,
    SUPPORTED_PROTOCOLS,
    UPNP_ERROR_ACTION_FAILED,
    UPNP_ERROR_INVALID_ACTION,
    UPNP_ERROR_SEEK_MODE_NOT_SUPPORTED,
)
from miair.dlna.renderer import DLNARenderer
from miair.dlna.templates import soap_fault, soap_response

log = logging.getLogger("miair")

# SOAP 命名空间
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"


def parse_soap_action(soap_action_header: str) -> tuple[str, str]:
    """从 SOAPAction header 解析服务 URN 和动作名

    Args:
        soap_action_header: e.g. '"urn:schemas-upnp-org:service:AVTransport:1#Play"'

    Returns:
        (service_urn, action_name)
    """
    # 移除引号
    header = soap_action_header.strip('"')
    if "#" in header:
        service_urn, action = header.split("#", 1)
        return service_urn, action
    return "", header


def parse_soap_body(body: str) -> dict[str, str]:
    """解析 SOAP Body 中的参数

    Returns:
        dict: 参数名 -> 参数值
    """
    params = {}
    try:
        root = ET.fromstring(body)
        # 找到 Body 下的 action 元素
        body_elem = root.find(f".//{{{SOAP_NS}}}Body")
        if body_elem is None:
            return params

        # action 元素是 Body 的第一个子元素
        action_elem = list(body_elem)[0] if len(body_elem) > 0 else None
        if action_elem is None:
            return params

        # 提取所有参数
        for child in action_elem:
            # 移除命名空间前缀
            tag = child.tag
            if "}" in tag:
                tag = tag.split("}", 1)[1]
            params[tag] = child.text or ""
    except ET.ParseError as e:
        log.warning(f"SOAP XML 解析错误: {e}")
    return params


async def handle_soap_request(
    renderer: DLNARenderer,
    service_urn: str,
    action: str,
    params: dict[str, str],
) -> tuple[str, int]:
    """处理 SOAP 请求并返回响应

    Returns:
        (response_xml, http_status_code)
    """
    log.info(f"[{renderer.friendly_name}] SOAP: {action} params={params}")

    if service_urn == AVTRANSPORT_URN:
        return await _handle_avtransport(renderer, action, params)
    elif service_urn == RENDERING_CONTROL_URN:
        return await _handle_rendering_control(renderer, action, params)
    elif service_urn == CONNECTION_MANAGER_URN:
        return _handle_connection_manager(action, params)
    else:
        return soap_fault(UPNP_ERROR_INVALID_ACTION, "Invalid Service"), 500


async def _handle_avtransport(
    renderer: DLNARenderer, action: str, params: dict
) -> tuple[str, int]:
    """处理 AVTransport 动作"""

    if action == "SetAVTransportURI":
        uri = params.get("CurrentURI", "")
        metadata = params.get("CurrentURIMetaData", "")
        success = await renderer.set_av_transport_uri(uri, metadata)
        if success:
            return soap_response(AVTRANSPORT_URN, action, {}), 200
        return soap_fault(UPNP_ERROR_ACTION_FAILED, "Unsupported media type (video files not supported)"), 715

    elif action == "Play":
        success = await renderer.play()
        if success:
            return soap_response(AVTRANSPORT_URN, action, {}), 200
        return soap_fault(UPNP_ERROR_ACTION_FAILED, "Play failed"), 500

    elif action == "Pause":
        await renderer.pause()
        return soap_response(AVTRANSPORT_URN, action, {}), 200

    elif action == "Stop":
        await renderer.stop()
        return soap_response(AVTRANSPORT_URN, action, {}), 200

    elif action == "Seek":
        unit = params.get("Unit", "REL_TIME")
        target = params.get("Target", "00:00:00")
        success = await renderer.seek(unit, target)
        if success:
            return soap_response(AVTRANSPORT_URN, action, {}), 200
        return soap_fault(UPNP_ERROR_SEEK_MODE_NOT_SUPPORTED, "Seek mode not supported"), 500

    elif action == "Next":
        await renderer.next_track()
        return soap_response(AVTRANSPORT_URN, action, {}), 200

    elif action == "Previous":
        await renderer.previous_track()
        return soap_response(AVTRANSPORT_URN, action, {}), 200

    elif action == "SetNextAVTransportURI":
        uri = params.get("NextURI", "")
        metadata = params.get("NextURIMetaData", "")
        await renderer.set_next_av_transport_uri(uri, metadata)
        return soap_response(AVTRANSPORT_URN, action, {}), 200

    elif action == "GetCurrentTransportActions":
        actions = renderer.get_current_transport_actions()
        return soap_response(AVTRANSPORT_URN, action, {"Actions": actions}), 200

    elif action == "GetTransportInfo":
        info = renderer.get_transport_info()
        return soap_response(AVTRANSPORT_URN, action, info), 200

    elif action == "GetPositionInfo":
        info = renderer.get_position_info()
        return soap_response(AVTRANSPORT_URN, action, info), 200

    elif action == "GetMediaInfo":
        info = renderer.get_media_info()
        return soap_response(AVTRANSPORT_URN, action, info), 200

    elif action == "GetTransportSettings":
        info = renderer.get_transport_settings()
        return soap_response(AVTRANSPORT_URN, action, info), 200

    elif action == "GetDeviceCapabilities":
        return soap_response(AVTRANSPORT_URN, action, {
            "PlayMedia": "NETWORK",
            "RecMedia": "NOT_IMPLEMENTED",
            "RecQualityModes": "NOT_IMPLEMENTED",
        }), 200

    elif action == "SetPlayMode":
        return soap_response(AVTRANSPORT_URN, action, {}), 200

    else:
        log.warning(f"未实现的 AVTransport 动作: {action}")
        return soap_fault(UPNP_ERROR_INVALID_ACTION, f"Unknown action: {action}"), 500


async def _handle_rendering_control(
    renderer: DLNARenderer, action: str, params: dict
) -> tuple[str, int]:
    """处理 RenderingControl 动作"""

    if action == "GetVolume":
        volume = await renderer.get_volume()
        return soap_response(RENDERING_CONTROL_URN, action, {"CurrentVolume": str(volume)}), 200

    elif action == "SetVolume":
        volume = int(params.get("DesiredVolume", "50"))
        await renderer.set_volume(volume)
        return soap_response(RENDERING_CONTROL_URN, action, {}), 200

    elif action == "GetMute":
        mute = renderer.get_mute()
        return soap_response(RENDERING_CONTROL_URN, action, {"CurrentMute": "1" if mute else "0"}), 200

    elif action == "SetMute":
        mute = params.get("DesiredMute", "0") in ("1", "true", "True")
        await renderer.set_mute(mute)
        return soap_response(RENDERING_CONTROL_URN, action, {}), 200

    elif action == "ListPresets":
        return soap_response(RENDERING_CONTROL_URN, action, {
            "CurrentPresetNameList": "FactoryDefaults",
        }), 200

    elif action == "SelectPreset":
        return soap_response(RENDERING_CONTROL_URN, action, {}), 200

    else:
        log.warning(f"未实现的 RenderingControl 动作: {action}")
        return soap_fault(UPNP_ERROR_INVALID_ACTION, f"Unknown action: {action}"), 500


def _handle_connection_manager(action: str, params: dict) -> tuple[str, int]:
    """处理 ConnectionManager 动作"""

    if action == "GetProtocolInfo":
        return soap_response(
            CONNECTION_MANAGER_URN,
            action,
            {"Source": "", "Sink": SUPPORTED_PROTOCOLS},
        ), 200

    elif action == "GetCurrentConnectionIDs":
        return soap_response(
            CONNECTION_MANAGER_URN, action, {"ConnectionIDs": "0"}
        ), 200

    elif action == "GetCurrentConnectionInfo":
        return soap_response(
            CONNECTION_MANAGER_URN,
            action,
            {
                "RcsID": "0",
                "AVTransportID": "0",
                "ProtocolInfo": "",
                "PeerConnectionManager": "",
                "PeerConnectionID": "-1",
                "Direction": "Input",
                "Status": "OK",
            },
        ), 200

    else:
        return soap_fault(UPNP_ERROR_INVALID_ACTION, f"Unknown action: {action}"), 500
