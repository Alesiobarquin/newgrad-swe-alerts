"""Quiet-hours routing, deterministic override, and Pushover dispatch."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import httpx

from app.config import Settings
from app.http import PUSHOVER_TIMEOUT
from app.models import Classification, QuietQueueItem
from app.state import RedisState

logger = logging.getLogger(__name__)

PUSHOVER_ENDPOINT = "https://api.pushover.net/1/messages.json"
PUSHOVER_MESSAGE_MAX = 1000
PUSHOVER_TITLE_MAX = 250
P2_RETRY_SECONDS = 30
P2_EXPIRE_SECONDS = 3600


class PushoverError(Exception):
    def __init__(self, message: str, *, sent_uncertain: bool = False) -> None:
        super().__init__(message)
        self.sent_uncertain = sent_uncertain


def in_quiet_hours(now: datetime, settings: Settings) -> bool:
    localized = now.astimezone(settings.tz) if now.tzinfo else now.replace(tzinfo=settings.tz)
    hour = localized.hour
    start = settings.quiet_hours_start_hour
    end = settings.quiet_hours_end_hour
    if start == end:
        return False
    if start > end:
        return hour >= start or hour < end
    return start <= hour < end


def is_override(classification: Classification, settings: Settings) -> bool:
    if classification.urgency_score >= 5:
        return True
    company = (classification.company or "").strip().lower()
    if not company:
        return False
    for listed in settings.override_company_set:
        if not listed:
            continue
        if listed == company:
            return True
        if re.search(rf"\b{re.escape(listed)}\b", company):
            return True
    return False


def poll_interval_seconds(now: datetime, settings: Settings) -> int:
    if in_quiet_hours(now, settings):
        return settings.quiet_poll_interval_seconds
    return settings.active_poll_interval_seconds


def truncate_message(text: str, limit: int = PUSHOVER_MESSAGE_MAX) -> str:
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


class Escalator:
    def __init__(self, settings: Settings, http: httpx.Client, state: RedisState) -> None:
        self._settings = settings
        self._http = http
        self._state = state

    def dispatch_positive(self, *, story_id: str, taken_at: datetime | None, classification: Classification, now: datetime) -> None:
        override = is_override(classification, self._settings)
        quiet = in_quiet_hours(now, self._settings)
        title, body, url = _format_drop(classification, story_id=story_id)
        if quiet and not override:
            item = QuietQueueItem(
                story_id=story_id,
                taken_at=taken_at,
                classification=classification,
                queued_at=now.astimezone(self._settings.tz),
            )
            self._state.push_quiet(item)
            self.send_priority_low(
                title=f"[Queued] {title}",
                message=body,
                url=url,
            )
            logger.info("queued story %s during quiet hours (override=%s)", story_id, override)
            return
        self.send_emergency(title=title, message=body, url=url)
        logger.info("emergency alert for story %s (override=%s quiet=%s)", story_id, override, quiet)

    def run_morning_burst(self) -> None:
        if not self._state.acquire_job_lock("morning_burst", ttl_seconds=300):
            logger.warning("morning burst skipped; lock held")
            return
        try:
            items = self._state.drain_quiet()
            if not items:
                logger.info("morning burst: quiet queue empty")
                return
            primary, overflow = pack_morning_burst(items)
            try:
                self.send_emergency(title="Overnight New Grad SWE Drops", message=primary)
            except PushoverError:
                self._state.restore_quiet(items)
                raise
            for extra in overflow:
                line = _format_overflow_line(extra)
                self.send_priority_low(title="Overnight drop (overflow)", message=line)
            logger.info("morning burst sent: %s in P2, %s overflow P-1", _count_primary(items, overflow), len(overflow))
        finally:
            self._state.release_job_lock("morning_burst")

    def send_debug(self, story_id: str, error: str) -> None:
        try:
            self.send_priority_low(
                title="[Debug] classification failed",
                message=truncate_message(f"story {story_id}: {error}"),
            )
        except PushoverError:
            logger.exception("failed to send classification debug ping for %s", story_id)

    def send_emergency(self, *, title: str, message: str, url: str | None = None) -> dict[str, Any]:
        return self._send(
            title=title,
            message=message,
            url=url,
            priority=2,
            retry=P2_RETRY_SECONDS,
            expire=P2_EXPIRE_SECONDS,
            sound="siren",
        )

    def send_priority_low(self, *, title: str, message: str, url: str | None = None) -> dict[str, Any]:
        return self._send(
            title=title,
            message=message,
            url=url,
            priority=-1,
            sound="none",
        )

    def _send(
        self,
        *,
        title: str,
        message: str,
        url: str | None,
        priority: int,
        sound: str,
        retry: int | None = None,
        expire: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "token": self._settings.pushover_app_token,
            "user": self._settings.pushover_user_key,
            "title": truncate_message(title, PUSHOVER_TITLE_MAX),
            "message": truncate_message(message, PUSHOVER_MESSAGE_MAX),
            "priority": priority,
            "sound": sound,
        }
        if url:
            payload["url"] = url[:512]
            payload["url_title"] = "Open listing"
        if retry is not None:
            payload["retry"] = retry
        if expire is not None:
            payload["expire"] = expire

        sent_uncertain = False
        try:
            response = self._http.post(PUSHOVER_ENDPOINT, data=payload, timeout=PUSHOVER_TIMEOUT)
            sent_uncertain = True
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise PushoverError("Pushover timed out", sent_uncertain=sent_uncertain) from exc
        except httpx.HTTPStatusError as exc:
            raise PushoverError(
                f"Pushover HTTP {exc.response.status_code}: {exc.response.text[:200]}",
                sent_uncertain=False,
            ) from exc
        except httpx.HTTPError as exc:
            raise PushoverError(f"Pushover transport error: {exc}", sent_uncertain=sent_uncertain) from exc

        try:
            body = response.json()
        except ValueError:
            body = {"status": 1}
        if body.get("status") != 1:
            raise PushoverError(f"Pushover rejected payload: {body}", sent_uncertain=False)
        return body


def pack_morning_burst(items: list[QuietQueueItem]) -> tuple[str, list[QuietQueueItem]]:
    header = f"Overnight new-grad SWE drops ({len(items)}):\n"
    included: list[QuietQueueItem] = []
    overflow: list[QuietQueueItem] = []
    body = header
    for item in items:
        line = _format_overflow_line(item)
        numbered = f"{len(included) + 1}. {line}"
        candidate = body + ("\n" if included else "") + numbered
        if not overflow and len(candidate) <= PUSHOVER_MESSAGE_MAX:
            body = candidate
            included.append(item)
        else:
            overflow.append(item)
    if not included and items:
        first = items[0]
        overflow = items[1:]
        body = truncate_message(header + _format_overflow_line(first))
    return body, overflow


def _format_drop(classification: Classification, *, story_id: str) -> tuple[str, str, str | None]:
    company = classification.company or "Unknown company"
    title = f"NEW GRAD SWE — {company}"
    parts = [
        classification.role_title or "New Grad Software Engineer",
        f"urgency {classification.urgency_score}/5",
    ]
    if classification.reason:
        parts.append(classification.reason)
    parts.append(f"story {story_id}")
    url = classification.job_link
    return title, truncate_message("\n".join(parts)), url


def _format_overflow_line(item: QuietQueueItem) -> str:
    c = item.classification
    bits = [c.company or "Unknown", c.role_title or "New Grad SWE", f"u{c.urgency_score}"]
    if c.job_link:
        bits.append(c.job_link)
    return " · ".join(bits)


def _count_primary(items: list[QuietQueueItem], overflow: list[QuietQueueItem]) -> int:
    return max(0, len(items) - len(overflow))
