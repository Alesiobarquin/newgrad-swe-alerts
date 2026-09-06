"""Run a single ingest → classify → notify tick (one RapidAPI request)."""

from __future__ import annotations

import sys

from pydantic import ValidationError
from pydantic_settings.exceptions import SettingsError

from app.config import Settings
from app.escalation import in_quiet_hours, poll_interval_seconds
from app.logging_setup import setup_logging
from app.main import build_context
from app.pipeline import run_poll_tick


def main() -> int:
    setup_logging("INFO")
    try:
        settings = Settings()
    except (ValidationError, SettingsError) as exc:
        print(f"invalid configuration: {exc}", file=sys.stderr)
        return 2

    from datetime import datetime

    now = datetime.now(settings.tz)
    interval = poll_interval_seconds(now, settings)
    quiet = in_quiet_hours(now, settings)
    print(
        f"One-shot poll @ {now.isoformat()} "
        f"quiet={quiet} scheduled_interval={interval}s "
        f"user=@{settings.target_ig_username} provider={settings.scraper_provider}"
    )
    print("This uses one RapidAPI stories request (plus a one-time user_id lookup if needed).")

    try:
        ctx = build_context(settings)
    except ValueError as exc:
        print(
            "Redis URL is invalid. In Upstash → Connect, copy the URL that starts with "
            "rediss://default:...@....upstash.io:6379 — not the redis-cli snippet.\n"
            f"Underlying error: {exc}",
            file=sys.stderr,
        )
        return 1
    try:
        ctx.state.ping()
    except Exception as exc:
        print(
            "Redis connection failed. In Upstash → your database → Connect, copy the "
            "Redis URL that starts with rediss:// (not the redis-cli command).\n"
            f"Underlying error: {exc}",
            file=sys.stderr,
        )
        ctx.http.close()
        ctx.state.close()
        return 1

    try:
        count = run_poll_tick(ctx, now=now)
        if count < 0:
            print("Poll failed. Scroll up for the scraper/Gemini error.")
            return 1
        if count == 0:
            print(
                "Scrape succeeded: 0 live stories. "
                "That is a valid E2E result if @zero2sudo currently has no stories. "
                "You should still see this request on the RapidAPI dashboard."
            )
            return 0
        print(
            f"Scrape succeeded: {count} stor(y/ies). "
            "New ones were classified; SWE drops would have hit ntfy. "
            "Duplicates already in Redis are skipped."
        )
        return 0
    finally:
        ctx.http.close()
        ctx.state.close()


if __name__ == "__main__":
    sys.exit(main())
