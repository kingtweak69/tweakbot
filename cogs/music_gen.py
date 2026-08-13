"""Music generation with your own voice on the vocals.

ACE Music writes and performs the music. ElevenLabs never sings — it only
does speech-to-speech conversion, swapping the AI singer's timbre for your
cloned voice while keeping the melody, timing, and delivery from ACE.

The hosted ACE Music API (api.acemusic.ai) is OpenAI-compatible and
synchronous: one POST to /v1/chat/completions returns the finished audio as
a base64 data URL. There is no job queue to poll.

Pipeline for vocals:
    1. ACE Music `cover` over the instrumental  -> aligned track with vocals
    2. ElevenLabs audio-isolation               -> vocal stem only
    3. ElevenLabs speech-to-speech              -> that stem in your voice
    4. ffmpeg amix                              -> your vocal over the
                                                   original instrumental

Commands:
    $gen <prompt>                  instrumental only
    $gen song <prompt>             instrumental, then vocals in your voice
    $gen apply <lyrics or style>   add your vocals to an existing instrumental
    $gen voice                     convert any audio into your voice
    $gen models                    list ACE models

Flags (any order, anywhere in the line):
    --len 60          duration in seconds
    --bpm 90          tempo
    --key "F minor"   key / scale
    --seed 42         reproducible generation
    --lang en         vocal language
    --lyrics "..."    custom lyrics
    --strength 0.55   cover strength, 0.0-1.0
    --vox 1.6         vocal gain in the final mix
    --isolate         ($gen voice) strip backing before converting

Required environment variables:
    ACE_MUSIC_API_KEY
    ELEVENLABS_API_KEY
    ELEVENLABS_VOICE_ID

Optional environment variables:
    ACE_MUSIC_API_URL=https://api.acemusic.ai
    ACE_MUSIC_MODEL=
    ACE_MUSIC_MAX_DURATION=240
    ELEVENLABS_STS_MODEL_ID=eleven_multilingual_sts_v2
    ELEVENLABS_OUTPUT_FORMAT=mp3_44100_128
    FFMPEG_BINARY=ffmpeg

Requires the `ffmpeg` binary on PATH.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
from typing import Any

import aiohttp
import discord
from discord.ext import commands

from utils.helpers import error_embed, info_embed

log = logging.getLogger("cogs.music_gen")

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

ACE_BASE = (
    os.getenv("ACE_MUSIC_API_URL", "https://api.acemusic.ai").strip().rstrip("/")
    or "https://api.acemusic.ai"
)
ACE_KEY = os.getenv("ACE_MUSIC_API_KEY", "").strip()
ACE_MODEL = os.getenv("ACE_MUSIC_MODEL", "").strip()
ACE_FALLBACK_MODEL = "acemusic/acestep-v15-turbo"

ELEVEN_BASE = "https://api.elevenlabs.io/v1"
ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVEN_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "").strip()
ELEVEN_STS_MODEL = (
    os.getenv("ELEVENLABS_STS_MODEL_ID", "eleven_multilingual_sts_v2").strip()
    or "eleven_multilingual_sts_v2"
)
ELEVEN_OUTPUT_FORMAT = (
    os.getenv("ELEVENLABS_OUTPUT_FORMAT", "mp3_44100_128").strip() or "mp3_44100_128"
)

FFMPEG_BINARY = os.getenv("FFMPEG_BINARY", "ffmpeg").strip() or "ffmpeg"

try:
    MAX_DURATION = float(os.getenv("ACE_MUSIC_MAX_DURATION", "240"))
except ValueError:
    MAX_DURATION = 240.0

MIN_DURATION = 10.0
DEFAULT_DURATION = 60.0

# ElevenLabs speech-to-speech caps at 5 minutes and 50 MB per request.
STS_MAX_SECONDS = 280.0

# The cloud endpoint blocks for the whole generation, so this has to be long.
ACE_TIMEOUT = aiohttp.ClientTimeout(total=700, connect=20)
ELEVEN_TIMEOUT = aiohttp.ClientTimeout(total=300)
HEARTBEAT_INTERVAL = 15.0
FFMPEG_TIMEOUT = 180

MAX_PROMPT_LENGTH = 1000
MAX_LYRICS_LENGTH = 3000
MAX_INPUT_BYTES = 25 * 1024 * 1024
HISTORY_SEARCH_LIMIT = 25

DEFAULT_COVER_STRENGTH = 0.55
DEFAULT_VOX_GAIN = 1.6

AUDIO_EXTENSIONS = (
    ".mp3",
    ".wav",
    ".ogg",
    ".oga",
    ".opus",
    ".m4a",
    ".aac",
    ".flac",
    ".webm",
    ".mp4",
)

FLAG_PATTERN = re.compile(
    r"--(?P<name>[a-zA-Z_]+)(?:(?:[=\s]+)(?P<value>\"[^\"]*\"|'[^']*'|\S+))?"
)

FLAG_ALIASES = {
    "len": "duration",
    "length": "duration",
    "duration": "duration",
    "secs": "duration",
    "seconds": "duration",
    "bpm": "bpm",
    "tempo": "bpm",
    "key": "key",
    "keyscale": "key",
    "scale": "key",
    "seed": "seed",
    "lang": "language",
    "language": "language",
    "lyrics": "lyrics",
    "strength": "strength",
    "cover": "strength",
    "vox": "vox",
    "vocals": "vox",
    "isolate": "isolate",
}

BOOLEAN_FLAGS = {"isolate"}


class PipelineError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Flag parsing
# --------------------------------------------------------------------------


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _parse_flags(raw: str) -> tuple[str, dict[str, str]]:
    flags: dict[str, str] = {}

    def _capture(match: re.Match[str]) -> str:
        name = FLAG_ALIASES.get(match.group("name").lower())

        if name is None:
            return match.group(0)

        if name in BOOLEAN_FLAGS:
            flags[name] = "true"
            return f" {match.group('value') or ''} "

        if match.group("value") is None:
            return match.group(0)

        flags[name] = _strip_quotes(match.group("value"))
        return " "

    prompt = FLAG_PATTERN.sub(_capture, raw)

    return re.sub(r"\s+", " ", prompt).strip(), flags


def _float_flag(
    flags: dict[str, str],
    name: str,
    low: float,
    high: float,
) -> float | None:
    if name not in flags:
        return None

    try:
        value = float(flags[name])
    except ValueError as exc:
        raise PipelineError(f"`--{name} {flags[name]}` is not a number.") from exc

    if not low <= value <= high:
        raise PipelineError(f"`--{name}` must be between {low:g} and {high:g}.")

    return value


def _build_audio_config(flags: dict[str, str]) -> dict[str, Any]:
    config: dict[str, Any] = {
        "format": "mp3",
        "duration": DEFAULT_DURATION,
    }

    duration = _float_flag(flags, "duration", MIN_DURATION, MAX_DURATION)
    if duration is not None:
        config["duration"] = duration

    bpm = _float_flag(flags, "bpm", 30, 300)
    if bpm is not None:
        config["bpm"] = int(bpm)

    if "key" in flags:
        config["key_scale"] = flags["key"][:40]

    if "language" in flags:
        config["vocal_language"] = flags["language"][:8]
    else:
        config["vocal_language"] = "en"

    return config


# --------------------------------------------------------------------------
# ffmpeg helpers
# --------------------------------------------------------------------------


async def _run_ffmpeg(args: list[str], stdin: bytes | None = None) -> bytes:
    try:
        process = await asyncio.create_subprocess_exec(
            FFMPEG_BINARY,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise PipelineError(
            f"`{FFMPEG_BINARY}` is not installed on this host."
        ) from exc

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(stdin),
            timeout=FFMPEG_TIMEOUT,
        )
    except asyncio.TimeoutError as exc:
        process.kill()
        await process.wait()
        raise PipelineError("ffmpeg took too long and was killed.") from exc

    if process.returncode != 0:
        detail = stderr.decode("utf-8", errors="replace").strip()[:400]
        raise PipelineError(f"ffmpeg failed: {detail or 'unknown error'}")

    return stdout


async def _compress_for_upload(audio: bytes) -> bytes:
    """The cloud edge rejects large uploads, so shrink the reference."""
    return await _run_ffmpeg(
        [
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "24000",
            "-b:a",
            "64k",
            "-f",
            "mp3",
            "pipe:1",
        ],
        audio,
    )


async def _mix(instrumental: bytes, vocal: bytes, vox_gain: float) -> bytes:
    """Lay the converted vocal over the original instrumental."""
    with tempfile.TemporaryDirectory(prefix="tweakbot-mix-") as workdir:
        inst_path = os.path.join(workdir, "inst.mp3")
        vox_path = os.path.join(workdir, "vox.mp3")

        with open(inst_path, "wb") as handle:
            handle.write(instrumental)

        with open(vox_path, "wb") as handle:
            handle.write(vocal)

        filter_complex = (
            f"[1:a]volume={vox_gain:.2f},aresample=44100[v];"
            "[0:a]aresample=44100[b];"
            "[b][v]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[m];"
            "[m]alimiter=limit=0.95[out]"
        )

        return await _run_ffmpeg(
            [
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                inst_path,
                "-i",
                vox_path,
                "-filter_complex",
                filter_complex,
                "-map",
                "[out]",
                "-f",
                "mp3",
                "-b:a",
                "192k",
                "pipe:1",
            ]
        )


# --------------------------------------------------------------------------
# ACE Music client (OpenAI-compatible completion endpoint)
# --------------------------------------------------------------------------


class AceMusicClient:
    def __init__(self, api_key: str, base_url: str):
        self.api_key = api_key
        self.base_url = base_url
        self.session: aiohttp.ClientSession | None = None
        self._model: str | None = ACE_MODEL or None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=ACE_TIMEOUT)
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    @property
    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    async def list_models(self) -> list[dict[str, Any]]:
        session = await self._get_session()

        try:
            async with session.get(
                f"{self.base_url}/v1/models",
                headers=self._auth,
            ) as response:
                if response.status == 401:
                    raise PipelineError("ACE Music rejected the API key.")

                if response.status >= 400:
                    raise PipelineError(
                        f"ACE Music returned HTTP {response.status} for /v1/models."
                    )

                body = await response.json(content_type=None)

        except asyncio.TimeoutError as exc:
            raise PipelineError("ACE Music timed out.") from exc

        except aiohttp.ClientError as exc:
            raise PipelineError(f"Could not reach ACE Music: {exc}") from exc

        models = body.get("data") if isinstance(body, dict) else None

        return models if isinstance(models, list) else []

    async def model_id(self) -> str:
        if self._model:
            return self._model

        try:
            models = await self.list_models()
        except PipelineError:
            models = []

        for entry in models:
            if isinstance(entry, dict) and entry.get("id"):
                self._model = str(entry["id"])
                return self._model

        self._model = ACE_FALLBACK_MODEL
        return self._model

    @staticmethod
    def _decode_audio(data_url: str) -> bytes:
        marker = "base64,"
        index = data_url.find(marker)
        payload = data_url[index + len(marker) :] if index != -1 else data_url

        try:
            return base64.b64decode(payload, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise PipelineError("Could not decode the audio ACE Music returned.") from exc

    async def generate(
        self,
        prompt: str,
        *,
        lyrics: str = "",
        description: str = "",
        audio_config: dict[str, Any] | None = None,
        src_audio: bytes | None = None,
        task_type: str = "text2music",
        cover_strength: float | None = None,
        seed: int | None = None,
    ) -> tuple[bytes, str]:
        """Returns (audio_bytes, text_metadata)."""
        if description:
            text_content = description
            sample_mode = True
        else:
            text_content = f"<prompt>{prompt}</prompt>"
            if lyrics:
                text_content += f"<lyrics>{lyrics}</lyrics>"
            sample_mode = False

        payload: dict[str, Any] = {
            "model": await self.model_id(),
            "stream": False,
            "thinking": True,
            "use_format": True,
            "sample_mode": sample_mode,
            "use_cot_caption": True,
            "use_cot_language": True,
            "batch_size": 1,
            "audio_config": audio_config or {"format": "mp3"},
        }

        if seed is not None:
            payload["seed"] = seed

        if src_audio is None:
            payload["messages"] = [{"role": "user", "content": text_content}]
        else:
            encoded = base64.b64encode(src_audio).decode("ascii")
            payload["messages"] = [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text_content},
                        {
                            "type": "input_audio",
                            "input_audio": {"data": encoded, "format": "mp3"},
                        },
                    ],
                }
            ]
            payload["task_type"] = task_type

            if cover_strength is not None:
                payload["audio_cover_strength"] = cover_strength

        session = await self._get_session()

        try:
            async with session.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers={
                    **self._auth,
                    "Content-Type": "application/json; charset=utf-8",
                },
            ) as response:
                raw = await response.text()

                if response.status == 401:
                    raise PipelineError("ACE Music rejected the API key.")

                if response.status == 429:
                    raise PipelineError("ACE Music is rate limiting. Try again shortly.")

                if response.status >= 400:
                    detail = raw[:300]

                    try:
                        parsed_error = json.loads(raw)
                        detail = (
                            parsed_error.get("detail")
                            or (parsed_error.get("error") or {}).get("message")
                            or detail
                        )
                    except (json.JSONDecodeError, AttributeError):
                        pass

                    raise PipelineError(
                        f"ACE Music returned HTTP {response.status}: {detail}"
                    )

        except asyncio.TimeoutError as exc:
            raise PipelineError("ACE Music timed out.") from exc

        except aiohttp.ClientError as exc:
            raise PipelineError(f"Could not reach ACE Music: {exc}") from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PipelineError("ACE Music returned a malformed response.") from exc

        choices = data.get("choices") or []

        if not choices:
            raise PipelineError("ACE Music returned no result.")

        choice = choices[0]
        message = choice.get("message") or {}

        if choice.get("finish_reason") == "error":
            detail = message.get("content") or "unknown error"
            raise PipelineError(f"Generation failed: {str(detail)[:300]}")

        audio_entries = message.get("audio") or []

        if not audio_entries:
            raise PipelineError("ACE Music returned no audio.")

        audio_url = (audio_entries[0].get("audio_url") or {}).get("url")

        if not audio_url:
            raise PipelineError("ACE Music returned an empty audio URL.")

        audio = self._decode_audio(audio_url)

        if not audio:
            raise PipelineError("The decoded track was empty.")

        return audio, str(message.get("content") or "")


# --------------------------------------------------------------------------
# ElevenLabs client (isolation + voice conversion only, never synthesis)
# --------------------------------------------------------------------------


class VoiceClient:
    def __init__(self, api_key: str, voice_id: str):
        self.api_key = api_key
        self.voice_id = voice_id
        self.session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=ELEVEN_TIMEOUT)
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    async def _post_audio(self, url: str, form: aiohttp.FormData, what: str) -> bytes:
        session = await self._get_session()

        try:
            async with session.post(
                url,
                data=form,
                headers={"xi-api-key": self.api_key, "Accept": "audio/mpeg"},
            ) as response:
                body = await response.read()

                if response.status >= 400:
                    detail = body.decode("utf-8", errors="replace")[:400]
                    raise PipelineError(
                        f"ElevenLabs {what} returned HTTP {response.status}: "
                        f"{detail or 'request failed'}"
                    )

        except asyncio.TimeoutError as exc:
            raise PipelineError(f"ElevenLabs {what} timed out.") from exc

        except aiohttp.ClientError as exc:
            raise PipelineError(f"Could not reach ElevenLabs: {exc}") from exc

        if not body:
            raise PipelineError(f"ElevenLabs {what} returned empty audio.")

        return body

    async def isolate(self, audio: bytes) -> bytes:
        """Strip the backing track, leaving the vocal performance."""
        form = aiohttp.FormData()
        form.add_field("audio", audio, filename="mix.mp3", content_type="audio/mpeg")

        return await self._post_audio(
            f"{ELEVEN_BASE}/audio-isolation",
            form,
            "isolation",
        )

    async def convert(self, audio: bytes) -> bytes:
        """Swap the performer's timbre for the cloned voice."""
        form = aiohttp.FormData()
        form.add_field("audio", audio, filename="vocal.mp3", content_type="audio/mpeg")
        form.add_field("model_id", ELEVEN_STS_MODEL)
        form.add_field("remove_background_noise", "true")
        form.add_field(
            "voice_settings",
            json.dumps(
                {
                    "stability": 0.35,
                    "similarity_boost": 0.9,
                    "style": 0.4,
                    "use_speaker_boost": True,
                }
            ),
        )

        url = (
            f"{ELEVEN_BASE}/speech-to-speech/{self.voice_id}"
            f"?output_format={ELEVEN_OUTPUT_FORMAT}"
        )

        return await self._post_audio(url, form, "voice conversion")


# --------------------------------------------------------------------------
# Cog
# --------------------------------------------------------------------------


def _is_audio(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()

    if content_type.startswith(("audio/", "video/")):
        return True

    return attachment.filename.lower().endswith(AUDIO_EXTENSIONS)


class MusicGen(commands.Cog):
    """Generate instrumentals, and put your own voice on the vocals."""

    def __init__(self, bot):
        self.bot = bot
        self.ace = AceMusicClient(ACE_KEY, ACE_BASE)
        self.voice = VoiceClient(ELEVEN_KEY, ELEVEN_VOICE_ID)
        self._locks: dict[int, asyncio.Lock] = {}

    async def cog_unload(self):
        await self.ace.close()
        await self.voice.close()

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    def _check_config(self, need_voice: bool) -> str | None:
        if not ACE_KEY:
            return "Set `ACE_MUSIC_API_KEY` first."

        if need_voice and not ELEVEN_KEY:
            return "Set `ELEVENLABS_API_KEY` first."

        if need_voice and not ELEVEN_VOICE_ID:
            return "Set `ELEVENLABS_VOICE_ID` to your cloned voice first."

        if need_voice and shutil.which(FFMPEG_BINARY) is None:
            return f"`{FFMPEG_BINARY}` is not installed on this host."

        return None

    async def _resolve_audio(self, ctx: commands.Context) -> discord.Attachment:
        for attachment in ctx.message.attachments:
            if _is_audio(attachment):
                return attachment

        reference = ctx.message.reference

        if reference is not None:
            replied = reference.resolved

            if isinstance(replied, discord.DeletedReferencedMessage):
                replied = None

            if replied is None and reference.message_id is not None:
                try:
                    replied = await ctx.channel.fetch_message(reference.message_id)
                except discord.HTTPException:
                    replied = None

            if replied is not None:
                for attachment in replied.attachments:
                    if _is_audio(attachment):
                        return attachment

        try:
            async for message in ctx.channel.history(limit=HISTORY_SEARCH_LIMIT):
                for attachment in message.attachments:
                    if _is_audio(attachment):
                        return attachment
        except discord.HTTPException:
            pass

        raise PipelineError(
            "No audio found. Attach a track, reply to one, or post one here first."
        )

    @staticmethod
    async def _download_attachment(attachment: discord.Attachment) -> bytes:
        if attachment.size > MAX_INPUT_BYTES:
            raise PipelineError("That file is too large.")

        try:
            return await attachment.read()
        except discord.HTTPException as exc:
            raise PipelineError(f"Could not download the audio: {exc}") from exc

    async def _set_status(self, status: discord.Message | None, text: str):
        if status is None:
            return

        try:
            await status.edit(embed=info_embed(text))
        except discord.HTTPException:
            pass

    def _heartbeat(self, status: discord.Message | None, label: str) -> asyncio.Task:
        """The cloud call blocks, so tick the status message while we wait."""

        async def _tick():
            started = time.monotonic()

            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                elapsed = time.monotonic() - started
                await self._set_status(status, f"🎹 {label}... {elapsed:.0f}s")

        return asyncio.create_task(_tick())

    async def _with_heartbeat(self, status, label: str, coro):
        beat = self._heartbeat(status, label)

        try:
            return await coro
        finally:
            beat.cancel()

    async def _generate_instrumental(
        self,
        prompt: str,
        flags: dict[str, str],
        status: discord.Message | None,
    ) -> tuple[bytes, str]:
        seed = None

        if "seed" in flags:
            try:
                seed = int(flags["seed"])
            except ValueError as exc:
                raise PipelineError(f"`--seed {flags['seed']}` is not a number.") from exc

        return await self._with_heartbeat(
            status,
            "Writing the instrumental",
            self.ace.generate(
                prompt=f"{prompt[:MAX_PROMPT_LENGTH]}, instrumental, no vocals",
                audio_config=_build_audio_config(flags),
                seed=seed,
            ),
        )

    async def _add_vocals(
        self,
        instrumental: bytes,
        prompt: str,
        flags: dict[str, str],
        status: discord.Message | None,
    ) -> bytes:
        """Cover the instrumental, isolate the vocal, convert it, mix it back."""
        strength = _float_flag(flags, "strength", 0.0, 1.0)
        vox_gain = _float_flag(flags, "vox", 0.2, 4.0)
        lyrics = flags.get("lyrics", "").strip()

        await self._set_status(status, "🎚️ Preparing the reference track...")
        reference = await _compress_for_upload(instrumental)

        await self._set_status(status, "🎤 Recording the vocal take...")
        covered, _ = await self._with_heartbeat(
            status,
            "Recording the vocal take",
            self.ace.generate(
                prompt=prompt[:MAX_PROMPT_LENGTH],
                lyrics=lyrics[:MAX_LYRICS_LENGTH],
                audio_config=_build_audio_config(flags),
                src_audio=reference,
                task_type="cover",
                cover_strength=(
                    strength if strength is not None else DEFAULT_COVER_STRENGTH
                ),
            ),
        )

        await self._set_status(status, "🎛️ Pulling the vocal off the backing...")
        vocal = await self.voice.isolate(covered)

        await self._set_status(status, "🗣️ Swapping in your voice...")
        converted = await self.voice.convert(vocal)

        await self._set_status(status, "🎚️ Mixing...")
        return await _mix(
            instrumental,
            converted,
            vox_gain if vox_gain is not None else DEFAULT_VOX_GAIN,
        )

    async def _deliver(
        self,
        ctx: commands.Context,
        status: discord.Message | None,
        audio: bytes,
        caption: str,
        stem: str,
    ):
        await ctx.send(
            content=caption[:1900],
            file=discord.File(io.BytesIO(audio), filename=f"{stem}.mp3"),
        )

        if status is not None:
            try:
                await status.delete()
            except discord.HTTPException:
                pass

    async def _run(self, ctx: commands.Context, coro, status):
        try:
            await coro
        except PipelineError as exc:
            log.warning("Pipeline failed for %s: %s", ctx.author.id, exc)

            if status is not None:
                try:
                    await status.edit(embed=error_embed(str(exc)[:1000]))
                    return
                except discord.HTTPException:
                    pass

            await ctx.send(embed=error_embed(str(exc)[:1000]))

        except discord.HTTPException as exc:
            log.warning("Could not upload result: %s", exc)
            await ctx.send(
                embed=error_embed(
                    "The track finished, but Discord rejected the upload. "
                    "Try a shorter `--len`."
                )
            )

        except Exception:
            log.exception("Unexpected music pipeline failure.")

            if status is not None:
                try:
                    await status.edit(embed=error_embed("That failed unexpectedly."))
                    return
                except discord.HTTPException:
                    pass

            await ctx.send(embed=error_embed("That failed unexpectedly."))

    # ----------------------------------------------------------------------
    # Commands
    # ----------------------------------------------------------------------

    @commands.group(
        name="gen",
        aliases=["music", "beat"],
        invoke_without_command=True,
        usage="gen <prompt> [--len 60] [--bpm 90] [--key \"F minor\"]",
    )
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def gen(self, ctx: commands.Context, *, raw: str = ""):
        """Generate an instrumental."""
        prefix = ctx.clean_prefix

        if not raw.strip():
            return await ctx.send(
                embed=info_embed(
                    "**Music generator**\n"
                    f"`{prefix}gen lo-fi hip hop, rainy night, dusty piano`\n"
                    f"`{prefix}gen song trap banger --lyrics \"[Verse] ...\"`\n"
                    f"`{prefix}gen apply <style>` — vocals onto an instrumental\n"
                    f"`{prefix}gen voice` — convert any audio to your voice\n"
                    f"`{prefix}gen models`\n\n"
                    "**Flags**\n"
                    "`--len 90` · `--bpm 140` · `--key \"E minor\"`\n"
                    "`--seed 42` · `--lang en` · `--lyrics \"...\"`\n"
                    "`--strength 0.55` · `--vox 1.6`\n\n"
                    f"Max length: **{MAX_DURATION:.0f}s**"
                )
            )

        problem = self._check_config(need_voice=False)

        if problem:
            return await ctx.send(embed=error_embed(problem))

        prompt, flags = _parse_flags(raw)

        if not prompt:
            return await ctx.send(embed=error_embed("Give it something to work with."))

        status = await ctx.send(embed=info_embed("🎹 Writing the instrumental..."))

        async def _work():
            async with self._lock_for(ctx.author.id):
                audio, notes = await self._generate_instrumental(prompt, flags, status)

            caption = "🎹 Instrumental"
            summary = notes.strip().splitlines()[0] if notes.strip() else ""

            if summary:
                caption += f" — {summary[:120]}"

            await self._deliver(ctx, status, audio, caption, "instrumental")

        await self._run(ctx, _work(), status)

    @gen.command(name="song", aliases=["vocal", "track"], usage="gen song <prompt>")
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def gen_song(self, ctx: commands.Context, *, raw: str):
        """Generate an instrumental, then sing over it in your voice."""
        problem = self._check_config(need_voice=True)

        if problem:
            return await ctx.send(embed=error_embed(problem))

        prompt, flags = _parse_flags(raw)

        if not prompt:
            return await ctx.send(embed=error_embed("Give it a style to work with."))

        try:
            duration = _float_flag(flags, "duration", MIN_DURATION, MAX_DURATION)
        except PipelineError as exc:
            return await ctx.send(embed=error_embed(str(exc)))

        if duration is not None and duration > STS_MAX_SECONDS:
            return await ctx.send(
                embed=error_embed(
                    f"Voice conversion caps out around {STS_MAX_SECONDS:.0f}s."
                )
            )

        status = await ctx.send(embed=info_embed("🎹 Writing the instrumental..."))

        async def _work():
            async with self._lock_for(ctx.author.id):
                instrumental, _ = await self._generate_instrumental(
                    prompt, flags, status
                )
                final = await self._add_vocals(instrumental, prompt, flags, status)

            await self._deliver(ctx, status, final, "🎤 Track with your vocals.", "song")

        await self._run(ctx, _work(), status)

    @gen.command(name="apply", aliases=["sing", "over"], usage="gen apply <style>")
    @commands.cooldown(1, 20, commands.BucketType.user)
    async def gen_apply(self, ctx: commands.Context, *, raw: str = ""):
        """Add vocals in your voice to an existing instrumental."""
        problem = self._check_config(need_voice=True)

        if problem:
            return await ctx.send(embed=error_embed(problem))

        prompt, flags = _parse_flags(raw)

        if not prompt and not flags.get("lyrics"):
            return await ctx.send(
                embed=error_embed(
                    "Describe the vocal style, or pass `--lyrics \"...\"`."
                )
            )

        status = await ctx.send(embed=info_embed("🎧 Finding the instrumental..."))

        async def _work():
            async with self._lock_for(ctx.author.id):
                attachment = await self._resolve_audio(ctx)
                instrumental = await self._download_attachment(attachment)
                final = await self._add_vocals(
                    instrumental,
                    prompt or "vocal performance",
                    flags,
                    status,
                )

            await self._deliver(
                ctx,
                status,
                final,
                f"🎤 Your vocals over `{attachment.filename}`.",
                "song",
            )

        await self._run(ctx, _work(), status)

    @gen.command(
        name="voice",
        aliases=["asme", "convert"],
        usage="gen voice [--isolate]",
    )
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def gen_voice(self, ctx: commands.Context, *, raw: str = ""):
        """Convert any audio into your cloned voice."""
        problem = self._check_config(need_voice=True)

        if problem:
            return await ctx.send(embed=error_embed(problem))

        _, flags = _parse_flags(raw)
        isolate_first = "isolate" in flags

        status = await ctx.send(embed=info_embed("🎧 Finding the audio..."))

        async def _work():
            async with self._lock_for(ctx.author.id):
                attachment = await self._resolve_audio(ctx)
                audio = await self._download_attachment(attachment)

                if isolate_first:
                    await self._set_status(status, "🎛️ Stripping the backing...")
                    audio = await self.voice.isolate(audio)

                await self._set_status(status, "🗣️ Swapping in your voice...")
                converted = await self.voice.convert(audio)

            await self._deliver(
                ctx,
                status,
                converted,
                f"🗣️ `{attachment.filename}` in your voice.",
                "converted",
            )

        await self._run(ctx, _work(), status)

    @gen.command(name="models", aliases=["stats"], usage="gen models")
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def gen_models(self, ctx: commands.Context):
        """List the models the ACE endpoint exposes."""
        if not ACE_KEY:
            return await ctx.send(embed=error_embed("Set `ACE_MUSIC_API_KEY` first."))

        try:
            models = await self.ace.list_models()
            selected = await self.ace.model_id()
        except PipelineError as exc:
            return await ctx.send(embed=error_embed(str(exc)))

        listing = "\n".join(
            f"• `{entry.get('id')}`" for entry in models[:15] if entry.get("id")
        )

        await ctx.send(
            embed=info_embed(
                "**ACE Music**\n"
                f"Endpoint: `{ACE_BASE}`\n"
                f"Using: `{selected}`\n"
                f"Voice: `{ELEVEN_VOICE_ID or 'not set'}`\n\n"
                f"{listing or 'No models reported.'}"
            )
        )


async def setup(bot):
    await bot.add_cog(MusicGen(bot))
