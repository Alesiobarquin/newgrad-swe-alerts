"""StoryScraper protocol and provider factory."""

from __future__ import annotations

from typing import Protocol

import httpx

from app.config import Settings
from app.models import StoryAsset


class ScraperError(Exception):
    """Raised when story ingestion fails."""


class StoryScraper(Protocol):
    def fetch_stories(self, username: str) -> list[StoryAsset]: ...


def build_scraper(settings: Settings, http: httpx.Client) -> StoryScraper:
    if settings.scraper_provider == "apify_standby":
        from app.scrapers.apify_standby import ApifyStandbyScraper

        return ApifyStandbyScraper(settings, http)
    if settings.scraper_provider == "instagram_downloader":
        from app.scrapers.instagram_downloader import InstagramDownloaderScraper

        return InstagramDownloaderScraper(settings, http)
    from app.scrapers.rapidapi import RapidApiScraper

    return RapidApiScraper(settings, http)
