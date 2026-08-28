from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from app.classifier import ClassificationError
from app.escalation import Escalator
from app.media import MediaError
from app.models import Classification, StoryAsset
from app.pipeline import AppContext, process_story, run_poll_tick
from app.scrapers.base import ScraperError
from app.state import CLAIMING_PREFIX, SEEN_PREFIX
from tests.conftest import make_settings

TZ = ZoneInfo("America/New_York")


def _asset(story_id: str = "s1") -> StoryAsset:
    return StoryAsset(
        story_id=story_id,
        username="zero2sudo",
        media_type="image",
        image_url="https://cdn.example/a.jpg",
    )


def _cls(**overrides: object) -> Classification:
    payload: dict[str, object] = {
        "is_new_grad_swe": True,
        "company": "BoutiqueCo",
        "role_title": "SWE",
        "job_link": None,
        "urgency_score": 3,
        "reason": "drop",
    }
    payload.update(overrides)
    return Classification.model_validate(payload)


def _ctx(state, *, scraper=None, classifier=None, http=None) -> AppContext:
    settings = make_settings()
    http = http or MagicMock()
    post = MagicMock(status_code=200)
    post.json.return_value = {"status": 1}
    post.raise_for_status = MagicMock()
    http.post.return_value = post
    return AppContext(
        settings=settings,
        http=http,
        state=state,
        scraper=scraper or MagicMock(),
        classifier=classifier or MagicMock(),
        escalator=Escalator(settings, http, state),
    )


def test_negative_classification_promotes_seen(monkeypatch, state, redis_client) -> None:
    monkeypatch.setattr("app.pipeline.prepare_frame", lambda http, asset: (b"img", "image/jpeg"))
    classifier = MagicMock()
    classifier.classify.return_value = _cls(is_new_grad_swe=False)
    ctx = _ctx(state, classifier=classifier)
    process_story(ctx, _asset(), now=datetime(2026, 8, 26, 12, 0, tzinfo=TZ))
    assert redis_client.exists(f"{SEEN_PREFIX}s1")
    assert not redis_client.exists(f"{CLAIMING_PREFIX}s1")
    ctx.http.post.assert_not_called()


def test_media_failure_releases_claim(monkeypatch, state, redis_client) -> None:
    monkeypatch.setattr("app.pipeline.prepare_frame", lambda http, asset: (_ for _ in ()).throw(MediaError("cdn")))
    process_story(_ctx(state), _asset(), now=datetime(2026, 8, 26, 12, 0, tzinfo=TZ))
    assert not redis_client.exists(f"{SEEN_PREFIX}s1")
    assert not redis_client.exists(f"{CLAIMING_PREFIX}s1")


def test_classification_failure_dlq_keeps_claiming(monkeypatch, state, redis_client) -> None:
    monkeypatch.setattr("app.pipeline.prepare_frame", lambda http, asset: (b"img", "image/jpeg"))
    classifier = MagicMock()
    classifier.classify.side_effect = ClassificationError("schema")
    ctx = _ctx(state, classifier=classifier)
    process_story(ctx, _asset(), now=datetime(2026, 8, 26, 12, 0, tzinfo=TZ))
    assert not redis_client.exists(f"{SEEN_PREFIX}s1")
    assert redis_client.exists(f"{CLAIMING_PREFIX}s1")
    assert redis_client.llen("queue:failed_classifications") == 1
    payload = ctx.http.post.call_args.kwargs["data"]
    assert payload["priority"] == -1
    assert "classification failed" in payload["title"].lower()


def test_quiet_positive_queues_without_siren(monkeypatch, state) -> None:
    monkeypatch.setattr("app.pipeline.prepare_frame", lambda http, asset: (b"img", "image/jpeg"))
    classifier = MagicMock()
    classifier.classify.return_value = _cls(company="BoutiqueCo", urgency_score=3)
    ctx = _ctx(state, classifier=classifier)
    process_story(ctx, _asset(), now=datetime(2026, 8, 26, 23, 40, tzinfo=TZ))
    queued = state.drain_quiet()
    assert queued[0].story_id == "s1"
    assert ctx.http.post.call_args.kwargs["data"]["priority"] == -1
    assert state.is_seen("s1")


def test_active_hours_positive_sends_p2(monkeypatch, state) -> None:
    monkeypatch.setattr("app.pipeline.prepare_frame", lambda http, asset: (b"img", "image/jpeg"))
    classifier = MagicMock()
    classifier.classify.return_value = _cls(company="BoutiqueCo")
    ctx = _ctx(state, classifier=classifier)
    process_story(ctx, _asset(), now=datetime(2026, 8, 26, 15, 0, tzinfo=TZ))
    assert ctx.http.post.call_args.kwargs["data"]["priority"] == 2
    assert state.is_seen("s1")
    assert state.drain_quiet() == []


def test_poll_tick_skips_on_scraper_error(state) -> None:
    scraper = MagicMock()
    scraper.fetch_stories.side_effect = ScraperError("429")
    ctx = _ctx(state, scraper=scraper)
    run_poll_tick(ctx, now=datetime(2026, 8, 26, 15, 0, tzinfo=TZ))
    scraper.fetch_stories.assert_called_once()
    assert state.acquire_job_lock("poller", ttl_seconds=10) is True


def test_poll_tick_does_not_reclaim_seen(monkeypatch, state) -> None:
    monkeypatch.setattr("app.pipeline.prepare_frame", lambda http, asset: (b"img", "image/jpeg"))
    classifier = MagicMock()
    classifier.classify.return_value = _cls(is_new_grad_swe=False)
    scraper = MagicMock()
    scraper.fetch_stories.return_value = [_asset("dup")]
    ctx = _ctx(state, scraper=scraper, classifier=classifier)
    now = datetime(2026, 8, 26, 15, 0, tzinfo=TZ)
    run_poll_tick(ctx, now=now)
    run_poll_tick(ctx, now=now)
    assert classifier.classify.call_count == 1
