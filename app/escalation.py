"""Quiet-hours routing, deterministic override, and pluggable push dispatch."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any, Protocol

import httpx

from app.config import Settings
from app.http import NOTIFY_TIMEOUT
from app.models import Classification, QuietQueueItem
from app.state import RedisState

logger = logging.getLogger(__name__)

PUSHOVER_ENDPOINT = "https://api.pushover.net/1/messages.json"
PUSHOVER_MESSAGE_MAX = 1000
PUSHOVER_TITLE_MAX = 250
P2_RETRY_SECONDS = 30
P2_EXPIRE_SECONDS = 3600

NTFY_PRIORITY_EMERGENCY = "5"
NTFY_PRIORITY_LOW = "1"


class NotifyError(Exception):
    def __init__(self, message: str, *, sent_uncertain: bool = False) -> None:
        super().__init__(message)
        self.sent_uncertain = sent_uncertain


PushoverError = NotifyError


class NotificationBackend(Protocol):
    def send_emergency(self, *, title: str, message: str, url: str | None = None) -> dict[str, Any]: ...

    def send_priority_low(self, *, title: str, message: str, url: str | None = None) -> dict[str, Any]: ...


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


def build_notifier(settings: Settings, http: httpx.Client) -> NotificationBackend:
    if settings.notify_provider == "pushover":
        return PushoverBackend(settings, http)
    return NtfyBackend(settings, http)


class NtfyBackend:
    def __init__(self, settings: Settings, http: httpx.Client) -> None:
        self._settings = settings
        self._http = http

    def send_emergency(self, *, title: str, message: str, url: str | None = None) -> dict[str, Any]:
        return self._publish(title=title, message=message, url=url, priority=NTFY_PRIORITY_EMERGENCY, tags="rotating_light,siren")

    def send_priority_low(self, *, title: str, message: str, url: str | None = None) -> dict[str, Any]:
        return self._publish(title=title, message=message, url=url, priority=NTFY_PRIORITY_LOW, tags="mute")

    def _publish(
        self,
        *,
        title: str,
        message: str,
        url: str | None,
        priority: str,
        tags: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "topic": self._settings.ntfy_topic.strip(),
            "title": truncate_message(title, PUSHOVER_TITLE_MAX),
            "message": truncate_message(message, PUSHOVER_MESSAGE_MAX),
            "priority": int(priority),
            "tags": [tag.strip() for tag in tags.split(",") if tag.strip()],
        }
        if url:
            payload["click"] = url[:512]
        headers: dict[str, str] = {"Content-Type": "application/json; charset=utf-8"}
        if self._settings.ntfy_token.strip():
            headers["Authorization"] = f"Bearer {self._settings.ntfy_token.strip()}"
        return _post_notify(
            self._http,
            self._settings.ntfy_base_url.rstrip("/"),
            headers=headers,
            json_body=payload,
            provider="ntfy",
        )


class PushoverBackend:
    def __init__(self, settings: Settings, http: httpx.Client) -> None:
        self._settings = settings
        self._http = http

    def send_emergency(self, *, title: str, message: str, url: str | None = None) -> dict[str, Any]:
        extra: dict[str, Any] = {"retry": P2_RETRY_SECONDS, "expire": P2_EXPIRE_SECONDS, "sound": "siren"}
        return self._publish(title=title, message=message, url=url, priority=2, extra=extra)

    def send_priority_low(self, *, title: str, message: str, url: str | None = None) -> dict[str, Any]:
        return self._publish(title=title, message=message, url=url, priority=-1, extra={"sound": "none"})

    def _publish(
        self,
        *,
        title: str,
        message: str,
        url: str | None,
        priority: int,
        extra: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "token": self._settings.pushover_app_token,
            "user": self._settings.pushover_user_key,
            "title": truncate_message(title, PUSHOVER_TITLE_MAX),
            "message": truncate_message(message, PUSHOVER_MESSAGE_MAX),
            "priority": priority,
            **extra,
        }
        if url:
            payload["url"] = url[:512]
            payload["url_title"] = "Open listing"
        body = _post_notify(self._http, PUSHOVER_ENDPOINT, data=payload, provider="pushover")
        if body.get("status") != 1:
            raise NotifyError(f"Pushover rejected payload: {body}", sent_uncertain=False)
        return body


class Escalator:
    def __init__(
        self,
        settings: Settings,
        http: httpx.Client,
        state: RedisState,
        notifier: NotificationBackend | None = None,
    ) -> None:
        self._settings = settings
        self._http = http
        self._state = state
        self._notifier = notifier or build_notifier(settings, http)

    def dispatch_positive(
        self,
        *,
        story_id: str,
        taken_at: datetime | None,
        classification: Classification,
        now: datetime,
        fallback_url: str | None = None,
    ) -> None:
        override = is_override(classification, self._settings)
        quiet = in_quiet_hours(now, self._settings)
        title, body, url = _format_drop(classification, story_id=story_id, fallback_url=fallback_url)
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
            except NotifyError:
                self._state.restore_quiet(items)
                raise
            for extra in overflow:
                line = _format_overflow_line(extra)
                self.send_priority_low(title="Overnight drop (overflow)", message=line)
            logger.info(
                "morning burst sent: %s emergency, %s overflow low-priority",
                _count_primary(items, overflow),
                len(overflow),
            )
        finally:
            self._state.release_job_lock("morning_burst")

    def send_debug(self, story_id: str, error: str) -> None:
        try:
            self.send_priority_low(
                title="[Debug] classification failed",
                message=truncate_message(f"story {story_id}: {error}"),
            )
        except NotifyError:
            logger.exception("failed to send classification debug ping for %s", story_id)

    def send_emergency(self, *, title: str, message: str, url: str | None = None) -> dict[str, Any]:
        return self._notifier.send_emergency(title=title, message=message, url=url)

    def send_priority_low(self, *, title: str, message: str, url: str | None = None) -> dict[str, Any]:
        return self._notifier.send_priority_low(title=title, message=message, url=url)


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


def _format_drop(
    classification: Classification,
    *,
    story_id: str,
    fallback_url: str | None = None,
) -> tuple[str, str, str | None]:
    company = classification.company or "Unknown company"
    title = f"NEW GRAD SWE — {company}"
    parts = [
        classification.role_title or "New Grad Software Engineer",
        f"urgency {classification.urgency_score}/5",
    ]
    if classification.reason:
        parts.append(classification.reason)
    parts.append(f"story {story_id}")
    url = classification.job_link or fallback_url
    return title, truncate_message("\n".join(parts)), url


def _format_overflow_line(item: QuietQueueItem) -> str:
    c = item.classification
    bits = [c.company or "Unknown", c.role_title or "New Grad SWE", f"u{c.urgency_score}"]
    if c.job_link:
        bits.append(c.job_link)
    return " · ".join(bits)


def _count_primary(items: list[QuietQueueItem], overflow: list[QuietQueueItem]) -> int:
    return max(0, len(items) - len(overflow))


def _post_notify(
    http: httpx.Client,
    endpoint: str,
    *,
    provider: str,
    headers: dict[str, str] | None = None,
    content: str | None = None,
    data: dict[str, Any] | None = None,
    json_body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sent_uncertain = False
    try:
        kwargs: dict[str, Any] = {"timeout": NOTIFY_TIMEOUT}
        if headers is not None:
            kwargs["headers"] = headers
        if json_body is not None:
            kwargs["json"] = json_body
        if content is not None:
            kwargs["content"] = content
        if data is not None:
            kwargs["data"] = data
        response = http.post(endpoint, **kwargs)
        sent_uncertain = True
        response.raise_for_status()
    except UnicodeEncodeError as exc:
        raise NotifyError(f"{provider} rejected non-ASCII headers: {exc}", sent_uncertain=False) from exc
    except httpx.TimeoutException as exc:
        raise NotifyError(f"{provider} timed out", sent_uncertain=sent_uncertain) from exc
    except httpx.HTTPStatusError as exc:
        raise NotifyError(
            f"{provider} HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            sent_uncertain=False,
        ) from exc
    except httpx.HTTPError as exc:
        raise NotifyError(f"{provider} transport error: {exc}", sent_uncertain=sent_uncertain) from exc

    try:
        body = response.json()
        return body if isinstance(body, dict) else {"status": 1, "raw": body}
    except ValueError:
        return {"status": 1}
