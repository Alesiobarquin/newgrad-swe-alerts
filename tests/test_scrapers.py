from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from app.scrapers.apify_standby import ApifyStandbyScraper
from app.scrapers.base import ScraperError, build_scraper
from app.scrapers.rapidapi import RapidApiScraper
from tests.conftest import make_settings


class _Resp:
    def __init__(self, status_code: int, payload: object, headers: dict[str, str] | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.text = json.dumps(payload) if not isinstance(payload, str) else payload

    def json(self) -> object:
        if isinstance(self._payload, str):
            raise json.JSONDecodeError("bad", self._payload, 0)
        return self._payload


def test_factory_selects_rapidapi() -> None:
    scraper = build_scraper(make_settings(), MagicMock())
    assert isinstance(scraper, RapidApiScraper)


def test_factory_selects_apify() -> None:
    settings = make_settings(
        scraper_provider="apify_standby",
        apify_api_token="token",
        apify_standby_url="https://actor.apify.actor/stories",
    )
    scraper = build_scraper(settings, MagicMock())
    assert isinstance(scraper, ApifyStandbyScraper)


def test_rapidapi_fetches_and_normalizes(monkeypatch) -> None:
    settings = make_settings()
    client = MagicMock()
    client.request.return_value = _Resp(
        200,
        {
            "data": {
                "stories": [
                    {
                        "id": "story-1",
                        "mediaUrl": "https://cdn.example/a.jpg",
                        "mediaType": "image",
                    }
                ]
            }
        },
    )
    monkeypatch.setattr("app.scrapers.rapidapi.time.sleep", lambda _s: None)
    stories = RapidApiScraper(settings, client).fetch_stories("zero2sudo")
    assert stories[0].story_id == "story-1"
    args, kwargs = client.request.call_args
    assert args[0] == "GET"
    assert "zero2sudo" in args[1]
    assert kwargs["headers"]["X-RapidAPI-Key"] == "test-rapidapi-key"
    assert kwargs["headers"]["X-RapidAPI-Host"] == settings.rapidapi_host


def test_rapidapi_retries_on_429(monkeypatch) -> None:
    settings = make_settings()
    client = MagicMock()
    client.request.side_effect = [
        _Resp(429, {"err": "slow down"}, headers={"Retry-After": "0"}),
        _Resp(200, {"data": {"stories": [{"pk": "2", "displayUrl": "https://cdn.example/b.jpg"}]}}),
    ]
    monkeypatch.setattr("app.scrapers.rapidapi.time.sleep", lambda _s: None)
    stories = RapidApiScraper(settings, client).fetch_stories("zero2sudo")
    assert stories[0].story_id == "2"
    assert client.request.call_count == 2


def test_rapidapi_4xx_does_not_retry(monkeypatch) -> None:
    settings = make_settings()
    client = MagicMock()
    client.request.return_value = _Resp(403, {"err": "nope"})
    monkeypatch.setattr("app.scrapers.rapidapi.time.sleep", lambda _s: None)
    with pytest.raises(ScraperError, match="403"):
        RapidApiScraper(settings, client).fetch_stories("zero2sudo")
    assert client.request.call_count == 1


def test_apify_standby_posts_bearer(monkeypatch) -> None:
    settings = make_settings(
        scraper_provider="apify_standby",
        apify_api_token="apify-token",
        apify_standby_url="https://actor.apify.actor/stories",
    )
    client = MagicMock()
    client.request.return_value = _Resp(200, [{"id": "9", "displayUrl": "https://cdn.example/c.jpg"}])
    monkeypatch.setattr("app.scrapers.apify_standby.time.sleep", lambda _s: None)
    stories = ApifyStandbyScraper(settings, client).fetch_stories("zero2sudo")
    assert stories[0].story_id == "9"
    args, kwargs = client.request.call_args
    assert args[0] == "POST"
    assert kwargs["headers"]["Authorization"] == "Bearer apify-token"
    assert kwargs["json"]["usernames"] == ["zero2sudo"]


def test_rapidapi_transport_error_exhausted(monkeypatch) -> None:
    settings = make_settings()
    client = MagicMock()
    client.request.side_effect = httpx.ConnectError("nope")
    monkeypatch.setattr("app.scrapers.rapidapi.time.sleep", lambda _s: None)
    with pytest.raises(ScraperError):
        RapidApiScraper(settings, client).fetch_stories("zero2sudo")


def test_rapidapi_resolves_user_id_then_fetches_stories(monkeypatch) -> None:
    settings = make_settings(
        rapidapi_url_template="https://{host}/stories?user_id={user_id}",
        rapidapi_items_path="",
        rapidapi_user_id_url_template="https://{host}/user_id_by_username?username={username}",
    )
    client = MagicMock()
    client.request.side_effect = [
        _Resp(200, {"UserID": 111222333}),
        _Resp(200, [{"id": "story-9", "displayUrl": "https://cdn.example/z.jpg"}]),
        _Resp(200, [{"id": "story-9", "displayUrl": "https://cdn.example/z.jpg"}]),
    ]
    monkeypatch.setattr("app.scrapers.rapidapi.time.sleep", lambda _s: None)
    scraper = RapidApiScraper(settings, client)
    stories = scraper.fetch_stories("zero2sudo")
    assert stories[0].story_id == "story-9"
    first_url = client.request.call_args_list[0].args[1]
    second_url = client.request.call_args_list[1].args[1]
    assert "user_id_by_username" in first_url
    assert "username=zero2sudo" in first_url
    assert "user_id=111222333" in second_url
    scraper.fetch_stories("zero2sudo")
    assert client.request.call_count == 3

