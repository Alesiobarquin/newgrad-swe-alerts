"""Shared httpx client with pooling and conservative timeouts."""

from __future__ import annotations

import httpx

DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=5.0)
MEDIA_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
PUSHOVER_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


def build_http_client() -> httpx.Client:
    return httpx.Client(
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        headers={"User-Agent": "newgrad-swe-alerts/1.0"},
    )
