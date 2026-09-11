"""Gemini 1.5 Flash structured classifier."""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from app.config import Settings
from app.models import CLASSIFICATION_JSON_SCHEMA, Classification, StoryAsset

logger = logging.getLogger(__name__)

SYSTEM_INSTRUCTION = """You are a strict classifier for Instagram stories from a new-grad job drop account.

Return ONLY JSON matching the provided schema.

Set is_new_grad_swe=true ONLY when the story frame and/or visible sticker URLs clearly show a LIVE, currently open application for an entry-level, new-grad, university grad, or early-career Software Engineer / SDE / SWE role. This includes active "applications open", "drop", "apply now", or hiring-update stories that are specifically for that role family.

Set is_new_grad_swe=false for:
- lifestyle, travel, memes, selfies, food
- math/quant riddles, puzzles, brainteasers
- generic career advice, resume tips, interview coaching
- course, newsletter, Discord, or bootcamp promotions
- senior, staff, principal, manager, or intern-only roles unless new-grad SWE is also explicit
- non-SWE roles (PM, design, quant research-only, data science-only) unless SWE/SDE is explicit
- vague "we're hiring" with no new-grad SWE signal

Prefer sticker/link text over vibes. A greenhouse/lever/ashby/careers URL for new-grad SWE is a strong true.

urgency_score: 1=weak/unclear, 3=normal live opening, 5=apply-now / closes today / major drop.

company and role_title: best-effort; use null if unknown.
job_link: the apply URL if visible in stickers or on-frame text; otherwise null.
reason: one short sentence citing the visual/text evidence.
"""


class ClassificationError(Exception):
    """Raised when Gemini cannot produce a valid Classification after retries."""


class ClassifierConfigError(ClassificationError):
    """Raised when the configured Gemini model is unavailable."""


class GeminiClassifier:
    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self._settings = settings
        self._client = client
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=settings.gemini_api_key)
        self._rate_lock = threading.Lock()
        self._last_request_started: float | None = None

    def classify(self, asset: StoryAsset, image_bytes: bytes, mime_type: str) -> Classification:
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                self._wait_for_rate_slot()
                return self._generate(asset, image_bytes, mime_type)
            except ClassificationError as exc:
                last_error = exc
                logger.warning("classification attempt %s failed: %s", attempt + 1, exc)
                continue
            except Exception as exc:
                last_error = exc
                if attempt == 0 and _is_retryable(exc):
                    logger.warning("retryable Gemini error: %s", exc)
                    continue
                raise ClassificationError(str(exc)) from exc
        raise ClassificationError(str(last_error) if last_error else "classification failed")

    def _wait_for_rate_slot(self) -> None:
        interval = self._settings.gemini_min_request_interval_seconds
        if interval <= 0:
            return
        with self._rate_lock:
            now = time.monotonic()
            if self._last_request_started is not None:
                remaining = interval - (now - self._last_request_started)
                if remaining > 0:
                    logger.info("waiting %.1fs for Gemini quota pacing", remaining)
                    time.sleep(remaining)
            self._last_request_started = time.monotonic()

    def _generate(self, asset: StoryAsset, image_bytes: bytes, mime_type: str) -> Classification:
        from google.genai import types

        prompt = _user_prompt(asset)
        config = _build_generate_config(types)
        try:
            response = self._client.models.generate_content(
                model=self._settings.gemini_model,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime_type),
                    prompt,
                ],
                config=config,
            )
        except TypeError:
            response = self._client.models.generate_content(
                model=self._settings.gemini_model,
                contents=[prompt],
                config=config,
            )
            logger.warning("Gemini client rejected image parts; classified from text context only")

        raw_text = getattr(response, "text", None)
        if not raw_text:
            parsed = getattr(response, "parsed", None)
            if isinstance(parsed, Classification):
                return parsed
            if isinstance(parsed, dict):
                return Classification.model_validate(parsed)
            raise ClassificationError("Gemini returned empty text")

        try:
            data = json.loads(_strip_fences(raw_text))
        except json.JSONDecodeError as exc:
            raise ClassificationError(f"Gemini JSON parse failed: {raw_text[:400]}") from exc
        try:
            return Classification.model_validate(data)
        except Exception as exc:
            raise ClassificationError(f"Gemini schema validation failed: {exc}") from exc


def _build_generate_config(types: Any) -> Any:
    kwargs: dict[str, Any] = {
        "temperature": 0.0,
        "response_mime_type": "application/json",
        "system_instruction": SYSTEM_INSTRUCTION,
    }
    try:
        return types.GenerateContentConfig(response_json_schema=CLASSIFICATION_JSON_SCHEMA, **kwargs)
    except TypeError:
        return types.GenerateContentConfig(response_schema=Classification, **kwargs)


def _user_prompt(asset: StoryAsset) -> str:
    links = ", ".join(asset.link_urls) if asset.link_urls else "(none)"
    caption = asset.caption or "(none)"
    return (
        f"Username: @{asset.username}\n"
        f"Story ID: {asset.story_id}\n"
        f"Media type: {asset.media_type}\n"
        f"Link stickers / URLs: {links}\n"
        f"Caption: {caption}\n"
        "Classify this Instagram story frame for a live new-grad SWE job drop."
    )


def _strip_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.endswith("```"):
            stripped = stripped[: -3]
    return stripped.strip()


def _is_retryable(exc: BaseException) -> bool:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status in {429, 500, 502, 503, 504}:
        return True
    name = type(exc).__name__.lower()
    return "timeout" in name or "unavailable" in name or "server" in name
