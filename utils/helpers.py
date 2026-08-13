"""
Shared helper utilities for the bot.
"""
import datetime
import re
import discord
from discord.ext import commands


# ── Duration parsing ──────────────────────────────────────────────────────────

DURATION_RE = re.compile(
    r"(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", re.IGNORECASE
)


def parse_duration(text: str) -> datetime.timedelta | None:
    """
    Parse a duration string like '1d2h30m' into a timedelta.
    Returns None if not parseable.
    """
    m = DURATION_RE.fullmatch(text.strip())
    if not m or not any(m.groups()):
        return None
    days    = int(m.group(1) or 0)
    hours   = int(m.group(2) or 0)
    minutes = int(m.group(3) or 0)
    seconds = int(m.group(4) or 0)
    td = datetime.timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
    return td if td.total_seconds() > 0 else None


def humanize_duration(td: datetime.timedelta) -> str:
    total = int(td.total_seconds())
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, seconds = divmod(rem, 60)
    parts = []
    if days:    parts.append(f"{days}d")
    if hours:   parts.append(f"{hours}h")
    if minutes: parts.append(f"{minutes}m")
    if seconds: parts.append(f"{seconds}s")
    return " ".join(parts) or "0s"


# ── Embed helpers ─────────────────────────────────────────────────────────────

def success_embed(description: str, title: str = None) -> discord.Embed:
    e = discord.Embed(description=description, color=discord.Color.green())
    if title:
        e.title = title
    return e


def error_embed(description: str, title: str = None) -> discord.Embed:
    e = discord.Embed(description=description, color=discord.Color.red())
    if title:
        e.title = title
    return e


def info_embed(description: str, title: str = None) -> discord.Embed:
    e = discord.Embed(description=description, color=discord.Color.blurple())
    if title:
        e.title = title
    return e


def mod_embed(
    action: str,
    target: discord.Member | discord.User,
    moderator: discord.Member,
    reason: str,
    color: discord.Color = discord.Color.orange(),
    **extra,
) -> discord.Embed:
    e = discord.Embed(title=f"🔨 {action}", color=color, timestamp=datetime.datetime.utcnow())
    e.add_field(name="User", value=f"{target.mention} (`{target.id}`)", inline=False)
    e.add_field(name="Moderator", value=f"{moderator.mention}", inline=True)
    e.add_field(name="Reason", value=reason or "No reason provided", inline=True)
    for k, v in extra.items():
        e.add_field(name=k, value=v, inline=True)
    e.set_thumbnail(url=target.display_avatar.url)
    return e


# ── Permission checks ─────────────────────────────────────────────────────────

def is_mod():
    """Check: user has kick members or manage guild permission."""
    async def predicate(ctx: commands.Context) -> bool:
        if not ctx.guild:
            raise commands.NoPrivateMessage()
        p = ctx.author.guild_permissions
        return p.kick_members or p.ban_members or p.manage_guild
    return commands.check(predicate)


def is_admin():
    async def predicate(ctx: commands.Context) -> bool:
        if not ctx.guild:
            raise commands.NoPrivateMessage()
        return ctx.author.guild_permissions.administrator
    return commands.check(predicate)


# ── Misc ──────────────────────────────────────────────────────────────────────

def format_dt(ts: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc)
    return discord.utils.format_dt(dt, style="F")


async def get_or_fetch_user(bot: commands.Bot, user_id: int) -> discord.User | None:
    try:
        return bot.get_user(user_id) or await bot.fetch_user(user_id)
    except Exception:
        return None


def chunks(lst: list, n: int):
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]
