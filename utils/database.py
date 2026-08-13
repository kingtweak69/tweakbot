"""
Async database layer for TweakBot. PostgreSQL only, via asyncpg.

DATABASE_URL is required. There is no local-file fallback on purpose: a SQLite
file on Railway lives on a disk that is destroyed on every redeploy, so it
looks like it works right up until it silently loses everything.

On Railway: add the Postgres plugin, then in the BOT service's Variables add
    DATABASE_URL = ${{Postgres.DATABASE_URL}}
Adding the plugin alone does not expose the variable to the bot service.

Schema is created at startup — nothing to run by hand. All ids are BIGINT
because Discord snowflakes exceed 2^31. All timestamps are BIGINT unix
seconds, matching the existing cogs' format_dt helper.
"""
import json
import logging
import math
import os
import time

import asyncpg

log = logging.getLogger("utils.database")


# Columns callers may update through set_guild_field. Allowlisted because the
# field name is interpolated into the SQL string.
GUILD_FIELDS = frozenset({
    "log_channel", "mod_channel", "jail_role", "mute_role", "autorole", "leveling",
})

SECURITY_FIELDS = frozenset({
    "antinuke_enabled", "antinuke_punishment", "antiraid_enabled",
    "raid_join_count", "raid_join_window", "raid_action",
    "raid_min_account_age_days", "raid_mode_minutes",
})

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS guilds (
        guild_id    BIGINT PRIMARY KEY,
        prefix      TEXT DEFAULT '!',
        log_channel BIGINT,
        mod_channel BIGINT,
        jail_role   BIGINT,
        mute_role   BIGINT,
        autorole    BIGINT,
        leveling    INTEGER DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        user_id  BIGINT,
        guild_id BIGINT,
        xp       BIGINT  DEFAULT 0,
        level    INTEGER DEFAULT 0,
        messages BIGINT  DEFAULT 0,
        last_xp  BIGINT  DEFAULT 0,
        PRIMARY KEY (user_id, guild_id)
    )""",
    """CREATE TABLE IF NOT EXISTS warnings (
        id         BIGSERIAL PRIMARY KEY,
        user_id    BIGINT NOT NULL,
        guild_id   BIGINT NOT NULL,
        mod_id     BIGINT NOT NULL,
        reason     TEXT,
        created_at BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS jailed_users (
        user_id   BIGINT,
        guild_id  BIGINT,
        roles     TEXT,
        jailed_at BIGINT NOT NULL,
        jailed_by BIGINT NOT NULL,
        reason    TEXT,
        PRIMARY KEY (user_id, guild_id)
    )""",
    """CREATE TABLE IF NOT EXISTS counting (
        guild_id   BIGINT PRIMARY KEY,
        channel_id BIGINT,
        count      BIGINT  DEFAULT 0,
        last_user  BIGINT  DEFAULT 0,
        high_score BIGINT  DEFAULT 0,
        strict     INTEGER DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS last_letter (
        guild_id   BIGINT PRIMARY KEY,
        channel_id BIGINT,
        last_word  TEXT,
        last_user  BIGINT  DEFAULT 0,
        active     INTEGER DEFAULT 1
    )""",
    """CREATE TABLE IF NOT EXISTS level_roles (
        guild_id BIGINT,
        level    INTEGER,
        role_id  BIGINT,
        PRIMARY KEY (guild_id, level)
    )""",
    """CREATE TABLE IF NOT EXISTS mod_actions (
        id         BIGSERIAL PRIMARY KEY,
        guild_id   BIGINT NOT NULL,
        action     TEXT   NOT NULL,
        target_id  BIGINT NOT NULL,
        mod_id     BIGINT NOT NULL,
        reason     TEXT,
        created_at BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS tts_settings (
        user_id BIGINT PRIMARY KEY,
        voice   TEXT DEFAULT 'en-US-GuyNeural'
    )""",
    """CREATE TABLE IF NOT EXISTS elevenlabs_voice_settings (
        user_id    BIGINT PRIMARY KEY,
        voice_id   TEXT NOT NULL,
        voice_name TEXT,
        updated_at BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS xp_multipliers (
        guild_id   BIGINT,
        role_id    BIGINT,
        multiplier DOUBLE PRECISION DEFAULT 1.0,
        PRIMARY KEY (guild_id, role_id)
    )""",
    """CREATE TABLE IF NOT EXISTS ai_settings (
        guild_id     BIGINT PRIMARY KEY,
        auto_respond INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS user_targets (
        user_id     BIGINT NOT NULL,
        provider    TEXT   NOT NULL,
        target_json TEXT   NOT NULL,
        updated_at  BIGINT NOT NULL,
        PRIMARY KEY (user_id, provider)
    )""",
    """CREATE TABLE IF NOT EXISTS log_settings (
        guild_id  BIGINT  NOT NULL,
        event_key TEXT    NOT NULL,
        enabled   INTEGER NOT NULL DEFAULT 1,
        PRIMARY KEY (guild_id, event_key)
    )""",
    """CREATE TABLE IF NOT EXISTS security_settings (
        guild_id                  BIGINT PRIMARY KEY,
        antinuke_enabled          INTEGER,
        antinuke_punishment       TEXT,
        antiraid_enabled          INTEGER,
        raid_join_count           INTEGER,
        raid_join_window          INTEGER,
        raid_action               TEXT,
        raid_min_account_age_days INTEGER,
        raid_mode_minutes         INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS nuke_thresholds (
        guild_id       BIGINT  NOT NULL,
        action_key     TEXT    NOT NULL,
        max_count      INTEGER NOT NULL,
        window_seconds INTEGER NOT NULL,
        PRIMARY KEY (guild_id, action_key)
    )""",
    """CREATE TABLE IF NOT EXISTS security_whitelist (
        guild_id BIGINT NOT NULL,
        user_id  BIGINT NOT NULL,
        PRIMARY KEY (guild_id, user_id)
    )""",
    """CREATE TABLE IF NOT EXISTS server_backups (
        id         BIGSERIAL PRIMARY KEY,
        guild_id   BIGINT NOT NULL,
        created_by BIGINT NOT NULL,
        name       TEXT   NOT NULL,
        payload    TEXT   NOT NULL,
        created_at BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS video_jobs (
        id          BIGSERIAL PRIMARY KEY,
        provider    TEXT   NOT NULL,
        external_id TEXT   NOT NULL,
        guild_id    BIGINT NOT NULL,
        channel_id  BIGINT NOT NULL,
        user_id     BIGINT NOT NULL,
        kind        TEXT   NOT NULL,
        prompt      TEXT,
        source_url  TEXT,
        status      TEXT   NOT NULL DEFAULT 'pending',
        result_url  TEXT,
        created_at  BIGINT NOT NULL,
        finished_at BIGINT
    )""",
    """CREATE TABLE IF NOT EXISTS media_spend (
        guild_id  BIGINT  NOT NULL,
        spend_day TEXT    NOT NULL,
        cents     INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, spend_day)
    )""",
    """CREATE TABLE IF NOT EXISTS agent_jobs (
        id          BIGSERIAL PRIMARY KEY,
        user_id     BIGINT NOT NULL,
        guild_id    BIGINT,
        channel_id  BIGINT NOT NULL,
        message_id  BIGINT NOT NULL,
        goal        TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'queued',
        state_json  TEXT NOT NULL DEFAULT '[]',
        result      TEXT,
        last_error  TEXT,
        step_count  INTEGER NOT NULL DEFAULT 0,
        max_steps   INTEGER NOT NULL DEFAULT 0,
        created_at  BIGINT NOT NULL,
        updated_at  BIGINT NOT NULL,
        started_at  BIGINT,
        finished_at BIGINT
    )""",
    """CREATE TABLE IF NOT EXISTS agent_job_steps (
        id          BIGSERIAL PRIMARY KEY,
        job_id      BIGINT NOT NULL REFERENCES agent_jobs(id) ON DELETE CASCADE,
        step_index  INTEGER NOT NULL,
        capability  TEXT NOT NULL,
        arguments   TEXT NOT NULL DEFAULT '{}',
        result      TEXT,
        status      TEXT NOT NULL DEFAULT 'completed',
        created_at  BIGINT NOT NULL,
        UNIQUE(job_id, step_index)
    )""",
    """CREATE TABLE IF NOT EXISTS agent_workspaces (
        workspace_id TEXT PRIMARY KEY,
        user_id      BIGINT NOT NULL,
        guild_id     BIGINT,
        repo         TEXT NOT NULL,
        branch       TEXT NOT NULL,
        root_path    TEXT NOT NULL,
        status       TEXT NOT NULL DEFAULT 'active',
        created_at   BIGINT NOT NULL,
        updated_at   BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS ai_conversation_messages (
        id         BIGSERIAL PRIMARY KEY,
        user_id    BIGINT NOT NULL,
        guild_id   BIGINT NOT NULL DEFAULT 0,
        channel_id BIGINT NOT NULL,
        role       TEXT NOT NULL,
        content    TEXT NOT NULL,
        kind       TEXT NOT NULL DEFAULT 'conversation',
        created_at BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS ai_conversation_summaries (
        user_id    BIGINT NOT NULL,
        guild_id   BIGINT NOT NULL DEFAULT 0,
        channel_id BIGINT NOT NULL,
        summary    TEXT NOT NULL,
        updated_at BIGINT NOT NULL,
        PRIMARY KEY (user_id, guild_id, channel_id)
    )""",
    """CREATE TABLE IF NOT EXISTS ai_memories (
        id           BIGSERIAL PRIMARY KEY,
        owner_user_id BIGINT NOT NULL DEFAULT 0,
        guild_id      BIGINT NOT NULL DEFAULT 0,
        scope         TEXT NOT NULL,
        memory_key    TEXT NOT NULL,
        memory_value  TEXT NOT NULL,
        updated_at    BIGINT NOT NULL,
        UNIQUE(owner_user_id, guild_id, scope, memory_key)
    )""",
    """CREATE TABLE IF NOT EXISTS ai_tool_events (
        id         BIGSERIAL PRIMARY KEY,
        user_id    BIGINT NOT NULL,
        guild_id   BIGINT NOT NULL DEFAULT 0,
        channel_id BIGINT NOT NULL,
        capability TEXT NOT NULL,
        arguments  TEXT NOT NULL DEFAULT '{}',
        result     TEXT NOT NULL DEFAULT '',
        created_at BIGINT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS schema_version (
        id      INTEGER PRIMARY KEY,
        version INTEGER NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_users_guild_xp ON users (guild_id, xp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_warnings_guild_user "
    "ON warnings (guild_id, user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_mod_actions_guild_target "
    "ON mod_actions (guild_id, target_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_backups_guild "
    "ON server_backups (guild_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_video_jobs_pending "
    "ON video_jobs (status, created_at)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_video_jobs_external "
    "ON video_jobs (provider, external_id)",
    "CREATE INDEX IF NOT EXISTS idx_agent_jobs_status_created "
    "ON agent_jobs (status, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_agent_jobs_user_created "
    "ON agent_jobs (user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_agent_workspaces_user_updated "
    "ON agent_workspaces (user_id, updated_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ai_messages_scope_created "
    "ON ai_conversation_messages (user_id, guild_id, channel_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_ai_tool_events_scope_created "
    "ON ai_tool_events (user_id, guild_id, channel_id, created_at DESC)",
]


class Database:
    """PostgreSQL data layer. Rows support row["column"] access."""

    def __init__(self, path: str = None, url: str | None = None):
        # `path` is accepted and ignored so existing bot.py calls keep working.
        self.url = (url or os.getenv("DATABASE_URL", "")).strip()
        # Railway hands out postgres://, asyncpg wants postgresql://
        if self.url.startswith("postgres://"):
            self.url = "postgresql://" + self.url[len("postgres://"):]
        self._pool: asyncpg.Pool | None = None

    @property
    def backend(self) -> str:
        return "postgres"

    # ── low-level access ──────────────────────────────────────────────────────

    async def _fetchone(self, sql: str, *params):
        return await self._pool.fetchrow(sql, *params)

    async def _fetchall(self, sql: str, *params):
        return await self._pool.fetch(sql, *params)

    async def _fetchval(self, sql: str, *params):
        return await self._pool.fetchval(sql, *params)

    async def _execute(self, sql: str, *params) -> int:
        """Run a write. Returns the number of rows affected."""
        status = await self._pool.execute(sql, *params)
        tail = str(status).rsplit(" ", 1)[-1]
        return int(tail) if tail.isdigit() else 0

    # ── setup ─────────────────────────────────────────────────────────────────

    async def setup(self):
        if not self.url:
            raise RuntimeError(
                "DATABASE_URL is not set.\n\n"
                "In your BOT service's Variables on Railway (not the Postgres service), add:\n"
                "    DATABASE_URL = ${{Postgres.DATABASE_URL}}\n"
                "Adding the Postgres plugin alone does not expose the variable to the bot."
            )

        self._pool = await asyncpg.create_pool(
            self.url, min_size=1, max_size=10, command_timeout=30
        )
        async with self._pool.acquire() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)
        log.info("Database ready (postgres) — data persists across redeploys.")
        await self._run_migrations()

    # MIGRATIONS below. Never edit or reorder an existing entry — deployed
    # databases have already run it and will skip it forever.
    MIGRATIONS: list[tuple[int, list[str]]] = [
        # (1, ["ALTER TABLE guilds ADD COLUMN example TEXT"]),
    ]

    async def _column_exists(self, table: str, column: str) -> bool:
        return await self._fetchval(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = $1 AND column_name = $2",
            table, column,
        ) is not None

    async def _add_column(self, table: str, column: str, definition: str):
        """ALTER TABLE ADD COLUMN that is safe to re-run."""
        if not await self._column_exists(table, column):
            await self._execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    async def _schema_version(self) -> int:
        value = await self._fetchval("SELECT version FROM schema_version WHERE id = 1")
        return int(value) if value is not None else 0

    async def _run_migrations(self):
        current = await self._schema_version()
        target = max((version for version, _ in self.MIGRATIONS), default=0)

        if current > target:
            log.warning(
                "Database schema version %d is newer than this build expects (%d). "
                "Running an older bot against a newer database may fail.",
                current, target,
            )
            return
        if current == target:
            log.debug("Database schema up to date (version %d).", current)
            return

        for version, statements in sorted(self.MIGRATIONS):
            if version <= current:
                continue
            log.info("Applying database migration %d...", version)
            try:
                # One transaction per migration — a failure halfway through
                # rolls back rather than leaving a half-migrated schema.
                async with self._pool.acquire() as conn:
                    async with conn.transaction():
                        for statement in statements:
                            await conn.execute(statement)
                        await conn.execute(
                            "INSERT INTO schema_version (id, version) VALUES (1, $1) "
                            "ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version",
                            version,
                        )
            except Exception:
                log.exception("Migration %d failed; database left at version %d.", version, current)
                raise
            current = version
            log.info("Migration %d applied.", version)

    # ── Guild helpers ─────────────────────────────────────────────────────────

    async def get_guild(self, guild_id: int):
        return await self._fetchone("SELECT * FROM guilds WHERE guild_id = $1", guild_id)

    async def ensure_guild(self, guild_id: int):
        await self._execute(
            "INSERT INTO guilds (guild_id) VALUES ($1) ON CONFLICT DO NOTHING", guild_id
        )

    async def get_guild_prefix(self, guild_id: int) -> str | None:
        return await self._fetchval("SELECT prefix FROM guilds WHERE guild_id = $1", guild_id)

    async def set_guild_prefix(self, guild_id: int, prefix: str):
        await self._execute(
            "INSERT INTO guilds (guild_id, prefix) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET prefix = EXCLUDED.prefix",
            guild_id, prefix,
        )

    async def set_guild_field(self, guild_id: int, field: str, value):
        if field not in GUILD_FIELDS:
            raise ValueError(f"Unsupported guild setting: {field}")
        if isinstance(value, bool):
            value = int(value)
        await self._execute(
            f"INSERT INTO guilds (guild_id, {field}) VALUES ($1, $2) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {field} = EXCLUDED.{field}",
            guild_id, value,
        )

    # ── AI settings ───────────────────────────────────────────────────────────

    async def get_ai_auto_response(self, guild_id: int) -> bool | None:
        value = await self._fetchval(
            "SELECT auto_respond FROM ai_settings WHERE guild_id = $1", guild_id
        )
        return bool(value) if value is not None else None

    async def set_ai_auto_response(self, guild_id: int, enabled: bool):
        await self._execute(
            "INSERT INTO ai_settings (guild_id, auto_respond) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET auto_respond = EXCLUDED.auto_respond",
            guild_id, int(enabled),
        )

    # ── Linked OAuth accounts ─────────────────────────────────────────────────

    async def purge_legacy_oauth_storage(self) -> None:
        """Remove the legacy OAuth credential table if it exists.

        Current OAuth sessions are process-local and are never persisted.
        This one-time migration prevents old encrypted tokens from remaining
        in a database after upgrading from older TweakBot releases.
        """
        await self._execute("DROP TABLE IF EXISTS linked_accounts")

    async def set_user_target(self, user_id: int, provider: str, target_json: str):
        await self._execute(
            """INSERT INTO user_targets (user_id, provider, target_json, updated_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id, provider) DO UPDATE SET
                   target_json = EXCLUDED.target_json,
                   updated_at  = EXCLUDED.updated_at""",
            user_id, provider, target_json, int(time.time()),
        )

    async def get_user_target(self, user_id: int, provider: str):
        return await self._fetchval(
            "SELECT target_json FROM user_targets WHERE user_id = $1 AND provider = $2",
            user_id, provider,
        )

    # ── XP / Leveling ─────────────────────────────────────────────────────────

    async def get_user(self, user_id: int, guild_id: int):
        return await self._fetchone(
            "SELECT * FROM users WHERE user_id = $1 AND guild_id = $2", user_id, guild_id
        )

    async def ensure_user(self, user_id: int, guild_id: int):
        await self._execute(
            "INSERT INTO users (user_id, guild_id) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            user_id, guild_id,
        )

    async def add_xp(self, user_id: int, guild_id: int, amount: int) -> tuple[int, int, bool]:
        """
        Add XP and return (new_xp, new_level, leveled_up).

        The increment happens in the database, not read-modify-write. Two
        messages from one user landing together used to read the same xp and
        write the same total, silently dropping a grant.
        """
        row = await self._fetchone(
            """INSERT INTO users (user_id, guild_id, xp, level, messages, last_xp)
               VALUES ($1, $2, $3, 0, 1, $4)
               ON CONFLICT (user_id, guild_id) DO UPDATE SET
                   xp       = users.xp + EXCLUDED.xp,
                   messages = users.messages + 1,
                   last_xp  = EXCLUDED.last_xp
               RETURNING xp, level""",
            user_id, guild_id, amount, int(time.time()),
        )
        new_xp, old_level = int(row["xp"]), int(row["level"])
        new_level = self.xp_to_level(new_xp)
        if new_level != old_level:
            await self._execute(
                "UPDATE users SET level = $1 WHERE user_id = $2 AND guild_id = $3",
                new_level, user_id, guild_id,
            )
        return new_xp, new_level, new_level > old_level

    async def set_xp(self, user_id: int, guild_id: int, amount: int):
        amount = max(0, int(amount))
        await self._execute(
            """INSERT INTO users (user_id, guild_id, xp, level)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id, guild_id) DO UPDATE SET
                   xp = EXCLUDED.xp, level = EXCLUDED.level""",
            user_id, guild_id, amount, self.xp_to_level(amount),
        )

    async def get_leaderboard(self, guild_id: int, limit: int = 10):
        return await self._fetchall(
            "SELECT * FROM users WHERE guild_id = $1 ORDER BY xp DESC LIMIT $2",
            guild_id, limit,
        )

    async def get_leaderboard_page(self, guild_id: int, limit: int, offset: int):
        return await self._fetchall(
            "SELECT user_id, xp, level FROM users WHERE guild_id = $1 "
            "ORDER BY xp DESC, user_id ASC LIMIT $2 OFFSET $3",
            guild_id, limit, offset,
        )

    async def get_rank_position(self, guild_id: int, xp: int) -> int:
        """1-based rank via an indexed count, not by pulling the whole board."""
        ahead = await self._fetchval(
            "SELECT COUNT(*) FROM users WHERE guild_id = $1 AND xp > $2", guild_id, xp
        )
        return (ahead or 0) + 1

    async def reset_leaderboard(self, guild_id: int) -> int:
        return await self._execute("DELETE FROM users WHERE guild_id = $1", guild_id)

    @staticmethod
    def xp_to_level(xp: int) -> int:
        """level = floor(0.1 * sqrt(xp)). Negative xp is clamped so sqrt can't blow up."""
        return int(0.1 * math.sqrt(max(0, xp)))

    @staticmethod
    def level_to_xp(level: int) -> int:
        """XP needed to reach a given level."""
        return (max(0, level) * 10) ** 2

    # ── Warnings ──────────────────────────────────────────────────────────────

    async def add_warning(self, user_id: int, guild_id: int, mod_id: int, reason: str) -> int:
        now = int(time.time())
        warn_id = await self._fetchval(
            "INSERT INTO warnings (user_id, guild_id, mod_id, reason, created_at) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            user_id, guild_id, mod_id, reason, now,
        )
        await self._execute(
            "INSERT INTO mod_actions (guild_id, action, target_id, mod_id, reason, created_at) "
            "VALUES ($1, 'warn', $2, $3, $4, $5)",
            guild_id, user_id, mod_id, reason, now,
        )
        return warn_id

    async def get_warnings(self, user_id: int, guild_id: int):
        return await self._fetchall(
            "SELECT * FROM warnings WHERE user_id = $1 AND guild_id = $2 "
            "ORDER BY created_at DESC",
            user_id, guild_id,
        )

    async def delete_warning(self, warn_id: int, guild_id: int) -> bool:
        return await self._execute(
            "DELETE FROM warnings WHERE id = $1 AND guild_id = $2", warn_id, guild_id
        ) > 0

    async def clear_warnings(self, user_id: int, guild_id: int) -> int:
        return await self._execute(
            "DELETE FROM warnings WHERE user_id = $1 AND guild_id = $2", user_id, guild_id
        )

    # ── Jail ──────────────────────────────────────────────────────────────────

    async def jail_user(
        self, user_id: int, guild_id: int, roles: list[int], jailed_by: int, reason: str
    ):
        await self._execute(
            """INSERT INTO jailed_users (user_id, guild_id, roles, jailed_at, jailed_by, reason)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (user_id, guild_id) DO UPDATE SET
                   roles     = EXCLUDED.roles,
                   jailed_at = EXCLUDED.jailed_at,
                   jailed_by = EXCLUDED.jailed_by,
                   reason    = EXCLUDED.reason""",
            user_id, guild_id, json.dumps(roles), int(time.time()), jailed_by, reason,
        )

    async def get_jailed(self, user_id: int, guild_id: int):
        return await self._fetchone(
            "SELECT * FROM jailed_users WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )

    async def unjail_user(self, user_id: int, guild_id: int):
        await self._execute(
            "DELETE FROM jailed_users WHERE user_id = $1 AND guild_id = $2",
            user_id, guild_id,
        )

    # ── Counting ──────────────────────────────────────────────────────────────

    async def get_counting(self, guild_id: int):
        return await self._fetchone("SELECT * FROM counting WHERE guild_id = $1", guild_id)

    async def set_counting(self, guild_id: int, channel_id: int):
        """Point counting at a channel without resetting count or high score."""
        await self._execute(
            "INSERT INTO counting (guild_id, channel_id) VALUES ($1, $2) "
            "ON CONFLICT (guild_id) DO UPDATE SET channel_id = EXCLUDED.channel_id",
            guild_id, channel_id,
        )

    async def update_counting(self, guild_id: int, count: int, last_user: int, high_score: int):
        await self._execute(
            "UPDATE counting SET count = $1, last_user = $2, "
            "high_score = GREATEST(high_score, $3) WHERE guild_id = $4",
            count, last_user, high_score, guild_id,
        )

    async def reset_counting(self, guild_id: int):
        await self._execute(
            "UPDATE counting SET count = 0, last_user = 0 WHERE guild_id = $1", guild_id
        )

    # ── Last letter ───────────────────────────────────────────────────────────

    async def get_last_letter(self, guild_id: int):
        return await self._fetchone("SELECT * FROM last_letter WHERE guild_id = $1", guild_id)

    async def set_last_letter_channel(self, guild_id: int, channel_id: int):
        """Re-pointing the channel must not wipe the word in play."""
        await self._execute(
            "INSERT INTO last_letter (guild_id, channel_id, active) VALUES ($1, $2, 1) "
            "ON CONFLICT (guild_id) DO UPDATE SET channel_id = EXCLUDED.channel_id, active = 1",
            guild_id, channel_id,
        )

    async def update_last_letter(self, guild_id: int, word: str, user_id: int):
        await self._execute(
            "UPDATE last_letter SET last_word = $1, last_user = $2 WHERE guild_id = $3",
            word, user_id, guild_id,
        )

    async def reset_last_letter(self, guild_id: int):
        await self._execute(
            "UPDATE last_letter SET last_word = NULL, last_user = 0 WHERE guild_id = $1",
            guild_id,
        )

    async def set_last_letter_active(self, guild_id: int, active: bool):
        await self._execute(
            "UPDATE last_letter SET active = $1 WHERE guild_id = $2", int(active), guild_id
        )

    # ── Level roles ───────────────────────────────────────────────────────────

    async def set_level_role(self, guild_id: int, level: int, role_id: int):
        await self._execute(
            "INSERT INTO level_roles (guild_id, level, role_id) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, level) DO UPDATE SET role_id = EXCLUDED.role_id",
            guild_id, level, role_id,
        )

    async def get_level_roles(self, guild_id: int):
        return await self._fetchall(
            "SELECT * FROM level_roles WHERE guild_id = $1 ORDER BY level", guild_id
        )

    async def remove_level_role(self, guild_id: int, level: int):
        await self._execute(
            "DELETE FROM level_roles WHERE guild_id = $1 AND level = $2", guild_id, level
        )

    # ── Mod actions ───────────────────────────────────────────────────────────

    async def log_action(
        self, guild_id: int, action: str, target_id: int, mod_id: int, reason: str = None
    ):
        await self._execute(
            "INSERT INTO mod_actions (guild_id, action, target_id, mod_id, reason, created_at) "
            "VALUES ($1, $2, $3, $4, $5, $6)",
            guild_id, action, target_id, mod_id, reason, int(time.time()),
        )

    async def get_mod_history(self, target_id: int, guild_id: int, limit: int = 20):
        return await self._fetchall(
            "SELECT * FROM mod_actions WHERE target_id = $1 AND guild_id = $2 "
            "ORDER BY created_at DESC LIMIT $3",
            target_id, guild_id, limit,
        )

    # ── Log settings ──────────────────────────────────────────────────────────

    async def get_disabled_log_events(self, guild_id: int):
        return await self._fetchall(
            "SELECT event_key FROM log_settings WHERE guild_id = $1 AND enabled = 0", guild_id
        )

    async def set_log_event(self, guild_id: int, event_key: str, enabled: bool):
        await self._execute(
            "INSERT INTO log_settings (guild_id, event_key, enabled) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, event_key) DO UPDATE SET enabled = EXCLUDED.enabled",
            guild_id, event_key, int(enabled),
        )

    # ── Security settings ─────────────────────────────────────────────────────

    async def get_security(self, guild_id: int):
        return await self._fetchone(
            "SELECT * FROM security_settings WHERE guild_id = $1", guild_id
        )

    async def set_security_field(self, guild_id: int, field: str, value):
        if field not in SECURITY_FIELDS:
            raise ValueError(f"Unsupported security setting: {field}")
        if isinstance(value, bool):
            value = int(value)
        await self._execute(
            f"INSERT INTO security_settings (guild_id, {field}) VALUES ($1, $2) "
            f"ON CONFLICT (guild_id) DO UPDATE SET {field} = EXCLUDED.{field}",
            guild_id, value,
        )

    async def get_nuke_thresholds(self, guild_id: int):
        return await self._fetchall(
            "SELECT action_key, max_count, window_seconds FROM nuke_thresholds "
            "WHERE guild_id = $1",
            guild_id,
        )

    async def set_nuke_threshold(
        self, guild_id: int, action_key: str, max_count: int, window_seconds: int
    ):
        await self._execute(
            """INSERT INTO nuke_thresholds (guild_id, action_key, max_count, window_seconds)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (guild_id, action_key) DO UPDATE SET
                   max_count      = EXCLUDED.max_count,
                   window_seconds = EXCLUDED.window_seconds""",
            guild_id, action_key, max_count, window_seconds,
        )

    async def get_security_whitelist(self, guild_id: int):
        return await self._fetchall(
            "SELECT user_id FROM security_whitelist WHERE guild_id = $1", guild_id
        )

    async def add_security_whitelist(self, guild_id: int, user_id: int):
        await self._execute(
            "INSERT INTO security_whitelist (guild_id, user_id) VALUES ($1, $2) "
            "ON CONFLICT DO NOTHING",
            guild_id, user_id,
        )

    async def remove_security_whitelist(self, guild_id: int, user_id: int):
        await self._execute(
            "DELETE FROM security_whitelist WHERE guild_id = $1 AND user_id = $2",
            guild_id, user_id,
        )

    # ── Server backups ────────────────────────────────────────────────────────

    async def create_backup(self, guild_id: int, created_by: int, name: str, payload: str) -> int:
        return await self._fetchval(
            "INSERT INTO server_backups (guild_id, created_by, name, payload, created_at) "
            "VALUES ($1, $2, $3, $4, $5) RETURNING id",
            guild_id, created_by, name, payload, int(time.time()),
        )

    async def list_backups(self, guild_id: int):
        return await self._fetchall(
            "SELECT id, name, created_by, created_at FROM server_backups "
            "WHERE guild_id = $1 ORDER BY created_at DESC",
            guild_id,
        )

    async def get_backup(self, backup_id: int, guild_id: int):
        return await self._fetchone(
            "SELECT * FROM server_backups WHERE id = $1 AND guild_id = $2",
            backup_id, guild_id,
        )

    async def delete_backup(self, backup_id: int, guild_id: int) -> bool:
        return await self._execute(
            "DELETE FROM server_backups WHERE id = $1 AND guild_id = $2", backup_id, guild_id
        ) > 0

    # ── AI conversation memory ─────────────────────────────────────────────

    async def add_ai_message(
        self, *, user_id: int, guild_id: int, channel_id: int, role: str,
        content: str, kind: str = 'conversation',
    ) -> int:
        return int(await self._fetchval(
            """INSERT INTO ai_conversation_messages
               (user_id, guild_id, channel_id, role, content, kind, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
            user_id, guild_id, channel_id, role, content, kind, int(time.time()),
        ))

    async def get_ai_messages(
        self, *, user_id: int, guild_id: int, channel_id: int, limit: int = 40,
        kinds: tuple[str, ...] = ('conversation',),
    ):
        return await self._fetchall(
            """SELECT * FROM (
                   SELECT * FROM ai_conversation_messages
                   WHERE user_id = $1 AND guild_id = $2 AND channel_id = $3
                     AND kind = ANY($4::text[])
                   ORDER BY id DESC LIMIT $5
               ) recent ORDER BY id ASC""",
            user_id, guild_id, channel_id, list(kinds), max(1, min(int(limit), 500)),
        )

    async def count_ai_messages(
        self, *, user_id: int, guild_id: int, channel_id: int,
    ) -> int:
        return int(await self._fetchval(
            "SELECT COUNT(*) FROM ai_conversation_messages "
            "WHERE user_id = $1 AND guild_id = $2 AND channel_id = $3 "
            "AND kind = 'conversation'",
            user_id, guild_id, channel_id,
        ) or 0)

    async def clear_ai_conversation(
        self, *, user_id: int, guild_id: int, channel_id: int,
    ):
        await self._execute(
            "DELETE FROM ai_conversation_messages WHERE user_id = $1 "
            "AND guild_id = $2 AND channel_id = $3",
            user_id, guild_id, channel_id,
        )
        await self._execute(
            "DELETE FROM ai_conversation_summaries WHERE user_id = $1 "
            "AND guild_id = $2 AND channel_id = $3",
            user_id, guild_id, channel_id,
        )

    async def delete_ai_message(self, message_id: int):
        await self._execute("DELETE FROM ai_conversation_messages WHERE id = $1", message_id)

    async def get_ai_summary(
        self, *, user_id: int, guild_id: int, channel_id: int,
    ) -> str:
        return str(await self._fetchval(
            "SELECT summary FROM ai_conversation_summaries "
            "WHERE user_id = $1 AND guild_id = $2 AND channel_id = $3",
            user_id, guild_id, channel_id,
        ) or '')

    async def set_ai_summary(
        self, *, user_id: int, guild_id: int, channel_id: int, summary: str,
    ):
        await self._execute(
            """INSERT INTO ai_conversation_summaries
               (user_id, guild_id, channel_id, summary, updated_at)
               VALUES ($1, $2, $3, $4, $5)
               ON CONFLICT (user_id, guild_id, channel_id) DO UPDATE SET
                   summary = EXCLUDED.summary, updated_at = EXCLUDED.updated_at""",
            user_id, guild_id, channel_id, summary, int(time.time()),
        )

    async def compact_ai_messages(
        self, *, user_id: int, guild_id: int, channel_id: int, keep_last: int,
    ) -> int:
        return await self._execute(
            """DELETE FROM ai_conversation_messages
               WHERE user_id = $1 AND guild_id = $2 AND channel_id = $3
                 AND kind = 'conversation'
                 AND id NOT IN (
                     SELECT id FROM ai_conversation_messages
                     WHERE user_id = $1 AND guild_id = $2 AND channel_id = $3
                       AND kind = 'conversation'
                     ORDER BY id DESC LIMIT $4
                 )""",
            user_id, guild_id, channel_id, max(1, int(keep_last)),
        )

    async def set_ai_memory(
        self, *, owner_user_id: int, guild_id: int, scope: str, key: str, value: str,
    ):
        await self._execute(
            """INSERT INTO ai_memories
               (owner_user_id, guild_id, scope, memory_key, memory_value, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6)
               ON CONFLICT (owner_user_id, guild_id, scope, memory_key) DO UPDATE SET
                   memory_value = EXCLUDED.memory_value, updated_at = EXCLUDED.updated_at""",
            owner_user_id, guild_id, scope, key, value, int(time.time()),
        )

    async def get_ai_memories(
        self, *, owner_user_id: int, guild_id: int, scopes: tuple[str, ...], limit: int = 100,
    ):
        return await self._fetchall(
            "SELECT * FROM ai_memories WHERE owner_user_id = $1 AND guild_id = $2 "
            "AND scope = ANY($3::text[]) ORDER BY updated_at DESC LIMIT $4",
            owner_user_id, guild_id, list(scopes), max(1, min(int(limit), 500)),
        )

    async def delete_ai_memory(
        self, *, owner_user_id: int, guild_id: int, scope: str, key: str,
    ) -> bool:
        return await self._execute(
            "DELETE FROM ai_memories WHERE owner_user_id = $1 AND guild_id = $2 "
            "AND scope = $3 AND memory_key = $4",
            owner_user_id, guild_id, scope, key,
        ) > 0

    async def log_ai_tool_event(
        self, *, user_id: int, guild_id: int, channel_id: int,
        capability: str, arguments: str, result: str,
    ):
        await self._execute(
            """INSERT INTO ai_tool_events
               (user_id, guild_id, channel_id, capability, arguments, result, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7)""",
            user_id, guild_id, channel_id, capability, arguments, result, int(time.time()),
        )

    # ── ElevenLabs voice preferences ───────────────────────────────────────

    async def get_elevenlabs_voice(self, user_id: int):
        return await self._fetchone(
            "SELECT voice_id, voice_name, updated_at FROM elevenlabs_voice_settings "
            "WHERE user_id = $1",
            int(user_id),
        )

    async def set_elevenlabs_voice(
        self, *, user_id: int, voice_id: str, voice_name: str = "",
    ):
        await self._execute(
            """INSERT INTO elevenlabs_voice_settings
               (user_id, voice_id, voice_name, updated_at)
               VALUES ($1, $2, $3, $4)
               ON CONFLICT (user_id) DO UPDATE SET
                   voice_id = EXCLUDED.voice_id,
                   voice_name = EXCLUDED.voice_name,
                   updated_at = EXCLUDED.updated_at""",
            int(user_id), str(voice_id), str(voice_name or ""), int(time.time()),
        )

    async def clear_elevenlabs_voice(self, user_id: int) -> bool:
        return await self._execute(
            "DELETE FROM elevenlabs_voice_settings WHERE user_id = $1", int(user_id)
        ) > 0

    # ── Persistent agent jobs ───────────────────────────────────────────────

    async def create_agent_job(
        self, *, user_id: int, guild_id: int | None, channel_id: int,
        message_id: int, goal: str, max_steps: int = 0,
    ) -> int:
        now = int(time.time())
        return int(await self._fetchval(
            """INSERT INTO agent_jobs
               (user_id, guild_id, channel_id, message_id, goal, status,
                state_json, step_count, max_steps, created_at, updated_at)
               VALUES ($1, $2, $3, $4, $5, 'queued', '[]', 0, $6, $7, $7)
               RETURNING id""",
            user_id, guild_id, channel_id, message_id, goal, max_steps, now,
        ))

    async def get_agent_job(self, job_id: int):
        return await self._fetchone("SELECT * FROM agent_jobs WHERE id = $1", job_id)

    async def list_agent_jobs(self, user_id: int, limit: int = 20):
        return await self._fetchall(
            "SELECT * FROM agent_jobs WHERE user_id = $1 "
            "ORDER BY created_at DESC LIMIT $2",
            user_id, max(1, min(int(limit), 100)),
        )

    async def count_active_agent_jobs(self, user_id: int) -> int:
        return int(await self._fetchval(
            "SELECT COUNT(*) FROM agent_jobs WHERE user_id = $1 "
            "AND status IN ('queued', 'running', 'needs_input')",
            user_id,
        ) or 0)

    async def recover_agent_jobs(self) -> int:
        now = int(time.time())
        return await self._execute(
            "UPDATE agent_jobs SET status = 'queued', updated_at = $1, "
            "last_error = COALESCE(last_error, 'Recovered after bot restart.') "
            "WHERE status = 'running'",
            now,
        )

    async def claim_next_agent_job(self):
        now = int(time.time())
        return await self._fetchone(
            """UPDATE agent_jobs
               SET status = 'running', updated_at = $1,
                   started_at = COALESCE(started_at, $1)
               WHERE id = (
                   SELECT id FROM agent_jobs
                   WHERE status = 'queued'
                   ORDER BY created_at ASC
                   FOR UPDATE SKIP LOCKED
                   LIMIT 1
               )
               RETURNING *""",
            now,
        )

    async def update_agent_job_state(
        self, job_id: int, *, state_json: str, step_count: int, last_error: str | None = None,
    ):
        await self._execute(
            "UPDATE agent_jobs SET state_json = $1, step_count = $2, "
            "last_error = $3, updated_at = $4 WHERE id = $5",
            state_json, step_count, last_error, int(time.time()), job_id,
        )

    async def finish_agent_job(
        self, job_id: int, *, status: str, result: str = "", last_error: str = "",
    ):
        now = int(time.time())
        await self._execute(
            "UPDATE agent_jobs SET status = $1, result = $2, last_error = $3, "
            "updated_at = $4, finished_at = CASE WHEN $1 IN "
            "('completed','failed','cancelled') THEN $4 ELSE finished_at END "
            "WHERE id = $5",
            status, result or None, last_error or None, now, job_id,
        )

    async def cancel_agent_job(self, job_id: int, user_id: int) -> bool:
        now = int(time.time())
        return await self._execute(
            "UPDATE agent_jobs SET status = 'cancelled', updated_at = $1, "
            "finished_at = $1 WHERE id = $2 AND user_id = $3 "
            "AND status IN ('queued','running','needs_input')",
            now, job_id, user_id,
        ) > 0

    async def resume_agent_job(
        self, job_id: int, user_id: int, *, guild_id: int | None = None,
        channel_id: int | None = None, message_id: int | None = None,
    ) -> bool:
        return await self._execute(
            "UPDATE agent_jobs SET status = 'queued', updated_at = $1, "
            "finished_at = NULL, guild_id = COALESCE($4, guild_id), "
            "channel_id = COALESCE($5, channel_id), message_id = COALESCE($6, message_id) "
            "WHERE id = $2 AND user_id = $3 AND status = 'needs_input'",
            int(time.time()), job_id, user_id, guild_id, channel_id, message_id,
        ) > 0

    async def add_agent_job_step(
        self, *, job_id: int, step_index: int, capability: str,
        arguments: str, result: str, status: str = 'completed',
    ):
        await self._execute(
            """INSERT INTO agent_job_steps
               (job_id, step_index, capability, arguments, result, status, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7)
               ON CONFLICT (job_id, step_index) DO UPDATE SET
                   capability = EXCLUDED.capability,
                   arguments = EXCLUDED.arguments,
                   result = EXCLUDED.result,
                   status = EXCLUDED.status""",
            job_id, step_index, capability, arguments, result, status, int(time.time()),
        )

    async def get_agent_job_steps(self, job_id: int, limit: int = 100):
        return await self._fetchall(
            "SELECT * FROM agent_job_steps WHERE job_id = $1 "
            "ORDER BY step_index ASC LIMIT $2",
            job_id, max(1, min(int(limit), 500)),
        )

    # ── Video jobs & media spend ──────────────────────────────────────────────

    # ── Persistent coding workspaces ─────────────────────────────────────────
    async def register_agent_workspace(
        self,
        workspace_id: str,
        user_id: int,
        guild_id: int | None,
        repo: str,
        branch: str,
        root_path: str,
    ) -> None:
        now = int(time.time())
        await self._execute(
            """INSERT INTO agent_workspaces
               (workspace_id, user_id, guild_id, repo, branch, root_path, status, created_at, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,'active',$7,$7)
               ON CONFLICT (workspace_id) DO UPDATE SET
                   root_path=EXCLUDED.root_path, repo=EXCLUDED.repo,
                   branch=EXCLUDED.branch, status='active', updated_at=EXCLUDED.updated_at""",
            workspace_id, user_id, guild_id, repo, branch, root_path, now,
        )

    async def touch_agent_workspace(self, workspace_id: str, user_id: int) -> bool:
        return bool(await self._execute(
            "UPDATE agent_workspaces SET updated_at=$1 WHERE workspace_id=$2 AND user_id=$3 AND status='active'",
            int(time.time()), workspace_id, user_id,
        ))

    async def list_agent_workspaces(self, user_id: int, limit: int = 50):
        return await self._fetchall(
            "SELECT * FROM agent_workspaces WHERE user_id=$1 AND status='active' "
            "ORDER BY updated_at DESC LIMIT $2", user_id, max(1, min(limit, 100))
        )

    async def discard_agent_workspace(self, workspace_id: str, user_id: int) -> bool:
        return bool(await self._execute(
            "UPDATE agent_workspaces SET status='discarded', updated_at=$1 "
            "WHERE workspace_id=$2 AND user_id=$3 AND status='active'",
            int(time.time()), workspace_id, user_id,
        ))

    async def prune_missing_agent_workspaces(self, existing_ids: set[str]) -> int:
        # Only marks records missing from the persistent volume; it never deletes files.
        rows = await self._fetchall(
            "SELECT workspace_id FROM agent_workspaces WHERE status='active'"
        )
        missing = [str(r["workspace_id"]) for r in rows if str(r["workspace_id"]) not in existing_ids]
        for wid in missing:
            await self._execute(
                "UPDATE agent_workspaces SET status='missing', updated_at=$1 WHERE workspace_id=$2",
                int(time.time()), wid,
            )
        return len(missing)

    async def create_video_job(
        self, *, provider: str, external_id: str, guild_id: int, channel_id: int,
        user_id: int, kind: str, prompt: str, source_url: str, parent_id=None,
    ) -> int | None:
        """
        Returns the new row id, or None if this provider job was already
        recorded — the unique index turns a duplicate submit into a no-op
        instead of an unhandled UniqueViolationError.
        """
        return await self._fetchval(
            """INSERT INTO video_jobs
               (provider, external_id, guild_id, channel_id, user_id, kind,
                prompt, source_url, created_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
               ON CONFLICT (provider, external_id) DO NOTHING
               RETURNING id""",
            provider, external_id, guild_id, channel_id, user_id, kind,
            prompt, source_url, int(time.time()),
        )

    async def get_pending_video_jobs(self, timeout_minutes: int = 20):
        now = int(time.time())
        await self._execute(
            "UPDATE video_jobs SET status = 'failed', finished_at = $1 "
            "WHERE status = 'pending' AND created_at < $2",
            now, now - timeout_minutes * 60,
        )
        return await self._fetchall(
            "SELECT * FROM video_jobs WHERE status = 'pending' ORDER BY created_at LIMIT 25"
        )

    async def finish_video_job(self, job_id: int, status: str, result_url: str | None):
        await self._execute(
            "UPDATE video_jobs SET status = $1, result_url = $2, finished_at = $3 WHERE id = $4",
            status, result_url, int(time.time()), job_id,
        )

    @staticmethod
    def _today() -> str:
        return time.strftime("%Y-%m-%d", time.gmtime())

    async def get_media_spend(self, guild_id: int) -> int:
        value = await self._fetchval(
            "SELECT cents FROM media_spend WHERE guild_id = $1 AND spend_day = $2",
            guild_id, self._today(),
        )
        return int(value or 0)

    async def add_media_spend(self, guild_id: int, cents: int):
        await self._execute(
            "INSERT INTO media_spend (guild_id, spend_day, cents) VALUES ($1, $2, $3) "
            "ON CONFLICT (guild_id, spend_day) DO UPDATE "
            "SET cents = media_spend.cents + EXCLUDED.cents",
            guild_id, self._today(), cents,
        )

    async def close(self):
        if self._pool:
            await self._pool.close()
            self._pool = None
            log.info("Database pool closed.")
