"""ElevenLabs TTS cog that sends generated audio as an MP3.

Commands:
    $tts <text>
    $tts rap <text>
    $tts sing <text>

These commands work in both server channels and DMs.

Required environment variables:
    ELEVENLABS_API_KEY
    ELEVENLABS_VOICE_ID

Optional environment variables:
    ELEVENLABS_MODEL_ID=eleven_multilingual_v2
    ELEVENLABS_PERFORMANCE_MODEL_ID=eleven_v3
    ELEVENLABS_OUTPUT_FORMAT=mp3_44100_128
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
from typing import Any, Literal

import aiohttp
import discord
from discord.ext import commands

from utils.helpers import error_embed, info_embed

log = logging.getLogger("cogs.tts")

API_URL = "https://api.elevenlabs.io/v1"
TIMEOUT = aiohttp.ClientTimeout(total=240)
MAX_TEXT_LENGTH = 4000

TTSMode = Literal["speak", "rap", "sing"]


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise RuntimeError(f"{name} contains an invalid control character.")
    return value


ELEVENLABS_API_KEY = _env("ELEVENLABS_API_KEY")
ELEVENLABS_VOICE_ID = _env("ELEVENLABS_VOICE_ID")
ELEVENLABS_MODEL_ID = _env(
    "ELEVENLABS_MODEL_ID",
    "eleven_multilingual_v2",
)
ELEVENLABS_PERFORMANCE_MODEL_ID = _env(
    "ELEVENLABS_PERFORMANCE_MODEL_ID",
    "eleven_v3",
)
ELEVENLABS_OUTPUT_FORMAT = _env(
    "ELEVENLABS_OUTPUT_FORMAT",
    "mp3_44100_128",
)
ELEVENLABS_AVAILABLE = bool(
    ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID
)


class ElevenLabsError(RuntimeError):
    pass


class ElevenLabsClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=TIMEOUT)
        return self.session

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()

    @staticmethod
    def _prepare_text(text: str, mode: TTSMode) -> str:
        if mode == "rap":
            return (
                "[rapping with a steady beat, sharp rhythm, confident delivery, "
                "clear punchlines, and natural breath control]\n"
                f"{text}"
            )

        if mode == "sing":
            return (
                "[singing melodically with expressive phrasing, sustained notes, "
                "clear pitch movement, and emotional delivery]\n"
                f"{text}"
            )

        return text

    @staticmethod
    def _voice_settings(mode: TTSMode) -> dict[str, Any]:
        if mode == "rap":
            return {
                "stability": 0.30,
                "similarity_boost": 0.85,
                "style": 0.75,
                "use_speaker_boost": True,
                "speed": 1.10,
            }

        if mode == "sing":
            return {
                "stability": 0.25,
                "similarity_boost": 0.82,
                "style": 0.90,
                "use_speaker_boost": True,
                "speed": 0.92,
            }

        return {
            "stability": 0.45,
            "similarity_boost": 0.85,
            "style": 0.20,
            "use_speaker_boost": True,
            "speed": 1.0,
        }

    async def list_voices(
        self,
        *,
        search: str = "",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List voices available to the configured ElevenLabs account."""
        if not self.api_key:
            raise ElevenLabsError("ELEVENLABS_API_KEY is not configured.")
        session = await self._get_session()
        params: dict[str, str] = {
            "page_size": str(max(1, min(int(limit), 100))),
            "include_total_count": "false",
            "sort": "name",
            "sort_direction": "asc",
        }
        if search.strip():
            params["search"] = search.strip()[:100]
        try:
            async with session.get(
                "https://api.elevenlabs.io/v2/voices",
                params=params,
                headers={"xi-api-key": self.api_key, "Accept": "application/json"},
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise ElevenLabsError(
                        f"ElevenLabs voices returned HTTP {response.status}: "
                        f"{str(body)[:700]}"
                    )
                voices = body.get("voices") if isinstance(body, dict) else None
                return [item for item in (voices or []) if isinstance(item, dict)]
        except asyncio.TimeoutError as exc:
            raise ElevenLabsError("ElevenLabs voice search timed out.") from exc
        except aiohttp.ClientError as exc:
            raise ElevenLabsError(f"Could not reach ElevenLabs: {exc}") from exc

    async def get_voice(self, voice_id: str) -> dict[str, Any]:
        if not self.api_key:
            raise ElevenLabsError("ELEVENLABS_API_KEY is not configured.")
        voice_id = str(voice_id or "").strip()
        if not voice_id:
            raise ElevenLabsError("Voice ID is empty.")
        session = await self._get_session()
        try:
            async with session.get(
                f"{API_URL}/voices/{voice_id}",
                headers={"xi-api-key": self.api_key, "Accept": "application/json"},
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400 or not isinstance(body, dict):
                    raise ElevenLabsError(
                        f"ElevenLabs voice lookup returned HTTP {response.status}."
                    )
                return body
        except asyncio.TimeoutError as exc:
            raise ElevenLabsError("ElevenLabs voice lookup timed out.") from exc
        except aiohttp.ClientError as exc:
            raise ElevenLabsError(f"Could not reach ElevenLabs: {exc}") from exc

    async def synthesize(
        self,
        text: str,
        voice_id: str,
        mode: TTSMode = "speak",
    ) -> bytes:
        if not self.api_key:
            raise ElevenLabsError(
                "ELEVENLABS_API_KEY is not configured."
            )

        if not voice_id:
            raise ElevenLabsError(
                "No ElevenLabs voice is configured. Set ELEVENLABS_VOICE_ID or use `voice set`."
            )

        model_id = (
            ELEVENLABS_PERFORMANCE_MODEL_ID
            if mode in {"rap", "sing"}
            else ELEVENLABS_MODEL_ID
        )

        url = (
            f"{API_URL}/text-to-speech/{voice_id}"
            f"?output_format={ELEVENLABS_OUTPUT_FORMAT}"
        )

        payload: dict[str, Any] = {
            "text": self._prepare_text(text, mode),
            "model_id": model_id,
            "voice_settings": self._voice_settings(mode),
        }

        headers = {
            "Accept": "audio/mpeg",
            "Content-Type": "application/json",
            "xi-api-key": self.api_key,
        }

        session = await self._get_session()

        try:
            async with session.post(
                url,
                json=payload,
                headers=headers,
            ) as response:
                body = await response.read()

                if response.status >= 400:
                    detail = body.decode(
                        "utf-8",
                        errors="replace",
                    )[:1000]
                    raise ElevenLabsError(
                        f"ElevenLabs returned HTTP {response.status}: "
                        f"{detail or 'request failed'}"
                    )

                if not body:
                    raise ElevenLabsError(
                        "ElevenLabs returned empty audio."
                    )

                return body

        except asyncio.TimeoutError as exc:
            raise ElevenLabsError(
                "ElevenLabs timed out."
            ) from exc

        except aiohttp.ClientError as exc:
            raise ElevenLabsError(
                f"Could not reach ElevenLabs: {exc}"
            ) from exc


class TTS(commands.Cog):
    """Generate speech, rap, or singing and return it as an MP3."""

    def __init__(self, bot):
        self.bot = bot
        self.client = ElevenLabsClient(ELEVENLABS_API_KEY)
        self._locks: dict[int, asyncio.Lock] = {}

    async def cog_unload(self):
        await self.client.close()

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    async def voice_for_user(self, user_id: int) -> tuple[str, str]:
        """Return (voice_id, display_name) for a user's saved/default voice."""
        if getattr(self.bot, "db", None):
            row = await self.bot.db.get_elevenlabs_voice(int(user_id))
            if row and row.get("voice_id"):
                return str(row["voice_id"]), str(row.get("voice_name") or row["voice_id"])
        return ELEVENLABS_VOICE_ID, "Railway default"

    async def resolve_voice(
        self, user_id: int, query: str = "",
    ) -> tuple[str, str]:
        query = str(query or "").strip()
        if not query:
            return await self.voice_for_user(user_id)

        voices = await self.client.list_voices(search=query, limit=100)
        folded = query.casefold()
        exact = [
            v for v in voices
            if str(v.get("voice_id") or "").casefold() == folded
            or str(v.get("name") or "").casefold() == folded
        ]
        matches = exact or [
            v for v in voices
            if folded in str(v.get("name") or "").casefold()
        ]
        if len(matches) == 1:
            voice = matches[0]
            return str(voice.get("voice_id") or ""), str(voice.get("name") or "unnamed")
        if not matches:
            # A copied voice ID need not appear in the first search page. Verify
            # it directly through ElevenLabs' get-voice endpoint.
            if " " not in query and 10 <= len(query) <= 128:
                try:
                    raw = await self.client.get_voice(query)
                    if raw.get("voice_id"):
                        return str(raw["voice_id"]), str(raw.get("name") or query)
                except ElevenLabsError:
                    pass
            raise ElevenLabsError(f"No ElevenLabs voice matched {query!r}.")
        names = ", ".join(str(v.get("name") or v.get("voice_id")) for v in matches[:12])
        raise ElevenLabsError(f"Voice {query!r} is ambiguous. Matches: {names}")

    async def save_voice(self, user_id: int, query: str) -> tuple[str, str]:
        voice_id, name = await self.resolve_voice(user_id, query)
        if not voice_id:
            raise ElevenLabsError("Resolved voice has no voice ID.")
        if getattr(self.bot, "db", None):
            await self.bot.db.set_elevenlabs_voice(
                user_id=int(user_id), voice_id=voice_id, voice_name=name
            )
        return voice_id, name

    async def synthesize_bytes(
        self,
        *,
        user_id: int,
        text: str,
        mode: TTSMode = "speak",
        voice_id: str | None = None,
    ) -> bytes:
        """Return generated audio without sending it to Discord.

        This is the shared primitive used by live voice mode and Voice Studio.
        It deliberately keeps the existing per-user generation lock and default
        Railway voice configuration.
        """
        text = str(text or "").strip()
        if not text:
            raise ElevenLabsError("Text cannot be empty.")
        if len(text) > MAX_TEXT_LENGTH:
            raise ElevenLabsError(
                f"Message too long. Maximum: {MAX_TEXT_LENGTH} characters."
            )
        if voice_id:
            selected_voice = str(voice_id).strip()
        else:
            selected_voice, _ = await self.voice_for_user(int(user_id))
            selected_voice = str(selected_voice or "").strip()
        lock = self._lock_for(int(user_id))
        async with lock:
            return await self.client.synthesize(
                text=text,
                voice_id=selected_voice,
                mode=mode,
            )

    async def _send_audio(
        self,
        ctx: commands.Context,
        text: str,
        mode: TTSMode,
    ):
        if not ELEVENLABS_API_KEY:
            return await ctx.send(
                embed=error_embed("Set `ELEVENLABS_API_KEY` first.")
            )

        text = text.strip()

        if not text:
            return await ctx.send(
                embed=error_embed("Text cannot be empty.")
            )

        if len(text) > MAX_TEXT_LENGTH:
            return await ctx.send(
                embed=error_embed(
                    f"Message too long. Maximum: "
                    f"{MAX_TEXT_LENGTH} characters."
                )
            )

        mode_labels = {
            "speak": "speech",
            "rap": "rap",
            "sing": "singing",
        }
        mode_emojis = {
            "speak": "🗣️",
            "rap": "🎤",
            "sing": "🎶",
        }

        try:
            status = await ctx.send(
                embed=info_embed(
                    f"{mode_emojis[mode]} Generating "
                    f"{mode_labels[mode]}..."
                )
            )
        except discord.HTTPException:
            status = None

        lock = self._lock_for(ctx.author.id)

        try:
            audio = await self.synthesize_bytes(
                user_id=ctx.author.id,
                text=text,
                mode=mode,
            )

            filename = {
                "speak": "tweakbot-tts.mp3",
                "rap": "tweakbot-rap.mp3",
                "sing": "tweakbot-sing.mp3",
            }[mode]

            audio_file = discord.File(
                io.BytesIO(audio),
                filename=filename,
            )

            # In DMs, ctx.send sends directly to the current DM.
            # In a server, try to DM the caller first, then fall back
            # to the server channel if their DMs are closed.
            if ctx.guild is None:
                await ctx.send(
                    content=f"{mode_emojis[mode]} Your audio is ready.",
                    file=audio_file,
                )
            else:
                try:
                    await ctx.author.send(
                        content=f"{mode_emojis[mode]} Your audio is ready.",
                        file=audio_file,
                    )
                    await ctx.send(
                        embed=info_embed(
                            "Generated audio sent to your DMs."
                        )
                    )
                except discord.Forbidden:
                    audio_file = discord.File(
                        io.BytesIO(audio),
                        filename=filename,
                    )
                    await ctx.send(
                        content=(
                            f"{ctx.author.mention} "
                            f"{mode_emojis[mode]} your audio is ready."
                        ),
                        file=audio_file,
                    )

            if status is not None:
                try:
                    await status.delete()
                except discord.HTTPException:
                    pass

        except ElevenLabsError as exc:
            log.warning(
                "ElevenLabs %s generation failed for user %s: %s",
                mode,
                ctx.author.id,
                exc,
            )

            if status is not None:
                try:
                    await status.edit(
                        embed=error_embed(str(exc))
                    )
                    return
                except discord.HTTPException:
                    pass

            await ctx.send(embed=error_embed(str(exc)))

        except discord.HTTPException as exc:
            log.exception(
                "Could not send generated TTS audio: %s",
                exc,
            )
            await ctx.send(
                embed=error_embed(
                    "The audio was generated, but Discord rejected "
                    "the upload. The file may exceed this server's "
                    "upload limit."
                )
            )

        except Exception:
            log.exception(
                "Unexpected ElevenLabs generation failure."
            )

            if status is not None:
                try:
                    await status.edit(
                        embed=error_embed(
                            "Audio generation failed unexpectedly."
                        )
                    )
                    return
                except discord.HTTPException:
                    pass

            await ctx.send(
                embed=error_embed(
                    "Audio generation failed unexpectedly."
                )
            )

    @commands.group(
        name="tts",
        aliases=["speak"],
        invoke_without_command=True,
        usage="tts <text> | tts rap <text> | tts sing <text>",
    )
    @commands.cooldown(
        1,
        2,
        commands.BucketType.user,
    )
    async def tts(
        self,
        ctx: commands.Context,
        *,
        text: str = "",
    ):
        """Generate normal speech and send the MP3 to the caller."""
        if not text:
            return await ctx.send(
                embed=info_embed(
                    "**ElevenLabs TTS**\n"
                    "`$tts <text>` — normal speech\n"
                    "`$tts rap <text>` — rap delivery\n"
                    "`$tts sing <text>` — singing delivery\n\n"
                    f"Maximum: **{MAX_TEXT_LENGTH} characters**\n"
                    "Works in DMs and server channels."
                )
            )

        await self._send_audio(ctx, text, "speak")

    @tts.command(
        name="rap",
        usage="tts rap <text>",
    )
    @commands.cooldown(
        1,
        2,
        commands.BucketType.user,
    )
    async def tts_rap(
        self,
        ctx: commands.Context,
        *,
        text: str,
    ):
        """Generate rap delivery and send the MP3 to the caller."""
        await self._send_audio(ctx, text, "rap")

    @tts.command(
        name="sing",
        usage="tts sing <text>",
    )
    @commands.cooldown(
        1,
        2,
        commands.BucketType.user,
    )
    async def tts_sing(
        self,
        ctx: commands.Context,
        *,
        text: str,
    ):
        """Generate singing delivery and send the MP3 to the caller."""
        await self._send_audio(ctx, text, "sing")

async def setup(bot):
    await bot.add_cog(TTS(bot))
