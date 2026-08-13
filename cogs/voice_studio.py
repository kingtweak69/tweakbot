"""TweakBot Voice Studio: selectable ElevenLabs voices + instrumental mixing."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

log = logging.getLogger("cogs.voice_studio")

MAX_INSTRUMENTAL_BYTES = max(
    1_000_000, int(os.getenv("VOICE_STUDIO_MAX_INSTRUMENTAL_BYTES", "50000000"))
)
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".ogg", ".flac", ".aac", ".opus", ".webm"}


class VoiceStudio(commands.Cog):
    SOURCE = "voice_studio"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}

    async def cog_load(self) -> None:
        r = self.bot.capabilities
        r.register(
            name="elevenlabs_list_voices",
            description="List/search ElevenLabs voices available to the requesting user's TweakBot account configuration.",
            parameters={
                "type": "object",
                "properties": {"search": {"type": "string"}},
            },
            handler=self._tool_list_voices,
            category="voice",
            source=self.SOURCE,
        )
        r.register(
            name="elevenlabs_set_voice",
            description="Set the requester's persistent ElevenLabs voice by exact name or voice ID.",
            parameters={
                "type": "object",
                "properties": {"voice": {"type": "string"}},
                "required": ["voice"],
            },
            handler=self._tool_set_voice,
            category="voice",
            source=self.SOURCE,
        )
        r.register(
            name="voice_studio_mix",
            description=(
                "Generate speech/rap/singing with ElevenLabs and mix it over the audio "
                "attachment or replied-to instrumental. Supports selected voice, vocal "
                "start delay, levels, ducking, beat looping, normalization, fades, MP3/WAV."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "voice": {"type": "string", "description": "Optional ElevenLabs voice name or ID."},
                    "mode": {"type": "string", "enum": ["speak", "rap", "sing"]},
                    "vocal_start": {"type": "number", "minimum": 0, "maximum": 600},
                    "instrumental_volume": {"type": "number", "minimum": 0, "maximum": 2},
                    "vocal_volume": {"type": "number", "minimum": 0, "maximum": 2},
                    "ducking": {"type": "boolean"},
                    "loop_instrumental": {"type": "boolean"},
                    "normalize": {"type": "boolean"},
                    "fade_out": {"type": "number", "minimum": 0, "maximum": 10},
                    "output_format": {"type": "string", "enum": ["mp3", "wav"]},
                },
                "required": ["text"],
            },
            handler=self._tool_mix,
            category="voice",
            source=self.SOURCE,
        )

    async def cog_unload(self) -> None:
        self.bot.capabilities.unregister_source(self.SOURCE)

    def _lock(self, user_id: int) -> asyncio.Lock:
        return self._locks.setdefault(int(user_id), asyncio.Lock())

    def _tts(self):
        tts = self.bot.get_cog("TTS")
        if tts is None:
            raise RuntimeError("TTS cog is not loaded.")
        return tts

    async def _audio_attachment(self, ctx: commands.Context) -> discord.Attachment | None:
        attachments = list(ctx.message.attachments)
        if not attachments and ctx.message.reference and ctx.message.reference.message_id:
            try:
                ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                attachments = list(ref.attachments)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
        for attachment in attachments:
            suffix = Path(attachment.filename or "").suffix.lower()
            ctype = (attachment.content_type or "").lower()
            if ctype.startswith("audio/") or suffix in AUDIO_SUFFIXES:
                return attachment
        return None

    async def _list_voices(self, search: str = "") -> str:
        voices = await self._tts().client.list_voices(search=search, limit=50)
        if not voices:
            return f"No ElevenLabs voices matched {search!r}." if search else "No ElevenLabs voices were returned."
        lines = []
        for item in voices[:50]:
            name = str(item.get("name") or "unnamed")
            voice_id = str(item.get("voice_id") or "")
            category = str(item.get("category") or "")
            labels = item.get("labels") or {}
            extra = ", ".join(
                str(labels.get(key)) for key in ("accent", "gender", "use_case") if labels.get(key)
            )
            suffix = f" — {category}" if category else ""
            if extra:
                suffix += f" ({extra})"
            lines.append(f"{name} — `{voice_id}`{suffix}")
        return "ElevenLabs voices:\n" + "\n".join(lines)

    async def _set_voice(self, ctx: commands.Context, query: str) -> str:
        voice_id, name = await self._tts().save_voice(ctx.author.id, query)
        return f"Default ElevenLabs voice set to **{name}** (`{voice_id}`)."

    async def _current_voice(self, ctx: commands.Context) -> str:
        voice_id, name = await self._tts().voice_for_user(ctx.author.id)
        return f"Current ElevenLabs voice: **{name}** (`{voice_id or 'not configured'}`)."

    @staticmethod
    async def _probe_duration(path: Path) -> float:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=20)
        if proc.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {err.decode(errors='replace')[:500]}")
        return max(0.1, float(out.decode().strip()))

    @staticmethod
    async def _run_ffmpeg(args: list[str]) -> None:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=240)
        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg failed: {err.decode(errors='replace')[-1200:]}")

    async def _mix(self, ctx: commands.Context, args: dict[str, Any]) -> tuple[Path, tempfile.TemporaryDirectory, str]:
        attachment = await self._audio_attachment(ctx)
        if attachment is None:
            raise RuntimeError("Attach an instrumental/audio file, or reply to a message containing one.")
        if attachment.size and attachment.size > MAX_INSTRUMENTAL_BYTES:
            raise RuntimeError(f"Instrumental exceeds {MAX_INSTRUMENTAL_BYTES // 1_000_000} MB.")
        if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
            raise RuntimeError("ffmpeg/ffprobe are not installed on this deployment.")

        text = str(args.get("text") or "").strip()
        if not text:
            raise RuntimeError("Text cannot be empty.")
        mode = str(args.get("mode") or "speak").lower()
        if mode not in {"speak", "rap", "sing"}:
            mode = "speak"
        voice_query = str(args.get("voice") or "").strip()
        voice_id = None
        voice_name = "saved/default"
        if voice_query:
            voice_id, voice_name = await self._tts().resolve_voice(ctx.author.id, voice_query)

        vocal_start = max(0.0, min(float(args.get("vocal_start", 0) or 0), 600.0))
        inst_vol = max(0.0, min(float(args.get("instrumental_volume", 0.45) or 0.45), 2.0))
        vocal_vol = max(0.0, min(float(args.get("vocal_volume", 1.0) or 1.0), 2.0))
        ducking = bool(args.get("ducking", True))
        loop_inst = bool(args.get("loop_instrumental", True))
        normalize = bool(args.get("normalize", True))
        fade_out = max(0.0, min(float(args.get("fade_out", 1.0) or 0), 10.0))
        output_format = str(args.get("output_format") or "mp3").lower()
        if output_format not in {"mp3", "wav"}:
            output_format = "mp3"

        temp = tempfile.TemporaryDirectory(prefix="tweakbot-voice-studio-")
        root = Path(temp.name)
        inst_suffix = Path(attachment.filename or "beat.mp3").suffix.lower() or ".mp3"
        inst_path = root / f"instrumental{inst_suffix}"
        vocal_path = root / "vocals.mp3"
        out_path = root / f"tweakbot-voice-studio.{output_format}"
        inst_path.write_bytes(await attachment.read())

        vocals = await self._tts().synthesize_bytes(
            user_id=ctx.author.id, text=text, mode=mode, voice_id=voice_id
        )
        vocal_path.write_bytes(vocals)
        vocal_duration = await self._probe_duration(vocal_path)
        target_duration = max(1.0, vocal_start + vocal_duration + max(0.5, fade_out))
        delay_ms = int(vocal_start * 1000)

        ffargs: list[str] = []
        if loop_inst:
            ffargs += ["-stream_loop", "-1"]
        ffargs += ["-i", str(inst_path), "-i", str(vocal_path)]

        vocal_chain = f"adelay={delay_ms}|{delay_ms},volume={vocal_vol}"
        if ducking:
            vocal_chain += ",asplit=2[vduck][vmix]"
            graph = (
                f"[0:a]volume={inst_vol}[inst];"
                f"[1:a]{vocal_chain};"
                "[inst][vduck]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[ducked];"
                "[ducked][vmix]amix=inputs=2:duration=longest:dropout_transition=2:normalize=0[mix]"
            )
        else:
            graph = (
                f"[0:a]volume={inst_vol}[inst];"
                f"[1:a]{vocal_chain}[voc];"
                "[inst][voc]amix=inputs=2:duration=longest:dropout_transition=2:normalize=0[mix]"
            )
        post = ""
        if normalize:
            post += ",loudnorm=I=-16:TP=-1.5:LRA=11"
        post += ",afade=t=in:st=0:d=0.15"
        if fade_out > 0:
            fade_start = max(0.0, target_duration - fade_out)
            post += f",afade=t=out:st={fade_start:.3f}:d={fade_out:.3f}"
        graph += post + "[out]"

        ffargs += ["-filter_complex", graph, "-map", "[out]", "-t", f"{target_duration:.3f}"]
        if output_format == "mp3":
            ffargs += ["-c:a", "libmp3lame", "-b:a", "192k"]
        else:
            ffargs += ["-c:a", "pcm_s16le"]
        ffargs += [str(out_path)]
        await self._run_ffmpeg(ffargs)
        if not out_path.exists() or out_path.stat().st_size == 0:
            temp.cleanup()
            raise RuntimeError("Voice Studio produced no output file.")
        meta = (
            f"mode={mode}; voice={voice_name}; start={vocal_start:.2f}s; "
            f"instrumental_volume={inst_vol}; vocal_volume={vocal_vol}; "
            f"ducking={ducking}; loop={loop_inst}; normalize={normalize}; format={output_format}"
        )
        return out_path, temp, meta

    async def _tool_list_voices(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        try:
            return await self._list_voices(str(args.get("search") or ""))
        except Exception as exc:
            return f"Voice lookup failed: {type(exc).__name__}: {exc}"

    async def _tool_set_voice(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        try:
            return await self._set_voice(ctx, str(args.get("voice") or ""))
        except Exception as exc:
            return f"Voice selection failed: {type(exc).__name__}: {exc}"

    async def _tool_mix(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        async with self._lock(ctx.author.id):
            temp = None
            try:
                out_path, temp, meta = await self._mix(ctx, args)
                await ctx.send(
                    content=f"🎚️ Voice Studio complete. `{meta}`",
                    file=discord.File(str(out_path), filename=out_path.name),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return f"Voice Studio output generated and attached in Discord. {meta}"
            except Exception as exc:
                log.exception("Voice Studio failed")
                return f"Voice Studio failed: {type(exc).__name__}: {exc}"
            finally:
                if temp is not None:
                    temp.cleanup()

    @commands.hybrid_group(name="voice", invoke_without_command=True)
    async def voice(self, ctx: commands.Context) -> None:
        await ctx.send(await self._current_voice(ctx))

    @voice.command(name="list")
    async def voice_list(self, ctx: commands.Context, *, search: str = "") -> None:
        await ctx.send((await self._list_voices(search))[:1900])

    @voice.command(name="set")
    async def voice_set(self, ctx: commands.Context, *, voice: str) -> None:
        await ctx.send(await self._set_voice(ctx, voice))

    @voice.command(name="current")
    async def voice_current(self, ctx: commands.Context) -> None:
        await ctx.send(await self._current_voice(ctx))

    @commands.hybrid_command(
        name="voicestudio", aliases=["studiovoice"], usage="voicestudio <text> + attach/reply to instrumental"
    )
    async def voicestudio(self, ctx: commands.Context, *, text: str) -> None:
        result = await self._tool_mix(ctx, {"text": text})
        if result.startswith("Voice Studio failed"):
            await ctx.send(result)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceStudio(bot))
