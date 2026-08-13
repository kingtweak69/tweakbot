"""Read-only self diagnostics for TweakBot's agent/runtime stack."""
from __future__ import annotations

import asyncio
import os
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

import config
from utils.workspace import WORKSPACE_ROOT


class AgentHealth(commands.Cog):
    SOURCE = "agent_health"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.capabilities.register(
            name="system_health",
            description=(
                "Run read-only TweakBot diagnostics: cogs, capability registry, Postgres, "
                "AI endpoint, GitHub/Railway linkage, Lavalink, ElevenLabs, voice receive, "
                "FFmpeg, guarded workspaces, persistent jobs, and API gateway. Never returns secrets."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "deep": {
                        "type": "boolean",
                        "description": "When true, make a tiny live request to the configured chat model endpoint.",
                    }
                },
            },
            handler=self._tool_health,
            category="diagnostics",
            source=self.SOURCE,
        )

    async def cog_unload(self) -> None:
        self.bot.capabilities.unregister_source(self.SOURCE)

    @staticmethod
    def _mark(ok: bool | None) -> str:
        return "✅" if ok is True else "❌" if ok is False else "⚠️"

    async def _database_status(self) -> tuple[bool, str]:
        db = getattr(self.bot, "db", None)
        if db is None:
            return False, "database object missing"
        try:
            value = await asyncio.wait_for(db._fetchval("SELECT 1"), timeout=5)
            return value == 1, "Postgres query OK" if value == 1 else f"unexpected SELECT 1 result: {value!r}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {exc}"

    async def _ai_status(self, deep: bool) -> tuple[bool | None, str]:
        personality = self.bot.get_cog("Personality")
        if personality is None:
            return False, "Personality cog not loaded"
        if not getattr(personality, "_client", None):
            return False, "chat client not configured"
        if not deep:
            return None, f"configured model={getattr(config, 'OPENAI_MODEL', '') or '<unset>'}; live ping skipped"
        try:
            data = await asyncio.wait_for(
                personality._client.create(
                    model=config.OPENAI_MODEL,
                    messages=[{"role": "user", "content": "Reply exactly OK"}],
                    max_tokens=8,
                    temperature=0,
                    stream=False,
                ),
                timeout=30,
            )
            choices = data.get("choices") or []
            return bool(choices), f"live model ping returned {len(choices)} choice(s)"
        except Exception as exc:
            return False, f"live ping failed: {type(exc).__name__}: {exc}"

    async def _oauth_status(self, ctx: commands.Context) -> list[tuple[str, bool | None, str]]:
        rows: list[tuple[str, bool | None, str]] = []
        db = getattr(self.bot, "db", None)

        github = self.bot.get_cog("GitHub")
        if github is None:
            rows.append(("GitHub", False, "cog not loaded"))
        else:
            try:
                linked = bool(await github._user_token(ctx.author.id))
                rows.append(("GitHub", linked, "linked for requester" if linked else "requester not linked"))
            except Exception as exc:
                rows.append(("GitHub", False, f"link check failed: {type(exc).__name__}"))

        railway = self.bot.get_cog("Railway")
        if railway is None:
            rows.append(("Railway", False, "cog not loaded"))
        else:
            configured = bool(getattr(railway, "_oauth_is_configured", lambda: False)())
            linked = False
            try:
                linked = bool(await railway._credentials(ctx.author.id))
            except Exception:
                pass
            rows.append((
                "Railway",
                configured and linked,
                f"oauth_configured={configured}; requester_session_active={linked}",
            ))
        return rows

    async def _report(self, ctx: commands.Context, *, deep: bool = False) -> str:
        lines = ["TweakBot system health"]

        # Cog loader state.
        loaded = list(getattr(self.bot, "loaded_extensions", []))
        failed = dict(getattr(self.bot, "failed_extensions", {}))
        lines.append(
            f"{self._mark(not failed)} Cogs: loaded={len(loaded) or len(self.bot.cogs)}; failed={len(failed)}"
        )
        for name, reason in list(failed.items())[:8]:
            lines.append(f"   - {name}: {reason[:180]}")

        # Registry state and source distribution.
        registry = getattr(self.bot, "capabilities", None)
        if registry is None:
            lines.append("❌ Capabilities: registry missing")
        else:
            available = registry.available(ctx)
            sources = Counter(cap.source for cap in available)
            categories = Counter(cap.category for cap in available)
            lines.append(
                f"✅ Capabilities: {len(available)} available; sources="
                + ", ".join(f"{k}:{v}" for k, v in sorted(sources.items()))
            )
            lines.append(
                "   categories=" + ", ".join(f"{k}:{v}" for k, v in sorted(categories.items()))
            )

        db_ok, db_detail = await self._database_status()
        lines.append(f"{self._mark(db_ok)} Postgres: {db_detail}")

        ai_ok, ai_detail = await self._ai_status(deep)
        lines.append(f"{self._mark(ai_ok)} AI: {ai_detail}")

        for label, ok, detail in await self._oauth_status(ctx):
            lines.append(f"{self._mark(ok)} {label}: {detail}")

        music = self.bot.get_cog("Music")
        music_ok = bool(music and getattr(music, "_connected", False))
        lines.append(
            f"{self._mark(music_ok)} Lavalink: "
            + ("connected" if music_ok else "not connected")
        )

        tts = self.bot.get_cog("TTS")
        eleven_key = bool(os.getenv("ELEVENLABS_API_KEY", "").strip())
        try:
            voice_id, voice_name = await tts.voice_for_user(ctx.author.id) if tts else ("", "")
        except Exception:
            voice_id, voice_name = "", ""
        tts_ok = bool(tts and eleven_key and voice_id)
        lines.append(
            f"{self._mark(tts_ok)} ElevenLabs: cog={bool(tts)}; key_configured={eleven_key}; "
            f"voice={'configured' if voice_id else 'missing'}"
            + (f" ({voice_name})" if voice_name else "")
        )

        try:
            from discord.ext import voice_recv  # noqa: F401
            voice_recv_ok = True
        except ImportError:
            voice_recv_ok = False
        lines.append(
            f"{self._mark(voice_recv_ok)} Voice receive: "
            + ("extension installed" if voice_recv_ok else "discord-ext-voice-recv missing")
        )

        ffmpeg = shutil.which("ffmpeg")
        ffprobe = shutil.which("ffprobe")
        lines.append(
            f"{self._mark(bool(ffmpeg and ffprobe))} FFmpeg: ffmpeg={bool(ffmpeg)}; ffprobe={bool(ffprobe)}"
        )

        try:
            WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
            owned = list(WORKSPACE_ROOT.glob(f"{ctx.author.id}-*"))
            disk = shutil.disk_usage(WORKSPACE_ROOT)
            lines.append(
                f"✅ Workspaces: requester={len(owned)}; free={disk.free // (1024**2)} MiB; root writable={os.access(WORKSPACE_ROOT, os.W_OK)}"
            )
        except Exception as exc:
            lines.append(f"❌ Workspaces: {type(exc).__name__}: {exc}")

        db = getattr(self.bot, "db", None)
        if db is not None:
            try:
                jobs = await db._fetchall(
                    "SELECT status, COUNT(*) AS count FROM agent_jobs WHERE user_id = $1 GROUP BY status",
                    int(ctx.author.id),
                )
                job_text = ", ".join(f"{row['status']}:{row['count']}" for row in jobs) or "none"
                lines.append(f"✅ Agent jobs: {job_text}")
            except Exception as exc:
                lines.append(f"❌ Agent jobs: {type(exc).__name__}: {exc}")

        try:
            from utils.server import server
            gateway_running = server.runner is not None
            lines.append(
                f"{self._mark(gateway_running)} API gateway: "
                + ("runner active" if gateway_running else "runner inactive")
            )
        except Exception as exc:
            lines.append(f"❌ API gateway: {type(exc).__name__}: {exc}")

        voice_agent = self.bot.get_cog("VoiceAgent")
        if ctx.guild and voice_agent:
            active = ctx.guild.id in getattr(voice_agent, "sessions", {})
            lines.append(f"✅ Live voice agent: {'active' if active else 'idle'}")

        return "\n".join(lines)[:12000]

    async def _tool_health(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        return await self._report(ctx, deep=bool(args.get("deep", False)))

    @commands.hybrid_command(name="agenthealth", aliases=["diagnostics", "diag"])
    async def agenthealth(self, ctx: commands.Context, deep: bool = False) -> None:
        """Run TweakBot self diagnostics. Set deep=true for a live model ping."""
        report = await self._report(ctx, deep=deep)
        # Discord limit is 2000 chars. Split without embeds so logs/errors remain copyable.
        for start in range(0, len(report), 1900):
            await ctx.send(
                f"```text\n{report[start:start+1900]}\n```",
                allowed_mentions=discord.AllowedMentions.none(),
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AgentHealth(bot))
