"""Generic RapidAPI Instagram stories adapter."""

from __future__ import annotations

import json
import logging
import time
from string import Formatter
from typing import Any

import httpx

from app.config import Settings
from app.models import StoryAsset
from app.scrapers.base import ScraperError
from app.scrapers.normalize import normalize_payload

logger = logging.getLogger(__name__)

_MAX_RETRIES = 4
_MAX_BACKOFF_SECONDS = 30.0


class RapidApiScraper:
    def __init__(self, settings: Settings, http: httpx.Client) -> None:
        self._settings = settings
        self._http = http
        self._user_ids: dict[str, str] = {}

    def _needs_user_id(self) -> bool:
        blob = f"{self._settings.rapidapi_url_template}{self._settings.rapidapi_body_template}"
        return "{user_id}" in blob

    def _resolve_user_id(self, username: str) -> str:
        configured = self._settings.target_ig_user_id.strip()
        if configured:
            return configured
        cached = self._user_ids.get(username)
        if cached:
            return cached
        url = _format_template(
            self._settings.rapidapi_user_id_url_template,
            host=self._settings.rapidapi_host,
            username=username,
        )
        headers = {
            "X-RapidAPI-Key": self._settings.rapidapi_key,
            "X-RapidAPI-Host": self._settings.rapidapi_host,
        }
        response = _request_with_retry(
            self._http,
            method="GET",
            url=url,
            headers=headers,
            json_body=None,
            content=None,
        )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ScraperError("RapidAPI user_id lookup returned non-JSON") from exc
        user_id = _extract_user_id(payload)
        if not user_id:
            raise ScraperError(f"could not parse Instagram user_id from lookup payload: {str(payload)[:240]}")
        logger.info("resolved @%s to Instagram user_id %s (cached for this process)", username, user_id)
        self._user_ids[username] = user_id
        return user_id

    def fetch_stories(self, username: str) -> list[StoryAsset]:
        user_id = self._resolve_user_id(username) if self._needs_user_id() else ""
        url = _format_template(
            self._settings.rapidapi_url_template,
            host=self._settings.rapidapi_host,
            username=username,
            user_id=user_id,
        )
        headers = {
            "X-RapidAPI-Key": self._settings.rapidapi_key,
            "X-RapidAPI-Host": self._settings.rapidapi_host,
        }
        body: str | None = None
        json_body: dict[str, Any] | None = None
        if self._settings.rapidapi_body_template.strip():
            rendered = _format_template(
                self._settings.rapidapi_body_template,
                host=self._settings.rapidapi_host,
                username=username,
                user_id=user_id,
            )
            try:
                parsed = json.loads(rendered)
            except json.JSONDecodeError:
                body = rendered
            else:
                if isinstance(parsed, dict):
                    json_body = parsed
                else:
                    body = rendered

        response = _request_with_retry(
            self._http,
            method=self._settings.rapidapi_method,
            url=url,
            headers=headers,
            json_body=json_body,
            content=body,
        )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ScraperError("RapidAPI returned non-JSON payload") from exc
        return normalize_payload(
            payload,
            username=username,
            items_path=self._settings.rapidapi_items_path,
        )


def _extract_user_id(payload: Any) -> str | None:
    if isinstance(payload, bool):
        return None
    if isinstance(payload, (int, str)) and str(payload).isdigit():
        return str(payload)
    if isinstance(payload, dict):
        lowered = {str(key).lower(): value for key, value in payload.items()}
        for key in ("user_id", "userid", "pk", "id"):
            value = lowered.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, str)) and str(value).isdigit() and len(str(value)) >= 5:
                return str(value)
        for nested_key in ("data", "user", "result"):
            nested = lowered.get(nested_key)
            found = _extract_user_id(nested)
            if found:
                return found
    return None


def _format_template(template: str, **values: str) -> str:
    available = {item[1] for item in Formatter().parse(template) if item[1]}
    mapping = {key: values[key] for key in available if key in values}
    try:
        return template.format(**mapping)
    except KeyError as exc:
        raise ScraperError(f"URL/body template missing placeholder {exc}") from exc


def _request_with_retry(
    http: httpx.Client,
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: dict[str, Any] | None,
    content: str | None,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES):
        try:
            kwargs: dict[str, Any] = {"headers": headers}
            if json_body is not None:
                kwargs["json"] = json_body
            elif content is not None:
                kwargs["content"] = content
                kwargs.setdefault("headers", {})["Content-Type"] = "application/json"
            response = http.request(method, url, **kwargs)
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else 2**attempt
                wait = min(wait, _MAX_BACKOFF_SECONDS)
                logger.warning("RapidAPI 429; backing off %.1fs (attempt %s)", wait, attempt + 1)
                time.sleep(wait)
                last_error = ScraperError("RapidAPI rate limited (429)")
                continue
            if response.status_code >= 500:
                wait = min(2**attempt, _MAX_BACKOFF_SECONDS)
                logger.warning("RapidAPI %s; retrying in %.1fs", response.status_code, wait)
                time.sleep(wait)
                last_error = ScraperError(f"RapidAPI HTTP {response.status_code}")
                continue
            if response.status_code >= 400:
                raise ScraperError(f"RapidAPI HTTP {response.status_code}: {response.text[:300]}")
            return response
        except httpx.HTTPError as exc:
            last_error = exc
            wait = min(2**attempt, _MAX_BACKOFF_SECONDS)
            logger.warning("RapidAPI transport error (%s); retrying in %.1fs", exc, wait)
            time.sleep(wait)
    raise ScraperError("RapidAPI request failed after retries") from last_error
