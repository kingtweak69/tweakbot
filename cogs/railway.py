"""Per-user Railway OAuth and management commands for TweakBot.

Every Railway request uses the OAuth token linked to the Discord user who
invoked the command.  There is no shared Railway API token anywhere in here.

Commands (prefix and slash):

    railway login | logout | account | whoami | diag
    railway workspaces | projects | select | target | status
    railway project create | create-in | rename | describe | delete
    railway environment list | select | create | rename | delete
    railway service list | select | create | create-github | create-image
    railway service create-db | rename | delete | connect-repo | disconnect-repo
    railway service deploy
    railway config show | build | start | root | dockerfile | healthcheck
    railway config region | replicas | cron | clear
    railway variable list | get | set | set-skip | reference | bulk | delete
    railway variable resolved
    railway deployments | deploy | redeploy | restart | rollback | stop
    railway cancel | logs [run|build]
    railway api  (raw GraphQL, private only)

Discord allows at most 25 sub-commands per slash-command group, so the top
level is kept at 23 children.  Anything added here must either replace an
existing top-level command or live inside one of the sub-groups above.

Everything is verified against docs.railway.com/integrations/api as of the
rewrite.  Where Railway has both a modern and a legacy shape for the same
operation, the modern one is tried first and the legacy one is used as a
fallback only when the error looks like a schema mismatch.

Databases: Railway has no "create a database" mutation.  A managed database
is an ordinary service built from an image, with a volume mounted at the
engine's data directory, the engine's env vars set, and a TCP proxy for
public access.  `railway service create-db` performs all four steps.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import aiohttp
import discord
from aiohttp import web
from discord.ext import commands

import config
from utils.credentials import CredentialVault
from utils.helpers import error_embed, info_embed, success_embed


log = logging.getLogger("cogs.railway")

COG_REVISION = "per-user OAuth"

API = "https://backboard.railway.com/graphql/v2"
AUTH = "https://backboard.railway.com/oauth/auth"
TOKEN = "https://backboard.railway.com/oauth/token"
ME = "https://backboard.railway.com/oauth/me"

DEFAULT_SCOPES = (
    "openid email profile offline_access workspace:admin project:member"
)

OAUTH_PENDING_TTL = 15 * 60
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=45)
CONFIRM_WORDS = {"confirm", "yes", "y", "true", "do it"}
MAX_PROJECTS = 200
MAX_DEPLOYMENTS = 25
MAX_LOG_LINES = 40
MAX_VARIABLES_PER_BULK_WRITE = 100
MAX_API_RESPONSE = 3_500
VARIABLE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
COMMIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
SERVICE_REF_RE = re.compile(r"^[A-Za-z0-9 _.\-]{1,64}$")

# Placeholder replaced with a freshly generated secret at creation time.  It is
# not a format string: the variable values contain ${{...}} references, which
# str.format would choke on.
PASSWORD_TOKEN = "__RAILWAY_GENERATED_PASSWORD__"

# Managed-database presets.  `postgres` mirrors what Railway's own Postgres
# template provisions; the others use the official images with their standard
# environment variables.
DATABASE_PRESETS: dict[str, dict[str, Any]] = {
    "postgres": {
        "aliases": ("postgresql", "pg", "psql"),
        "label": "PostgreSQL",
        "default_name": "Postgres",
        "image": "ghcr.io/railwayapp-templates/postgres-ssl:17",
        "mount_path": "/var/lib/postgresql/data",
        "port": 5432,
        "url_variable": "DATABASE_URL",
        "variables": {
            "POSTGRES_USER": "postgres",
            "POSTGRES_PASSWORD": PASSWORD_TOKEN,
            "POSTGRES_DB": "railway",
            "PGDATA": "/var/lib/postgresql/data/pgdata",
            "PGHOST": "${{RAILWAY_PRIVATE_DOMAIN}}",
            "PGPORT": "5432",
            "PGUSER": "${{POSTGRES_USER}}",
            "PGPASSWORD": "${{POSTGRES_PASSWORD}}",
            "PGDATABASE": "${{POSTGRES_DB}}",
            "SSL_CERT_DAYS": "820",
            "DATABASE_URL": (
                "postgresql://${{POSTGRES_USER}}:${{POSTGRES_PASSWORD}}"
                "@${{RAILWAY_PRIVATE_DOMAIN}}:5432/${{POSTGRES_DB}}"
            ),
            "DATABASE_PUBLIC_URL": (
                "postgresql://${{POSTGRES_USER}}:${{POSTGRES_PASSWORD}}"
                "@${{RAILWAY_TCP_PROXY_DOMAIN}}:${{RAILWAY_TCP_PROXY_PORT}}"
                "/${{POSTGRES_DB}}"
            ),
        },
    },
    "mysql": {
        "aliases": ("maria", "mariadb"),
        "label": "MySQL",
        "default_name": "MySQL",
        "image": "mysql:8",
        "mount_path": "/var/lib/mysql",
        "port": 3306,
        "url_variable": "MYSQL_URL",
        "variables": {
            "MYSQL_ROOT_PASSWORD": PASSWORD_TOKEN,
            "MYSQL_DATABASE": "railway",
            "MYSQL_USER": "mysql",
            "MYSQL_PASSWORD": PASSWORD_TOKEN,
            "MYSQLHOST": "${{RAILWAY_PRIVATE_DOMAIN}}",
            "MYSQLPORT": "3306",
            "MYSQLUSER": "${{MYSQL_USER}}",
            "MYSQLPASSWORD": "${{MYSQL_PASSWORD}}",
            "MYSQLDATABASE": "${{MYSQL_DATABASE}}",
            "MYSQL_URL": (
                "mysql://${{MYSQL_USER}}:${{MYSQL_PASSWORD}}"
                "@${{RAILWAY_PRIVATE_DOMAIN}}:3306/${{MYSQL_DATABASE}}"
            ),
            "MYSQL_PUBLIC_URL": (
                "mysql://${{MYSQL_USER}}:${{MYSQL_PASSWORD}}"
                "@${{RAILWAY_TCP_PROXY_DOMAIN}}:${{RAILWAY_TCP_PROXY_PORT}}"
                "/${{MYSQL_DATABASE}}"
            ),
        },
    },
    "redis": {
        "aliases": ("valkey", "kv"),
        "label": "Redis",
        "default_name": "Redis",
        "image": "bitnami/redis:7.2.5",
        "mount_path": "/bitnami",
        "port": 6379,
        "url_variable": "REDIS_URL",
        "variables": {
            "REDIS_PASSWORD": PASSWORD_TOKEN,
            "REDIS_AOF_ENABLED": "no",
            "REDISHOST": "${{RAILWAY_PRIVATE_DOMAIN}}",
            "REDISPORT": "6379",
            "REDISUSER": "default",
            "REDISPASSWORD": "${{REDIS_PASSWORD}}",
            "REDIS_URL": (
                "redis://default:${{REDIS_PASSWORD}}"
                "@${{RAILWAY_PRIVATE_DOMAIN}}:6379"
            ),
            "REDIS_PUBLIC_URL": (
                "redis://default:${{REDIS_PASSWORD}}"
                "@${{RAILWAY_TCP_PROXY_DOMAIN}}:${{RAILWAY_TCP_PROXY_PORT}}"
            ),
        },
    },
    "mongo": {
        "aliases": ("mongodb",),
        "label": "MongoDB",
        "default_name": "MongoDB",
        "image": "mongo:7",
        "mount_path": "/data/db",
        "port": 27017,
        "url_variable": "MONGO_URL",
        "variables": {
            "MONGO_INITDB_ROOT_USERNAME": "mongo",
            "MONGO_INITDB_ROOT_PASSWORD": PASSWORD_TOKEN,
            "MONGOHOST": "${{RAILWAY_PRIVATE_DOMAIN}}",
            "MONGOPORT": "27017",
            "MONGOUSER": "${{MONGO_INITDB_ROOT_USERNAME}}",
            "MONGOPASSWORD": "${{MONGO_INITDB_ROOT_PASSWORD}}",
            "MONGO_URL": (
                "mongodb://${{MONGO_INITDB_ROOT_USERNAME}}:"
                "${{MONGO_INITDB_ROOT_PASSWORD}}"
                "@${{RAILWAY_PRIVATE_DOMAIN}}:27017"
            ),
            "MONGO_PUBLIC_URL": (
                "mongodb://${{MONGO_INITDB_ROOT_USERNAME}}:"
                "${{MONGO_INITDB_ROOT_PASSWORD}}"
                "@${{RAILWAY_TCP_PROXY_DOMAIN}}:${{RAILWAY_TCP_PROXY_PORT}}"
            ),
        },
    },
}


def _database_preset(engine: str) -> tuple[str, dict[str, Any]]:
    key = engine.strip().casefold()
    if key in DATABASE_PRESETS:
        return key, DATABASE_PRESETS[key]
    for name, preset in DATABASE_PRESETS.items():
        if key in preset["aliases"]:
            return name, preset
    raise RailwayError(
        "Database engine must be one of: "
        + ", ".join(f"`{name}`" for name in sorted(DATABASE_PRESETS))
    )


class RailwayError(RuntimeError):
    """A Railway error that is safe to show to the command invoker."""


@dataclass(slots=True)
class OAuthPending:
    user_id: int
    verifier: str
    created_at: float


@dataclass(slots=True)
class RailwayTarget:
    project_id: str
    project_name: str
    environment_id: str | None = None
    environment_name: str | None = None
    service_id: str | None = None
    service_name: str | None = None

    def as_json(self) -> str:
        return json.dumps(
            {
                "project_id": self.project_id,
                "project_name": self.project_name,
                "environment_id": self.environment_id,
                "environment_name": self.environment_name,
                "service_id": self.service_id,
                "service_name": self.service_name,
            },
            separators=(",", ":"),
        )


class RailwayClient:
    """Token-scoped GraphQL client.  It never stores a shared token."""

    def __init__(self, access_token: str):
        self.access_token = access_token

    async def graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "TweakBot-Railway-OAuth/3.0",
        }
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.post(
                    API,
                    json={"query": query, "variables": variables or {}},
                    headers=headers,
                ) as response:
                    status = response.status
                    retry_after = response.headers.get("Retry-After")
                    raw = await response.text()
        except asyncio.TimeoutError as exc:
            raise RailwayError(
                "Railway did not respond in time. The action may still have "
                "completed; check the project before retrying."
            ) from exc
        except aiohttp.ClientError as exc:
            raise RailwayError(f"Could not reach Railway: {exc}") from exc

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = None

        if status == 401:
            raise RailwayError("Railway rejected this login. Run `railway login` again.")
        if status == 403:
            detail = _graphql_error(payload) if isinstance(payload, dict) else ""
            raise RailwayError(
                (detail or "Railway denied that action.")
                + " Your OAuth grant may not cover this workspace/project, or the "
                "scope is read-only. Reconnect with `railway login` and pick the "
                "right resources on the consent screen."
            )
        if status == 429:
            suffix = f" Retry after `{retry_after}`." if retry_after else ""
            raise RailwayError(f"Railway rate limited this request.{suffix}")
        if status >= 400:
            detail = _graphql_error(payload) if isinstance(payload, dict) else ""
            raise RailwayError(detail or f"Railway returned HTTP {status}.")
        if not isinstance(payload, dict):
            raise RailwayError("Railway returned an invalid response.")
        if payload.get("errors"):
            raise RailwayError(_graphql_error(payload))

        data = payload.get("data")
        if not isinstance(data, dict):
            raise RailwayError("Railway returned no GraphQL data.")
        return data

    async def try_graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Best-effort query: returns None instead of raising."""
        try:
            return await self.graphql(query, variables)
        except RailwayError as exc:
            log.debug("Optional Railway query failed: %s", exc)
            return None


class Railway(commands.Cog):
    """🚄 Per-user Railway OAuth and project management."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.vault = CredentialVault()
        self.pending: dict[str, OAuthPending] = {}
        self.runner: web.AppRunner | None = None
        self._token_locks: dict[int, asyncio.Lock] = {}
        self._write_locks: dict[int, asyncio.Lock] = {}

    # ------------------------------------------------------------------
    # OAuth lifecycle
    # ------------------------------------------------------------------

    async def cog_load(self) -> None:
        if not self._oauth_is_configured():
            log.warning(
                "Railway OAuth is not configured (client id/secret, public base URL). "
                "Commands will report this instead of failing."
            )
            return

        host = str(getattr(config, "OAUTH_CALLBACK_HOST", "0.0.0.0"))
        port = self._callback_port()
        app = web.Application()
        app.router.add_get("/oauth/railway/callback", self._callback)
        app.router.add_get("/oauth/railway/health", self._health)
        runner = web.AppRunner(app)
        await runner.setup()
        try:
            await web.TCPSite(runner, host, port).start()
        except OSError as exc:
            # Never take the whole bot down over a port clash; strict cog loading
            # would otherwise refuse startup.
            await runner.cleanup()
            log.error(
                "Railway OAuth callback could not bind %s:%s (%s). Login links will "
                "not complete until this is fixed.",
                host,
                port,
                exc,
            )
            return
        self.runner = runner
        log.info("Railway OAuth callback listening on %s:%s", host, port)

    async def cog_unload(self) -> None:
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    @staticmethod
    def _callback_port() -> int:
        configured = getattr(config, "OAUTH_CALLBACK_PORT", None)
        if configured:
            return int(configured)
        return int(os.getenv("PORT", "8080"))

    def _oauth_is_configured(self) -> bool:
        return bool(
            getattr(config, "RAILWAY_OAUTH_CLIENT_ID", "")
            and getattr(config, "RAILWAY_OAUTH_CLIENT_SECRET", "")
            and getattr(config, "OAUTH_PUBLIC_BASE_URL", "")
            and self.vault
        )

    def _scopes(self) -> str:
        return str(getattr(config, "RAILWAY_OAUTH_SCOPES", "") or DEFAULT_SCOPES)

    def _redirect_uri(self) -> str:
        base_url = str(getattr(config, "OAUTH_PUBLIC_BASE_URL", "")).rstrip("/")
        return f"{base_url}/oauth/railway/callback"

    def _discard_expired_pending(self) -> None:
        cutoff = time.time() - OAUTH_PENDING_TTL
        for state, pending in tuple(self.pending.items()):
            if pending.created_at < cutoff:
                self.pending.pop(state, None)

    async def _health(self, request: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def _callback(self, request: web.Request) -> web.Response:
        self._discard_expired_pending()
        state = request.query.get("state", "")
        code = request.query.get("code", "")
        oauth_error = request.query.get("error", "")
        pending = self.pending.pop(state, None)

        if not pending:
            return web.Response(
                text="This Railway login link is invalid or expired. Run Railway login again.",
                status=400,
            )
        if oauth_error or not code:
            return web.Response(
                text=f"Railway authorization failed: {oauth_error or 'missing code'}",
                status=400,
            )
        if not self.vault:
            return web.Response(
                text="TweakBot ephemeral session storage is unavailable.", status=500
            )

        basic = self._basic_auth()
        token_payload = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self._redirect_uri(),
            "code_verifier": pending.verifier,
        }

        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.post(
                    TOKEN,
                    data=token_payload,
                    headers={"Authorization": f"Basic {basic}", "Accept": "application/json"},
                ) as response:
                    token_status = response.status
                    token_raw = await response.text()
                try:
                    token_data = json.loads(token_raw)
                except json.JSONDecodeError:
                    token_data = {}

                if token_status >= 400 or not token_data.get("access_token"):
                    log.warning("Railway token exchange failed with HTTP %s", token_status)
                    return web.Response(
                        text="Railway token exchange failed. Return to Discord and try again.",
                        status=400,
                    )

                profile: dict[str, Any] = {}
                async with session.get(
                    ME,
                    headers={"Authorization": f"Bearer {token_data['access_token']}"},
                ) as response:
                    if response.status < 400:
                        try:
                            profile = await response.json(content_type=None)
                        except (aiohttp.ContentTypeError, json.JSONDecodeError):
                            profile = {}
        except (asyncio.TimeoutError, aiohttp.ClientError):
            return web.Response(
                text="Railway could not complete the login. Return to Discord and try again.",
                status=502,
            )

        token_data["obtained_at"] = int(time.time())
        account_name = str(profile.get("name") or profile.get("email") or "Railway user")
        account_id = str(profile.get("sub") or "")
        # OAuth credentials and account identity are process-local only.
        # Nothing from the provider login is written to the database.
        token_data["account_name"] = account_name
        token_data["account_id"] = account_id
        await self.vault.put(pending.user_id, "railway", token_data)

        granted = str(token_data.get("scope") or "")
        user = self.bot.get_user(pending.user_id)
        if user:
            note = ""
            if granted and "offline_access" not in granted:
                note = (
                    "\n\nHeads up: no `offline_access` was granted, so this session "
                    "expires in about an hour and you will need to log in again."
                )
            try:
                await user.send(
                    embed=success_embed(
                        f"Connected as `{account_name}`.\nGranted scopes: "
                        f"`{granted or 'unknown'}`\n\nRun `railway projects` to see what "
                        f"you shared, then `railway select <project>`.{note}",
                        title="🚄 Railway connected",
                    )
                )
            except discord.HTTPException:
                pass

        return web.Response(
            text="Railway is connected to TweakBot. You can close this page."
        )

    def _basic_auth(self) -> str:
        client_id = str(getattr(config, "RAILWAY_OAUTH_CLIENT_ID", ""))
        client_secret = str(getattr(config, "RAILWAY_OAUTH_CLIENT_SECRET", ""))
        return base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    def _token_lock(self, user_id: int) -> asyncio.Lock:
        return self._token_locks.setdefault(user_id, asyncio.Lock())

    def _write_lock(self, user_id: int) -> asyncio.Lock:
        return self._write_locks.setdefault(user_id, asyncio.Lock())

    async def _credentials(self, user_id: int) -> dict[str, Any] | None:
        if not self.vault:
            return None

        async with self._token_lock(user_id):
            data = await self.vault.get(user_id, "railway")
            if not isinstance(data, dict) or not data.get("access_token"):
                raise RailwayError("Your Railway session is not active after a restart. Run `railway login` again.")

            obtained_at = float(data.get("obtained_at") or data.get("stored_at") or 0)
            expires_in = int(data.get("expires_in") or 3600)
            if time.time() < obtained_at + expires_in - 120:
                return data

            refresh_token = data.get("refresh_token")
            if not refresh_token:
                raise RailwayError(
                    "Railway session expired and no refresh token was issued. "
                    "Run `railway login` again (the app must request `offline_access`)."
                )

            try:
                async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                    async with session.post(
                        TOKEN,
                        data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                        headers={
                            "Authorization": f"Basic {self._basic_auth()}",
                            "Accept": "application/json",
                        },
                    ) as response:
                        refresh_status = response.status
                        refresh_raw = await response.text()
            except (asyncio.TimeoutError, aiohttp.ClientError) as exc:
                raise RailwayError("Could not refresh the Railway session. Try again.") from exc

            try:
                refreshed = json.loads(refresh_raw)
            except json.JSONDecodeError:
                refreshed = {}
            if refresh_status >= 400 or not refreshed.get("access_token"):
                raise RailwayError("Railway session expired. Run `railway login` again.")

            # Railway may rotate refresh tokens; keep the newest token in RAM only.
            refreshed.setdefault("refresh_token", refresh_token)
            refreshed["obtained_at"] = int(time.time())
            await self.vault.put(user_id, "railway", refreshed)
            return refreshed

    async def _client(self, ctx: commands.Context) -> RailwayClient:
        if not self._oauth_is_configured():
            raise RailwayError(
                "Railway OAuth is not configured. Set `RAILWAY_OAUTH_CLIENT_ID`, "
                "`RAILWAY_OAUTH_CLIENT_SECRET`, and `OAUTH_PUBLIC_BASE_URL`."
            )
        data = await self._credentials(ctx.author.id)
        if not data:
            raise RailwayError("No Railway account is linked. Use `railway login`.")
        return RailwayClient(str(data["access_token"]))

    # ------------------------------------------------------------------
    # Context, target, and response helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _embed(
        title: str, description: str = "", color: discord.Color | None = None
    ) -> discord.Embed:
        return discord.Embed(
            title=f"🚄 {title}",
            description=description,
            color=color or discord.Color.blurple(),
        )

    @staticmethod
    def _prefix(ctx: commands.Context) -> str:
        return str(
            getattr(ctx, "clean_prefix", None)
            or getattr(ctx, "prefix", None)
            or getattr(config, "PREFIX", "!")
        )

    async def _send(
        self,
        ctx: commands.Context,
        *,
        embed: discord.Embed,
        ephemeral: bool = False,
    ) -> None:
        kwargs: dict[str, Any] = {
            "embed": embed,
            "allowed_mentions": discord.AllowedMentions.none(),
        }
        if getattr(ctx, "interaction", None) is not None:
            kwargs["ephemeral"] = ephemeral
        await ctx.send(**kwargs)

    async def _defer(self, ctx: commands.Context, *, ephemeral: bool = False) -> None:
        interaction = getattr(ctx, "interaction", None)
        if interaction is not None and not interaction.response.is_done():
            await ctx.defer(ephemeral=ephemeral, thinking=True)

    async def _scrub(self, ctx: commands.Context) -> None:
        """Delete the invoking prefix message when it contained a secret."""
        if getattr(ctx, "interaction", None) is not None:
            return
        message = getattr(ctx, "message", None)
        if message is None or ctx.guild is None:
            return
        try:
            await message.delete()
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            log.debug("Could not delete a Railway command message containing a secret.")

    async def _require_private(self, ctx: commands.Context, action: str) -> bool:
        """Only used for commands that PRINT secrets back."""
        if getattr(ctx, "interaction", None) is not None or ctx.guild is None:
            return True
        await self._send(
            ctx,
            embed=error_embed(
                f"{action} would be printed in this channel. Use the slash command "
                "so the reply is ephemeral, or DM the bot.",
                title="🚄 Private Railway command required",
            ),
        )
        return False

    async def _target(
        self,
        ctx: commands.Context,
        *,
        environment: bool = True,
        service: bool = False,
    ) -> RailwayTarget:
        if not getattr(self.bot, "db", None):
            raise RailwayError("TweakBot target storage is unavailable.")
        raw = await self.bot.db.get_user_target(ctx.author.id, "railway")
        if not raw:
            raise RailwayError("Select a project first with `railway select <project>`.")
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise RailwayError(
                "Saved Railway target is invalid. Run `railway select <project>`."
            ) from exc
        if not data.get("project_id"):
            raise RailwayError("Select a project first with `railway select <project>`.")

        target = RailwayTarget(
            project_id=str(data["project_id"]),
            project_name=str(data.get("project_name") or data["project_id"]),
            environment_id=_maybe_string(data.get("environment_id")),
            environment_name=_maybe_string(data.get("environment_name")),
            service_id=_maybe_string(data.get("service_id")),
            service_name=_maybe_string(data.get("service_name")),
        )
        if environment and not target.environment_id:
            raise RailwayError(
                "No environment is selected. Use `railway environment select <name-or-id>`."
            )
        if service and not target.service_id:
            raise RailwayError(
                "No service is selected. Use `railway service select <name-or-id>`."
            )
        return target

    async def _save_target(self, ctx: commands.Context, target: RailwayTarget) -> None:
        if not getattr(self.bot, "db", None):
            raise RailwayError("TweakBot target storage is unavailable.")
        await self.bot.db.set_user_target(ctx.author.id, "railway", target.as_json())

    async def _clear_target(self, ctx: commands.Context) -> None:
        if getattr(self.bot, "db", None):
            await self.bot.db.set_user_target(ctx.author.id, "railway", "")

    # ------------------------------------------------------------------
    # Discovery: workspaces and projects
    #
    # Railway exposes granted resources two different ways depending on the
    # scope the user approved:
    #   * workspace scopes  -> me { workspaces { id name } }
    #   * project scopes    -> externalWorkspaces { id name projects { id name } }
    # A token can have either, both, or (personal account tokens) neither, so
    # all three paths are attempted and merged.
    # ------------------------------------------------------------------

    async def _workspaces(self, client: RailwayClient) -> list[dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}

        data = await client.try_graphql("query { me { workspaces { id name } } }")
        for item in _as_nodes((data or {}).get("me", {}).get("workspaces")):
            if item.get("id"):
                found[str(item["id"])] = {
                    "id": str(item["id"]),
                    "name": str(item.get("name") or item["id"]),
                    "scope": "workspace",
                }

        data = await client.try_graphql(
            "query { externalWorkspaces { id name projects { id name } } }"
        )
        for item in _as_nodes((data or {}).get("externalWorkspaces")):
            if not item.get("id"):
                continue
            key = str(item["id"])
            entry = found.get(key) or {
                "id": key,
                "name": str(item.get("name") or key),
                "scope": "project",
            }
            entry["projects"] = _as_nodes(item.get("projects"))
            found[key] = entry

        return list(found.values())

    async def _projects(
        self,
        client: RailwayClient,
        workspace_id: str | None = None,
    ) -> list[dict[str, Any]]:
        workspaces = await self._workspaces(client)
        if workspace_id:
            workspaces = [w for w in workspaces if str(w.get("id")) == workspace_id]
            if not workspaces:
                raise RailwayError(
                    "That workspace was not shared with this OAuth login. "
                    "Run `railway workspaces`."
                )

        projects: dict[str, dict[str, Any]] = {}

        for workspace in workspaces:
            # Projects already returned by externalWorkspaces (project scope).
            for project in workspace.get("projects") or []:
                if project.get("id"):
                    projects[str(project["id"])] = {
                        "id": str(project["id"]),
                        "name": str(project.get("name") or project["id"]),
                        "workspace_id": str(workspace["id"]),
                        "workspace_name": str(workspace.get("name") or workspace["id"]),
                    }
            # Workspace scope: enumerate every project in the workspace.
            data = await client.try_graphql(
                """
                query WorkspaceProjects($workspaceId: String!) {
                  projects(workspaceId: $workspaceId) {
                    edges { node { id name description } }
                  }
                }
                """,
                {"workspaceId": str(workspace["id"])},
            )
            for project in _edge_nodes((data or {}).get("projects", {}).get("edges")):
                if not project.get("id"):
                    continue
                projects[str(project["id"])] = {
                    "id": str(project["id"]),
                    "name": str(project.get("name") or project["id"]),
                    "description": project.get("description"),
                    "workspace_id": str(workspace["id"]),
                    "workspace_name": str(workspace.get("name") or workspace["id"]),
                }

        if not projects and not workspace_id:
            # Personal-account fallback (no workspace/project scope granted).
            data = await client.try_graphql(
                "query { projects { edges { node { id name description } } } }"
            )
            for project in _edge_nodes((data or {}).get("projects", {}).get("edges")):
                if project.get("id"):
                    projects[str(project["id"])] = {
                        "id": str(project["id"]),
                        "name": str(project.get("name") or project["id"]),
                        "description": project.get("description"),
                        "workspace_id": "",
                        "workspace_name": "personal",
                    }

        if not projects:
            raise RailwayError(
                "No Railway projects are visible to this login. Run `railway diag` to "
                "see what your token can actually reach, then `railway login` again and "
                "select the workspace or projects on the consent screen."
            )
        return list(projects.values())[:MAX_PROJECTS]

    async def _project_detail(self, client: RailwayClient, project_id: str) -> dict[str, Any]:
        data = await client.graphql(
            """
            query Project($id: String!) {
              project(id: $id) {
                id
                name
                description
                environments { edges { node { id name } } }
                services { edges { node { id name } } }
              }
            }
            """,
            {"id": project_id},
        )
        project = data.get("project")
        if not isinstance(project, dict):
            raise RailwayError("Railway project was not found, or this login cannot see it.")
        return project

    async def _service_instance(
        self, client: RailwayClient, target: RailwayTarget
    ) -> dict[str, Any]:
        if not target.service_id or not target.environment_id:
            raise RailwayError("Select a Railway service and environment first.")
        data = await client.graphql(
            """
            query ServiceInstance($serviceId: String!, $environmentId: String!) {
              serviceInstance(serviceId: $serviceId, environmentId: $environmentId) {
                id
                serviceId
                serviceName
                environmentId
                buildCommand
                startCommand
                rootDirectory
                dockerfilePath
                healthcheckPath
                region
                numReplicas
                cronSchedule
                sleepApplication
                restartPolicyType
                latestDeployment { id status createdAt url staticUrl }
              }
            }
            """,
            {"serviceId": target.service_id, "environmentId": target.environment_id},
        )
        instance = data.get("serviceInstance")
        if not isinstance(instance, dict):
            raise RailwayError("Railway service instance was not found.")
        return instance

    async def _service_source(
        self, client: RailwayClient, target: RailwayTarget
    ) -> str:
        """Best effort: the source shape has moved around in Railway's schema."""
        data = await client.try_graphql(
            """
            query ServiceSource($serviceId: String!, $environmentId: String!) {
              serviceInstance(serviceId: $serviceId, environmentId: $environmentId) {
                source { repo image }
                branch
              }
            }
            """,
            {"serviceId": target.service_id, "environmentId": target.environment_id},
        )
        instance = (data or {}).get("serviceInstance") or {}
        source = instance.get("source") or {}
        if not isinstance(source, dict):
            return "unknown"
        text = source.get("repo") or source.get("image")
        if not text:
            return "not configured"
        if source.get("repo") and instance.get("branch"):
            text = f"{text} @ {instance['branch']}"
        return str(text)

    async def _deployments(
        self, client: RailwayClient, target: RailwayTarget, limit: int
    ) -> list[dict[str, Any]]:
        if not target.service_id or not target.environment_id:
            raise RailwayError("Select a Railway service and environment first.")
        data = await client.graphql(
            """
            query Deployments($input: DeploymentListInput!, $first: Int) {
              deployments(input: $input, first: $first) {
                edges {
                  node {
                    id
                    status
                    createdAt
                    url
                    staticUrl
                    canRedeploy
                    canRollback
                  }
                }
              }
            }
            """,
            {
                "input": {
                    "projectId": target.project_id,
                    "environmentId": target.environment_id,
                    "serviceId": target.service_id,
                },
                "first": max(1, min(limit, MAX_DEPLOYMENTS)),
            },
        )
        return _edge_nodes((data.get("deployments") or {}).get("edges"))

    async def _latest_deployment(
        self, client: RailwayClient, target: RailwayTarget
    ) -> dict[str, Any]:
        deployments = await self._deployments(client, target, 1)
        if not deployments:
            raise RailwayError("No Railway deployments exist for the selected service.")
        return deployments[0]

    async def _variables(
        self,
        client: RailwayClient,
        target: RailwayTarget,
        *,
        unrendered: bool = True,
    ) -> dict[str, Any]:
        if not target.environment_id:
            raise RailwayError("Select a Railway environment first.")
        data = await client.graphql(
            """
            query Variables(
              $projectId: String!,
              $environmentId: String!,
              $serviceId: String,
              $unrendered: Boolean
            ) {
              variables(
                projectId: $projectId,
                environmentId: $environmentId,
                serviceId: $serviceId,
                unrendered: $unrendered
              )
            }
            """,
            {
                "projectId": target.project_id,
                "environmentId": target.environment_id,
                "serviceId": target.service_id,
                "unrendered": unrendered,
            },
        )
        variables = data.get("variables") or {}
        return variables if isinstance(variables, dict) else {}

    async def _update_instance(
        self,
        client: RailwayClient,
        target: RailwayTarget,
        changes: dict[str, Any],
    ) -> None:
        if not target.service_id or not target.environment_id:
            raise RailwayError("Select a Railway service and environment first.")
        await client.graphql(
            """
            mutation ServiceInstanceUpdate(
              $serviceId: String!,
              $environmentId: String!,
              $input: ServiceInstanceUpdateInput!
            ) {
              serviceInstanceUpdate(
                serviceId: $serviceId,
                environmentId: $environmentId,
                input: $input
              )
            }
            """,
            {
                "serviceId": target.service_id,
                "environmentId": target.environment_id,
                "input": changes,
            },
        )

    async def _upsert_variable(
        self,
        client: RailwayClient,
        target: RailwayTarget,
        name: str,
        value: str,
        *,
        skip_deploys: bool,
    ) -> None:
        await client.graphql(
            """
            mutation VariableUpsert($input: VariableUpsertInput!) {
              variableUpsert(input: $input)
            }
            """,
            {
                "input": {
                    "projectId": target.project_id,
                    "environmentId": target.environment_id,
                    "serviceId": target.service_id,
                    "name": name,
                    "value": value,
                    "skipDeploys": skip_deploys,
                }
            },
        )

    # ------------------------------------------------------------------
    # Root commands
    # ------------------------------------------------------------------

    @commands.hybrid_group(name="railway", aliases=["rw"], invoke_without_command=True)
    async def railway(self, ctx: commands.Context) -> None:
        """Connect and manage the caller's own Railway account."""
        prefix = self._prefix(ctx)
        embed = self._embed(
            "Railway command deck",
            "Every user links and controls only their own Railway account through OAuth.",
        )
        embed.set_footer(text=f"{COG_REVISION} · no shared Railway token")
        embed.add_field(
            name="Account & selection",
            value=(
                f"`{prefix}railway login` · `{prefix}railway diag`\n"
                f"`{prefix}railway projects` · `{prefix}railway select <project>`\n"
                f"`{prefix}railway environment select <env>` · "
                f"`{prefix}railway service select <service>`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Projects & services",
            value=(
                f"`{prefix}railway project create <name>` · "
                f"`{prefix}railway project rename <name>`\n"
                f"`{prefix}railway service create-github <name> <owner/repo> [branch]`\n"
                f"`{prefix}railway service create-image <name> <image>`\n"
                f"`{prefix}railway service create-db <postgres|mysql|redis|mongo> [name]`\n"
                f"`{prefix}railway service connect-repo <owner/repo> [branch]`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Config & variables",
            value=(
                f"`{prefix}railway config build <cmd>` · `{prefix}railway config start <cmd>`\n"
                f"`{prefix}railway config root <dir>` · `{prefix}railway config show`\n"
                f"`{prefix}railway variable set <KEY> <value>` · "
                f"`{prefix}railway variable reference <KEY> <Service> <VAR>`\n"
                f"`{prefix}railway variable delete <KEY> confirm`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Deployments",
            value=(
                f"`{prefix}railway deploy [commit-sha]` · `{prefix}railway redeploy` · "
                f"`{prefix}railway restart`\n"
                f"`{prefix}railway deployments` · `{prefix}railway logs build` · "
                f"`{prefix}railway status`"
            ),
            inline=False,
        )
        await self._send(ctx, embed=embed)

    @railway.command(name="login")
    async def login(self, ctx: commands.Context) -> None:
        """Connect your own Railway account using OAuth and PKCE."""
        if not self._oauth_is_configured():
            await self._send(
                ctx,
                embed=error_embed(
                    "Railway OAuth is not configured. Set the Railway OAuth client "
                    "values, and public callback URL. OAuth tokens are kept in RAM only.",
                    title="🚄 Railway OAuth unavailable",
                ),
            )
            return

        self._discard_expired_pending()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        self.pending[state] = OAuthPending(ctx.author.id, verifier, time.time())

        query = urlencode(
            {
                "response_type": "code",
                "client_id": getattr(config, "RAILWAY_OAUTH_CLIENT_ID"),
                "redirect_uri": self._redirect_uri(),
                "scope": self._scopes(),
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                # Required for refresh tokens, and forces the resource picker so
                # you can change which workspaces/projects are shared.
                "prompt": "consent",
            }
        )
        embed = self._embed(
            "Connect your Railway account",
            f"[Authorize TweakBot on Railway]({AUTH}?{query})\n\n"
            "On the consent screen, **select the workspaces or projects you want to "
            "manage** — anything you skip stays invisible to these commands.\n\n"
            "The link is tied to your Discord account and expires after use or "
            "when the bot restarts.",
        )
        embed.set_footer(text=f"scopes: {_trim(self._scopes(), 200)}")
        await self._send(
            ctx, embed=embed, ephemeral=getattr(ctx, "interaction", None) is not None
        )

    async def _revoke_railway_token(self, access_token: str) -> bool:
        url = str(getattr(config, "RAILWAY_OAUTH_REVOKE_URL", "") or "").strip()
        if not url or not access_token:
            return False
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.post(
                    url,
                    data={"token": access_token},
                    headers={"Accept": "application/json"},
                ) as response:
                    return response.status in {200, 202, 204}
        except Exception:
            log.warning("Railway token revocation request failed", exc_info=True)
            return False

    @railway.command(name="logout")
    async def logout(self, ctx: commands.Context) -> None:
        """Disconnect the ephemeral Railway session and clear its target."""
        data = await self.vault.get(ctx.author.id, "railway") if self.vault else None
        token = str((data or {}).get("access_token") or "")
        revoked = await self._revoke_railway_token(token)
        if self.vault:
            await self.vault.delete(ctx.author.id, "railway")
        if getattr(self.bot, "db", None):
            await self._clear_target(ctx)
        status = (
            "The configured Railway OAuth provider endpoint accepted a revocation request."
            if revoked else
            "The local session was erased. No provider-side Railway revocation endpoint is assumed "
            "unless you configure `RAILWAY_OAUTH_REVOKE_URL`."
        )
        await self._send(
            ctx,
            embed=success_embed(
                "Your Railway OAuth session was erased from RAM. Nothing was persisted by TweakBot.\n\n" + status,
                title="🚄 Railway disconnected",
            ),
        )

    @railway.command(name="account", aliases=["auth", "whoami"])
    async def account(self, ctx: commands.Context) -> None:
        """Show the current ephemeral Railway session."""
        data = await self.vault.get(ctx.author.id, "railway") if self.vault else None
        if not data:
            await self._send(
                ctx,
                embed=info_embed("No Railway session is active. Use `railway login`."),
            )
            return
        await self._send(
            ctx,
            embed=info_embed(
                f"Current Railway session: `{data.get('account_name') or 'authenticated'}`\n"
                "Session storage: **RAM only**\n"
                "Persistence: **none**",
                title="🚄 Railway account",
            ),
        )

    @railway.command(name="diag", aliases=["debug", "doctor"])
    async def diag(self, ctx: commands.Context) -> None:
        """Show exactly what this OAuth token can reach. Start here when things break."""
        await self._defer(ctx)
        lines: list[str] = []
        lines.append(f"OAuth configured: `{self._oauth_is_configured()}`")
        lines.append(f"Callback: `{self._redirect_uri()}`")
        lines.append(f"Callback bound: `{self.runner is not None}`")
        lines.append(f"Requested scopes: `{_trim(self._scopes(), 300)}`")

        data = await self._credentials(ctx.author.id)
        if not data:
            lines.append("Linked account: `none` — run `railway login`")
            await self._send(ctx, embed=self._embed("Railway diagnostics", "\n".join(lines)))
            return

        granted = str(data.get("scope") or "unknown")
        has_refresh = bool(data.get("refresh_token"))
        lines.append(f"Granted scopes: `{_trim(granted, 300)}`")
        lines.append(f"Refresh token: `{has_refresh}`")

        client = RailwayClient(str(data["access_token"]))
        me = await client.try_graphql("query { me { id name email } }")
        lines.append(f"`me` query: `{'ok' if me else 'FAILED'}`")

        ws = await client.try_graphql("query { me { workspaces { id name } } }")
        ws_nodes = _as_nodes((ws or {}).get("me", {}).get("workspaces"))
        lines.append(f"`me.workspaces`: `{len(ws_nodes)}` (needs a workspace:* scope)")

        ext = await client.try_graphql(
            "query { externalWorkspaces { id name projects { id name } } }"
        )
        ext_nodes = _as_nodes((ext or {}).get("externalWorkspaces"))
        shared = sum(len(_as_nodes(node.get("projects"))) for node in ext_nodes)
        lines.append(
            f"`externalWorkspaces`: `{len(ext_nodes)}` workspaces / `{shared}` shared "
            "projects (needs a project:* scope)"
        )

        try:
            projects = await self._projects(client)
            lines.append(f"Resolved projects: `{len(projects)}`")
        except RailwayError as exc:
            lines.append(f"Resolved projects: `0` — {exc}")

        raw_target = (
            await self.bot.db.get_user_target(ctx.author.id, "railway")
            if getattr(self.bot, "db", None)
            else ""
        )
        lines.append(f"Saved target: `{'yes' if raw_target else 'no'}`")

        await self._send(
            ctx,
            embed=self._embed("Railway diagnostics", "\n".join(lines)),
            ephemeral=True,
        )

    @railway.command(name="workspaces", aliases=["ws"])
    async def workspaces(self, ctx: commands.Context) -> None:
        """List the workspaces this login can see."""
        await self._defer(ctx)
        nodes = await self._workspaces(await self._client(ctx))
        if not nodes:
            await self._send(
                ctx,
                embed=info_embed(
                    "No workspaces are shared with this login. That is normal if you "
                    "only granted project scopes — use `railway projects` instead."
                ),
            )
            return
        lines = [
            f"`{node.get('id')}` — **{node.get('name')}** (via {node.get('scope', 'workspace')} scope)"
            for node in nodes
        ]
        await self._send(
            ctx, embed=self._embed("Railway workspaces", _trim("\n".join(lines), 3900))
        )

    @railway.command(name="projects", aliases=["ls"])
    async def projects(self, ctx: commands.Context) -> None:
        """List projects visible to your linked Railway account."""
        await self._defer(ctx)
        nodes = await self._projects(await self._client(ctx))
        lines = [
            f"`{node['id']}` — **{node['name']}**  ·  {node.get('workspace_name') or 'personal'}"
            for node in nodes[:40]
        ]
        await self._send(
            ctx, embed=self._embed("Your Railway projects", _trim("\n".join(lines), 3900))
        )

    @railway.command(name="select", aliases=["use", "project-select"])
    async def select(self, ctx: commands.Context, *, selection: str) -> None:
        """Select the project used by later Railway commands."""
        await self._defer(ctx)
        client = await self._client(ctx)
        project = _resolve_node(await self._projects(client), selection, "project")
        detail = await self._project_detail(client, str(project["id"]))
        environments = _edge_nodes((detail.get("environments") or {}).get("edges"))
        services = _edge_nodes((detail.get("services") or {}).get("edges"))
        environment = _preferred_environment(environments)
        service = services[0] if len(services) == 1 else None
        target = RailwayTarget(
            project_id=str(detail["id"]),
            project_name=str(detail.get("name") or detail["id"]),
            environment_id=_maybe_string(environment.get("id")) if environment else None,
            environment_name=_maybe_string(environment.get("name")) if environment else None,
            service_id=_maybe_string(service.get("id")) if service else None,
            service_name=_maybe_string(service.get("name")) if service else None,
        )
        await self._save_target(ctx, target)
        extra = ""
        if not target.service_id and services:
            names = ", ".join(f"`{s.get('name')}`" for s in services[:10])
            extra = f"\n\nServices available: {names}\nPick one with `railway service select <name>`."
        await self._send(
            ctx,
            embed=success_embed(
                _target_text(target) + extra, title="🚄 Railway target selected"
            ),
        )

    @railway.command(name="target")
    async def target(self, ctx: commands.Context) -> None:
        """Show your current per-user Railway target."""
        target = await self._target(ctx, environment=False)
        await self._send(ctx, embed=self._embed("Current target", _target_text(target)))

    @railway.command(name="status")
    async def status(self, ctx: commands.Context) -> None:
        """Show status for the selected Railway service."""
        await self._defer(ctx)
        client = await self._client(ctx)
        target = await self._target(ctx, service=True)
        instance = await self._service_instance(client, target)
        latest = instance.get("latestDeployment") or {}
        if not latest:
            latest = await self._latest_deployment(client, target)
        status = str(latest.get("status") or "UNKNOWN")
        embed = self._embed(
            f"{target.service_name or instance.get('serviceName') or 'Service'} status",
            color=_status_color(status),
        )
        embed.add_field(name="Status", value=f"`{status}`")
        embed.add_field(name="Environment", value=f"`{target.environment_name}`")
        embed.add_field(
            name="Deployment", value=f"`{latest.get('id') or 'none'}`", inline=False
        )
        embed.add_field(
            name="Created", value=f"`{latest.get('createdAt') or 'unknown'}`", inline=False
        )
        url = latest.get("staticUrl") or latest.get("url")
        if url:
            embed.add_field(name="URL", value=f"`{url}`", inline=False)
        embed.add_field(
            name="Source", value=_code_or_unset(await self._service_source(client, target)),
            inline=False,
        )
        embed.add_field(
            name="Build", value=_code_or_unset(instance.get("buildCommand")), inline=False
        )
        embed.add_field(
            name="Start", value=_code_or_unset(instance.get("startCommand")), inline=False
        )
        embed.add_field(name="Region", value=_code_or_unset(instance.get("region")))
        embed.add_field(name="Replicas", value=_code_or_unset(instance.get("numReplicas")))
        await self._send(ctx, embed=embed)

    # ------------------------------------------------------------------
    # Project management
    # ------------------------------------------------------------------

    @railway.group(name="project", aliases=["proj"], invoke_without_command=True)
    async def project(self, ctx: commands.Context) -> None:
        """Create, rename, describe, or delete a project."""
        prefix = self._prefix(ctx)
        await self._send(
            ctx,
            embed=self._embed(
                "Project commands",
                f"`{prefix}railway project create <name>`\n"
                f"`{prefix}railway project create-in <workspace-id> <name>`\n"
                f"`{prefix}railway project rename <name>`\n"
                f"`{prefix}railway project describe <description>`\n"
                f"`{prefix}railway project delete confirm`",
            ),
        )

    @project.command(name="create")
    async def project_create(self, ctx: commands.Context, *, name: str) -> None:
        """Create a project. Requires exactly one shared workspace."""
        await self._defer(ctx)
        client = await self._client(ctx)
        workspaces = [w for w in await self._workspaces(client) if w.get("scope") == "workspace"]
        if len(workspaces) > 1:
            raise RailwayError(
                "You have more than one workspace. Use "
                "`railway workspaces` then `railway project create-in <workspace-id> <name>`."
            )
        workspace_id = str(workspaces[0]["id"]) if workspaces else None
        await self._create_project(ctx, client, name, workspace_id)

    @project.command(name="create-in")
    async def project_create_in(
        self, ctx: commands.Context, workspace_id: str, *, name: str
    ) -> None:
        """Create a project in a specific workspace and select it."""
        await self._defer(ctx)
        client = await self._client(ctx)
        workspaces = await self._workspaces(client)
        if not any(str(w.get("id")) == workspace_id for w in workspaces):
            raise RailwayError(
                "That workspace is not shared with this login. Run `railway workspaces`."
            )
        await self._create_project(ctx, client, name, workspace_id)

    async def _create_project(
        self,
        ctx: commands.Context,
        client: RailwayClient,
        name: str,
        workspace_id: str | None,
    ) -> None:
        name = name.strip()
        if not name:
            raise RailwayError("Project name cannot be empty.")
        mutation = """
            mutation ProjectCreate($input: ProjectCreateInput!) {
              projectCreate(input: $input) { id name }
            }
        """
        payload: dict[str, Any] = {"name": name}
        if workspace_id:
            payload["workspaceId"] = workspace_id
        async with self._write_lock(ctx.author.id):
            try:
                data = await client.graphql(mutation, {"input": payload})
            except RailwayError as exc:
                # Older Railway schemas called this teamId.
                if not workspace_id or not _looks_like_schema_mismatch(str(exc)):
                    raise
                data = await client.graphql(
                    mutation, {"input": {"name": name, "teamId": workspace_id}}
                )
        project = data.get("projectCreate")
        if not isinstance(project, dict) or not project.get("id"):
            raise RailwayError("Railway did not return the new project.")
        detail = await self._project_detail(client, str(project["id"]))
        environment = _preferred_environment(
            _edge_nodes((detail.get("environments") or {}).get("edges"))
        )
        target = RailwayTarget(
            project_id=str(project["id"]),
            project_name=str(project.get("name") or name),
            environment_id=_maybe_string(environment.get("id")) if environment else None,
            environment_name=_maybe_string(environment.get("name")) if environment else None,
        )
        await self._save_target(ctx, target)
        await self._send(
            ctx,
            embed=success_embed(
                f"Created and selected `{target.project_name}`.\n{_target_text(target)}",
                title="🚄 Railway project created",
            ),
        )

    @project.command(name="rename")
    async def project_rename(self, ctx: commands.Context, *, name: str) -> None:
        """Rename the selected Railway project."""
        name = name.strip()
        if not name:
            raise RailwayError("Project name cannot be empty.")
        await self._defer(ctx)
        target = await self._target(ctx, environment=False)
        async with self._write_lock(ctx.author.id):
            data = await (await self._client(ctx)).graphql(
                """
                mutation ProjectUpdate($id: String!, $input: ProjectUpdateInput!) {
                  projectUpdate(id: $id, input: $input) { id name }
                }
                """,
                {"id": target.project_id, "input": {"name": name}},
            )
        project = data.get("projectUpdate") or {}
        target.project_name = str(project.get("name") or name)
        await self._save_target(ctx, target)
        await self._send(
            ctx,
            embed=success_embed(
                f"Renamed the selected project to `{target.project_name}`.",
                title="🚄 Project renamed",
            ),
        )

    @project.command(name="describe")
    async def project_describe(self, ctx: commands.Context, *, description: str) -> None:
        """Set the selected project's description."""
        await self._defer(ctx)
        target = await self._target(ctx, environment=False)
        async with self._write_lock(ctx.author.id):
            await (await self._client(ctx)).graphql(
                """
                mutation ProjectUpdate($id: String!, $input: ProjectUpdateInput!) {
                  projectUpdate(id: $id, input: $input) { id }
                }
                """,
                {"id": target.project_id, "input": {"description": description.strip()}},
            )
        await self._send(
            ctx,
            embed=success_embed("Updated the project description.", title="🚄 Project updated"),
        )

    @project.command(name="delete", aliases=["remove", "rm"])
    async def project_delete(self, ctx: commands.Context, confirmation: str = "") -> None:
        """Permanently delete the selected Railway project."""
        target = await self._target(ctx, environment=False)
        if confirmation.lower() not in CONFIRM_WORDS:
            await self._send(
                ctx,
                embed=info_embed(
                    f"This permanently deletes `{target.project_name}` and everything in "
                    f"it. Use `{self._prefix(ctx)}railway project delete confirm`.",
                    title="🚄 Confirm project deletion",
                ),
            )
            return
        await self._defer(ctx)
        async with self._write_lock(ctx.author.id):
            await (await self._client(ctx)).graphql(
                "mutation ProjectDelete($id: String!) { projectDelete(id: $id) }",
                {"id": target.project_id},
            )
        await self._clear_target(ctx)
        await self._send(
            ctx,
            embed=success_embed(
                f"Deleted Railway project `{target.project_name}`.",
                title="🚄 Project deleted",
            ),
        )

    # ------------------------------------------------------------------
    # Environment management
    # ------------------------------------------------------------------

    @railway.group(name="environment", aliases=["env"], invoke_without_command=True)
    async def environment(self, ctx: commands.Context) -> None:
        """List, select, create, rename, and delete environments."""
        await self._list_environments(ctx)

    @environment.command(name="list")
    async def environment_list(self, ctx: commands.Context) -> None:
        """List environments in the selected project."""
        await self._list_environments(ctx)

    async def _list_environments(self, ctx: commands.Context) -> None:
        await self._defer(ctx)
        target = await self._target(ctx, environment=False)
        detail = await self._project_detail(await self._client(ctx), target.project_id)
        nodes = _edge_nodes((detail.get("environments") or {}).get("edges"))
        if not nodes:
            await self._send(ctx, embed=info_embed("This project has no environments."))
            return
        lines = [
            f"`{node.get('id')}` — **{node.get('name') or 'Unnamed'}**"
            + ("  ← selected" if str(node.get("id")) == target.environment_id else "")
            for node in nodes
        ]
        await self._send(ctx, embed=self._embed("Project environments", "\n".join(lines)))

    @environment.command(name="select", aliases=["use"])
    async def environment_select(self, ctx: commands.Context, *, selection: str) -> None:
        """Select the environment used by later Railway commands."""
        await self._defer(ctx)
        target = await self._target(ctx, environment=False)
        detail = await self._project_detail(await self._client(ctx), target.project_id)
        environment = _resolve_node(
            _edge_nodes((detail.get("environments") or {}).get("edges")),
            selection,
            "environment",
        )
        target.environment_id = str(environment["id"])
        target.environment_name = str(environment.get("name") or environment["id"])
        await self._save_target(ctx, target)
        await self._send(
            ctx,
            embed=success_embed(
                f"Selected environment `{target.environment_name}`.",
                title="🚄 Environment selected",
            ),
        )

    @environment.command(name="create")
    async def environment_create(self, ctx: commands.Context, *, name: str) -> None:
        """Create an environment in the selected project and select it."""
        name = name.strip()
        if not name:
            raise RailwayError("Environment name cannot be empty.")
        await self._defer(ctx)
        target = await self._target(ctx, environment=False)
        async with self._write_lock(ctx.author.id):
            data = await (await self._client(ctx)).graphql(
                """
                mutation EnvironmentCreate($input: EnvironmentCreateInput!) {
                  environmentCreate(input: $input) { id name }
                }
                """,
                {"input": {"projectId": target.project_id, "name": name}},
            )
        environment = data.get("environmentCreate")
        if not isinstance(environment, dict) or not environment.get("id"):
            raise RailwayError("Railway did not return the new environment.")
        target.environment_id = str(environment["id"])
        target.environment_name = str(environment.get("name") or name)
        await self._save_target(ctx, target)
        await self._send(
            ctx,
            embed=success_embed(
                f"Created and selected environment `{target.environment_name}`.",
                title="🚄 Environment created",
            ),
        )

    @environment.command(name="rename")
    async def environment_rename(self, ctx: commands.Context, *, name: str) -> None:
        """Rename the selected environment."""
        name = name.strip()
        if not name:
            raise RailwayError("Environment name cannot be empty.")
        await self._defer(ctx)
        target = await self._target(ctx)
        async with self._write_lock(ctx.author.id):
            await (await self._client(ctx)).graphql(
                """
                mutation EnvironmentRename($id: String!, $input: EnvironmentRenameInput!) {
                  environmentRename(id: $id, input: $input)
                }
                """,
                {"id": target.environment_id, "input": {"name": name}},
            )
        target.environment_name = name
        await self._save_target(ctx, target)
        await self._send(
            ctx,
            embed=success_embed(
                f"Renamed the environment to `{name}`.", title="🚄 Environment renamed"
            ),
        )

    @environment.command(name="delete", aliases=["remove", "rm"])
    async def environment_delete(self, ctx: commands.Context, confirmation: str = "") -> None:
        """Delete the selected Railway environment."""
        target = await self._target(ctx)
        if confirmation.lower() not in CONFIRM_WORDS:
            await self._send(
                ctx,
                embed=info_embed(
                    f"Use `{self._prefix(ctx)}railway environment delete confirm` to delete "
                    f"`{target.environment_name}` and its deployments.",
                    title="🚄 Confirm environment deletion",
                ),
            )
            return
        await self._defer(ctx)
        async with self._write_lock(ctx.author.id):
            await (await self._client(ctx)).graphql(
                "mutation EnvironmentDelete($id: String!) { environmentDelete(id: $id) }",
                {"id": target.environment_id},
            )
        target.environment_id = None
        target.environment_name = None
        target.service_id = None
        target.service_name = None
        await self._save_target(ctx, target)
        await self._send(
            ctx,
            embed=success_embed(
                "Deleted the selected Railway environment.", title="🚄 Environment deleted"
            ),
        )

    # ------------------------------------------------------------------
    # Service management
    # ------------------------------------------------------------------

    @railway.group(name="service", aliases=["svc"], invoke_without_command=True)
    async def service(self, ctx: commands.Context) -> None:
        """Create, select, rename, connect, or delete services."""
        await self._list_services(ctx)

    @service.command(name="list")
    async def service_list(self, ctx: commands.Context) -> None:
        """List services in the selected project."""
        await self._list_services(ctx)

    async def _list_services(self, ctx: commands.Context) -> None:
        await self._defer(ctx)
        target = await self._target(ctx, environment=False)
        detail = await self._project_detail(await self._client(ctx), target.project_id)
        nodes = _edge_nodes((detail.get("services") or {}).get("edges"))
        if not nodes:
            await self._send(
                ctx,
                embed=info_embed(
                    "This project has no services. Create one with "
                    "`railway service create-github <name> <owner/repo>`, or a database "
                    "with `railway service create-db postgres`."
                ),
            )
            return
        lines = [
            f"`{node.get('id')}` — **{node.get('name') or 'Unnamed'}**"
            + ("  ← selected" if str(node.get("id")) == target.service_id else "")
            for node in nodes
        ]
        await self._send(ctx, embed=self._embed("Project services", "\n".join(lines)))

    @service.command(name="select", aliases=["use"])
    async def service_select(self, ctx: commands.Context, *, selection: str) -> None:
        """Select the service used by later Railway commands."""
        await self._defer(ctx)
        target = await self._target(ctx, environment=False)
        detail = await self._project_detail(await self._client(ctx), target.project_id)
        service = _resolve_node(
            _edge_nodes((detail.get("services") or {}).get("edges")), selection, "service"
        )
        target.service_id = str(service["id"])
        target.service_name = str(service.get("name") or service["id"])
        await self._save_target(ctx, target)
        await self._send(
            ctx,
            embed=success_embed(
                f"Selected service `{target.service_name}`.", title="🚄 Service selected"
            ),
        )

    @service.command(name="create", aliases=["create-empty", "new"])
    async def service_create_empty(self, ctx: commands.Context, *, name: str) -> None:
        """Create an empty service in the selected project."""
        await self._create_service(ctx, name=name, source=None, branch=None)

    @service.command(name="create-github", aliases=["github", "create-repo"])
    async def service_create_github(
        self, ctx: commands.Context, name: str, repo: str, branch: str = "main"
    ) -> None:
        """Create a service from a GitHub repo Railway is authorized to read."""
        if not _is_repo(repo):
            raise RailwayError("Repository must be in `owner/repo` form.")
        await self._create_service(
            ctx, name=name, source={"repo": repo.strip()}, branch=branch.strip() or "main"
        )

    @service.command(name="create-image", aliases=["image", "docker"])
    async def service_create_image(
        self, ctx: commands.Context, name: str, image: str
    ) -> None:
        """Create a service from a container image."""
        image = image.strip()
        if not image:
            raise RailwayError("Image name cannot be empty.")
        await self._create_service(ctx, name=name, source={"image": image}, branch=None)

    @service.command(name="create-db", aliases=["create-database", "database", "db"])
    async def service_create_database(
        self, ctx: commands.Context, engine: str = "postgres", name: str | None = None
    ) -> None:
        """Provision a database service: postgres, mysql, redis, or mongo.

        Railway has no database mutation. This creates the service from the
        engine's image, attaches a volume at its data directory, writes the
        engine's environment variables, opens a TCP proxy, and deploys it.

        The selected service is deliberately left alone so the target keeps
        pointing at your app — wire them together with
        `railway variable reference DATABASE_URL <db-service> DATABASE_URL`.
        """
        engine_key, preset = _database_preset(engine)
        service_name = (name or preset["default_name"]).strip()
        if not SERVICE_REF_RE.fullmatch(service_name):
            raise RailwayError(
                "Database service name must be 1–64 characters of letters, digits, "
                "spaces, dots, dashes, or underscores."
            )
        await self._defer(ctx)
        # Volumes and variables are environment-scoped, so an environment is
        # required even though serviceCreate itself does not take one.
        target = await self._target(ctx)
        client = await self._client(ctx)

        password = secrets.token_hex(24)
        variables = {
            key: value.replace(PASSWORD_TOKEN, password)
            for key, value in preset["variables"].items()
        }

        steps: list[str] = []
        async with self._write_lock(ctx.author.id):
            data = await client.graphql(
                """
                mutation ServiceCreate($input: ServiceCreateInput!) {
                  serviceCreate(input: $input) { id name }
                }
                """,
                {
                    "input": {
                        "projectId": target.project_id,
                        "name": service_name,
                        "source": {"image": preset["image"]},
                    }
                },
            )
            created = data.get("serviceCreate")
            if not isinstance(created, dict) or not created.get("id"):
                raise RailwayError("Railway did not return the new database service.")
            service_id = str(created["id"])
            service_name = str(created.get("name") or service_name)
            steps.append(f"service created from `{preset['image']}`")

            volume_ok = await self._create_volume(
                client, target, service_id, preset["mount_path"]
            )
            steps.append(
                f"volume mounted at `{preset['mount_path']}`"
                if volume_ok
                else f"⚠️ volume FAILED — data will not survive redeploys"
            )

            await client.graphql(
                """
                mutation VariableCollectionUpsert($input: VariableCollectionUpsertInput!) {
                  variableCollectionUpsert(input: $input)
                }
                """,
                {
                    "input": {
                        "projectId": target.project_id,
                        "environmentId": target.environment_id,
                        "serviceId": service_id,
                        "variables": variables,
                        "replace": False,
                    }
                },
            )
            steps.append(f"{len(variables)} variables written")

            proxy = await client.try_graphql(
                """
                mutation TCPProxyCreate($input: TCPProxyCreateInput!) {
                  tcpProxyCreate(input: $input) { id domain proxyPort applicationPort }
                }
                """,
                {
                    "input": {
                        "environmentId": target.environment_id,
                        "serviceId": service_id,
                        "applicationPort": preset["port"],
                    }
                },
            )
            steps.append(
                f"TCP proxy opened on port `{preset['port']}`"
                if proxy
                else "TCP proxy skipped (private networking still works)"
            )

            deployed = await client.try_graphql(
                """
                mutation Deploy($serviceId: String!, $environmentId: String!) {
                  serviceInstanceDeployV2(
                    serviceId: $serviceId, environmentId: $environmentId
                  )
                }
                """,
                {"serviceId": service_id, "environmentId": target.environment_id},
            )
            steps.append(
                "deploy started"
                if deployed
                else "⚠️ deploy not started — run it from `railway service select` + `railway deploy`"
            )

        prefix = self._prefix(ctx)
        body = "\n".join(f"• {step}" for step in steps)
        await self._send(
            ctx,
            embed=success_embed(
                f"**{preset['label']}** is provisioning as `{service_name}` in "
                f"`{target.environment_name}`.\n\n{body}\n\n"
                f"The generated password is stored on the database service only — read it "
                f"with `{prefix}railway service select {service_name}` then "
                f"`{prefix}railway variable get {preset['url_variable']}` "
                f"(DM or slash only).\n\n"
                f"Connect your app to it without copying secrets:\n"
                f"`{prefix}railway service select <your-app>`\n"
                f"`{prefix}railway variable reference {preset['url_variable']} "
                f"{service_name} {preset['url_variable']}`\n\n"
                f"Your selected service was left as "
                f"`{target.service_name or 'not selected'}`.",
                title=f"🚄 {preset['label']} created",
            ),
        )

    async def _create_volume(
        self,
        client: RailwayClient,
        target: RailwayTarget,
        service_id: str,
        mount_path: str,
    ) -> bool:
        """Attach a persistent volume. Returns False if Railway refused it."""
        mutation = """
            mutation VolumeCreate($input: VolumeCreateInput!) {
              volumeCreate(input: $input) { id }
            }
        """
        payload: dict[str, Any] = {
            "projectId": target.project_id,
            "environmentId": target.environment_id,
            "serviceId": service_id,
            "mountPath": mount_path,
        }
        try:
            await client.graphql(mutation, {"input": payload})
            return True
        except RailwayError as exc:
            if not _looks_like_schema_mismatch(str(exc)):
                log.warning("Railway volumeCreate failed: %s", exc)
                return False
            # Some schema revisions drop projectId from VolumeCreateInput.
            payload.pop("projectId", None)
            try:
                await client.graphql(mutation, {"input": payload})
                return True
            except RailwayError as retry_exc:
                log.warning("Railway volumeCreate fallback failed: %s", retry_exc)
                return False

    async def _create_service(
        self,
        ctx: commands.Context,
        *,
        name: str,
        source: dict[str, str] | None,
        branch: str | None,
    ) -> None:
        name = name.strip()
        if not name:
            raise RailwayError("Service name cannot be empty.")
        await self._defer(ctx)
        target = await self._target(ctx, environment=False)
        # ServiceCreateInput takes projectId/name/source/branch. It does NOT take
        # environmentId — sending one makes Railway reject the whole mutation.
        input_data: dict[str, Any] = {"projectId": target.project_id, "name": name}
        if source:
            input_data["source"] = source
        if branch:
            input_data["branch"] = branch
        async with self._write_lock(ctx.author.id):
            data = await (await self._client(ctx)).graphql(
                """
                mutation ServiceCreate($input: ServiceCreateInput!) {
                  serviceCreate(input: $input) { id name }
                }
                """,
                {"input": input_data},
            )
        service = data.get("serviceCreate")
        if not isinstance(service, dict) or not service.get("id"):
            raise RailwayError("Railway did not return the new service.")
        target.service_id = str(service["id"])
        target.service_name = str(service.get("name") or name)
        await self._save_target(ctx, target)
        detail = "empty" if not source else source.get("repo") or source.get("image")
        await self._send(
            ctx,
            embed=success_embed(
                f"Created and selected `{target.service_name}` from `{detail}`.\n"
                f"Set variables/config, then `{self._prefix(ctx)}railway deploy`.",
                title="🚄 Service created",
            ),
        )

    @service.command(name="rename")
    async def service_rename(self, ctx: commands.Context, *, name: str) -> None:
        """Rename the selected Railway service."""
        name = name.strip()
        if not name:
            raise RailwayError("Service name cannot be empty.")
        await self._defer(ctx)
        target = await self._target(ctx, environment=False, service=True)
        async with self._write_lock(ctx.author.id):
            data = await (await self._client(ctx)).graphql(
                """
                mutation ServiceUpdate($id: String!, $input: ServiceUpdateInput!) {
                  serviceUpdate(id: $id, input: $input) { id name }
                }
                """,
                {"id": target.service_id, "input": {"name": name}},
            )
        service = data.get("serviceUpdate") or {}
        target.service_name = str(service.get("name") or name)
        await self._save_target(ctx, target)
        await self._send(
            ctx,
            embed=success_embed(
                f"Renamed the selected service to `{target.service_name}`.",
                title="🚄 Service renamed",
            ),
        )

    @service.command(name="connect-repo", aliases=["repo", "connect"])
    async def service_connect_repo(
        self, ctx: commands.Context, repo: str, branch: str = "main"
    ) -> None:
        """Connect the selected service to a GitHub repository."""
        if not _is_repo(repo):
            raise RailwayError("Repository must be in `owner/repo` form.")
        await self._defer(ctx)
        target = await self._target(ctx, environment=False, service=True)
        async with self._write_lock(ctx.author.id):
            await (await self._client(ctx)).graphql(
                """
                mutation ServiceConnect($id: String!, $input: ServiceConnectInput!) {
                  serviceConnect(id: $id, input: $input) { id }
                }
                """,
                {
                    "id": target.service_id,
                    "input": {"repo": repo.strip(), "branch": branch.strip() or "main"},
                },
            )
        await self._send(
            ctx,
            embed=success_embed(
                f"Connected `{target.service_name}` to `{repo}` on `{branch or 'main'}`.\n"
                f"Run `{self._prefix(ctx)}railway deploy` when ready.",
                title="🚄 Repository connected",
            ),
        )

    @service.command(name="disconnect-repo", aliases=["disconnect-source", "disconnect"])
    async def service_disconnect_repo(
        self, ctx: commands.Context, confirmation: str = ""
    ) -> None:
        """Disconnect the selected service from its repo/image source."""
        target = await self._target(ctx, environment=False, service=True)
        if confirmation.lower() not in CONFIRM_WORDS:
            await self._send(
                ctx,
                embed=info_embed(
                    f"Use `{self._prefix(ctx)}railway service disconnect-repo confirm` "
                    f"to disconnect `{target.service_name}`'s source.",
                    title="🚄 Confirm source disconnect",
                ),
            )
            return
        await self._defer(ctx)
        async with self._write_lock(ctx.author.id):
            await (await self._client(ctx)).graphql(
                "mutation ServiceDisconnect($id: String!) { serviceDisconnect(id: $id) { id } }",
                {"id": target.service_id},
            )
        await self._send(
            ctx,
            embed=success_embed(
                f"Disconnected the source for `{target.service_name}`.",
                title="🚄 Service source disconnected",
            ),
        )

    @service.command(name="delete", aliases=["remove", "rm"])
    async def service_delete(self, ctx: commands.Context, confirmation: str = "") -> None:
        """Permanently delete the selected service and its deployments."""
        target = await self._target(ctx, environment=False, service=True)
        if confirmation.lower() not in CONFIRM_WORDS:
            await self._send(
                ctx,
                embed=info_embed(
                    f"This permanently deletes `{target.service_name}` and its "
                    f"deployments. Use `{self._prefix(ctx)}railway service delete confirm`.",
                    title="🚄 Confirm service deletion",
                ),
            )
            return
        await self._defer(ctx)
        async with self._write_lock(ctx.author.id):
            await (await self._client(ctx)).graphql(
                "mutation ServiceDelete($id: String!) { serviceDelete(id: $id) }",
                {"id": target.service_id},
            )
        deleted = target.service_name
        target.service_id = None
        target.service_name = None
        await self._save_target(ctx, target)
        await self._send(
            ctx,
            embed=success_embed(
                f"Deleted Railway service `{deleted}`.", title="🚄 Service deleted"
            ),
        )

    @service.command(name="deploy", aliases=["rebuild"])
    async def service_deploy(
        self, ctx: commands.Context, commit_sha: str | None = None
    ) -> None:
        """Build/deploy the selected service."""
        await self._deploy_selected_service(ctx, commit_sha)

    # ------------------------------------------------------------------
    # Service configuration
    # ------------------------------------------------------------------

    @railway.group(name="config", aliases=["cfg", "settings"], invoke_without_command=True)
    async def config_group(self, ctx: commands.Context) -> None:
        """Update build/start commands and service instance settings."""
        prefix = self._prefix(ctx)
        await self._send(
            ctx,
            embed=self._embed(
                "Service configuration",
                f"`{prefix}railway config show`\n"
                f"`{prefix}railway config build <command>`\n"
                f"`{prefix}railway config start <command>`\n"
                f"`{prefix}railway config root <directory>`\n"
                f"`{prefix}railway config dockerfile <path>`\n"
                f"`{prefix}railway config healthcheck <path>`\n"
                f"`{prefix}railway config region <region>` · "
                f"`{prefix}railway config replicas <n>`\n"
                f"`{prefix}railway config cron <schedule>`\n"
                f"`{prefix}railway config clear <build|start|root|dockerfile|healthcheck|cron> confirm`\n\n"
                "Config changes are staged — they apply on the next deploy.",
            ),
        )

    @config_group.command(name="show")
    async def config_show(self, ctx: commands.Context) -> None:
        """Show the selected service's current build/deploy configuration."""
        await self._defer(ctx)
        client = await self._client(ctx)
        target = await self._target(ctx, service=True)
        instance = await self._service_instance(client, target)
        embed = self._embed(f"{target.service_name} configuration")
        embed.add_field(
            name="Source",
            value=_code_or_unset(await self._service_source(client, target)),
            inline=False,
        )
        embed.add_field(
            name="Build command", value=_code_or_unset(instance.get("buildCommand")), inline=False
        )
        embed.add_field(
            name="Start command", value=_code_or_unset(instance.get("startCommand")), inline=False
        )
        embed.add_field(
            name="Root directory", value=_code_or_unset(instance.get("rootDirectory")), inline=False
        )
        embed.add_field(
            name="Dockerfile", value=_code_or_unset(instance.get("dockerfilePath")), inline=False
        )
        embed.add_field(
            name="Healthcheck", value=_code_or_unset(instance.get("healthcheckPath")), inline=False
        )
        embed.add_field(name="Region", value=_code_or_unset(instance.get("region")))
        embed.add_field(name="Replicas", value=_code_or_unset(instance.get("numReplicas")))
        embed.add_field(name="Cron", value=_code_or_unset(instance.get("cronSchedule")))
        embed.add_field(
            name="Restart policy", value=_code_or_unset(instance.get("restartPolicyType"))
        )
        await self._send(ctx, embed=embed)

    @config_group.command(name="build")
    async def config_build(self, ctx: commands.Context, *, command: str) -> None:
        """Set the build command."""
        await self._set_config_value(ctx, "buildCommand", command, "Build command")

    @config_group.command(name="start")
    async def config_start(self, ctx: commands.Context, *, command: str) -> None:
        """Set the start command."""
        await self._set_config_value(ctx, "startCommand", command, "Start command")

    @config_group.command(name="root")
    async def config_root(self, ctx: commands.Context, *, directory: str) -> None:
        """Set the root directory (monorepos)."""
        await self._set_config_value(ctx, "rootDirectory", directory, "Root directory")

    @config_group.command(name="dockerfile")
    async def config_dockerfile(self, ctx: commands.Context, *, path: str) -> None:
        """Set a custom Dockerfile path."""
        await self._set_config_value(ctx, "dockerfilePath", path, "Dockerfile path")

    @config_group.command(name="healthcheck")
    async def config_healthcheck(self, ctx: commands.Context, *, path: str) -> None:
        """Set the healthcheck path."""
        await self._set_config_value(ctx, "healthcheckPath", path, "Healthcheck path")

    @config_group.command(name="region")
    async def config_region(self, ctx: commands.Context, region: str) -> None:
        """Set the deployment region, e.g. us-west1."""
        await self._set_config_value(ctx, "region", region, "Region")

    @config_group.command(name="replicas")
    async def config_replicas(self, ctx: commands.Context, replicas: int) -> None:
        """Set the replica count."""
        if replicas < 0 or replicas > 50:
            raise RailwayError("Replica count must be between 0 and 50.")
        await self._set_config_value(ctx, "numReplicas", replicas, "Replica count")

    @config_group.command(name="cron")
    async def config_cron(self, ctx: commands.Context, *, schedule: str) -> None:
        """Set a cron schedule for the service."""
        await self._set_config_value(ctx, "cronSchedule", schedule, "Cron schedule")

    @config_group.command(name="clear")
    async def config_clear(
        self, ctx: commands.Context, setting: str, confirmation: str = ""
    ) -> None:
        """Clear one configuration value."""
        lookup = {
            "build": ("buildCommand", "Build command"),
            "start": ("startCommand", "Start command"),
            "root": ("rootDirectory", "Root directory"),
            "dockerfile": ("dockerfilePath", "Dockerfile path"),
            "healthcheck": ("healthcheckPath", "Healthcheck path"),
            "cron": ("cronSchedule", "Cron schedule"),
        }
        result = lookup.get(setting.strip().lower())
        if not result:
            raise RailwayError(f"Setting must be one of: {', '.join(sorted(lookup))}.")
        if confirmation.lower() not in CONFIRM_WORDS:
            await self._send(
                ctx,
                embed=info_embed(
                    f"Use `{self._prefix(ctx)}railway config clear {setting} confirm`.",
                    title="🚄 Confirm configuration clear",
                ),
            )
            return
        await self._set_config_value(ctx, result[0], None, f"Cleared {result[1].lower()}")

    async def _set_config_value(
        self, ctx: commands.Context, field: str, value: Any, label: str
    ) -> None:
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise RailwayError(f"{label} cannot be empty. Use `config clear` to remove it.")
        await self._defer(ctx)
        target = await self._target(ctx, service=True)
        async with self._write_lock(ctx.author.id):
            await self._update_instance(await self._client(ctx), target, {field: value})
        await self._send(
            ctx,
            embed=success_embed(
                f"{label} updated for `{target.service_name}`.\n"
                f"Railway stages this — run `{self._prefix(ctx)}railway redeploy` to apply it.",
                title="🚄 Service configuration updated",
            ),
        )

    # ------------------------------------------------------------------
    # Variables
    # ------------------------------------------------------------------

    @railway.group(
        name="variable", aliases=["variables", "var", "vars"], invoke_without_command=True
    )
    async def variable(self, ctx: commands.Context) -> None:
        """List and manage variables on the selected service/environment."""
        await self._variable_list(ctx)

    @variable.command(name="list")
    async def variable_list(self, ctx: commands.Context) -> None:
        """List variable names without exposing their values."""
        await self._variable_list(ctx)

    async def _variable_list(self, ctx: commands.Context) -> None:
        await self._defer(ctx)
        target = await self._target(ctx, service=False)
        values = await self._variables(await self._client(ctx), target)
        names = sorted(map(str, values.keys()), key=str.casefold)
        scope = target.service_name or f"shared variables in {target.environment_name}"
        if not names:
            await self._send(ctx, embed=info_embed(f"No variables found for `{scope}`."))
            return
        lines = []
        for name in names:
            raw = str(values[name])
            # References are not secrets, so show them; real values stay masked.
            shown = raw if raw.startswith("${{") and raw.endswith("}}") else "••••••••"
            lines.append(f"• `{name}` = `{_trim(shown, 80)}`")
        await self._send(
            ctx,
            embed=self._embed(
                f"Variables for {scope} ({len(names)})", _trim("\n".join(lines), 3900)
            ),
        )

    @variable.command(name="get")
    async def variable_get(self, ctx: commands.Context, name: str) -> None:
        """Read one variable value privately (DM or ephemeral slash reply)."""
        name = _normalise_variable_name(name)
        if not await self._require_private(ctx, "Variable values"):
            return
        await self._defer(ctx, ephemeral=True)
        target = await self._target(ctx, service=False)
        values = await self._variables(await self._client(ctx), target)
        if name not in values:
            raise RailwayError(f"Variable `{name}` was not found for the selected target.")
        value = _trim(str(values[name]).replace("```", "'''"), 3500)
        await self._send(
            ctx,
            embed=self._embed(f"Variable {name}", f"```text\n{value}\n```"),
            ephemeral=True,
        )

    @variable.command(name="resolved")
    async def variable_resolved(self, ctx: commands.Context) -> None:
        """Show variables as the deployment sees them, with references resolved."""
        if not await self._require_private(ctx, "Resolved variable values"):
            return
        await self._defer(ctx, ephemeral=True)
        target = await self._target(ctx, service=True)
        data = await (await self._client(ctx)).graphql(
            """
            query VariablesForServiceDeployment(
              $projectId: String!, $environmentId: String!, $serviceId: String!
            ) {
              variablesForServiceDeployment(
                projectId: $projectId,
                environmentId: $environmentId,
                serviceId: $serviceId
              )
            }
            """,
            {
                "projectId": target.project_id,
                "environmentId": target.environment_id,
                "serviceId": target.service_id,
            },
        )
        values = data.get("variablesForServiceDeployment") or {}
        if not isinstance(values, dict) or not values:
            await self._send(ctx, embed=info_embed("No resolved variables."), ephemeral=True)
            return
        rendered = json.dumps(values, indent=2, ensure_ascii=False).replace("```", "'''")
        await self._send(
            ctx,
            embed=self._embed(
                f"Resolved variables for {target.service_name}",
                f"```json\n{_trim(rendered, MAX_API_RESPONSE)}\n```",
            ),
            ephemeral=True,
        )

    @variable.command(name="set")
    async def variable_set(self, ctx: commands.Context, name: str, *, value: str) -> None:
        """Create or update a variable. The value is never echoed back."""
        await self._set_variable(ctx, name, value, skip_deploys=False)

    @variable.command(name="set-skip", aliases=["setskip", "set-no-deploy"])
    async def variable_set_skip(self, ctx: commands.Context, name: str, *, value: str) -> None:
        """Create/update a variable without triggering a deployment."""
        await self._set_variable(ctx, name, value, skip_deploys=True)

    async def _set_variable(
        self, ctx: commands.Context, name: str, value: str, *, skip_deploys: bool
    ) -> None:
        name = _normalise_variable_name(name)
        if not value:
            raise RailwayError("Variable value cannot be empty.")
        # Works anywhere: the value is never echoed, and the prefix message that
        # contained it is deleted when the bot can delete it.
        await self._scrub(ctx)
        await self._defer(ctx, ephemeral=True)
        target = await self._target(ctx, service=False)
        async with self._write_lock(ctx.author.id):
            await self._upsert_variable(
                await self._client(ctx), target, name, value, skip_deploys=skip_deploys
            )
        scope = target.service_name or f"shared / {target.environment_name}"
        note = "No deploy requested." if skip_deploys else "Railway may redeploy."
        await self._send(
            ctx,
            embed=success_embed(
                f"Set `{name}` on `{scope}`. {note}", title="🚄 Variable saved"
            ),
            ephemeral=True,
        )

    @variable.command(name="reference", aliases=["ref", "link"])
    async def variable_reference(
        self, ctx: commands.Context, name: str, service: str, source_name: str | None = None
    ) -> None:
        """Point a variable at another service's variable.

        `railway variable reference DATABASE_URL Postgres DATABASE_URL`
        sets it to ${{Postgres.DATABASE_URL}}. Use `shared` as the service to
        reference an environment-shared variable.
        """
        name = _normalise_variable_name(name)
        source_name = _normalise_variable_name(source_name or name)
        service = service.strip()
        if not SERVICE_REF_RE.fullmatch(service):
            raise RailwayError(
                "Service name for a reference must be the plain Railway service name, "
                "e.g. `Postgres`, or `shared` for environment-shared variables."
            )
        value = "${{" + f"{service}.{source_name}" + "}}"
        await self._defer(ctx)
        target = await self._target(ctx, service=True)
        async with self._write_lock(ctx.author.id):
            await self._upsert_variable(
                await self._client(ctx), target, name, value, skip_deploys=False
            )
        await self._send(
            ctx,
            embed=success_embed(
                f"`{name}` on `{target.service_name}` now references `{value}`.\n"
                "Railway resolves it at deploy time — if the service name is wrong the "
                "deployment will fail with an unresolved reference.",
                title="🚄 Variable reference set",
            ),
        )

    @variable.command(name="bulk")
    async def variable_bulk(self, ctx: commands.Context, *, variables_json: str) -> None:
        """Upsert a JSON object of variables. Omitted keys are left alone."""
        try:
            raw = json.loads(variables_json)
        except json.JSONDecodeError as exc:
            raise RailwayError(
                "Bulk variables must be a JSON object, e.g. `{\"PORT\": \"3000\"}`."
            ) from exc
        if not isinstance(raw, dict) or not raw:
            raise RailwayError("Bulk variables must be a non-empty JSON object.")
        if len(raw) > MAX_VARIABLES_PER_BULK_WRITE:
            raise RailwayError(
                f"Bulk writes are limited to {MAX_VARIABLES_PER_BULK_WRITE} variables."
            )
        variables: dict[str, str] = {}
        for key, value in raw.items():
            key = _normalise_variable_name(str(key))
            if not isinstance(value, (str, int, float, bool)):
                raise RailwayError(f"`{key}` must be a string, number, or boolean.")
            variables[key] = str(value)
        await self._scrub(ctx)
        await self._defer(ctx, ephemeral=True)
        target = await self._target(ctx, service=False)
        async with self._write_lock(ctx.author.id):
            await (await self._client(ctx)).graphql(
                """
                mutation VariableCollectionUpsert($input: VariableCollectionUpsertInput!) {
                  variableCollectionUpsert(input: $input)
                }
                """,
                {
                    "input": {
                        "projectId": target.project_id,
                        "environmentId": target.environment_id,
                        "serviceId": target.service_id,
                        "variables": variables,
                        # replace defaults to false; never nuke omitted keys here.
                        "replace": False,
                    }
                },
            )
        await self._send(
            ctx,
            embed=success_embed(
                f"Updated {len(variables)} variables. Omitted keys were left untouched.",
                title="🚄 Variables saved",
            ),
            ephemeral=True,
        )

    @variable.command(name="delete", aliases=["remove", "rm", "del"])
    async def variable_delete(
        self, ctx: commands.Context, name: str, confirmation: str = ""
    ) -> None:
        """Delete one variable from the selected service (or shared scope)."""
        await self._delete_variable(ctx, name, confirmation, "variable delete")

    async def _delete_variable(
        self, ctx: commands.Context, name: str, confirmation: str, command_name: str
    ) -> None:
        name = _normalise_variable_name(name)
        if confirmation.lower() not in CONFIRM_WORDS:
            await self._send(
                ctx,
                embed=info_embed(
                    f"Use `{self._prefix(ctx)}railway {command_name} {name} confirm`.",
                    title="🚄 Confirm variable deletion",
                ),
            )
            return
        await self._defer(ctx)
        target = await self._target(ctx, service=False)
        client = await self._client(ctx)
        payload = {
            "projectId": target.project_id,
            "environmentId": target.environment_id,
            "serviceId": target.service_id,
            "name": name,
        }
        async with self._write_lock(ctx.author.id):
            try:
                await client.graphql(
                    """
                    mutation VariableDelete($input: VariableDeleteInput!) {
                      variableDelete(input: $input)
                    }
                    """,
                    {"input": payload},
                )
            except RailwayError as exc:
                if not _looks_like_schema_mismatch(str(exc)):
                    raise
                await client.graphql(
                    """
                    mutation VariableDelete(
                      $projectId: String!, $environmentId: String!,
                      $serviceId: String, $name: String!
                    ) {
                      variableDelete(
                        projectId: $projectId, environmentId: $environmentId,
                        serviceId: $serviceId, name: $name
                      )
                    }
                    """,
                    payload,
                )
        await self._send(
            ctx,
            embed=success_embed(f"Deleted variable `{name}`.", title="🚄 Variable deleted"),
        )

    # ------------------------------------------------------------------
    # Deployment lifecycle
    # ------------------------------------------------------------------

    @railway.command(name="deployments", aliases=["history", "deps"])
    async def deployments(self, ctx: commands.Context, limit: int = 10) -> None:
        """List recent deployments for the selected service."""
        await self._defer(ctx)
        target = await self._target(ctx, service=True)
        deployments = await self._deployments(await self._client(ctx), target, limit)
        if not deployments:
            await self._send(ctx, embed=info_embed("No deployments found."))
            return
        lines = []
        for index, deployment in enumerate(deployments, start=1):
            rollback = " · rollback ok" if deployment.get("canRollback") else ""
            lines.append(
                f"`{index:02}` **{deployment.get('status') or 'UNKNOWN'}** "
                f"`{deployment.get('id') or 'unknown'}`{rollback}\n"
                f"└ `{deployment.get('createdAt') or 'unknown'}`"
            )
        await self._send(
            ctx,
            embed=self._embed(
                f"Deployments for {target.service_name}", _trim("\n".join(lines), 3900)
            ),
        )

    async def _deploy_selected_service(
        self, ctx: commands.Context, commit_sha: str | None
    ) -> None:
        if commit_sha:
            commit_sha = commit_sha.strip()
            # Tolerate the old `deploy confirm` habit.
            if commit_sha.lower() in CONFIRM_WORDS:
                commit_sha = None
            elif not COMMIT_SHA_RE.fullmatch(commit_sha):
                raise RailwayError("Commit SHA must be 7–64 hex characters.")
        await self._defer(ctx)
        target = await self._target(ctx, service=True)
        client = await self._client(ctx)
        async with self._write_lock(ctx.author.id):
            if commit_sha:
                data = await client.graphql(
                    """
                    mutation Deploy(
                      $serviceId: String!, $environmentId: String!, $commitSha: String!
                    ) {
                      serviceInstanceDeployV2(
                        serviceId: $serviceId,
                        environmentId: $environmentId,
                        commitSha: $commitSha
                      )
                    }
                    """,
                    {
                        "serviceId": target.service_id,
                        "environmentId": target.environment_id,
                        "commitSha": commit_sha,
                    },
                )
            else:
                data = await client.graphql(
                    """
                    mutation Deploy($serviceId: String!, $environmentId: String!) {
                      serviceInstanceDeployV2(
                        serviceId: $serviceId, environmentId: $environmentId
                      )
                    }
                    """,
                    {"serviceId": target.service_id, "environmentId": target.environment_id},
                )
        deployment_id = data.get("serviceInstanceDeployV2") or "requested"
        detail = f" at `{commit_sha}`" if commit_sha else ""
        await self._send(
            ctx,
            embed=success_embed(
                f"Deploy started for `{target.service_name}`{detail}.\n"
                f"Deployment: `{deployment_id}`\n"
                f"Watch it with `{self._prefix(ctx)}railway status`.",
                title="🚄 Deployment started",
            ),
        )

    @railway.command(name="deploy", aliases=["rebuild", "up"])
    async def deploy(self, ctx: commands.Context, commit_sha: str | None = None) -> None:
        """Deploy the selected service, optionally from a specific commit SHA."""
        await self._deploy_selected_service(ctx, commit_sha)

    @railway.command(name="redeploy")
    async def redeploy(self, ctx: commands.Context) -> None:
        """Redeploy the current commit and apply staged config changes."""
        await self._defer(ctx)
        target = await self._target(ctx, service=True)
        async with self._write_lock(ctx.author.id):
            await (await self._client(ctx)).graphql(
                """
                mutation Redeploy($serviceId: String!, $environmentId: String!) {
                  serviceInstanceRedeploy(
                    serviceId: $serviceId, environmentId: $environmentId
                  )
                }
                """,
                {"serviceId": target.service_id, "environmentId": target.environment_id},
            )
        await self._send(
            ctx,
            embed=success_embed(
                f"Redeploy requested for `{target.service_name}`.", title="🚄 Redeploy started"
            ),
        )

    @railway.command(name="restart")
    async def restart(self, ctx: commands.Context) -> None:
        """Restart the latest deployment without rebuilding."""
        await self._defer(ctx)
        target = await self._target(ctx, service=True)
        client = await self._client(ctx)
        deployment_id = str((await self._latest_deployment(client, target))["id"])
        async with self._write_lock(ctx.author.id):
            await client.graphql(
                "mutation Restart($id: String!) { deploymentRestart(id: $id) }",
                {"id": deployment_id},
            )
        await self._send(
            ctx,
            embed=success_embed(
                f"Restarted `{deployment_id}` for `{target.service_name}`.",
                title="🚄 Deployment restarted",
            ),
        )

    @railway.command(name="rollback")
    async def rollback(
        self, ctx: commands.Context, deployment_id: str, confirmation: str = ""
    ) -> None:
        """Roll back to an eligible deployment (see `railway deployments`)."""
        if confirmation.lower() not in CONFIRM_WORDS:
            await self._send(
                ctx,
                embed=info_embed(
                    f"Use `{self._prefix(ctx)}railway rollback {deployment_id} confirm`.",
                    title="🚄 Confirm rollback",
                ),
            )
            return
        await self._defer(ctx)
        client = await self._client(ctx)
        async with self._write_lock(ctx.author.id):
            data = await client.graphql(
                """
                mutation Rollback($id: String!) {
                  deploymentRollback(id: $id) { id status }
                }
                """,
                {"id": deployment_id},
            )
        deployment = data.get("deploymentRollback") or {}
        await self._send(
            ctx,
            embed=success_embed(
                f"Rollback requested. New deployment: `{deployment.get('id') or 'requested'}` "
                f"(`{deployment.get('status') or 'UNKNOWN'}`).",
                title="🚄 Rollback started",
            ),
        )

    @railway.command(name="stop")
    async def stop(self, ctx: commands.Context, confirmation: str = "") -> None:
        """Stop the latest deployment."""
        if confirmation.lower() not in CONFIRM_WORDS:
            await self._send(
                ctx,
                embed=info_embed(
                    f"This takes the service offline. Use "
                    f"`{self._prefix(ctx)}railway stop confirm`.",
                    title="🚄 Confirm stop",
                ),
            )
            return
        await self._defer(ctx)
        target = await self._target(ctx, service=True)
        client = await self._client(ctx)
        deployment_id = str((await self._latest_deployment(client, target))["id"])
        async with self._write_lock(ctx.author.id):
            await client.graphql(
                "mutation Stop($id: String!) { deploymentStop(id: $id) }", {"id": deployment_id}
            )
        await self._send(
            ctx,
            embed=success_embed(
                f"Stopped deployment `{deployment_id}`.", title="🚄 Deployment stopped"
            ),
        )

    @railway.command(name="cancel")
    async def cancel(self, ctx: commands.Context) -> None:
        """Cancel the latest queued/building deployment."""
        await self._defer(ctx)
        target = await self._target(ctx, service=True)
        client = await self._client(ctx)
        deployment_id = str((await self._latest_deployment(client, target))["id"])
        async with self._write_lock(ctx.author.id):
            await client.graphql(
                "mutation Cancel($id: String!) { deploymentCancel(id: $id) }",
                {"id": deployment_id},
            )
        await self._send(
            ctx,
            embed=success_embed(
                f"Cancelled deployment `{deployment_id}`.", title="🚄 Deployment cancelled"
            ),
        )

    @railway.command(name="logs")
    async def logs(self, ctx: commands.Context, kind: str = "run", limit: int = 20) -> None:
        """Show latest logs. `railway logs` for runtime, `railway logs build` for build."""
        kind = kind.strip().lower()
        if kind.isdigit():
            limit = int(kind)
            kind = "run"
        if kind not in {"run", "runtime", "deploy", "build"}:
            raise RailwayError("Log kind must be `run` or `build`.")
        await self._show_logs(ctx, limit, build=kind == "build")

    async def _redact_log_secrets(self, client: RailwayClient, target: RailwayTarget, body: str) -> str:
        """Remove current Railway variable values before logs reach Discord/AI/audit storage."""
        try:
            values = await self._variables(client, target, unrendered=True)
        except Exception:
            values = {}
        redacted = body
        for raw in values.values():
            value = str(raw or "")
            if len(value) >= 6 and not value.startswith("${{"):
                redacted = redacted.replace(value, "[REDACTED_RAILWAY_SECRET]")
        redacted = re.sub(r"(?i)\b(?:gho|ghp|ghu|ghr|github_pat)_[A-Za-z0-9_\-]{8,}\b", "[REDACTED_TOKEN]", redacted)
        redacted = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{12,}\b", "Bearer [REDACTED_TOKEN]", redacted)
        return redacted

    async def _show_logs(self, ctx: commands.Context, limit: int, *, build: bool) -> None:
        await self._defer(ctx)
        target = await self._target(ctx, service=True)
        client = await self._client(ctx)
        deployment_id = str((await self._latest_deployment(client, target))["id"])
        field = "buildLogs" if build else "deploymentLogs"
        data = await client.graphql(
            f"""
            query Logs($deploymentId: String!, $limit: Int) {{
              {field}(deploymentId: $deploymentId, limit: $limit) {{
                timestamp
                message
                severity
              }}
            }}
            """,
            {"deploymentId": deployment_id, "limit": max(1, min(limit, MAX_LOG_LINES))},
        )
        entries = data.get(field) or []
        if not isinstance(entries, list) or not entries:
            await self._send(ctx, embed=info_embed("No log lines returned."))
            return
        body = "\n".join(
            f"{str(entry.get('message') or '').rstrip()}"
            for entry in entries[-MAX_LOG_LINES:]
            if isinstance(entry, dict)
        ).replace("```", "'''")
        body = await self._redact_log_secrets(client, target, body)
        await self._send(
            ctx,
            embed=self._embed(
                f"{'Build' if build else 'Runtime'} logs · {target.service_name}",
                f"```log\n{_trim(body, 3800)}\n```",
            ),
        )

    # ------------------------------------------------------------------
    # Raw GraphQL escape hatch (private only, still per-user OAuth)
    # ------------------------------------------------------------------

    @railway.command(name="api", aliases=["graphql", "gql"])
    async def api(self, ctx: commands.Context, *, payload_json: str) -> None:
        """Run a GraphQL JSON payload with your own Railway OAuth token."""
        if not await self._require_private(ctx, "Raw Railway API responses"):
            return
        try:
            payload = json.loads(payload_json)
        except json.JSONDecodeError as exc:
            raise RailwayError(
                "Pass JSON like `{\"query\": \"query { me { id } }\", \"variables\": {}}`."
            ) from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("query"), str):
            raise RailwayError("Raw API payload must contain a string `query` field.")
        variables = payload.get("variables") or {}
        if not isinstance(variables, dict):
            raise RailwayError("Raw API `variables` must be an object.")
        await self._defer(ctx, ephemeral=True)
        result = await (await self._client(ctx)).graphql(payload["query"], variables)
        rendered = _trim(json.dumps(result, indent=2, ensure_ascii=False), MAX_API_RESPONSE)
        await self._send(
            ctx,
            embed=self._embed(
                "Railway API response", f"```json\n{rendered.replace('```', chr(39) * 3)}\n```"
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------

    async def cog_command_error(
        self, ctx: commands.Context, error: commands.CommandError
    ) -> None:
        original = getattr(error, "original", error)
        if isinstance(original, RailwayError):
            await self._send(
                ctx,
                embed=error_embed(_trim(str(original), 1800), title="🚄 Railway error"),
                ephemeral=getattr(ctx, "interaction", None) is not None,
            )
            return
        if isinstance(original, commands.MissingRequiredArgument):
            await self._send(
                ctx,
                embed=error_embed(
                    f"Missing `{original.param.name}`. Run `{self._prefix(ctx)}railway` for usage.",
                    title="🚄 Missing Railway argument",
                ),
            )
            return
        if isinstance(original, commands.BadArgument):
            await self._send(
                ctx, embed=error_embed(str(original), title="🚄 Invalid Railway argument")
            )
            return
        log.exception("Unhandled Railway command error", exc_info=original)
        await self._send(
            ctx,
            embed=error_embed(
                f"Unexpected error: `{type(original).__name__}: {_trim(str(original), 500)}`",
                title="🚄 Railway error",
            ),
        )


def _edge_nodes(edges: Any) -> list[dict[str, Any]]:
    if not isinstance(edges, list):
        return []
    return [
        edge["node"]
        for edge in edges
        if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
    ]


def _as_nodes(value: Any) -> list[dict[str, Any]]:
    """Accept either a plain list or a Relay connection."""
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return _edge_nodes(value.get("edges"))
    return []


def _resolve_node(nodes: list[dict[str, Any]], selection: str, kind: str) -> dict[str, Any]:
    selection = selection.strip()
    if not selection:
        raise RailwayError(f"Choose a {kind} by name or ID.")
    low = selection.casefold()
    exact = [
        node
        for node in nodes
        if str(node.get("id") or "") == selection
        or str(node.get("name") or "").casefold() == low
    ]
    if len(exact) == 1:
        return exact[0]
    matches = [node for node in nodes if low in str(node.get("name") or "").casefold()]
    if len(matches) == 1:
        return matches[0]
    if not matches and not exact:
        available = ", ".join(f"`{n.get('name')}`" for n in nodes[:10]) or "none"
        raise RailwayError(f"No {kind} matched `{selection}`. Available: {available}")
    raise RailwayError(f"`{selection}` matched several {kind}s. Use the ID instead.")


def _preferred_environment(environments: list[dict[str, Any]]) -> dict[str, Any] | None:
    for environment in environments:
        if str(environment.get("name") or "").casefold() == "production":
            return environment
    return environments[0] if environments else None


def _target_text(target: RailwayTarget) -> str:
    return (
        f"Project: `{target.project_name}`\n"
        f"Environment: `{target.environment_name or 'not selected'}`\n"
        f"Service: `{target.service_name or 'not selected'}`"
    )


def _maybe_string(value: Any) -> str | None:
    return str(value) if value not in (None, "") else None


def _normalise_variable_name(value: str) -> str:
    name = value.strip()
    if not VARIABLE_NAME_RE.fullmatch(name):
        raise RailwayError("Variable names must match `[A-Za-z_][A-Za-z0-9_]*`.")
    return name


def _is_repo(value: str) -> bool:
    owner, separator, name = value.strip().partition("/")
    return bool(separator and owner and name and "/" not in name)


def _graphql_error(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "Unknown Railway API error."
    errors = payload.get("errors")
    if not isinstance(errors, list) or not errors:
        return str(payload.get("message") or "Unknown Railway API error.")
    messages: list[str] = []
    for item in errors[:5]:
        message = (
            str(item.get("message") or "Unknown Railway error")
            if isinstance(item, dict)
            else str(item)
        )
        if message not in messages:
            messages.append(message)
    return "; ".join(messages)


def _looks_like_schema_mismatch(message: str) -> bool:
    low = message.casefold()
    return any(
        marker in low
        for marker in (
            "unknown argument",
            "unknown type",
            "unknown field",
            "cannot query field",
            "is not defined by type",
            "validation",
        )
    )


def _trim(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"


def _code_or_unset(value: Any) -> str:
    if value in (None, ""):
        return "`not set`"
    return f"`{_trim(str(value), 950)}`"


def _status_color(status: str) -> discord.Color:
    status = status.upper()
    if status == "SUCCESS":
        return discord.Color.green()
    if status in {"FAILED", "CRASHED", "REMOVED"}:
        return discord.Color.red()
    if status in {"BUILDING", "DEPLOYING", "QUEUED", "WAITING"}:
        return discord.Color.orange()
    return discord.Color.blurple()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Railway(bot))
