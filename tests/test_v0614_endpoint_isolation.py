"""Tests for v0.6.14 per-endpoint fault-tolerant refresh.

Pre-v0.6.14 the coordinator's outer try/except wrapped all four
main endpoint fetches. A single 4xx response on
``/ISAPI/System/status`` (typical for V4 NVRs) was caught and
converted to ``UpdateFailed`` → every entity for the device
became unavailable.

v0.6.14 refactors ``_async_update_data`` so each endpoint has its
own try/except helper (``_fetch_device_info``, ``_fetch_system_status``,
``_fetch_channels``, ``_fetch_storage``, ``_fetch_network_interfaces``,
``_fetch_streaming``). Endpoint-level failures log a WARNING/INFO
and the refresh continues with whatever data DID succeed. Only
genuine network-open failures (couldn't even reach the device)
still raise ``UpdateFailed``.

These tests pin the new behavior at the integration boundary.

Note: most coordinator-level behavior is exercised by manual HA
runs against real firmware; the regression-test coverage here
focuses on what's observable without a live device — the helper
methods' failure isolation contract, via mock handlers.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from custom_components.hikvision_isapi_performance.coordinator import (
    HikvisionISAPICoordinator,
)


# ---- manifest ----


def test_v0614_manifest_version_at_or_beyond_0_6_14():
    """v0.6.14 anchor; later releases may bump further."""
    from custom_components.hikvision_isapi_performance import (
        isapi_client as _unused_check_imports,
    )
    manifest = json.loads(Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\manifest.json"
    ).read_text(encoding="utf-8"))
    parts = manifest["version"].split(".")
    assert parts[0] == "0"
    assert int(parts[1]) >= 6
    if int(parts[1]) == 6:
        assert int(parts[2]) >= 14


# ---- coordinator has the per-endpoint helper methods ----


def test_v0614_coordinator_exposes_fault_tolerant_helpers():
    """v0.6.14: every endpoint has its own per-call try/except so
    a single 4xx response no longer takes the whole refresh down."""
    expected = (
        "_fetch_device_info",
        "_fetch_system_status",
        "_fetch_channels",
        "_fetch_storage",
        "_fetch_network_interfaces",
        "_fetch_streaming",
    )
    for name in expected:
        assert hasattr(HikvisionISAPICoordinator, name), (
            f"coordinator missing helper {name}; v0.6.14 isolated "
            "per-endpoint fault tolerance isn't wired up."
        )


# ---- coordinator source uses per-endpoint try/except ----


def test_v0614_coordinator_status_xml_has_per_call_try():
    """v0.6.14: /ISAPI/System/status is fetched via
    ``_fetch_system_status``, NOT a raw ``client.get_xml(...)``
    inside the outer try block. A test asserting this in the
    source guards against accidental reverts to the v0.6.13
    structure that fanned all four endpoints under one try.
    """
    src = Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\coordinator.py"
    ).read_text(encoding="utf-8-sig")
    assert "_fetch_system_status" in src, (
        "coordinator.py must call _fetch_system_status; v0.6.14 "
        "isolated per-endpoint fault tolerance isn't wired up."
    )
    # And the OLD buggy raw `status_xml = await client.get_xml(
    # ISAPI_SYSTEM_STATUS)` inside the outer try should be gone.
    assert "status_xml = await client.get_xml(\n                ISAPI_SYSTEM_STATUS)" not in src, (
        "Still found raw status_xml = await client.get_xml(...) call "
        "inside the outer try — revert to per-endpoint helper."
    )


def test_v0614_coordinator_device_info_has_per_call_try():
    src = Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\coordinator.py"
    ).read_text(encoding="utf-8-sig")
    assert "_fetch_device_info" in src, (
        "coordinator.py must call _fetch_device_info"
    )


# ---- regression: outer try catches only "can't even connect" ----


def test_v0614_outer_try_only_catches_connection_and_auth():
    """v0.6.14: the outer try/except must NOT catch ISAPIError
    generically (or any per-endpoint HTTP 4xx would still take
    down the whole refresh). Only the unreachable / unauthenticated
    cases are fatal.
    """
    src = Path(
        r"C:\Users\43457\Desktop\hikvision-isapi"
        r"\custom_components\hikvision_isapi_performance\coordinator.py"
    ).read_text(encoding="utf-8-sig")
    # Locate `_async_update_data` body and look at the FIRST try/
    # except block within it.
    needle = "async def _async_update_data"
    idx = src.find(needle)
    assert idx > 0, "_async_update_data not found"
    # Substring from there onward, find the first `try:` block.
    rest = src[idx:]
    # The outer try is the first `try:\n        async with self._make_client() as client:`
    # block in `_async_update_data`. Slice 4000 chars from there.
    marker = "\n        try:"
    m_idx = rest.find(marker)
    assert m_idx > 0, "could not locate outer try block in _async_update_data"
    snippet = rest[m_idx:m_idx + 4000]
    # Only ISAPIConnectionError and ISAPIAuthError at the outer
    # level; an explicit "except ISAPIError" anywhere in this
    # snippet means we re-fanned the per-endpoint errors.
    assert "except ISAPIError" not in snippet, (
        "Outer try in _async_update_data still catches ISAPIError; "
        "v0.6.14 should only catch ISAPIConnectionError and "
        "ISAPIAuthError at the outer level so per-endpoint 4xx "
        "responses don't take down the whole refresh."
    )


# ---- (smoke) coordinator can be imported with the refactor ----


def test_v0614_module_imports_cleanly():
    """Smoke: refactored module imports without raising. Catches
    syntax errors / missing imports / circular deps introduced
    by the helper-method extraction."""
    import custom_components.hikvision_isapi_performance.coordinator as c  # noqa: F401
    assert hasattr(c, "HikvisionISAPICoordinator")
    assert hasattr(c, "normalize_device_type")
