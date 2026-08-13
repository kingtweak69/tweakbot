"""
Leveling cog — XP, level-up notifications, leaderboard, level roles.
"""
from __future__ import annotations

import datetime
import logging
import random
import time

import discord
from discord.ext import commands, tasks

import config
from utils.helpers import error_embed, info_embed, success_embed

log = logging.getLogger("cogs.leveling")

COOLDOWN_PRUNE_MINUTES = 10
LEADERBOARD_PER_PAGE = 10


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


class ConfirmView(discord.ui.View):
    """Yes/no buttons locked to one user. Reaction checks are too easy to get wrong."""

    def __init__(self, author_id: int, timeout: float = 30.0):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return False
        return True

    def _finish(self):
        for child in self.children:
            child.disabled = True
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self._finish()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self._finish()
        await interaction.response.edit_message(view=self)


class Leveling(commands.Cog):
    """⭐ XP and leveling system."""

    def __init__(self, bot):
        self.bot = bot
        self._xp_cooldowns: dict[tuple[int, int], float] = {}
        # guild_id -> whether leveling is on. Avoids a DB read per message.
        self._enabled: dict[int, bool] = {}

    async def cog_load(self):
        self.prune_cooldowns.start()

    async def cog_unload(self):
        self.prune_cooldowns.cancel()

    @tasks.loop(minutes=COOLDOWN_PRUNE_MINUTES)
    async def prune_cooldowns(self):
        """Without this the dict grows for every user seen, forever."""
        cutoff = time.time() - (config.XP_COOLDOWN_SECONDS * 2)
        stale = [k for k, v in self._xp_cooldowns.items() if v < cutoff]
        for key in stale:
            self._xp_cooldowns.pop(key, None)

    @prune_cooldowns.before_loop
    async def _before_prune(self):
        await self.bot.wait_until_ready()

    # ── Leveling toggle cache ─────────────────────────────────────────────────

    async def _leveling_on(self, guild_id: int) -> bool:
        if guild_id not in self._enabled:
            row = await self.bot.db.get_guild(guild_id)
            self._enabled[guild_id] = bool(row["leveling"]) if row else True
        return self._enabled[guild_id]

    # ── XP grant ──────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        gid = message.guild.id
        uid = message.author.id
        key = (uid, gid)
        now = time.time()

        # Cheap check first — most messages are inside the cooldown window.
        if now - self._xp_cooldowns.get(key, 0.0) < config.XP_COOLDOWN_SECONDS:
            return
        if not await self._leveling_on(gid):
            return

        self._xp_cooldowns[key] = now
        amount = random.randint(config.XP_PER_MESSAGE_MIN, config.XP_PER_MESSAGE_MAX)
        _, new_level, leveled_up = await self.bot.db.add_xp(uid, gid, amount)

        if leveled_up:
            await self._handle_level_up(message, new_level)

    async def _handle_level_up(self, message: discord.Message, level: int):
        e = discord.Embed(
            title="⬆️ Level Up!",
            description=f"**{message.author.mention}** reached **Level {level}**! 🎉",
            color=discord.Color.gold(),
            timestamp=_utcnow(),
        )
        e.set_thumbnail(url=message.author.display_avatar.url)
        e.add_field(name="XP to next level", value=f"`{self.bot.db.level_to_xp(level + 1):,}`")
        try:
            await message.channel.send(embed=e)
        except discord.HTTPException as exc:
            log.warning("Could not announce level up in %s: %s", message.channel.id, exc)

        await self._grant_level_roles(message.guild, message.author, level)

    async def _grant_level_roles(self, guild: discord.Guild, member: discord.Member, level: int):
        """
        Grant every reward at or below the new level, not just an exact match —
        an admin `addxp` can jump someone several levels at once.
        """
        try:
            rows = await self.bot.db.get_level_roles(guild.id)
        except Exception as exc:
            log.error("Could not load level roles for %s: %s", guild.id, exc)
            return

        owed = []
        for lr in rows:
            if lr["level"] > level:
                continue
            role = guild.get_role(lr["role_id"])
            if role and role not in member.roles:
                owed.append(role)

        if not owed:
            return

        try:
            await member.add_roles(*owed, reason=f"Level {level} reward")
        except discord.Forbidden:
            log.warning(
                "Missing permission or role hierarchy blocks granting %s in %s — "
                "check the bot's top role is above the reward roles.",
                ", ".join(r.name for r in owed), guild.id,
            )
        except discord.HTTPException as exc:
            log.warning("Could not grant level roles in %s: %s", guild.id, exc)

    # ── Rank ──────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="rank", aliases=["level", "xp"], usage="rank [member]")
    @commands.guild_only()
    async def rank(self, ctx: commands.Context, member: discord.Member = None):
        """Show your (or another member's) rank and XP."""
        member = member or ctx.author
        row = await self.bot.db.get_user(member.id, ctx.guild.id)
        if not row:
            return await ctx.send(embed=info_embed(f"{member.mention} has no XP yet."))

        xp = row["xp"]
        level = row["level"]
        next_xp = self.bot.db.level_to_xp(level + 1)
        prev_xp = self.bot.db.level_to_xp(level)
        needed = next_xp - prev_xp
        pct = min(100, max(0, int(((xp - prev_xp) / needed) * 100))) if needed > 0 else 100

        # Indexed COUNT, not a 500-row fetch. Works at any rank.
        position = await self.bot.db.get_rank_position(ctx.guild.id, xp)

        filled = pct // 5
        bar = "█" * filled + "░" * (20 - filled)

        e = discord.Embed(color=discord.Color.blurple(), timestamp=_utcnow())
        e.set_author(name=f"{member.display_name}'s Rank", icon_url=member.display_avatar.url)
        e.add_field(name="Level", value=f"**{level}**", inline=True)
        e.add_field(name="Rank", value=f"**#{position}**", inline=True)
        e.add_field(name="Messages", value=f"**{row['messages']:,}**", inline=True)
        e.add_field(name=f"XP — {xp:,} / {next_xp:,}", value=f"`{bar}` **{pct}%**", inline=False)
        e.set_thumbnail(url=member.display_avatar.url)
        await ctx.send(embed=e)

    # ── Leaderboard ───────────────────────────────────────────────────────────

    @commands.hybrid_command(name="leaderboard", aliases=["lb", "top"], usage="leaderboard [page]")
    @commands.guild_only()
    async def leaderboard(self, ctx: commands.Context, page: int = 1):
        """Show the server XP leaderboard."""
        if page < 1:
            return await ctx.send(embed=error_embed("Page must be 1 or higher."))

        offset = (page - 1) * LEADERBOARD_PER_PAGE
        rows = await self.bot.db.get_leaderboard_page(ctx.guild.id, LEADERBOARD_PER_PAGE, offset)
        if not rows:
            message = "No XP data for this server yet." if page == 1 else "No data for that page."
            return await ctx.send(embed=info_embed(message))

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, r in enumerate(rows, start=offset + 1):
            prefix = medals[i - 1] if i <= 3 else f"`{i}.`"
            member = ctx.guild.get_member(r["user_id"])
            name = discord.utils.escape_markdown(member.display_name) if member else f"ID:{r['user_id']}"
            lines.append(f"{prefix} **{name}** — Level **{r['level']}** | `{r['xp']:,}` XP")

        e = discord.Embed(
            title=f"🏆 XP Leaderboard — {ctx.guild.name}",
            description="\n".join(lines),
            color=discord.Color.gold(),
            timestamp=_utcnow(),
        )
        e.set_footer(text=f"Page {page}")
        await ctx.send(embed=e)

    # ── Admin XP commands ─────────────────────────────────────────────────────

    @commands.command(name="addxp", usage="addxp <member> <amount>")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def addxp(self, ctx: commands.Context, member: discord.Member, amount: int):
        """Add XP to a member."""
        if amount <= 0:
            return await ctx.send(embed=error_embed("Amount must be positive."))
        row = await self.bot.db.get_user(member.id, ctx.guild.id)
        total = (row["xp"] if row else 0) + amount
        await self.bot.db.set_xp(member.id, ctx.guild.id, total)
        await ctx.send(embed=success_embed(f"Added `{amount:,}` XP to {member.mention}. Total: `{total:,}`."))

    @commands.command(name="removexp", usage="removexp <member> <amount>")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def removexp(self, ctx: commands.Context, member: discord.Member, amount: int):
        """Remove XP from a member."""
        if amount <= 0:
            return await ctx.send(embed=error_embed("Amount must be positive."))
        row = await self.bot.db.get_user(member.id, ctx.guild.id)
        new = max(0, (row["xp"] if row else 0) - amount)
        await self.bot.db.set_xp(member.id, ctx.guild.id, new)
        await ctx.send(embed=success_embed(f"Removed `{amount:,}` XP from {member.mention}. New total: `{new:,}`."))

    @commands.command(name="setxp", usage="setxp <member> <amount>")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def setxp(self, ctx: commands.Context, member: discord.Member, amount: int):
        """Set a member's XP to a specific value."""
        amount = max(0, amount)
        await self.bot.db.set_xp(member.id, ctx.guild.id, amount)
        await ctx.send(embed=success_embed(f"Set {member.mention}'s XP to `{amount:,}`."))

    @commands.command(name="resetxp", usage="resetxp <member>")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def resetxp(self, ctx: commands.Context, member: discord.Member):
        """Reset a member's XP to zero."""
        await self.bot.db.set_xp(member.id, ctx.guild.id, 0)
        await ctx.send(embed=success_embed(f"Reset {member.mention}'s XP."))

    @commands.command(name="resetleaderboard", usage="resetleaderboard")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def resetleaderboard(self, ctx: commands.Context):
        """Wipe every member's XP in this server."""
        view = ConfirmView(ctx.author.id)
        prompt = await ctx.send(
            embed=discord.Embed(
                title="⚠️ Reset the entire leaderboard?",
                description="Every member's XP and level in this server will be deleted. This cannot be undone.",
                color=discord.Color.red(),
            ),
            view=view,
        )
        await view.wait()

        if view.value is None:
            return await prompt.edit(embed=info_embed("Timed out — nothing was changed."), view=None)
        if not view.value:
            return await prompt.edit(embed=info_embed("Cancelled — nothing was changed."), view=None)

        try:
            deleted = await self.bot.db.reset_leaderboard(ctx.guild.id)
        except Exception as exc:
            log.error("Leaderboard reset failed for %s: %s", ctx.guild.id, exc, exc_info=True)
            return await prompt.edit(embed=error_embed("The reset failed — check the logs."), view=None)

        await prompt.edit(
            embed=success_embed(f"Leaderboard reset. Cleared `{deleted}` member record(s)."),
            view=None,
        )

    # ── Level roles ───────────────────────────────────────────────────────────

    @commands.hybrid_group(name="levelroles", aliases=["lr"], invoke_without_command=True)
    @commands.guild_only()
    async def levelroles(self, ctx: commands.Context):
        """Manage level reward roles."""
        rows = await self.bot.db.get_level_roles(ctx.guild.id)
        if not rows:
            return await ctx.send(embed=info_embed("No level roles configured."))
        lines = []
        for r in sorted(rows, key=lambda x: x["level"]):
            role = ctx.guild.get_role(r["role_id"])
            label = role.mention if role else f"`deleted role {r['role_id']}`"
            lines.append(f"Level **{r['level']}** → {label}")
        e = discord.Embed(title="Level Roles", description="\n".join(lines), color=discord.Color.blurple())
        await ctx.send(embed=e)

    @levelroles.command(name="add", usage="levelroles add <level> <role>")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def levelroles_add(self, ctx: commands.Context, level: int, role: discord.Role):
        """Set the role awarded at a given level."""
        if level < 1:
            return await ctx.send(embed=error_embed("Level must be 1 or higher."))
        if role.is_default() or role.managed:
            return await ctx.send(embed=error_embed("That role can't be assigned by a bot."))
        if ctx.guild.me.top_role <= role:
            return await ctx.send(embed=error_embed(
                f"{role.mention} sits above my highest role, so I can't grant it. Move my role up."
            ))
        await self.bot.db.set_level_role(ctx.guild.id, level, role.id)
        await ctx.send(embed=success_embed(f"Level `{level}` will now reward {role.mention}."))

    @levelroles.command(name="remove", usage="levelroles remove <level>")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def levelroles_remove(self, ctx: commands.Context, level: int):
        """Remove the reward for a level."""
        await self.bot.db.remove_level_role(ctx.guild.id, level)
        await ctx.send(embed=success_embed(f"Removed level role reward for level `{level}`."))

    # ── Toggle ────────────────────────────────────────────────────────────────

    @commands.command(name="toggleleveling", usage="toggleleveling")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def toggleleveling(self, ctx: commands.Context):
        """Enable or disable the leveling system for this server."""
        row = await self.bot.db.get_guild(ctx.guild.id)
        current = row["leveling"] if row else True
        new = not bool(current)

        # The column may be BOOLEAN or SMALLINT depending on your schema, and
        # asyncpg rejects the wrong Python type. Match whatever came back.
        value = new if isinstance(current, bool) else int(new)

        await self.bot.db.set_guild_field(ctx.guild.id, "leveling", value)
        self._enabled[ctx.guild.id] = new
        await ctx.send(embed=success_embed(f"Leveling system **{'enabled' if new else 'disabled'}**."))


async def setup(bot):
    await bot.add_cog(Leveling(bot))
