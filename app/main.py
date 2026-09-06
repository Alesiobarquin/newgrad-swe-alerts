"""APScheduler entrypoint: dynamic 45s/300s polling and 08:00 ET morning burst."""

from __future__ import annotations

import logging
import signal
import sys
from datetime import datetime
from types import FrameType

from apscheduler.schedulers.base import BaseScheduler
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from pydantic import ValidationError
from pydantic_settings.exceptions import SettingsError

from app.classifier import GeminiClassifier
from app.config import Settings
from app.escalation import Escalator, in_quiet_hours, poll_interval_seconds
from app.http import build_http_client
from app.logging_setup import setup_logging
from app.pipeline import AppContext, run_morning_burst, run_poll_tick
from app.scrapers.base import build_scraper
from app.state import RedisState

logger = logging.getLogger(__name__)

POLL_JOB_ID = "poll"
QUIET_START_JOB_ID = "quiet_start"
MORNING_JOB_ID = "morning_burst"


def apply_poll_interval(ctx: AppContext, now: datetime | None = None) -> int:
    if ctx.scheduler is None:
        raise RuntimeError("scheduler is not attached to AppContext")
    now = now or datetime.now(ctx.settings.tz)
    seconds = poll_interval_seconds(now, ctx.settings)
    ctx.scheduler.reschedule_job(
        POLL_JOB_ID,
        trigger=IntervalTrigger(seconds=seconds, timezone=ctx.settings.tz),
    )
    logger.info(
        "poll interval set to %ss (quiet_hours=%s)",
        seconds,
        in_quiet_hours(now, ctx.settings),
    )
    return seconds


def on_quiet_hours_start(ctx: AppContext) -> None:
    logger.info("entering quiet hours; slowing poller")
    apply_poll_interval(ctx)


def on_morning(ctx: AppContext) -> None:
    logger.info("08:00 ET morning burst")
    try:
        run_morning_burst(ctx)
    except Exception:
        logger.exception("morning burst failed")
    apply_poll_interval(ctx)


def build_context(settings: Settings) -> AppContext:
    http = build_http_client()
    state = RedisState(settings)
    scraper = build_scraper(settings, http)
    classifier = GeminiClassifier(settings)
    escalator = Escalator(settings, http, state)
    return AppContext(
        settings=settings,
        http=http,
        state=state,
        scraper=scraper,
        classifier=classifier,
        escalator=escalator,
    )


def register_jobs(scheduler: BaseScheduler, ctx: AppContext) -> int:
    ctx.scheduler = scheduler
    initial = poll_interval_seconds(datetime.now(ctx.settings.tz), ctx.settings)
    scheduler.add_job(
        run_poll_tick,
        trigger=IntervalTrigger(seconds=initial, timezone=ctx.settings.tz),
        kwargs={"ctx": ctx},
        id=POLL_JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=max(30, initial),
        replace_existing=True,
    )
    scheduler.add_job(
        on_quiet_hours_start,
        trigger=CronTrigger(hour=ctx.settings.quiet_hours_start_hour, minute=0, second=0, timezone=ctx.settings.tz),
        kwargs={"ctx": ctx},
        id=QUIET_START_JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=60,
        replace_existing=True,
    )
    scheduler.add_job(
        on_morning,
        trigger=CronTrigger(hour=ctx.settings.quiet_hours_end_hour, minute=0, second=0, timezone=ctx.settings.tz),
        kwargs={"ctx": ctx},
        id=MORNING_JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=120,
        replace_existing=True,
    )
    logger.info("scheduler registered with %ss poll interval", initial)
    return initial


def build_scheduler(ctx: AppContext) -> BlockingScheduler:
    scheduler = BlockingScheduler(timezone=ctx.settings.tz)
    register_jobs(scheduler, ctx)
    return scheduler


def main() -> int:
    setup_logging("INFO")
    try:
        settings = Settings()
    except (ValidationError, SettingsError) as exc:
        logger.error("invalid configuration; fill .env from .env.example\n%s", exc)
        return 2
    setup_logging(settings.log_level)
    logger.info(
        "starting alert engine provider=%s username=@%s tz=%s",
        settings.scraper_provider,
        settings.target_ig_username,
        settings.timezone,
    )
    ctx = build_context(settings)
    try:
        ctx.state.ping()
    except Exception:
        logger.exception("redis ping failed; refusing to start without dedup")
        ctx.http.close()
        ctx.state.close()
        return 1

    scheduler = build_scheduler(ctx)

    def _shutdown(_signum: int, _frame: FrameType | None) -> None:
        logger.info("shutdown signal received")
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("scheduler stopped")
    finally:
        ctx.http.close()
        ctx.state.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
