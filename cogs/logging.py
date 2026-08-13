"""
Detailed event logging cog.

Three things drive the design here:

1. Every event used to cost a database read to find the log channel. Voice
   updates alone fire constantly, so channel IDs are cached per guild.
2. A raid or a mass-role-change generates hundreds of embeds at once. Sending
   them individually gets the whole bot rate-limited, so everything goes
   through a per-channel queue that batches up to 10 embeds per message and
   drops (with a notice) rather than growing without bound.
3. Plain events never tell you *who* did something. Bans, kicks, deletes, and
   role changes are resolved against the audit log so the actor is named.
"""
from __future__ import annotations

import asyncio
import datetime
import logging
from collections import defaultdict

import discord
from discord.ext import commands

import config
from utils.helpers import error_embed, info_embed, success_embed

log = logging.getLogger("cogs.logging")

FIELD_MAX = 1024
EMBEDS_PER_MESSAGE = 10
COLLECT_WINDOW_SECONDS = 1.5
QUEUE_MAX = 400
AUDIT_DELAY_SECONDS = 1.5
AUDIT_MAX_AGE_SECONDS = 20

# Everything an admin can turn off individually.
EVENT_KEYS = {
    "message_edit": "Message edits",
    "message_delete": "Message deletions",
    "bulk_delete": "Bulk deletions",
    "member_join": "Members joining",
    "member_leave": "Members leaving or kicked",
    "member_update": "Nickname and role changes",
    "member_timeout": "Timeouts applied and lifted",
    "ban": "Bans and unbans",
    "voice": "Voice channel joins, leaves, moves",
    "channel": "Channel create, delete, update",
    "role": "Role create, delete, update",
    "guild": "Server setting changes",
    "invite": "Invite create and delete",
    "emoji": "Emoji changes",
    "sticker": "Sticker changes",
}

MOD_EVENTS = {"ban", "member_leave", "member_timeout"}


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def _clip(value: str, limit: int = FIELD_MAX) -> str:
    value = value or ""
    if len(value) <= limit:
        return value or "*empty*"
    return value[: limit - 1] + "…"


def _add_list_field(embed: discord.Embed, name: str, items: list[str], sep: str = " "):
    """Split a long list of mentions across as many fields as it takes."""
    if not items:
        return
    blocks: list[str] = []
    current = ""
    for item in items:
        candidate = f"{current}{sep}{item}" if current else item
        if len(candidate) > FIELD_MAX:
            blocks.append(current)
            current = item
        else:
            current = candidate
    if current:
        blocks.append(current)
    for index, block in enumerate(blocks[:5]):
        embed.add_field(name=name if index == 0 else f"{name} (cont.)", value=block, inline=False)


class LogDispatcher:
    """Per-channel queue. Batches bursts, drops overflow instead of dying."""

    def __init__(self, bot):
        self.bot = bot
        self._queues: dict[int, asyncio.Queue] = {}
        self._workers: dict[int, asyncio.Task] = {}
        self._dropped: dict[int, int] = defaultdict(int)

    def submit(self, channel_id: int, embed: discord.Embed):
        queue = self._queues.get(channel_id)
        if queue is None:
            queue = asyncio.Queue(maxsize=QUEUE_MAX)
            self._queues[channel_id] = queue
            self._workers[channel_id] = asyncio.create_task(self._worker(channel_id, queue))
        try:
            queue.put_nowait(embed)
        except asyncio.QueueFull:
            self._dropped[channel_id] += 1

    async def _worker(self, channel_id: int, queue: asyncio.Queue):
        while True:
            try:
                first = await queue.get()
                # Give a burst a moment to arrive so we can batch it.
                await asyncio.sleep(COLLECT_WINDOW_SECONDS)

                batch = [first]
                while len(batch) < EMBEDS_PER_MESSAGE:
                    try:
                        batch.append(queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                dropped = self._dropped.pop(channel_id, 0)
                if dropped and len(batch) < EMBEDS_PER_MESSAGE:
                    notice = discord.Embed(
                        description=f"⚠️ `{dropped}` further event(s) were dropped — too many at once.",
                        color=discord.Color.dark_orange(),
                    )
                    batch.append(notice)

                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    continue
                try:
                    await channel.send(embeds=batch)
                except discord.Forbidden:
                    log.warning("No permission to post logs in %s — dropping queue.", channel_id)
                except discord.HTTPException as exc:
                    log.warning("Log send failed in %s: %s", channel_id, exc)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.error("Log worker for %s hiccupped: %s", channel_id, exc, exc_info=True)
                await asyncio.sleep(5)

    def shutdown(self):
        for task in self._workers.values():
            task.cancel()
        self._workers.clear()
        self._queues.clear()


class Logging(commands.Cog):
    """📋 Detailed server event logging."""

    def __init__(self, bot):
        self.bot = bot
        self.dispatcher = LogDispatcher(bot)
        # guild_id -> {"log_channel": id|None, "mod_channel": id|None}
        self._channels: dict[int, dict[str, int | None]] = {}
        # guild_id -> set of disabled event keys
        self._disabled: dict[int, set[str]] = {}

    async def cog_unload(self):
        self.dispatcher.shutdown()

    # ── Caches ────────────────────────────────────────────────────────────────

    async def _channel_ids(self, guild_id: int) -> dict[str, int | None]:
        if guild_id not in self._channels:
            row = await self.bot.db.get_guild(guild_id)
            self._channels[guild_id] = {
                "log_channel": row["log_channel"] if row else None,
                "mod_channel": row["mod_channel"] if row else None,
            }
        return self._channels[guild_id]

    async def _is_enabled(self, guild_id: int, key: str) -> bool:
        if guild_id not in self._disabled:
            try:
                rows = await self.bot.db.get_disabled_log_events(guild_id)
                self._disabled[guild_id] = {r["event_key"] for r in rows}
            except Exception as exc:
                log.error("Could not load log settings for %s: %s", guild_id, exc)
                self._disabled[guild_id] = set()
        return key not in self._disabled[guild_id]

    # ── Emit ──────────────────────────────────────────────────────────────────

    async def _emit(self, guild: discord.Guild, key: str, embed: discord.Embed):
        """Route one event to whichever channels are configured for it."""
        if guild is None or not await self._is_enabled(guild.id, key):
            return

        ids = await self._channel_ids(guild.id)
        general = ids.get("log_channel")
        mod = ids.get("mod_channel")

        targets: list[int] = []
        if key in MOD_EVENTS and mod:
            targets.append(mod)
        if general and general not in targets:
            targets.append(general)

        for channel_id in targets:
            self.dispatcher.submit(channel_id, embed)

    def _embed(self, color: discord.Color, title: str) -> discord.Embed:
        return discord.Embed(title=title, color=color, timestamp=_utcnow())

    # ── Audit log ─────────────────────────────────────────────────────────────

    async def _audit(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: int | None = None,
    ) -> discord.AuditLogEntry | None:
        """
        Find who did it. Audit entries land slightly after the gateway event,
        so we wait before looking, and ignore anything stale in case the same
        action happened minutes ago.
        """
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None

        await asyncio.sleep(AUDIT_DELAY_SECONDS)
        try:
            async for entry in guild.audit_logs(limit=6, action=action):
                age = (_utcnow() - entry.created_at).total_seconds()
                if age > AUDIT_MAX_AGE_SECONDS:
                    break
                if target_id is not None and getattr(entry.target, "id", None) != target_id:
                    continue
                return entry
        except discord.Forbidden:
            return None
        except discord.HTTPException as exc:
            log.debug("Audit lookup failed in %s: %s", guild.id, exc)
        return None

    @staticmethod
    def _attach_actor(embed: discord.Embed, entry: discord.AuditLogEntry | None):
        if entry is None:
            embed.add_field(name="Performed by", value="Unknown — I can't read the audit log", inline=False)
            return
        actor = entry.user
        value = f"{actor.mention} (`{actor.id}`)" if actor else "Unknown"
        embed.add_field(name="Performed by", value=value, inline=False)
        if entry.reason:
            embed.add_field(name="Reason", value=_clip(entry.reason, 500), inline=False)

    # ── Setup commands ────────────────────────────────────────────────────────

    async def _set_channel(self, ctx: commands.Context, field: str, channel: discord.TextChannel, label: str):
        perms = channel.permissions_for(ctx.guild.me)
        missing = [
            name for name, ok in (
                ("View Channel", perms.view_channel),
                ("Send Messages", perms.send_messages),
                ("Embed Links", perms.embed_links),
            ) if not ok
        ]
        if missing:
            return await ctx.send(embed=error_embed(
                f"I can't log to {channel.mention} — missing: {', '.join(missing)}."
            ))

        await self.bot.db.set_guild_field(ctx.guild.id, field, channel.id)
        self._channels.setdefault(ctx.guild.id, {})[field] = channel.id
        await ctx.send(embed=success_embed(f"{label} set to {channel.mention}."))

    @commands.command(name="setlogchannel", usage="setlogchannel <channel>")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def setlogchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the general log channel."""
        await self._set_channel(ctx, "log_channel", channel, "Log channel")

    @commands.command(name="setmodlog", usage="setmodlog <channel>")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def setmodlog(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the moderation log channel. Bans, kicks, and timeouts go here."""
        await self._set_channel(ctx, "mod_channel", channel, "Mod log channel")

    @commands.command(name="unsetlog", aliases=["unsetlogchannel"], usage="unsetlog")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def unsetlog(self, ctx: commands.Context):
        """Stop logging. Clears the log channel; event toggles are kept."""
        await self.bot.db.set_guild_field(ctx.guild.id, "log_channel", None)
        self._channels.setdefault(ctx.guild.id, {})["log_channel"] = None
        await ctx.send(embed=success_embed(
            "Logging disabled — no channel is set. Your per-event toggles are remembered."
        ))

    @commands.command(name="unsetmodlog", usage="unsetmodlog")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def unsetmodlog(self, ctx: commands.Context):
        """
        Clear the mod log channel.

        Note that moderation.py and the security cog also write here — once
        this is unset they fall back to the general log channel, or go
        nowhere if that isn't set either.
        """
        await self.bot.db.set_guild_field(ctx.guild.id, "mod_channel", None)
        self._channels.setdefault(ctx.guild.id, {})["mod_channel"] = None
        await ctx.send(embed=success_embed(
            "Mod log cleared. Moderation and security alerts will fall back to the general log channel."
        ))

    @commands.command(name="logevents", usage="logevents")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def logevents(self, ctx: commands.Context):
        """Show which event types are being logged."""
        await self._is_enabled(ctx.guild.id, "message_edit")  # warm the cache
        disabled = self._disabled.get(ctx.guild.id, set())
        lines = [
            f"{'❌' if key in disabled else '✅'} `{key}` — {label}"
            for key, label in EVENT_KEYS.items()
        ]
        e = discord.Embed(
            title="📋 Logged events",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        e.set_footer(text="Toggle with logtoggle <key>")
        await ctx.send(embed=e)

    @commands.command(name="logtoggle", usage="logtoggle <event>")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def logtoggle(self, ctx: commands.Context, event: str):
        """Turn one event type on or off."""
        key = event.strip().lower()
        if key not in EVENT_KEYS:
            return await ctx.send(embed=error_embed(
                f"Unknown event `{key}`. Run `logevents` to see the list."
            ))

        await self._is_enabled(ctx.guild.id, key)
        disabled = self._disabled[ctx.guild.id]
        now_enabled = key in disabled

        await self.bot.db.set_log_event(ctx.guild.id, key, now_enabled)
        if now_enabled:
            disabled.discard(key)
        else:
            disabled.add(key)

        state = "enabled" if now_enabled else "disabled"
        await ctx.send(embed=success_embed(f"`{key}` logging **{state}**."))

    # ── Message events (raw, so uncached messages still log) ──────────────────

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        if not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        before = payload.cached_message
        if before and before.author.bot:
            return

        after_content = (payload.data or {}).get("content")
        if before is not None and after_content is not None and before.content == after_content:
            return

        channel = guild.get_channel(payload.channel_id)
        e = self._embed(discord.Color.yellow(), "✏️ Message Edited")

        if before:
            e.set_author(name=str(before.author), icon_url=before.author.display_avatar.url)
            e.add_field(name="Author", value=before.author.mention, inline=True)
            e.set_footer(text=f"User ID: {before.author.id}")
        else:
            e.add_field(name="Author", value="Not cached", inline=True)
            e.set_footer(text=f"Message ID: {payload.message_id}")

        e.add_field(name="Channel", value=channel.mention if channel else f"`{payload.channel_id}`", inline=True)

        if config.LOG_MESSAGE_CONTENT:
            if before:
                e.add_field(name="Before", value=_clip(before.content), inline=False)
            e.add_field(name="After", value=_clip(after_content or ""), inline=False)

        jump = f"https://discord.com/channels/{payload.guild_id}/{payload.channel_id}/{payload.message_id}"
        e.add_field(name="Jump", value=f"[Link]({jump})", inline=True)
        await self._emit(guild, "message_edit", e)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        message = payload.cached_message
        if message and message.author.bot:
            return

        channel = guild.get_channel(payload.channel_id)
        e = self._embed(discord.Color.red(), "🗑️ Message Deleted")

        target_id = message.author.id if message else None
        if message:
            e.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
            e.add_field(name="Author", value=message.author.mention, inline=True)
        else:
            e.add_field(name="Author", value="Not cached", inline=True)

        e.add_field(name="Channel", value=channel.mention if channel else f"`{payload.channel_id}`", inline=True)

        if message and config.LOG_MESSAGE_CONTENT:
            e.add_field(name="Content", value=_clip(message.content), inline=False)
        if message and message.attachments and config.LOG_ATTACHMENT_NAMES:
            _add_list_field(e, "Attachments", [a.filename for a in message.attachments], sep="\n")

        e.set_footer(text=f"Message ID: {payload.message_id}")

        entry = await self._audit(guild, discord.AuditLogAction.message_delete, target_id)
        if entry and entry.user and (not message or entry.user.id != message.author.id):
            self._attach_actor(e, entry)

        await self._emit(guild, "message_delete", e)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        if not payload.guild_id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        channel = guild.get_channel(payload.channel_id)
        e = self._embed(discord.Color.red(), "🗑️ Bulk Delete")
        e.add_field(name="Channel", value=channel.mention if channel else f"`{payload.channel_id}`")
        e.add_field(name="Count", value=str(len(payload.message_ids)))

        cached = [m for m in payload.cached_messages if not m.author.bot]
        if cached and config.LOG_MESSAGE_CONTENT:
            preview = "\n".join(f"**{m.author}**: {_clip(m.content, 60)}" for m in cached[:10])
            e.add_field(name="Preview (up to 10 cached)", value=_clip(preview), inline=False)

        entry = await self._audit(guild, discord.AuditLogAction.message_bulk_delete)
        self._attach_actor(e, entry)
        await self._emit(guild, "bulk_delete", e)

    # ── Member events ─────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        e = self._embed(discord.Color.green(), "📥 Member Joined")
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        e.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=False)
        e.add_field(name="Account Created", value=discord.utils.format_dt(member.created_at, "R"))
        e.add_field(name="Member Count", value=str(member.guild.member_count or 0))
        e.set_thumbnail(url=member.display_avatar.url)
        await self._emit(member.guild, "member_join", e)
        await self._apply_autorole(member)

    async def _apply_autorole(self, member: discord.Member):
        try:
            row = await self.bot.db.get_guild(member.guild.id)
        except Exception as exc:
            return log.error("Autorole lookup failed for %s: %s", member.guild.id, exc)
        if not row or not row["autorole"]:
            return

        role = member.guild.get_role(row["autorole"])
        if role is None:
            return
        if member.guild.me.top_role <= role:
            return log.warning(
                "Autorole %s in guild %s is above my top role — can't apply it.",
                role.id, member.guild.id,
            )
        try:
            await member.add_roles(role, reason="Autorole")
        except discord.Forbidden:
            log.warning("Missing Manage Roles for autorole in guild %s.", member.guild.id)
        except discord.HTTPException as exc:
            log.warning("Autorole failed in guild %s: %s", member.guild.id, exc)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        kick = await self._audit(member.guild, discord.AuditLogAction.kick, member.id)

        if kick:
            e = self._embed(discord.Color.red(), "👢 Member Kicked")
        else:
            e = self._embed(discord.Color.orange(), "📤 Member Left")

        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        e.add_field(name="User", value=f"{member.mention} (`{member.id}`)", inline=False)
        roles = [r.mention for r in member.roles if r != member.guild.default_role]
        _add_list_field(e, "Roles", roles or ["None"])
        e.add_field(
            name="Joined",
            value=discord.utils.format_dt(member.joined_at, "R") if member.joined_at else "Unknown",
        )
        e.set_thumbnail(url=member.display_avatar.url)
        if kick:
            self._attach_actor(e, kick)
        await self._emit(member.guild, "member_leave", e)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.timed_out_until != after.timed_out_until:
            await self._log_timeout(before, after)

        changes: list[str] = []
        if before.nick != after.nick:
            changes.append(f"**Nickname:** `{before.nick or 'none'}` → `{after.nick or 'none'}`")

        added = [r for r in after.roles if r not in before.roles]
        removed = [r for r in before.roles if r not in after.roles]
        if not changes and not added and not removed:
            return

        e = self._embed(discord.Color.blurple(), "👤 Member Updated")
        e.set_author(name=str(after), icon_url=after.display_avatar.url)
        e.add_field(name="User", value=f"{after.mention} (`{after.id}`)", inline=False)
        if changes:
            e.add_field(name="Changes", value=_clip("\n".join(changes)), inline=False)
        _add_list_field(e, "Roles Added", [r.mention for r in added], sep=", ")
        _add_list_field(e, "Roles Removed", [r.mention for r in removed], sep=", ")

        action = (
            discord.AuditLogAction.member_role_update if (added or removed)
            else discord.AuditLogAction.member_update
        )
        entry = await self._audit(after.guild, action, after.id)
        if entry and entry.user and entry.user.id != after.id:
            self._attach_actor(e, entry)

        await self._emit(after.guild, "member_update", e)

    async def _log_timeout(self, before: discord.Member, after: discord.Member):
        until = after.timed_out_until
        if until and until > _utcnow():
            e = self._embed(discord.Color.orange(), "🔇 Member Timed Out")
            e.add_field(name="Until", value=discord.utils.format_dt(until, "R"), inline=False)
        else:
            e = self._embed(discord.Color.green(), "🔈 Timeout Lifted")

        e.set_author(name=str(after), icon_url=after.display_avatar.url)
        e.add_field(name="User", value=f"{after.mention} (`{after.id}`)", inline=False)
        entry = await self._audit(after.guild, discord.AuditLogAction.member_update, after.id)
        self._attach_actor(e, entry)
        await self._emit(after.guild, "member_timeout", e)

    # ── Ban / Unban ───────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        e = self._embed(discord.Color.red(), "🔨 Member Banned")
        e.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=False)
        e.set_thumbnail(url=user.display_avatar.url)
        entry = await self._audit(guild, discord.AuditLogAction.ban, user.id)
        self._attach_actor(e, entry)
        await self._emit(guild, "ban", e)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        e = self._embed(discord.Color.green(), "✅ Member Unbanned")
        e.add_field(name="User", value=f"{user.mention} (`{user.id}`)", inline=False)
        e.set_thumbnail(url=user.display_avatar.url)
        entry = await self._audit(guild, discord.AuditLogAction.unban, user.id)
        self._attach_actor(e, entry)
        await self._emit(guild, "ban", e)

    # ── Voice ─────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before: discord.VoiceState, after: discord.VoiceState):
        if before.channel == after.channel:
            return

        e = self._embed(discord.Color.blurple(), "🔊 Voice Update")
        e.set_author(name=str(member), icon_url=member.display_avatar.url)
        e.add_field(name="User", value=member.mention)

        if before.channel is None:
            e.add_field(name="Action", value="Joined")
            e.add_field(name="Channel", value=after.channel.mention)
        elif after.channel is None:
            e.add_field(name="Action", value="Left")
            e.add_field(name="Channel", value=before.channel.mention)
        else:
            e.add_field(name="Action", value="Moved")
            e.add_field(name="From", value=before.channel.mention)
            e.add_field(name="To", value=after.channel.mention)

        e.set_footer(text=f"User ID: {member.id}")
        await self._emit(member.guild, "voice", e)

    # ── Channels ──────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        e = self._embed(discord.Color.green(), "📁 Channel Created")
        e.add_field(name="Name", value=channel.mention)
        e.add_field(name="Type", value=str(channel.type).replace("_", " ").title())
        if getattr(channel, "category", None):
            e.add_field(name="Category", value=channel.category.name)
        entry = await self._audit(channel.guild, discord.AuditLogAction.channel_create, channel.id)
        self._attach_actor(e, entry)
        await self._emit(channel.guild, "channel", e)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        e = self._embed(discord.Color.red(), "📁 Channel Deleted")
        e.add_field(name="Name", value=f"#{channel.name}")
        e.add_field(name="Type", value=str(channel.type).replace("_", " ").title())
        entry = await self._audit(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
        self._attach_actor(e, entry)
        await self._emit(channel.guild, "channel", e)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        e = self._embed(discord.Color.yellow(), "📁 Channel Updated")
        found = False

        if before.name != after.name:
            e.add_field(name="Name", value=f"`{before.name}` → `{after.name}`", inline=False)
            found = True
        if getattr(before, "topic", None) != getattr(after, "topic", None):
            e.add_field(name="Topic before", value=_clip(getattr(before, "topic", "") or "*none*", 500), inline=False)
            e.add_field(name="Topic after", value=_clip(getattr(after, "topic", "") or "*none*", 500), inline=False)
            found = True
        if getattr(before, "slowmode_delay", None) != getattr(after, "slowmode_delay", None):
            e.add_field(
                name="Slowmode",
                value=f"`{before.slowmode_delay}s` → `{after.slowmode_delay}s`",
                inline=False,
            )
            found = True
        if not found:
            return

        e.insert_field_at(0, name="Channel", value=after.mention, inline=False)
        entry = await self._audit(after.guild, discord.AuditLogAction.channel_update, after.id)
        self._attach_actor(e, entry)
        await self._emit(after.guild, "channel", e)

    # ── Roles ─────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        e = self._embed(discord.Color.green(), "🏷️ Role Created")
        e.add_field(name="Name", value=role.mention)
        e.add_field(name="Color", value=str(role.color))
        entry = await self._audit(role.guild, discord.AuditLogAction.role_create, role.id)
        self._attach_actor(e, entry)
        await self._emit(role.guild, "role", e)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        e = self._embed(discord.Color.red(), "🏷️ Role Deleted")
        e.add_field(name="Name", value=f"@{role.name}")
        e.add_field(name="Color", value=str(role.color))
        entry = await self._audit(role.guild, discord.AuditLogAction.role_delete, role.id)
        self._attach_actor(e, entry)
        await self._emit(role.guild, "role", e)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes: list[str] = []
        if before.name != after.name:
            changes.append(f"**Name:** `{before.name}` → `{after.name}`")
        if before.color != after.color:
            changes.append(f"**Color:** `{before.color}` → `{after.color}`")
        if before.hoist != after.hoist:
            changes.append(f"**Hoisted:** `{before.hoist}` → `{after.hoist}`")
        if before.mentionable != after.mentionable:
            changes.append(f"**Mentionable:** `{before.mentionable}` → `{after.mentionable}`")

        if before.permissions != after.permissions:
            gained = [
                name.replace("_", " ").title()
                for name, value in after.permissions
                if value and not getattr(before.permissions, name)
            ]
            lost = [
                name.replace("_", " ").title()
                for name, value in before.permissions
                if value and not getattr(after.permissions, name)
            ]
            if gained:
                changes.append(f"**Granted:** {_clip(', '.join(gained), 400)}")
            if lost:
                changes.append(f"**Revoked:** {_clip(', '.join(lost), 400)}")

        if not changes:
            return

        e = self._embed(discord.Color.yellow(), "🏷️ Role Updated")
        e.add_field(name="Role", value=after.mention, inline=False)
        e.add_field(name="Changes", value=_clip("\n".join(changes)), inline=False)
        entry = await self._audit(after.guild, discord.AuditLogAction.role_update, after.id)
        self._attach_actor(e, entry)
        await self._emit(after.guild, "role", e)

    # ── Guild ─────────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        changes: list[str] = []
        if before.name != after.name:
            changes.append(f"**Name:** `{before.name}` → `{after.name}`")
        if before.icon != after.icon:
            changes.append("**Icon changed**")
        if before.owner_id != after.owner_id:
            changes.append(f"**Owner:** `{before.owner_id}` → `{after.owner_id}`")
        if before.verification_level != after.verification_level:
            changes.append(f"**Verification:** `{before.verification_level}` → `{after.verification_level}`")
        if not changes:
            return

        e = self._embed(discord.Color.blurple(), "🛡️ Server Updated")
        e.add_field(name="Changes", value=_clip("\n".join(changes)), inline=False)
        entry = await self._audit(after, discord.AuditLogAction.guild_update)
        self._attach_actor(e, entry)
        await self._emit(after, "guild", e)

    # ── Invites ───────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        e = self._embed(discord.Color.green(), "🔗 Invite Created")
        e.add_field(name="Inviter", value=invite.inviter.mention if invite.inviter else "Unknown")
        e.add_field(name="Code", value=f"`{invite.code}`")
        e.add_field(name="Channel", value=invite.channel.mention if invite.channel else "Unknown")
        e.add_field(name="Max Uses", value=str(invite.max_uses or "∞"))
        e.add_field(
            name="Expires",
            value=discord.utils.format_dt(invite.expires_at, "R") if invite.expires_at else "Never",
        )
        await self._emit(invite.guild, "invite", e)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        e = self._embed(discord.Color.red(), "🔗 Invite Deleted")
        e.add_field(name="Code", value=f"`{invite.code}`")
        entry = await self._audit(invite.guild, discord.AuditLogAction.invite_delete)
        self._attach_actor(e, entry)
        await self._emit(invite.guild, "invite", e)

    # ── Emoji & stickers ──────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before: list, after: list):
        before_ids = {em.id for em in before}
        after_ids = {em.id for em in after}
        added = [em for em in after if em.id not in before_ids]
        removed = [em for em in before if em.id not in after_ids]

        if added:
            e = self._embed(discord.Color.green(), "😀 Emoji Added")
            _add_list_field(e, "Emoji", [f"{em} `{em.name}`" for em in added[:20]])
            await self._emit(guild, "emoji", e)

        if removed:
            e = self._embed(discord.Color.red(), "😀 Emoji Removed")
            _add_list_field(e, "Emoji", [f"`{em.name}`" for em in removed[:20]], sep=", ")
            await self._emit(guild, "emoji", e)

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild: discord.Guild, before: list, after: list):
        before_ids = {s.id for s in before}
        after_ids = {s.id for s in after}
        added = [s for s in after if s.id not in before_ids]
        removed = [s for s in before if s.id not in after_ids]

        if added:
            e = self._embed(discord.Color.green(), "🏷️ Sticker Added")
            _add_list_field(e, "Stickers", [f"`{s.name}`" for s in added[:20]], sep=", ")
            await self._emit(guild, "sticker", e)

        if removed:
            e = self._embed(discord.Color.red(), "🏷️ Sticker Removed")
            _add_list_field(e, "Stickers", [f"`{s.name}`" for s in removed[:20]], sep=", ")
            await self._emit(guild, "sticker", e)


async def setup(bot):
    await bot.add_cog(Logging(bot))
