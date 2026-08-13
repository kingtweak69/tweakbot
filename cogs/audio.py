"""Audio editing and effects (ffmpeg powered).

Works on an attachment in your message, an attachment in the message you
replied to, or the most recent audio file posted in the channel/DM.

Editing:
    $audio speed <0.25-4.0>      tempo change, pitch preserved
    $audio pitch <-12 to 12>     pitch shift, duration preserved
    $audio volume <-30 to 30>    gain in dB
    $audio bass <-20 to 20>      low end gain in dB
    $audio trim <start> <end>    mm:ss or seconds
    $audio reverse
    $audio info

Effects (just name it — amount is optional):
    $audio distort 12
    $audio reverb
    $audio crush 5
    $audio list                  show every effect

Stacking and presets:
    $audio chain distort:8 reverb:0.6 crush:5
    $audio remix phonk
    $audio remix                 random preset

Optional environment variables:
    FFMPEG_BINARY=ffmpeg
    FFPROBE_BINARY=ffprobe
    AUDIO_MAX_INPUT_MB=25
    AUDIO_BITRATE=192k
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import random
import shutil

import discord
from discord.ext import commands

from utils.helpers import error_embed, info_embed

log = logging.getLogger("cogs.audio")

FFMPEG_BINARY = os.getenv("FFMPEG_BINARY", "ffmpeg").strip() or "ffmpeg"
FFPROBE_BINARY = os.getenv("FFPROBE_BINARY", "ffprobe").strip() or "ffprobe"
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "192k").strip() or "192k"

try:
    MAX_INPUT_MB = float(os.getenv("AUDIO_MAX_INPUT_MB", "25"))
except ValueError:
    MAX_INPUT_MB = 25.0

MAX_INPUT_BYTES = int(MAX_INPUT_MB * 1024 * 1024)
FFMPEG_TIMEOUT = 180
HISTORY_SEARCH_LIMIT = 25
SAMPLE_RATE = 44100
MAX_CHAIN_LENGTH = 6

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
    ".mov",
    ".mkv",
)


class AudioError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# Filter helpers
# --------------------------------------------------------------------------


def _atempo_chain(factor: float) -> str:
    """ffmpeg's atempo only accepts 0.5-2.0, so chain multiple filters."""
    filters: list[str] = []
    remaining = factor

    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0

    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining *= 2.0

    filters.append(f"atempo={remaining:.6f}")

    return ",".join(filters)


def _resample_pitch(ratio: float, keep_duration: bool) -> str:
    """Pitch via sample rate. keep_duration adds a compensating atempo."""
    chain = (
        f"aresample={SAMPLE_RATE},"
        f"asetrate={int(SAMPLE_RATE * ratio)},"
        f"aresample={SAMPLE_RATE}"
    )

    if keep_duration:
        chain += f",{_atempo_chain(1.0 / ratio)}"

    return chain


# --------------------------------------------------------------------------
# Effect registry: name -> (builder, default, low, high, blurb)
# --------------------------------------------------------------------------

EFFECTS: dict[str, tuple] = {
    "distort": (
        lambda a: (
            f"volume={a:g}dB,asoftclip=type=tanh:param=0.7,"
            f"volume={-a * 0.55:g}dB,alimiter=limit=0.95"
        ),
        12.0,
        1.0,
        30.0,
        "drive in dB",
    ),
    "crush": (
        lambda a: f"acrusher=level_in=1:level_out=1:bits={a:g}:mode=log:aa=1",
        6.0,
        2.0,
        16.0,
        "bit depth",
    ),
    "reverb": (
        lambda a: (
            f"aecho=0.8:0.88:60|120|200:"
            f"{0.45 * a:.3f}|{0.32 * a:.3f}|{0.22 * a:.3f}"
        ),
        0.6,
        0.1,
        1.0,
        "wetness",
    ),
    "echo": (
        lambda a: f"aecho=0.8:0.85:{int(a)}|{int(a * 2)}:0.5|0.3",
        300.0,
        40.0,
        1500.0,
        "delay in ms",
    ),
    "phaser": (
        lambda a: (
            f"aphaser=in_gain=0.6:out_gain=0.8:delay=3:decay=0.5:speed={a:g}"
        ),
        0.6,
        0.1,
        2.0,
        "sweep speed",
    ),
    "flanger": (
        lambda a: f"flanger=delay=5:depth=2:regen=0:width=71:speed={a:g}",
        0.5,
        0.1,
        10.0,
        "sweep speed",
    ),
    "chorus": (
        lambda a: (
            "chorus=0.5:0.9:50|60|40:0.4|0.32|0.3:0.25|0.4|0.3:2|2.3|1.3"
        ),
        0.0,
        0.0,
        0.0,
        "no knob",
    ),
    "tremolo": (
        lambda a: f"tremolo=f={a:g}:d=0.7",
        5.0,
        0.1,
        20.0,
        "rate in Hz",
    ),
    "vibrato": (
        lambda a: f"vibrato=f={a:g}:d=0.5",
        5.0,
        0.1,
        20.0,
        "rate in Hz",
    ),
    "wide": (
        lambda a: f"aformat=channel_layouts=stereo,extrastereo=m={a:g}",
        2.5,
        0.0,
        8.0,
        "stereo width",
    ),
    "rotate": (
        lambda a: f"aformat=channel_layouts=stereo,apulsator=hz={a:g}",
        0.125,
        0.01,
        2.0,
        "rotation Hz",
    ),
    "radio": (
        lambda a: (
            "highpass=f=700,lowpass=f=3200,acrusher=bits=10:mode=log:aa=1,"
            "volume=4dB,alimiter=limit=0.95"
        ),
        0.0,
        0.0,
        0.0,
        "no knob",
    ),
    "phone": (
        lambda a: "highpass=f=900,lowpass=f=2800,volume=3dB",
        0.0,
        0.0,
        0.0,
        "no knob",
    ),
    "underwater": (
        lambda a: f"lowpass=f={int(a)},aecho=0.8:0.9:120|240:0.4|0.25",
        400.0,
        100.0,
        2000.0,
        "cutoff Hz",
    ),
    "muffle": (
        lambda a: f"lowpass=f={int(a)}",
        900.0,
        200.0,
        6000.0,
        "cutoff Hz",
    ),
    "treble": (
        lambda a: f"treble=g={a:g}",
        8.0,
        -20.0,
        20.0,
        "gain in dB",
    ),
    "robot": (
        lambda a: (
            "afftfilt=real='hypot(re,im)*sin(0)':"
            "imag='hypot(re,im)*cos(0)':win_size=512:overlap=0.75"
        ),
        0.0,
        0.0,
        0.0,
        "no knob",
    ),
    "whisper": (
        lambda a: (
            "afftfilt=real='hypot(re,im)*cos((random(0)*2-1)*2*3.14)':"
            "imag='hypot(re,im)*sin((random(1)*2-1)*2*3.14)':"
            "win_size=128:overlap=0.8"
        ),
        0.0,
        0.0,
        0.0,
        "no knob",
    ),
    "earrape": (
        lambda a: f"volume={a:g}dB,alimiter=limit=1:level=disabled",
        22.0,
        6.0,
        30.0,
        "gain in dB",
    ),
    "normalize": (
        lambda a: "loudnorm=I=-14:TP=-1.5:LRA=11",
        0.0,
        0.0,
        0.0,
        "no knob",
    ),
    "denoise": (
        lambda a: f"afftdn=nr={a:g}:nf=-25",
        12.0,
        1.0,
        97.0,
        "reduction dB",
    ),
    "karaoke": (
        lambda a: (
            "aformat=channel_layouts=stereo,"
            "pan=stereo|c0=0.5*c0-0.5*c1|c1=0.5*c1-0.5*c0"
        ),
        0.0,
        0.0,
        0.0,
        "center removal",
    ),
    "mono": (
        lambda a: "aformat=channel_layouts=mono",
        0.0,
        0.0,
        0.0,
        "no knob",
    ),
}

EFFECT_ALIASES = {
    "dist": "distort",
    "overdrive": "distort",
    "bitcrush": "crush",
    "verb": "reverb",
    "delay": "echo",
    "stereo": "wide",
    "8d": "rotate",
    "telephone": "phone",
    "lowpass": "muffle",
    "highs": "treble",
    "loud": "earrape",
    "norm": "normalize",
    "instrumental": "karaoke",
}

# --------------------------------------------------------------------------
# Remix presets: name -> full filter chain
# --------------------------------------------------------------------------

PRESETS: dict[str, str] = {
    "nightcore": _resample_pitch(1.25, keep_duration=False),
    "slowed": (
        _resample_pitch(0.85, keep_duration=False)
        + ",aecho=0.8:0.85:180|320:0.3|0.2"
    ),
    "chopped": (
        _resample_pitch(0.75, keep_duration=False)
        + ",aecho=0.8:0.88:220|440:0.35|0.22,bass=g=5:f=100:w=0.6"
    ),
    "phonk": (
        _resample_pitch(0.88, keep_duration=False)
        + ",bass=g=9:f=90:w=0.6,aecho=0.8:0.85:200|380:0.28|0.18,"
        "alimiter=limit=0.95"
    ),
    "vaporwave": (
        _resample_pitch(0.80, keep_duration=False)
        + ",lowpass=f=6000,aecho=0.8:0.9:250|500:0.4|0.3,"
        "aformat=channel_layouts=stereo,extrastereo=m=2.0"
    ),
    "lofi": (
        f"aresample={SAMPLE_RATE},highpass=f=180,lowpass=f=3400,"
        "acrusher=bits=12:mode=log:aa=1,bass=g=4:f=110:w=0.6,volume=1dB"
    ),
    "club": (
        "bass=g=8:f=80:w=0.7,aformat=channel_layouts=stereo,extrastereo=m=1.8,"
        "alimiter=limit=0.95,volume=2dB"
    ),
    "broken": (
        "acrusher=bits=4:mode=log:aa=1,volume=10dB,asoftclip=type=tanh:param=0.7,"
        "tremolo=f=9:d=0.6,alimiter=limit=0.95"
    ),
    "cave": (
        "aecho=0.85:0.9:180|400|760:0.5|0.35|0.22,lowpass=f=7000"
    ),
    "spin": (
        "aformat=channel_layouts=stereo,apulsator=hz=0.15,extrastereo=m=2.2,"
        "aecho=0.8:0.88:120|240:0.3|0.2"
    ),
    "tape": (
        f"aresample={SAMPLE_RATE},vibrato=f=3:d=0.15,lowpass=f=8000,"
        "highpass=f=90,acrusher=bits=13:mode=log:aa=1,volume=1dB"
    ),
}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _resolve_effect(name: str) -> str | None:
    name = name.lower().strip()
    name = EFFECT_ALIASES.get(name, name)
    return name if name in EFFECTS else None


def _build_effect(name: str, amount: float | None) -> str:
    builder, default, low, high, blurb = EFFECTS[name]

    if amount is None:
        amount = default

    if high > low and not low <= amount <= high:
        raise AudioError(f"`{name}` takes {blurb} between {low:g} and {high:g}.")

    return builder(amount)


def _parse_chain(tokens: list[str]) -> tuple[str, list[str]]:
    """Turn ['distort:8', 'reverb'] into one filter chain."""
    if not tokens:
        raise AudioError("Name at least one effect.")

    if len(tokens) > MAX_CHAIN_LENGTH:
        raise AudioError(f"Maximum {MAX_CHAIN_LENGTH} effects in one chain.")

    filters: list[str] = []
    labels: list[str] = []

    for token in tokens:
        if ":" in token:
            raw_name, _, raw_amount = token.partition(":")
        elif "=" in token:
            raw_name, _, raw_amount = token.partition("=")
        else:
            raw_name, raw_amount = token, ""

        name = _resolve_effect(raw_name)

        if name is None:
            raise AudioError(f"`{raw_name}` is not an effect. Try `audio list`.")

        amount: float | None = None

        if raw_amount:
            try:
                amount = float(raw_amount)
            except ValueError as exc:
                raise AudioError(
                    f"`{raw_amount}` is not a number for `{name}`."
                ) from exc

        filters.append(_build_effect(name, amount))
        labels.append(name if amount is None else f"{name} {amount:g}")

    return ",".join(filters), labels


def _parse_timestamp(value: str) -> float:
    value = value.strip()
    parts = value.split(":")

    if len(parts) > 3:
        raise AudioError(f"Could not read the timestamp `{value}`.")

    total = 0.0

    try:
        for part in parts:
            total = total * 60 + float(part)
    except ValueError as exc:
        raise AudioError(f"Could not read the timestamp `{value}`.") from exc

    if total < 0:
        raise AudioError("Timestamps cannot be negative.")

    return total


def _is_audio(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()

    if content_type.startswith(("audio/", "video/")):
        return True

    return attachment.filename.lower().endswith(AUDIO_EXTENSIONS)


# --------------------------------------------------------------------------
# Cog
# --------------------------------------------------------------------------


class Audio(commands.Cog):
    """Edit audio files with ffmpeg and send the result back."""

    def __init__(self, bot):
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    @staticmethod
    def _ffmpeg_available() -> bool:
        return shutil.which(FFMPEG_BINARY) is not None

    async def _resolve_attachment(
        self,
        ctx: commands.Context,
    ) -> discord.Attachment:
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

        raise AudioError(
            "No audio found. Attach a file, reply to one, or post one "
            "in this channel first."
        )

    @staticmethod
    async def _download(attachment: discord.Attachment) -> bytes:
        if attachment.size > MAX_INPUT_BYTES:
            raise AudioError(
                f"That file is too large. Maximum: {MAX_INPUT_MB:.0f} MB."
            )

        try:
            return await attachment.read()
        except discord.HTTPException as exc:
            raise AudioError(f"Could not download the audio: {exc}") from exc

    @staticmethod
    async def _run_ffmpeg(data: bytes, filter_chain: str) -> bytes:
        args = [
            FFMPEG_BINARY,
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-vn",
            "-filter:a",
            filter_chain,
            "-f",
            "mp3",
            "-b:a",
            AUDIO_BITRATE,
            "pipe:1",
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise AudioError(
                f"`{FFMPEG_BINARY}` is not installed on this host."
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(data),
                timeout=FFMPEG_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise AudioError("ffmpeg took too long and was killed.") from exc

        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[:500]
            raise AudioError(f"ffmpeg failed: {detail or 'unknown error'}")

        if not stdout:
            raise AudioError("ffmpeg produced no audio.")

        return stdout

    async def _process(
        self,
        ctx: commands.Context,
        filter_chain: str,
        label: str,
        suffix: str,
    ):
        if not self._ffmpeg_available():
            return await ctx.send(
                embed=error_embed(
                    f"`{FFMPEG_BINARY}` is not installed on this host."
                )
            )

        try:
            status = await ctx.send(embed=info_embed(f"🎚️ Applying {label}..."))
        except discord.HTTPException:
            status = None

        lock = self._lock_for(ctx.author.id)

        try:
            attachment = await self._resolve_attachment(ctx)
            data = await self._download(attachment)

            async with lock:
                result = await self._run_ffmpeg(data, filter_chain)

            stem = os.path.splitext(attachment.filename)[0][:40] or "audio"

            await ctx.send(
                content=f"🎚️ {label} applied to `{attachment.filename}`.",
                file=discord.File(
                    io.BytesIO(result),
                    filename=f"{stem}-{suffix}.mp3",
                ),
            )

            if status is not None:
                try:
                    await status.delete()
                except discord.HTTPException:
                    pass

        except AudioError as exc:
            if status is not None:
                try:
                    await status.edit(embed=error_embed(str(exc)))
                    return
                except discord.HTTPException:
                    pass

            await ctx.send(embed=error_embed(str(exc)))

        except discord.HTTPException as exc:
            log.warning("Could not send edited audio: %s", exc)
            await ctx.send(
                embed=error_embed(
                    "The audio was processed, but Discord rejected the "
                    "upload. The file is probably over the size limit."
                )
            )

        except Exception:
            log.exception("Unexpected audio processing failure.")

            if status is not None:
                try:
                    await status.edit(
                        embed=error_embed("Audio processing failed.")
                    )
                    return
                except discord.HTTPException:
                    pass

            await ctx.send(embed=error_embed("Audio processing failed."))

    # ----------------------------------------------------------------------
    # Commands
    # ----------------------------------------------------------------------

    @commands.group(
        name="audio",
        aliases=["ae", "fx"],
        invoke_without_command=True,
        usage="audio <effect> [amount] | audio chain a:1 b:2 | audio remix <preset>",
    )
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def audio(self, ctx: commands.Context, *, raw: str = ""):
        """Apply a single named effect, or show help."""
        prefix = ctx.clean_prefix

        if not raw.strip():
            return await ctx.send(
                embed=info_embed(
                    "**Audio editor**\n"
                    f"`{prefix}audio distort 12` · `{prefix}audio reverb`\n"
                    f"`{prefix}audio chain distort:8 reverb:0.6`\n"
                    f"`{prefix}audio remix phonk`\n"
                    f"`{prefix}audio list` — every effect and preset\n\n"
                    "**Editing**\n"
                    f"`{prefix}audio speed 1.5` · `{prefix}audio pitch 4`\n"
                    f"`{prefix}audio volume -6` · `{prefix}audio bass 8`\n"
                    f"`{prefix}audio trim 0:15 1:05` · `{prefix}audio reverse`\n\n"
                    "Attach a file, reply to one, or just run the command "
                    "after posting audio here.\n"
                    f"Maximum input: **{MAX_INPUT_MB:.0f} MB**"
                )
            )

        tokens = raw.split()
        name = _resolve_effect(tokens[0])

        if name is None:
            return await ctx.send(
                embed=error_embed(
                    f"`{tokens[0]}` is not an effect. Try `{prefix}audio list`."
                )
            )

        amount: float | None = None

        if len(tokens) > 1:
            try:
                amount = float(tokens[1])
            except ValueError:
                return await ctx.send(
                    embed=error_embed(f"`{tokens[1]}` is not a number.")
                )

        try:
            chain = _build_effect(name, amount)
        except AudioError as exc:
            return await ctx.send(embed=error_embed(str(exc)))

        label = name if amount is None else f"{name} {amount:g}"

        await self._process(ctx, chain, label, name)

    @audio.command(name="chain", aliases=["stack"], usage="chain distort:8 reverb:0.6")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def audio_chain(self, ctx: commands.Context, *tokens: str):
        """Stack several effects in order."""
        try:
            chain, labels = _parse_chain(list(tokens))
        except AudioError as exc:
            return await ctx.send(embed=error_embed(str(exc)))

        await self._process(ctx, chain, " → ".join(labels), "chained")

    @audio.command(name="remix", aliases=["preset"], usage="remix [preset]")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def audio_remix(self, ctx: commands.Context, preset: str = ""):
        """Apply a curated multi-effect preset."""
        preset = preset.lower().strip()

        if not preset:
            preset = random.choice(list(PRESETS))
        elif preset not in PRESETS:
            return await ctx.send(
                embed=error_embed(
                    f"Unknown preset. Options: {', '.join(sorted(PRESETS))}"
                )
            )

        await self._process(ctx, PRESETS[preset], f"{preset} remix", preset)

    @audio.command(name="list", aliases=["effects", "help"], usage="list")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def audio_list(self, ctx: commands.Context):
        """Show every effect and preset."""
        lines = []

        for name in sorted(EFFECTS):
            _, default, low, high, blurb = EFFECTS[name]

            if high > low:
                lines.append(f"`{name}` — {blurb}, {low:g}-{high:g} (def {default:g})")
            else:
                lines.append(f"`{name}`")

        await ctx.send(
            embed=info_embed(
                "**Effects**\n"
                + "\n".join(lines)
                + "\n\n**Remix presets**\n"
                + ", ".join(f"`{name}`" for name in sorted(PRESETS))
                + f"\n\nStack up to {MAX_CHAIN_LENGTH}: "
                f"`{ctx.clean_prefix}audio chain crush:5 reverb:0.7 wide:3`"
            )
        )

    @audio.command(name="speed", aliases=["tempo"], usage="speed <0.25-4.0>")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def audio_speed(self, ctx: commands.Context, factor: float):
        """Change tempo without touching pitch."""
        if not 0.25 <= factor <= 4.0:
            return await ctx.send(
                embed=error_embed("Speed must be between 0.25 and 4.0.")
            )

        await self._process(
            ctx,
            _atempo_chain(factor),
            f"{factor:g}x speed",
            f"{factor:g}x",
        )

    @audio.command(name="pitch", usage="pitch <-12 to 12>")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def audio_pitch(self, ctx: commands.Context, semitones: float):
        """Shift pitch while keeping the original duration."""
        if not -12.0 <= semitones <= 12.0:
            return await ctx.send(
                embed=error_embed("Pitch must be between -12 and +12.")
            )

        await self._process(
            ctx,
            _resample_pitch(2.0 ** (semitones / 12.0), keep_duration=True),
            f"{semitones:+g} semitone pitch shift",
            "pitch",
        )

    @audio.command(name="bass", aliases=["bassboost"], usage="bass <-20 to 20>")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def audio_bass(self, ctx: commands.Context, gain_db: float):
        """Boost or cut the low end."""
        if not -20.0 <= gain_db <= 20.0:
            return await ctx.send(
                embed=error_embed("Bass gain must be between -20 and 20 dB.")
            )

        await self._process(
            ctx,
            f"bass=g={gain_db:g}:f=110:w=0.6",
            f"{gain_db:+g} dB bass",
            "bass",
        )

    @audio.command(name="volume", aliases=["vol"], usage="volume <-30 to 30>")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def audio_volume(self, ctx: commands.Context, gain_db: float):
        """Change overall loudness."""
        if not -30.0 <= gain_db <= 30.0:
            return await ctx.send(
                embed=error_embed("Volume must be between -30 and 30 dB.")
            )

        await self._process(
            ctx,
            f"volume={gain_db:g}dB",
            f"{gain_db:+g} dB volume",
            "volume",
        )

    @audio.command(name="reverse", usage="reverse")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def audio_reverse(self, ctx: commands.Context):
        """Play the audio backwards."""
        await self._process(ctx, "areverse", "reverse", "reversed")

    @audio.command(name="trim", aliases=["cut"], usage="trim <start> <end>")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def audio_trim(self, ctx: commands.Context, start: str, end: str):
        """Keep only the section between two timestamps."""
        try:
            start_seconds = _parse_timestamp(start)
            end_seconds = _parse_timestamp(end)
        except AudioError as exc:
            return await ctx.send(embed=error_embed(str(exc)))

        if end_seconds <= start_seconds:
            return await ctx.send(
                embed=error_embed("The end time must be after the start time.")
            )

        chain = (
            f"atrim=start={start_seconds:g}:end={end_seconds:g},"
            "asetpts=PTS-STARTPTS"
        )

        await self._process(ctx, chain, f"trim {start} to {end}", "trimmed")

    @audio.command(name="info", usage="info")
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def audio_info(self, ctx: commands.Context):
        """Show ffprobe details for the resolved audio file."""
        if shutil.which(FFPROBE_BINARY) is None:
            return await ctx.send(
                embed=error_embed(f"`{FFPROBE_BINARY}` is not installed.")
            )

        try:
            attachment = await self._resolve_attachment(ctx)
            data = await self._download(attachment)
        except AudioError as exc:
            return await ctx.send(embed=error_embed(str(exc)))

        args = [
            FFPROBE_BINARY,
            "-hide_banner",
            "-loglevel",
            "error",
            "-show_entries",
            "format=duration,bit_rate,format_name",
            "-of",
            "default=noprint_wrappers=1",
            "-i",
            "pipe:0",
        ]

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, _ = await asyncio.wait_for(
                process.communicate(data),
                timeout=60,
            )
        except (FileNotFoundError, asyncio.TimeoutError):
            return await ctx.send(embed=error_embed("ffprobe failed."))

        details = stdout.decode("utf-8", errors="replace").strip()

        await ctx.send(
            embed=info_embed(
                f"**{attachment.filename}**\n"
                f"Size: {attachment.size / 1024 / 1024:.2f} MB\n"
                f"```\n{details or 'no data'}\n```"
            )
        )


async def setup(bot):
    await bot.add_cog(Audio(bot))
