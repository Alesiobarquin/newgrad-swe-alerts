from __future__ import annotations

from collections.abc import Iterator

import fakeredis
import pytest

from app.config import Settings
from app.state import RedisState


def make_settings(**overrides: object) -> Settings:
    payload: dict[str, object] = {
        "scraper_provider": "rapidapi",
        "rapidapi_key": "test-rapidapi-key",
        "rapidapi_host": "instagram.example.p.rapidapi.com",
        "upstash_redis_url": "rediss://default:fake@localhost:6379",
        "gemini_api_key": "test-gemini-key",
        "pushover_app_token": "test-pushover-app",
        "pushover_user_key": "test-pushover-user",
        "notify_provider": "pushover",
        "target_ig_username": "zero2sudo",
    }
    payload.update(overrides)
    return Settings(_env_file=None, **payload)


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def redis_client() -> Iterator[fakeredis.FakeRedis]:
    client = fakeredis.FakeRedis(decode_responses=True)
    yield client
    client.flushall()


@pytest.fixture
def state(settings: Settings, redis_client: fakeredis.FakeRedis) -> RedisState:
    return RedisState(settings, client=redis_client)
