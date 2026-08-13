"""
Emoji, sticker, and soundboard cog.
 
Four verbs each, nothing else:

    emoji   steal · create · rename · delete
    sticker steal · create · rename · delete
    sound   steal · create · rename · delete

"steal" takes an asset that exists somewhere else and installs it here.
"create" builds one from a file you supply.
"""
import io
import logging
import re

import aiohttp
import discord
from discord.ext import commands

from utils.helpers import error_embed, success_embed

log = logging.getLogger("cogs.emoji")

CUSTOM_EMOJI_RE = re.compile(r"<(a?):(\w+):(\d+)>")

EMOJI_LIMIT = 256 * 1024    # Discord: 256 KB
STICKER_LIMIT = 512 * 1024  # Discord: 512 KB, PNG/APNG
SOUND_LIMIT = 512 * 1024    # Discord: 512 KB, MP3/OGG, max 5.2 seconds

SWEEP_LIMIT = None          # None = walk the channel all the way back
SWEEP_PROGRESS_EVERY = 2500  # edit the status message every N messages scanned


async def _download(url: str, limit: int) -> bytes:
    """Fetch a file, refusing anything over the Discord limit."""
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
            if resp.status != 200:
                raise ValueError(f"download failed: HTTP {resp.status}")
            if resp.content_length and resp.content_length > limit:
                raise ValueError(
                    f"file is {resp.content_length // 1024} KB, "
                    f"limit is {limit // 1024} KB"
                )
            data = await resp.read()
    if len(data) > limit:
        raise ValueError(
            f"file is {len(data) // 1024} KB, limit is {limit // 1024} KB"
        )
    return data


def _clean_name(raw: str | None, fallback: str, cap: int = 32) -> str:
    """Discord names: letters, digits, underscores, 2-32 characters."""
    name = re.sub(r"[^a-zA-Z0-9_]", "_", (raw or "").strip())
    name = name.strip("_") or fallback
    return name[:cap].ljust(2, "_")


class EmojiCog(commands.Cog, name="Emoji & Stickers"):
    """😀 Emoji, sticker, and soundboard assets."""

    def __init__(self, bot):
        self.bot = bot

    # ── shared helpers ─────────────────────────────────────────────────────────

    async def _source_url(self, ctx: commands.Context, argument: str | None) -> str | None:
        """Find a file: attachment, replied-to message, or a URL argument."""
        if ctx.message.attachments:
            return ctx.message.attachments[0].url

        if ctx.message.reference:
            replied = ctx.message.reference.resolved
            if not isinstance(replied, discord.Message):
                try:
                    replied = await ctx.channel.fetch_message(
                        ctx.message.reference.message_id
                    )
                except Exception:
                    replied = None
            if isinstance(replied, discord.Message):
                if replied.attachments:
                    return replied.attachments[0].url
                if replied.embeds and replied.embeds[0].image:
                    return replied.embeds[0].image.url

        if argument:
            for word in argument.split():
                if word.startswith("http"):
                    return word
        return None

    async def _find_sound(self, ctx: commands.Context, query: str):
        """Look up a soundboard sound by id or name."""
        sounds = await ctx.guild.fetch_soundboard_sounds()
        if query.isdigit():
            match = discord.utils.get(sounds, id=int(query))
            if match:
                return match
        return discord.utils.find(
            lambda s: s.name.lower() == query.lower().strip(), sounds
        )

    async def _sweep_channel(self, ctx: commands.Context, status=None):
        """Walk the channel to its beginning, collecting every emoji and sticker."""
        emojis: dict[str, tuple[str, str, str]] = {}   # id -> (animated, name, id)
        stickers: dict[int, discord.StickerItem] = {}
        scanned = 0

        async for message in ctx.channel.history(limit=SWEEP_LIMIT, oldest_first=False):
            scanned += 1
            if message.id != ctx.message.id:
                for animated, name, emoji_id in CUSTOM_EMOJI_RE.findall(message.content):
                    emojis.setdefault(emoji_id, (animated, name, emoji_id))
                for reaction in message.reactions:
                    emoji = reaction.emoji
                    if isinstance(emoji, (discord.Emoji, discord.PartialEmoji)) and emoji.id:
                        emojis.setdefault(
                            str(emoji.id),
                            ("a" if emoji.animated else "", emoji.name or "stolen", str(emoji.id)),
                        )
                for sticker in message.stickers:
                    stickers.setdefault(sticker.id, sticker)

            if status is not None and scanned % SWEEP_PROGRESS_EVERY == 0:
                try:
                    await status.edit(embed=success_embed(
                        f"Scanning… {scanned:,} messages, "
                        f"{len(emojis)} emoji and {len(stickers)} sticker(s) so far."
                    ))
                except Exception:
                    status = None  # message gone; stop trying

        return list(emojis.values()), list(stickers.values()), scanned

    def _free_emoji_slots(self, ctx: commands.Context) -> tuple[int, int]:
        """(free static slots, free animated slots)."""
        limit = ctx.guild.emoji_limit
        static = sum(1 for e in ctx.guild.emojis if not e.animated)
        animated = sum(1 for e in ctx.guild.emojis if e.animated)
        return max(0, limit - static), max(0, limit - animated)

    # ── Emoji ──────────────────────────────────────────────────────────────────

    @commands.group(name="emoji", aliases=["em"], invoke_without_command=True)
    @commands.guild_only()
    async def emoji(self, ctx: commands.Context):
        """Emoji assets: steal, create, rename, delete."""
        p = ctx.clean_prefix
        await ctx.send(embed=error_embed(
            f"`{p}emoji steal` — read the whole channel and take every emoji in it\n"
            f"`{p}emoji steal <emoji(s)|url> [name]`\n"
            f"`{p}emoji create <name> [url or attachment]`\n"
            f"`{p}emoji rename <emoji> <new_name>`\n"
            f"`{p}emoji delete <emoji>`"
        ))

    @emoji.command(name="steal", aliases=["yoink"], usage="emoji steal [emoji(s)|url] [name]")
    @commands.guild_only()
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def emoji_steal(self, ctx: commands.Context, *, target: str = ""):
        """Steal emoji. Argless sweeps the channel for every emoji it can find."""
        found = CUSTOM_EMOJI_RE.findall(target)
        swept = False

        if not found and ctx.message.reference:
            replied = ctx.message.reference.resolved
            if isinstance(replied, discord.Message):
                found = CUSTOM_EMOJI_RE.findall(replied.content)

        if not found and not target.strip():
            # No target at all — walk the whole channel.
            if not ctx.channel.permissions_for(ctx.me).read_message_history:
                return await ctx.send(embed=error_embed(
                    "I can't read this channel's history, so I can't sweep it."
                ))
            status = await ctx.send(embed=success_embed(
                "Reading this channel back to the beginning. On a large "
                "channel this takes a while."
            ))
            async with ctx.typing():
                found, _, scanned = await self._sweep_channel(ctx, status)
            swept = True
            if not found:
                return await status.edit(embed=error_embed(
                    f"Scanned {scanned:,} messages. No custom emoji anywhere in them."
                ))
            have = {e.id for e in ctx.guild.emojis}
            fresh = [f for f in found if int(f[2]) not in have]
            if not fresh:
                return await status.edit(embed=success_embed(
                    f"Scanned {scanned:,} messages and found {len(found)} emoji — "
                    "the server already has every one of them."
                ))
            found = fresh

        # No custom emoji given — fall back to an image source.
        if not found:
            url = await self._source_url(ctx, target)
            if not url:
                return await ctx.send(embed=error_embed(
                    "Give me a custom emoji like `<:name:id>`, a URL, or an image."
                ))
            words = [w for w in target.split() if not w.startswith("http")]
            return await self._install_emoji(
                ctx, url, _clean_name(words[0] if words else None, "stolen")
            )

        if swept:
            free_static, free_animated = self._free_emoji_slots(ctx)
            trimmed, skipped_full = [], 0
            for entry in found:
                if entry[0]:  # animated
                    if free_animated <= 0:
                        skipped_full += 1
                        continue
                    free_animated -= 1
                else:
                    if free_static <= 0:
                        skipped_full += 1
                        continue
                    free_static -= 1
                trimmed.append(entry)
            note = f" {skipped_full} skipped — no free slots." if skipped_full else ""
            found = trimmed
            if not found:
                return await status.edit(embed=error_embed(
                    f"Found {skipped_full} new emoji, but the server has no free slots."
                ))
            await status.edit(embed=success_embed(
                f"Scanned {scanned:,} messages. Taking {len(found)} new emoji.{note} "
                "Discord rate limits this heavily, so it will be slow."
            ))

        installed, failed = [], []
        for animated, name, emoji_id in found[:] if swept else found[:25]:
            ext = "gif" if animated else "png"
            url = f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}"
            try:
                data = await _download(url, EMOJI_LIMIT)
                created = await ctx.guild.create_custom_emoji(
                    name=_clean_name(name, "stolen"),
                    image=data,
                    reason=f"Stolen by {ctx.author} ({ctx.author.id})",
                )
                installed.append(str(created))
            except discord.HTTPException as exc:
                failed.append(f"{name}: {exc.text or exc}")
            except Exception as exc:
                failed.append(f"{name}: {exc}")

        lines = []
        if installed:
            lines.append(f"Stolen {len(installed)}: " + " ".join(installed))
        if failed:
            lines.append("Failed: " + "; ".join(f[:120] for f in failed[:5]))
        text = "\n".join(lines) or "Nothing to steal."
        await ctx.send(embed=success_embed(text) if installed else error_embed(text))

    @emoji.command(name="create", aliases=["add", "make"], usage="emoji create <name> [url or attachment]")
    @commands.guild_only()
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def emoji_create(self, ctx: commands.Context, name: str, url: str = None):
        """Create an emoji from an attachment or URL."""
        source = await self._source_url(ctx, url)
        if not source:
            return await ctx.send(embed=error_embed("Attach an image or give me a URL."))
        await self._install_emoji(ctx, source, _clean_name(name, "emoji"))

    async def _install_emoji(self, ctx: commands.Context, url: str, name: str):
        try:
            data = await _download(url, EMOJI_LIMIT)
            created = await ctx.guild.create_custom_emoji(
                name=name,
                image=data,
                reason=f"Added by {ctx.author} ({ctx.author.id})",
            )
        except discord.HTTPException as exc:
            return await ctx.send(embed=error_embed(f"Discord refused it: `{exc.text or exc}`"))
        except Exception as exc:
            return await ctx.send(embed=error_embed(str(exc)))
        await ctx.send(embed=success_embed(f"Created {created} `:{created.name}:`"))

    @emoji.command(name="rename", usage="emoji rename <emoji> <new_name>")
    @commands.guild_only()
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def emoji_rename(self, ctx: commands.Context, emoji: discord.Emoji, new_name: str):
        """Rename a custom emoji."""
        if emoji.guild_id != ctx.guild.id:
            return await ctx.send(embed=error_embed("That emoji isn't from this server."))
        old = emoji.name
        cleaned = _clean_name(new_name, old)
        try:
            updated = await emoji.edit(name=cleaned, reason=f"Renamed by {ctx.author}")
        except discord.HTTPException as exc:
            return await ctx.send(embed=error_embed(f"Failed: `{exc.text or exc}`"))
        await ctx.send(embed=success_embed(
            f"`:{old}:` is now `:{updated.name}:` {updated}"
        ))

    @emoji.command(name="delete", aliases=["remove"], usage="emoji delete <emoji>")
    @commands.guild_only()
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def emoji_delete(self, ctx: commands.Context, emoji: discord.Emoji):
        """Delete a custom emoji."""
        if emoji.guild_id != ctx.guild.id:
            return await ctx.send(embed=error_embed("That emoji isn't from this server."))
        name = emoji.name
        try:
            await emoji.delete(reason=f"Deleted by {ctx.author} ({ctx.author.id})")
        except discord.HTTPException as exc:
            return await ctx.send(embed=error_embed(f"Failed: `{exc.text or exc}`"))
        await ctx.send(embed=success_embed(f"Deleted `:{name}:`."))

    # ── Stickers ───────────────────────────────────────────────────────────────

    @commands.group(name="sticker", aliases=["stk"], invoke_without_command=True)
    @commands.guild_only()
    async def sticker(self, ctx: commands.Context):
        """Sticker assets: steal, create, rename, delete."""
        p = ctx.clean_prefix
        await ctx.send(embed=error_embed(
            f"`{p}sticker steal` — read the whole channel and take every sticker in it\n"
            f"`{p}sticker steal [name]` — reply to a message with one, or pass an ID\n"
            f"`{p}sticker create <name> <emoji> [attachment]`\n"
            f"`{p}sticker rename <sticker_id> <new_name>`\n"
            f"`{p}sticker delete <sticker_id>`"
        ))

    @sticker.command(name="steal", aliases=["yoink"], usage="sticker steal [sticker_id] [name]")
    @commands.guild_only()
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def sticker_steal(self, ctx: commands.Context, *, target: str = ""):
        """Steal a sticker by replying to a message with one, or by ID."""
        words = target.split()
        original = None

        if ctx.message.stickers:
            original = await ctx.message.stickers[0].fetch()
        elif words and words[0].isdigit():
            try:
                original = await self.bot.fetch_sticker(int(words[0]))
                words = words[1:]
            except discord.NotFound:
                return await ctx.send(embed=error_embed("Sticker not found."))
        elif ctx.message.reference:
            replied = ctx.message.reference.resolved
            if not isinstance(replied, discord.Message):
                try:
                    replied = await ctx.channel.fetch_message(
                        ctx.message.reference.message_id
                    )
                except Exception:
                    replied = None
            if isinstance(replied, discord.Message) and replied.stickers:
                original = await replied.stickers[0].fetch()

        if not original and not target.strip():
            # Argless — sweep the channel for every sticker.
            if not ctx.channel.permissions_for(ctx.me).read_message_history:
                return await ctx.send(embed=error_embed(
                    "I can't read this channel's history, so I can't sweep it."
                ))
            status = await ctx.send(embed=success_embed(
                "Reading this channel back to the beginning. On a large "
                "channel this takes a while."
            ))
            async with ctx.typing():
                _, sticker_items, scanned = await self._sweep_channel(ctx, status)
            if not sticker_items:
                return await status.edit(embed=error_embed(
                    f"Scanned {scanned:,} messages. No stickers in any of them."
                ))
            have = {s.id for s in await ctx.guild.fetch_stickers()}
            fresh = [s for s in sticker_items if s.id not in have]
            if not fresh:
                return await status.edit(embed=success_embed(
                    f"Scanned {scanned:,} messages and found {len(sticker_items)} "
                    "sticker(s) — the server already has every one."
                ))
            free = max(0, ctx.guild.sticker_limit - len(have))
            if not free:
                return await status.edit(embed=error_embed(
                    f"Found {len(fresh)} new sticker(s), but the server has no free slots."
                ))
            await status.edit(embed=success_embed(
                f"Scanned {scanned:,} messages. Taking {min(len(fresh), free)} "
                "new sticker(s)."
            ))
            return await self._steal_stickers(ctx, fresh[:free])

        if not original:
            return await ctx.send(embed=error_embed(
                "Reply to a message with a sticker, send one with the command, "
                "or pass a sticker ID."
            ))

        if getattr(original, "format", None) == discord.StickerFormatType.lottie:
            return await ctx.send(embed=error_embed(
                "Lottie stickers can't be copied — Discord doesn't allow it."
            ))

        try:
            data = await _download(original.url, STICKER_LIMIT)
            created = await ctx.guild.create_sticker(
                name=_clean_name(words[0] if words else original.name, "sticker", 30),
                description=(getattr(original, "description", "") or "Stolen.")[:100],
                emoji=str(getattr(original, "emoji", "") or "grinning"),
                file=discord.File(io.BytesIO(data), filename="sticker.png"),
                reason=f"Stolen by {ctx.author} ({ctx.author.id})",
            )
        except discord.HTTPException as exc:
            return await ctx.send(embed=error_embed(f"Discord refused it: `{exc.text or exc}`"))
        except Exception as exc:
            return await ctx.send(embed=error_embed(str(exc)))
        await ctx.send(embed=success_embed(f"Stolen sticker **{created.name}** (`{created.id}`)."))

    async def _steal_stickers(self, ctx: commands.Context, items: list):
        """Install a batch of stickers swept from the channel."""
        installed, failed = [], []
        async with ctx.typing():
            for item in items:
                try:
                    full = await item.fetch()
                    if getattr(full, "format", None) == discord.StickerFormatType.lottie:
                        failed.append(f"{item.name}: lottie, can't be copied")
                        continue
                    data = await _download(full.url, STICKER_LIMIT)
                    created = await ctx.guild.create_sticker(
                        name=_clean_name(full.name, "sticker", 30),
                        description=(getattr(full, "description", "") or "Stolen.")[:100],
                        emoji=str(getattr(full, "emoji", "") or "grinning"),
                        file=discord.File(io.BytesIO(data), filename="sticker.png"),
                        reason=f"Swept by {ctx.author} ({ctx.author.id})",
                    )
                    installed.append(created.name)
                except discord.HTTPException as exc:
                    failed.append(f"{item.name}: {exc.text or exc}")
                except Exception as exc:
                    failed.append(f"{item.name}: {exc}")

        lines = []
        if installed:
            lines.append(f"Stolen {len(installed)}: " + ", ".join(f"**{n}**" for n in installed))
        if failed:
            lines.append("Failed: " + "; ".join(f[:120] for f in failed[:5]))
        text = "\n".join(lines) or "Nothing installed."
        await ctx.send(embed=success_embed(text) if installed else error_embed(text))

    @sticker.command(name="create", aliases=["add", "make"], usage="sticker create <name> <emoji> [description]")
    @commands.guild_only()
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def sticker_create(
        self,
        ctx: commands.Context,
        name: str,
        emoji: str = "grinning",
        *,
        description: str = "Custom sticker",
    ):
        """Create a sticker from an attached PNG or APNG, 512 KB max."""
        source = await self._source_url(ctx, None)
        if not source:
            return await ctx.send(embed=error_embed("Attach a PNG or APNG under 512 KB."))
        try:
            data = await _download(source, STICKER_LIMIT)
            created = await ctx.guild.create_sticker(
                name=_clean_name(name, "sticker", 30),
                description=description[:100],
                emoji=emoji.strip(":") or "grinning",
                file=discord.File(io.BytesIO(data), filename="sticker.png"),
                reason=f"Created by {ctx.author} ({ctx.author.id})",
            )
        except discord.HTTPException as exc:
            return await ctx.send(embed=error_embed(f"Discord refused it: `{exc.text or exc}`"))
        except Exception as exc:
            return await ctx.send(embed=error_embed(str(exc)))
        await ctx.send(embed=success_embed(f"Created sticker **{created.name}** (`{created.id}`)."))

    @sticker.command(name="rename", usage="sticker rename <sticker_id> <new_name>")
    @commands.guild_only()
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def sticker_rename(self, ctx: commands.Context, sticker_id: int, new_name: str):
        """Rename a sticker in this server."""
        stickers = await ctx.guild.fetch_stickers()
        stk = discord.utils.get(stickers, id=sticker_id)
        if not stk:
            return await ctx.send(embed=error_embed("No sticker with that ID in this server."))
        old = stk.name
        try:
            updated = await stk.edit(
                name=_clean_name(new_name, old, 30),
                reason=f"Renamed by {ctx.author}",
            )
        except discord.HTTPException as exc:
            return await ctx.send(embed=error_embed(f"Failed: `{exc.text or exc}`"))
        await ctx.send(embed=success_embed(f"**{old}** is now **{updated.name}**."))

    @sticker.command(name="delete", aliases=["remove"], usage="sticker delete <sticker_id>")
    @commands.guild_only()
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def sticker_delete(self, ctx: commands.Context, sticker_id: int):
        """Delete a sticker from this server."""
        stickers = await ctx.guild.fetch_stickers()
        stk = discord.utils.get(stickers, id=sticker_id)
        if not stk:
            return await ctx.send(embed=error_embed("No sticker with that ID in this server."))
        name = stk.name
        try:
            await stk.delete(reason=f"Deleted by {ctx.author} ({ctx.author.id})")
        except discord.HTTPException as exc:
            return await ctx.send(embed=error_embed(f"Failed: `{exc.text or exc}`"))
        await ctx.send(embed=success_embed(f"Deleted sticker **{name}**."))

    # ── Soundboard ─────────────────────────────────────────────────────────────

    @commands.group(name="sound", aliases=["soundboard", "sb"], invoke_without_command=True)
    @commands.guild_only()
    async def sound(self, ctx: commands.Context):
        """Soundboard assets: steal, create, rename, delete."""
        p = ctx.clean_prefix
        await ctx.send(embed=error_embed(
            f"`{p}sound steal <url|attachment> [name]`\n"
            f"`{p}sound create <name> [emoji] [attachment]`\n"
            f"`{p}sound rename <name|id> <new_name>`\n"
            f"`{p}sound delete <name|id>`"
        ))

    @sound.command(name="steal", aliases=["yoink"], usage="sound steal <url|attachment> [name]")
    @commands.guild_only()
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def sound_steal(self, ctx: commands.Context, *, target: str = ""):
        """Steal a soundboard sound from a URL or attachment."""
        source = await self._source_url(ctx, target)
        if not source:
            return await ctx.send(embed=error_embed(
                "Attach an MP3 or OGG, or give me a URL. Discord caps these at "
                "512 KB and 5.2 seconds."
            ))
        words = [w for w in target.split() if not w.startswith("http")]
        await self._install_sound(
            ctx, source, _clean_name(words[0] if words else None, "stolen"), None
        )

    @sound.command(name="create", aliases=["add", "make"], usage="sound create <name> [emoji] [attachment]")
    @commands.guild_only()
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def sound_create(self, ctx: commands.Context, name: str, emoji: str = None):
        """Create a soundboard sound from an attached MP3 or OGG."""
        source = await self._source_url(ctx, None)
        if not source:
            return await ctx.send(embed=error_embed("Attach an MP3 or OGG under 512 KB."))
        await self._install_sound(ctx, source, _clean_name(name, "sound"), emoji)

    async def _install_sound(self, ctx: commands.Context, url: str, name: str, emoji: str | None):
        if not hasattr(ctx.guild, "create_soundboard_sound"):
            return await ctx.send(embed=error_embed(
                "This discord.py build has no soundboard support. Upgrade to 2.5+."
            ))
        try:
            data = await _download(url, SOUND_LIMIT)
            kwargs = {
                "name": name,
                "sound": data,
                "reason": f"Added by {ctx.author} ({ctx.author.id})",
            }
            if emoji:
                kwargs["emoji"] = emoji.strip(":")
            created = await ctx.guild.create_soundboard_sound(**kwargs)
        except discord.HTTPException as exc:
            return await ctx.send(embed=error_embed(
                f"Discord refused it: `{exc.text or exc}`. Sounds must be MP3 or "
                "OGG, under 512 KB and 5.2 seconds."
            ))
        except Exception as exc:
            return await ctx.send(embed=error_embed(str(exc)))
        await ctx.send(embed=success_embed(f"Added sound **{created.name}** (`{created.id}`)."))

    @sound.command(name="rename", usage="sound rename <name|id> <new_name>")
    @commands.guild_only()
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def sound_rename(self, ctx: commands.Context, target: str, new_name: str):
        """Rename a soundboard sound."""
        try:
            found = await self._find_sound(ctx, target)
        except Exception as exc:
            return await ctx.send(embed=error_embed(f"Failed to read soundboard: `{exc}`"))
        if not found:
            return await ctx.send(embed=error_embed("No sound with that name or ID."))
        old = found.name
        try:
            await found.edit(name=_clean_name(new_name, old), reason=f"Renamed by {ctx.author}")
        except discord.HTTPException as exc:
            return await ctx.send(embed=error_embed(f"Failed: `{exc.text or exc}`"))
        await ctx.send(embed=success_embed(f"**{old}** is now **{_clean_name(new_name, old)}**."))

    @sound.command(name="delete", aliases=["remove"], usage="sound delete <name|id>")
    @commands.guild_only()
    @commands.has_permissions(manage_emojis=True)
    @commands.bot_has_permissions(manage_emojis=True)
    async def sound_delete(self, ctx: commands.Context, *, target: str):
        """Delete a soundboard sound."""
        try:
            found = await self._find_sound(ctx, target)
        except Exception as exc:
            return await ctx.send(embed=error_embed(f"Failed to read soundboard: `{exc}`"))
        if not found:
            return await ctx.send(embed=error_embed("No sound with that name or ID."))
        name = found.name
        try:
            await found.delete(reason=f"Deleted by {ctx.author} ({ctx.author.id})")
        except discord.HTTPException as exc:
            return await ctx.send(embed=error_embed(f"Failed: `{exc.text or exc}`"))
        await ctx.send(embed=success_embed(f"Deleted sound **{name}**."))


async def setup(bot):
    await bot.add_cog(EmojiCog(bot))
