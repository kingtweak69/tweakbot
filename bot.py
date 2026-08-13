"""
Main bot entry point — loads all cogs and connects to Discord.
"""
import asyncio
import logging
import os
import sys
import uuid
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

import config
from utils.database import Database
from utils.capabilities import CapabilityRegistry

# ── Logging setup ────────────────────────────────────────────────────────────
try:
    import colorlog
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        }
    ))
    logging.root.addHandler(handler)
except ImportError:
    logging.basicConfig(
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

logging.root.setLevel(logging.INFO)
log = logging.getLogger("bot")

# ── Intents ──────────────────────────────────────────────────────────────────
def build_intents() -> discord.Intents:
    """Enable only the gateway events this prefix-command bot actually needs."""
    intents = discord.Intents.default()
    # These are privileged intents. Enable them in the Discord Developer Portal
    # as well, otherwise member-dependent features and prefix commands will not
    # behave reliably.
    intents.members = True
    intents.message_content = True
    return intents


intents = build_intents()

# ── Bot class ────────────────────────────────────────────────────────────────
class Bot(commands.Bot):
    def __init__(self):
        super().__init__(
            command_prefix=self._get_prefix,
            intents=intents,
            help_command=None,
            owner_ids=set(config.OWNER_IDS) if config.OWNER_IDS else None,
            strip_after_prefix=True,
        )
        self.db: Database | None = None
        self.capabilities = CapabilityRegistry(self)
        self.loaded_extensions: list[str] = []
        self.failed_extensions: dict[str, str] = {}

    async def _get_prefix(self, bot: "Bot", message: discord.Message):
        """Per-guild prefix with fallback to config default."""
        if self.db and message.guild:
            prefix = await self.db.get_guild_prefix(message.guild.id)
            return prefix or config.PREFIX
        return config.PREFIX

    async def setup_hook(self):
        # ── Database ─────────────────────────────────────────────────────────
        # All durable bot/agent state lives in PostgreSQL. OAuth credentials are
        # deliberately excluded from the database and remain process-local only.
        self.db = Database(url=config.DATABASE_URL)
        await self.db.setup()
        await self.db.purge_legacy_oauth_storage()
        log.info("PostgreSQL database ready; durable agent state enabled; legacy OAuth storage removed")

        # ── Load cogs ─────────────────────────────────────────────────────────
        cogs_dir = Path(__file__).parent / "cogs"
        for cog_file in sorted(cogs_dir.glob("*.py")):
            if cog_file.stem.startswith("_"):
                continue
            ext = f"cogs.{cog_file.stem}"
            try:
                await self.load_extension(ext)
                self.loaded_extensions.append(ext)
                log.info("Loaded cog: %s", ext)
            except Exception as exc:
                self.failed_extensions[ext] = str(exc)
                log.error("Failed to load cog %s: %s", ext, exc, exc_info=True)

        if self.failed_extensions:
            failed = ", ".join(self.failed_extensions)
            if config.STRICT_COG_LOADING:
                raise RuntimeError(
                    f"Refusing to start with failed cogs: {failed}. "
                    "Fix the errors or set STRICT_COG_LOADING=false for a temporary degraded start."
                )
            log.warning("Starting in degraded mode; unavailable cogs: %s", failed)

        # ── Public API server ─────────────────────────────────────────────────
        # Binds $PORT, which is also what makes Railway expose a public domain.
        # Failure here must not take the bot down: Discord is the primary job.
        try:
            from utils.server import server
            await server.start(self)
        except Exception as exc:
            log.error("API server failed to start: %s", exc, exc_info=True)

        # ── Sync app commands ─────────────────────────────────────────────────
        app_commands = self.tree.get_commands()
        if app_commands:
            try:
                synced = await self.tree.sync()
                log.info("Synced %d slash commands globally", len(synced))
            except Exception as exc:
                log.error("Slash command sync failed: %s", exc, exc_info=True)
        else:
            log.info("No application commands registered; skipping slash-command sync.")

    async def on_ready(self):
        log.info("Logged in as %s (ID: %s)", self.user, self.user.id)
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{config.PREFIX}help | {len(self.guilds)} servers"
            )
        )

    async def close(self):
        """Release the API port and database before Discord shuts down."""
        try:
            from utils.server import server
            await server.stop()
        except Exception as exc:
            log.error("API server failed to stop cleanly: %s", exc, exc_info=True)

        if self.db:
            await self.db.close()
            self.db = None
        await super().close()

    async def on_error(self, event_method: str, *args, **kwargs):
        """Keep listener failures out of Discord while preserving a trace in logs."""
        incident_id = uuid.uuid4().hex[:8]
        log.exception("Unhandled Discord event %s [%s]", event_method, incident_id)

    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingPermissions):
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ You need the following permissions: `{'`, `'.join(error.missing_permissions)}`",
                    color=discord.Color.red()
                )
            )
        elif isinstance(error, commands.BotMissingPermissions):
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ I'm missing permissions: `{'`, `'.join(error.missing_permissions)}`",
                    color=discord.Color.red()
                )
            )
        elif isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ Missing argument: `{error.param.name}`\nUsage: `{ctx.prefix}{ctx.command.usage or ctx.command.name}`",
                    color=discord.Color.red()
                )
            )
        elif isinstance(error, commands.BadArgument):
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ Bad argument: {error}",
                    color=discord.Color.red()
                )
            )
        elif isinstance(error, commands.CommandOnCooldown):
            await ctx.send(
                embed=discord.Embed(
                    description=f"⏱️ Slow down! Try again in `{error.retry_after:.1f}s`.",
                    color=discord.Color.orange()
                )
            )
        elif isinstance(error, commands.MaxConcurrencyReached):
            await ctx.send(
                embed=discord.Embed(
                    description="⏳ That command is already running. Please wait for it to finish.",
                    color=discord.Color.orange()
                )
            )
        elif isinstance(error, commands.NoPrivateMessage):
            await ctx.send(
                embed=discord.Embed(
                    description="❌ This command can only be used in a server.",
                    color=discord.Color.red()
                )
            )
        elif isinstance(error, commands.NotOwner):
            await ctx.send(
                embed=discord.Embed(
                    description="❌ This command is restricted to the bot owner.",
                    color=discord.Color.red()
                )
            )
        elif isinstance(error, commands.DisabledCommand):
            await ctx.send(
                embed=discord.Embed(
                    description="❌ This command is disabled by the bot configuration.",
                    color=discord.Color.red()
                )
            )
        elif isinstance(error, commands.CheckFailure):
            await ctx.send(
                embed=discord.Embed(
                    description="❌ You don't have permission to use this command.",
                    color=discord.Color.red()
                )
            )
        else:
            incident_id = uuid.uuid4().hex[:8]
            log.error(
                "Unhandled command error in %s [%s]: %s",
                ctx.command,
                incident_id,
                error,
                exc_info=True,
            )
            await ctx.send(
                embed=discord.Embed(
                    description=f"❌ An unexpected error occurred. Reference: `{incident_id}`",
                    color=discord.Color.red()
                )
            )

# ── Entry point ──────────────────────────────────────────────────────────────
async def main():
    load_dotenv()
    errors, warnings = config.validate_configuration()
    for warning in warnings:
        log.warning("Configuration: %s", warning)
    if errors:
        for error in errors:
            log.critical("Configuration: %s", error)
        sys.exit(1)

    bot = Bot()
    async with bot:
        await bot.start(config.TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
