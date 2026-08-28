from __future__ import annotations

from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from pydantic import ValidationError

from app.escalation import Escalator, poll_interval_seconds
from app.main import (
    MORNING_JOB_ID,
    POLL_JOB_ID,
    QUIET_START_JOB_ID,
    apply_poll_interval,
    main,
    register_jobs,
)
from app.pipeline import AppContext
from tests.conftest import make_settings

TZ = ZoneInfo("America/New_York")


def _ctx(state) -> AppContext:
    settings = make_settings()
    http = MagicMock()
    return AppContext(
        settings=settings,
        http=http,
        state=state,
        scraper=MagicMock(),
        classifier=MagicMock(),
        escalator=Escalator(settings, http, state),
    )


def test_main_returns_2_when_config_invalid(monkeypatch) -> None:
    def boom() -> None:
        raise ValidationError.from_exception_data(
            "Settings",
            [{"type": "missing", "loc": ("gemini_api_key",), "input": {}}],
        )

    monkeypatch.setattr("app.main.Settings", boom)
    monkeypatch.setattr("app.main.build_context", MagicMock())
    assert main() == 2


def test_scheduler_registers_poll_quiet_and_morning_jobs(state) -> None:
    ctx = _ctx(state)
    scheduler = BackgroundScheduler(timezone=ctx.settings.tz)
    register_jobs(scheduler, ctx)
    scheduler.start(paused=True)
    try:
        jobs = {job.id: job for job in scheduler.get_jobs()}
        assert set(jobs) == {POLL_JOB_ID, QUIET_START_JOB_ID, MORNING_JOB_ID}
        quiet = jobs[QUIET_START_JOB_ID]
        morning = jobs[MORNING_JOB_ID]
        assert "23" in str(quiet.trigger)
        assert "8" in str(morning.trigger)
    finally:
        scheduler.shutdown(wait=False)


def test_reschedule_switches_between_45_and_300(state) -> None:
    ctx = _ctx(state)
    scheduler = BackgroundScheduler(timezone=ctx.settings.tz)
    register_jobs(scheduler, ctx)
    scheduler.start(paused=True)
    try:
        quiet_now = datetime(2026, 8, 26, 23, 5, tzinfo=TZ)
        seconds = apply_poll_interval(ctx, now=quiet_now)
        assert seconds == 300
        poll = scheduler.get_job(POLL_JOB_ID)
        assert poll is not None
        assert poll.trigger.interval.total_seconds() == 300

        active_now = datetime(2026, 8, 27, 8, 1, tzinfo=TZ)
        seconds = apply_poll_interval(ctx, now=active_now)
        assert seconds == 45
        poll = scheduler.get_job(POLL_JOB_ID)
        assert poll is not None
        assert poll.trigger.interval.total_seconds() == 45
    finally:
        scheduler.shutdown(wait=False)


def test_interval_helper_matches_settings() -> None:
    settings = make_settings(active_poll_interval_seconds=45, quiet_poll_interval_seconds=300)
    assert poll_interval_seconds(datetime(2026, 8, 26, 9, 0, tzinfo=TZ), settings) == 45
    assert poll_interval_seconds(datetime(2026, 8, 26, 23, 0, tzinfo=TZ), settings) == 300
