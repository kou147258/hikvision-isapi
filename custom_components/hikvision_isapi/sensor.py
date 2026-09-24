"""Sensor platform for Hikvision ISAPI.

Exposes device_info and system_status as sensors. The mapping is
fixed in ``SENSORS`` — the user can rename / re-unit / hide any of
them in the HA UI.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    UnitOfInformation,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HikvisionISAPICoordinator, HikvisionISAPIData

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class HikvisionISAPISensorDescription(SensorEntityDescription):
    """A sensor description for ISAPI-derived values."""

    value_fn: Any  # Callable[[HikvisionISAPIData], Any]


SENSORS: tuple[HikvisionISAPISensorDescription, ...] = (
    HikvisionISAPISensorDescription(
        key="model",
        translation_key="model",
        name="型号",
        icon="mdi:information-outline",
        value_fn=lambda d: d.device_info.get("model", ""),
    ),
    HikvisionISAPISensorDescription(
        key="serial_number",
        translation_key="serial_number",
        name="序列号",
        icon="mdi:barcode",
        value_fn=lambda d: d.device_info.get("serialNumber", ""),
    ),
    HikvisionISAPISensorDescription(
        key="firmware_version",
        translation_key="firmware_version",
        name="固件版本",
        icon="mdi:chip",
        value_fn=lambda d: d.device_info.get("firmwareVersion", ""),
    ),
    HikvisionISAPISensorDescription(
        key="device_status",
        translation_key="device_status",
        name="设备状态",
        icon="mdi:check-circle",
        value_fn=lambda d: d.system_status.get("deviceStatus", "Unknown"),
    ),
    HikvisionISAPISensorDescription(
        key="cpu_usage",
        translation_key="cpu_usage",
        name="CPU 使用率",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:chip",
        value_fn=lambda d: _safe_int(d.system_status.get("cpuUsage")),
    ),
    HikvisionISAPISensorDescription(
        key="memory_usage",
        translation_key="memory_usage",
        name="内存使用率",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:memory",
        value_fn=lambda d: _safe_int(d.system_status.get("memoryUsage")),
    ),
    HikvisionISAPISensorDescription(
        key="uptime",
        translation_key="uptime",
        name="运行时长",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:clock-outline",
        value_fn=lambda d: _safe_int(d.system_status.get("uptime")),
    ),
    HikvisionISAPISensorDescription(
        key="channel_count",
        translation_key="channel_count",
        name="通道数",
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:counter",
        value_fn=lambda d: len(d.channels),
    ),
    # ---- v0.2.0 — storage / network / per-channel bitrate ----
    # Storage (NVR / DVR only). The storage endpoint exists on most
    # NVRs but not on small IPCs; if the device returns an error or
    # empty payload, ``d.storage`` is an empty dict and these
    # extractors return ``None`` so the entity shows ``unknown`` /
    # ``unavailable`` rather than garbage.
    HikvisionISAPISensorDescription(
        key="storage_total",
        translation_key="storage_total",
        name="存储总量",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        icon="mdi:harddisk",
        # Storage values are reported in MB by Hikvision.
        value_fn=lambda d: _mb_to_gb(d.storage.get("total_mb")),
    ),
    HikvisionISAPISensorDescription(
        key="storage_used",
        translation_key="storage_used",
        name="存储使用量",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        icon="mdi:harddisk",
        value_fn=lambda d: _mb_to_gb(d.storage.get("used_mb")),
    ),
    HikvisionISAPISensorDescription(
        key="storage_free",
        translation_key="storage_free",
        name="存储剩余",
        device_class=SensorDeviceClass.DATA_SIZE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfInformation.MEGABYTES,
        icon="mdi:harddisk",
        value_fn=lambda d: _mb_to_gb(d.storage.get("free_mb")),
    ),
    HikvisionISAPISensorDescription(
        key="storage_usage",
        translation_key="storage_usage",
        name="存储使用率",
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        icon="mdi:harddisk",
        value_fn=lambda d: _storage_usage_pct(d.storage),
    ),
    # Network interface — IP / subnet mask / gateway. We use the
    # first network interface Hikvision reports (most devices have a
    # single LAN port). Future work: per-interface entities when
    # multiple NICs are present.
    HikvisionISAPISensorDescription(
        key="network_ip",
        translation_key="network_ip",
        name="IP 地址",
        icon="mdi:ip",
        value_fn=lambda d: _first_iface(d, "ip_address"),
    ),
    HikvisionISAPISensorDescription(
        key="network_subnet",
        translation_key="network_subnet",
        name="子网掩码",
        icon="mdi:subnet",
        value_fn=lambda d: _first_iface(d, "subnet_mask"),
    ),
    HikvisionISAPISensorDescription(
        key="network_gateway",
        translation_key="network_gateway",
        name="默认网关",
        icon="mdi:router-network",
        value_fn=lambda d: _first_iface(d, "default_gateway"),
    ),
    HikvisionISAPISensorDescription(
        key="network_mac",
        translation_key="network_mac",
        name="MAC 地址",
        icon="mdi:network",
        value_fn=lambda d: _first_iface(d, "mac_address"),
    ),
    # Per-channel average bitrate (kbps). Reads from
    # /ISAPI/Streaming/channels/1 — Hikvision's main channel
    # streaming endpoint. Useful for spotting motion-triggered
    # bitrate spikes on the IPC side.
    HikvisionISAPISensorDescription(
        key="channel_1_bitrate",
        translation_key="channel_1_bitrate",
        name="通道 1 平均码率",
        device_class=SensorDeviceClass.DATA_RATE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement="kbps",
        icon="mdi:video",
        value_fn=lambda d: d.streaming_bitrate_kbps.get("1"),
    ),
)


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _mb_to_gb(mb: int | None) -> float | None:
    """Convert MB to GB with one decimal place (HA prefers round numbers)."""
    if mb is None:
        return None
    return round(mb / 1024, 1)


def _storage_usage_pct(storage: dict[str, Any]) -> float | None:
    """Compute used / total as a percentage."""
    total = storage.get("total_mb")
    used = storage.get("used_mb")
    if total is None or used is None or total <= 0:
        return None
    return round(used * 100 / total, 1)


def _first_iface(data: HikvisionISAPIData, field: str) -> str | None:
    """Return ``field`` from the first network interface, or None."""
    ifaces = data.network_interfaces
    if not ifaces:
        return None
    value = ifaces[0].get(field)
    return value or None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up static ISAPI sensors (model, firmware, cpu, memory, etc.)."""
    coordinator: HikvisionISAPICoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        HikvisionISAPISensor(coordinator, entry, desc) for desc in SENSORS
    ]
    async_add_entities(entities)


class HikvisionISAPISensor(CoordinatorEntity[HikvisionISAPICoordinator], SensorEntity):
    """Static ISAPI sensor entity."""

    _attr_has_entity_name = True
    entity_description: HikvisionISAPISensorDescription

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        description: HikvisionISAPISensorDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_{description.key}"
        self._attr_device_info = None  # filled by async_added_to_hass

    @property
    def native_value(self) -> Any:
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
