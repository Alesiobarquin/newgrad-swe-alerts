from __future__ import annotations

from datetime import datetime, timezone

from app.models import StoryAsset
from app.scrapers.normalize import normalize_payload


def test_instagram_private_payload() -> None:
    payload = {
        "data": {
            "stories": [
                {
                    "id": "3920998746292350747_232192182",
                    "pk": "3920998746292350747",
                    "taken_at": 1781637893,
                    "media_type": 2,
                    "video_versions": [{"url": "https://cdn.example/video.mp4"}],
                    "image_versions2": {
                        "candidates": [
                            {"url": "https://cdn.example/frame0.jpg"},
                            {"url": "https://cdn.example/frame1.jpg"},
                        ]
                    },
                    "story_link_stickers": [{"story_link": {"url": "https://jobs.example/apply"}}],
                    "extractedCaption": "SWE drop",
                }
            ]
        }
    }
    stories = normalize_payload(payload, username="zero2sudo", items_path="data.stories")
    assert len(stories) == 1
    story = stories[0]
    assert story.story_id == "3920998746292350747_232192182"
    assert story.media_type == "video"
    assert story.video_url.endswith(".mp4")
    assert story.image_url == "https://cdn.example/frame0.jpg"
    assert story.extra_frame_urls == ["https://cdn.example/frame1.jpg"]
    assert story.link_urls == ["https://jobs.example/apply"]
    assert story.caption == "SWE drop"
    assert story.taken_at == datetime.fromtimestamp(1781637893, tz=timezone.utc)


def test_rapidapi_flat_story_id_shape() -> None:
    payload = {
        "data": {
            "stories": [
                {
                    "storyId": "abc123",
                    "mediaType": "image",
                    "mediaUrl": "https://cdn.example/story.jpg",
                    "links": ["https://boards.greenhouse.io/acme/jobs/1"],
                    "timestamp": "2026-08-26T12:00:00Z",
                }
            ]
        }
    }
    stories = normalize_payload(payload, username="zero2sudo", items_path="data.stories")
    assert stories[0].story_id == "abc123"
    assert stories[0].media_type == "image"
    assert stories[0].link_urls[0].startswith("https://boards.greenhouse.io")


def test_nested_user_stories_wrapper() -> None:
    payload = [{"username": "zero2sudo", "stories": [{"pk": "99", "displayUrl": "https://cdn.example/a.jpg"}]}]
    stories = normalize_payload(payload, username="zero2sudo", items_path="")
    assert len(stories) == 1
    assert stories[0].story_id == "99"


def test_skips_items_without_id() -> None:
    payload = {"data": {"stories": [{"mediaUrl": "https://cdn.example/x.jpg"}]}}
    assert normalize_payload(payload, username="zero2sudo", items_path="data.stories") == []


def test_story_asset_roundtrip() -> None:
    asset = StoryAsset(story_id="1", username="zero2sudo", image_url="https://cdn.example/a.jpg")
    assert asset.media_type == "unknown"
