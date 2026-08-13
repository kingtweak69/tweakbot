"""Durable user/guild memory capabilities for TweakBot's agent."""
from __future__ import annotations

from typing import Any

import discord
from discord.ext import commands

SOURCE = "agent_memory"
_SECRET_WORDS = {"token", "password", "secret", "api_key", "apikey", "credential", "private_key"}


class AgentMemory(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        registry = self.bot.capabilities
        base_schema = {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["key", "value"],
        }
        registry.register(
            name="remember_user_memory",
            description=(
                "Persist a durable non-secret fact or preference about the requesting user. "
                "Use only for information worth remembering across conversations."
            ),
            parameters=base_schema,
            handler=self._remember_user,
            category="memory",
            source=SOURCE,
        )
        registry.register(
            name="forget_user_memory",
            description="Delete one durable memory belonging to the requesting user.",
            parameters={
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
            handler=self._forget_user,
            category="memory",
            source=SOURCE,
        )
        registry.register(
            name="remember_guild_memory",
            description=(
                "Persist a non-secret server-wide fact/configuration note. The requester must "
                "have Manage Server permission."
            ),
            parameters=base_schema,
            handler=self._remember_guild,
            category="memory",
            source=SOURCE,
            guild_only=True,
        )
        registry.register(
            name="list_memories",
            description="List durable user and server memories currently available to TweakBot.",
            parameters={"type": "object", "properties": {}},
            handler=self._list,
            category="memory",
            source=SOURCE,
        )

    async def cog_unload(self) -> None:
        self.bot.capabilities.unregister_source(SOURCE)

    @staticmethod
    def _validate(key: str, value: str) -> tuple[str, str]:
        key = key.strip()[:120]
        value = value.strip()[:2000]
        if not key or not value:
            raise ValueError("Memory key and value are required.")
        lowered = key.casefold().replace("-", "_").replace(" ", "_")
        if any(word in lowered for word in _SECRET_WORDS):
            raise ValueError("Secrets and credentials must not be stored as AI memory.")
        return key, value

    async def _remember_user(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        if not self.bot.db:
            return "Database is unavailable."
        try:
            key, value = self._validate(str(args.get("key") or ""), str(args.get("value") or ""))
        except ValueError as exc:
            return str(exc)
        await self.bot.db.set_ai_memory(
            owner_user_id=ctx.author.id,
            guild_id=0,
            scope="user",
            key=key,
            value=value,
        )
        return f"Remembered user memory `{key}`."

    async def _forget_user(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        if not self.bot.db:
            return "Database is unavailable."
        key = str(args.get("key") or "").strip()[:120]
        removed = await self.bot.db.delete_ai_memory(
            owner_user_id=ctx.author.id,
            guild_id=0,
            scope="user",
            key=key,
        )
        return f"Forgot user memory `{key}`." if removed else f"No user memory named `{key}` exists."

    async def _remember_guild(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        if not self.bot.db or not ctx.guild or not isinstance(ctx.author, discord.Member):
            return "Guild memory is unavailable here."
        if not ctx.author.guild_permissions.manage_guild:
            return "Denied: Manage Server permission is required for guild memory."
        try:
            key, value = self._validate(str(args.get("key") or ""), str(args.get("value") or ""))
        except ValueError as exc:
            return str(exc)
        await self.bot.db.set_ai_memory(
            owner_user_id=0,
            guild_id=ctx.guild.id,
            scope="guild",
            key=key,
            value=value,
        )
        return f"Remembered guild memory `{key}`."

    async def _list(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        if not self.bot.db:
            return "Database is unavailable."
        user_rows = await self.bot.db.get_ai_memories(
            owner_user_id=ctx.author.id, guild_id=0, scopes=("user",), limit=50
        )
        guild_rows = []
        if ctx.guild:
            guild_rows = await self.bot.db.get_ai_memories(
                owner_user_id=0, guild_id=ctx.guild.id, scopes=("guild",), limit=50
            )
        lines = [f"user:{row['memory_key']} = {row['memory_value']}" for row in user_rows]
        lines += [f"guild:{row['memory_key']} = {row['memory_value']}" for row in guild_rows]
        return "\n".join(lines)[:12000] if lines else "No durable memories are stored."


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AgentMemory(bot))
