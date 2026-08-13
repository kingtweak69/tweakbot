"""Live opt-in voice conversation mode for TweakBot.

The requester explicitly starts a session while connected to a Discord voice
channel. Only that requester's decoded PCM is processed. Audio is buffered in
memory, transcribed, passed through the normal Personality/agent runtime, spoken
back with the existing TTS cog, and discarded. This cog intentionally refuses to
replace a Wavelink/Music voice client: music/DJ and live voice-agent modes remain
separate so neither voice protocol corrupts the other.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import threading
import time
import wave
from dataclasses import dataclass
from typing import Any

import discord
from discord.ext import commands

import config
from utils.attachments import AttachmentAnalyzer

try:
    from discord.ext import voice_recv
except ImportError:  # Keep the rest of TweakBot bootable if an install is incomplete.
    voice_recv = None  # type: ignore[assignment]

log = logging.getLogger("cogs.voice_agent")

VOICE_AGENT_SILENCE_SECONDS = max(
    0.5, float(os.getenv("VOICE_AGENT_SILENCE_SECONDS", "1.1"))
)
VOICE_AGENT_MAX_UTTERANCE_SECONDS = max(
    3, min(120, int(os.getenv("VOICE_AGENT_MAX_UTTERANCE_SECONDS", "30")))
)
VOICE_AGENT_MIN_UTTERANCE_MS = max(
    100, min(3000, int(os.getenv("VOICE_AGENT_MIN_UTTERANCE_MS", "300")))
)
VOICE_AGENT_TEXT_MIRROR = os.getenv("VOICE_AGENT_TEXT_MIRROR", "true").strip().lower() in {
    "1", "true", "yes", "on"
}
PCM_RATE = 48_000
PCM_CHANNELS = 2
PCM_SAMPLE_WIDTH = 2
PCM_BYTES_PER_SECOND = PCM_RATE * PCM_CHANNELS * PCM_SAMPLE_WIDTH
MAX_PCM_BYTES = PCM_BYTES_PER_SECOND * VOICE_AGENT_MAX_UTTERANCE_SECONDS
MIN_PCM_BYTES = max(1, PCM_BYTES_PER_SECOND * VOICE_AGENT_MIN_UTTERANCE_MS // 1000)


if voice_recv is not None:
    class TargetPCMSink(voice_recv.AudioSink):
        """Thread-safe, single-user decoded-PCM buffer."""

        def __init__(self, target_user_id: int) -> None:
            super().__init__()
            self.target_user_id = int(target_user_id)
            self._lock = threading.Lock()
            self._buffer = bytearray()
            self._last_packet = 0.0
            self._closed = False

        def wants_opus(self) -> bool:
            return False

        def write(self, user, data) -> None:
            if self._closed or user is None or int(user.id) != self.target_user_id:
                return
            pcm = getattr(data, "pcm", None)
            if not pcm:
                return
            now = time.monotonic()
            with self._lock:
                remaining = MAX_PCM_BYTES - len(self._buffer)
                if remaining > 0:
                    self._buffer.extend(bytes(pcm)[:remaining])
                self._last_packet = now

        def pop_ready(self, silence_seconds: float) -> bytes | None:
            now = time.monotonic()
            with self._lock:
                if len(self._buffer) < MIN_PCM_BYTES:
                    return None
                if len(self._buffer) < MAX_PCM_BYTES and now - self._last_packet < silence_seconds:
                    return None
                payload = bytes(self._buffer)
                self._buffer.clear()
                self._last_packet = 0.0
                return payload

        def discard(self) -> None:
            with self._lock:
                self._buffer.clear()
                self._last_packet = 0.0

        def cleanup(self) -> None:
            self._closed = True
            self.discard()
else:
    class TargetPCMSink:  # type: ignore[no-redef]
        pass


@dataclass(slots=True)
class VoiceAgentSession:
    guild_id: int
    owner_id: int
    channel_id: int
    text_channel_id: int
    ctx: commands.Context
    voice_client: Any
    sink: Any
    monitor_task: asyncio.Task | None = None
    processing_lock: asyncio.Lock | None = None
    active: bool = True


class VoiceAgent(commands.Cog):
    SOURCE = "voice_agent"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.sessions: dict[int, VoiceAgentSession] = {}
        self.transcriber = AttachmentAnalyzer(bot)

    async def cog_load(self) -> None:
        registry = self.bot.capabilities
        registry.register(
            name="voice_agent_start",
            description=(
                "Start an opt-in live voice conversation with the requesting user in "
                "their current Discord voice channel. Music/DJ must be disconnected first."
            ),
            parameters={"type": "object", "properties": {}},
            handler=self._tool_start,
            category="voice",
            source=self.SOURCE,
            guild_only=True,
        )
        registry.register(
            name="voice_agent_stop",
            description="Stop the requester's live TweakBot voice conversation session.",
            parameters={"type": "object", "properties": {}},
            handler=self._tool_stop,
            category="voice",
            source=self.SOURCE,
            guild_only=True,
        )
        registry.register(
            name="voice_agent_status",
            description="Show whether live TweakBot voice conversation mode is active here.",
            parameters={"type": "object", "properties": {}},
            handler=self._tool_status,
            category="voice",
            source=self.SOURCE,
            guild_only=True,
        )

    async def cog_unload(self) -> None:
        self.bot.capabilities.unregister_source(self.SOURCE)
        for guild_id in list(self.sessions):
            await self._stop_guild(guild_id, disconnect=True)

    @staticmethod
    def _wav_bytes(pcm: bytes) -> bytes:
        output = io.BytesIO()
        with wave.open(output, "wb") as wav:
            wav.setnchannels(PCM_CHANNELS)
            wav.setsampwidth(PCM_SAMPLE_WIDTH)
            wav.setframerate(PCM_RATE)
            wav.writeframes(pcm)
        return output.getvalue()

    async def _start(self, ctx: commands.Context) -> str:
        if voice_recv is None:
            return "Live voice receive is unavailable: discord-ext-voice-recv is not installed."
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            return "Live voice mode only works inside a Discord server."
        if not ctx.author.voice or not ctx.author.voice.channel:
            return "Join a voice channel first."

        existing = self.sessions.get(ctx.guild.id)
        if existing and existing.active:
            if existing.owner_id == ctx.author.id:
                return f"Voice agent is already listening to you in <#{existing.channel_id}>."
            return "A voice-agent session is already active in this server."

        # A guild can only have one Discord voice protocol at a time. Never replace
        # the Wavelink player; doing so is exactly the kind of cross-feature breakage
        # this upgrade is designed to avoid.
        if ctx.guild.voice_client is not None:
            return (
                "TweakBot is already connected to voice (usually Music/DJ). "
                "Disconnect music first, then start voice-agent mode."
            )

        channel = ctx.author.voice.channel
        try:
            vc = await channel.connect(
                cls=voice_recv.VoiceRecvClient,
                self_deaf=False,
                self_mute=False,
            )
        except Exception as exc:
            log.exception("Voice agent could not connect")
            return f"Could not connect voice receive: {type(exc).__name__}: {exc}"

        sink = TargetPCMSink(ctx.author.id)
        session = VoiceAgentSession(
            guild_id=ctx.guild.id,
            owner_id=ctx.author.id,
            channel_id=channel.id,
            text_channel_id=ctx.channel.id,
            ctx=ctx,
            voice_client=vc,
            sink=sink,
            processing_lock=asyncio.Lock(),
        )
        self.sessions[ctx.guild.id] = session
        vc.listen(sink, after=lambda error: self._listen_after(ctx.guild.id, error))
        session.monitor_task = asyncio.create_task(
            self._monitor(session), name=f"tweakbot-voice-agent-{ctx.guild.id}"
        )
        return (
            f"Voice agent started in {channel.mention}. I will process only "
            f"{ctx.author.mention}'s audio; raw audio is discarded after transcription."
        )

    def _listen_after(self, guild_id: int, error: Exception | None) -> None:
        if error:
            log.error("Voice receive stopped for guild %s: %s", guild_id, error)

    async def _monitor(self, session: VoiceAgentSession) -> None:
        try:
            while session.active:
                await asyncio.sleep(0.25)
                payload = session.sink.pop_ready(VOICE_AGENT_SILENCE_SECONDS)
                if payload:
                    asyncio.create_task(self._process_utterance(session, payload))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Voice agent monitor failed for guild %s", session.guild_id)

    async def _process_utterance(self, session: VoiceAgentSession, pcm: bytes) -> None:
        if not session.active or not session.processing_lock:
            return
        async with session.processing_lock:
            # PCM is already detached from the sink buffer. It exists only in this
            # coroutine and is released after this turn.
            wav_data = self._wav_bytes(pcm)
            transcript = await self.transcriber._transcribe(
                wav_data, "voice-agent-turn.wav", "audio/wav"
            )
            if not session.active:
                return
            if not transcript or transcript.startswith("Transcription unavailable"):
                log.warning("Voice-agent transcription failed: %s", transcript[:300])
                return

            personality = self.bot.get_cog("Personality")
            if personality is None or not hasattr(personality, "respond"):
                return

            if VOICE_AGENT_TEXT_MIRROR:
                try:
                    await session.ctx.channel.send(
                        f"🎙️ **{session.ctx.author.display_name}:** {transcript[:1800]}",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass

            reply = await personality.respond(
                session.ctx,
                transcript,
                send_reply=False,
                analyze_attachments=False,
            )
            if not reply or not session.active:
                return

            if VOICE_AGENT_TEXT_MIRROR:
                try:
                    await session.ctx.channel.send(
                        f"🔊 **TweakBot:** {reply[:1800]}",
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                except discord.HTTPException:
                    pass

            tts = self.bot.get_cog("TTS")
            if tts is None or not hasattr(tts, "synthesize_bytes"):
                return
            try:
                audio = await tts.synthesize_bytes(
                    user_id=session.owner_id,
                    text=reply[:4000],
                    mode="speak",
                )
                await self._play_bytes(session, audio)
            except Exception:
                log.exception("Voice-agent TTS/playback failed")

    async def _play_bytes(self, session: VoiceAgentSession, audio: bytes) -> None:
        vc = session.voice_client
        if not session.active or not vc or not vc.is_connected():
            return
        if vc.is_playing():
            vc.stop_playing() if hasattr(vc, "stop_playing") else vc.stop()
        done = asyncio.Event()
        loop = asyncio.get_running_loop()

        def after(error: Exception | None) -> None:
            if error:
                log.error("Voice-agent playback error: %s", error)
            loop.call_soon_threadsafe(done.set)

        source = discord.FFmpegPCMAudio(io.BytesIO(audio), pipe=True, options="-vn")
        vc.play(source, after=after)
        try:
            await asyncio.wait_for(done.wait(), timeout=180)
        except asyncio.TimeoutError:
            if vc.is_playing():
                vc.stop_playing() if hasattr(vc, "stop_playing") else vc.stop()

    async def _stop_guild(self, guild_id: int, *, disconnect: bool) -> bool:
        session = self.sessions.pop(int(guild_id), None)
        if not session:
            return False
        session.active = False
        session.sink.discard()
        if session.monitor_task:
            session.monitor_task.cancel()
        vc = session.voice_client
        try:
            if hasattr(vc, "is_listening") and vc.is_listening():
                vc.stop_listening()
        except Exception:
            pass
        if disconnect and vc and vc.is_connected():
            try:
                await vc.disconnect(force=True)
            except Exception:
                log.exception("Voice-agent disconnect failed")
        return True

    async def _stop(self, ctx: commands.Context) -> str:
        if ctx.guild is None:
            return "Voice-agent mode only works in a server."
        session = self.sessions.get(ctx.guild.id)
        if not session:
            return "No voice-agent session is active here."
        allowed = session.owner_id == ctx.author.id
        if isinstance(ctx.author, discord.Member):
            allowed = allowed or ctx.author.guild_permissions.manage_guild
        if not allowed:
            return "Only the user who started this session or a server manager can stop it."
        await self._stop_guild(ctx.guild.id, disconnect=True)
        return "Voice agent stopped and disconnected."

    async def _status(self, ctx: commands.Context) -> str:
        if ctx.guild is None:
            return "Voice-agent mode only works in a server."
        session = self.sessions.get(ctx.guild.id)
        if not session or not session.active:
            return "Voice agent is not active here."
        return (
            f"Voice agent is active in <#{session.channel_id}> for <@{session.owner_id}>. "
            "Only that user's audio is processed."
        )

    async def _tool_start(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        return await self._start(ctx)

    async def _tool_stop(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        return await self._stop(ctx)

    async def _tool_status(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        return await self._status(ctx)

    @commands.hybrid_group(name="voiceagent", aliases=["va"], invoke_without_command=True)
    async def voiceagent(self, ctx: commands.Context) -> None:
        """Manage live opt-in voice conversation mode."""
        await ctx.send(await self._status(ctx))

    @voiceagent.command(name="start")
    async def voiceagent_start(self, ctx: commands.Context) -> None:
        await ctx.send(await self._start(ctx))

    @voiceagent.command(name="stop")
    async def voiceagent_stop(self, ctx: commands.Context) -> None:
        await ctx.send(await self._stop(ctx))

    @voiceagent.command(name="status")
    async def voiceagent_status(self, ctx: commands.Context) -> None:
        await ctx.send(await self._status(ctx))

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        session = self.sessions.get(member.guild.id)
        if not session or session.owner_id != member.id:
            return
        if after.channel is None or after.channel.id != session.channel_id:
            await self._stop_guild(member.guild.id, disconnect=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(VoiceAgent(bot))
