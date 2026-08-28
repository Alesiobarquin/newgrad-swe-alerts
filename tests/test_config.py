from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.conftest import make_settings


def test_override_companies_split_from_csv() -> None:
    settings = make_settings(override_companies="Google, Stripe, Jane Street")
    assert settings.override_company_set == frozenset({"google", "stripe", "jane street"})


def test_username_strips_at_sign() -> None:
    settings = make_settings(target_ig_username="@zero2sudo")
    assert settings.target_ig_username == "zero2sudo"


def test_rapidapi_provider_requires_host_and_key() -> None:
    with pytest.raises(ValidationError):
        make_settings(rapidapi_key="", rapidapi_host="")


def test_apify_provider_requires_standby_url() -> None:
    with pytest.raises(ValidationError):
        make_settings(
            scraper_provider="apify_standby",
            apify_api_token="token",
            apify_standby_url="",
        )


def test_apify_provider_ok_without_rapidapi() -> None:
    settings = make_settings(
        scraper_provider="apify_standby",
        rapidapi_key="",
        rapidapi_host="",
        apify_api_token="token",
        apify_standby_url="https://actor.apify.actor/stories",
    )
    assert settings.scraper_provider == "apify_standby"


def test_empty_secrets_are_rejected() -> None:
    with pytest.raises(ValidationError):
        make_settings(gemini_api_key="")
    with pytest.raises(ValidationError):
        make_settings(pushover_user_key="")


def test_poller_lock_ttl_scales_with_interval() -> None:
    settings = make_settings(active_poll_interval_seconds=60)
    assert settings.poller_lock_ttl_seconds == 180


def test_dotenv_comma_companies_roundtrip(tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "SCRAPER_PROVIDER=rapidapi",
                "RAPIDAPI_KEY=k",
                "RAPIDAPI_HOST=example.p.rapidapi.com",
                "UPSTASH_REDIS_URL=rediss://default:fake@localhost:6379",
                "GEMINI_API_KEY=g",
                "PUSHOVER_APP_TOKEN=p",
                "PUSHOVER_USER_KEY=u",
                "OVERRIDE_COMPANIES=Google,Stripe,Jane Street",
            ]
        )
        + "\n"
    )
    from app.config import Settings

    settings = Settings(_env_file=env_file)
    assert settings.override_company_set == frozenset({"google", "stripe", "jane street"})
