"""
Backup cog — snapshot and restore server structure.

What a backup contains: roles (name, colour, permissions, hoist, mentionable,
position), categories and channels (name, type, topic, slowmode, NSFW, bitrate,
user limit, position, parent) and their permission overwrites, plus a few
server settings.

What it does NOT and cannot contain: messages, members, who had which role,
emoji or sticker image data, bans, invites, webhooks, or integrations. Discord
exposes no way to restore those. A backup gets your structure back after a
nuke — it does not get your community back.

Restore is additive: it creates what's missing and leaves what exists alone.
That's what you want after a nuke, and it means restoring twice doesn't
duplicate everything.
"""
from __future__ import annotations

import asyncio
import datetime
import io
import json
import logging

import discord
from discord.ext import commands

from utils.helpers import error_embed, info_embed, success_embed
from utils.modui import ConfirmView, chunk_field, clip, utcnow

log = logging.getLogger("cogs.backup")

MAX_BACKUPS_PER_GUILD = 10
RESTORE_DELAY = 0.4

def _ts(value) -> datetime.datetime:
    """created_at is a BIGINT of unix seconds, not a datetime."""
    if isinstance(value, datetime.datetime):
        return value
    return datetime.datetime.fromtimestamp(int(value), datetime.timezone.utc)


def _rel(value) -> str:
    """Discord's own relative-timestamp markdown, straight from a unix int."""
    if isinstance(value, datetime.datetime):
        return f"<t:{int(value.timestamp())}:R>"
    return f"<t:{int(value)}:R>"


CHANNEL_TYPES = {
    "text": discord.ChannelType.text,
    "voice": discord.ChannelType.voice,
    "category": discord.ChannelType.category,
    "news": discord.ChannelType.news,
    "stage_voice": discord.ChannelType.stage_voice,
    "forum": discord.ChannelType.forum,
}


def _serialize_overwrites(channel) -> list[dict]:
    """Role overwrites only. Per-member overwrites reference people who may be gone."""
    out = []
    for target, overwrite in channel.overwrites.items():
        if not isinstance(target, discord.Role):
            continue
        allow, deny = overwrite.pair()
        out.append({"role": target.name, "allow": allow.value, "deny": deny.value})
    return out


class Backup(commands.Cog):
    """💾 Server structure backup and restore."""

    def __init__(self, bot):
        self.bot = bot

    # ── Snapshot ──────────────────────────────────────────────────────────────

    def _snapshot(self, guild: discord.Guild) -> dict:
        roles = [
            {
                "name": r.name,
                "permissions": r.permissions.value,
                "colour": r.colour.value,
                "hoist": r.hoist,
                "mentionable": r.mentionable,
                "position": r.position,
            }
            for r in sorted(guild.roles, key=lambda r: r.position)
            if not r.is_default() and not r.managed
        ]

        channels = []
        for channel in sorted(guild.channels, key=lambda c: (c.position, c.id)):
            entry = {
                "name": channel.name,
                "type": channel.type.name,
                "position": channel.position,
                "parent": channel.category.name if channel.category else None,
                "overwrites": _serialize_overwrites(channel),
            }
            if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
                entry["topic"] = channel.topic
                entry["nsfw"] = channel.is_nsfw()
                entry["slowmode_delay"] = channel.slowmode_delay
            if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
                entry["bitrate"] = channel.bitrate
                entry["user_limit"] = channel.user_limit
            channels.append(entry)

        return {
            "version": 1,
            "guild": {
                "name": guild.name,
                "verification_level": guild.verification_level.name,
                "explicit_content_filter": guild.explicit_content_filter.name,
                "afk_timeout": guild.afk_timeout,
                "system_channel": guild.system_channel.name if guild.system_channel else None,
            },
            "roles": roles,
            "channels": channels,
            "counts": {"roles": len(roles), "channels": len(channels)},
        }

    # ── Commands ──────────────────────────────────────────────────────────────

    @commands.group(name="backup", invoke_without_command=True)
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def backup(self, ctx: commands.Context):
        """Server backup commands."""
        await ctx.send(embed=info_embed(
            "`backup create [name]` · `backup list` · `backup info <id>` · "
            "`backup restore <id>` · `backup export <id>` · `backup delete <id>`"
        ))

    @backup.command(name="create", usage="backup create [name]")
    @commands.has_permissions(administrator=True)
    async def backup_create(self, ctx: commands.Context, *, name: str = None):
        """Snapshot the server's roles, channels, and permissions."""
        existing = await self.bot.db.list_backups(ctx.guild.id)
        if len(existing) >= MAX_BACKUPS_PER_GUILD:
            return await ctx.send(embed=error_embed(
                f"You already have {MAX_BACKUPS_PER_GUILD} backups. Delete one first."
            ))

        async with ctx.typing():
            payload = self._snapshot(ctx.guild)
            label = (name or f"{ctx.guild.name} snapshot")[:80]
            backup_id = await self.bot.db.create_backup(
                ctx.guild.id, ctx.author.id, label, json.dumps(payload)
            )

        e = discord.Embed(title="💾 Backup created", color=discord.Color.green(), timestamp=utcnow())
        e.add_field(name="ID", value=f"`{backup_id}`")
        e.add_field(name="Name", value=clip(label, 200))
        e.add_field(name="Roles", value=str(payload["counts"]["roles"]))
        e.add_field(name="Channels", value=str(payload["counts"]["channels"]))
        e.set_footer(text="Structure only — no messages, members, or role assignments.")
        await ctx.send(embed=e)

    @backup.command(name="list", usage="backup list")
    @commands.has_permissions(administrator=True)
    async def backup_list(self, ctx: commands.Context):
        """List this server's backups."""
        rows = await self.bot.db.list_backups(ctx.guild.id)
        if not rows:
            return await ctx.send(embed=info_embed("No backups yet. Run `backup create`."))

        e = discord.Embed(title="💾 Backups", color=discord.Color.blurple())
        chunk_field(e, "Saved", [
            f"`#{r['id']}` **{r['name']}** — "
            f"{_rel(r['created_at'])} by <@{r['created_by']}>"
            for r in rows
        ])
        await ctx.send(embed=e)

    @backup.command(name="info", usage="backup info <id>")
    @commands.has_permissions(administrator=True)
    async def backup_info(self, ctx: commands.Context, backup_id: int):
        """Show what's inside a backup."""
        row = await self.bot.db.get_backup(backup_id, ctx.guild.id)
        if not row:
            return await ctx.send(embed=error_embed(f"No backup `#{backup_id}` in this server."))

        raw = row["payload"]
        payload = json.loads(raw) if isinstance(raw, str) else raw

        categories = [c for c in payload["channels"] if c["type"] == "category"]
        e = discord.Embed(
            title=f"💾 Backup #{backup_id} — {row['name']}",
            color=discord.Color.blurple(),
            timestamp=_ts(row["created_at"]),
        )
        e.add_field(name="Server name", value=clip(payload["guild"]["name"], 200))
        e.add_field(name="Roles", value=str(len(payload["roles"])))
        e.add_field(name="Channels", value=str(len(payload["channels"])))
        e.add_field(name="Categories", value=str(len(categories)))
        chunk_field(e, "Role names", [r["name"] for r in payload["roles"][:40]], sep=", ")
        await ctx.send(embed=e)

    @backup.command(name="export", usage="backup export <id>")
    @commands.has_permissions(administrator=True)
    async def backup_export(self, ctx: commands.Context, backup_id: int):
        """Download a backup as a JSON file."""
        row = await self.bot.db.get_backup(backup_id, ctx.guild.id)
        if not row:
            return await ctx.send(embed=error_embed(f"No backup `#{backup_id}` in this server."))

        raw = row["payload"]
        text = raw if isinstance(raw, str) else json.dumps(raw, indent=2)
        data = io.BytesIO(text.encode("utf-8"))
        await ctx.send(
            embed=info_embed(f"Backup `#{backup_id}` — keep this somewhere off Discord."),
            file=discord.File(data, filename=f"backup-{ctx.guild.id}-{backup_id}.json"),
        )

    @backup.command(name="delete", usage="backup delete <id>")
    @commands.has_permissions(administrator=True)
    async def backup_delete(self, ctx: commands.Context, backup_id: int):
        """Delete a backup."""
        if await self.bot.db.delete_backup(backup_id, ctx.guild.id):
            return await ctx.send(embed=success_embed(f"Backup `#{backup_id}` deleted."))
        await ctx.send(embed=error_embed(f"No backup `#{backup_id}` in this server."))

    # ── Restore ───────────────────────────────────────────────────────────────

    @backup.command(name="restore", usage="backup restore <id>")
    @commands.has_permissions(administrator=True)
    @commands.bot_has_permissions(manage_roles=True, manage_channels=True)
    async def backup_restore(self, ctx: commands.Context, backup_id: int):
        """Recreate anything from the backup that's currently missing."""
        if ctx.author.id != ctx.guild.owner_id:
            return await ctx.send(embed=error_embed("Only the server owner can restore a backup."))

        row = await self.bot.db.get_backup(backup_id, ctx.guild.id)
        if not row:
            return await ctx.send(embed=error_embed(f"No backup `#{backup_id}` in this server."))

        raw = row["payload"]
        payload = json.loads(raw) if isinstance(raw, str) else raw

        have_roles = {r.name for r in ctx.guild.roles}
        have_channels = {(c.name, c.type.name) for c in ctx.guild.channels}
        missing_roles = [r for r in payload["roles"] if r["name"] not in have_roles]
        missing_channels = [c for c in payload["channels"] if (c["name"], c["type"]) not in have_channels]

        if not missing_roles and not missing_channels:
            return await ctx.send(embed=info_embed(
                "Nothing to restore — every role and channel in that backup already exists."
            ))

        view = ConfirmView(ctx.author.id, timeout=60, confirm_label="Restore")
        prompt = await ctx.send(
            embed=discord.Embed(
                title="💾 Restore backup",
                description=(
                    f"Will create `{len(missing_roles)}` role(s) and `{len(missing_channels)}` channel(s).\n\n"
                    "Existing roles and channels are left alone. Nothing is deleted.\n"
                    "**Messages, members, and role assignments cannot be restored.**"
                ),
                color=discord.Color.orange(),
            ),
            view=view,
        )
        await view.wait()
        if not view.value:
            return await prompt.edit(embed=info_embed("Cancelled."), view=None)

        await prompt.edit(embed=info_embed("💾 Restoring... this takes a while on a large server."), view=None)
        result = await self._do_restore(ctx.guild, payload, missing_roles, missing_channels, prompt)

        e = discord.Embed(title="💾 Restore complete", color=discord.Color.green(), timestamp=utcnow())
        e.add_field(name="Roles created", value=str(result["roles"]))
        e.add_field(name="Channels created", value=str(result["channels"]))
        if result["failed"]:
            chunk_field(e, f"Failed ({len(result['failed'])})", result["failed"][:20])
        e.set_footer(text="Role assignments and messages are gone for good — Discord has no API for them.")
        await prompt.edit(embed=e)

    async def _do_restore(self, guild, payload, missing_roles, missing_channels, prompt) -> dict:
        failed: list[str] = []
        made_roles = made_channels = 0
        top = guild.me.top_role

        # Roles first — channel overwrites reference them by name.
        for spec in sorted(missing_roles, key=lambda r: r["position"]):
            try:
                await guild.create_role(
                    name=spec["name"],
                    permissions=discord.Permissions(spec["permissions"]),
                    colour=discord.Colour(spec["colour"]),
                    hoist=spec["hoist"],
                    mentionable=spec["mentionable"],
                    reason="Backup restore",
                )
                made_roles += 1
            except discord.Forbidden:
                failed.append(f"role `{spec['name']}`: forbidden")
            except discord.HTTPException as exc:
                failed.append(f"role `{spec['name']}`: HTTP {exc.status}")
            await asyncio.sleep(RESTORE_DELAY)

        roles_by_name = {r.name: r for r in guild.roles}

        def overwrites_for(spec) -> dict:
            mapping = {}
            for item in spec.get("overwrites", []):
                role = roles_by_name.get(item["role"])
                if role is None or role >= top:
                    continue
                mapping[role] = discord.PermissionOverwrite.from_pair(
                    discord.Permissions(item["allow"]), discord.Permissions(item["deny"])
                )
            return mapping

        # Categories before their children, so parents resolve.
        ordered = (
            [c for c in missing_channels if c["type"] == "category"]
            + [c for c in missing_channels if c["type"] != "category"]
        )

        for index, spec in enumerate(ordered, start=1):
            try:
                categories = {c.name: c for c in guild.categories}
                parent = categories.get(spec["parent"]) if spec["parent"] else None
                overwrites = overwrites_for(spec)
                kind = spec["type"]

                if kind == "category":
                    await guild.create_category(spec["name"], overwrites=overwrites, reason="Backup restore")
                elif kind in ("voice", "stage_voice"):
                    await guild.create_voice_channel(
                        spec["name"], overwrites=overwrites, category=parent,
                        bitrate=min(spec.get("bitrate") or 64000, guild.bitrate_limit),
                        user_limit=spec.get("user_limit") or 0,
                        reason="Backup restore",
                    )
                elif kind == "forum":
                    await guild.create_forum(
                        spec["name"], overwrites=overwrites, category=parent,
                        topic=spec.get("topic"), reason="Backup restore",
                    )
                else:
                    await guild.create_text_channel(
                        spec["name"], overwrites=overwrites, category=parent,
                        topic=spec.get("topic"), nsfw=spec.get("nsfw", False),
                        slowmode_delay=spec.get("slowmode_delay") or 0,
                        reason="Backup restore",
                    )
                made_channels += 1
            except discord.Forbidden:
                failed.append(f"channel `{spec['name']}`: forbidden")
            except discord.HTTPException as exc:
                failed.append(f"channel `{spec['name']}`: HTTP {exc.status}")

            if index % 10 == 0:
                try:
                    await prompt.edit(embed=info_embed(
                        f"💾 Restoring... `{index}/{len(ordered)}` channels processed."
                    ))
                except discord.HTTPException:
                    pass
            await asyncio.sleep(RESTORE_DELAY)

        return {"roles": made_roles, "channels": made_channels, "failed": failed}


async def setup(bot):
    await bot.add_cog(Backup(bot))
