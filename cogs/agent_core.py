"""Core AI-agent capabilities for TweakBot.

The conversation cog does not own these operations.  This cog registers the
framework-level capabilities (command discovery/execution and Discord-native
moderation) with ``bot.capabilities``.  Other cogs register their own tools.
"""
from __future__ import annotations

import copy
import datetime
import logging
from typing import Any

import discord
from discord.ext import commands

import config

log = logging.getLogger("cogs.agent_core")

AI_COMMAND_TOOLS_ENABLED = bool(getattr(config, "AI_COMMAND_TOOLS_ENABLED", False))
AI_DESTRUCTIVE_TOOLS_ENABLED = bool(getattr(config, "AI_DESTRUCTIVE_TOOLS_ENABLED", False))
AI_MODERATION_TOOLS_ENABLED = bool(getattr(config, "AI_MODERATION_TOOLS_ENABLED", False))


class AgentCore(commands.Cog):
    SOURCE = "agent_core"

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        if AI_COMMAND_TOOLS_ENABLED:
            self._register_command_tools()
        if AI_MODERATION_TOOLS_ENABLED:
            self._register_moderation_tools()

    async def cog_unload(self) -> None:
        self.bot.capabilities.unregister_source(self.SOURCE)

    def _register_command_tools(self) -> None:
        registry = self.bot.capabilities
        registry.register(
            name="list_bot_commands",
            description=(
                "Inspect TweakBot's currently loaded command catalog. Use this when an "
                "operation is needed but the exact command syntax is uncertain."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional capability/command keyword filter.",
                    }
                },
            },
            handler=self._tool_list_bot_commands,
            category="core",
            source=self.SOURCE,
        )
        if not AI_DESTRUCTIVE_TOOLS_ENABLED:
            return
        registry.register(
            name="run_bot_command",
            description=(
                "Execute an existing TweakBot command as the requesting Discord user. "
                "Do not include the bot prefix. Normal command checks, permissions, "
                "per-user GitHub/Railway OAuth, confirmations, cooldowns, attachments, "
                "and audit behavior remain authoritative."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "Exact TweakBot command text without a prefix.",
                    }
                },
                "required": ["command"],
            },
            handler=self._tool_run_bot_command,
            category="core",
            source=self.SOURCE,
            destructive=True,
        )

    def _register_moderation_tools(self) -> None:
        definitions: list[tuple[str, str, dict[str, Any]]] = [
            (
                "ban_members",
                "Ban one or more members from the server.",
                {
                    "type": "object",
                    "properties": {
                        "user_ids": {"type": "array", "items": {"type": "string"}},
                        "reason": {"type": "string"},
                    },
                    "required": ["user_ids"],
                },
            ),
            (
                "kick_members",
                "Kick one or more members from the server.",
                {
                    "type": "object",
                    "properties": {
                        "user_ids": {"type": "array", "items": {"type": "string"}},
                        "reason": {"type": "string"},
                    },
                    "required": ["user_ids"],
                },
            ),
            (
                "timeout_member",
                "Timeout a member for a number of minutes.",
                {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "duration_minutes": {"type": "integer"},
                        "reason": {"type": "string"},
                    },
                    "required": ["user_id", "duration_minutes"],
                },
            ),
            (
                "timeout_remove",
                "Remove an active timeout from a member.",
                {
                    "type": "object",
                    "properties": {"user_id": {"type": "string"}},
                    "required": ["user_id"],
                },
            ),
            (
                "warn_member",
                "Issue a formal warning to a member.",
                {
                    "type": "object",
                    "properties": {
                        "user_id": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                    "required": ["user_id"],
                },
            ),
            (
                "purge_messages",
                "Delete recent messages from the current channel.",
                {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                    "required": ["count"],
                },
            ),
            (
                "lock_channel",
                "Lock the current channel.",
                {"type": "object", "properties": {}},
            ),
            (
                "unlock_channel",
                "Unlock the current channel.",
                {"type": "object", "properties": {}},
            ),
            (
                "slowmode",
                "Set slowmode delay on the current channel.",
                {
                    "type": "object",
                    "properties": {"seconds": {"type": "integer"}},
                    "required": ["seconds"],
                },
            ),
        ]
        for name, description, schema in definitions:
            self.bot.capabilities.register(
                name=name,
                description=description,
                parameters=schema,
                handler=self._moderation_handler(name),
                category="moderation",
                source=self.SOURCE,
                guild_only=True,
                destructive=True,
            )

    def _command_catalog(self, query: str = "") -> str:
        query = (query or "").strip().casefold()
        lines: list[str] = []
        for command in sorted(self.bot.walk_commands(), key=lambda item: item.qualified_name):
            qualified = command.qualified_name
            usage = str(command.usage or command.signature or "").strip()
            short_doc = str(command.short_doc or "").strip().replace("\n", " ")
            searchable = f"{qualified} {usage} {short_doc}".casefold()
            if query and query not in searchable:
                continue
            line = qualified
            if usage:
                line += f" {usage}"
            if short_doc:
                line += f" — {short_doc}"
            lines.append(line[:300])
            if len(lines) >= 100:
                break
        if not lines:
            return f"No loaded commands matched {query!r}." if query else "No commands are loaded."
        return ("Loaded TweakBot commands:\n" + "\n".join(lines))[:12000]

    async def _tool_list_bot_commands(
        self, ctx: commands.Context, args: dict[str, Any]
    ) -> str:
        return self._command_catalog(str(args.get("query") or ""))

    async def _tool_run_bot_command(
        self, ctx: commands.Context, args: dict[str, Any]
    ) -> str:
        command_text = str(args.get("command") or "").strip()
        if not command_text:
            return "No command was provided."
        if len(command_text) > 1900:
            return "Command text is too long."
        if any(ord(char) < 32 and char != "\t" for char in command_text):
            return "Command text contains invalid control characters."

        prefix = await self.bot._get_prefix(self.bot, ctx.message)
        if isinstance(prefix, (list, tuple)):
            prefix = next(
                (item for item in prefix if isinstance(item, str) and item),
                config.PREFIX,
            )
        prefix = str(prefix or config.PREFIX)

        if command_text.startswith(prefix):
            command_text = command_text[len(prefix):].lstrip()
        elif command_text.startswith("/"):
            command_text = command_text[1:].lstrip()

        synthetic = copy.copy(ctx.message)
        synthetic.content = f"{prefix}{command_text}"
        command_ctx = await self.bot.get_context(synthetic)

        if not command_ctx.valid or command_ctx.command is None:
            root = command_text.split(maxsplit=1)[0] if command_text else ""
            return (
                f"Unknown TweakBot command {root!r}. "
                "Use list_bot_commands to inspect the loaded command catalog."
            )

        # Prevent recursive/self-mutating calls into the conversation engine.
        if command_ctx.command.qualified_name in {"chatreset", "chathistory"}:
            return "That conversation-management command cannot run inside the AI tool loop."

        await self.bot.invoke(command_ctx)
        if command_ctx.command_failed:
            return (
                f"Command `{command_ctx.command.qualified_name}` ran but failed. "
                "The normal command/error handler posted the authoritative details to Discord."
            )
        return (
            f"Command `{command_ctx.command.qualified_name}` executed as {ctx.author}. "
            "Its normal Discord response contains the authoritative result."
        )

    def _moderation_handler(self, name: str):
        async def handler(ctx: commands.Context, args: dict[str, Any]) -> str:
            return await self._moderation_action(ctx, name, args)
        return handler

    @staticmethod
    def _can_act_on(author: discord.Member, target: discord.Member) -> bool:
        if target.id == author.id:
            return False
        if target.id == author.guild.owner_id:
            return False
        if author.id != author.guild.owner_id and target.top_role >= author.top_role:
            return False
        return True

    async def _moderation_action(
        self,
        ctx: commands.Context,
        name: str,
        args: dict[str, Any],
    ) -> str:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return "Moderation tools only work inside a server."

        guild = ctx.guild
        author = ctx.author
        reason = str(args.get("reason") or "Requested through TweakBot AI.")[:450]

        try:
            if name == "ban_members":
                if not author.guild_permissions.ban_members:
                    return "Denied: missing ban_members permission."
                ids = [int(value) for value in args.get("user_ids", [])][:20]
                completed = 0
                for user_id in ids:
                    member = guild.get_member(user_id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(user_id)
                        except discord.NotFound:
                            continue
                        except discord.HTTPException:
                            continue
                    if not self._can_act_on(author, member):
                        continue
                    try:
                        await guild.ban(
                            discord.Object(id=user_id),
                            reason=f"{reason} | {author} ({author.id})",
                            delete_message_days=0,
                        )
                        if self.bot.db:
                            await self.bot.db.log_action(
                                guild.id, "ban", user_id, author.id, reason
                            )
                        completed += 1
                    except Exception:
                        log.exception("AI failed to ban user %s", user_id)
                return f"Banned {completed}/{len(ids)} member(s)."

            if name == "kick_members":
                if not author.guild_permissions.kick_members:
                    return "Denied: missing kick_members permission."
                ids = [int(value) for value in args.get("user_ids", [])][:20]
                completed = 0
                for user_id in ids:
                    member = guild.get_member(user_id)
                    if not member:
                        continue
                    if not self._can_act_on(author, member):
                        continue
                    try:
                        await member.kick(reason=f"{reason} | {author} ({author.id})")
                        if self.bot.db:
                            await self.bot.db.log_action(
                                guild.id, "kick", user_id, author.id, reason
                            )
                        completed += 1
                    except Exception:
                        log.exception("AI failed to kick user %s", user_id)
                return f"Kicked {completed}/{len(ids)} member(s)."

            if name == "timeout_member":
                if not author.guild_permissions.moderate_members:
                    return "Denied: missing moderate_members permission."
                member = guild.get_member(int(args["user_id"]))
                if not member:
                    return "Member not found."
                if not self._can_act_on(author, member):
                    return "Denied: the target is not below your highest role."
                minutes = max(1, min(int(args.get("duration_minutes", 60)), 40320))
                until = discord.utils.utcnow() + datetime.timedelta(minutes=minutes)
                await member.timeout(until, reason=reason)
                return f"Timed out {member.display_name} for {minutes} minute(s)."

            if name == "timeout_remove":
                if not author.guild_permissions.moderate_members:
                    return "Denied: missing moderate_members permission."
                member = guild.get_member(int(args["user_id"]))
                if not member:
                    return "Member not found."
                if not self._can_act_on(author, member):
                    return "Denied: the target is not below your highest role."
                await member.timeout(None, reason=reason)
                return f"Removed timeout from {member.display_name}."

            if name == "warn_member":
                if not author.guild_permissions.kick_members:
                    return "Denied: missing kick_members permission."
                member = guild.get_member(int(args["user_id"]))
                if not member:
                    return "Member not found."
                if not self.bot.db:
                    return "Warning database is unavailable."
                warning_id = await self.bot.db.add_warning(
                    member.id, guild.id, author.id, reason
                )
                return f"Issued warning #{warning_id} to {member.display_name}."

            if name == "purge_messages":
                if not author.guild_permissions.manage_messages:
                    return "Denied: missing manage_messages permission."
                count = max(1, min(int(args.get("count", 1)), 1000))
                deleted = await ctx.channel.purge(limit=count + 1)
                return f"Deleted {max(0, len(deleted) - 1)} message(s)."

            if name in {"lock_channel", "unlock_channel"}:
                if not author.guild_permissions.manage_channels:
                    return "Denied: missing manage_channels permission."
                overwrite = ctx.channel.overwrites_for(guild.default_role)
                overwrite.send_messages = False if name == "lock_channel" else None
                await ctx.channel.set_permissions(
                    guild.default_role,
                    overwrite=overwrite,
                    reason=reason,
                )
                return "Channel locked." if name == "lock_channel" else "Channel unlocked."

            if name == "slowmode":
                if not author.guild_permissions.manage_channels:
                    return "Denied: missing manage_channels permission."
                seconds = max(0, min(int(args.get("seconds", 0)), 21600))
                await ctx.channel.edit(slowmode_delay=seconds)
                return f"Slowmode set to {seconds} second(s)."

            return f"Moderation capability {name!r} is unavailable."

        except discord.Forbidden:
            return "The bot lacks permission to complete that action."
        except Exception as exc:
            log.exception("AI moderation capability failed: %s", name)
            return f"Action failed: {type(exc).__name__}: {exc}"[:1000]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AgentCore(bot))
