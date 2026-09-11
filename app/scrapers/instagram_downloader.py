"""RapidAPI Instagram Downloader adapter for public account story media."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from app.config import Settings
from app.models import StoryAsset
from app.scrapers.base import ScraperError

logger = logging.getLogger(__name__)

_MAX_RETRIES = 4
_MAX_BACKOFF_SECONDS = 30.0


class InstagramDownloaderScraper:
    """Fetch current stories through the downloader's ``/convert`` endpoint.

    The provider returns media URLs without Instagram story IDs. A deterministic
    ID derived from the CDN path keeps Redis deduplication stable while ignoring
    expiring query-string signatures.
    """

    def __init__(self, settings: Settings, http: httpx.Client) -> None:
        self._settings = settings
        self._http = http

    def fetch_stories(self, username: str) -> list[StoryAsset]:
        endpoint = f"https://{self._settings.rapidapi_host}/convert"
        response = _request_with_retry(
            self._http,
            endpoint,
            params={"url": f"https://www.instagram.com/stories/{username}/"},
            headers={
                "X-RapidAPI-Key": self._settings.rapidapi_key,
                "X-RapidAPI-Host": self._settings.rapidapi_host,
            },
        )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ScraperError("Instagram Downloader returned non-JSON payload") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("media"), list):
            raise ScraperError("Instagram Downloader response is missing the media list")

        stories: list[StoryAsset] = []
        seen_ids: set[str] = set()
        for item in payload["media"]:
            story = _normalize_media(item, username=username)
            if story is None or story.story_id in seen_ids:
                continue
            seen_ids.add(story.story_id)
            stories.append(story)
        return stories


def _normalize_media(item: Any, *, username: str) -> StoryAsset | None:
    if not isinstance(item, dict):
        return None
    media_url = item.get("url")
    if not isinstance(media_url, str) or not _is_http(media_url):
        return None
    raw_type = str(item.get("type") or "").strip().lower()
    media_type = "video" if raw_type == "video" else "image" if raw_type == "image" else "unknown"
    thumbnail = item.get("thumbnail")
    image_url = thumbnail if isinstance(thumbnail, str) and _is_http(thumbnail) else None
    if media_type == "image":
        image_url = media_url
    return StoryAsset(
        story_id=_synthetic_story_id(media_url),
        username=username.lstrip("@"),
        media_type=media_type,
        image_url=image_url,
        video_url=media_url if media_type == "video" else None,
        raw=item,
    )


def _synthetic_story_id(media_url: str) -> str:
    path = unquote(urlsplit(media_url).path)
    # The Instagram filename is stable even if the CDN host/path prefix changes.
    filename = PurePosixPath(path).name
    identity = filename or path
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return f"downloader:{digest}"


def _is_http(value: str) -> bool:
    return value.startswith("https://") or value.startswith("http://")


def _request_with_retry(
    http: httpx.Client,
    endpoint: str,
    *,
    params: dict[str, str],
    headers: dict[str, str],
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            response = http.get(endpoint, params=params, headers=headers)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 2**attempt
                wait = min(wait, _MAX_BACKOFF_SECONDS)
                logger.warning("Instagram Downloader 429; backing off %.1fs", wait)
                time.sleep(wait)
                last_error = ScraperError("Instagram Downloader rate limited (429)")
                continue
            if response.status_code >= 500:
                wait = min(2**attempt, _MAX_BACKOFF_SECONDS)
                logger.warning("Instagram Downloader %s; retrying in %.1fs", response.status_code, wait)
                time.sleep(wait)
                last_error = ScraperError(f"Instagram Downloader HTTP {response.status_code}")
                continue
            if response.status_code >= 400:
                raise ScraperError(f"Instagram Downloader HTTP {response.status_code}: {response.text[:300]}")
            return response
        except httpx.HTTPError as exc:
            last_error = exc
            wait = min(2**attempt, _MAX_BACKOFF_SECONDS)
            logger.warning("Instagram Downloader transport error (%s); retrying in %.1fs", exc, wait)
            time.sleep(wait)
    raise ScraperError("Instagram Downloader request failed after retries") from last_error
