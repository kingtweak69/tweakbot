"""Structured persistent memory helpers for TweakBot conversations."""
from __future__ import annotations

from typing import Any

from discord.ext import commands


class MemoryManager:
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @staticmethod
    def scope(ctx: commands.Context) -> tuple[int, int, int]:
        return (
            int(ctx.author.id),
            int(ctx.guild.id) if ctx.guild else 0,
            int(ctx.channel.id),
        )

    async def append(
        self,
        ctx: commands.Context,
        role: str,
        content: str,
        *,
        kind: str = "conversation",
    ) -> int | None:
        if not self.bot.db:
            return None
        user_id, guild_id, channel_id = self.scope(ctx)
        return await self.bot.db.add_ai_message(
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            role=role,
            content=(content or "")[:20000],
            kind=kind,
        )

    async def recent(self, ctx: commands.Context, limit: int) -> list[dict[str, Any]]:
        if not self.bot.db:
            return []
        user_id, guild_id, channel_id = self.scope(ctx)
        rows = await self.bot.db.get_ai_messages(
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            limit=limit,
        )
        return [
            {"role": str(row["role"]), "content": str(row["content"])}
            for row in rows
            if row["role"] in {"user", "assistant"}
        ]

    async def summary(self, ctx: commands.Context) -> str:
        if not self.bot.db:
            return ""
        user_id, guild_id, channel_id = self.scope(ctx)
        return await self.bot.db.get_ai_summary(
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
        )

    async def set_summary(self, ctx: commands.Context, summary: str) -> None:
        if not self.bot.db:
            return
        user_id, guild_id, channel_id = self.scope(ctx)
        await self.bot.db.set_ai_summary(
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            summary=summary[:12000],
        )

    async def clear_conversation(self, ctx: commands.Context) -> None:
        if not self.bot.db:
            return
        user_id, guild_id, channel_id = self.scope(ctx)
        await self.bot.db.clear_ai_conversation(
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
        )

    async def count(self, ctx: commands.Context) -> int:
        if not self.bot.db:
            return 0
        user_id, guild_id, channel_id = self.scope(ctx)
        return await self.bot.db.count_ai_messages(
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
        )

    async def compact(self, ctx: commands.Context, keep_last: int) -> int:
        if not self.bot.db:
            return 0
        user_id, guild_id, channel_id = self.scope(ctx)
        return await self.bot.db.compact_ai_messages(
            user_id=user_id,
            guild_id=guild_id,
            channel_id=channel_id,
            keep_last=keep_last,
        )

    async def delete_message(self, message_id: int | None) -> None:
        if self.bot.db and message_id:
            await self.bot.db.delete_ai_message(int(message_id))

    async def memory_preamble(self, ctx: commands.Context) -> str:
        if not self.bot.db:
            return ""
        user_rows = await self.bot.db.get_ai_memories(
            owner_user_id=ctx.author.id,
            guild_id=0,
            scopes=("user",),
            limit=50,
        )
        guild_rows = []
        if ctx.guild:
            guild_rows = await self.bot.db.get_ai_memories(
                owner_user_id=0,
                guild_id=ctx.guild.id,
                scopes=("guild",),
                limit=50,
            )

        lines: list[str] = []
        if user_rows:
            lines.append("Durable user memory:")
            lines.extend(
                f"- {row['memory_key']}: {row['memory_value']}" for row in reversed(user_rows)
            )
        if guild_rows:
            lines.append("Durable guild memory:")
            lines.extend(
                f"- {row['memory_key']}: {row['memory_value']}" for row in reversed(guild_rows)
            )
        return "\n".join(lines)[:12000]
