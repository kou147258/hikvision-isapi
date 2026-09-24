"""Minimal async ISAPI client for Hikvision NVR / IPC.

ISAPI is Hikvision's HTTP API on top of HTTP/1.1 + Digest auth + (optional)
session cookies + (typically) self-signed TLS. This module wraps
``aiohttp`` with a small surface tuned for the endpoints the integration
actually uses (``/ISAPI/System/deviceInfo``, ``/ISAPI/System/status``,
``/ISAPI/System/time``, ``/ISAPI/Streaming/channels/{id}/picture``,
``/ISAPI/ContentMgmt/InputProxy/channels/{id}/capabilities``, etc.).

The client is intentionally small and *not* a full ISAPI wrapper — every
endpoint we use is a simple GET/PUT with text/xml or application/json
body. We return raw bytes / text; the calling code parses.

aiohttp 3.8+ no longer ships a built-in ``DigestAuth`` class, so we
implement digest auth manually here. The flow:

1. Client sends GET, server responds 401 with
   ``WWW-Authenticate: Digest realm="...", nonce="...", qop="auth"``
2. Client computes HA1 / HA2 / response per RFC 7616
3. Client sends GET with ``Authorization: Digest ...``
4. Subsequent requests reuse the same nonce (server may also issue a
   new nonce which we re-handshake on)

This is the standard RFC 2617 / RFC 7616 digest auth flow that
Hikvision implements.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
from typing import Any
from xml.etree import ElementTree as ET

import aiohttp
from aiohttp import ClientError, ClientResponse, ClientSession

_LOGGER = logging.getLogger(__name__)


class ISAPIAuthError(Exception):
    """Raised when the device rejects our credentials (HTTP 401/403)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class ISAPIConnectionError(Exception):
    """Raised on network / TLS / timeout failures."""


class ISAPIError(Exception):
    """Generic ISAPI failure (HTTP 4xx/5xx other than auth, parse errors)."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def _parse_digest_challenge(www_authenticate: str) -> dict[str, str] | None:
    """Parse a ``Digest`` WWW-Authenticate challenge into a dict of fields."""
    if not www_authenticate:
        return None
    m = re.match(
        r'^\s*Digest\s+(.*)$', www_authenticate, flags=re.IGNORECASE | re.DOTALL
    )
    if not m:
        return None
    body = m.group(1)
    out: dict[str, str] = {}
    for match in re.finditer(r'([a-zA-Z][a-zA-Z0-9_-]*)\s*=\s*(?:"([^"]*)"|([^,\s]+))', body):
        key = match.group(1)
        value = match.group(2) if match.group(2) is not None else match.group(3)
        out[key.lower()] = value
    return out


def _compute_digest_response(
    username: str,
    password: str,
    method: str,
    path: str,
    challenge: dict[str, str],
    nc_count: int,
    cnonce: str,
) -> str:
    """Compute the ``response`` value for RFC 2617 digest auth.

    Supports qop=auth (with nc, cnonce) and the legacy qop-less form.
    """
    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    qop = challenge.get("qop", "").strip('"').lower()
    algorithm = challenge.get("algorithm", "MD5").upper()

    ha1 = hashlib.md5(
        f"{username}:{realm}:{password}".encode("utf-8")
    ).hexdigest()
    ha2 = hashlib.md5(f"{method}:{path}".encode("utf-8")).hexdigest()

    if "auth" in qop:
        nc = f"{nc_count:08x}"
        response = hashlib.md5(
            f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode("utf-8")
        ).hexdigest()
    else:
        # Legacy RFC 2069 form (no qop)
        response = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode("utf-8")).hexdigest()

    return response


def _build_digest_header(
    username: str,
    password: str,
    method: str,
    path: str,
    challenge: dict[str, str],
) -> str:
    """Build a complete ``Authorization: Digest ...`` header value."""
    nc_count = 1
    cnonce = secrets.token_hex(8)
    response = _compute_digest_response(
        username, password, method, path, challenge, nc_count, cnonce
    )

    realm = challenge.get("realm", "")
    nonce = challenge.get("nonce", "")
    qop = challenge.get("qop", "").strip('"').lower()
    algorithm = challenge.get("algorithm", "MD5").upper()
    opaque = challenge.get("opaque", "")
    nc = f"{nc_count:08x}"

    parts = [
        f'username="{username}"',
        f'realm="{realm}"',
        f'nonce="{nonce}"',
        f'uri="{path}"',
        f"response={response}",  # response is hex, no quotes per RFC
        f'algorithm={algorithm}',
    ]
    if "auth" in qop:
        parts.append(f"qop=auth")
        parts.append(f"nc={nc}")
        parts.append(f'cnonce="{cnonce}"')
    if opaque:
        parts.append(f'opaque="{opaque}"')
    return "Digest " + ", ".join(parts)


class ISAPIClient:
    """Async ISAPI client.

    Use as an async context manager:

        async with ISAPIClient(host, username, password) as client:
            info = await client.get_xml("/ISAPI/System/deviceInfo")
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        *,
        port: int = 443,
        verify_ssl: bool = False,
        timeout: float = 10.0,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        self._timeout = timeout
        self._session: ClientSession | None = None
        # The DigestAuth machinery needs the same nonce reuse counter
        # for repeated requests. Hikvision typically issues a new
        # nonce per 401 challenge, so we just track that and re-handshake.
        self._base_url = f"http{'s' if port == 443 else 'http'}://{host}:{port}"

    async def __aenter__(self) -> "ISAPIClient":
        ssl = None if self._verify_ssl else False
        self._session = ClientSession(
            timeout=aiohttp.ClientTimeout(total=self._timeout),
            connector=aiohttp.TCPConnector(ssl=ssl, force_close=True),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    @property
    def base_url(self) -> str:
        return self._base_url

    async def get_text(self, path: str) -> str:
        """GET ``path`` and return the response body as text."""
        return await self._request("GET", path, expect_json=False)

    async def get_bytes(self, path: str) -> bytes:
        """GET ``path`` and return the response body as raw bytes."""
        if self._session is None:
            raise ISAPIConnectionError("Client not opened")
        url = f"{self._base_url}{path}"
        try:
            async with self._session.get(url) as resp:
                return await self._read_response_bytes(resp, path)
        except ISAPIAuthError:
            raise
        except (ClientError, asyncio.TimeoutError) as exc:
            raise ISAPIConnectionError(str(exc)) from exc

    async def get_xml(self, path: str) -> ET.Element:
        """GET ``path`` and return the parsed XML root element."""
        text = await self.get_text(path)
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            raise ISAPIError(f"invalid XML from {path}: {exc}") from exc

    async def put_text(self, path: str, body: str) -> str:
        """PUT ``body`` (XML) to ``path`` and return the response body as text."""
        return await self._request(
            "PUT", path, expect_json=False, body=body,
            content_type="application/xml; charset=\"UTF-8\"",
        )

    async def put_xml(self, path: str, body: str) -> ET.Element:
        """PUT ``body`` (XML) to ``path`` and return the parsed XML root element."""
        text = await self.put_text(path, body)
        try:
            return ET.fromstring(text)
        except ET.ParseError as exc:
            raise ISAPIError(f"invalid XML from {path}: {exc}") from exc

    async def _request(
        self,
        method: str,
        path: str,
        *,
        expect_json: bool,
        body: str | None = None,
        content_type: str | None = None,
    ) -> str:
        if self._session is None:
            raise ISAPIConnectionError("Client not opened")
        url = f"{self._base_url}{path}"
        headers: dict[str, str] = {}
        if body is not None and content_type:
            headers["Content-Type"] = content_type
        try:
            async with self._session.request(
                method, url, data=body, headers=headers
            ) as resp:
                return await self._read_response_text(resp, path, method)
        except ISAPIAuthError:
            raise
        except (ClientError, asyncio.TimeoutError) as exc:
            raise ISAPIConnectionError(str(exc)) from exc

    async def _read_response_text(
        self, resp: ClientResponse, path: str, method: str
    ) -> str:
        if resp.status in (401, 403):
            # Try Digest auth handshake.
            www_auth = resp.headers.get("WWW-Authenticate", "")
            challenge = _parse_digest_challenge(www_auth)
            if challenge is None:
                raise ISAPIAuthError(
                    f"HTTP {resp.status} on {resp.url}: "
                    f"unrecognized WWW-Authenticate: {www_auth[:120]}",
                    status_code=resp.status,
                )
            auth_header = _build_digest_header(
                self._username, self._password, method, path, challenge
            )
            # Retry once with the auth header.
            url = str(resp.url)
            try:
                async with self._session.request(
                    method, url, data=await resp.read(),
                    headers={"Authorization": auth_header, **(
                        {"Content-Type": content_type}
                        if content_type else {}
                    )},
                ) as retry_resp:
                    return await self._read_response_text(retry_resp, path, method)
            except ISAPIAuthError:
                raise
            except (ClientError, asyncio.TimeoutError) as exc:
                raise ISAPIConnectionError(str(exc)) from exc
        if resp.status >= 400:
            body = await resp.text()
            raise ISAPIError(
                f"HTTP {resp.status} on {resp.url}: {body[:200]}",
                status_code=resp.status,
            )
        return await resp.text()

    async def _read_response_bytes(
        self, resp: ClientResponse, path: str
    ) -> bytes:
        if resp.status in (401, 403):
            # For binary endpoints (camera image), retry once with auth.
            www_auth = resp.headers.get("WWW-Authenticate", "")
            challenge = _parse_digest_challenge(www_auth)
            if challenge is None:
                raise ISAPIAuthError(
                    f"HTTP {resp.status} on {resp.url}: "
                    f"unrecognized WWW-Authenticate: {www_auth[:120]}",
                    status_code=resp.status,
                )
            auth_header = _build_digest_header(
                self._username, self._password, "GET", path, challenge
            )
            url = str(resp.url)
            try:
                async with self._session.get(
                    url, headers={"Authorization": auth_header}
                ) as retry_resp:
                    if retry_resp.status in (401, 403):
                        raise ISAPIAuthError(
                            f"HTTP {retry_resp.status} on {retry_resp.url} after auth",
                            status_code=retry_resp.status,
                        )
                    if retry_resp.status >= 400:
                        body = await retry_resp.text()
                        raise ISAPIError(
                            f"HTTP {retry_resp.status} on {retry_resp.url}: {body[:200]}",
                            status_code=retry_resp.status,
                        )
                    return await retry_resp.read()
            except ISAPIAuthError:
                raise
            except (ClientError, asyncio.TimeoutError) as exc:
                raise ISAPIConnectionError(str(exc)) from exc
        if resp.status >= 400:
            body = await resp.text()
            raise ISAPIError(
                f"HTTP {resp.status} on {resp.url}: {body[:200]}",
                status_code=resp.status,
            )
        return await resp.read()
