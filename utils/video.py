"""
Video generation layer for TweakBot.

Provider-agnostic: the cog only ever talks to VideoProvider. Adding Runway,
PixVerse, or anything else means writing one subclass, not touching the cog.

Video "extend" here does NOT use a provider's native extend endpoint. Runway's
native extend only works on clips Runway itself generated (it takes a task ID,
not a file), which would mean users could never extend their own uploads.
Instead we pull the last stable frame with ffmpeg and feed it back through
image-to-video, then concatenate. That works on any video from any source.

VERIFY BEFORE SPENDING MONEY: the three marked blocks in KlingProvider
(_submit payload, _poll response parsing, endpoint constants) are the only
provider-specific parts. Check them against current Kling API docs — vendors
rename fields without warning. Everything else is provider-independent.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("utils.video")

# Cap on how much video we'll pull down from a provider or from Discord.
MAX_VIDEO_BYTES = 200 * 1024 * 1024
MAX_IMAGE_BYTES = 20 * 1024 * 1024

FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")


class VideoError(Exception):
    """Anything that went wrong that the user should hear about."""


@dataclass
class VideoJob:
    """A submitted generation, before we know how it turned out."""
    provider: str
    external_id: str
    duration_s: int
    cost_cents: int


@dataclass
class VideoResult:
    """The outcome of polling a job."""
    status: str          # "pending" | "done" | "failed"
    url: str | None = None
    error: str | None = None


# ── ffmpeg helpers ────────────────────────────────────────────────────────────

async def _run(*args: str, timeout: int = 300) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise VideoError("ffmpeg timed out.")
    return proc.returncode, out, err


def ffmpeg_available() -> bool:
    return bool(FFMPEG and FFPROBE)


async def probe_duration(path: str) -> float:
    """Length of a video in seconds."""
    if not FFPROBE:
        raise VideoError("ffprobe is not installed on this host.")
    code, out, err = await _run(
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        path,
        timeout=60,
    )
    if code != 0:
        raise VideoError("Could not read that video — is it a valid MP4?")
    try:
        return float(out.decode().strip())
    except ValueError:
        raise VideoError("Could not read that video's duration.")


async def extract_last_frame(video_path: str, out_path: str, back_off_s: float = 0.4) -> str:
    """
    Grab a frame from just before the end. We back off slightly because the
    literal final frame is often a fade, a black frame, or motion-blurred,
    and feeding that into image-to-video produces a dead first second.
    """
    if not FFMPEG:
        raise VideoError("ffmpeg is not installed on this host.")
    code, _, err = await _run(
        FFMPEG, "-y",
        "-sseof", f"-{back_off_s}",
        "-i", video_path,
        "-update", "1",
        "-frames:v", "1",
        "-q:v", "2",
        out_path,
        timeout=120,
    )
    if code != 0 or not os.path.exists(out_path):
        log.error("last-frame extraction failed: %s", err.decode()[-800:])
        raise VideoError("Could not pull a frame from the end of that video.")
    return out_path


async def concat_videos(paths: list[str], out_path: str) -> str:
    """
    Join clips end to end. Tries a stream copy first (instant, lossless); if the
    clips have mismatched codecs it falls back to re-encoding.
    """
    if not FFMPEG:
        raise VideoError("ffmpeg is not installed on this host.")

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
        list_path = f.name

    try:
        code, _, _ = await _run(
            FFMPEG, "-y", "-f", "concat", "-safe", "0",
            "-i", list_path, "-c", "copy", out_path,
        )
        if code == 0 and os.path.exists(out_path):
            return out_path

        # Mismatched streams — re-encode.
        code, _, err = await _run(
            FFMPEG, "-y", "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac", "-movflags", "+faststart",
            out_path,
            timeout=600,
        )
        if code != 0 or not os.path.exists(out_path):
            log.error("concat failed: %s", err.decode()[-800:])
            raise VideoError("Could not stitch the clips together.")
        return out_path
    finally:
        try:
            os.unlink(list_path)
        except OSError:
            pass


async def shrink_to_fit(path: str, out_path: str, target_bytes: int) -> str | None:
    """
    Re-encode to land under a byte budget. Returns None if we can't get there
    without destroying it — the caller should post a link instead.
    """
    if not FFMPEG:
        return None
    duration = await probe_duration(path)
    if duration <= 0:
        return None

    # Leave 12% headroom for container overhead and audio.
    target_bits = target_bytes * 8 * 0.88
    bitrate = int(target_bits / duration)
    if bitrate < 200_000:
        return None

    code, _, err = await _run(
        FFMPEG, "-y", "-i", path,
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", str(bitrate), "-maxrate", str(bitrate), "-bufsize", str(bitrate * 2),
        "-vf", "scale='min(1280,iw)':-2",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        out_path,
        timeout=900,
    )
    if code != 0 or not os.path.exists(out_path):
        log.error("shrink failed: %s", err.decode()[-800:])
        return None
    if os.path.getsize(out_path) > target_bytes:
        return None
    return out_path


# ── Download helper ───────────────────────────────────────────────────────────

async def download(session: aiohttp.ClientSession, url: str, dest: str, max_bytes: int) -> str:
    """Stream a URL to disk, aborting if it goes over budget."""
    timeout = aiohttp.ClientTimeout(total=300)
    written = 0
    async with session.get(url, timeout=timeout) as resp:
        if resp.status != 200:
            raise VideoError(f"Download failed (HTTP {resp.status}).")
        declared = resp.headers.get("Content-Length")
        if declared and int(declared) > max_bytes:
            raise VideoError("That file is too large to process.")
        with open(dest, "wb") as f:
            async for chunk in resp.content.iter_chunked(64 * 1024):
                written += len(chunk)
                if written > max_bytes:
                    raise VideoError("That file is too large to process.")
                f.write(chunk)
    return dest


# ── Provider interface ────────────────────────────────────────────────────────

class VideoProvider(ABC):
    """One subclass per vendor. The cog only knows about this."""

    name: str = "base"
    cost_cents_per_second: float = 0.0
    min_duration: int = 5
    max_duration: int = 10

    def __init__(self, api_key: str, session: aiohttp.ClientSession):
        self.api_key = api_key
        self.session = session

    def estimate_cents(self, duration_s: int) -> int:
        return max(1, round(self.cost_cents_per_second * duration_s))

    def clamp_duration(self, duration_s: int) -> int:
        return max(self.min_duration, min(self.max_duration, duration_s))

    @abstractmethod
    async def animate(self, image_url: str, prompt: str, duration_s: int) -> VideoJob:
        """Submit an image-to-video job. Returns immediately with a job handle."""

    @abstractmethod
    async def poll(self, external_id: str) -> VideoResult:
        """Check on a submitted job."""


class KlingProvider(VideoProvider):
    """
    Kling 3.0 image-to-video.

    Kling's public API is JWT-authenticated: you're issued an access key and a
    secret, and you sign a short-lived token. If your key is from a reseller
    (Kie.ai, fal.ai, Apiframe, PiAPI) it's usually a plain bearer token instead
    — set KLING_AUTH_STYLE=bearer in that case.
    """

    name = "kling"
    cost_cents_per_second = 10.0   # ~$0.10/sec standard mode
    min_duration = 5
    max_duration = 10

    # ── VERIFY THIS BLOCK against current docs ────────────────────────────────
    BASE = os.getenv("KLING_BASE_URL", "https://api.klingai.com")
    SUBMIT_PATH = "/v1/videos/image2video"
    STATUS_PATH = "/v1/videos/image2video/{id}"
    MODEL = os.getenv("KLING_MODEL", "kling-v3")
    # ──────────────────────────────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def animate(self, image_url: str, prompt: str, duration_s: int) -> VideoJob:
        duration_s = self.clamp_duration(duration_s)

        # ── VERIFY THIS PAYLOAD ───────────────────────────────────────────────
        payload = {
            "model_name": self.MODEL,
            "image": image_url,
            "prompt": prompt[:2000],
            "duration": str(duration_s),
            "mode": "std",
            "cfg_scale": 0.5,
        }
        # ──────────────────────────────────────────────────────────────────────

        url = f"{self.BASE}{self.SUBMIT_PATH}"
        timeout = aiohttp.ClientTimeout(total=60)
        async with self.session.post(url, json=payload, headers=self._headers(), timeout=timeout) as resp:
            body = await resp.text()
            if resp.status >= 400:
                log.error("Kling submit %s: %s", resp.status, body[:600])
                raise VideoError(f"The video service rejected that request (HTTP {resp.status}).")
            try:
                data = json.loads(body)
            except ValueError:
                raise VideoError("The video service returned something unreadable.")

        external_id = self._extract_id(data)
        if not external_id:
            log.error("Kling submit had no task id: %s", body[:600])
            raise VideoError("The video service did not return a job ID.")

        return VideoJob(
            provider=self.name,
            external_id=external_id,
            duration_s=duration_s,
            cost_cents=self.estimate_cents(duration_s),
        )

    async def poll(self, external_id: str) -> VideoResult:
        url = f"{self.BASE}{self.STATUS_PATH.format(id=external_id)}"
        timeout = aiohttp.ClientTimeout(total=60)
        try:
            async with self.session.get(url, headers=self._headers(), timeout=timeout) as resp:
                body = await resp.text()
                if resp.status >= 500:
                    return VideoResult(status="pending")
                if resp.status >= 400:
                    log.error("Kling poll %s: %s", resp.status, body[:600])
                    return VideoResult(status="failed", error=f"HTTP {resp.status}")
                data = json.loads(body)
        except (aiohttp.ClientError, asyncio.TimeoutError):
            return VideoResult(status="pending")
        except ValueError:
            return VideoResult(status="failed", error="unreadable response")

        # ── VERIFY THIS PARSING ───────────────────────────────────────────────
        payload = data.get("data", data)
        raw_status = str(payload.get("task_status") or payload.get("status") or "").lower()

        if raw_status in ("succeed", "success", "succeeded", "completed", "done"):
            video_url = self._extract_url(payload)
            if not video_url:
                return VideoResult(status="failed", error="finished with no video URL")
            return VideoResult(status="done", url=video_url)

        if raw_status in ("failed", "fail", "error", "cancelled", "canceled"):
            reason = payload.get("task_status_msg") or payload.get("message") or "generation failed"
            return VideoResult(status="failed", error=str(reason)[:300])
        # ──────────────────────────────────────────────────────────────────────

        return VideoResult(status="pending")

    @staticmethod
    def _extract_id(data: dict) -> str | None:
        payload = data.get("data", data)
        for key in ("task_id", "taskId", "id", "job_id"):
            value = payload.get(key)
            if value:
                return str(value)
        return None

    @staticmethod
    def _extract_url(payload: dict) -> str | None:
        result = payload.get("task_result") or payload.get("result") or payload
        videos = result.get("videos") or result.get("video") or []
        if isinstance(videos, dict):
            videos = [videos]
        for item in videos:
            if isinstance(item, dict):
                for key in ("url", "video_url", "resource_url"):
                    if item.get(key):
                        return str(item[key])
            elif isinstance(item, str):
                return item
        for key in ("video_url", "url"):
            if result.get(key):
                return str(result[key])
        return None


PROVIDERS: dict[str, type[VideoProvider]] = {
    "kling": KlingProvider,
}


def build_provider(name: str, api_key: str, session: aiohttp.ClientSession) -> VideoProvider | None:
    cls = PROVIDERS.get((name or "").lower())
    if not cls or not api_key:
        return None
    return cls(api_key, session)
