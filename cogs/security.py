"""
Security cog — anti-nuke and anti-raid.

What this can and can't do, so nobody is surprised at 3am:

- Anti-nuke is DETECTION, not prevention. Discord gives no pre-action hook.
  The bot learns who deleted a channel by reading the audit log *after* it's
  gone. It can stop attack #4 onward; it can never stop #1.
- It cannot touch the server owner. Discord forbids it, full stop. If the
  owner's account is compromised, this cog can only alert.
- It cannot punish anyone whose top role sits above the bot's. Put the bot's
  role at the very top or half of this is decorative.
- Audit lookups are rate-limited, so a genuine mass-nuke may outrun detection
  by a few actions.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque

import discord
from discord.ext import commands, tasks

from utils.helpers import error_embed, info_embed, success_embed
from utils.modui import chunk_field, utcnow

log = logging.getLogger("cogs.security")

AUDIT_DELAY_SECONDS = 1.2
AUDIT_MAX_AGE_SECONDS = 25
CHANNEL_EDIT_DELAY = 0.35

# action key -> (default count, default window seconds, audit action)
NUKE_ACTIONS: dict[str, tuple[int, int, discord.AuditLogAction]] = {
    "channel_delete": (3, 30, discord.AuditLogAction.channel_delete),
    "channel_create": (5, 30, discord.AuditLogAction.channel_create),
    "role_delete": (3, 30, discord.AuditLogAction.role_delete),
    "role_create": (5, 30, discord.AuditLogAction.role_create),
    "ban": (5, 30, discord.AuditLogAction.ban),
    "kick": (5, 30, discord.AuditLogAction.kick),
    "webhook_create": (3, 30, discord.AuditLogAction.webhook_create),
}

PUNISHMENTS = ("alert", "strip", "kick", "ban")
RAID_ACTIONS = ("alert", "kick", "ban", "lock")

DEFAULTS = {
    "antinuke_enabled": False,
    "antinuke_punishment": "strip",
    "antiraid_enabled": False,
    "raid_join_count": 8,
    "raid_join_window": 15,
    "raid_action": "kick",
    "raid_min_account_age_days": 0,
    "raid_mode_minutes": 10,
}


class Security(commands.Cog):
    """🛡️ Anti-nuke and anti-raid protection."""

    def __init__(self, bot):
        self.bot = bot
        self._settings: dict[int, dict] = {}
        self._thresholds: dict[int, dict[str, tuple[int, int]]] = {}
        self._whitelist: dict[int, set[int]] = {}
        # (guild_id, actor_id, action) -> timestamps
        self._counters: dict[tuple[int, int, str], deque] = defaultdict(deque)
        self._joins: dict[int, deque] = defaultdict(deque)
        # guild_id -> unix timestamp when raid mode lifts
        self._raid_until: dict[int, float] = {}
        self._punishing: set[tuple[int, int]] = set()

    async def cog_load(self):
        self.housekeeping.start()

    async def cog_unload(self):
        self.housekeeping.cancel()

    @tasks.loop(minutes=5)
    async def housekeeping(self):
        """Drop stale counters so the dicts don't grow forever."""
        now = time.time()
        for key in [k for k, v in self._counters.items() if not v or now - v[-1] > 300]:
            self._counters.pop(key, None)
        for guild_id in [g for g, until in self._raid_until.items() if until < now]:
            self._raid_until.pop(guild_id, None)
            await self._alert_guild(guild_id, discord.Embed(
                title="🛡️ Raid mode expired",
                description="Automatic raid mode has lifted. New joins are no longer being screened.",
                color=discord.Color.green(), timestamp=utcnow(),
            ))

    @housekeeping.before_loop
    async def _before_housekeeping(self):
        await self.bot.wait_until_ready()

    # ── Settings ──────────────────────────────────────────────────────────────

    async def _settings_for(self, guild_id: int) -> dict:
        if guild_id not in self._settings:
            try:
                row = await self.bot.db.get_security(guild_id)
            except Exception as exc:
                log.error("Security settings load failed for %s: %s", guild_id, exc)
                row = None
            merged = dict(DEFAULTS)
            if row:
                for key in DEFAULTS:
                    if row[key] is not None:
                        merged[key] = row[key]
            self._settings[guild_id] = merged
        return self._settings[guild_id]

    async def _thresholds_for(self, guild_id: int) -> dict[str, tuple[int, int]]:
        if guild_id not in self._thresholds:
            table = {key: (c, w) for key, (c, w, _) in NUKE_ACTIONS.items()}
            try:
                for row in await self.bot.db.get_nuke_thresholds(guild_id):
                    if row["action_key"] in table:
                        table[row["action_key"]] = (row["max_count"], row["window_seconds"])
            except Exception as exc:
                log.error("Threshold load failed for %s: %s", guild_id, exc)
            self._thresholds[guild_id] = table
        return self._thresholds[guild_id]

    async def _whitelist_for(self, guild_id: int) -> set[int]:
        if guild_id not in self._whitelist:
            try:
                rows = await self.bot.db.get_security_whitelist(guild_id)
                self._whitelist[guild_id] = {r["user_id"] for r in rows}
            except Exception as exc:
                log.error("Whitelist load failed for %s: %s", guild_id, exc)
                self._whitelist[guild_id] = set()
        return self._whitelist[guild_id]

    async def _alert(self, guild: discord.Guild, embed: discord.Embed):
        row = await self.bot.db.get_guild(guild.id)
        channel_id = (row["mod_channel"] or row["log_channel"]) if row else None
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            channel = guild.system_channel
        if channel is None:
            return
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _alert_guild(self, guild_id: int, embed: discord.Embed):
        guild = self.bot.get_guild(guild_id)
        if guild:
            await self._alert(guild, embed)

    # ── Anti-nuke core ────────────────────────────────────────────────────────

    async def _audit_actor(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: int | None = None,
    ) -> discord.AuditLogEntry | None:
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None
        await asyncio.sleep(AUDIT_DELAY_SECONDS)
        try:
            async for entry in guild.audit_logs(limit=8, action=action):
                if (utcnow() - entry.created_at).total_seconds() > AUDIT_MAX_AGE_SECONDS:
                    break
                # For member-targeted actions the entry MUST be about this
                # member. Without this a voluntary leave landing seconds after
                # a real kick gets blamed on whoever did the kicking.
                if target_id is not None and getattr(entry.target, "id", None) != target_id:
                    continue
                return entry
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    async def _record(self, guild: discord.Guild, action_key: str, target_id: int | None = None):
        """One protected action happened. Find who, count it, punish if over."""
        settings = await self._settings_for(guild.id)
        if not settings["antinuke_enabled"]:
            return

        _, _, audit_action = NUKE_ACTIONS[action_key]
        entry = await self._audit_actor(guild, audit_action, target_id)
        # No matching audit entry means nobody did this to that target — a
        # voluntary leave, not a kick. Don't count it against anyone.
        if entry is None or entry.user is None:
            return

        actor = entry.user
        if actor.id == self.bot.user.id or actor.id == guild.owner_id:
            return
        if actor.id in await self._whitelist_for(guild.id):
            return

        max_count, window = (await self._thresholds_for(guild.id))[action_key]
        now = time.time()
        bucket = self._counters[(guild.id, actor.id, action_key)]
        bucket.append(now)
        while bucket and now - bucket[0] > window:
            bucket.popleft()

        if len(bucket) < max_count:
            return

        bucket.clear()
        await self._punish(guild, actor, action_key, max_count, window, settings["antinuke_punishment"])

    async def _punish(self, guild, actor, action_key, count, window, punishment):
        key = (guild.id, actor.id)
        if key in self._punishing:
            return
        self._punishing.add(key)
        try:
            member = guild.get_member(actor.id)
            outcome = "alert only"

            if punishment != "alert" and member is not None:
                if member.top_role >= guild.me.top_role:
                    outcome = "❗ could not act — their role is above mine"
                else:
                    try:
                        if punishment == "ban":
                            await member.ban(reason=f"Anti-nuke: {action_key} x{count}", delete_message_seconds=0)
                            outcome = "banned"
                        elif punishment == "kick":
                            await member.kick(reason=f"Anti-nuke: {action_key} x{count}")
                            outcome = "kicked"
                        elif punishment == "strip":
                            keep = [r for r in member.roles if r.managed or r.is_default()]
                            await member.edit(roles=keep, reason=f"Anti-nuke: {action_key} x{count}")
                            outcome = "all roles stripped"
                    except discord.Forbidden:
                        outcome = "❗ missing permissions to act"
                    except discord.HTTPException as exc:
                        outcome = f"❗ failed (HTTP {exc.status})"
            elif member is None:
                outcome = "❗ actor is no longer in the server"

            e = discord.Embed(
                title="🚨 Anti-nuke triggered",
                description=f"**{actor}** (`{actor.id}`) hit the limit for `{action_key}`.",
                color=discord.Color.red(), timestamp=utcnow(),
            )
            e.add_field(name="Trigger", value=f"`{count}` in `{window}`s")
            e.add_field(name="Punishment", value=punishment)
            e.add_field(name="Outcome", value=outcome, inline=False)
            e.set_footer(text="Anti-nuke reacts after the fact — check what was already destroyed.")
            await self._alert(guild, e)
            log.warning("Anti-nuke fired in %s on %s for %s (%s)", guild.id, actor.id, action_key, outcome)
        finally:
            await asyncio.sleep(5)
            self._punishing.discard(key)

    # ── Anti-nuke listeners ───────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel):
        await self._record(channel.guild, "channel_delete", channel.id)

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel):
        await self._record(channel.guild, "channel_create", channel.id)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role):
        await self._record(role.guild, "role_delete", role.id)

    @commands.Cog.listener()
    async def on_guild_role_create(self, role):
        await self._record(role.guild, "role_create", role.id)

    @commands.Cog.listener()
    async def on_member_ban(self, guild, user):
        await self._record(guild, "ban", user.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member):
        # Fires for leaves too; the audit target match sorts kicks from quits.
        await self._record(member.guild, "kick", member.id)

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel):
        await self._record(channel.guild, "webhook_create")

    # ── Anti-raid ─────────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        settings = await self._settings_for(member.guild.id)
        if not settings["antiraid_enabled"]:
            return

        guild = member.guild
        now = time.time()

        min_age_days = settings["raid_min_account_age_days"] or 0
        if min_age_days > 0:
            age_days = (utcnow() - member.created_at).days
            if age_days < min_age_days:
                await self._handle_raider(
                    member, settings["raid_action"],
                    f"account is {age_days}d old, minimum is {min_age_days}d",
                )
                return

        if self._raid_until.get(guild.id, 0) > now:
            await self._handle_raider(member, settings["raid_action"], "joined during active raid mode")
            return

        window = settings["raid_join_window"]
        joins = self._joins[guild.id]
        joins.append(now)
        while joins and now - joins[0] > window:
            joins.popleft()

        if len(joins) >= settings["raid_join_count"]:
            joins.clear()
            await self._trigger_raid_mode(guild, settings)

    async def _trigger_raid_mode(self, guild: discord.Guild, settings: dict):
        minutes = settings["raid_mode_minutes"]
        self._raid_until[guild.id] = time.time() + minutes * 60

        e = discord.Embed(
            title="🚨 Raid detected",
            description=(
                f"`{settings['raid_join_count']}` joins inside `{settings['raid_join_window']}`s.\n"
                f"Raid mode is on for **{minutes} minute(s)** — new joins will be "
                f"**{settings['raid_action']}**."
            ),
            color=discord.Color.red(), timestamp=utcnow(),
        )
        e.set_footer(text="raidmode off — to lift it early")
        await self._alert(guild, e)

        if settings["raid_action"] == "lock":
            await self._lock_server(guild, True)

    async def _handle_raider(self, member: discord.Member, action: str, why: str):
        reason = f"Anti-raid: {why}"
        try:
            if action == "ban":
                await member.ban(reason=reason, delete_message_seconds=0)
            elif action == "kick":
                await member.kick(reason=reason)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning("Anti-raid could not act on %s in %s: %s", member.id, member.guild.id, exc)

        e = discord.Embed(
            title="🛡️ Anti-raid",
            description=f"**{member}** (`{member.id}`) — {why}",
            color=discord.Color.orange(), timestamp=utcnow(),
        )
        e.add_field(name="Action", value=action)
        await self._alert(member.guild, e)

    async def _lock_server(self, guild: discord.Guild, locked: bool) -> int:
        changed = 0
        for channel in guild.text_channels:
            try:
                overwrite = channel.overwrites_for(guild.default_role)
                overwrite.send_messages = False if locked else None
                await channel.set_permissions(
                    guild.default_role, overwrite=overwrite,
                    reason="Anti-raid lockdown" if locked else "Anti-raid lift",
                )
                changed += 1
            except discord.HTTPException:
                pass
            await asyncio.sleep(CHANNEL_EDIT_DELAY)
        return changed

    # ── Anti-nuke commands ────────────────────────────────────────────────────

    @commands.group(name="antinuke", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def antinuke(self, ctx: commands.Context):
        """Anti-nuke status and configuration."""
        settings = await self._settings_for(ctx.guild.id)
        thresholds = await self._thresholds_for(ctx.guild.id)
        whitelist = await self._whitelist_for(ctx.guild.id)

        e = discord.Embed(
            title="🛡️ Anti-nuke",
            color=discord.Color.green() if settings["antinuke_enabled"] else discord.Color.dark_grey(),
        )
        e.add_field(name="Status", value="ON ✅" if settings["antinuke_enabled"] else "OFF ❌")
        e.add_field(name="Punishment", value=settings["antinuke_punishment"])
        e.add_field(name="Whitelisted", value=str(len(whitelist)))
        e.add_field(
            name="Thresholds",
            value="\n".join(f"`{k}` — {c} in {w}s" for k, (c, w) in thresholds.items()),
            inline=False,
        )

        me = ctx.guild.me
        problems = []
        if not me.guild_permissions.view_audit_log:
            problems.append("I don't have **View Audit Log** — anti-nuke cannot identify anyone.")
        if not me.guild_permissions.ban_members:
            problems.append("I don't have **Ban Members**.")
        if me.top_role.position < len(ctx.guild.roles) - 2:
            problems.append("My role isn't near the top — I can't punish anyone above it.")
        if problems:
            e.add_field(name="⚠️ Problems", value="\n".join(problems), inline=False)

        e.set_footer(text="Detection only — it reacts after damage starts, and can never touch the owner.")
        await ctx.send(embed=e)

    @antinuke.command(name="on")
    @commands.has_permissions(administrator=True)
    async def antinuke_on(self, ctx: commands.Context):
        """Turn anti-nuke on."""
        await self.bot.db.set_security_field(ctx.guild.id, "antinuke_enabled", True)
        (await self._settings_for(ctx.guild.id))["antinuke_enabled"] = True
        note = ""
        if not ctx.guild.me.guild_permissions.view_audit_log:
            note = "\n\n⚠️ I still need **View Audit Log** or this does nothing."
        await ctx.send(embed=success_embed(f"Anti-nuke enabled.{note}"))

    @antinuke.command(name="off")
    @commands.has_permissions(administrator=True)
    async def antinuke_off(self, ctx: commands.Context):
        """Turn anti-nuke off."""
        await self.bot.db.set_security_field(ctx.guild.id, "antinuke_enabled", False)
        (await self._settings_for(ctx.guild.id))["antinuke_enabled"] = False
        await ctx.send(embed=success_embed("Anti-nuke disabled."))

    @antinuke.command(name="punishment", usage="antinuke punishment <alert|strip|kick|ban>")
    @commands.has_permissions(administrator=True)
    async def antinuke_punishment(self, ctx: commands.Context, mode: str):
        """What happens to someone who trips anti-nuke."""
        mode = mode.lower()
        if mode not in PUNISHMENTS:
            return await ctx.send(embed=error_embed(f"Pick one of: {', '.join(PUNISHMENTS)}."))
        await self.bot.db.set_security_field(ctx.guild.id, "antinuke_punishment", mode)
        (await self._settings_for(ctx.guild.id))["antinuke_punishment"] = mode
        await ctx.send(embed=success_embed(f"Anti-nuke punishment set to **{mode}**."))

    @antinuke.command(name="threshold", usage="antinuke threshold <action> <count> <seconds>")
    @commands.has_permissions(administrator=True)
    async def antinuke_threshold(self, ctx: commands.Context, action: str, count: int, seconds: int):
        """Set how many of an action trip the alarm, and over what window."""
        action = action.lower()
        if action not in NUKE_ACTIONS:
            return await ctx.send(embed=error_embed(
                f"Unknown action. Options: {', '.join(f'`{k}`' for k in NUKE_ACTIONS)}"
            ))
        if not (2 <= count <= 50) or not (5 <= seconds <= 600):
            return await ctx.send(embed=error_embed("Count 2–50, window 5–600 seconds."))

        await self.bot.db.set_nuke_threshold(ctx.guild.id, action, count, seconds)
        (await self._thresholds_for(ctx.guild.id))[action] = (count, seconds)
        await ctx.send(embed=success_embed(f"`{action}` now trips at `{count}` in `{seconds}`s."))

    @antinuke.command(name="whitelist", usage="antinuke whitelist <add|remove|list> [user]")
    @commands.has_permissions(administrator=True)
    async def antinuke_whitelist(self, ctx: commands.Context, mode: str, user: discord.User = None):
        """Exempt trusted users from anti-nuke."""
        mode = mode.lower()
        whitelist = await self._whitelist_for(ctx.guild.id)

        if mode == "list":
            if not whitelist:
                return await ctx.send(embed=info_embed("Nobody is whitelisted. The owner is always exempt."))
            e = discord.Embed(title="🛡️ Anti-nuke whitelist", color=discord.Color.blurple())
            chunk_field(e, "Users", [f"<@{uid}> `{uid}`" for uid in whitelist])
            return await ctx.send(embed=e)

        if user is None:
            return await ctx.send(embed=error_embed("Name a user."))

        if mode == "add":
            await self.bot.db.add_security_whitelist(ctx.guild.id, user.id)
            whitelist.add(user.id)
            return await ctx.send(embed=success_embed(
                f"{user.mention} is exempt from anti-nuke. Only do this for people you'd trust with the server."
            ))
        if mode == "remove":
            await self.bot.db.remove_security_whitelist(ctx.guild.id, user.id)
            whitelist.discard(user.id)
            return await ctx.send(embed=success_embed(f"{user.mention} removed from the whitelist."))

        await ctx.send(embed=error_embed("Use `add`, `remove`, or `list`."))

    # ── Anti-raid commands ────────────────────────────────────────────────────

    @commands.group(name="antiraid", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def antiraid(self, ctx: commands.Context):
        """Anti-raid status and configuration."""
        s = await self._settings_for(ctx.guild.id)
        active = self._raid_until.get(ctx.guild.id, 0) > time.time()

        e = discord.Embed(
            title="🛡️ Anti-raid",
            color=discord.Color.red() if active else (
                discord.Color.green() if s["antiraid_enabled"] else discord.Color.dark_grey()
            ),
        )
        e.add_field(name="Status", value="ON ✅" if s["antiraid_enabled"] else "OFF ❌")
        e.add_field(name="Raid mode", value="ACTIVE 🚨" if active else "idle")
        e.add_field(name="Action", value=s["raid_action"])
        e.add_field(name="Join trigger", value=f"{s['raid_join_count']} in {s['raid_join_window']}s")
        e.add_field(name="Min account age", value=f"{s['raid_min_account_age_days']}d" if s["raid_min_account_age_days"] else "off")
        e.add_field(name="Raid mode length", value=f"{s['raid_mode_minutes']}m")
        await ctx.send(embed=e)

    @antiraid.command(name="on")
    @commands.has_permissions(administrator=True)
    async def antiraid_on(self, ctx: commands.Context):
        """Turn anti-raid on."""
        await self.bot.db.set_security_field(ctx.guild.id, "antiraid_enabled", True)
        (await self._settings_for(ctx.guild.id))["antiraid_enabled"] = True
        await ctx.send(embed=success_embed("Anti-raid enabled."))

    @antiraid.command(name="off")
    @commands.has_permissions(administrator=True)
    async def antiraid_off(self, ctx: commands.Context):
        """Turn anti-raid off."""
        await self.bot.db.set_security_field(ctx.guild.id, "antiraid_enabled", False)
        (await self._settings_for(ctx.guild.id))["antiraid_enabled"] = False
        self._raid_until.pop(ctx.guild.id, None)
        await ctx.send(embed=success_embed("Anti-raid disabled."))

    @antiraid.command(name="joins", usage="antiraid joins <count> <seconds>")
    @commands.has_permissions(administrator=True)
    async def antiraid_joins(self, ctx: commands.Context, count: int, seconds: int):
        """How many joins in how few seconds counts as a raid."""
        if not (3 <= count <= 100) or not (5 <= seconds <= 600):
            return await ctx.send(embed=error_embed("Count 3–100, window 5–600 seconds."))
        await self.bot.db.set_security_field(ctx.guild.id, "raid_join_count", count)
        await self.bot.db.set_security_field(ctx.guild.id, "raid_join_window", seconds)
        s = await self._settings_for(ctx.guild.id)
        s["raid_join_count"], s["raid_join_window"] = count, seconds
        await ctx.send(embed=success_embed(f"Raid trigger set to `{count}` joins in `{seconds}`s."))

    @antiraid.command(name="action", usage="antiraid action <alert|kick|ban|lock>")
    @commands.has_permissions(administrator=True)
    async def antiraid_action(self, ctx: commands.Context, mode: str):
        """What happens to raiders."""
        mode = mode.lower()
        if mode not in RAID_ACTIONS:
            return await ctx.send(embed=error_embed(f"Pick one of: {', '.join(RAID_ACTIONS)}."))
        await self.bot.db.set_security_field(ctx.guild.id, "raid_action", mode)
        (await self._settings_for(ctx.guild.id))["raid_action"] = mode
        await ctx.send(embed=success_embed(f"Raid action set to **{mode}**."))

    @antiraid.command(name="minage", usage="antiraid minage <days>")
    @commands.has_permissions(administrator=True)
    async def antiraid_minage(self, ctx: commands.Context, days: int):
        """Reject accounts younger than this. 0 turns it off."""
        if not 0 <= days <= 365:
            return await ctx.send(embed=error_embed("Days must be 0–365."))
        await self.bot.db.set_security_field(ctx.guild.id, "raid_min_account_age_days", days)
        (await self._settings_for(ctx.guild.id))["raid_min_account_age_days"] = days
        if days == 0:
            return await ctx.send(embed=success_embed("Account age check disabled."))
        await ctx.send(embed=success_embed(f"Accounts newer than `{days}` day(s) will be screened."))

    @antiraid.command(name="duration", usage="antiraid duration <minutes>")
    @commands.has_permissions(administrator=True)
    async def antiraid_duration(self, ctx: commands.Context, minutes: int):
        """How long raid mode stays on once triggered."""
        if not 1 <= minutes <= 240:
            return await ctx.send(embed=error_embed("Minutes must be 1–240."))
        await self.bot.db.set_security_field(ctx.guild.id, "raid_mode_minutes", minutes)
        (await self._settings_for(ctx.guild.id))["raid_mode_minutes"] = minutes
        await ctx.send(embed=success_embed(f"Raid mode will last `{minutes}` minute(s)."))

    @commands.command(name="raidmode", usage="raidmode <on|off>")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def raidmode(self, ctx: commands.Context, state: str):
        """Turn raid mode on or off by hand."""
        state = state.lower()
        s = await self._settings_for(ctx.guild.id)

        if state == "on":
            self._raid_until[ctx.guild.id] = time.time() + s["raid_mode_minutes"] * 60
            if s["raid_action"] == "lock":
                status = await ctx.send(embed=info_embed("Locking channels..."))
                changed = await self._lock_server(ctx.guild, True)
                return await status.edit(embed=success_embed(
                    f"🚨 Raid mode on for `{s['raid_mode_minutes']}`m. Locked `{changed}` channel(s)."
                ))
            return await ctx.send(embed=success_embed(
                f"🚨 Raid mode on for `{s['raid_mode_minutes']}`m. New joins will be **{s['raid_action']}**."
            ))

        if state == "off":
            self._raid_until.pop(ctx.guild.id, None)
            if s["raid_action"] == "lock":
                status = await ctx.send(embed=info_embed("Unlocking channels..."))
                changed = await self._lock_server(ctx.guild, False)
                return await status.edit(embed=success_embed(f"Raid mode off. Unlocked `{changed}` channel(s)."))
            return await ctx.send(embed=success_embed("Raid mode off."))

        await ctx.send(embed=error_embed("Use `raidmode on` or `raidmode off`."))


async def setup(bot):
    await bot.add_cog(Security(bot))
