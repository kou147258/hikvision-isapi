"""Tests for ISAPI digest-auth parsing and coordinator XML extraction.

The actual HTTP client is a thin wrapper around aiohttp; we test
the pure-Python parts (digest challenge parsing, digest response
computation, XML extraction) here. The HTTP transport itself is
covered by aiohttp's own test suite.
"""

from __future__ import annotations

import hashlib

from custom_components.hikvision_isapi.coordinator import (
    _parse_channel_status,
    _parse_channel_status_extended,
    _parse_channels,
    _parse_device_info,
    _parse_network_interfaces,
    _parse_storage,
    _parse_streaming_channels,
    _parse_system_status,
    normalize_device_type,
)
from custom_components.hikvision_isapi.isapi_client import (
    _build_digest_header,
    _compute_digest_response,
    _parse_digest_challenge,
)


# ---- digest challenge parser ----


def test_parse_digest_challenge_basic():
    challenge = (
        'Digest realm="IPCamera", nonce="abc123", '
        'qop="auth", algorithm=MD5'
    )
    out = _parse_digest_challenge(challenge)
    assert out is not None
    assert out["realm"] == "IPCamera"
    assert out["nonce"] == "abc123"
    assert out["qop"] == "auth"
    assert out["algorithm"] == "MD5"


def test_parse_digest_challenge_no_quotes():
    challenge = "Digest realm=IPCamera, nonce=abc123, algorithm=MD5"
    out = _parse_digest_challenge(challenge)
    assert out is not None
    assert out["realm"] == "IPCamera"
    assert out["nonce"] == "abc123"


def test_parse_digest_challenge_returns_none_for_non_digest():
    assert _parse_digest_challenge("Basic realm=foo") is None
    assert _parse_digest_challenge("") is None
    assert _parse_digest_challenge(None) is None


def test_parse_digest_challenge_extracts_opaque():
    challenge = (
        'Digest realm="r", nonce="n", qop="auth", algorithm=MD5, '
        'opaque="5ccc069c403ebaf9f0171e9517f40e41"'
    )
    out = _parse_digest_challenge(challenge)
    assert out is not None
    assert out["opaque"] == "5ccc069c403ebaf9f0171e9517f40e41"


# ---- digest response computation ----


def test_compute_digest_response_qop_auth():
    """RFC 2617 digest computation matches a hand-rolled reference."""
    username = "admin"
    password = "12345"
    realm = "IPCamera"
    method = "GET"
    path = "/ISAPI/System/deviceInfo"
    nonce = "abc123"
    nc_count = 1
    cnonce = "0a4f113b"

    ha1 = hashlib.md5(
        f"{username}:{realm}:{password}".encode("utf-8")
    ).hexdigest()
    ha2 = hashlib.md5(f"{method}:{path}".encode("utf-8")).hexdigest()
    expected = hashlib.md5(
        f"{ha1}:{nonce}:{nc_count:08x}:{cnonce}:auth:{ha2}".encode("utf-8")
    ).hexdigest()

    challenge = {
        "realm": realm,
        "nonce": nonce,
        "qop": "auth",
        "algorithm": "MD5",
    }
    got = _compute_digest_response(
        username, password, method, path, challenge, nc_count, cnonce
    )
    assert got == expected


def test_compute_digest_response_legacy_no_qop():
    """Legacy RFC 2069 form (no qop, no nc, no cnonce)."""
    username = "admin"
    password = "pw"
    realm = "r"
    method = "GET"
    path = "/x"
    nonce = "n"

    ha1 = hashlib.md5(
        f"{username}:{realm}:{password}".encode("utf-8")
    ).hexdigest()
    ha2 = hashlib.md5(f"{method}:{path}".encode("utf-8")).hexdigest()
    expected = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode("utf-8")).hexdigest()

    challenge = {"realm": realm, "nonce": nonce, "qop": "", "algorithm": "MD5"}
    got = _compute_digest_response(username, password, method, path, challenge, 1, "x")
    assert got == expected


# ---- digest header builder ----


def test_build_digest_header_qop_auth():
    challenge = {
        "realm": "IPCamera",
        "nonce": "abc",
        "qop": '"auth"',
        "algorithm": "MD5",
    }
    header = _build_digest_header("admin", "pw", "GET", "/x", challenge)
    assert header.startswith("Digest ")
    assert 'username="admin"' in header
    assert 'realm="IPCamera"' in header
    assert 'nonce="abc"' in header
    assert 'uri="/x"' in header
    assert "qop=auth" in header
    assert "nc=00000001" in header
    assert 'cnonce="' in header
    assert "response=" in header
    assert "algorithm=MD5" in header


def test_build_digest_header_includes_opaque():
    challenge = {
        "realm": "r",
        "nonce": "n",
        "qop": '"auth"',
        "algorithm": "MD5",
        "opaque": "deadbeef",
    }
    header = _build_digest_header("admin", "pw", "GET", "/x", challenge)
    assert 'opaque="deadbeef"' in header


def test_build_digest_header_no_qop():
    challenge = {
        "realm": "r",
        "nonce": "n",
        "qop": "",
        "algorithm": "MD5",
    }
    header = _build_digest_header("admin", "pw", "GET", "/x", challenge)
    assert "qop=" not in header
    assert "nc=" not in header
    assert "cnonce=" not in header


# ---- coordinator XML parsing ----


def test_parse_device_info_extracts_fields():
    from xml.etree import ElementTree as ET

    xml = """<DeviceInfo>
        <deviceName>Front Door</deviceName>
        <deviceID>abc123</deviceID>
        <model>DS-2CD2143G2-I</model>
        <serialNumber>SN-9876</serialNumber>
        <firmwareVersion>V5.7.10 build 240120</firmwareVersion>
        <firmwareReleasedDate>build 240120</firmwareReleasedDate>
        <deviceType>IPCamera</deviceType>
        <macAddress>00:11:22:33:44:55</macAddress>
    </DeviceInfo>"""
    root = ET.fromstring(xml)
    info = _parse_device_info(root)
    assert info["model"] == "DS-2CD2143G2-I"
    assert info["serialNumber"] == "SN-9876"
    assert info["firmwareVersion"] == "V5.7.10 build 240120"
    assert info["macAddress"] == "00:11:22:33:44:55"
    assert info["manufacturer"] == "Hikvision"


def test_parse_device_info_handles_missing_fields():
    from xml.etree import ElementTree as ET

    info = _parse_device_info(ET.fromstring("<DeviceInfo/>"))
    assert info["model"] == ""
    assert info["serialNumber"] == ""


def test_parse_system_status_extracts_fields():
    from xml.etree import ElementTree as ET

    xml = """<SystemStatus>
        <deviceStatus>OK</deviceStatus>
        <CPUUsage>27</CPUUsage>
        <memoryUsage>35</memoryUsage>
        <uptime>12345</uptime>
    </SystemStatus>"""
    root = ET.fromstring(xml)
    status = _parse_system_status(root)
    assert status["deviceStatus"] == "OK"
    assert status["cpuUtilization"] == "27"
    assert status["memoryUsage"] == "35"
    # memoryAvailable isn't in this flat XML — the parser falls back
    # to the "0" default rather than None (we want the sensor to
    # always have a numeric value).
    assert status["memoryAvailable"] == "0"
    assert status["uptime"] == "12345"


def test_parse_channels_extracts_id_name_online_recording():
    from xml.etree import ElementTree as ET

    xml = """<InputProxyChannelList>
        <InputProxyChannel>
            <id>1</id>
            <name>Front Door</name>
            <online>true</online>
            <recordStatus>recording</recordStatus>
        </InputProxyChannel>
        <InputProxyChannel>
            <id>2</id>
            <name>Side Gate</name>
            <online>false</online>
            <recordStatus>idle</recordStatus>
        </InputProxyChannel>
    </InputProxyChannelList>"""
    root = ET.fromstring(xml)
    channels = _parse_channels(root)
    assert len(channels) == 2
    assert channels[0]["id"] == "1"
    assert channels[0]["name"] == "Front Door"
    assert channels[0]["online"] is True
    assert channels[0]["recording"] is True
    assert channels[1]["online"] is False
    assert channels[1]["recording"] is False


def test_parse_channels_handles_empty_root():
    from xml.etree import ElementTree as ET

    assert _parse_channels(ET.fromstring("<InputProxyChannelList/>")) == []
    assert _parse_channels(None) == []


# ---- v0.2.0 — storage / network / streaming XML extractors ----


def test_parse_storage_extracts_capacity_and_status():
    from xml.etree import ElementTree as ET

    xml = """<Storage>
        <totalCapacity>2000000</totalCapacity>
        <usedCapacity>1234567</usedCapacity>
        <freeCapacity>765433</freeCapacity>
        <status>normal</status>
    </Storage>"""
    storage = _parse_storage(ET.fromstring(xml))
    assert storage["total_mb"] == 2000000
    assert storage["used_mb"] == 1234567
    assert storage["free_mb"] == 765433
    assert storage["status"] == "normal"


def test_parse_storage_handles_empty_root():
    storage = _parse_storage(None)
    assert storage == {
        "total_mb": None, "used_mb": None,
        "free_mb": None, "status": "unknown",
    }


def test_parse_storage_handles_unparseable_capacity():
    from xml.etree import ElementTree as ET

    storage = _parse_storage(ET.fromstring(
        '<Storage><totalCapacity>not-a-number</totalCapacity></Storage>'
    ))
    assert storage["total_mb"] is None
    assert storage["status"] == "unknown"


def test_parse_network_interfaces_extracts_ip_mask_gateway():
    from xml.etree import ElementTree as ET

    xml = """<NetworkInterfaceList>
        <NetworkInterface>
            <id>1</id>
            <interfaceName>LAN1</interfaceName>
            <IPAddress>192.168.1.10</IPAddress>
            <subnetMask>255.255.255.0</subnetMask>
            <DefaultGateway>192.168.1.1</DefaultGateway>
            <MTU>1500</MTU>
            <MACAddress>00:11:22:33:44:55</MACAddress>
        </NetworkInterface>
        <NetworkInterface>
            <id>2</id>
            <interfaceName>WIFI</interfaceName>
            <IPAddress>10.0.0.5</IPAddress>
            <subnetMask>255.255.255.0</subnetMask>
            <DefaultGateway>10.0.0.1</DefaultGateway>
            <MTU>1500</MTU>
            <MACAddress>AA:BB:CC:DD:EE:FF</MACAddress>
        </NetworkInterface>
    </NetworkInterfaceList>"""
    ifs = _parse_network_interfaces(ET.fromstring(xml))
    assert len(ifs) == 2
    assert ifs[0]["ip_address"] == "192.168.1.10"
    assert ifs[0]["subnet_mask"] == "255.255.255.0"
    assert ifs[0]["default_gateway"] == "192.168.1.1"
    assert ifs[1]["ip_address"] == "10.0.0.5"
    assert ifs[1]["name"] == "WIFI"


def test_parse_network_interfaces_handles_empty_root():
    from xml.etree import ElementTree as ET

    assert _parse_network_interfaces(ET.fromstring("<NetworkInterfaceList/>")) == []
    assert _parse_network_interfaces(None) == []


def test_parse_streaming_channels_extracts_bitrate():
    from xml.etree import ElementTree as ET

    xml = """<StreamingChannelList>
        <StreamingChannel>
            <id>1</id>
            <videoChannelId>1</videoChannelId>
            <videoAverageBitrate>2048</videoAverageBitrate>
        </StreamingChannel>
        <StreamingChannel>
            <id>2</id>
            <videoChannelId>2</videoChannelId>
            <maxBitrate>4096</maxBitrate>
        </StreamingChannel>
    </StreamingChannelList>"""
    bitrates = _parse_streaming_channels(ET.fromstring(xml))
    assert bitrates == {"1": 2048, "2": 4096}


def test_parse_streaming_channels_handles_empty_root():
    from xml.etree import ElementTree as ET

    assert _parse_streaming_channels(ET.fromstring("<StreamingChannelList/>")) == {}
    assert _parse_streaming_channels(None) == {}


def test_parse_streaming_channels_skips_entries_without_bitrate():
    from xml.etree import ElementTree as ET

    bitrates = _parse_streaming_channels(ET.fromstring(
        '<StreamingChannel><id>1</id></StreamingChannel>'
    ))
    assert bitrates == {}


# ---- v0.3.0 — per-channel status (binary sensor inputs) ----


def test_parse_channel_status_online_recording_motion():
    from xml.etree import ElementTree as ET

    xml = """<InputProxyChannelStatus>
        <online>true</online>
        <recordStatus>recording</recordStatus>
        <motionDetection>false</motionDetection>
    </InputProxyChannelStatus>"""
    status = _parse_channel_status(ET.fromstring(xml))
    assert status["online"] is True
    assert status["recording"] is True
    assert status["motion_detected"] is False


def test_parse_channel_status_idle_offline():
    from xml.etree import ElementTree as ET

    xml = """<InputProxyChannelStatus>
        <online>false</online>
        <recordStatus>idle</recordStatus>
    </InputProxyChannelStatus>"""
    status = _parse_channel_status(ET.fromstring(xml))
    assert status["online"] is False
    assert status["recording"] is False
    assert status["motion_detected"] is False


def test_parse_channel_status_handles_empty_root():
    status = _parse_channel_status(None)
    assert status == {
        "online": False,
        "recording": False,
        "motion_detected": False,
    }


def test_parse_channel_status_handles_partial_xml():
    from xml.etree import ElementTree as ET

    status = _parse_channel_status(ET.fromstring(
        '<InputProxyChannelStatus><online>true</online></InputProxyChannelStatus>'
    ))
    assert status["online"] is True
    assert status["recording"] is False  # missing field defaults to False
    assert status["motion_detected"] is False


# ---- v0.4.0 — NVR / IPC device-type normalization ----


def test_normalize_device_type_ipcamera_variants():
    """`IPCamera` / `ipc` / mixed case → canonical `ipcamera`."""
    for raw in ("IPCamera", "ipcamera", "IPC", "IpC", " ipc "):
        assert normalize_device_type(raw) == "ipcamera"


def test_normalize_device_type_nvr_variants():
    """`NetworkVideoRecorder` / `nvr` → `networkvideorecorder`."""
    for raw in ("NetworkVideoRecorder", "nvr", "NVR", "NetworkVideoRecorder "):
        assert normalize_device_type(raw) == "networkvideorecorder"


def test_normalize_device_type_dvr_variants():
    assert normalize_device_type("DVR") == "dvr"
    assert normalize_device_type("DigitalVideoRecorder") == "dvr"


def test_normalize_device_type_defaults_to_ipcamera():
    """Unknown / empty / None defaults to `ipcamera` (most devices are IPC)."""
    assert normalize_device_type("") == "ipcamera"
    assert normalize_device_type("UnknownType") == "ipcamera"
    assert normalize_device_type(None) == "ipcamera"


# ---- v0.5.0 — nested <CPUList>/<MemoryList> schema ----


def test_parse_system_status_nested_schema():
    """V5.x schema: <CPUList><CPU><cpuUtilization>… + <MemoryList>…"""
    from xml.etree import ElementTree as ET

    xml = """<DeviceStatus version="2.0">
        <currentDeviceTime>2026-09-24T16:42:03+08:00</currentDeviceTime>
        <deviceUpTime>92914</deviceUpTime>
        <CPUList>
            <CPU>
                <cpuDescription>ARM926EJ-Sid(wb)</cpuDescription>
                <cpuUtilization>16</cpuUtilization>
            </CPU>
        </CPUList>
        <MemoryList>
            <Memory>
                <memoryDescription>DDR Memory</memoryDescription>
                <memoryUsage>99</memoryUsage>
                <memoryAvailable>9696</memoryAvailable>
            </Memory>
        </MemoryList>
        <totalRebootCount>40</totalRebootCount>
    </DeviceStatus>"""
    status = _parse_system_status(ET.fromstring(xml))
    assert status["deviceStatus"] == "Unknown"  # not present, default
    assert status["cpuUtilization"] == "16"
    assert status["memoryUsage"] == "99"
    assert status["memoryAvailable"] == "9696"
    assert status["uptime"] == "92914"
    assert status["rebootCount"] == "40"
    assert status["cpuDescription"] == "ARM926EJ-Sid(wb)"


def test_parse_system_status_nvr_decimal_memory():
    """NVRs report memoryUsage as decimal MB (e.g. 1116.457031)."""
    from xml.etree import ElementTree as ET

    xml = """<DeviceStatus>
        <deviceUpTime>86343</deviceUpTime>
        <CPUList>
            <CPU>
                <cpuDescription>ARMv7 Processor rev 1 (v7l)</cpuDescription>
                <cpuUtilization>7</cpuUtilization>
            </CPU>
        </CPUList>
        <MemoryList>
            <Memory>
                <memoryDescription>DDR Memory</memoryDescription>
                <memoryUsage>1116.457031</memoryUsage>
                <memoryAvailable>13.5</memoryAvailable>
            </Memory>
        </MemoryList>
    </DeviceStatus>"""
    status = _parse_system_status(ET.fromstring(xml))
    assert status["cpuUtilization"] == "7"
    assert status["memoryUsage"] == "1116.457031"
    assert status["memoryAvailable"] == "13.5"
    assert status["uptime"] == "86343"
    assert status["rebootCount"] is None  # NVR doesn't have this field
    assert status["cpuDescription"] == "ARMv7 Processor rev 1 (v7l)"


def test_parse_system_status_handles_empty_root():
    """None → all defaults, rebootCount/cpuDescription None."""
    status = _parse_system_status(None)
    assert status["deviceStatus"] == "Unknown"
    assert status["cpuUtilization"] == "0"
    assert status["memoryUsage"] == "0"
    assert status["memoryAvailable"] == "0"
    assert status["uptime"] == "0"
    assert status["rebootCount"] is None
    assert status["cpuDescription"] is None


def test_parse_channel_status_extended_includes_health_fields():
    """v0.5.0 — extract SD card writes / reboot / uptime / dome info."""
    from xml.etree import ElementTree as ET

    xml = """<InputProxyChannelStatus>
        <online>true</online>
        <recordStatus>recording</recordStatus>
        <motionDetection>false</motionDetection>
        <deviceUpTime>594666</deviceUpTime>
        <totalRebootCount>40</totalRebootCount>
        <SDCardStatusInfo>
            <videoRewritingTimes>3581</videoRewritingTimes>
        </SDCardStatusInfo>
        <Camera>
            <cameraRunTotalTime>594666</cameraRunTotalTime>
        </Camera>
        <DomeInfo>
            <domeRunTotalTime>594666</domeRunTotalTime>
            <heatState>0</heatState>
            <fanState>1</fanState>
            <runtimeOverPositiveforty>346495</runtimeOverPositiveforty>
        </DomeInfo>
    </InputProxyChannelStatus>"""
    status = _parse_channel_status_extended(ET.fromstring(xml))
    # v0.3.0 fields
    assert status["online"] is True
    assert status["recording"] is True
    assert status["motion_detected"] is False
    # v0.5.0 fields
    assert status["uptime"] == "594666"
    assert status["reboot_count"] == "40"
    assert status["sd_card_writes"] == "3581"
    assert status["camera_run_total_time"] == "594666"
    assert status["dome_heat_state"] == "0"
    assert status["dome_fan_state"] == "1"
    assert status["dome_runtime_over_40"] == "346495"


def test_parse_channel_status_extended_handles_missing_optional_sections():
    """Missing SDCardStatusInfo / Camera / DomeInfo → all optional None."""
    from xml.etree import ElementTree as ET

    xml = """<InputProxyChannelStatus>
        <online>false</online>
        <recordStatus>idle</recordStatus>
    </InputProxyChannelStatus>"""
    status = _parse_channel_status_extended(ET.fromstring(xml))
    assert status["online"] is False
    assert status["recording"] is False
    assert status["uptime"] is None
    assert status["reboot_count"] is None
    assert status["sd_card_writes"] is None
    assert status["dome_heat_state"] is None
