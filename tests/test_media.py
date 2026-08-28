from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.media import MediaError, prepare_frame, _extract_video_keyframe, _sniff_image_mime
from app.models import StoryAsset


def test_image_download_uses_last_extra_frame(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_get(url, timeout=None):
        captured["url"] = url
        resp = MagicMock()
        resp.content = b"\xff\xd8\xff" + b"jpeg"
        resp.headers = {"content-type": "image/jpeg"}
        resp.raise_for_status = MagicMock()
        return resp

    client = MagicMock()
    client.get.side_effect = fake_get
    asset = StoryAsset(
        story_id="1",
        username="zero2sudo",
        media_type="image",
        image_url="https://cdn.example/first.jpg",
        extra_frame_urls=["https://cdn.example/second.jpg", "https://cdn.example/third.jpg"],
    )
    payload, mime = prepare_frame(client, asset)
    assert captured["url"] == "https://cdn.example/third.jpg"
    assert mime == "image/jpeg"
    assert payload.startswith(b"\xff\xd8\xff")


def test_video_tempdir_is_removed(monkeypatch) -> None:
    created: list[str] = []
    real_td = tempfile.TemporaryDirectory

    def tracking(*args, **kwargs):
        td = real_td(*args, **kwargs)
        created.append(td.name)
        return td

    monkeypatch.setattr("app.media.tempfile.TemporaryDirectory", tracking)
    monkeypatch.setattr("app.media._download_to_file", lambda client, url, dest: dest.write_bytes(b"fake-mp4"))
    monkeypatch.setattr("app.media._probe_seek_seconds", lambda path: 1.25)
    monkeypatch.setattr(
        "app.media._run_ffmpeg",
        lambda src, dest, seek: dest.write_bytes(b"\xff\xd8\xffframe"),
    )

    payload, mime = _extract_video_keyframe(MagicMock(), "https://cdn.example/story.mp4")
    assert mime == "image/jpeg"
    assert payload.startswith(b"\xff\xd8\xff")
    assert created
    assert not Path(created[0]).exists()


def test_video_cleanup_on_ffmpeg_failure(monkeypatch) -> None:
    created: list[str] = []
    real_td = tempfile.TemporaryDirectory

    def tracking(*args, **kwargs):
        td = real_td(*args, **kwargs)
        created.append(td.name)
        return td

    monkeypatch.setattr("app.media.tempfile.TemporaryDirectory", tracking)
    monkeypatch.setattr("app.media._download_to_file", lambda client, url, dest: dest.write_bytes(b"fake-mp4"))
    monkeypatch.setattr("app.media._probe_seek_seconds", lambda path: 2.0)

    def boom(src, dest, seek):
        raise MediaError("ffmpeg failed")

    monkeypatch.setattr("app.media._run_ffmpeg", boom)
    with pytest.raises(MediaError):
        _extract_video_keyframe(MagicMock(), "https://cdn.example/story.mp4")
    assert created
    assert not Path(created[0]).exists()


def test_sniff_image_mime() -> None:
    assert _sniff_image_mime(b"\xff\xd8\xffabc", None) == "image/jpeg"
    assert _sniff_image_mime(b"\x89PNG\r\n\x1a\n", None) == "image/png"
    assert _sniff_image_mime(b"RIFF....WEBP", "application/octet-stream") == "image/webp"


@pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_middle_keyframe() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "clip.mp4"
        dest = Path(tmp) / "frame.jpg"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=320x240:d=2",
                "-pix_fmt",
                "yuv420p",
                str(src),
            ],
            check=True,
            capture_output=True,
        )
        from app.media import _probe_seek_seconds, _run_ffmpeg

        seek = _probe_seek_seconds(src)
        assert seek >= 0.9
        _run_ffmpeg(src, dest, seek)
        assert dest.is_file() and dest.stat().st_size > 0
        assert dest.read_bytes()[:3] == b"\xff\xd8\xff"
