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
from homeassistant.const import PERCENTAGE, UnitOfTime
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
)


def _safe_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


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
