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
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_REQUEST_TIMEOUT,
    DEVICE_TYPE_DVR,
    DEVICE_TYPE_IPCAMERA,
    DEVICE_TYPE_NETWORK_VIDEO_RECORDER,
    DOMAIN,
    ISAPI_CONTENT_MGMT_STORAGE,
    ISAPI_INPUT_PROXY_CHANNELS,
    ISAPI_INPUT_PROXY_CHANNELS_STATUS,
    ISAPI_STREAMING_CHANNELS,
    ISAPI_STREAMING_CHANNELS_STATUS,
    ISAPI_SYSTEM_DEVICE_INFO,
    ISAPI_SYSTEM_NETWORK_INTERFACES,
    ISAPI_SYSTEM_STATUS,
    ISAPI_SYSTEM_STORAGE_HARDDISKS,
)
from .isapi_client import ISAPIAuthError, ISAPIConnectionError, ISAPIClient, ISAPIError

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


def normalize_device_type(raw: str) -> str:
    """Map ``deviceInfo.deviceType`` to one of the canonical DEVICE_TYPE_*.

    Hikvision's ``deviceType`` field is free-form text — common
    values are ``IPCamera``, ``NetworkVideoRecorder``, ``DVR``,
    ``ipc``, ``nvr``, ``dvr`` (any case). The path selection in
    ``camera.py`` depends on the canonical IPCamera / NVR / DVR
    mapping; everything else (unknown / empty) defaults to IPCamera
    since the user fleet is mostly IPC.
    """
    s = (raw or "").strip().lower()
    if s in {"nvr", "networkvideorecorder"}:
        return DEVICE_TYPE_NETWORK_VIDEO_RECORDER
    if s in {"dvr", "digitalvideorecorder"}:
        return DEVICE_TYPE_DVR
    # Default to IPC for "ipcamera", "ipc", "" or anything unknown.
    return DEVICE_TYPE_IPCAMERA


def _parse_system_status(root: ET.Element | None) -> dict[str, str]:
    """Parse ``/ISAPI/System/status`` response.

    Hikvision's response is::

        <DeviceStatus version="2.0" ...>
          <currentDeviceTime>2026-09-24T...</currentDeviceTime>
          <deviceUpTime>12345</deviceUpTime>
          <CPUList>
            <CPU>
              <cpuDescription>ARMv7 ...</cpuDescription>
              <cpuUtilization>27</cpuUtilization>   (%)
            </CPU>
          </CPUList>
          <MemoryList>
            <Memory>
              <memoryDescription>DDR Memory</memoryDescription>
              <memoryUsage>1234</memoryUsage>         (KB on IPC, MB on NVR)
              <memoryAvailable>5678</memoryAvailable>
            </Memory>
          </MemoryList>
          <totalRebootCount>40</totalRebootCount>     (optional — IPC only)
        </DeviceStatus>

    Older / future firmwares sometimes use a flat ``<SystemStatus>``
    schema (``<CPUUsage>`` / ``<memoryUsage>`` as direct children).
    We accept both.
    """
    if root is None:
        return {
            "deviceStatus": "Unknown",
            "cpuUtilization": "0",
            "memoryUsage": "0",
            "memoryAvailable": "0",
            "uptime": "0",
            "rebootCount": None,
            "cpuDescription": None,
        }
    # New schema (V5.x) — nested CPUList / MemoryList.
    cpu = root.find(".//CPU")
    memory = root.find(".//Memory")
    cpu_util = _xml_text(cpu, "cpuUtilization") if cpu is not None else None
    if cpu_util is None:
        # Flat schema fallback
        cpu_util = _xml_text(root, "CPUUsage")
    mem_usage = _xml_text(memory, "memoryUsage") if memory is not None else None
    if mem_usage is None:
        mem_usage = _xml_text(root, "memoryUsage")
    mem_avail = _xml_text(memory, "memoryAvailable") if memory is not None else None
    if mem_avail is None:
        mem_avail = _xml_text(root, "memoryFree")

    return {
        "deviceStatus": (
            _xml_text(root, "deviceStatus") or "Unknown"
        ),
        "cpuUtilization": cpu_util or "0",
        "memoryUsage": mem_usage or "0",
        "memoryAvailable": mem_avail or "0",
        "uptime": _xml_text(root, "deviceUpTime") or _xml_text(root, "uptime") or "0",
        "rebootCount": _xml_text(root, "totalRebootCount"),
        "cpuDescription": _xml_text(cpu, "cpuDescription") if cpu is not None else None,
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


def _parse_streaming_channels_list(
    root: ET.Element | None,
) -> list[dict[str, Any]]:
    """Parse ``/ISAPI/Streaming/channels`` response (IPC-side).

    Hikvision's response shape is::

        <StreamingChannelList>
          <StreamingChannel>
            <id>1</id>
            <videoInputChannelID>1</videoInputChannelID>
            <name>Camera 1</name>            (optional)
            <online>true</online>           (optional)
            ...
          </StreamingChannel>
        </StreamingChannelList>

    Unlike the InputProxy list, this response shape doesn't always
    include ``recordStatus`` or ``online`` — those come from the
    per-channel status endpoint instead.
    """
    if root is None:
        return []
    out: list[dict[str, Any]] = []
    for ch in root.findall(".//StreamingChannel"):
        ch_id = (
            _xml_text(ch, "id")
            or _xml_text(ch, "videoInputChannelID")
            or ""
        )
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


def _parse_channel_status_extended(
    root: ET.Element | None,
) -> dict[str, Any]:
    """Extended per-channel status (v0.5.0).

    Same schema as ``_parse_channel_status`` but reads additional
    fields useful for IPC health monitoring:

    - ``uptime`` (seconds) — device uptime, same value as
      ``/ISAPI/System/status``'s ``deviceUpTime``.
    - ``reboot_count`` — total device reboots.
    - ``sd_card_writes`` — ``SDCardStatusInfo/videoRewritingTimes`` for
      IPCs with SD cards; the most actionable health indicator
      (SD cards typically last ~3,000-5,000 rewrite cycles before
      failing).
    - ``camera_run_total_time`` — ``Camera/cameraRunTotalTime`` for
      PTZ IPCs.
    - ``dome_heat_state`` / ``dome_fan_state`` — ``DomeInfo/heatState``
      / ``DomeInfo/fanState`` (0=ok, 1=running/active).
    - ``dome_runtime_over_40`` — ``DomeInfo/runtimeOverPositiveforty``
      (cumulative seconds operating above 40°C; high = thermal
      stress).
    """
    if root is None:
        return {
            "online": False,
            "recording": False,
            "motion_detected": False,
            "uptime": None,
            "reboot_count": None,
            "sd_card_writes": None,
            "camera_run_total_time": None,
            "dome_heat_state": None,
            "dome_fan_state": None,
            "dome_runtime_over_40": None,
        }
    dome = root.find(".//DomeInfo")
    camera = root.find(".//Camera")
    sdcard = root.find(".//SDCardStatusInfo")
    return {
        "online": (_xml_text(root, "online") or "").lower() == "true",
        "recording": (
            (_xml_text(root, "recordStatus") or "").lower() == "recording"
        ),
        "motion_detected": (
            _xml_text(root, "motionDetection") or ""
        ).lower() == "true",
        "uptime": _xml_text(root, "deviceUpTime") or _xml_text(root, "uptime"),
        "reboot_count": _xml_text(root, "totalRebootCount"),
        "sd_card_writes": (
            _xml_text(sdcard, "videoRewritingTimes") if sdcard is not None else None
        ),
        "camera_run_total_time": (
            _xml_text(camera, "cameraRunTotalTime") if camera is not None else None
        ),
        "dome_heat_state": (
            _xml_text(dome, "heatState") if dome is not None else None
        ),
        "dome_fan_state": (
            _xml_text(dome, "fanState") if dome is not None else None
        ),
        "dome_runtime_over_40": (
            _xml_text(dome, "runtimeOverPositiveforty") if dome is not None else None
        ),
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
        # Normalized device type: "ipcamera" / "networkvideorecorder" /
        # "dvr". Used by camera.py to route the snapshot endpoint.
        self.device_type: str = normalize_device_type(
            device_info.get("deviceType", "")
        )
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
        use_https: bool,
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
        self._use_https = use_https
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
            use_https=self._use_https,
            timeout=DEFAULT_REQUEST_TIMEOUT,
        )

    async def _async_update_data(self) -> HikvisionISAPIData:
        """One coordinator refresh: GET deviceInfo / status / channels / storage / network.

        v0.6.9: device-type-aware endpoint selection. The user has
        both IPCs (10.18.176.10, 10.18.176.65) and NVRs/DVRs
        (192.168.10.10) on the network, and the channel-list
        endpoint differs:

        - **IPC**: ``/ISAPI/Streaming/channels`` returns the
          streaming channel list (used for snapshot URL
          ``/Streaming/channels/{id}/picture``).
        - **NVR / DVR**: ``/ISAPI/ContentMgmt/InputProxy/channels``
          returns the mounted IPC channel list (used for snapshot
          URL ``/ContentMgmt/StreamingProxy/channels/{id}/picture``).

        Pre-v0.6.9 we hard-coded the NVR endpoint for all device
        types. IPCs returned HTTP 403 on the NVR endpoint, leaving
        the channels list empty for every IPC and no per-channel
        entities.

        We now fetch deviceInfo first (always succeeds on Hikvision
        firmware), determine the device type from
        ``deviceInfo.deviceType``, then choose the right endpoint.
        Fallbacks handle firmware quirks:

        - IPC: try ``/Streaming/channels``, fall back to
          ``/ContentMgmt/InputProxy/channels``.
        - NVR/DVR: try ``/ContentMgmt/InputProxy/channels``, fall
          back to ``/Streaming/channels``.

        Per-channel status endpoint is similarly chosen from device
        type (``/Streaming/channels/{id}/status`` for IPC,
        ``/ContentMgmt/InputProxy/channels/{id}/status`` for NVR/DVR).
        """
        try:
            async with self._make_client() as client:
                # Fetch deviceInfo first so we know which channel-list
                # endpoint to query.
                device_info_xml = await client.get_xml(ISAPI_SYSTEM_DEVICE_INFO)
                status_xml = await client.get_xml(ISAPI_SYSTEM_STATUS)

                device_info = _parse_device_info(device_info_xml)
                device_type = normalize_device_type(
                    device_info.get("deviceType", "")
                )

                # Pick the right channel-list endpoint(s) for this
                # device type. We try the "primary" first then fall
                # back to the alternate in case of firmware that
                # implements only one.
                if device_type == DEVICE_TYPE_IPCAMERA:
                    primary_channels = ISAPI_STREAMING_CHANNELS
                    alt_channels = ISAPI_INPUT_PROXY_CHANNELS
                    primary_status_fmt = ISAPI_STREAMING_CHANNELS_STATUS
                    alt_status_fmt = ISAPI_INPUT_PROXY_CHANNELS_STATUS
                else:
                    # NVR or DVR
                    primary_channels = ISAPI_INPUT_PROXY_CHANNELS
                    alt_channels = ISAPI_STREAMING_CHANNELS
                    primary_status_fmt = ISAPI_INPUT_PROXY_CHANNELS_STATUS
                    alt_status_fmt = ISAPI_STREAMING_CHANNELS_STATUS

                channels_xml: ET.Element | None = None
                try:
                    channels_xml = await client.get_xml(primary_channels)
                except (ISAPIError, ISAPIAuthError) as exc:
                    _LOGGER.debug(
                        "Primary channels endpoint %s unavailable on "
                        "%s (device_type=%s): %s — trying fallback %s",
                        primary_channels, self._host, device_type,
                        exc, alt_channels,
                    )
                    try:
                        channels_xml = await client.get_xml(alt_channels)
                    except (ISAPIError, ISAPIAuthError) as exc2:
                        _LOGGER.debug(
                            "Fallback channels endpoint %s also "
                            "unavailable on %s: %s — continuing with "
                            "empty channels list",
                            alt_channels, self._host, exc2,
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
                    # Old V4 NVRs (DS-7708-I4, DS-8632-I8, etc.) don't
                    # implement /ContentMgmt/storage. Fall back to the
                    # legacy /System/Storage/hardDisks endpoint that
                    # these firmwares do expose.
                    if exc.status_code == 404:
                        _LOGGER.debug(
                            "ContentMgmt/storage not supported on %s, "
                            "falling back to System/Storage/hardDisks",
                            self._host,
                        )
                        try:
                            storage_xml = await client.get_xml(
                                ISAPI_SYSTEM_STORAGE_HARDDISKS
                            )
                        except ISAPIError as exc2:
                            _LOGGER.debug(
                                "System/Storage/hardDisks also unavailable "
                                "on %s: %s",
                                self._host, exc2,
                            )
                    else:
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
        # Parse channels based on which endpoint returned XML. The two
        # endpoint response shapes (InputProxyChannel vs StreamingChannel)
        # differ in wrapper / child element names; we pick the matching
        # parser. The ``primary_channels`` endpoint tells us which shape
        # to expect.
        if channels_xml is not None:
            # Detect which shape we got by looking at the root tag.
            root_tag = channels_xml.tag.split("}")[-1]  # strip namespace
            if root_tag.endswith("StreamingChannelList"):
                channels = _parse_streaming_channels_list(channels_xml)
            else:
                channels = _parse_channels(channels_xml)
        else:
            channels = []
        storage = _parse_storage(storage_xml)
        network_interfaces = _parse_network_interfaces(network_xml)
        streaming_bitrate_kbps = _parse_streaming_channels(streaming_xml)

        # v0.6.9 — per-channel status endpoint chosen by device type.
        # IPCs use /Streaming/channels/{id}/status, NVR/DVRs use
        # /ContentMgmt/InputProxy/channels/{id}/status.
        # We determine the right format from device_type (already parsed
        # above into device_info). Fall back to primary_status_fmt
        # then alt_status_fmt if one returns ISAPIError.
        per_ch_status_fmt = primary_status_fmt

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
            status_xml = None
            try:
                async with self._make_client() as client:
                    status_xml = await client.get_xml(
                        per_ch_status_fmt.format(id=ch_id)
                    )
            except (ISAPIError, ISAPIAuthError) as exc:
                # Try the alt status endpoint in case device type
                # detection was wrong (e.g. deviceType is unknown and
                # defaulted to ipcamera but it's actually an NVR).
                if alt_status_fmt != per_ch_status_fmt:
                    try:
                        async with self._make_client() as client:
                            status_xml = await client.get_xml(
                                alt_status_fmt.format(id=ch_id)
                            )
                    except (ISAPIError, ISAPIAuthError) as exc2:
                        _LOGGER.debug(
                            "Per-channel status unavailable for "
                            "%s ch %s: %s / %s",
                            self._host, ch_id, exc, exc2,
                        )
                else:
                    _LOGGER.debug(
                        "Per-channel status unavailable for %s "
                        "ch %s: %s",
                        self._host, ch_id, exc,
                    )
            if status_xml is None:
                continue
            try:
                ch_status = _parse_channel_status_extended(status_xml)
                # Only override fields that the per-channel endpoint
                # actually reported (non-default). This way, the
                # channel-list's online/recording values stay when the
                # per-channel endpoint is unavailable.
                ch["online"] = ch_status["online"] or ch.get("online", False)
                ch["recording"] = (
                    ch_status["recording"] or ch.get("recording", False)
                )
                ch["motion_detected"] = ch_status["motion_detected"]
                # v0.5.0 — extended IPC health fields. The endpoint
                # reports the device uptime (not the channel's); useful
                # for the binary_sensor platforms and the new per-channel
                # sensor entities.
                for k in (
                    "uptime",
                    "reboot_count",
                    "sd_card_writes",
                    "camera_run_total_time",
                    "dome_heat_state",
                    "dome_fan_state",
                    "dome_runtime_over_40",
                ):
                    v = ch_status.get(k)
                    if v is not None:
                        ch[k] = v
            except ISAPIError as exc:
                _LOGGER.debug(
                    "Per-channel status parse failed for %s ch %s: %s",
                    self._host, ch_id, exc,
                )

        capabilities: dict[str, bool] = {
            "ptz": device_info.get("deviceType", "").lower()
            in {"ptz", "ptzdome"},
        }

        # Cache the parsed fields for downstream platforms.
        self.device_info = device_info
        self.device_type = normalize_device_type(
            device_info.get("deviceType", "")
        )
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
