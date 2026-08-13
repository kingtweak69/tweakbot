"""
Moderation cog — ban, kick, jail, timeout, mute, deafen, warn, purge, lockdown, etc.
"""
import asyncio
import datetime
import json
import logging

import discord
from discord.ext import commands

from utils.helpers import (
    error_embed, success_embed, info_embed, mod_embed, parse_duration,
    humanize_duration, format_dt
)

log = logging.getLogger("cogs.moderation")

MAX_AUDIT_REASON = 512


def _reason(ctx: commands.Context, reason: str | None) -> str:
    text = reason or "No reason provided"
    tag = f" | {ctx.author} ({ctx.author.id})"
    return (text + tag)[:MAX_AUDIT_REASON]


async def _send_dm(user: discord.Member, embed: discord.Embed):
    try:
        await user.send(embed=embed)
    except Exception:
        pass


class Moderation(commands.Cog):
    """🔨 Complete moderation suite."""

    def __init__(self, bot):
        self.bot = bot

    async def _log_mod(self, guild: discord.Guild, embed: discord.Embed):
        """Send an embed to the guild's mod-log channel if configured."""
        row = await self.bot.db.get_guild(guild.id)
        if not row or not row["mod_channel"]:
            return
        ch = guild.get_channel(row["mod_channel"])
        if ch:
            try:
                await ch.send(embed=embed)
            except Exception:
                pass

    # ── Ban ────────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="ban", usage="ban <member1> [member2 ...] [reason]")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def ban(self, ctx: commands.Context, members: commands.Greedy[discord.Member], *, reason: str = None):
        """Permanently ban up to 20 members at once. Mention or provide IDs space-separated."""
        if not members:
            return await ctx.send(embed=error_embed("Provide at least one member to ban."))
        if len(members) > 20:
            return await ctx.send(embed=error_embed("Cannot ban more than 20 members at once."))

        valid, skipped = [], []
        for m in members:
            if m == ctx.author:
                skipped.append((m, "cannot ban yourself"))
                continue
            if m.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
                skipped.append((m, "equal or higher role"))
                continue
            valid.append(m)

        if not valid:
            return await ctx.send(embed=error_embed("No valid members to ban."))

        # DM all targets concurrently
        dm_e = discord.Embed(
            title="You have been banned",
            description=f"You have been **banned** from **{ctx.guild.name}**.",
            color=discord.Color.red(),
            timestamp=datetime.datetime.utcnow(),
        )
        dm_e.add_field(name="Reason",    value=reason or "No reason provided")
        dm_e.add_field(name="Moderator", value=str(ctx.author))
        await asyncio.gather(*[_send_dm(m, dm_e) for m in valid], return_exceptions=True)

        # Ban all concurrently
        results = await asyncio.gather(*[
            m.ban(reason=_reason(ctx, reason), delete_message_days=0) for m in valid
        ], return_exceptions=True)

        banned = [m for m, r in zip(valid, results) if not isinstance(r, Exception)]
        failed = [m for m, r in zip(valid, results) if isinstance(r, Exception)]

        for m in banned:
            await self.bot.db.log_action(ctx.guild.id, "ban", m.id, ctx.author.id, reason)

        # Single-target path — clean embed
        if len(banned) == 1 and not failed and not skipped:
            e = mod_embed("Member Banned", banned[0], ctx.author, reason, discord.Color.red())
            await ctx.send(embed=e)
            await self._log_mod(ctx.guild, e)
            return

        # Multi-target summary
        e = discord.Embed(title="🔨 Ban", color=discord.Color.red(), timestamp=datetime.datetime.utcnow())
        if banned:
            e.add_field(
                name=f"✅ Banned ({len(banned)})",
                value="\n".join(f"{m.mention} `{m.id}`" for m in banned),
                inline=False,
            )
        if failed:
            e.add_field(
                name=f"❌ Failed ({len(failed)})",
                value="\n".join(m.mention for m in failed),
                inline=False,
            )
        if skipped:
            e.add_field(
                name=f"⏭️ Skipped ({len(skipped)})",
                value="\n".join(f"{m.mention}: {r}" for m, r in skipped),
                inline=False,
            )
        e.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        e.add_field(name="Reason",    value=reason or "No reason provided", inline=True)
        await ctx.send(embed=e)
        await self._log_mod(ctx.guild, e)

    @commands.hybrid_command(name="unban", usage="unban <user_id> [reason]")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def unban(self, ctx: commands.Context, user_id: int, *, reason: str = None):
        """Unban a user by ID."""
        try:
            user = await self.bot.fetch_user(user_id)
        except discord.NotFound:
            return await ctx.send(embed=error_embed(f"No user found with ID `{user_id}`."))

        try:
            await ctx.guild.unban(user, reason=_reason(ctx, reason))
        except discord.NotFound:
            return await ctx.send(embed=error_embed(f"{user} is not banned."))

        await self.bot.db.log_action(ctx.guild.id, "unban", user.id, ctx.author.id, reason)
        e = discord.Embed(
            title="✅ Member Unbanned",
            description=f"**{user}** (`{user.id}`) has been unbanned.",
            color=discord.Color.green(),
            timestamp=datetime.datetime.utcnow()
        )
        e.add_field(name="Moderator", value=ctx.author.mention)
        e.add_field(name="Reason", value=reason or "No reason provided")
        await ctx.send(embed=e)
        await self._log_mod(ctx.guild, e)

    @commands.command(name="massban", usage="massban <id1> <id2> ... [reason]")
    @commands.guild_only()
    @commands.has_permissions(ban_members=True)
    @commands.bot_has_permissions(ban_members=True)
    async def massban(self, ctx: commands.Context, *args):
        """Ban multiple users by ID. Separate IDs with spaces; optionally end with a text reason."""
        ids = []
        reason_parts = []
        for arg in args:
            if arg.isdigit():
                ids.append(int(arg))
            else:
                reason_parts.append(arg)
        reason = " ".join(reason_parts) or None

        if not ids:
            return await ctx.send(embed=error_embed("Provide at least one user ID."))

        banned, failed = [], []
        for uid in ids:
            try:
                await ctx.guild.ban(discord.Object(id=uid), reason=_reason(ctx, reason), delete_message_days=0)
                banned.append(uid)
            except Exception:
                failed.append(uid)

        e = discord.Embed(title="🔨 Mass Ban", color=discord.Color.red(), timestamp=datetime.datetime.utcnow())
        e.add_field(name="Banned", value="\n".join(f"`{i}`" for i in banned) or "None", inline=True)
        if failed:
            e.add_field(name="Failed", value="\n".join(f"`{i}`" for i in failed), inline=True)
        e.add_field(name="Moderator", value=ctx.author.mention, inline=False)
        e.add_field(name="Reason", value=reason or "No reason provided")
        await ctx.send(embed=e)
        await self._log_mod(ctx.guild, e)

    # ── Kick ───────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="kick", usage="kick <member1> [member2 ...] [reason]")
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    @commands.bot_has_permissions(kick_members=True)
    async def kick(self, ctx: commands.Context, members: commands.Greedy[discord.Member], *, reason: str = None):
        """Kick up to 20 members at once. Mention or provide IDs space-separated."""
        if not members:
            return await ctx.send(embed=error_embed("Provide at least one member to kick."))
        if len(members) > 20:
            return await ctx.send(embed=error_embed("Cannot kick more than 20 members at once."))

        valid, skipped = [], []
        for m in members:
            if m == ctx.author:
                skipped.append((m, "cannot kick yourself"))
                continue
            if m.top_role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
                skipped.append((m, "equal or higher role"))
                continue
            valid.append(m)

        if not valid:
            return await ctx.send(embed=error_embed("No valid members to kick."))

        # DM all targets concurrently
        dm_e = discord.Embed(
            title="You have been kicked",
            description=f"You have been **kicked** from **{ctx.guild.name}**.",
            color=discord.Color.orange(),
            timestamp=datetime.datetime.utcnow(),
        )
        dm_e.add_field(name="Reason",    value=reason or "No reason provided")
        dm_e.add_field(name="Moderator", value=str(ctx.author))
        await asyncio.gather(*[_send_dm(m, dm_e) for m in valid], return_exceptions=True)

        # Kick all concurrently
        results = await asyncio.gather(*[
            m.kick(reason=_reason(ctx, reason)) for m in valid
        ], return_exceptions=True)

        kicked = [m for m, r in zip(valid, results) if not isinstance(r, Exception)]
        failed = [m for m, r in zip(valid, results) if isinstance(r, Exception)]

        for m in kicked:
            await self.bot.db.log_action(ctx.guild.id, "kick", m.id, ctx.author.id, reason)

        # Single-target path — clean embed
        if len(kicked) == 1 and not failed and not skipped:
            e = mod_embed("Member Kicked", kicked[0], ctx.author, reason, discord.Color.orange())
            await ctx.send(embed=e)
            await self._log_mod(ctx.guild, e)
            return

        # Multi-target summary
        e = discord.Embed(title="👢 Kick", color=discord.Color.orange(), timestamp=datetime.datetime.utcnow())
        if kicked:
            e.add_field(
                name=f"✅ Kicked ({len(kicked)})",
                value="\n".join(f"{m.mention} `{m.id}`" for m in kicked),
                inline=False,
            )
        if failed:
            e.add_field(
                name=f"❌ Failed ({len(failed)})",
                value="\n".join(m.mention for m in failed),
                inline=False,
            )
        if skipped:
            e.add_field(
                name=f"⏭️ Skipped ({len(skipped)})",
                value="\n".join(f"{m.mention}: {r}" for m, r in skipped),
                inline=False,
            )
        e.add_field(name="Moderator", value=ctx.author.mention, inline=True)
        e.add_field(name="Reason",    value=reason or "No reason provided", inline=True)
        await ctx.send(embed=e)
        await self._log_mod(ctx.guild, e)

    # ── Timeout ────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="timeout", aliases=["mute"], usage="timeout <member> <duration> [reason]")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def timeout(self, ctx: commands.Context, member: discord.Member, duration: str, *, reason: str = None):
        """Timeout (mute) a member. Duration: 1d2h30m format. Max 28 days."""
        td = parse_duration(duration)
        if not td:
            return await ctx.send(embed=error_embed("Invalid duration. Use format like `10m`, `1h30m`, `1d`."))
        if td.total_seconds() > 28 * 86400:
            return await ctx.send(embed=error_embed("Max timeout is 28 days."))

        until = discord.utils.utcnow() + td
        await member.timeout(until, reason=_reason(ctx, reason))
        await self.bot.db.log_action(ctx.guild.id, "timeout", member.id, ctx.author.id, reason)

        e = mod_embed("Member Timed Out", member, ctx.author, reason, discord.Color.yellow())
        e.add_field(name="Duration", value=humanize_duration(td))
        e.add_field(name="Until", value=discord.utils.format_dt(until, "R"))
        await ctx.send(embed=e)
        await self._log_mod(ctx.guild, e)

    @commands.hybrid_command(name="untimeout", aliases=["unmute"], usage="untimeout <member> [reason]")
    @commands.guild_only()
    @commands.has_permissions(moderate_members=True)
    @commands.bot_has_permissions(moderate_members=True)
    async def untimeout(self, ctx: commands.Context, member: discord.Member, *, reason: str = None):
        """Remove a member's timeout."""
        await member.timeout(None, reason=_reason(ctx, reason))
        e = mod_embed("Timeout Removed", member, ctx.author, reason, discord.Color.green())
        await ctx.send(embed=e)
        await self._log_mod(ctx.guild, e)

    # ── Server Mute / Deafen ───────────────────────────────────────────────────

    @commands.hybrid_command(name="voicemute", usage="voicemute <member> [reason]")
    @commands.guild_only()
    @commands.has_permissions(mute_members=True)
    @commands.bot_has_permissions(mute_members=True)
    async def voicemute(self, ctx: commands.Context, member: discord.Member, *, reason: str = None):
        """Server-mute a member in voice channels."""
        if not member.voice:
            return await ctx.send(embed=error_embed(f"{member.mention} is not in a voice channel."))
        await member.edit(mute=True, reason=_reason(ctx, reason))
        e = mod_embed("Voice Muted", member, ctx.author, reason, discord.Color.yellow())
        await ctx.send(embed=e)

    @commands.hybrid_command(name="voiceunmute", usage="voiceunmute <member>")
    @commands.guild_only()
    @commands.has_permissions(mute_members=True)
    @commands.bot_has_permissions(mute_members=True)
    async def voiceunmute(self, ctx: commands.Context, member: discord.Member, *, reason: str = None):
        """Remove server-mute from a member."""
        await member.edit(mute=False, reason=_reason(ctx, reason))
        e = mod_embed("Voice Unmuted", member, ctx.author, reason, discord.Color.green())
        await ctx.send(embed=e)

    @commands.hybrid_command(name="deafen", usage="deafen <member> [reason]")
    @commands.guild_only()
    @commands.has_permissions(deafen_members=True)
    @commands.bot_has_permissions(deafen_members=True)
    async def deafen(self, ctx: commands.Context, member: discord.Member, *, reason: str = None):
        """Server-deafen a member in voice channels."""
        if not member.voice:
            return await ctx.send(embed=error_embed(f"{member.mention} is not in a voice channel."))
        await member.edit(deafen=True, reason=_reason(ctx, reason))
        e = mod_embed("Server Deafened", member, ctx.author, reason, discord.Color.yellow())
        await ctx.send(embed=e)

    @commands.hybrid_command(name="undeafen", usage="undeafen <member>")
    @commands.guild_only()
    @commands.has_permissions(deafen_members=True)
    @commands.bot_has_permissions(deafen_members=True)
    async def undeafen(self, ctx: commands.Context, member: discord.Member, *, reason: str = None):
        """Remove server-deafen from a member."""
        await member.edit(deafen=False, reason=_reason(ctx, reason))
        e = mod_embed("Server Undeafened", member, ctx.author, reason, discord.Color.green())
        await ctx.send(embed=e)

    # ── Jail ───────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="jail", usage="jail <member> [reason]")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def jail(self, ctx: commands.Context, member: discord.Member, *, reason: str = None):
        """Jail a member: removes all roles and assigns the Jail role."""
        row = await self.bot.db.get_guild(ctx.guild.id)
        if not row or not row["jail_role"]:
            return await ctx.send(embed=error_embed(
                "No jail role configured. Set the `jail_role` guild field first."
            ))

        jail_role = ctx.guild.get_role(row["jail_role"])
        if not jail_role:
            return await ctx.send(embed=error_embed("Configured jail role no longer exists."))

        already = await self.bot.db.get_jailed(member.id, ctx.guild.id)
        if already:
            return await ctx.send(embed=error_embed(f"{member.mention} is already jailed."))

        saved_roles = [r.id for r in member.roles if r != ctx.guild.default_role]
        await self.bot.db.jail_user(member.id, ctx.guild.id, saved_roles, ctx.author.id, reason)

        try:
            await member.edit(
                roles=[jail_role],
                reason=_reason(ctx, reason)
            )
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("Missing permissions to modify this member's roles."))

        dm_embed = mod_embed("Jailed", member, ctx.author, reason, discord.Color.dark_orange())
        dm_embed.description = f"You have been **jailed** in **{ctx.guild.name}**."
        await _send_dm(member, dm_embed)

        e = mod_embed("Member Jailed", member, ctx.author, reason, discord.Color.dark_orange())
        e.add_field(name="Roles Saved", value=f"{len(saved_roles)} role(s)")
        await ctx.send(embed=e)
        await self._log_mod(ctx.guild, e)

    @commands.hybrid_command(name="unjail", usage="unjail <member> [reason]")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def unjail(self, ctx: commands.Context, member: discord.Member, *, reason: str = None):
        """Release a jailed member and restore their roles."""
        record = await self.bot.db.get_jailed(member.id, ctx.guild.id)
        if not record:
            return await ctx.send(embed=error_embed(f"{member.mention} is not jailed."))

        saved_ids = json.loads(record["roles"])
        restored = [ctx.guild.get_role(rid) for rid in saved_ids]
        restored = [r for r in restored if r is not None]

        row = await self.bot.db.get_guild(ctx.guild.id)
        jail_role = ctx.guild.get_role(row["jail_role"]) if row else None
        new_roles = [r for r in member.roles if r != jail_role] + restored

        try:
            await member.edit(roles=list(set(new_roles)), reason=_reason(ctx, reason))
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("Missing permissions to restore roles."))

        await self.bot.db.unjail_user(member.id, ctx.guild.id)

        dm_embed = mod_embed("Released from Jail", member, ctx.author, reason, discord.Color.green())
        dm_embed.description = f"You have been **released** in **{ctx.guild.name}**."
        await _send_dm(member, dm_embed)

        e = mod_embed("Member Released", member, ctx.author, reason, discord.Color.green())
        e.add_field(name="Roles Restored", value=f"{len(restored)} role(s)")
        await ctx.send(embed=e)
        await self._log_mod(ctx.guild, e)

    # ── Warnings ───────────────────────────────────────────────────────────────

    # ── Purge ──────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="purge", aliases=["clear"], usage="purge [count]")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def purge(self, ctx: commands.Context, count: int = None):
        """
        Purge messages.
        - `purge 50`   — deletes the last 50 messages
        - `purge`      — deletes ALL messages in the channel (no time limit, slow)
        """
        await ctx.message.delete()

        if count is not None:
            if count < 1 or count > 5000:
                return await ctx.send(embed=error_embed("Count must be between 1 and 5000."), delete_after=5)
            deleted = await ctx.channel.purge(limit=count, bulk=True)
            msg = await ctx.send(embed=success_embed(f"Deleted `{len(deleted)}` message(s)."))
            await asyncio.sleep(4)
            await msg.delete()
        else:
            # Purge ALL messages regardless of age
            confirm_msg = await ctx.send(
                embed=discord.Embed(
                    description="⚠️ This will delete **ALL** messages in this channel. React ✅ to confirm.",
                    color=discord.Color.red()
                )
            )
            await confirm_msg.add_reaction("✅")
            await confirm_msg.add_reaction("❌")

            def check(reaction, user):
                return user == ctx.author and str(reaction.emoji) in ("✅", "❌") and reaction.message.id == confirm_msg.id

            try:
                reaction, _ = await self.bot.wait_for("reaction_add", timeout=30.0, check=check)
            except asyncio.TimeoutError:
                await confirm_msg.delete()
                return

            if str(reaction.emoji) == "❌":
                return await confirm_msg.delete()

            await confirm_msg.delete()
            status_msg = await ctx.send(embed=info_embed("🗑️ Purging all messages… this may take a while."))

            total = 0
            while True:
                deleted = await ctx.channel.purge(limit=1000, bulk=True)
                total += len(deleted)
                if not deleted:
                    # Try fetching older messages (bulk delete can't go past 14 days)
                    old_msgs = []
                    async for msg in ctx.channel.history(limit=1000):
                        old_msgs.append(msg)
                    if not old_msgs:
                        break
                    for msg in old_msgs:
                        try:
                            await msg.delete()
                            total += 1
                            await asyncio.sleep(0.5)  # rate limit respect
                        except Exception:
                            pass
                    break
                await asyncio.sleep(0.5)

            try:
                await status_msg.delete()
            except Exception:
                pass
            done = await ctx.send(embed=success_embed(f"Purged `{total}` total message(s)."))
            await asyncio.sleep(5)
            await done.delete()

    @commands.hybrid_command(name="purgeuser", usage="purgeuser <member> [count]")
    @commands.guild_only()
    @commands.has_permissions(manage_messages=True)
    @commands.bot_has_permissions(manage_messages=True)
    async def purgeuser(self, ctx: commands.Context, member: discord.Member, count: int = 100):
        """Purge up to [count] messages from a specific user."""
        await ctx.message.delete()
        deleted = await ctx.channel.purge(limit=count * 5, check=lambda m: m.author == member, bulk=True)
        msg = await ctx.send(embed=success_embed(f"Deleted `{len(deleted)}` message(s) from {member.mention}."))
        await asyncio.sleep(4)
        await msg.delete()

    # ── Lockdown ───────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="lock", usage="lock [channel] [reason]")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def lock(self, ctx: commands.Context, channel: discord.TextChannel = None, *, reason: str = None):
        """Lock a channel so @everyone cannot send messages."""
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=_reason(ctx, reason))
        await channel.send(embed=discord.Embed(
            description=f"🔒 This channel has been locked by {ctx.author.mention}.\n**Reason:** {reason or 'No reason provided'}",
            color=discord.Color.red()
        ))
        if channel != ctx.channel:
            await ctx.send(embed=success_embed(f"🔒 {channel.mention} locked."))

    @commands.hybrid_command(name="unlock", usage="unlock [channel] [reason]")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def unlock(self, ctx: commands.Context, channel: discord.TextChannel = None, *, reason: str = None):
        """Unlock a locked channel."""
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = None  # reset to default
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=_reason(ctx, reason))
        await channel.send(embed=discord.Embed(
            description=f"🔓 This channel has been unlocked by {ctx.author.mention}.",
            color=discord.Color.green()
        ))

    @commands.hybrid_command(name="lockdown", usage="lockdown [reason]")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def lockdown(self, ctx: commands.Context, *, reason: str = None):
        """Lock ALL text channels in the server."""
        locked = 0
        for channel in ctx.guild.text_channels:
            try:
                overwrite = channel.overwrites_for(ctx.guild.default_role)
                overwrite.send_messages = False
                await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite, reason=_reason(ctx, reason))
                locked += 1
            except Exception:
                pass
        await ctx.send(embed=success_embed(f"🔒 Server lockdown activated. Locked `{locked}` channels."))

    @commands.hybrid_command(name="unlockdown", usage="unlockdown")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def unlockdown(self, ctx: commands.Context):
        """Remove lockdown from ALL text channels."""
        unlocked = 0
        for channel in ctx.guild.text_channels:
            try:
                overwrite = channel.overwrites_for(ctx.guild.default_role)
                overwrite.send_messages = None
                await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
                unlocked += 1
            except Exception:
                pass
        await ctx.send(embed=success_embed(f"🔓 Server lockdown lifted. Unlocked `{unlocked}` channels."))

    # ── Moderation history ─────────────────────────────────────────────────────

    # ── Slowmode ───────────────────────────────────────────────────────────────

    # ── Nick ───────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="nick", usage="nick <member> [new_nick]")
    @commands.guild_only()
    @commands.has_permissions(manage_nicknames=True)
    @commands.bot_has_permissions(manage_nicknames=True)
    async def nick(self, ctx: commands.Context, member: discord.Member, *, new_nick: str = None):
        """Change or reset a member's nickname."""
        old = member.display_name
        await member.edit(nick=new_nick, reason=_reason(ctx, None))
        if new_nick:
            await ctx.send(embed=success_embed(f"Renamed **{old}** → **{new_nick}**."))
        else:
            await ctx.send(embed=success_embed(f"Reset **{old}**'s nickname."))

    # ── Voice kick ─────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="vcmove", usage="vcmove <member> <voice_channel>")
    @commands.guild_only()
    @commands.has_permissions(move_members=True)
    @commands.bot_has_permissions(move_members=True)
    async def vcmove(self, ctx: commands.Context, member: discord.Member, channel: discord.VoiceChannel):
        """Move a member to a different voice channel."""
        if not member.voice:
            return await ctx.send(embed=error_embed(f"{member.mention} is not in a voice channel."))
        await member.move_to(channel, reason=_reason(ctx, None))
        await ctx.send(embed=success_embed(f"Moved {member.mention} to **{channel.name}**."))

    @commands.hybrid_command(name="vckick", usage="vckick <member>")
    @commands.guild_only()
    @commands.has_permissions(move_members=True)
    @commands.bot_has_permissions(move_members=True)
    async def vckick(self, ctx: commands.Context, member: discord.Member):
        """Kick a member from their voice channel."""
        if not member.voice:
            return await ctx.send(embed=error_embed(f"{member.mention} is not in a voice channel."))
        await member.move_to(None)
        await ctx.send(embed=success_embed(f"Removed {member.mention} from voice."))


    @commands.hybrid_command(name="modlogs", aliases=["history"], usage="modlogs <member>")
    @commands.guild_only()
    @commands.has_permissions(kick_members=True)
    async def modlogs(self, ctx: commands.Context, member: discord.Member):
        """View moderation history for a member."""
        actions = await self.bot.db.get_mod_history(member.id, ctx.guild.id)
        if not actions:
            return await ctx.send(embed=success_embed(f"{member.mention} has no moderation history."))

        e = discord.Embed(
            title=f"📋 Mod History — {member}",
            color=discord.Color.blurple(),
            timestamp=datetime.datetime.utcnow()
        )
        e.set_thumbnail(url=member.display_avatar.url)
        for a in actions[:10]:
            mod = ctx.guild.get_member(a["mod_id"]) or f"ID:{a['mod_id']}"
            e.add_field(
                name=f"{a['action'].upper()} — {format_dt(a['created_at'])}",
                value=f"**By:** {mod}\n**Reason:** {a['reason'] or 'None'}",
                inline=False
            )
        if len(actions) > 10:
            e.set_footer(text=f"Showing 10/{len(actions)} actions")
        await ctx.send(embed=e)


async def setup(bot):
    await bot.add_cog(Moderation(bot))
