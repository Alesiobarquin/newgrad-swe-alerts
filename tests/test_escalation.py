from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.escalation import (
    PUSHOVER_MESSAGE_MAX,
    Escalator,
    in_quiet_hours,
    is_override,
    pack_morning_burst,
    poll_interval_seconds,
    truncate_message,
)
from app.models import Classification, QuietQueueItem
from tests.conftest import make_settings

TZ = ZoneInfo("America/New_York")


def _cls(**overrides: object) -> Classification:
    payload: dict[str, object] = {
        "is_new_grad_swe": True,
        "company": "BoutiqueCo",
        "role_title": "New Grad SWE",
        "job_link": "https://jobs.example/1",
        "urgency_score": 3,
        "reason": "apply link visible",
    }
    payload.update(overrides)
    return Classification.model_validate(payload)


def test_quiet_window_half_open() -> None:
    settings = make_settings()
    assert in_quiet_hours(datetime(2026, 8, 26, 22, 59, tzinfo=TZ), settings) is False
    assert in_quiet_hours(datetime(2026, 8, 26, 23, 0, tzinfo=TZ), settings) is True
    assert in_quiet_hours(datetime(2026, 8, 27, 7, 59, tzinfo=TZ), settings) is True
    assert in_quiet_hours(datetime(2026, 8, 27, 8, 0, tzinfo=TZ), settings) is False
    assert in_quiet_hours(datetime(2026, 8, 27, 12, 0, tzinfo=TZ), settings) is False


def test_poll_interval_follows_window() -> None:
    settings = make_settings()
    assert poll_interval_seconds(datetime(2026, 8, 26, 15, 0, tzinfo=TZ), settings) == 45
    assert poll_interval_seconds(datetime(2026, 8, 26, 23, 15, tzinfo=TZ), settings) == 300
    assert poll_interval_seconds(datetime(2026, 8, 27, 8, 0, tzinfo=TZ), settings) == 45


def test_override_company_list_and_urgency_five() -> None:
    settings = make_settings()
    assert is_override(_cls(company="Google LLC", urgency_score=2), settings) is True
    assert is_override(_cls(company="Jane Street", urgency_score=1), settings) is True
    assert is_override(_cls(company="BoutiqueCo", urgency_score=5), settings) is True
    assert is_override(_cls(company="BoutiqueCo", urgency_score=4), settings) is False
    assert is_override(_cls(company="Metamask Labs", urgency_score=3), settings) is False


def test_truncate_message_under_limit() -> None:
    text = "a" * 1005
    out = truncate_message(text)
    assert len(out) == PUSHOVER_MESSAGE_MAX
    assert out.endswith("…")


def test_morning_burst_packs_one_p2_and_overflow() -> None:
    items = [
        QuietQueueItem(
            story_id=str(i),
            classification=_cls(
                company=f"Company{i}",
                role_title="New Grad Software Engineer Backend Platform",
                job_link=f"https://boards.greenhouse.io/very-long-path/jobs/{i}?gh_src={'x' * 80}",
                urgency_score=4,
            ),
            queued_at=datetime(2026, 8, 27, 8, 0, tzinfo=TZ),
        )
        for i in range(12)
    ]
    body, overflow = pack_morning_burst(items)
    assert len(body) <= PUSHOVER_MESSAGE_MAX
    assert overflow
    assert len(overflow) < len(items)


def test_dispatch_quiet_queues_and_sends_p1(state) -> None:
    settings = make_settings()
    http = MagicMock()
    http.post.return_value = MagicMock(status_code=200, json=lambda: {"status": 1})
    http.post.return_value.raise_for_status = MagicMock()
    escalator = Escalator(settings, http, state)
    now = datetime(2026, 8, 26, 23, 30, tzinfo=TZ)
    escalator.dispatch_positive(story_id="s1", taken_at=None, classification=_cls(company="BoutiqueCo"), now=now)
    queued = state.drain_quiet()
    assert len(queued) == 1
    payload = http.post.call_args.kwargs["data"]
    assert payload["priority"] == -1
    assert payload["sound"] == "none"
    assert payload["title"].startswith("[Queued]")
    assert len(payload["message"]) <= PUSHOVER_MESSAGE_MAX


def test_dispatch_override_during_quiet_sends_p2(state) -> None:
    settings = make_settings()
    http = MagicMock()
    http.post.return_value = MagicMock(status_code=200, json=lambda: {"status": 1, "receipt": "abc"})
    http.post.return_value.raise_for_status = MagicMock()
    escalator = Escalator(settings, http, state)
    now = datetime(2026, 8, 26, 23, 30, tzinfo=TZ)
    escalator.dispatch_positive(story_id="s1", taken_at=None, classification=_cls(company="Stripe"), now=now)
    assert state.drain_quiet() == []
    payload = http.post.call_args.kwargs["data"]
    assert payload["priority"] == 2
    assert payload["retry"] == 30
    assert payload["expire"] == 3600
    assert payload["sound"] == "siren"


def test_dispatch_uses_story_fallback_when_job_link_missing(state) -> None:
    settings = make_settings()
    http = MagicMock()
    http.post.return_value = MagicMock(status_code=200, json=lambda: {"status": 1})
    http.post.return_value.raise_for_status = MagicMock()
    escalator = Escalator(settings, http, state)

    escalator.dispatch_positive(
        story_id="fallback",
        taken_at=None,
        classification=_cls(job_link=None),
        now=datetime(2026, 8, 26, 12, 0, tzinfo=TZ),
        fallback_url="https://www.instagram.com/stories/zero2sudo/",
    )

    assert http.post.call_args.kwargs["data"]["url"] == "https://www.instagram.com/stories/zero2sudo/"


def test_morning_burst_one_siren_then_p1_overflow(state) -> None:
    settings = make_settings()
    http = MagicMock()
    http.post.return_value = MagicMock(status_code=200, json=lambda: {"status": 1})
    http.post.return_value.raise_for_status = MagicMock()
    escalator = Escalator(settings, http, state)
    for i in range(15):
        state.push_quiet(
            QuietQueueItem(
                story_id=str(i),
                classification=_cls(
                    company=f"Company{i}",
                    job_link=f"https://example.com/jobs/{i}/{'a' * 90}",
                ),
                queued_at=datetime(2026, 8, 27, 8, 0, tzinfo=TZ),
            )
        )
    escalator.run_morning_burst()
    calls = http.post.call_args_list
    assert calls
    priorities = [call.kwargs["data"]["priority"] for call in calls]
    assert priorities[0] == 2
    assert priorities.count(2) == 1
    if len(priorities) > 1:
        assert set(priorities[1:]) == {-1}
    for call in calls:
        assert len(call.kwargs["data"]["message"]) <= PUSHOVER_MESSAGE_MAX
    assert state.drain_quiet() == []


def test_ntfy_emergency_uses_priority_5(state) -> None:
    settings = make_settings(
        notify_provider="ntfy",
        ntfy_topic="swe-alerts-test",
        ntfy_base_url="https://ntfy.sh",
        pushover_app_token="",
        pushover_user_key="",
    )
    http = MagicMock()
    http.post.return_value = MagicMock(status_code=200, json=lambda: {"id": "abc"})
    http.post.return_value.raise_for_status = MagicMock()
    escalator = Escalator(settings, http, state)
    now = datetime(2026, 8, 26, 15, 0, tzinfo=TZ)
    escalator.dispatch_positive(
        story_id="s1",
        taken_at=None,
        classification=_cls(company="BoutiqueCo"),
        now=now,
    )
    args, kwargs = http.post.call_args
    assert args[0] == "https://ntfy.sh"
    body = kwargs["json"]
    assert body["topic"] == "swe-alerts-test"
    assert body["priority"] == 5
    assert body["title"].startswith("NEW GRAD SWE")
    assert "—" in body["title"]
    assert body["click"] == "https://jobs.example/1"
