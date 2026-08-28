"""Download story frames and extract a middle video keyframe via ffmpeg."""

from __future__ import annotations

import logging
import subprocess
import tempfile
from pathlib import Path

import httpx

from app.http import MEDIA_TIMEOUT
from app.models import StoryAsset

logger = logging.getLogger(__name__)

FFPROBE_TIMEOUT_SECONDS = 30
FFMPEG_TIMEOUT_SECONDS = 60
FALLBACK_SEEK_SECONDS = 2.0


class MediaError(Exception):
    """Raised when a story frame cannot be prepared."""


def prepare_frame(client: httpx.Client, asset: StoryAsset) -> tuple[bytes, str]:
    if asset.media_type == "video" and asset.video_url:
        return _extract_video_keyframe(client, asset.video_url)
    image_url = _pick_image_url(asset)
    if image_url:
        return _download_image(client, image_url)
    if asset.video_url:
        return _extract_video_keyframe(client, asset.video_url)
    raise MediaError(f"story {asset.story_id} has no downloadable image or video URL")


def _pick_image_url(asset: StoryAsset) -> str | None:
    extras = [url for url in asset.extra_frame_urls if url]
    if len(extras) > 1:
        return extras[-1]
    if extras:
        return extras[0]
    return asset.image_url


def _download_image(client: httpx.Client, url: str) -> tuple[bytes, str]:
    try:
        response = client.get(url, timeout=MEDIA_TIMEOUT)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise MediaError(f"image download failed: {exc}") from exc
    payload = response.content
    if not payload:
        raise MediaError("image download returned empty body")
    return payload, _sniff_image_mime(payload, response.headers.get("content-type"))


def _extract_video_keyframe(client: httpx.Client, video_url: str) -> tuple[bytes, str]:
    with tempfile.TemporaryDirectory(prefix="swe-alert-") as tmp:
        tmpdir = Path(tmp)
        mp4_path = tmpdir / "story.mp4"
        jpg_path = tmpdir / "frame.jpg"
        _download_to_file(client, video_url, mp4_path)
        seek = _probe_seek_seconds(mp4_path)
        _run_ffmpeg(mp4_path, jpg_path, seek)
        if not jpg_path.is_file() or jpg_path.stat().st_size == 0:
            raise MediaError("ffmpeg produced an empty keyframe")
        return jpg_path.read_bytes(), "image/jpeg"


def _download_to_file(client: httpx.Client, url: str, dest: Path) -> None:
    try:
        with client.stream("GET", url, timeout=MEDIA_TIMEOUT) as response:
            response.raise_for_status()
            with dest.open("wb") as handle:
                for chunk in response.iter_bytes(65536):
                    handle.write(chunk)
    except httpx.HTTPError as exc:
        raise MediaError(f"video download failed: {exc}") from exc
    if dest.stat().st_size == 0:
        raise MediaError("video download returned empty body")


def _probe_seek_seconds(mp4_path: Path) -> float:
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(mp4_path),
            ],
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logger.warning("ffprobe unavailable or timed out (%s); seeking to %.1fs", exc, FALLBACK_SEEK_SECONDS)
        return FALLBACK_SEEK_SECONDS

    raw = (result.stdout or "").strip()
    try:
        duration = float(raw)
    except ValueError:
        logger.warning("ffprobe duration unreadable (%r); seeking to %.1fs", raw, FALLBACK_SEEK_SECONDS)
        return FALLBACK_SEEK_SECONDS
    if duration <= 0:
        return FALLBACK_SEEK_SECONDS
    return max(duration / 2.0, 0.0)


def _run_ffmpeg(src: Path, dest: Path, seek: float) -> None:
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{seek:.3f}",
                "-i",
                str(src),
                "-frames:v",
                "1",
                "-q:v",
                "2",
                str(dest),
            ],
            capture_output=True,
            text=True,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError as exc:
        raise MediaError("ffmpeg is not installed in this container/runtime") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError("ffmpeg timed out extracting a keyframe") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()[-500:]
        raise MediaError(f"ffmpeg failed (exit {result.returncode}): {stderr}")


def _sniff_image_mime(payload: bytes, header_value: str | None) -> str:
    if payload.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if payload[8:12] == b"WEBP":
        return "image/webp"
    if header_value:
        mime = header_value.split(";", 1)[0].strip().lower()
        if mime.startswith("image/"):
            return mime
    return "image/jpeg"
