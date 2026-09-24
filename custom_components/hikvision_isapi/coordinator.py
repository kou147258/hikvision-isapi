"""DataUpdateCoordinator for the Hikvision ISAPI integration.

Polls the device's ``/ISAPI/System/deviceInfo`` and
``/ISAPI/System/status`` endpoints on a configurable interval. Each
refresh builds the data dict that the sensor / camera / switch /
button platforms read.

The data shape is::

    {
        "deviceInfo": {
            "deviceName": "...",
            "deviceID": "...",
            "model": "DS-...",
            "serialNumber": "...",
            "firmwareVersion": "V5.x.x",
            "firmwareReleasedDate": "...",
            "deviceType": "...",
            "manufacturer": "Hikvision",
        },
        "systemStatus": {
            "deviceStatus": "OK" | "...",
            "cpuUsage": "27",       # percent as string
            "memoryUsage": "30",    # percent as string
            "uptime": "12345",      # seconds as string
        },
        "channels": [
            {"id": "1", "name": "Camera 1", "online": True, "recording": True},
            ...
        ],
        "capabilities": {
            "ptz": True | False,
        },
    }
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from xml.etree import ElementTree as ET

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    DOMAIN,
    ISAPI_CONTENT_MGMT_HDD,
    ISAPI_CONTENT_MGMT_STORAGE,
    ISAPI_INPUT_PROXY_CHANNELS,
    ISAPI_INPUT_PROXY_CHANNELS_STATUS,
    ISAPI_STREAMING_CHANNELS,
    ISAPI_SYSTEM_DEVICE_INFO,
    ISAPI_SYSTEM_NETWORK_INTERFACES,
    ISAPI_SYSTEM_STATUS,
)
from .isapi_client import ISAPIConnectionError, ISAPIClient, ISAPIError

_LOGGER = logging.getLogger(__name__)


def _xml_text(element: ET.Element | None, *path: str) -> str | None:
    """Return the text of an XML element at ``path`` (relative)."""
    if element is None:
        return None
    for tag in path:
        element = element.find(tag)
        if element is None:
            return None
    return element.text


def _parse_device_info(root: ET.Element | None) -> dict[str, str]:
    """Parse ``/ISAPI/System/deviceInfo`` response."""
    if root is None:
        return {}
    return {
        "deviceName": _xml_text(root, "deviceName") or "",
        "deviceID": _xml_text(root, "deviceID") or "",
        "model": _xml_text(root, "model") or "",
        "serialNumber": _xml_text(root, "serialNumber") or "",
        "firmwareVersion": _xml_text(root, "firmwareVersion") or "",
        "firmwareReleasedDate": _xml_text(root, "firmwareReleasedDate") or "",
        "deviceType": _xml_text(root, "deviceType") or "",
        "macAddress": _xml_text(root, "macAddress") or "",
        "manufacturer": "Hikvision",
    }


def _parse_system_status(root: ET.Element | None) -> dict[str, str]:
    """Parse ``/ISAPI/System/status`` response."""
    if root is None:
        return {}
    return {
        "deviceStatus": _xml_text(root, "deviceStatus") or "Unknown",
        "cpuUsage": _xml_text(root, "CPUUsage") or "0",
        "memoryUsage": _xml_text(root, "memoryUsage") or "0",
        "uptime": _xml_text(root, "uptime") or "0",
    }


def _parse_channels(root: ET.Element | None) -> list[dict[str, Any]]:
    """Parse ``/ISAPI/ContentMgmt/InputProxy/channels`` response.

    Hikvision's response is::

        <InputProxyChannelList>
          <InputProxyChannel>
            <id>1</id>
            <name>Camera 1</name>
            <online>true</online>
            ...
          </InputProxyChannel>
        </InputProxyChannelList>
    """
    if root is None:
        return []
    out: list[dict[str, Any]] = []
    for ch in root.findall(".//InputProxyChannel"):
        ch_id = _xml_text(ch, "id") or ""
        if not ch_id:
            continue
        out.append(
            {
                "id": ch_id,
                "name": _xml_text(ch, "name") or f"Channel {ch_id}",
                "online": (_xml_text(ch, "online") or "").lower() == "true",
                "recording": (_xml_text(ch, "recordStatus") or "").lower()
                == "recording",
            }
        )
    return out


def _parse_storage(root: ET.Element | None) -> dict[str, Any]:
    """Parse ``/ISAPI/ContentMgmt/storage`` response (NVR / DVR only).

    Hikvision's response shape is::

        <Storage>
          <totalCapacity>2000000</totalCapacity>          (MB)
          <usedCapacity>1234567</usedCapacity>           (MB)
          <freeCapacity>765433</freeCapacity>            (MB)
          <status>normal</status>                        (normal | exception)
        </Storage>
    """
    if root is None:
        return {
            "total_mb": None,
            "used_mb": None,
            "free_mb": None,
            "status": "unknown",
        }
    return {
        "total_mb": _safe_int_mb(_xml_text(root, "totalCapacity")),
        "used_mb": _safe_int_mb(_xml_text(root, "usedCapacity")),
        "free_mb": _safe_int_mb(_xml_text(root, "freeCapacity")),
        "status": _xml_text(root, "status") or "unknown",
    }


def _parse_network_interfaces(
    root: ET.Element | None,
) -> list[dict[str, Any]]:
    """Parse ``/ISAPI/System/Network/interfaces`` response.

    Hikvision's response shape is::

        <NetworkInterfaceList>
          <NetworkInterface>
            <id>1</id>
            <interfaceName>LAN1</interfaceName>
            <IPAddress>192.168.1.10</IPAddress>
            <subnetMask>255.255.255.0</subnetMask>
            <DefaultGateway>192.168.1.1</DefaultGateway>
            <MTU>1500</MTU>
            <MACAddress>00:11:22:33:44:55</MACAddress>
            ...
          </NetworkInterface>
        </NetworkInterfaceList>
    """
    if root is None:
        return []
    out: list[dict[str, Any]] = []
    for iface in root.findall(".//NetworkInterface"):
        out.append(
            {
                "id": _xml_text(iface, "id") or "",
                "name": _xml_text(iface, "interfaceName") or "",
                "ip_address": _xml_text(iface, "IPAddress") or "",
                "subnet_mask": _xml_text(iface, "subnetMask") or "",
                "default_gateway": _xml_text(iface, "DefaultGateway")
                or "",
                "mtu": _safe_int_mb(_xml_text(iface, "MTU")),
                "mac_address": _xml_text(iface, "MACAddress") or "",
            }
        )
    return out


def _parse_streaming_channels(
    root: ET.Element | None,
) -> dict[str, int]:
    """Parse ``/ISAPI/Streaming/channels`` for per-channel bitrate.

    Returns ``{channel_id: bitrate_kbps}``. Bitrate may be reported as
    ``videoAverageBitrate`` (kbps) or absent (older firmware); we
    silently skip channels without a bitrate field.
    """
    if root is None:
        return {}
    out: dict[str, int] = {}
    for ch in root.findall(".//StreamingChannel"):
        ch_id = _xml_text(ch, "id") or ""
        if not ch_id:
            continue
        bitrate = _safe_int_mb(
            _xml_text(ch, "videoAverageBitrate")
        )
        if bitrate is None:
            bitrate = _safe_int_mb(_xml_text(ch, "maxBitrate"))
        if bitrate is not None:
            out[ch_id] = bitrate
    return out


def _parse_channel_status(
    root: ET.Element | None,
) -> dict[str, bool]:
    """Parse ``/ISAPI/ContentMgmt/InputProxy/channels/<id>/status``.

    Hikvision's response shape is::

        <InputProxyChannelStatus>
          <online>true|false</online>
          <recordStatus>recording|idle</recordStatus>
          <signalLost>true|false</signalLost>     (optional — signal state)
          <motionDetection>true|false</motionDetection>  (optional)
        </InputProxyChannelStatus>

    We return a dict with three booleans; missing fields default to
    False (we don't know the channel is offline just because the
    firmware didn't report the field).
    """
    if root is None:
        return {
            "online": False,
            "recording": False,
            "motion_detected": False,
        }
    return {
        "online": (_xml_text(root, "online") or "").lower() == "true",
        "recording": (_xml_text(root, "recordStatus") or "").lower()
        == "recording",
        "motion_detected": (
            _xml_text(root, "motionDetection") or ""
        ).lower() == "true",
    }


def _safe_int_mb(value: Any) -> int | None:
    """Best-effort int parsing that handles leading/trailing whitespace."""
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


class HikvisionISAPIData:
    """Container for one coordinator refresh result."""

    def __init__(
        self,
        device_info: dict[str, str],
        system_status: dict[str, str],
        channels: list[dict[str, Any]],
        capabilities: dict[str, bool],
        storage: dict[str, Any] | None = None,
        network_interfaces: list[dict[str, Any]] | None = None,
        streaming_bitrate_kbps: dict[str, int] | None = None,
    ) -> None:
        self.device_info = device_info
        self.system_status = system_status
        self.channels = channels
        self.capabilities = capabilities
        self.storage = storage or {}
        self.network_interfaces = network_interfaces or []
        self.streaming_bitrate_kbps = streaming_bitrate_kbps or {}


class HikvisionISAPICoordinator(DataUpdateCoordinator[HikvisionISAPIData]):
    """Polls ``/ISAPI/System/deviceInfo`` etc. on a configurable interval.

    Single shared client per entry (and per coordinator instance) —
    this matches the v0.1.12 pattern from the hikvision_snmp integration
    where one shared engine is faster than per-call setup.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        *,
        host: str,
        port: int,
        username: str,
        password: str,
        verify_ssl: bool,
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{host}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        # Reuse HA's shared aiohttp session for connection pooling, etc.
        # We still need to use our own ISAPIClient for the DigestAuth and
        # TLS bypass logic, but we can plug HA's session in later.
        self._ha_session = async_get_clientsession(hass)
        self.device_info: dict[str, str] = {}
        self.system_status: dict[str, str] = {}
        self.channels: list[dict[str, Any]] = []
        self.capabilities: dict[str, bool] = {}
        self.storage: dict[str, Any] = {}
        self.network_interfaces: list[dict[str, Any]] = []
        self.streaming_bitrate_kbps: dict[str, int] = {}

    @property
    def host(self) -> str:
        return self._host

    def _make_client(self) -> ISAPIClient:
        return ISAPIClient(
            host=self._host,
            port=self._port,
            username=self._username,
            password=self._password,
            verify_ssl=self._verify_ssl,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

    async def _async_update_data(self) -> HikvisionISAPIData:
        """One coordinator refresh: GET deviceInfo / status / channels / storage / network."""
        try:
            async with self._make_client() as client:
                # Always fetched.
                device_info_xml = await client.get_xml(ISAPI_SYSTEM_DEVICE_INFO)
                status_xml = await client.get_xml(ISAPI_SYSTEM_STATUS)
                channels_xml = await client.get_xml(
                    ISAPI_INPUT_PROXY_CHANNELS
                )
                # Best-effort — some devices (mostly small IPCs) don't
                # implement these endpoints. We catch the ISAPIError so
                # a missing endpoint doesn't take down the entire
                # coordinator refresh.
                storage_xml: ET.Element | None = None
                try:
                    storage_xml = await client.get_xml(
                        ISAPI_CONTENT_MGMT_STORAGE
                    )
                except ISAPIError as exc:
                    _LOGGER.debug(
                        "Storage endpoint unavailable on %s: %s",
                        self._host, exc,
                    )
                network_xml: ET.Element | None = None
                try:
                    network_xml = await client.get_xml(
                        ISAPI_SYSTEM_NETWORK_INTERFACES
                    )
                except ISAPIError as exc:
                    _LOGGER.debug(
                        "Network endpoint unavailable on %s: %s",
                        self._host, exc,
                    )
                streaming_xml: ET.Element | None = None
                try:
                    streaming_xml = await client.get_xml(
                        f"{ISAPI_STREAMING_CHANNELS}/1"
                    )
                except ISAPIError as exc:
                    _LOGGER.debug(
                        "Streaming endpoint unavailable on %s: %s",
                        self._host, exc,
                    )
        except ISAPIConnectionError as exc:
            raise UpdateFailed(
                f"Network error talking to {self._host}: {exc}"
            ) from exc
        except ISAPIAuthError as exc:
            raise UpdateFailed(
                f"Authentication failed for {self._host}: {exc}"
            ) from exc
        except ISAPIError as exc:
            raise UpdateFailed(
                f"ISAPI error from {self._host}: {exc}"
            ) from exc

        device_info = _parse_device_info(device_info_xml)
        system_status = _parse_system_status(status_xml)
        channels = _parse_channels(channels_xml)
        storage = _parse_storage(storage_xml)
        network_interfaces = _parse_network_interfaces(network_xml)
        streaming_bitrate_kbps = _parse_streaming_channels(streaming_xml)

        # v0.3.0 — enrich each channel with detailed per-channel status
        # (online / recording / motion_detected). Best-effort per
        # channel: a small IPC that doesn't implement the per-channel
        # status endpoint keeps the channel-level online / recording
        # values from the channels list response (which are usually
        # present).
        for ch in channels:
            ch_id = ch.get("id", "")
            if not ch_id:
                continue
            try:
                async with self._make_client() as client:
                    status_xml = await client.get_xml(
                        ISAPI_INPUT_PROXY_CHANNELS_STATUS.format(id=ch_id)
                    )
                ch_status = _parse_channel_status(status_xml)
                # Only override fields that the per-channel endpoint
                # actually reported (non-default). This way, the
                # channel-list's online/recording values stay when the
                # per-channel endpoint is unavailable.
                ch["online"] = ch_status["online"] or ch.get("online", False)
                ch["recording"] = (
                    ch_status["recording"] or ch.get("recording", False)
                )
                ch["motion_detected"] = ch_status["motion_detected"]
            except ISAPIError as exc:
                _LOGGER.debug(
                    "Per-channel status unavailable for %s ch %s: %s",
                    self._host, ch_id, exc,
                )

        capabilities: dict[str, bool] = {
            "ptz": device_info.get("deviceType", "").lower()
            in {"ptz", "ptzdome"},
        }

        # Cache the parsed fields for downstream platforms.
        self.device_info = device_info
        self.system_status = system_status
        self.channels = channels
        self.capabilities = capabilities
        self.storage = storage
        self.network_interfaces = network_interfaces
        self.streaming_bitrate_kbps = streaming_bitrate_kbps

        return HikvisionISAPIData(
            device_info=device_info,
            system_status=system_status,
            channels=channels,
            capabilities=capabilities,
            storage=storage,
            network_interfaces=network_interfaces,
            streaming_bitrate_kbps=streaming_bitrate_kbps,
        )
