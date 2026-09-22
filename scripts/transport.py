#!/usr/bin/env python3
"""HTTPS client with CA validation, no ambient proxy and canonical-name resolution."""

from __future__ import annotations

import base64
import http.client
import json
import re
import socket
import ssl
import urllib.parse


def basic_auth_headers(username: str, password: str) -> dict[str, str]:
    encoded = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def validate_https_origin(url: str, canonical_host: str) -> None:
    """Accept only https://<canonical_host>[:port] with a valid port."""
    if not canonical_host:
        raise ValueError("canonical service host is missing")
    match = re.fullmatch(rf"https://{re.escape(canonical_host)}(?::([0-9]+))?", url or "")
    if not match:
        raise ValueError("service URL is not the required canonical HTTPS origin")
    if match.group(1) is not None and not 1 <= int(match.group(1)) <= 65535:
        raise ValueError("service URL port is outside the valid range")


class ResolvedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a fixed address while validating TLS against the canonical host."""

    def __init__(self, host: str, port: int, *, address: str, context: ssl.SSLContext, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=context)
        self.address = address

    def connect(self) -> None:
        raw = socket.create_connection((self.address, self.port), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class UrlTransport:
    def __init__(self, base_url: str, ca_file: str, *, headers: dict[str, str] | None = None,
                 timeout: float = 30, resolve: str | None = None) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("base URL must be https://host[:port][/prefix]")
        self.base_url = base_url.rstrip("/")
        self.host = parsed.hostname
        self.port = parsed.port or 443
        self.prefix = parsed.path.rstrip("/")
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.resolve = resolve
        self.context = ssl.create_default_context(cafile=ca_file)

    def _connection(self) -> http.client.HTTPSConnection:
        if self.resolve:
            return ResolvedHTTPSConnection(self.host, self.port, address=self.resolve,
                                           context=self.context, timeout=self.timeout)
        return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self.context)

    def request(self, method: str, path: str, fields: dict[str, object] | None = None,
                expected: tuple[int, ...] = (200,), *, body: bytes | str | None = None,
                content_type: str | None = None) -> tuple[int, object, dict[str, str]]:
        headers = dict(self.headers)
        data: bytes | None = None
        if fields is not None:
            data = urllib.parse.urlencode(fields, doseq=True).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        elif body is not None:
            data = body if isinstance(body, bytes) else body.encode()
            headers["Content-Type"] = content_type or "application/xml"
        connection = self._connection()
        try:
            connection.request(method, self.prefix + path, body=data, headers=headers)
            response = connection.getresponse()
            status = response.status
            raw = response.read()
            response_headers = dict(response.getheaders())
        finally:
            connection.close()
        if status not in expected:
            raise RuntimeError(f"{method} {path} returned HTTP {status}")
        payload: object
        if not raw:
            payload = {}
        else:
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = raw
        return status, payload, response_headers
