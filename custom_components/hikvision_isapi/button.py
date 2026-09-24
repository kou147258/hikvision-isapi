"""Button platform for Hikvision ISAPI.

One device-level button: ``Reboot Device``. Sends
``PUT /ISAPI/System/reboot``. The device reboots within ~30-60 s, during
which the integration's coordinator polls will fail with
connection errors. The coordinator recovers on the next successful
poll (10-30 s after the device is back up).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, ISAPI_SYSTEM_REBOOT
from .coordinator import HikvisionISAPICoordinator
from .isapi_client import (
    ISAPIConnectionError,
    ISAPIClient,
    ISAPIError,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the device-level Reboot button."""
    coordinator: HikvisionISAPICoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([HikvisionISAPIRebootButton(coordinator, entry)])


class HikvisionISAPIRebootButton(
    CoordinatorEntity[HikvisionISAPICoordinator], ButtonEntity
):
    """A button that reboots the device via ``PUT /ISAPI/System/reboot``."""

    _attr_translation_key = "reboot"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_reboot"
        self._attr_name = "重启设备"
        self._attr_device_info = None

    async def async_press(self) -> None:
        coordinator = self.coordinator
        try:
            async with ISAPIClient(
                host=coordinator._host,
                port=coordinator._port,
                username=coordinator._username,
                password=coordinator._password,
                verify_ssl=coordinator._verify_ssl,
                timeout=10,
            ) as client:
                await client.put_text(ISAPI_SYSTEM_REBOOT, "")
        except (ISAPIConnectionError, ISAPIError) as exc:
            _LOGGER.warning("Failed to send reboot to %s: %s", coordinator._host, exc)
            # The reboot may have succeeded anyway; the connection drops
            # during the device's reboot sequence. We don't raise.
        else:
            _LOGGER.info("Sent reboot to %s", coordinator._host)
        # Trigger a refresh so HA reflects any state changes after reboot.
        await coordinator.async_request_refresh()
