"""Constants for the Hikvision ISAPI integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "hikvision_isapi"
MANUFACTURER: Final = "Hikvision"

# ---- Polling defaults ----

DEFAULT_PORT: Final = 443
DEFAULT_SCAN_INTERVAL: Final = 30  # seconds
MIN_SCAN_INTERVAL: Final = 5
MAX_SCAN_INTERVAL: Final = 300

# ---- Network ----

# Connection timeout for a single HTTP request (seconds)
DEFAULT_REQUEST_TIMEOUT: Final = 10

# ---- Config flow fields ----

CONF_HOST: Final = "host"
CONF_PORT: Final = "port"
CONF_USERNAME: Final = "username"
CONF_PASSWORD: Final = "password"
CONF_VERIFY_SSL: Final = "verify_ssl"
CONF_SCAN_INTERVAL: Final = "scan_interval"

# ---- ISAPI paths ----

ISAPI_SYSTEM_DEVICE_INFO: Final = "/ISAPI/System/deviceInfo"
ISAPI_SYSTEM_STATUS: Final = "/ISAPI/System/status"
ISAPI_SYSTEM_TIME: Final = "/ISAPI/System/time"
ISAPI_SYSTEM_NETWORK_INTERFACES: Final = (
    "/ISAPI/System/Network/interfaces"
)
ISAPI_SYSTEM_REBOOT: Final = "/ISAPI/System/reboot"

# Channel / camera streams
ISAPI_INPUT_PROXY_CHANNELS: Final = (
    "/ISAPI/ContentMgmt/InputProxy/channels"
)
ISAPI_STREAMING_CHANNELS: Final = "/ISAPI/Streaming/channels"

# Storage (NVR / DVR)
ISAPI_CONTENT_MGMT_STORAGE: Final = "/ISAPI/ContentMgmt/storage"
ISAPI_CONTENT_MGMT_HDD: Final = "/ISAPI/ContentMgmt/hdd"

# Event streaming
ISAPI_EVENT_ALERT_STREAM: Final = (
    "/ISAPI/Event/notification/alertStream"
)

# PTZ
ISAPI_PTZ_CTRL_CHANNELS: Final = "/ISAPI/PTZCtrl/channels"

# ---- Capability flags returned by deviceInfo ----
# We use these to decide which platforms to register per device.
CAP_PTZ: Final = "PTZ"
CAP_VIDEO_INPUT: Final = "videoInput"
CAP_INGRESS_ALARM: Final = "ingressAlarm"
