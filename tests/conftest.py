"""Pytest config — stub homeassistant modules so unit tests can import the
integration without a full HA install.
"""

from __future__ import annotations

import os
import sys
import types

# Ensure the project root (where ``custom_components/`` lives) is on
# sys.path so the test file's absolute imports work. Pytest's default
# sys.path[0] is the tests/ directory; without this insert Python
# can't find ``custom_components.<integration_domain>``.
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


def _install_stub(name: str, attrs: dict[str, object] | None = None) -> None:
    if name in sys.modules:
        return
    module = types.ModuleType(name)
    for k, v in (attrs or {}).items():
        setattr(module, k, v)
    sys.modules[name] = module


# Top-level homeassistant namespace
_install_stub("homeassistant")
# Parent sub-namespace so ``from homeassistant.helpers import X`` works
_install_stub("homeassistant.helpers")

# Core HA constants
_install_stub("homeassistant.const", {
    "CONF_HOST": "host",
    "CONF_PASSWORD": "password",
    "CONF_PORT": "port",
    "CONF_SCAN_INTERVAL": "scan_interval",
    "CONF_USERNAME": "username",
    "PERCENTAGE": "%",
    "Platform": types.SimpleNamespace(
        CAMERA="camera",
        SENSOR="sensor",
        BINARY_SENSOR="binary_sensor",
        SWITCH="switch",
        BUTTON="button",
    ),
    "UnitOfTime": types.SimpleNamespace(SECONDS="s", HOURS="h"),
    "UnitOfInformation": types.SimpleNamespace(MEGABYTES="MB", GIGABYTES="GB"),
    "ATTR_DEVICE_ID": "device_id",
})

# Core
_install_stub("homeassistant.core", {
    "HomeAssistant": object,
    "ServiceCall": object,
    "callback": lambda f: f,
})

# Config entries
_install_stub("homeassistant.config_entries", {
    "ConfigEntry": object,
    "ConfigFlow": type("ConfigFlow", (), {}),
    "ConfigFlowResult": dict,
    "OptionsFlow": type("OptionsFlow", (), {}),
})

# Components — camera
_install_stub("homeassistant.components.camera", {
    "Camera": type("Camera", (), {}),
})

# Components — sensor
def _sed_init(self, **kwargs):
    """Stub __init__ that accepts any kwargs (real HA is a dataclass)."""
    for k, v in kwargs.items():
        setattr(self, k, v)


_install_stub("homeassistant.components.sensor", {
    "SensorDeviceClass": types.SimpleNamespace(DURATION="duration"),
    "SensorEntity": type("SensorEntity", (), {}),
    "SensorEntityDescription": type(
        "SensorEntityDescription", (), {"__init__": _sed_init},
    ),
    "SensorStateClass": types.SimpleNamespace(
        MEASUREMENT="measurement", TOTAL_INCREASING="total_increasing"
    ),
})

# Components — switch
_install_stub("homeassistant.components.switch", {
    "SwitchEntity": type("SwitchEntity", (), {}),
})

# Components — button
_install_stub("homeassistant.components.button", {
    "ButtonEntity": type("ButtonEntity", (), {}),
})

# Components — binary sensor
_install_stub("homeassistant.components.binary_sensor", {
    "BinarySensorDeviceClass": types.SimpleNamespace(
        CONNECTIVITY="connectivity",
        RUNNING="running",
        MOTION="motion",
    ),
    "BinarySensorEntity": type("BinarySensorEntity", (), {}),
})

# Exceptions
_install_stub("homeassistant.exceptions", {
    "ConfigEntryNotReady": Exception,
})

# Helpers — update coordinator
async def _stub_async_added_to_hass(self):
    """Default async_added_to_hass for stub CoordinatorEntity."""
    return None


_install_stub("homeassistant.helpers.update_coordinator", {
    "DataUpdateCoordinator": type(
        "DataUpdateCoordinator",
        (),
        {"__class_getitem__": classmethod(lambda cls, _x: cls)},
    ),
    "CoordinatorEntity": type(
        "CoordinatorEntity",
        (),
        {
            "__class_getitem__": classmethod(lambda cls, _x: cls),
            "__init__": lambda self, coordinator: setattr(self, "coordinator", coordinator),
            "async_added_to_hass": _stub_async_added_to_hass,
            "_handle_coordinator_update": lambda self: None,
        },
    ),
    "UpdateFailed": type("UpdateFailed", (Exception,), {}),
})

# Helpers — entity platform
_install_stub("homeassistant.helpers.entity_platform", {
    "AddEntitiesCallback": object,
})

# Helpers — aiohttp client
_install_stub("homeassistant.helpers.aiohttp_client", {
    "async_get_clientsession": lambda hass: None,
})

# Helpers — device registry
_install_stub("homeassistant.helpers.device_registry", {
    "async_get": lambda hass: types.SimpleNamespace(
        async_get=lambda device_id: None,
    ),
    # DeviceInfo is a TypedDict in real HA. The integration only reads
    # fields from the dict it builds, so a plain dict class is enough
    # for our unit tests.
    "DeviceInfo": dict,
})

# Helpers — config validation
_install_stub("homeassistant.helpers.config_validation", {
    "string": str,
})

# Helpers — selector
_ = sys.modules  # silence linter unused-import for sys
selector = types.ModuleType("homeassistant.helpers.selector")
selector.BooleanSelector = lambda: None
selector.NumberSelector = lambda *a, **k: None
selector.NumberSelectorConfig = lambda **k: None
selector.NumberSelectorMode = types.SimpleNamespace(BOX="box")
sys.modules["homeassistant.helpers.selector"] = selector
