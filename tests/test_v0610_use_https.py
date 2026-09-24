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


def test_default_use_https_443_returns_true():
    """v0.6.10: port 443 → HTTPS by default (most common case)."""
    assert _default_use_https(443) is True


def test_default_use_https_80_returns_false():
    """v0.6.10: port 80 → HTTP by default."""
    assert _default_use_https(80) is False


def test_default_use_https_non_standard_returns_false():
    """v0.6.10: any non-443 port → HTTP default.

    The user can override if their setup uses HTTPS on a non-standard
    port. The default just gives the most-common-case behavior.
    """
    for port in (8080, 8443, 8000, 8001):
        assert _default_use_https(port) is False, (
            f"port {port} should default to HTTP"
        )


# ---- ISAPIClient URL construction ----


def test_isapi_client_builds_https_url_when_use_https_true():
    """v0.6.10: use_https=True → https:// scheme."""
    client = ISAPIClient(
        host="10.18.176.10", username="admin", password="pw",
        use_https=True,
    )
    assert client.base_url == "https://10.18.176.10:443"


def test_isapi_client_builds_http_url_when_use_https_false():
    """v0.6.10: use_https=False → http:// scheme even on port 443.

    This is the user's bug case: their Hikvision device serves ISAPI
    over plain HTTP on port 443.
    """
    client = ISAPIClient(
        host="10.18.176.10", username="admin", password="pw",
        port=443, use_https=False,
    )
    assert client.base_url == "http://10.18.176.10:443"


def test_isapi_client_use_https_default_is_true():
    """v0.6.10: omitting use_https still defaults to HTTPS (back-compat)."""
    client = ISAPIClient(
        host="10.18.176.10", username="admin", password="pw",
    )
    assert client.base_url == "https://10.18.176.10:443"
    assert client._use_https is True


def test_isapi_client_http_on_port_80():
    """v0.6.10: port 80 + http → http://host:80."""
    client = ISAPIClient(
        host="10.18.176.10", username="admin", password="pw",
        port=80, use_https=False,
    )
    assert client.base_url == "http://10.18.176.10:80"


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