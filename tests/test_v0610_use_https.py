"""Tests for v0.6.10 explicit HTTP/HTTPS scheme selection.

Pre-v0.6.10 the integration guessed scheme from port (443 → HTTPS).
That worked for most Hikvision devices but failed for two common
firmware configurations:

- ISAPI served over HTTP on port 443 (some V5.x firmware config).
- ISAPI served over HTTPS on a non-standard port (e.g. self-hosted
  reverse proxies).

v0.6.10 fix: add an explicit `use_https` boolean to the config
flow, persist in entry.data, and propagate through to the
ISAPIClient. The port-based guess becomes the default; the user
can override per device.

Note: users who already configured the integration under v0.6.9
or earlier will continue using HTTPS (the default). To switch
to HTTP for a device whose ISAPI listens on port 443 over HTTP,
remove and re-add the integration, OR wait for the options-flow
extension in a future release.
"""

from __future__ import annotations

from custom_components.hikvision_isapi_performance.config_flow import (
    _default_use_https,
)
from custom_components.hikvision_isapi_performance.isapi_client import (
    ISAPIClient,
)


# ---- Default-port inference ----


def test_default_use_https_always_false():
    """v0.6.11: port 443 → HTTP by default (was HTTPS in v0.6.10).

    Hikvision V5.x firmware serves ISAPI on plain HTTP by default
    on both port 80 and port 443. The HTTPS-on-443 setup requires
    explicit enablement in the device's web-server settings.
    Reference: the community ISAPI integration uses HTTP always
    (`base_url = f"http://{host}"`).
    """
    for port in (80, 443, 8080, 8443, 8000, 8001):
        assert _default_use_https(port) is False, (
            f"port {port} should default to HTTP"
        )


def test_default_use_https_independent_of_port():
    """v0.6.11: scheme defaults to HTTP regardless of port.

    Pre-v0.6.11: port 443 → True (HTTPS), other ports → False.
    v0.6.11: all ports → False (HTTP). User opts into HTTPS via
    the config-form checkbox.
    """
    assert _default_use_https(80) is False
    assert _default_use_https(443) is False
    assert _default_use_https(8080) is False


# ---- ISAPIClient URL construction ----


def test_isapi_client_builds_https_url_when_use_https_true():
    """v0.6.11: use_https=True → https:// scheme (opt-in)."""
    client = ISAPIClient(
        host="10.18.176.10", username="admin", password="pw",
        port=443, use_https=True,
    )
    assert client.base_url == "https://10.18.176.10:443"


def test_isapi_client_builds_http_url_when_use_https_false():
    """v0.6.11: use_https=False → http:// scheme even on port 443.

    This is the user's bug case: their Hikvision device serves ISAPI
    over plain HTTP on port 443.
    """
    client = ISAPIClient(
        host="10.18.176.10", username="admin", password="pw",
        port=443, use_https=False,
    )
    assert client.base_url == "http://10.18.176.10:443"


def test_isapi_client_use_https_default_is_false():
    """v0.6.11: omitting use_https defaults to HTTP (was True in v0.6.10)."""
    client = ISAPIClient(
        host="10.18.176.10", username="admin", password="pw",
    )
    # Default port is now 80 (was 443 pre-v0.6.11).
    assert client.base_url == "http://10.18.176.10:80"
    assert client._use_https is False


def test_isapi_client_default_port_changed_to_80():
    """v0.6.11: port default 443 → 80 (matches the V5.x firmware default)."""
    client = ISAPIClient(
        host="10.18.176.10", username="admin", password="pw",
    )
    assert client._port == 80


# ---- Const + config flow wiring ----


def test_use_https_constant_defined():
    """v0.6.10: CONF_USE_HTTPS constant exists in const.py."""
    from custom_components.hikvision_isapi_performance.const import (
        CONF_USE_HTTPS,
    )
    assert CONF_USE_HTTPS == "use_https"


def test_config_flow_user_schema_includes_use_https():
    """v0.6.10: USER_SCHEMA exposes use_https as an optional field."""
    from custom_components.hikvision_isapi_performance.config_flow import (
        USER_SCHEMA,
        CONF_USE_HTTPS,
    )
    # voluptuous schemas expose their keys via .schema
    assert CONF_USE_HTTPS in USER_SCHEMA.schema, (
        f"USER_SCHEMA missing {CONF_USE_HTTPS}; "
        "config flow won't show the HTTP/HTTPS toggle."
    )


def test_coordinator_accepts_use_https_kwarg():
    """v0.6.10: HikvisionISAPICoordinator takes use_https in __init__."""
    from custom_components.hikvision_isapi_performance.coordinator import (
        HikvisionISAPICoordinator,
    )
    import inspect

    sig = inspect.signature(HikvisionISAPICoordinator.__init__)
    assert "use_https" in sig.parameters, (
        "HikvisionISAPICoordinator.__init__ missing use_https"
    )