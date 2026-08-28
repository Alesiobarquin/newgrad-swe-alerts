"""Apify Actor Standby HTTP adapter (no per-tick actor cold starts)."""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse, parse_qsl

import httpx

from app.config import Settings
from app.models import StoryAsset
from app.scrapers.base import ScraperError
from app.scrapers.normalize import normalize_payload

logger = logging.getLogger(__name__)

_MAX_RETRIES = 4
_MAX_BACKOFF_SECONDS = 30.0


class ApifyStandbyScraper:
    def __init__(self, settings: Settings, http: httpx.Client) -> None:
        self._settings = settings
        self._http = http

    def fetch_stories(self, username: str) -> list[StoryAsset]:
        url = self._settings.apify_standby_url
        headers = {
            "Authorization": f"Bearer {self._settings.apify_api_token}",
            "Accept": "application/json",
        }
        method = self._settings.apify_standby_method
        json_body: dict[str, Any] | None = None
        if method == "GET":
            url = _with_query(url, {"username": username, "usernames": username})
        else:
            json_body = {"username": username, "usernames": [username]}
            headers["Content-Type"] = "application/json"

        response = _request_with_retry(self._http, method=method, url=url, headers=headers, json_body=json_body)
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ScraperError("Apify Standby returned non-JSON payload") from exc
        return normalize_payload(payload, username=username, items_path="")


def _with_query(url: str, extra: dict[str, str]) -> str:
    parsed = urlparse(url)
    existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
    existing.update(extra)
    return urlunparse(parsed._replace(query=urlencode(existing)))


def _request_with_retry(
    http: httpx.Client,
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: dict[str, Any] | None,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            kwargs: dict[str, Any] = {"headers": headers}
            if json_body is not None:
                kwargs["json"] = json_body
            response = http.request(method, url, **kwargs)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 2**attempt
                wait = min(wait, _MAX_BACKOFF_SECONDS)
                logger.warning("Apify Standby 429; backing off %.1fs", wait)
                time.sleep(wait)
                last_error = ScraperError("Apify Standby rate limited (429)")
                continue
            if response.status_code >= 500:
                wait = min(2**attempt, _MAX_BACKOFF_SECONDS)
                logger.warning("Apify Standby %s; retrying in %.1fs", response.status_code, wait)
                time.sleep(wait)
                last_error = ScraperError(f"Apify Standby HTTP {response.status_code}")
                continue
            if response.status_code >= 400:
                raise ScraperError(f"Apify Standby HTTP {response.status_code}: {response.text[:300]}")
            return response
        except httpx.HTTPError as exc:
            last_error = exc
            wait = min(2**attempt, _MAX_BACKOFF_SECONDS)
            logger.warning("Apify Standby transport error (%s); retrying in %.1fs", exc, wait)
            time.sleep(wait)
    raise ScraperError("Apify Standby request failed after retries") from last_error
