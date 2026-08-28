from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.classifier import ClassificationError, GeminiClassifier, SYSTEM_INSTRUCTION
from app.models import Classification, StoryAsset
from tests.conftest import make_settings


def _asset() -> StoryAsset:
    return StoryAsset(
        story_id="s1",
        username="zero2sudo",
        media_type="image",
        link_urls=["https://boards.greenhouse.io/stripe/jobs/1"],
        caption="new grad swe",
    )


def _payload(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "is_new_grad_swe": True,
        "company": "Stripe",
        "role_title": "New Grad SWE",
        "job_link": "https://boards.greenhouse.io/stripe/jobs/1",
        "urgency_score": 5,
        "reason": "Apply sticker for new-grad SWE",
    }
    data.update(overrides)
    return data


def test_classifier_parses_strict_json() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(text=json.dumps(_payload()), parsed=None)
    classifier = GeminiClassifier(make_settings(), client=client)
    result = classifier.classify(_asset(), b"\xff\xd8\xff" + b"x" * 16, "image/jpeg")
    assert isinstance(result, Classification)
    assert result.company == "Stripe"
    assert result.urgency_score == 5
    assert result.is_new_grad_swe is True
    client.models.generate_content.assert_called_once()


def test_classifier_clamps_urgency_and_blanks() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(
        text=json.dumps(_payload(urgency_score=99, company="", job_link="")),
        parsed=None,
    )
    result = GeminiClassifier(make_settings(), client=client).classify(_asset(), b"img", "image/jpeg")
    assert result.urgency_score == 5
    assert result.company is None
    assert result.job_link is None


def test_classifier_retries_then_raises() -> None:
    client = MagicMock()
    client.models.generate_content.return_value = SimpleNamespace(text="not-json", parsed=None)
    with pytest.raises(ClassificationError, match="JSON parse"):
        GeminiClassifier(make_settings(), client=client).classify(_asset(), b"bytes", "image/jpeg")
    assert client.models.generate_content.call_count == 2


def test_system_prompt_is_strict() -> None:
    lowered = SYSTEM_INSTRUCTION.lower()
    assert "is_new_grad_swe=true only" in lowered
    assert "quant" in lowered
    assert "lifestyle" in lowered
