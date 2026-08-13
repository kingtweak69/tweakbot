"""
Channels cog — voice channel access control, plus channel and category management.

Everything here is prefix-only on purpose. Slash commands are capped at 100
globally and this repo is already close to it, so a 20-command admin suite
would eat a fifth of the budget for commands nobody types twice a month.
"""
from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from utils.helpers import error_embed, info_embed, success_embed
from utils.modui import ConfirmView, chunk_field, clip

log = logging.getLogger("cogs.channels")

CHANNEL_EDIT_DELAY = 0.35

CHANNEL_KINDS = ("text", "voice", "stage", "forum", "news")


def _reason(ctx: commands.Context, what: str) -> str:
    return f"{what} | {ctx.author} ({ctx.author.id})"[:512]


class Channels(commands.Cog):
    """📁 Voice access control and channel management."""

    def __init__(self, bot):
        self.bot = bot

    # ── Shared ────────────────────────────────────────────────────────────────

    def _resolve_vc(self, ctx: commands.Context, channel) -> discord.VoiceChannel | None:
        """Named channel, else whichever one the caller is sitting in."""
        if channel:
            return channel
        if ctx.author.voice and ctx.author.voice.channel:
            return ctx.author.voice.channel
        return None

    async def _set_overwrite(self, channel, target, reason: str, **perms) -> str | None:
        """Apply an overwrite. Returns an error string, or None on success."""
        overwrite = channel.overwrites_for(target)
        for name, value in perms.items():
            setattr(overwrite, name, value)
        try:
            await channel.set_permissions(target, overwrite=overwrite, reason=reason)
        except discord.Forbidden:
            return "I'm missing Manage Roles or Manage Channels on that channel."
        except discord.HTTPException as exc:
            return f"Discord rejected that (HTTP {exc.status})."
        return None

    # ── Voice access ──────────────────────────────────────────────────────────

    @commands.command(name="vclock", usage="vclock [voice_channel]")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def vclock(self, ctx: commands.Context, channel: discord.VoiceChannel = None):
        """Stop @everyone joining a voice channel. People already inside stay."""
        channel = self._resolve_vc(ctx, channel)
        if not channel:
            return await ctx.send(embed=error_embed("Join a voice channel or name one."))

        error = await self._set_overwrite(
            channel, ctx.guild.default_role, _reason(ctx, "vclock"), connect=False
        )
        if error:
            return await ctx.send(embed=error_embed(error))

        inside = len([m for m in channel.members if not m.bot])
        note = f" `{inside}` member(s) already inside are unaffected." if inside else ""
        await ctx.send(embed=success_embed(f"🔒 **{channel.name}** locked.{note}"))

    @commands.command(name="vcunlock", usage="vcunlock [voice_channel]")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def vcunlock(self, ctx: commands.Context, channel: discord.VoiceChannel = None):
        """Let @everyone join a voice channel again."""
        channel = self._resolve_vc(ctx, channel)
        if not channel:
            return await ctx.send(embed=error_embed("Join a voice channel or name one."))

        error = await self._set_overwrite(
            channel, ctx.guild.default_role, _reason(ctx, "vcunlock"), connect=None
        )
        if error:
            return await ctx.send(embed=error_embed(error))
        await ctx.send(embed=success_embed(f"🔓 **{channel.name}** unlocked."))

    @commands.command(name="vcallow", usage="vcallow <member> [voice_channel]")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def vcallow(
        self, ctx: commands.Context, member: discord.Member, channel: discord.VoiceChannel = None
    ):
        """Let one member into a locked voice channel."""
        channel = self._resolve_vc(ctx, channel)
        if not channel:
            return await ctx.send(embed=error_embed("Join a voice channel or name one."))

        error = await self._set_overwrite(
            channel, member, _reason(ctx, "vcallow"), connect=True, view_channel=True
        )
        if error:
            return await ctx.send(embed=error_embed(error))
        await ctx.send(embed=success_embed(f"✅ {member.mention} can join **{channel.name}**."))

    @commands.command(name="vcdeny", usage="vcdeny <member> [voice_channel]")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def vcdeny(
        self, ctx: commands.Context, member: discord.Member, channel: discord.VoiceChannel = None
    ):
        """Bar one member from a voice channel, and boot them if they're in it."""
        channel = self._resolve_vc(ctx, channel)
        if not channel:
            return await ctx.send(embed=error_embed("Join a voice channel or name one."))

        error = await self._set_overwrite(
            channel, member, _reason(ctx, "vcdeny"), connect=False
        )
        if error:
            return await ctx.send(embed=error_embed(error))

        kicked = ""
        if member.voice and member.voice.channel == channel:
            try:
                await member.move_to(None, reason=_reason(ctx, "vcdeny"))
                kicked = " They were disconnected."
            except discord.HTTPException:
                kicked = " I couldn't disconnect them — they're still in there."

        await ctx.send(embed=success_embed(f"⛔ {member.mention} barred from **{channel.name}**.{kicked}"))

    @commands.command(name="vcreset", usage="vcreset <member> [voice_channel]")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def vcreset(
        self, ctx: commands.Context, member: discord.Member, channel: discord.VoiceChannel = None
    ):
        """Clear a member's personal allow/deny so the channel default applies."""
        channel = self._resolve_vc(ctx, channel)
        if not channel:
            return await ctx.send(embed=error_embed("Join a voice channel or name one."))
        try:
            await channel.set_permissions(member, overwrite=None, reason=_reason(ctx, "vcreset"))
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I'm missing Manage Roles on that channel."))
        await ctx.send(embed=success_embed(f"Cleared {member.mention}'s override on **{channel.name}**."))

    # ── Channel management ────────────────────────────────────────────────────

    @commands.group(name="channel", aliases=["chan"], invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def channel(self, ctx: commands.Context):
        """Channel management."""
        await ctx.send(embed=info_embed(
            "`channel create <name> [text|voice|stage|forum|news] [category]`\n"
            "`channel delete [channel]` · `channel rename <channel> <name>`\n"
            "`channel topic <channel> <text>` · `channel slowmode <channel> <seconds>`\n"
            "`channel nsfw <channel>` · `channel move <channel> <category>`\n"
            "`channel clone [channel]` · `channel list [category]`"
        ))

    @channel.command(name="create", usage="channel create <name> [kind] [category]")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def channel_create(
        self, ctx: commands.Context, name: str,
        kind: str = "text", *, category: discord.CategoryChannel = None,
    ):
        """Create a channel. Kind defaults to text."""
        kind = kind.lower()
        if kind not in CHANNEL_KINDS:
            return await ctx.send(embed=error_embed(
                f"Kind must be one of: {', '.join(f'`{k}`' for k in CHANNEL_KINDS)}"
            ))
        if len(name) > 100:
            return await ctx.send(embed=error_embed("Channel names max out at 100 characters."))

        reason = _reason(ctx, "channel create")
        try:
            if kind == "voice":
                created = await ctx.guild.create_voice_channel(name, category=category, reason=reason)
            elif kind == "stage":
                created = await ctx.guild.create_stage_channel(name, category=category, reason=reason)
            elif kind == "forum":
                created = await ctx.guild.create_forum(name, category=category, reason=reason)
            elif kind == "news":
                created = await ctx.guild.create_text_channel(name, category=category, news=True, reason=reason)
            else:
                created = await ctx.guild.create_text_channel(name, category=category, reason=reason)
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I'm missing Manage Channels."))
        except discord.HTTPException as exc:
            return await ctx.send(embed=error_embed(f"Discord rejected that (HTTP {exc.status})."))

        where = f" in **{category.name}**" if category else ""
        await ctx.send(embed=success_embed(f"Created {created.mention}{where}."))

    @channel.command(name="delete", aliases=["remove"], usage="channel delete [channel]")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def channel_delete(self, ctx: commands.Context, channel: discord.abc.GuildChannel = None):
        """Delete a channel. Every message in it goes too."""
        channel = channel or ctx.channel
        view = ConfirmView(ctx.author.id, confirm_label="Delete")
        prompt = await ctx.send(
            embed=discord.Embed(
                title="⚠️ Delete channel",
                description=f"**#{channel.name}** and every message in it. This cannot be undone.",
                color=discord.Color.red(),
            ),
            view=view,
        )
        await view.wait()
        if not view.value:
            return await prompt.edit(embed=info_embed("Cancelled."), view=None)

        name = channel.name
        deleting_here = channel.id == ctx.channel.id
        try:
            await channel.delete(reason=_reason(ctx, "channel delete"))
        except discord.Forbidden:
            return await prompt.edit(embed=error_embed("I'm missing Manage Channels."), view=None)

        if not deleting_here:
            await prompt.edit(embed=success_embed(f"Deleted **#{name}**."), view=None)

    @channel.command(name="rename", usage="channel rename <channel> <name>")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def channel_rename(
        self, ctx: commands.Context, channel: discord.abc.GuildChannel, *, name: str
    ):
        """Rename a channel."""
        if len(name) > 100:
            return await ctx.send(embed=error_embed("Channel names max out at 100 characters."))
        old = channel.name
        try:
            await channel.edit(name=name, reason=_reason(ctx, "channel rename"))
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I'm missing Manage Channels."))
        await ctx.send(embed=success_embed(f"**#{old}** → **#{name}**."))

    @channel.command(name="topic", usage="channel topic <channel> <text>")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def channel_topic(
        self, ctx: commands.Context, channel: discord.TextChannel, *, text: str = None
    ):
        """Set or clear a channel topic."""
        if text and len(text) > 1024:
            return await ctx.send(embed=error_embed("Topics max out at 1024 characters."))
        try:
            await channel.edit(topic=text, reason=_reason(ctx, "channel topic"))
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I'm missing Manage Channels."))
        if text:
            return await ctx.send(embed=success_embed(f"Topic for {channel.mention} set."))
        await ctx.send(embed=success_embed(f"Topic for {channel.mention} cleared."))

    @channel.command(name="slowmode", usage="channel slowmode <channel> <seconds>")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def channel_slowmode(
        self, ctx: commands.Context, channel: discord.TextChannel, seconds: int = 0
    ):
        """Set slowmode on a channel. 0 turns it off. Max 21600 (6 hours)."""
        if not 0 <= seconds <= 21600:
            return await ctx.send(embed=error_embed("Slowmode must be 0–21600 seconds."))
        try:
            await channel.edit(slowmode_delay=seconds, reason=_reason(ctx, "slowmode"))
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I'm missing Manage Channels."))
        if seconds == 0:
            return await ctx.send(embed=success_embed(f"Slowmode off in {channel.mention}."))
        await ctx.send(embed=success_embed(f"Slowmode in {channel.mention} set to `{seconds}`s."))

    @channel.command(name="nsfw", usage="channel nsfw <channel>")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def channel_nsfw(self, ctx: commands.Context, channel: discord.TextChannel = None):
        """Toggle a channel's age-restricted flag."""
        channel = channel or ctx.channel
        new = not channel.is_nsfw()
        try:
            await channel.edit(nsfw=new, reason=_reason(ctx, "nsfw toggle"))
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I'm missing Manage Channels."))
        await ctx.send(embed=success_embed(
            f"{channel.mention} is {'now' if new else 'no longer'} age-restricted."
        ))

    @channel.command(name="move", usage="channel move <channel> <category>")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def channel_move(
        self, ctx: commands.Context, channel: discord.abc.GuildChannel,
        *, category: discord.CategoryChannel = None,
    ):
        """Move a channel into a category, or out of one if you name none."""
        try:
            await channel.edit(category=category, reason=_reason(ctx, "channel move"))
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I'm missing Manage Channels."))
        where = f"into **{category.name}**" if category else "out of its category"
        await ctx.send(embed=success_embed(f"Moved **#{channel.name}** {where}."))

    @channel.command(name="clone", usage="channel clone [channel]")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def channel_clone(self, ctx: commands.Context, channel: discord.abc.GuildChannel = None):
        """Copy a channel's settings and permissions into a new empty channel."""
        channel = channel or ctx.channel
        try:
            clone = await channel.clone(reason=_reason(ctx, "channel clone"))
            await clone.edit(position=channel.position + 1)
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I'm missing Manage Channels."))
        except discord.HTTPException as exc:
            return await ctx.send(embed=error_embed(f"Discord rejected that (HTTP {exc.status})."))
        await ctx.send(embed=success_embed(f"Cloned into {clone.mention} — settings and permissions only, no messages."))

    @channel.command(name="list", usage="channel list [category]")
    @commands.has_permissions(manage_channels=True)
    async def channel_list(self, ctx: commands.Context, *, category: discord.CategoryChannel = None):
        """List channels, optionally within one category."""
        icons = {
            discord.ChannelType.text: "💬", discord.ChannelType.voice: "🔊",
            discord.ChannelType.news: "📢", discord.ChannelType.forum: "🗂️",
            discord.ChannelType.stage_voice: "🎙️", discord.ChannelType.category: "📁",
        }
        source = category.channels if category else [
            c for c in ctx.guild.channels if c.type is not discord.ChannelType.category
        ]
        if not source:
            return await ctx.send(embed=info_embed("Nothing to list."))

        lines = [
            f"{icons.get(c.type, '•')} **{c.name}** `{c.id}`"
            for c in sorted(source, key=lambda c: c.position)
        ]
        e = discord.Embed(
            title=f"📁 {category.name if category else ctx.guild.name}",
            color=discord.Color.blurple(),
        )
        chunk_field(e, f"Channels ({len(source)})", lines)
        await ctx.send(embed=e)

    # ── Category management ───────────────────────────────────────────────────

    @commands.group(name="category", aliases=["cat"], invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def category(self, ctx: commands.Context):
        """Category management."""
        await ctx.send(embed=info_embed(
            "`category create <name>` · `category rename <category> <name>`\n"
            "`category delete <category> [--purge]` · `category list`"
        ))

    @category.command(name="create", usage="category create <name>")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def category_create(self, ctx: commands.Context, *, name: str):
        """Create a category."""
        if len(name) > 100:
            return await ctx.send(embed=error_embed("Category names max out at 100 characters."))
        try:
            created = await ctx.guild.create_category(name, reason=_reason(ctx, "category create"))
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I'm missing Manage Channels."))
        await ctx.send(embed=success_embed(f"Created category **{created.name}**."))

    @category.command(name="rename", usage="category rename <category> <name>")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def category_rename(
        self, ctx: commands.Context, category: discord.CategoryChannel, *, name: str
    ):
        """Rename a category."""
        old = category.name
        try:
            await category.edit(name=name, reason=_reason(ctx, "category rename"))
        except discord.Forbidden:
            return await ctx.send(embed=error_embed("I'm missing Manage Channels."))
        await ctx.send(embed=success_embed(f"**{old}** → **{name}**."))

    @category.command(name="delete", aliases=["remove"], usage="category delete <category> [--purge]")
    @commands.has_permissions(manage_channels=True)
    @commands.bot_has_permissions(manage_channels=True)
    async def category_delete(
        self, ctx: commands.Context, category: discord.CategoryChannel, *, flags: str = ""
    ):
        """
        Delete a category. Its channels are orphaned to the top level unless
        you pass --purge, which deletes them and every message in them.
        """
        purge = "--purge" in flags.lower()
        children = list(category.channels)

        description = (
            f"**{category.name}** and its `{len(children)}` channel(s), "
            "including every message in them. This cannot be undone."
            if purge else
            f"**{category.name}**. Its `{len(children)}` channel(s) will be kept "
            "and moved to the top level."
        )
        view = ConfirmView(ctx.author.id, confirm_label="Purge" if purge else "Delete")
        prompt = await ctx.send(
            embed=discord.Embed(
                title="⚠️ Delete category",
                description=description,
                color=discord.Color.red(),
            ),
            view=view,
        )
        await view.wait()
        if not view.value:
            return await prompt.edit(embed=info_embed("Cancelled."), view=None)

        await prompt.edit(embed=info_embed("Working..."), view=None)
        reason = _reason(ctx, "category delete")
        done = failed = 0

        for child in children:
            try:
                if purge:
                    await child.delete(reason=reason)
                else:
                    await child.edit(category=None, reason=reason)
                done += 1
            except discord.HTTPException:
                failed += 1
            await asyncio.sleep(CHANNEL_EDIT_DELAY)

        name = category.name
        try:
            await category.delete(reason=reason)
        except discord.HTTPException as exc:
            return await prompt.edit(embed=error_embed(
                f"Handled `{done}` child channel(s) but couldn't delete the category (HTTP {exc.status})."
            ))

        verb = "deleted" if purge else "moved out"
        await prompt.edit(embed=success_embed(
            f"Deleted **{name}**. `{done}` channel(s) {verb}."
            + (f" `{failed}` failed." if failed else "")
        ))

    @category.command(name="list", usage="category list")
    @commands.has_permissions(manage_channels=True)
    async def category_list(self, ctx: commands.Context):
        """List every category and how many channels each holds."""
        categories = sorted(ctx.guild.categories, key=lambda c: c.position)
        if not categories:
            return await ctx.send(embed=info_embed("This server has no categories."))

        orphans = len([
            c for c in ctx.guild.channels
            if c.category is None and c.type is not discord.ChannelType.category
        ])
        lines = [f"📁 **{c.name}** — `{len(c.channels)}` channel(s) `{c.id}`" for c in categories]
        if orphans:
            lines.append(f"— *{orphans} channel(s) outside any category*")

        e = discord.Embed(title=f"📁 Categories — {ctx.guild.name}", color=discord.Color.blurple())
        chunk_field(e, f"Categories ({len(categories)})", lines)
        await ctx.send(embed=e)


async def setup(bot):
    await bot.add_cog(Channels(bot))
