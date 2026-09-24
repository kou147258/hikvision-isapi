"""Camera platform for Hikvision ISAPI.

Each detected channel becomes one ``Camera`` entity that streams a
single ``/ISAPI/Streaming/channels/{id}/picture`` request to HA's
camera component. The HA frontend uses the returned bytes to display
a still image (refreshed by the camera component's standard
``async_camera_image`` polling).

A future v0.2 release can add a continuous MJPEG stream via
``/ISAPI/Streaming/channels/{id}/httppreview`` for live video, but
HA's built-in camera component only supports still images so that's
out of scope here.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.camera import Camera
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, ISAPI_STREAMING_CHANNELS
from .coordinator import HikvisionISAPICoordinator
from .isapi_client import ISAPIConnectionError, ISAPIClient, ISAPIError

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up camera entities from the coordinator's channel list."""
    coordinator: HikvisionISAPICoordinator = hass.data[DOMAIN][entry.entry_id]

    # v0.1.23 pattern (carried over from hikvision_snmp v0.1.23):
    # do not wait for the first refresh; the coordinator's normal poll
    # cycle populates data in the background. Register a listener for
    # late-arriving channels so we don't miss any.
    entities = [
        HikvisionISAPICamera(coordinator, entry, ch)
        for ch in coordinator.channels
    ]
    async_add_entities(entities)

    if not getattr(coordinator, "_hikvision_isapi_camera_added", False):
        coordinator._hikvision_isapi_camera_added = False  # type: ignore[attr-defined]
        coordinator.async_add_listener(
            _make_camera_listener(
                hass, entry, coordinator, async_add_entities
            )
        )


def _make_camera_listener(
    hass: HomeAssistant,
    entry: ConfigEntry,
    coordinator: HikvisionISAPICoordinator,
    async_add_entities: AddEntitiesCallback,
):
    async def _on_update() -> None:
        if getattr(coordinator, "_hikvision_isapi_camera_added", False):
            return
        if coordinator.data is None:
            return
        new_entities = [
            HikvisionISAPICamera(coordinator, entry, ch)
            for ch in coordinator.channels
        ]
        if not new_entities:
            return
        coordinator._hikvision_isapi_camera_added = True  # type: ignore[attr-defined]
        async_add_entities(new_entities)

    return _on_update


class HikvisionISAPICamera(CoordinatorEntity[HikvisionISAPICoordinator], Camera):
    """A still-image camera entity backed by Hikvision's /picture endpoint."""

    _attr_translation_key = "camera"
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HikvisionISAPICoordinator,
        entry: ConfigEntry,
        channel: dict[str, Any],
    ) -> None:
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)
        self._entry = entry
        self._channel = channel
        self._attr_unique_id = f"{entry.entry_id}_camera_{channel['id']}"
        self._attr_translation_key = "camera"
        # Use channel name (e.g. "Camera 1") as the entity name suffix.
        self._attr_name = channel.get("name") or f"Channel {channel['id']}"
        self._attr_device_info = coordinator.device_info if hasattr(coordinator, "device_info") else None

    @property
    def channel_id(self) -> str:
        return self._channel["id"]

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
    ) -> bytes | None:
        """Return a single still frame from ``/ISAPI/Streaming/channels/{id}/picture``.

        Hikvision's ``/picture`` endpoint ignores ``width`` / ``height``
        parameters and returns the full-resolution JPEG; the HA frontend
        scales it to fit the card. We forward ``width`` / ``height`` via
        query string for documentation only.
        """
        coordinator: HikvisionISAPICoordinator = self.coordinator
        path = f"{ISAPI_STREAMING_CHANNELS}/{self.channel_id}/picture"
        # Add width / height query params for documentation; Hikvision
        # ignores them but third-party integrators might check.
        if width or height:
            qs = []
            if width:
                qs.append(f"videoResolutionWidth={int(width)}")
            if height:
                qs.append(f"videoResolutionHeight={int(height)}")
            path = f"{path}?{'&'.join(qs)}"

        try:
            async with ISAPIClient(
                host=coordinator._host,
                port=coordinator._port,
                username=coordinator._username,
                password=coordinator._password,
                verify_ssl=coordinator._verify_ssl,
                timeout=coordinator._timeout if hasattr(coordinator, "_timeout") else 10,
            ) as client:
                return await client.get_bytes(path)
        except (ISAPIConnectionError, ISAPIError) as exc:
            _LOGGER.debug("Camera image fetch failed for %s: %s", path, exc)
            return None
