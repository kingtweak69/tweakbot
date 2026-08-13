"""Bot configuration loaded from environment variables.

Copy ``.env.example`` to ``.env`` and change only the settings you need. The
defaults favour safe operation: no arbitrary owner eval, no message-content
logging, no unrestricted AI replies, and no direct commits to protected branches.
"""
import os

from dotenv import load_dotenv

load_dotenv()

_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


def _env_bool(name: str, default: bool = False) -> bool:
    """Read a strict boolean environment variable with a useful error."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(
        f"{name} must be one of: true/false, yes/no, on/off, or 1/0."
    )


def _env_int(
    name: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Read a bounded integer environment variable."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer.") from exc

    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}.")
    return value


def _env_float(
    name: str,
    default: float,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    """Read a bounded float environment variable."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        value = default
    else:
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError(f"{name} must be a number.") from exc

    if minimum is not None and value < minimum:
        raise ValueError(f"{name} must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}.")
    return value


def _env_owner_ids() -> list[int]:
    values: list[int] = []
    for raw_id in os.getenv("OWNER_IDS", "").split(","):
        raw_id = raw_id.strip()
        if not raw_id:
            continue
        if not raw_id.isdigit() or int(raw_id) <= 0:
            raise ValueError(
                "OWNER_IDS must be a comma-separated list of positive Discord IDs."
            )
        values.append(int(raw_id))
    return values


def _env_id_tuple(name: str) -> tuple[int, ...]:
    """Read a comma-separated list of Discord snowflake IDs."""
    values: list[int] = []
    for raw_id in os.getenv(name, "").split(","):
        raw_id = raw_id.strip()
        if not raw_id:
            continue
        if not raw_id.isdigit() or int(raw_id) <= 0:
            raise ValueError(
                f"{name} must be a comma-separated list of positive Discord IDs."
            )
        values.append(int(raw_id))
    return tuple(values)


def _env_branches(name: str, default: str) -> tuple[str, ...]:
    raw = os.getenv(name, default)
    return tuple(
        branch.strip()
        for branch in raw.split(",")
        if branch.strip()
    )


# ── Core ────────────────────────────────────────────────────────────────────
TOKEN: str = os.getenv("DISCORD_TOKEN", "").strip()
PREFIX: str = os.getenv("BOT_PREFIX", "!").strip() or "!"
OWNER_IDS: list[int] = _env_owner_ids()
STRICT_COG_LOADING: bool = _env_bool("STRICT_COG_LOADING", True)
SYNC_COMMANDS_ON_STARTUP: bool = _env_bool(
    "SYNC_COMMANDS_ON_STARTUP",
    True,
)
ENABLE_OWNER_EVAL: bool = _env_bool("ENABLE_OWNER_EVAL", False)


# ── Public HTTP server (utils/server.py) ─────────────────────────────────────
# Railway injects PORT and proxies the public domain to it, so this is the one
# that must own it. Anything else in the process needs its own port.
API_PORT: int = _env_int(
    "API_PORT",
    _env_int("PORT", 8080, minimum=1, maximum=65535),
    minimum=1,
    maximum=65535,
)
TWEAKBOT_API_TOKEN: str = os.getenv("TWEAKBOT_API_TOKEN", "").strip()


# ── Lavalink ────────────────────────────────────────────────────────────────
LAVALINK_HOST: str = os.getenv(
    "LAVALINK_HOST",
    "127.0.0.1",
).strip()
LAVALINK_PORT: int = _env_int(
    "LAVALINK_PORT",
    2333,
    minimum=1,
    maximum=65535,
)
LAVALINK_PASSWORD: str = os.getenv(
    "LAVALINK_PASSWORD",
    "youshallnotpass",
)
LAVALINK_SECURE: bool = _env_bool("LAVALINK_SECURE", False)


# ── Database ─────────────────────────────────────────────────────────────────
# PostgreSQL is the durable store for bot state, memory, conversations, jobs, and audit data.
DATABASE_URL: str = os.getenv("DATABASE_URL", "").strip()



# ── Chat AI (OpenAI-compatible) ──────────────────────────────────────────────
# Used by cogs/personality.py. Works with OpenRouter and other compatible APIs.
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_BASE_URL: str = (
    os.getenv(
        "OPENAI_BASE_URL",
        "https://openrouter.ai/api/v1",
    ).strip().rstrip("/")
    or "https://openrouter.ai/api/v1"
)
OPENAI_MODEL: str = (
    os.getenv(
        "OPENAI_MODEL",
        "venice/uncensored",
    ).strip()
    or "venice/uncensored"
)

# Optional OpenRouter identification headers.
OPENROUTER_SITE_URL: str = os.getenv(
    "OPENROUTER_SITE_URL",
    "",
).strip()
OPENROUTER_APP_NAME: str = (
    os.getenv("OPENROUTER_APP_NAME", "TweakBot").strip()
    or "TweakBot"
)

CHAT_MAX_TOKENS: int = _env_int(
    "CHAT_MAX_TOKENS",
    900,
    minimum=64,
    maximum=16_000,
)
CHAT_TEMPERATURE: float = _env_float(
    "CHAT_TEMPERATURE",
    0.7,
    minimum=0.0,
    maximum=2.0,
)
CHAT_REQUEST_TIMEOUT: int = _env_int(
    "CHAT_REQUEST_TIMEOUT",
    90,
    minimum=5,
    maximum=600,
)

# Persistent agent jobs. A max-step value of 0 means no configured step cap.
AGENT_JOB_MAX_STEPS: int = _env_int(
    "AGENT_JOB_MAX_STEPS", 0, minimum=0, maximum=10000
)
AGENT_MAX_ACTIVE_JOBS: int = _env_int(
    "AGENT_MAX_ACTIVE_JOBS", 3, minimum=1, maximum=100
)
AGENT_JOB_POLL_SECONDS: float = _env_float(
    "AGENT_JOB_POLL_SECONDS", 3.0, minimum=1.0, maximum=60.0
)
AGENT_JOB_MAX_TOKENS: int = _env_int(
    "AGENT_JOB_MAX_TOKENS", 1200, minimum=128, maximum=16000
)

# Model-facing persistent-job transcript compaction. Full step records remain in
# PostgreSQL; only the context sent back to the model is compacted.
AGENT_JOB_COMPACT_AFTER_MESSAGES: int = _env_int(
    "AGENT_JOB_COMPACT_AFTER_MESSAGES", 48, minimum=12, maximum=10000
)
AGENT_JOB_COMPACT_AFTER_CHARS: int = _env_int(
    "AGENT_JOB_COMPACT_AFTER_CHARS", 60000, minimum=12000, maximum=2000000
)
AGENT_JOB_COMPACT_KEEP_ROUNDS: int = _env_int(
    "AGENT_JOB_COMPACT_KEEP_ROUNDS", 6, minimum=1, maximum=100
)
AGENT_JOB_COMPACT_SUMMARY_CHARS: int = _env_int(
    "AGENT_JOB_COMPACT_SUMMARY_CHARS", 18000, minimum=4000, maximum=100000
)

# Coding workspaces. Point AGENT_WORKSPACE_ROOT at a persistent volume mount
# (for example /data/tweakbot-workspaces on Railway). OAuth tokens are never
# written here.
AGENT_WORKSPACE_ROOT: str = os.getenv(
    "AGENT_WORKSPACE_ROOT",
    "/data/tweakbot-workspaces",
).strip() or "/data/tweakbot-workspaces"
AGENT_WORKSPACE_PERSISTENT: bool = _env_bool(
    "AGENT_WORKSPACE_PERSISTENT",
    True,
)



# ── Image / vision ───────────────────────────────────────────────────────────
# Existing image cogs still read OPENAI_API_KEY. If you use OpenRouter for chat
# and direct OpenAI for images, update the image cog to read IMAGE_OPENAI_API_KEY.
IMAGE_OPENAI_API_KEY: str = os.getenv(
    "IMAGE_OPENAI_API_KEY",
    "",
).strip()
STABILITY_API_KEY: str = os.getenv(
    "STABILITY_API_KEY",
    "",
).strip()
AI_MODEL: str = os.getenv("AI_MODEL", "gpt-4o").strip() or "gpt-4o"

# Model used by $imagine. Routed through OPENAI_BASE_URL like every other
# OpenAI-shaped call, so it must be a model that endpoint actually lists.
IMAGE_MODEL: str = (
    os.getenv("IMAGE_MODEL", "gpt-5.4-image-2").strip()
    or "gpt-5.4-image-2"
)
IMAGE_SIZE: str = (
    os.getenv("IMAGE_SIZE", "1024x1024").strip() or "1024x1024"
)
# Only sent when set. dall-e-3 wants standard/hd; most other image models
# reject the parameter outright, so the default is to omit it.
IMAGE_QUALITY: str = os.getenv("IMAGE_QUALITY", "").strip()

# Model used by $describe.
VISION_MODEL: str = (
    os.getenv("VISION_MODEL", "qwen3.5-omni-plus").strip()
    or "qwen3.5-omni-plus"
)


# ── Chat scope / cost control ────────────────────────────────────────────────
AI_CHAT_CHANNEL_IDS: tuple[int, ...] = _env_id_tuple(
    "AI_CHAT_CHANNEL_IDS"
)
AI_AUTO_RESPOND: bool = _env_bool("AI_AUTO_RESPOND", False)
AI_RESPOND_TO_MENTIONS: bool = _env_bool(
    "AI_RESPOND_TO_MENTIONS",
    True,
)
AI_RESPOND_IN_DMS: bool = _env_bool("AI_RESPOND_IN_DMS", True)
AI_MODERATION_TOOLS_ENABLED: bool = _env_bool(
    "AI_MODERATION_TOOLS_ENABLED",
    False,
)
AI_MUSIC_TOOLS_ENABLED: bool = _env_bool(
    "AI_MUSIC_TOOLS_ENABLED",
    True,
)
AI_COMMAND_TOOLS_ENABLED: bool = _env_bool(
    "AI_COMMAND_TOOLS_ENABLED",
    False,
)
AI_DESTRUCTIVE_TOOLS_ENABLED: bool = _env_bool(
    "AI_DESTRUCTIVE_TOOLS_ENABLED",
    False,
)
AI_MENTION_COOLDOWN_SECONDS: int = _env_int(
    "AI_MENTION_COOLDOWN_SECONDS",
    8,
    minimum=1,
    maximum=3600,
)
AI_HISTORY_PAIRS: int = _env_int(
    "AI_HISTORY_PAIRS",
    20,
    minimum=1,
    maximum=100,
)
AI_SUMMARY_TRIGGER_MESSAGES: int = _env_int(
    "AI_SUMMARY_TRIGGER_MESSAGES",
    60,
    minimum=12,
    maximum=10000,
)
AI_SUMMARY_KEEP_MESSAGES: int = _env_int(
    "AI_SUMMARY_KEEP_MESSAGES",
    40,
    minimum=6,
    maximum=5000,
)
AI_MAX_INPUT_CHARS: int = _env_int(
    "AI_MAX_INPUT_CHARS",
    4000,
    minimum=100,
    maximum=20_000,
)
AI_GUILD_HOURLY_LIMIT: int = _env_int(
    "AI_GUILD_HOURLY_LIMIT",
    120,
    minimum=0,
    maximum=100_000,
)


# ── GitHub ───────────────────────────────────────────────────────────────────
GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_OAUTH_CLIENT_ID: str = os.getenv(
    "GITHUB_OAUTH_CLIENT_ID",
    "",
).strip()
# Optional: required only if you want TweakBot to revoke GitHub OAuth grants
# automatically on logout. It is the app's secret, never a user's credential.
GITHUB_OAUTH_CLIENT_SECRET: str = os.getenv(
    "GITHUB_OAUTH_CLIENT_SECRET",
    "",
).strip()
GITHUB_PROTECTED_BRANCHES: tuple[str, ...] = _env_branches(
    "GITHUB_PROTECTED_BRANCHES",
    "main,master",
)
GITHUB_ALLOW_PROTECTED_BRANCH_COMMITS: bool = _env_bool(
    "GITHUB_ALLOW_PROTECTED_BRANCH_COMMITS",
    False,
)
GITHUB_ALLOW_PR_MERGE: bool = _env_bool(
    "GITHUB_ALLOW_PR_MERGE",
    False,
)


# ── Railway OAuth ─────────────────────────────────────────────────────────────
RAILWAY_OAUTH_CLIENT_ID: str = os.getenv(
    "RAILWAY_OAUTH_CLIENT_ID",
    "",
).strip()
RAILWAY_OAUTH_CLIENT_SECRET: str = os.getenv(
    "RAILWAY_OAUTH_CLIENT_SECRET",
    "",
).strip()
RAILWAY_OAUTH_SCOPES: str = os.getenv(
    "RAILWAY_OAUTH_SCOPES",
    (
        "openid offline_access email profile workspace:viewer "
        "project:viewer project:member"
    ),
).strip()
OAUTH_PUBLIC_BASE_URL: str = os.getenv(
    "OAUTH_PUBLIC_BASE_URL",
    "",
).strip().rstrip("/")
OAUTH_CALLBACK_HOST: str = os.getenv(
    "OAUTH_CALLBACK_HOST",
    "127.0.0.1",
).strip()
# This used to read PORT first, which meant the OAuth callback grabbed the one
# port Railway proxies the public domain to — and utils/server.py could never
# bind. The callback gets its own port; PORT belongs to the API server.
OAUTH_CALLBACK_PORT: int = _env_int(
    "OAUTH_CALLBACK_PORT",
    8787,
    minimum=1,
    maximum=65535,
)
RAILWAY_OAUTH_REVOKE_URL: str = os.getenv(
    "RAILWAY_OAUTH_REVOKE_URL",
    "",
).strip()


# ── Leveling ─────────────────────────────────────────────────────────────────
XP_PER_MESSAGE_MIN: int = _env_int(
    "XP_PER_MESSAGE_MIN",
    15,
    minimum=0,
    maximum=10_000,
)
XP_PER_MESSAGE_MAX: int = _env_int(
    "XP_PER_MESSAGE_MAX",
    25,
    minimum=0,
    maximum=10_000,
)
XP_COOLDOWN_SECONDS: int = _env_int(
    "XP_COOLDOWN_SECONDS",
    60,
    minimum=0,
    maximum=86_400,
)


# ── Logging / privacy ────────────────────────────────────────────────────────
LOG_CHANNEL_NAME: str = os.getenv("LOG_CHANNEL_NAME", "bot-logs")
MOD_LOG_CHANNEL_NAME: str = os.getenv(
    "MOD_LOG_CHANNEL_NAME",
    "mod-logs",
)
LOG_MESSAGE_CONTENT: bool = _env_bool(
    "LOG_MESSAGE_CONTENT",
    False,
)
LOG_ATTACHMENT_NAMES: bool = _env_bool(
    "LOG_ATTACHMENT_NAMES",
    False,
)

# ── Video generation (cogs/imaging.py: animate, extend) ──────────────────────
# VIDEO_PROVIDER selects a branch in utils/video.py's build_provider().
# VIDEO_MODEL is the model id that provider sends upstream.
VIDEO_PROVIDER: str = (
    os.getenv("VIDEO_PROVIDER", "kling").strip().lower() or "kling"
)
VIDEO_MODEL: str = (
    os.getenv("VIDEO_MODEL", "wan-2.7").strip() or "wan-2.7"
)
VIDEO_API_KEY: str = os.getenv("VIDEO_API_KEY", "").strip()
VIDEO_DURATION_SECONDS: int = _env_int(
    "VIDEO_DURATION_SECONDS",
    5,
    minimum=1,
    maximum=30,
)


# ── Paid media budget ────────────────────────────────────────────────────────
# Per-guild, per-day ceiling in cents across image AND video generation.
# 500 = $5/day/server. 0 disables the cap entirely, which on a public bot is
# how a card gets drained overnight — set it on purpose.
DAILY_MEDIA_CAP_CENTS: int = _env_int(
    "DAILY_MEDIA_CAP_CENTS",
    500,
    minimum=0,
    maximum=1_000_000,
)
# Rough cost of one generated image. DALL-E 3 standard 1024x1024 is ~$0.04.
IMAGE_COST_CENTS: int = _env_int(
    "IMAGE_COST_CENTS",
    4,
    minimum=0,
    maximum=10_000,
)

def validate_configuration() -> tuple[list[str], list[str]]:
    """Return startup errors and operator-facing security warnings."""
    errors: list[str] = []
    warnings: list[str] = []

    if not TOKEN:
        errors.append("DISCORD_TOKEN is not set.")

    if len(PREFIX) > 10:
        errors.append("BOT_PREFIX must be 10 characters or fewer.")

    if XP_PER_MESSAGE_MIN > XP_PER_MESSAGE_MAX:
        errors.append(
            "XP_PER_MESSAGE_MIN cannot be greater than XP_PER_MESSAGE_MAX."
        )

    if API_PORT == OAUTH_CALLBACK_PORT:
        errors.append(
            "API_PORT and OAUTH_CALLBACK_PORT are the same; only one server "
            "can bind a port and the other will fail to start."
        )

    if not DATABASE_URL:
        errors.append("DATABASE_URL is not set. Persistent state requires PostgreSQL.")

    if not OWNER_IDS:
        warnings.append(
            "OWNER_IDS is not set; owner-only commands will rely on "
            "Discord's application owner. Set explicit IDs before public "
            "deployment."
        )

    if ENABLE_OWNER_EVAL:
        warnings.append(
            "ENABLE_OWNER_EVAL is on. The configured bot owner can execute "
            "arbitrary Python on the host."
        )

    if not TWEAKBOT_API_TOKEN:
        warnings.append(
            "TWEAKBOT_API_TOKEN is not set; the public API server will serve "
            "/health but reject every /v1 route."
        )

    if LOG_MESSAGE_CONTENT or LOG_ATTACHMENT_NAMES:
        warnings.append(
            "Sensitive message or attachment metadata logging is enabled. "
            "Restrict access to log channels."
        )

    if not OPENAI_API_KEY:
        warnings.append(
            "OPENAI_API_KEY is not set; AI chat will be unavailable."
        )

    if not OPENAI_BASE_URL:
        warnings.append(
            "OPENAI_BASE_URL is not set; AI chat will be unavailable."
        )

    if not OPENAI_MODEL:
        warnings.append(
            "OPENAI_MODEL is not set; AI chat will be unavailable."
        )

    if AI_AUTO_RESPOND:
        warnings.append(
            "AI_AUTO_RESPOND is on. The bot will reply unprompted in EVERY "
            "readable channel. This can be expensive and noisy."
        )
    elif not AI_CHAT_CHANNEL_IDS:
        warnings.append(
            "No AI_CHAT_CHANNEL_IDS set; the bot only replies to mentions, "
            "replies, and DMs."
        )

    if AI_MODERATION_TOOLS_ENABLED:
        warnings.append(
            "AI moderation tools are enabled. They still check the "
            "requester's Discord permissions."
        )

    if VIDEO_PROVIDER and not VIDEO_API_KEY:
        warnings.append(
            f"VIDEO_PROVIDER is set to {VIDEO_PROVIDER!r} without "
            "VIDEO_API_KEY; animate and extend will stay disabled."
        )

    if GITHUB_TOKEN and not OWNER_IDS:
        warnings.append(
            "GITHUB_TOKEN is configured without explicit OWNER_IDS. "
            "Configure them before using GitHub writes."
        )

    if GITHUB_ALLOW_PROTECTED_BRANCH_COMMITS:
        warnings.append(
            "Direct GitHub commits to protected branches are enabled."
        )

    if GITHUB_ALLOW_PR_MERGE:
        warnings.append(
            "GitHub pull-request merging is enabled."
        )

    return errors, warnings
