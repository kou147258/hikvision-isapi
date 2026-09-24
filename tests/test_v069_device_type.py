"""Tests for v0.6.9 device-type-aware channel endpoint selection.

Pre-v0.6.9 the coordinator hard-coded the NVR-style endpoint
``/ISAPI/ContentMgmt/InputProxy/channels`` for ALL device types.
On IPCs (which the user has at 10.18.176.10 / 10.18.176.65) that
endpoint returns HTTP 403 with empty WWW-Authenticate, leaving the
channels list empty for every IPC and no per-channel entities
(camera / recording switch / online-recording-motion binary
sensors). Device-level sensors worked because deviceInfo /
systemStatus endpoints are device-type-agnostic.

v0.6.9 fix: after fetching deviceInfo, the coordinator picks the
correct channel-list endpoint by device type:

- **IPC** → ``/ISAPI/Streaming/channels`` (returns
  ``<StreamingChannelList>`` wrapper with ``<StreamingChannel>``
  children, id-only fields).
- **NVR / DVR** → ``/ISAPI/ContentMgmt/InputProxy/channels``
  (returns ``<InputProxyChannelList>`` wrapper).

Per-channel status is similarly device-type-aware. Fallbacks cover
firmware quirks.
"""

from __future__ import annotations

from xml.etree import ElementTree as ET

from custom_components.hikvision_isapi_performance.coordinator import (
    _parse_streaming_channels_list,
)


def test_parse_streaming_channels_list_extracts_id_and_name():
    """v0.6.9: streaming-channel XML parser (IPC side)."""
    xml = """<StreamingChannelList>
        <StreamingChannel>
            <id>1</id>
            <videoInputChannelID>1</videoInputChannelID>
            <name>Front Door</name>
        </StreamingChannel>
        <StreamingChannel>
            <id>2</id>
            <videoInputChannelID>2</videoInputChannelID>
            <name>Side Gate</name>
        </StreamingChannel>
    </StreamingChannelList>"""
    root = ET.fromstring(xml)
    chans = _parse_streaming_channels_list(root)
    assert len(chans) == 2
    assert chans[0]["id"] == "1"
    assert chans[0]["name"] == "Front Door"
    assert chans[1]["id"] == "2"
    assert chans[1]["name"] == "Side Gate"


def test_parse_streaming_channels_list_handles_missing_name():
    """Missing <name> falls back to 'Channel {id}' placeholder."""
    xml = """<StreamingChannelList>
        <StreamingChannel>
            <id>3</id>
            <videoInputChannelID>3</videoInputChannelID>
        </StreamingChannel>
    </StreamingChannelList>"""
    chans = _parse_streaming_channels_list(ET.fromstring(xml))
    assert chans[0]["name"] == "Channel 3"


def test_parse_streaming_channels_list_falls_back_to_videoInputChannelID():
    """v0.6.9: some IPCs (Hikvision firmware quirks) omit <id> but
    provide <videoInputChannelID>. Use the latter as the canonical id."""
    xml = """<StreamingChannelList>
        <StreamingChannel>
            <videoInputChannelID>5</videoInputChannelID>
            <name>Doorbell</name>
        </StreamingChannel>
    </StreamingChannelList>"""
    chans = _parse_streaming_channels_list(ET.fromstring(xml))
    assert len(chans) == 1
    assert chans[0]["id"] == "5"
    assert chans[0]["name"] == "Doorbell"


def test_parse_streaming_channels_list_skips_entries_without_any_id():
    """Entries with neither <id> nor <videoInputChannelID> are dropped."""
    xml = """<StreamingChannelList>
        <StreamingChannel>
            <name>Unnamed</name>
        </StreamingChannel>
        <StreamingChannel>
            <id>1</id>
            <name>Real</name>
        </StreamingChannel>
    </StreamingChannelList>"""
    chans = _parse_streaming_channels_list(ET.fromstring(xml))
    assert len(chans) == 1
    assert chans[0]["id"] == "1"


def test_parse_streaming_channels_list_handles_empty_root():
    assert _parse_streaming_channels_list(None) == []
    assert _parse_streaming_channels_list(
        ET.fromstring("<StreamingChannelList/>")
    ) == []


def test_parse_streaming_channels_list_reads_optional_online_recording():
    """When present, <online> and <recordStatus> are mapped correctly.
    These fields are not always present on /Streaming/channels
    responses — they're more reliably returned by the per-channel
    status endpoint — but the parser accepts them when there.
    """
    xml = """<StreamingChannelList>
        <StreamingChannel>
            <id>1</id>
            <videoInputChannelID>1</videoInputChannelID>
            <online>true</online>
            <recordStatus>recording</recordStatus>
        </StreamingChannel>
    </StreamingChannelList>"""
    chans = _parse_streaming_channels_list(ET.fromstring(xml))
    assert chans[0]["online"] is True
    assert chans[0]["recording"] is True


# ---- Device-type-aware endpoint constants ----


def test_endpoint_constants_distinguish_ipc_vs_nvr():
    """v0.6.9: the constants needed for device-type-aware endpoint
    selection are all defined in const.py."""
    from custom_components.hikvision_isapi_performance.const import (
        ISAPI_INPUT_PROXY_CHANNELS,
        ISAPI_INPUT_PROXY_CHANNELS_STATUS,
        ISAPI_STREAMING_CHANNELS,
        ISAPI_STREAMING_CHANNELS_STATUS,
    )
    # IPC-side endpoints
    assert ISAPI_STREAMING_CHANNELS == "/ISAPI/Streaming/channels"
    assert "{id}" in ISAPI_STREAMING_CHANNELS_STATUS
    assert "/Streaming/channels/" in ISAPI_STREAMING_CHANNELS_STATUS
    # NVR-side endpoints
    assert ISAPI_INPUT_PROXY_CHANNELS == "/ISAPI/ContentMgmt/InputProxy/channels"
    assert "{id}" in ISAPI_INPUT_PROXY_CHANNELS_STATUS
    assert "/ContentMgmt/InputProxy/channels/" in ISAPI_INPUT_PROXY_CHANNELS_STATUS
    # The two families must be distinct endpoints.
    assert ISAPI_STREAMING_CHANNELS != ISAPI_INPUT_PROXY_CHANNELS
    assert ISAPI_STREAMING_CHANNELS_STATUS != ISAPI_INPUT_PROXY_CHANNELS_STATUS