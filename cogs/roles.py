"""
Roles cog — create, delete, edit, give, take, autorole, and reaction roles.

Reaction roles:
  - `rr create #channel <title> | <desc>` → the bot posts a new message
  - `rr add <message link or ID>`         → use a message that already exists
"""
import json
import logging
import re

import discord
from discord.ext import commands

from utils.helpers import error_embed, success_embed, info_embed

log = logging.getLogger("cogs.roles")

MAX_REACTION_ROLES = 20  # Discord caps distinct reactions per message

CREATE_RR_TABLE = """
CREATE TABLE IF NOT EXISTS reaction_roles (
    message_id  BIGINT PRIMARY KEY,
    guild_id    BIGINT NOT NULL,
    channel_id  BIGINT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT,
    owned       INTEGER NOT NULL DEFAULT 1,
    roles       TEXT NOT NULL
)
"""

ADD_OWNED_COLUMN = (
    "ALTER TABLE reaction_roles ADD COLUMN owned INTEGER NOT NULL DEFAULT 1"
)

MESSAGE_LINK_RE = re.compile(
    r"https?://(?:canary\\.|ptb\\.)?"
    r"discord(?:app)?\\.com/channels/"
    r"(?P<guild_id>\\d+)/"
    r"(?P<channel_id>\\d+)/"
    r"(?P<message_id>\\d+)"
)

CHANNEL_MESSAGE_RE = re.compile(
    r"^(?:<#)?(?P<channel_id>\\d+)>?"
    r"(?:\\s+|/|:|-)"
    r"(?P<message_id>\\d+)$"
)


def emoji_matches(entry: dict, emoji: discord.PartialEmoji) -> bool:
    """Match a raw reaction against a stored entry."""
    stored_id = entry.get("emoji_id")
    if stored_id:
        return emoji.id is not None and emoji.id == int(stored_id)
    return emoji.id is None and str(emoji) == entry.get("emoji")


class Roles(commands.Cog):
    """🏷️ Role management."""

    def __init__(self, bot):
        self.bot = bot
        # message_id -> {guild_id, channel_id, title, description, owned, entries}
        self._panels: dict[int, dict] = {}

    # ── Reaction role storage ──────────────────────────────────────────────────

    async def cog_load(self):
        if self.bot.db is None:
            log.warning("Database not ready — reaction roles disabled this session.")
            return

        await self.bot.db._execute(CREATE_RR_TABLE)

        if not await self.bot.db._column_exists("reaction_roles", "owned"):
            await self.bot.db._execute(ADD_OWNED_COLUMN)

        rows = await self.bot.db._fetchall("SELECT * FROM reaction_roles")
        for row in rows:
            try:
                entries = json.loads(row["roles"])
            except (TypeError, ValueError):
                entries = []

            self._panels[int(row["message_id"])] = {
                "guild_id": int(row["guild_id"]),
                "channel_id": int(row["channel_id"]),
                "title": row["title"],
                "description": row["description"],
                "owned": bool(row["owned"]),
                "entries": entries,
            }

        log.info("Restored %d reaction role message(s).", len(self._panels))

    async def _insert_panel(self, message_id: int, panel: dict):
        self._panels[message_id] = panel
        await self.bot.db._execute(
            "INSERT INTO reaction_roles "
            "(message_id, guild_id, channel_id, title, description, owned, roles) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                message_id,
                panel["guild_id"],
                panel["channel_id"],
                panel["title"],
                panel["description"],
                int(panel["owned"]),
                json.dumps(panel["entries"]),
            ),
        )

    async def _save_entries(self, message_id: int, entries: list[dict]):
        self._panels[message_id]["entries"] = entries
        await self.bot.db._execute(
            "UPDATE reaction_roles SET roles = ? WHERE message_id = ?",
            (json.dumps(entries), message_id),
        )

    def _build_embed(self, guild: discord.Guild, panel: dict) -> discord.Embed:
        lines = []
        for entry in panel["entries"]:
            role = guild.get_role(int(entry["role_id"]))
            label = entry.get("label") or (role.name if role else "unknown")
            mention = role.mention if role else "*deleted role*"
            lines.append(f"{entry['emoji']} **{label}** — {mention}")

        e = discord.Embed(
            title=panel["title"],
            description=panel["description"] or None,
            color=discord.Color.blurple(),
        )
        e.add_field(
            name="Roles",
            value="\n".join(lines) or "No roles yet. Add some with `reactionrole give`.",
            inline=False,
        )
        e.set_footer(text="React to get the role. Remove your reaction to lose it.")
        return e

    async def _get_message_channel(
        self,
        guild: discord.Guild,
        channel_id: int,
    ):
        """Resolve a text channel or thread, including uncached threads."""
        channel = guild.get_channel_or_thread(channel_id)

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                return None

        channel_guild = getattr(channel, "guild", None)
        if channel_guild is None or channel_guild.id != guild.id:
            return None

        if not hasattr(channel, "fetch_message"):
            return None

        return channel

    async def _fetch_message_from_channel(
        self,
        guild: discord.Guild,
        channel_id: int,
        message_id: int,
    ):
        """Fetch a message from a known channel or thread."""
        channel = await self._get_message_channel(guild, channel_id)
        if channel is None:
            return None

        try:
            return await channel.fetch_message(message_id)
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
        ):
            return None

    async def _resolve_message_reference(
        self,
        ctx: commands.Context,
        reference: str,
    ):
        """
        Resolve a message from a link, channel/message pair, or bare message ID.
        """
        reference = reference.strip().strip("<>")

        link_match = MESSAGE_LINK_RE.search(reference)
        if link_match:
            guild_id = int(link_match.group("guild_id"))
            channel_id = int(link_match.group("channel_id"))
            message_id = int(link_match.group("message_id"))

            if guild_id != ctx.guild.id:
                return None, "That message belongs to another server."

            message = await self._fetch_message_from_channel(
                ctx.guild,
                channel_id,
                message_id,
            )
            if message is None:
                return None, (
                    "I couldn't access that message. Make sure I can view the "
                    "channel and read its message history."
                )

            return message, None

        pair_match = CHANNEL_MESSAGE_RE.fullmatch(reference)
        if pair_match:
            channel_id = int(pair_match.group("channel_id"))
            message_id = int(pair_match.group("message_id"))

            message = await self._fetch_message_from_channel(
                ctx.guild,
                channel_id,
                message_id,
            )
            if message is None:
                return None, (
                    "I couldn't find that message in the specified channel."
                )

            return message, None

        try:
            message_id = int(reference)
        except ValueError:
            return None, (
                "Invalid message reference. Send a message link, a message ID, "
                "or `channel_id/message_id`."
            )

        me = ctx.guild.me
        if me is None:
            return None, "I couldn't resolve my server member information."

        search_channels = []
        seen_channel_ids = set()

        def add_search_channel(channel):
            if channel.id in seen_channel_ids:
                return

            if not hasattr(channel, "fetch_message"):
                return

            permissions = channel.permissions_for(me)
            if not permissions.view_channel:
                return
            if not permissions.read_message_history:
                return

            seen_channel_ids.add(channel.id)
            search_channels.append(channel)

        for channel in ctx.guild.text_channels:
            add_search_channel(channel)

        for thread in ctx.guild.threads:
            add_search_channel(thread)

        # Search the command channel first.
        search_channels.sort(
            key=lambda channel: channel.id != ctx.channel.id
        )

        for channel in search_channels:
            try:
                message = await channel.fetch_message(message_id)
            except (
                discord.NotFound,
                discord.Forbidden,
                discord.HTTPException,
            ):
                continue
            else:
                return message, None

        return None, (
            f"I couldn't find message `{message_id}` in any channel I can read. "
            "Use the full Discord message link for archived threads or faster lookup."
        )

    async def _fetch_message(self, guild: discord.Guild, message_id: int):
        panel = self._panels.get(message_id)
        if panel is None:
            return None

        return await self._fetch_message_from_channel(
            guild,
            int(panel["channel_id"]),
            message_id,
        )

    async def _refresh(self, guild: discord.Guild, message_id: int) -> bool:
        """Rewrite the embed. No-op for messages the bot didn't send."""
        panel = self._panels.get(message_id)
        if panel is None or not panel["owned"]:
            return True
        message = await self._fetch_message(guild, message_id)
        if message is None:
            return False
        await message.edit(embed=self._build_embed(guild, panel))
        return True

    def _resolve_panel(self, ctx: commands.Context, message_id: int):
        """Return (panel, error_message)."""
        panel = self._panels.get(message_id)
        if panel is None:
            return None, (
                f"No reaction roles set up on message `{message_id}`. "
                "Use `rr add <message link>` first."
            )
        if panel["guild_id"] != ctx.guild.id:
            return None, "That message isn't in this server."
        return panel, None

    def _check_role(self, ctx: commands.Context, role: discord.Role):
        """Return an error string, or None if the role is usable."""
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return "You can't use a role equal to or higher than yours."
        if role.managed:
            return "That role is bot-managed. Nobody can assign it."
        if role >= ctx.guild.me.top_role:
            return f"{role.mention} is above me. Move my role higher or pick another."
        return None

    def _parse_emoji(self, guild: discord.Guild, arg: str):
        """
        Resolve a Unicode emoji or custom Discord emoji.

        Accepted custom emoji formats:
          :shadowperson:
          shadowperson
          123456789012345678
          <:shadowperson:123456789012345678>
          <a:shadowperson:123456789012345678>
        """
        arg = arg.strip()

        if not arg:
            return None, None, "Give me an emoji."

        # Raw custom emoji ID.
        if arg.isdigit():
            custom = self.bot.get_emoji(int(arg))

            if custom is None:
                return (
                    None,
                    None,
                    "I can't access a custom emoji with that ID.",
                )

            return str(custom), custom.id, None

        # Full Discord custom emoji markup:
        # <:name:id> or <a:name:id>
        partial = discord.PartialEmoji.from_str(arg)

        if partial.id is not None:
            custom = self.bot.get_emoji(partial.id)

            if custom is None:
                return (
                    None,
                    None,
                    "I can't access that custom emoji. Make sure the bot is "
                    "in a server containing it.",
                )

            return str(custom), custom.id, None

        # Resolve :emoji_name: or emoji_name.
        emoji_name = arg.strip(":")

        # Prefer an emoji from the current server.
        custom = discord.utils.find(
            lambda emoji: emoji.name.lower() == emoji_name.lower(),
            guild.emojis,
        )

        # Fall back to custom emojis from other servers the bot belongs to.
        if custom is None:
            custom = discord.utils.find(
                lambda emoji: emoji.name.lower() == emoji_name.lower(),
                self.bot.emojis,
            )

        if custom is not None:
            return str(custom), custom.id, None

        # If the input looks like a named custom emoji but was not found,
        # return a useful error instead of sending literal ":name:" text.
        if (
            len(arg) >= 3
            and arg.startswith(":")
            and arg.endswith(":")
        ):
            return (
                None,
                None,
                f"I couldn't find a custom emoji named `{emoji_name}`.",
            )

        # Otherwise assume this is a normal Unicode emoji.
        return arg, None, None

    # ── Reaction listeners ─────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction(payload, grant=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        await self._handle_reaction(payload, grant=False)

    async def _handle_reaction(self, payload: discord.RawReactionActionEvent, *, grant: bool):
        panel = self._panels.get(payload.message_id)
        if panel is None or payload.guild_id is None:
            return
        if payload.user_id == self.bot.user.id:
            return

        entry = next((e for e in panel["entries"] if emoji_matches(e, payload.emoji)), None)
        if entry is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        role = guild.get_role(int(entry["role_id"]))
        if role is None or role.managed or role >= guild.me.top_role:
            log.warning("Reaction role %s is unassignable.", entry["role_id"])
            return

        member = payload.member if grant else guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.HTTPException:
                return
        if member.bot:
            return

        try:
            if grant and role not in member.roles:
                await member.add_roles(role, reason="Reaction role")
            elif not grant and role in member.roles:
                await member.remove_roles(role, reason="Reaction role")
        except discord.Forbidden:
            log.warning("Missing Manage Roles for reaction role %s", role.id)
        except discord.HTTPException:
            log.exception("Reaction role toggle failed")

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        if payload.message_id not in self._panels:
            return
        self._panels.pop(payload.message_id, None)
        await self.bot.db._execute(
            "DELETE FROM reaction_roles WHERE message_id = ?", (payload.message_id,)
        )
        log.info("Reaction role message %s deleted.", payload.message_id)

    # ── Reaction roles ─────────────────────────────────────────────────────────

    @commands.group(
        name="reactionrole",
        aliases=["rr", "reactionroles"],
        invoke_without_command=True,
        usage="reactionrole <create|add|edit|delete|give|take|reaction>",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def reactionrole(self, ctx: commands.Context):
        """Self-assignable roles via emoji reactions."""
        await ctx.send(embed=info_embed(
            "**Reaction roles**\n"
            "`rr create <#channel> <title> | <description>` — post a new message\n"
            "`rr add <message link or ID>` — use any accessible existing message\n"
            "`rr edit <message_id> <title> | <description>`\n"
            "`rr delete <message_id>`\n"
            "`rr give <message_id> <role> | <emoji> | <label>`\n"
            "`rr take <message_id> <role>`\n"
            "`rr reaction <message_id> <role> | <new emoji>`\n\n"
            f"Up to {MAX_REACTION_ROLES} roles per message. `edit` only works on "
            "messages I posted myself."
        ))

    @reactionrole.command(name="create", usage="rr create <#channel> <title> | <description>")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def rr_create(self, ctx: commands.Context, channel: discord.TextChannel, *, text: str):
        """Post a new reaction role message."""
        title, _, description = text.partition("|")
        title, description = title.strip(), description.strip()

        if not title:
            return await ctx.send(embed=error_embed("Give it a title."))

        panel = {
            "guild_id": ctx.guild.id,
            "channel_id": channel.id,
            "title": title,
            "description": description,
            "owned": True,
            "entries": [],
        }

        try:
            message = await channel.send(embed=self._build_embed(ctx.guild, panel))
        except discord.Forbidden:
            return await ctx.send(embed=error_embed(f"I can't post in {channel.mention}."))

        await self._insert_panel(message.id, panel)

        await ctx.send(embed=success_embed(
            f"Posted in {channel.mention}.\n"
            f"Message ID: `{message.id}`\n"
            f"Add roles with `rr give {message.id} <role> | <emoji>`"
        ))

    @reactionrole.command(
        name="add",
        aliases=["attach"],
        usage="rr add <message link, message ID, or channel_id/message_id>",
    )
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def rr_add(
        self,
        ctx: commands.Context,
        *,
        reference: str,
    ):
        """Set up reaction roles on any accessible existing message."""
        message, resolve_error = await self._resolve_message_reference(
            ctx,
            reference,
        )
        if resolve_error:
            return await ctx.send(embed=error_embed(resolve_error))

        if message.guild is None or message.guild.id != ctx.guild.id:
            return await ctx.send(
                embed=error_embed("That message isn't in this server.")
            )

        if message.id in self._panels:
            return await ctx.send(
                embed=error_embed(
                    f"`{message.id}` already has reaction roles. "
                    "Use `rr give` to add roles."
                )
            )

        permissions = message.channel.permissions_for(ctx.guild.me)

        missing_permissions = []
        if not permissions.view_channel:
            missing_permissions.append("View Channel")
        if not permissions.read_message_history:
            missing_permissions.append("Read Message History")
        if not permissions.add_reactions:
            missing_permissions.append("Add Reactions")

        if missing_permissions:
            return await ctx.send(
                embed=error_embed(
                    "I am missing these permissions in "
                    f"{message.channel.mention}:\n"
                    f"`{', '.join(missing_permissions)}`"
                )
            )

        owned = message.author.id == self.bot.user.id

        await self._insert_panel(
            message.id,
            {
                "guild_id": ctx.guild.id,
                "channel_id": message.channel.id,
                "title": "",
                "description": "",
                "owned": owned,
                "entries": [],
            },
        )

        ownership_note = ""
        if not owned:
            ownership_note = (
                "\nI didn't send that message, so I cannot edit its text. "
                "Reaction roles will still work normally."
            )

        await ctx.send(
            embed=success_embed(
                f"Reaction roles enabled on "
                f"[this message]({message.jump_url}) in "
                f"{message.channel.mention}.\n"
                f"Message ID: `{message.id}`\n"
                f"Add a role with:\n"
                f"`rr give {message.id} <role> | <emoji>`"
                f"{ownership_note}"
            )
        )

    @reactionrole.command(name="edit", usage="rr edit <message_id> <title> | <description>")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def rr_edit(self, ctx: commands.Context, message_id: int, *, text: str):
        """Change the title and description."""
        panel, err = self._resolve_panel(ctx, message_id)
        if err:
            return await ctx.send(embed=error_embed(err))
        if not panel["owned"]:
            return await ctx.send(embed=error_embed(
                "I didn't post that message, so I can't edit it. "
                "The reactions still work — only the text is out of my hands."
            ))

        title, _, description = text.partition("|")
        title, description = title.strip(), description.strip()

        if not title:
            return await ctx.send(embed=error_embed("Give it a title."))

        panel["title"] = title
        panel["description"] = description
        await self.bot.db._execute(
            "UPDATE reaction_roles SET title = ?, description = ? WHERE message_id = ?",
            (title, description, message_id),
        )

        if not await self._refresh(ctx.guild, message_id):
            return await ctx.send(embed=error_embed("Saved, but I couldn't edit the message."))
        await ctx.send(embed=success_embed(f"Updated `{message_id}`."))

    @reactionrole.command(name="delete", aliases=["detach"], usage="rr delete <message_id>")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def rr_delete(self, ctx: commands.Context, message_id: int):
        """Remove reaction roles. Deletes the message only if I posted it."""
        panel, err = self._resolve_panel(ctx, message_id)
        if err:
            return await ctx.send(embed=error_embed(err))

        message = await self._fetch_message(ctx.guild, message_id)
        if message:
            if panel["owned"]:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
            else:
                for entry in panel["entries"]:
                    try:
                        await message.clear_reaction(entry["emoji"])
                    except discord.HTTPException:
                        pass

        self._panels.pop(message_id, None)
        await self.bot.db._execute(
            "DELETE FROM reaction_roles WHERE message_id = ?", (message_id,)
        )

        verb = "Deleted" if panel["owned"] else "Detached from"
        await ctx.send(embed=success_embed(f"{verb} `{message_id}`."))

    @reactionrole.command(name="give", usage="rr give <message_id> <role> | <emoji> | <label>")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True, add_reactions=True)
    async def rr_give(self, ctx: commands.Context, message_id: int, *, rest: str):
        """Attach a role to an emoji on a message."""
        panel, err = self._resolve_panel(ctx, message_id)
        if err:
            return await ctx.send(embed=error_embed(err))

        entries = list(panel["entries"])
        if len(entries) >= MAX_REACTION_ROLES:
            return await ctx.send(embed=error_embed(
                f"That message already has {MAX_REACTION_ROLES} roles."
            ))

        parts = [p.strip() for p in rest.split("|")]
        if len(parts) < 2 or not parts[1]:
            return await ctx.send(embed=error_embed(
                "Need an emoji: `rr give <message_id> <role> | 😭`"
            ))

        try:
            role = await commands.RoleConverter().convert(ctx, parts[0])
        except commands.BadArgument:
            return await ctx.send(embed=error_embed(f"Role `{parts[0]}` not found."))

        role_err = self._check_role(ctx, role)
        if role_err:
            return await ctx.send(embed=error_embed(role_err))
        if any(int(e["role_id"]) == role.id for e in entries):
            return await ctx.send(embed=error_embed(f"{role.mention} is already on that message."))

        emoji, emoji_id, emoji_err = self._parse_emoji(ctx.guild, parts[1])
        if emoji_err:
            return await ctx.send(embed=error_embed(emoji_err))
        if any(emoji_matches(e, discord.PartialEmoji.from_str(emoji)) for e in entries):
            return await ctx.send(embed=error_embed("That emoji is already used on this message."))

        label = (parts[2] if len(parts) > 2 else "") or role.name

        message = await self._fetch_message(ctx.guild, message_id)
        if message is None:
            return await ctx.send(embed=error_embed("I can't find that message. Was it deleted?"))

        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            return await ctx.send(embed=error_embed(
                f"Discord rejected `{emoji}`. Use a standard emoji or one I can access."
            ))

        entries.append({
            "role_id": role.id,
            "label": label[:80],
            "emoji": emoji,
            "emoji_id": emoji_id,
        })

        await self._save_entries(message_id, entries)
        await self._refresh(ctx.guild, message_id)
        await ctx.send(embed=success_embed(f"{emoji} → {role.mention} on `{message_id}`."))

    @reactionrole.command(name="take", usage="rr take <message_id> <role>")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    async def rr_take(self, ctx: commands.Context, message_id: int, *, role: discord.Role):
        """Detach a role from a message."""
        panel, err = self._resolve_panel(ctx, message_id)
        if err:
            return await ctx.send(embed=error_embed(err))

        target = next((e for e in panel["entries"] if int(e["role_id"]) == role.id), None)
        if target is None:
            return await ctx.send(embed=error_embed(f"{role.mention} isn't on that message."))

        message = await self._fetch_message(ctx.guild, message_id)
        if message:
            try:
                await message.clear_reaction(target["emoji"])
            except discord.HTTPException:
                pass

        entries = [e for e in panel["entries"] if int(e["role_id"]) != role.id]
        await self._save_entries(message_id, entries)
        await self._refresh(ctx.guild, message_id)
        await ctx.send(embed=success_embed(f"Removed {role.mention} from `{message_id}`."))

    @reactionrole.command(name="reaction", aliases=["emoji"], usage="rr reaction <message_id> <role> | <new emoji>")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(add_reactions=True)
    async def rr_reaction(self, ctx: commands.Context, message_id: int, *, rest: str):
        """Change which emoji triggers a role."""
        panel, err = self._resolve_panel(ctx, message_id)
        if err:
            return await ctx.send(embed=error_embed(err))

        parts = [p.strip() for p in rest.split("|")]
        if len(parts) < 2 or not parts[1]:
            return await ctx.send(embed=error_embed(
                "Need the new emoji: `rr reaction <message_id> <role> | 🟢`"
            ))

        try:
            role = await commands.RoleConverter().convert(ctx, parts[0])
        except commands.BadArgument:
            return await ctx.send(embed=error_embed(f"Role `{parts[0]}` not found."))

        entries = [dict(e) for e in panel["entries"]]
        target = next((e for e in entries if int(e["role_id"]) == role.id), None)
        if target is None:
            return await ctx.send(embed=error_embed(f"{role.mention} isn't on that message."))

        emoji, emoji_id, emoji_err = self._parse_emoji(ctx.guild, parts[1])
        if emoji_err:
            return await ctx.send(embed=error_embed(emoji_err))

        new_partial = discord.PartialEmoji.from_str(emoji)
        if any(
            int(e["role_id"]) != role.id and emoji_matches(e, new_partial)
            for e in entries
        ):
            return await ctx.send(embed=error_embed("That emoji is already used on this message."))

        message = await self._fetch_message(ctx.guild, message_id)
        if message is None:
            return await ctx.send(embed=error_embed("I can't find that message. Was it deleted?"))

        old_emoji = target["emoji"]
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            return await ctx.send(embed=error_embed(
                f"Discord rejected `{emoji}`. Use a standard emoji or one I can access."
            ))

        try:
            await message.clear_reaction(old_emoji)
        except discord.HTTPException:
            pass

        target["emoji"] = emoji
        target["emoji_id"] = emoji_id

        await self._save_entries(message_id, entries)
        await self._refresh(ctx.guild, message_id)
        await ctx.send(embed=success_embed(
            f"{role.mention} now uses {emoji} instead of {old_emoji}."
        ))

    # ── Role info ──────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="roleinfo", aliases=["ri"], usage="roleinfo <role>")
    @commands.guild_only()
    async def roleinfo(self, ctx: commands.Context, *, role: discord.Role):
        """Show detailed information about a role."""
        key_perms = []
        p = role.permissions
        if p.administrator:        key_perms.append("Administrator")
        if p.manage_guild:         key_perms.append("Manage Server")
        if p.manage_channels:      key_perms.append("Manage Channels")
        if p.manage_messages:      key_perms.append("Manage Messages")
        if p.manage_roles:         key_perms.append("Manage Roles")
        if p.ban_members:          key_perms.append("Ban Members")
        if p.kick_members:         key_perms.append("Kick Members")
        if p.moderate_members:     key_perms.append("Timeout Members")
        if p.mention_everyone:     key_perms.append("Mention @everyone")

        e = discord.Embed(title=f"🏷️ {role.name}", color=role.color)
        e.add_field(name="ID", value=f"`{role.id}`")
        e.add_field(name="Color", value=str(role.color))
        e.add_field(name="Position", value=str(role.position))
        e.add_field(name="Mentionable", value=str(role.mentionable))
        e.add_field(name="Hoisted", value=str(role.hoist))
        e.add_field(name="Members", value=str(len(role.members)))
        e.add_field(name="Managed", value=str(role.managed))
        e.add_field(name="Created", value=discord.utils.format_dt(role.created_at, "R"))
        if key_perms:
            e.add_field(name="Key Permissions", value=", ".join(key_perms), inline=False)
        await ctx.send(embed=e)

    # ── Give / Take ────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="giverole", aliases=["addrole", "ar"], usage="giverole <member> <role>")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def giverole(self, ctx: commands.Context, member: discord.Member, *, role: discord.Role):
        """Give a role to a member."""
        if role in member.roles:
            return await ctx.send(embed=error_embed(f"{member.mention} already has {role.mention}."))
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(embed=error_embed("You can't assign a role equal to or higher than yours."))
        await member.add_roles(role, reason=f"Added by {ctx.author}")
        await ctx.send(embed=success_embed(f"Gave {role.mention} to {member.mention}."))

    @commands.hybrid_command(name="takerole", aliases=["removerole"], usage="takerole <member> <role>")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def takerole(self, ctx: commands.Context, member: discord.Member, *, role: discord.Role):
        """Remove a role from a member."""
        if role not in member.roles:
            return await ctx.send(embed=error_embed(f"{member.mention} doesn't have {role.mention}."))
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(embed=error_embed("You can't remove a role equal to or higher than yours."))
        await member.remove_roles(role, reason=f"Removed by {ctx.author}")
        await ctx.send(embed=success_embed(f"Removed {role.mention} from {member.mention}."))

    @commands.command(name="massgiverole", usage="massgiverole <role> [--bots|--humans]")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def massgiverole(self, ctx: commands.Context, role: discord.Role, filter_flag: str = None):
        """Give a role to all members (optionally only --bots or --humans)."""
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(embed=error_embed("You can't assign that role."))

        members = ctx.guild.members
        if filter_flag == "--bots":
            members = [m for m in members if m.bot]
        elif filter_flag == "--humans":
            members = [m for m in members if not m.bot]

        async with ctx.typing():
            added = 0
            for m in members:
                if role not in m.roles:
                    try:
                        await m.add_roles(role)
                        added += 1
                    except Exception:
                        pass
        await ctx.send(embed=success_embed(f"Gave {role.mention} to `{added}` members."))

    @commands.command(name="masstakerole", usage="masstakerole <role>")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def masstakerole(self, ctx: commands.Context, role: discord.Role):
        """Remove a role from all members who have it."""
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(embed=error_embed("You can't manage that role."))

        async with ctx.typing():
            removed = 0
            for m in role.members:
                try:
                    await m.remove_roles(role)
                    removed += 1
                except Exception:
                    pass
        await ctx.send(embed=success_embed(f"Removed {role.mention} from `{removed}` members."))

    # ── Create / Delete ────────────────────────────────────────────────────────

    @commands.hybrid_command(name="createrole", usage="createrole <name> [color]")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def createrole(self, ctx: commands.Context, name: str, color: discord.Color = None):
        """Create a new role."""
        role = await ctx.guild.create_role(
            name=name,
            color=color or discord.Color.default(),
            reason=f"Created by {ctx.author}"
        )
        await ctx.send(embed=success_embed(f"Created role {role.mention}."))

    @commands.hybrid_command(name="deleterole", usage="deleterole <role>")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def deleterole(self, ctx: commands.Context, *, role: discord.Role):
        """Delete a role."""
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(embed=error_embed("You can't delete a role equal to or higher than yours."))
        name = role.name
        await role.delete(reason=f"Deleted by {ctx.author}")
        await ctx.send(embed=success_embed(f"Deleted role **@{name}**."))

    @commands.hybrid_command(name="editrole", usage="editrole <role> <field> <value>")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def editrole(self, ctx: commands.Context, role: discord.Role, field: str, *, value: str):
        """
        Edit a role's properties.
        Fields: name, color, hoist, mentionable
        """
        field = field.lower()
        if role >= ctx.author.top_role and ctx.author != ctx.guild.owner:
            return await ctx.send(embed=error_embed("You can't edit a role equal to or higher than yours."))

        try:
            if field == "name":
                await role.edit(name=value)
                await ctx.send(embed=success_embed(f"Renamed role to **@{value}**."))
            elif field == "color":
                try:
                    color = discord.Color(int(value.lstrip("#"), 16))
                except Exception:
                    return await ctx.send(embed=error_embed("Invalid color. Use hex like `#FF5733`."))
                await role.edit(color=color)
                await ctx.send(embed=success_embed(f"Changed {role.mention} color to `{value}`."))
            elif field == "hoist":
                val = value.lower() in ("true", "yes", "1", "on")
                await role.edit(hoist=val)
                await ctx.send(embed=success_embed(f"{'Hoisted' if val else 'Unhoisted'} {role.mention}."))
            elif field == "mentionable":
                val = value.lower() in ("true", "yes", "1", "on")
                await role.edit(mentionable=val)
                await ctx.send(embed=success_embed(f"{role.mention} is {'now' if val else 'no longer'} mentionable."))
            else:
                await ctx.send(embed=error_embed("Valid fields: `name`, `color`, `hoist`, `mentionable`."))
        except discord.Forbidden:
            await ctx.send(embed=error_embed("Missing permissions to edit this role."))

    @commands.hybrid_command(name="rolecolor", usage="rolecolor <role> <hex_color>")
    @commands.guild_only()
    @commands.has_permissions(manage_roles=True)
    @commands.bot_has_permissions(manage_roles=True)
    async def rolecolor(self, ctx: commands.Context, role: discord.Role, hex_color: str):
        """Change a role's color."""
        try:
            color = discord.Color(int(hex_color.lstrip("#"), 16))
        except Exception:
            return await ctx.send(embed=error_embed("Invalid hex color. Example: `#FF5733`"))
        await role.edit(color=color)
        e = discord.Embed(description=f"Changed {role.mention} to color `{hex_color}`", color=color)
        await ctx.send(embed=e)

    # ── Autorole ───────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="autorole", usage="autorole <role|off>")
    @commands.guild_only()
    @commands.has_permissions(administrator=True)
    async def autorole(self, ctx: commands.Context, *, arg: str):
        """Set the role given to new members on join, or 'off' to disable."""
        if arg.lower() == "off":
            await self.bot.db.set_guild_field(ctx.guild.id, "autorole", None)
            return await ctx.send(embed=success_embed("Autorole disabled."))

        # Try to find the role
        role = None
        try:
            role = await commands.RoleConverter().convert(ctx, arg)
        except Exception:
            return await ctx.send(embed=error_embed(f"Role `{arg}` not found."))

        await self.bot.db.set_guild_field(ctx.guild.id, "autorole", role.id)
        await ctx.send(embed=success_embed(f"Autorole set to {role.mention}. New members will receive this role."))

    # ── Members with role ──────────────────────────────────────────────────────

    @commands.hybrid_command(name="inrole", usage="inrole <role>")
    @commands.guild_only()
    async def inrole(self, ctx: commands.Context, *, role: discord.Role):
        """List members with a specific role."""
        members = role.members
        if not members:
            return await ctx.send(embed=info_embed(f"No members have {role.mention}."))

        chunks = [members[i:i+20] for i in range(0, len(members), 20)]
        lines = [f"{m.mention} (`{m.id}`)" for m in chunks[0]]
        e = discord.Embed(
            title=f"Members with @{role.name}",
            description="\n".join(lines),
            color=role.color
        )
        e.set_footer(text=f"{len(members)} total members | Page 1/{len(chunks)}")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="roles", usage="roles [member]")
    @commands.guild_only()
    async def roles(self, ctx: commands.Context, member: discord.Member = None):
        """List all roles in the server, or a member's roles."""
        if member:
            member_roles = [r for r in member.roles if r != ctx.guild.default_role]
            member_roles.sort(key=lambda r: r.position, reverse=True)
            role_list = " ".join(r.mention for r in member_roles[:30])
            e = discord.Embed(
                title=f"Roles for {member.display_name}",
                description=role_list or "No roles",
                color=discord.Color.blurple()
            )
            e.set_footer(text=f"{len(member_roles)} roles")
        else:
            all_roles = [r for r in ctx.guild.roles if r != ctx.guild.default_role]
            all_roles.sort(key=lambda r: r.position, reverse=True)
            lines = [f"{r.mention} — `{len(r.members)}` members" for r in all_roles[:30]]
            e = discord.Embed(
                title=f"Roles in {ctx.guild.name}",
                description="\n".join(lines) or "No roles",
                color=discord.Color.blurple()
            )
            e.set_footer(text=f"{len(all_roles)} total roles")
        await ctx.send(embed=e)


async def setup(bot):
    await bot.add_cog(Roles(bot))
