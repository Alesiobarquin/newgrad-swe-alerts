"""Vendor-agnostic Instagram story JSON -> StoryAsset."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.models import MediaType, StoryAsset

logger = logging.getLogger(__name__)

_STORY_ID_KEYS = ("id", "storyId", "story_id", "pk", "pk_id")
_IMAGE_KEYS = (
    "displayUrl",
    "display_url",
    "thumbnail_url",
    "thumbnailUrl",
    "image_url",
    "imageUrl",
    "mediaUrl",
    "media_url",
    "url",
)
_VIDEO_KEYS = ("videoUrl", "video_url", "video", "video_versions")
_CAPTION_KEYS = ("caption", "extractedCaption", "text", "title")
_TAKEN_AT_KEYS = ("taken_at", "takenAt", "timestamp", "created_at", "createdAt")


def normalize_payload(payload: Any, *, username: str, items_path: str = "") -> list[StoryAsset]:
    items = _coerce_story_list(payload, items_path)
    stories: list[StoryAsset] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            stories.append(_normalize_item(item, username=username))
        except ValueError as exc:
            logger.warning("skipping unnormalizable story: %s", exc)
    return stories


def _coerce_story_list(payload: Any, items_path: str) -> list[dict[str, Any]]:
    node = _get_path(payload, items_path) if items_path else payload
    if node is None:
        node = payload
    if isinstance(node, list):
        return _flatten_wrappers(node)
    if isinstance(node, dict):
        for key in ("stories", "items", "data", "results", "reels", "tray"):
            inner = node.get(key)
            if isinstance(inner, list):
                return _flatten_wrappers(inner)
        if _looks_like_story(node):
            return [node]
    return []


def _flatten_wrappers(items: list[Any]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        nested = item.get("stories")
        if isinstance(nested, list) and nested and not _looks_like_story(item):
            flattened.extend(entry for entry in nested if isinstance(entry, dict))
            continue
        flattened.append(item)
    return flattened


def _normalize_item(item: dict[str, Any], *, username: str) -> StoryAsset:
    story_id = _first_str(item, *_STORY_ID_KEYS)
    if not story_id:
        raise ValueError("story payload missing id/pk")

    image_urls = _collect_image_urls(item)
    video_url = _first_video_url(item)
    media_type = _infer_media_type(item, image_urls=image_urls, video_url=video_url)
    owner = (
        _first_str(item, "username", "scraped_username", "ownerUsername")
        or _nested_str(item, "user", "username")
        or username
    )
    return StoryAsset(
        story_id=str(story_id),
        username=str(owner).lstrip("@"),
        taken_at=_parse_taken_at(item),
        media_type=media_type,
        image_url=image_urls[0] if image_urls else None,
        video_url=video_url,
        extra_frame_urls=image_urls[1:],
        link_urls=_collect_links(item),
        caption=_first_str(item, *_CAPTION_KEYS),
        raw=item,
    )


def _infer_media_type(item: dict[str, Any], *, image_urls: list[str], video_url: str | None) -> MediaType:
    raw_type = item.get("media_type")
    if raw_type == 2 or str(item.get("mediaType") or item.get("media_type") or "").lower() in {
        "video",
        "2",
    }:
        return "video"
    if raw_type == 1 or str(item.get("mediaType") or "").lower() in {"image", "photo", "1"}:
        return "image"
    if video_url:
        return "video"
    if image_urls:
        return "image"
    return "unknown"


def _collect_image_urls(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for key in _IMAGE_KEYS:
        value = item.get(key)
        if isinstance(value, str) and _is_http(value):
            urls.append(value)
    versions = item.get("image_versions2")
    if isinstance(versions, dict):
        candidates = versions.get("candidates")
        if isinstance(candidates, list):
            for candidate in candidates:
                if isinstance(candidate, dict):
                    url = candidate.get("url")
                    if isinstance(url, str) and _is_http(url):
                        urls.append(url)
    return _dedupe(urls)


def _first_video_url(item: dict[str, Any]) -> str | None:
    for key in ("videoUrl", "video_url", "video"):
        value = item.get(key)
        if isinstance(value, str) and _is_http(value):
            return value
    versions = item.get("video_versions")
    if isinstance(versions, list):
        for candidate in versions:
            if isinstance(candidate, dict):
                url = candidate.get("url")
                if isinstance(url, str) and _is_http(url):
                    return url
            elif isinstance(candidate, str) and _is_http(candidate):
                return candidate
    return None


def _collect_links(item: dict[str, Any]) -> list[str]:
    links: list[str] = []
    for key in ("links", "linkUrls", "link_urls"):
        value = item.get(key)
        if isinstance(value, list):
            for entry in value:
                if isinstance(entry, str) and _is_http(entry):
                    links.append(entry)
                elif isinstance(entry, dict):
                    url = entry.get("url") or entry.get("link")
                    if isinstance(url, str) and _is_http(url):
                        links.append(url)
        elif isinstance(value, str) and _is_http(value):
            links.append(value)

    stickers = item.get("story_link_stickers") or item.get("storyLinkStickers") or []
    if isinstance(stickers, list):
        for sticker in stickers:
            if not isinstance(sticker, dict):
                continue
            nested = sticker.get("story_link") or sticker.get("storyLink") or sticker
            if isinstance(nested, dict):
                url = nested.get("url") or nested.get("link")
                if isinstance(url, str) and _is_http(url):
                    links.append(url)
    direct = item.get("linkUrl") or item.get("link_url")
    if isinstance(direct, str) and _is_http(direct):
        links.append(direct)
    return _dedupe(links)


def _parse_taken_at(item: dict[str, Any]) -> datetime | None:
    for key in _TAKEN_AT_KEYS:
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 10_000_000_000:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        if isinstance(value, str):
            raw = value.strip()
            if not raw:
                continue
            try:
                if raw.isdigit():
                    return datetime.fromtimestamp(float(raw), tz=timezone.utc)
                return datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
    return None


def _get_path(payload: Any, path: str) -> Any:
    if not path:
        return payload
    current = payload
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


def _looks_like_story(item: dict[str, Any]) -> bool:
    return any(key in item for key in _STORY_ID_KEYS)


def _first_str(item: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            return str(value).strip()
    return None


def _nested_str(item: dict[str, Any], *path: str) -> str | None:
    current: Any = item
    for part in path:
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    if isinstance(current, str) and current.strip():
        return current.strip()
    return None


def _is_http(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _dedupe(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique
