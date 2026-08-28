"""Domain models and Gemini JSON schema."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

MediaType = Literal["image", "video", "unknown"]


def _blank_to_none(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


class StoryAsset(BaseModel):
    story_id: str
    username: str
    taken_at: datetime | None = None
    media_type: MediaType = "unknown"
    image_url: str | None = None
    video_url: str | None = None
    extra_frame_urls: list[str] = Field(default_factory=list)
    link_urls: list[str] = Field(default_factory=list)
    caption: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class Classification(BaseModel):
    is_new_grad_swe: bool
    company: str | None = None
    role_title: str | None = None
    job_link: str | None = None
    urgency_score: int = Field(ge=1, le=5)
    reason: str = ""

    @field_validator("company", "role_title", "job_link", mode="before")
    @classmethod
    def _empty_optional(cls, value: object) -> object:
        return _blank_to_none(value)

    @field_validator("urgency_score", mode="before")
    @classmethod
    def _clamp_urgency(cls, value: object) -> object:
        try:
            score = int(value)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return 1
        return min(5, max(1, score))


class QuietQueueItem(BaseModel):
    story_id: str
    taken_at: datetime | None = None
    classification: Classification
    queued_at: datetime


class FailedClassificationItem(BaseModel):
    story_id: str
    error: str
    failed_at: datetime
    image_url: str | None = None
    video_url: str | None = None
    link_urls: list[str] = Field(default_factory=list)


CLASSIFICATION_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_new_grad_swe": {"type": "boolean"},
        "company": {"type": ["string", "null"]},
        "role_title": {"type": ["string", "null"]},
        "job_link": {"type": ["string", "null"]},
        "urgency_score": {"type": "integer", "minimum": 1, "maximum": 5},
        "reason": {"type": "string"},
    },
    "required": [
        "is_new_grad_swe",
        "company",
        "role_title",
        "job_link",
        "urgency_score",
        "reason",
    ],
}
