"""Tests for the v0.6.2 fix batch.

Covers regressions found during the v0.6.1 → v0.6.2 audit:

1. **NVR snapshot URL bug** (camera.py) — the previous code built the
   NVR streaming-proxy URL as a literal f-string concatenation that
   included the unevaluated text ``.format(id=N)`` as part of the
   URL, producing requests like
   ``/ISAPI/ContentMgmt/StreamingProxy/channels/{id}/picture.format(id=1)``
   that always 404. The fix calls ``.format(id=…)`` properly.

2. **IPC snapshot URL is unchanged** — sanity check that the
   pre-existing IPC path is still correct.

3. **build_device_info helper** — the ``entity.py`` helper groups all
   entities for one entry under a single HA device tile (MAC /
   identifier / connections / model / configuration_url).

4. **HikvisionISAPIEntity base class** — sub-classes pick up
   ``device_info`` from the coordinator's data on
   ``async_added_to_hass`` and re-bind on every coordinator update.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock as _MagMock  # noqa: F401  (placeholder)

from custom_components.hikvision_isapi_performance.const import (
    DOMAIN,
    ISAPI_CONTENT_MGMT_STREAMING_PROXY_CHANNELS_PICTURE,
    ISAPI_STREAMING_CHANNELS,
)
from custom_components.hikvision_isapi_performance.entity import (
    HikvisionISAPIEntity,
    build_device_info,
)


# ---- camera snapshot URL routing ----


def test_nvr_snapshot_path_formats_channel_id_correctly():
    """v0.6.2 regression: NVR path must call .format(id=...), not embed it as literal.

    The pre-v0.6.2 code did
        f"{ISAPI_CONTENT_MGMT_STREAMING_PROXY_CHANNELS_PICTURE}.format(id={channel_id})"
    which produced a path ending in ``.format(id=1)`` instead of ``/1/picture``.
    """
    channel_id = "1"
    path = ISAPI_CONTENT_MGMT_STREAMING_PROXY_CHANNELS_PICTURE.format(id=channel_id)
    assert path == "/ISAPI/ContentMgmt/StreamingProxy/channels/1/picture"
    # And specifically: the buggy string was
    #   "/ISAPI/ContentMgmt/StreamingProxy/channels/{id}/picture.format(id=1)"
    assert ".format(" not in path


def test_nvr_snapshot_path_handles_multi_channel_nvr():
    """Multi-channel NVR: each channel id resolves to its own URL."""
    for cid in ("1", "4", "16", "32"):
        path = ISAPI_CONTENT_MGMT_STREAMING_PROXY_CHANNELS_PICTURE.format(id=cid)
        assert f"/channels/{cid}/picture" in path
        assert path.endswith("/picture")


def test_ipc_snapshot_path_uses_streaming_channels():
    """IPC path is the direct /Streaming/channels/{id}/picture endpoint."""
    channel_id = "1"
    path = f"{ISAPI_STREAMING_CHANNELS}/{channel_id}/picture"
    assert path == "/ISAPI/Streaming/channels/1/picture"


def test_nvr_snapshot_path_no_literal_format_substring():
    """v0.6.2 regression: camera.py code path must not include ``.format(`` in URL.

    Reads the camera.py source and checks the NVR-branch string template
    doesn't have the pre-fix typo of ``f"...format(id={self.channel_id})"``
    inside an f-string. We look for the exact buggy substring that v0.6.1
    shipped with, which produced URLs ending in ``.format(id=N)``.
    """
    from pathlib import Path
    src = (Path(__file__).parent.parent / "custom_components"
           / "hikvision_isapi_performance" / "camera.py").read_text(
        encoding="utf-8-sig"
    )
    # The buggy pattern was: f"{CONST}.format(id={self.channel_id})"
    # We allow the literal `.format(` only if it's a *function call*
    # outside of an f-string interpolation, e.g. CONST.format(id=...).
    assert 'f"{ISAPI_CONTENT_MGMT_STREAMING_PROXY_CHANNELS_PICTURE}"' not in src, (
        "camera.py has the v0.6.1 NVR snapshot bug — "
        "ISAPI_CONTENT_MGMT_STREAMING_PROXY_CHANNELS_PICTURE is being "
        "concatenated instead of .format()'d."
    )
    assert '.format(id={self.channel_id})"' not in src, (
        "camera.py contains the buggy f-string '.format(id=...)' "
        "embedded as a literal URL component."
    )


# ---- build_device_info ----


def test_build_device_info_includes_mac_as_connection():
    """Device grouping in HA: MAC becomes a ``mac`` connection."""
    entry = _make_entry()
    info = build_device_info(entry, {"macAddress": "AA:BB:CC:DD:EE:FF", "model": "DS-2CD2143G2-I"})
    # HA normalizes to lowercase; we lowercase before adding.
    assert ("mac", "aa:bb:cc:dd:ee:ff") in info["connections"]
    assert info["model"] == "DS-2CD2143G2-I"
    assert info["manufacturer"] == "Hikvision"


def test_build_device_info_no_mac_still_has_identifier():
    """Without a MAC, the entry_id is still used as a unique identifier."""
    entry = _make_entry()
    info = build_device_info(entry, {"model": "DS-2CD2143G2-I"})
    # No MAC → no connections
    assert not info["connections"]
    # But the identifier is still present so HA groups entities by entry.
    assert (DOMAIN, entry.entry_id) in info["identifiers"]


def test_build_device_info_uses_friendly_name_when_present():
    """deviceName from /deviceInfo becomes the HA device name."""
    entry = _make_entry()
    info = build_device_info(entry, {"deviceName": "Front Door Camera"})
    assert info["name"] == "Front Door Camera"


def test_build_device_info_falls_back_to_host_when_no_device_name():
    entry = _make_entry(host="192.168.10.42")
    info = build_device_info(entry, {})
    assert info["name"] == "Hikvision 192.168.10.42"


def test_build_device_info_includes_configuration_url():
    entry = _make_entry(host="192.168.10.42")
    info = build_device_info(entry, {})
    assert info["configuration_url"] == "https://192.168.10.42"


def test_build_device_info_includes_firmware_version():
    entry = _make_entry()
    info = build_device_info(entry, {"firmwareVersion": "V5.7.10 build 240120"})
    assert info["sw_version"] == "V5.7.10 build 240120"


def test_build_device_info_handles_missing_mac_in_xml():
    """An empty <macAddress> tag shouldn't crash — just skip the connection."""
    entry = _make_entry()
    info = build_device_info(entry, {"macAddress": ""})
    assert not info["connections"]


# ---- HikvisionISAPIEntity base ----


def test_hikvision_entity_picks_up_device_info_on_add():
    """async_added_to_hass should bind device_info from coordinator.data."""
    entity = _make_entity_with_data(
        {"model": "DS-2CD2143G2-I", "macAddress": "00:11:22:33:44:55"}
    )
    # Pre-condition: device_info not set
    assert entity._attr_device_info is None

    # Run the async lifecycle (no real HA, just invoke the coroutine)
    import asyncio
    asyncio.run(entity.async_added_to_hass())

    # Post-condition: device_info now has a model + identifier
    assert entity._attr_device_info is not None
    assert entity._attr_device_info["model"] == "DS-2CD2143G2-I"
    assert ("mac", "00:11:22:33:44:55") in entity._attr_device_info["connections"]


def test_hikvision_entity_handles_no_data_yet():
    """If the coordinator hasn't refreshed yet, device_info stays None."""
    entity = _make_entity_with_data(None)
    import asyncio
    asyncio.run(entity.async_added_to_hass())
    # Still None — first refresh hasn't happened yet.
    assert entity._attr_device_info is None


def test_hikvision_entity_rebinds_device_info_on_coordinator_update():
    """When the coordinator's data refreshes, device_info follows."""
    entity = _make_entity_with_data(None)
    import asyncio

    # First add: nothing
    asyncio.run(entity.async_added_to_hass())
    assert entity._attr_device_info is None

    # Now data arrives — replace the mock coordinator's `.data` with a
    # HikvisionISAPIData carrying device_info.
    from custom_components.hikvision_isapi_performance.coordinator import (
        HikvisionISAPIData,
    )
    entity.coordinator.data = HikvisionISAPIData(
        device_info={"model": "DS-2CD2143G2-I"},
        system_status={},
        channels=[],
        capabilities={},
    )
    entity._handle_coordinator_update()
    assert entity._attr_device_info is not None
    assert entity._attr_device_info["model"] == "DS-2CD2143G2-I"


# ---- helpers ----


def _make_entry(
    host: str = "192.168.10.72",
    entry_id: str = "test_entry_id",
) -> Any:
    """Build a minimal mock ConfigEntry for build_device_info."""
    entry = _MagMock()
    entry.entry_id = entry_id
    entry.data = {"host": host}
    return entry


def _make_entity_with_data(device_info: dict[str, Any] | None) -> HikvisionISAPIEntity:
    """Build a HikvisionISAPIEntity with a mock coordinator carrying device_info."""
    from custom_components.hikvision_isapi_performance.coordinator import (
        HikvisionISAPIData,
    )

    coordinator = _MagMock()
    coordinator._data = device_info
    if device_info is None:
        coordinator.data = None
    else:
        coordinator.data = HikvisionISAPIData(
            device_info=device_info,
            system_status={},
            channels=[],
            capabilities={},
        )

    entry = _make_entry()

    # HikvisionISAPIEntity needs a no-op ``available`` property; we subclass
    # minimally here so we don't pull in any actual HA platform base class.
    class _TestEntity(HikvisionISAPIEntity):
        pass

    return _TestEntity(coordinator, entry)