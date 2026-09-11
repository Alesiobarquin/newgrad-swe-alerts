"""One poll tick: ingest → claim → classify → escalate."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import httpx

from app.classifier import ClassificationError, ClassifierConfigError, GeminiClassifier
from app.config import Settings
from app.escalation import Escalator, PushoverError
from app.media import MediaError, prepare_frame
from app.models import FailedClassificationItem, StoryAsset
from app.scrapers.base import ScraperError, StoryScraper
from app.state import RedisState

if TYPE_CHECKING:
    from apscheduler.schedulers.blocking import BlockingScheduler

logger = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    http: httpx.Client
    state: RedisState
    scraper: StoryScraper
    classifier: GeminiClassifier
    escalator: Escalator
    scheduler: BlockingScheduler | None = None


def run_poll_tick(ctx: AppContext, *, now: datetime | None = None) -> int:
    now = now or datetime.now(ctx.settings.tz)
    if not ctx.state.acquire_job_lock("poller", ttl_seconds=ctx.settings.poller_lock_ttl_seconds):
        logger.warning("poll tick skipped; lock held")
        return 0
    try:
        stories = ctx.scraper.fetch_stories(ctx.settings.target_ig_username)
        logger.info("fetched %s stories for @%s", len(stories), ctx.settings.target_ig_username)
        claimed_ids = ctx.state.try_claim_many([story.story_id for story in stories])
        for story in stories:
            if story.story_id in claimed_ids:
                process_story(ctx, story, now=now, already_claimed=True)
        return len(stories)
    except ScraperError:
        logger.exception("ingest failed; tick aborted without claiming stories")
        return -1
    except ClassifierConfigError as exc:
        logger.error("%s", exc)
        return -1
    except Exception:
        logger.exception("poll tick failed")
        return -1
    finally:
        ctx.state.release_job_lock("poller")


def process_story(ctx: AppContext, story: StoryAsset, *, now: datetime, already_claimed: bool = False) -> None:
    if not already_claimed and not ctx.state.try_claim(story.story_id):
        logger.debug("skip story %s (seen or in-flight)", story.story_id)
        return

    try:
        frame_bytes, mime_type = prepare_frame(ctx.http, story)
    except MediaError:
        logger.exception("media failed for %s; releasing claim for retry", story.story_id)
        ctx.state.release_claim(story.story_id)
        return

    try:
        classification = ctx.classifier.classify(story, frame_bytes, mime_type)
    except ClassificationError as exc:
        logger.error("classification failed for %s: %s", story.story_id, exc)
        if "NOT_FOUND" in str(exc) or "not found" in str(exc).lower():
            ctx.state.release_claim(story.story_id)
            raise ClassifierConfigError(
                "Gemini model is unavailable. Set GEMINI_MODEL to a current Flash model "
                "(e.g. gemini-3.6-flash) and rerun. Claim released so stories can retry."
            ) from exc
        ctx.state.push_dlq(
            FailedClassificationItem(
                story_id=story.story_id,
                error=str(exc),
                failed_at=datetime.now(timezone.utc),
                image_url=story.image_url,
                video_url=story.video_url,
                link_urls=story.link_urls,
            )
        )
        ctx.escalator.send_debug(story.story_id, str(exc))
        return

    if not classification.is_new_grad_swe:
        ctx.state.mark_seen_and_release(story.story_id)
        logger.info("story %s classified negative", story.story_id)
        return

    try:
        ctx.escalator.dispatch_positive(
            story_id=story.story_id,
            taken_at=story.taken_at,
            classification=classification,
            now=now,
            fallback_url=f"https://www.instagram.com/stories/{story.username}/",
        )
    except PushoverError as exc:
        if exc.sent_uncertain:
            logger.exception("Pushover uncertain success for %s; marking seen to avoid duplicate sirens", story.story_id)
            ctx.state.mark_seen_and_release(story.story_id)
            return
        logger.exception("Pushover failed for %s; leaving claiming TTL for retry", story.story_id)
        return

    ctx.state.mark_seen_and_release(story.story_id)


def run_morning_burst(ctx: AppContext) -> None:
    ctx.escalator.run_morning_burst()
