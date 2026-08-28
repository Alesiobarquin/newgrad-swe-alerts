"""Environment-backed application settings."""

from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_OVERRIDE_COMPANIES = (
    "Google,Meta,Stripe,Jane Street,Citadel,OpenAI,Nvidia,Apple,Amazon,Microsoft,Netflix"
)

ScraperProvider = Literal["rapidapi", "apify_standby"]
HttpMethod = Literal["GET", "POST"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    scraper_provider: ScraperProvider = "rapidapi"
    target_ig_username: str = "zero2sudo"

    rapidapi_key: str = ""
    rapidapi_host: str = ""
    rapidapi_method: HttpMethod = "GET"
    rapidapi_url_template: str = "https://{host}/user/stories?username={username}"
    rapidapi_body_template: str = ""
    rapidapi_items_path: str = "data.stories"

    apify_api_token: str = ""
    apify_standby_url: str = ""
    apify_standby_method: HttpMethod = "POST"

    upstash_redis_url: str = Field(min_length=1)
    gemini_api_key: str = Field(min_length=1)
    gemini_model: str = "gemini-1.5-flash"
    pushover_app_token: str = Field(min_length=1)
    pushover_user_key: str = Field(min_length=1)

    override_companies: str = _DEFAULT_OVERRIDE_COMPANIES
    quiet_hours_start_hour: int = Field(default=23, ge=0, le=23)
    quiet_hours_end_hour: int = Field(default=8, ge=0, le=23)
    timezone: str = "America/New_York"
    active_poll_interval_seconds: int = Field(default=45, ge=5)
    quiet_poll_interval_seconds: int = Field(default=300, ge=5)
    claim_ttl_seconds: int = Field(default=180, ge=30)
    story_ttl_seconds: int = Field(default=172800, ge=60)
    log_level: str = "INFO"

    @field_validator("override_companies", mode="before")
    @classmethod
    def _normalize_companies(cls, value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, (list, tuple)):
            return ",".join(str(item).strip() for item in value if str(item).strip())
        return _DEFAULT_OVERRIDE_COMPANIES

    @field_validator("rapidapi_method", "apify_standby_method", mode="before")
    @classmethod
    def _upper_method(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().upper()
        return value

    @field_validator("target_ig_username", mode="before")
    @classmethod
    def _strip_at(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lstrip("@")
        return value

    @model_validator(mode="after")
    def _require_provider_secrets(self) -> Settings:
        if self.scraper_provider == "rapidapi":
            if not self.rapidapi_key or not self.rapidapi_host:
                raise ValueError("RAPIDAPI_KEY and RAPIDAPI_HOST are required when SCRAPER_PROVIDER=rapidapi")
        elif self.scraper_provider == "apify_standby":
            if not self.apify_api_token or not self.apify_standby_url:
                raise ValueError(
                    "APIFY_API_TOKEN and APIFY_STANDBY_URL are required when SCRAPER_PROVIDER=apify_standby"
                )
        return self

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @property
    def override_company_set(self) -> frozenset[str]:
        return frozenset(
            name.strip().lower()
            for name in self.override_companies.split(",")
            if name.strip()
        )

    @property
    def poller_lock_ttl_seconds(self) -> int:
        return max(120, self.active_poll_interval_seconds * 3)
