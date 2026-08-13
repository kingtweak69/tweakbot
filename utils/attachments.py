"""Attachment ingestion for TweakBot conversations.

Images/video frames are converted to textual vision context, audio is optionally
transcribed through an OpenAI-compatible transcription endpoint, and text/PDF/
ZIP content is inspected locally.  Nothing from an archive is executed here.
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import os
import shutil
import tempfile
import zipfile
from pathlib import PurePosixPath
from typing import Any

import aiohttp
import discord

import config

log = logging.getLogger("utils.attachments")

MAX_ATTACHMENTS = max(1, int(os.getenv("AI_MAX_ATTACHMENTS", "4")))
MAX_ATTACHMENT_BYTES = max(1_000_000, int(os.getenv("AI_MAX_ATTACHMENT_BYTES", "50000000")))
MAX_CONTEXT_CHARS = max(4000, int(os.getenv("AI_ATTACHMENT_CONTEXT_CHARS", "30000")))
MAX_TEXT_BYTES = max(100_000, int(os.getenv("AI_ATTACHMENT_TEXT_BYTES", "1000000")))
STT_MODEL = os.getenv("STT_MODEL", "whisper-1").strip() or "whisper-1"
VISION_MODEL = getattr(config, "VISION_MODEL", os.getenv("VISION_MODEL", "gpt-4o-mini"))

_TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env.example", ".sh",
    ".ps1", ".bat", ".cmd", ".java", ".kt", ".go", ".rs", ".c", ".h", ".cpp",
    ".hpp", ".cs", ".html", ".css", ".scss", ".sql", ".xml", ".csv", ".log",
}
_AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".webm"}
_VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _base_url(path: str) -> str:
    base = str(getattr(config, "OPENAI_BASE_URL", "") or "").rstrip("/")
    if base.endswith("/v1"):
        return base + path
    return base + "/v1" + path


def _headers() -> dict[str, str]:
    key = str(getattr(config, "OPENAI_API_KEY", "") or "")
    return {"Authorization": f"Bearer {key}"} if key else {}


def _suffix(name: str) -> str:
    lower = name.lower()
    # .env.example and similar multi-suffix files
    if lower.endswith(".env.example"):
        return ".env.example"
    idx = lower.rfind(".")
    return lower[idx:] if idx >= 0 else ""


class AttachmentAnalyzer:
    def __init__(self, bot) -> None:
        self.bot = bot

    async def attachments_for(self, message: discord.Message) -> list[discord.Attachment]:
        attachments = list(message.attachments)
        if not attachments and message.reference and message.reference.message_id:
            try:
                ref = await message.channel.fetch_message(message.reference.message_id)
                attachments = list(ref.attachments)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        return attachments[:MAX_ATTACHMENTS]

    async def analyze_message(self, message: discord.Message) -> str:
        attachments = await self.attachments_for(message)
        if not attachments:
            return ""
        sections: list[str] = []
        for attachment in attachments:
            try:
                sections.append(await self.analyze_attachment(attachment))
            except Exception as exc:
                log.exception("Attachment analysis failed for %s", attachment.filename)
                sections.append(
                    f"ATTACHMENT {attachment.filename}: analysis failed ({type(exc).__name__}: {exc})."
                )
            if sum(len(section) for section in sections) >= MAX_CONTEXT_CHARS:
                break
        return "\n\n".join(sections)[:MAX_CONTEXT_CHARS]

    async def analyze_attachment(self, attachment: discord.Attachment) -> str:
        name = attachment.filename or "attachment"
        suffix = _suffix(name)
        content_type = (attachment.content_type or "").lower()
        if attachment.size and attachment.size > MAX_ATTACHMENT_BYTES:
            return f"ATTACHMENT {name}: {attachment.size} bytes; too large for automatic analysis."

        if content_type.startswith("image/") or suffix in _IMAGE_EXTENSIONS:
            desc = await self._vision_url(
                attachment.url,
                "Describe this image/screenshot precisely for a technical assistant. Extract visible text, UI state, errors, code, and relevant visual details.",
            )
            return f"IMAGE {name}:\n{desc}"

        data = await attachment.read()
        if len(data) > MAX_ATTACHMENT_BYTES:
            return f"ATTACHMENT {name}: too large for automatic analysis."

        if content_type == "application/pdf" or suffix == ".pdf":
            return f"PDF {name}:\n{self._pdf_text(data)}"
        if content_type in {"application/zip", "application/x-zip-compressed"} or suffix == ".zip":
            return f"ZIP {name}:\n{self._zip_summary(data)}"
        if content_type.startswith("audio/") or suffix in _AUDIO_EXTENSIONS:
            transcript = await self._transcribe(data, name, content_type or "application/octet-stream")
            return f"AUDIO {name}:\n{transcript}"
        if content_type.startswith("video/") or suffix in _VIDEO_EXTENSIONS:
            return f"VIDEO {name}:\n{await self._video_summary(data, suffix or '.mp4')}"
        if content_type.startswith("text/") or suffix in _TEXT_EXTENSIONS:
            return f"TEXT FILE {name}:\n{self._decode_text(data)}"

        # Last chance: small UTF-8-ish files are useful even with bad MIME metadata.
        if len(data) <= MAX_TEXT_BYTES and b"\x00" not in data:
            try:
                text = data.decode("utf-8")
                return f"FILE {name}:\n{text[:MAX_CONTEXT_CHARS]}"
            except UnicodeDecodeError:
                pass
        return f"ATTACHMENT {name}: binary file, {len(data)} bytes, MIME {content_type or 'unknown'}."

    @staticmethod
    def _decode_text(data: bytes) -> str:
        data = data[:MAX_TEXT_BYTES]
        return data.decode("utf-8", errors="replace")[:MAX_CONTEXT_CHARS]

    @staticmethod
    def _pdf_text(data: bytes) -> str:
        try:
            from pypdf import PdfReader
        except ImportError:
            return "PDF parser unavailable (install pypdf)."
        reader = PdfReader(io.BytesIO(data))
        chunks: list[str] = []
        for index, page in enumerate(reader.pages[:30], 1):
            text = (page.extract_text() or "").strip()
            if text:
                chunks.append(f"--- page {index} ---\n{text}")
            if sum(map(len, chunks)) >= MAX_CONTEXT_CHARS:
                break
        return "\n".join(chunks)[:MAX_CONTEXT_CHARS] or "No extractable PDF text found."

    @staticmethod
    def _zip_summary(data: bytes) -> str:
        lines: list[str] = []
        snippets: list[str] = []
        total_uncompressed = 0
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()[:1000]
            for info in infos:
                p = PurePosixPath(info.filename)
                if p.is_absolute() or any(part in {"", ".", ".."} for part in p.parts):
                    continue
                if any(part in {".git", "__pycache__", "node_modules", ".venv", "venv"} for part in p.parts):
                    continue
                total_uncompressed += int(info.file_size or 0)
                if not info.is_dir() and len(lines) < 350:
                    lines.append(f"{info.file_size:>9} {p.as_posix()}")
                if info.is_dir() or info.file_size > 200_000 or len(snippets) >= 12:
                    continue
                if _suffix(p.name) not in _TEXT_EXTENSIONS and p.name not in {"Dockerfile", "Makefile"}:
                    continue
                try:
                    raw = zf.read(info)
                    if b"\x00" in raw:
                        continue
                    text = raw.decode("utf-8", errors="replace")
                    snippets.append(f"--- {p.as_posix()} ---\n{text[:2500]}")
                except Exception:
                    continue
        header = f"Archive entries shown: {len(lines)}; uncompressed bytes scanned: {total_uncompressed}\n"
        return (header + "\n".join(lines) + "\n\nSELECTED TEXT SNIPPETS\n" + "\n\n".join(snippets))[:MAX_CONTEXT_CHARS]

    async def _vision_url(self, url: str, instruction: str) -> str:
        if not getattr(config, "OPENAI_BASE_URL", ""):
            return "Vision endpoint is not configured."
        payload = {
            "model": VISION_MODEL,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": instruction},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }],
            "max_tokens": 900,
            "temperature": 0.1,
        }
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
            async with session.post(
                _base_url("/chat/completions"),
                json=payload,
                headers={**_headers(), "Content-Type": "application/json"},
            ) as response:
                raw = await response.text()
                if response.status >= 400:
                    return f"Vision request failed HTTP {response.status}: {raw[:500]}"
                try:
                    body = json.loads(raw)
                    message = (body.get("choices") or [{}])[0].get("message") or {}
                    content = message.get("content") or ""
                    if isinstance(content, list):
                        content = "".join(str(p.get("text") or "") for p in content if isinstance(p, dict))
                    return str(content).strip()[:MAX_CONTEXT_CHARS] or "Vision returned no description."
                except Exception:
                    return f"Vision returned an unreadable response: {raw[:500]}"

    async def _vision_bytes(self, data: bytes, mime: str, instruction: str) -> str:
        encoded = base64.b64encode(data).decode("ascii")
        return await self._vision_url(f"data:{mime};base64,{encoded}", instruction)

    async def _transcribe(self, data: bytes, filename: str, mime: str) -> str:
        form = aiohttp.FormData()
        form.add_field("model", STT_MODEL)
        form.add_field("file", data, filename=filename, content_type=mime)
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
            async with session.post(_base_url("/audio/transcriptions"), data=form, headers=_headers()) as response:
                raw = await response.text()
                if response.status >= 400:
                    return f"Transcription unavailable (HTTP {response.status}): {raw[:500]}"
                try:
                    body = json.loads(raw)
                    return str(body.get("text") or body.get("transcript") or raw).strip()[:MAX_CONTEXT_CHARS]
                except json.JSONDecodeError:
                    return raw[:MAX_CONTEXT_CHARS]

    async def _video_summary(self, data: bytes, suffix: str) -> str:
        if not shutil.which("ffmpeg"):
            return f"Video received ({len(data)} bytes), but ffmpeg is unavailable for frame extraction."
        with tempfile.TemporaryDirectory(prefix="tweakbot-video-") as tmp:
            src = os.path.join(tmp, "input" + suffix)
            with open(src, "wb") as handle:
                handle.write(data)
            frames: list[bytes] = []
            for idx, seconds in enumerate((0.5, 5.0)):
                out = os.path.join(tmp, f"frame{idx}.jpg")
                proc = await asyncio.create_subprocess_exec(
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", str(seconds),
                    "-i", src, "-frames:v", "1", "-q:v", "3", "-y", out,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
                try:
                    await asyncio.wait_for(proc.communicate(), timeout=30)
                except asyncio.TimeoutError:
                    proc.kill(); await proc.communicate()
                if os.path.isfile(out):
                    with open(out, "rb") as handle:
                        frames.append(handle.read())
            if not frames:
                return f"Video received ({len(data)} bytes); no frames could be extracted."
            descriptions = []
            for index, frame in enumerate(frames, 1):
                desc = await self._vision_bytes(
                    frame,
                    "image/jpeg",
                    f"Describe representative video frame {index}. Extract visible text/UI and explain the scene relevant to the user's request.",
                )
                descriptions.append(f"Frame {index}: {desc}")
            return "\n".join(descriptions)[:MAX_CONTEXT_CHARS]
