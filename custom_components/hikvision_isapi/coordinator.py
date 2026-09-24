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
    ISAPI_INPUT_PROXY_CHANNELS,
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


class HikvisionISAPIData:
    """Container for one coordinator refresh result."""

    def __init__(
        self,
        device_info: dict[str, str],
        system_status: dict[str, str],
        channels: list[dict[str, Any]],
        capabilities: dict[str, bool],
    ) -> None:
        self.device_info = device_info
        self.system_status = system_status
        self.channels = channels
        self.capabilities = capabilities


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
        """One coordinator refresh: GET deviceInfo / status / channels."""
        try:
            async with self._make_client() as client:
                device_info_xml = await client.get_xml(ISAPI_SYSTEM_DEVICE_INFO)
                status_xml = await client.get_xml(ISAPI_SYSTEM_STATUS)
                channels_xml = await client.get_xml(
                    ISAPI_INPUT_PROXY_CHANNELS
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

        capabilities: dict[str, bool] = {
            "ptz": device_info.get("deviceType", "").lower()
            in {"ptz", "ptzdome"},
        }

        # Cache the parsed fields for downstream platforms.
        self.device_info = device_info
        self.system_status = system_status
        self.channels = channels
        self.capabilities = capabilities

        return HikvisionISAPIData(
            device_info=device_info,
            system_status=system_status,
            channels=channels,
            capabilities=capabilities,
        )
