"""Runtime capability registry for TweakBot's agent layer.

Cogs register executable AI capabilities here instead of teaching the
conversation cog about every subsystem.  The registry is intentionally small:
it owns tool metadata, discovery, lifecycle cleanup, and dispatch.  Permission,
OAuth, confirmation, cooldown, and business rules remain inside the handlers
that already own those operations.
"""
from __future__ import annotations

import inspect
import json
import logging
import re

import config
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from discord.ext import commands

log = logging.getLogger("utils.capabilities")


_SECRET_KEY_RE = re.compile(
    r"(token|secret|password|api[_-]?key|authorization|private[_-]?key|client[_-]?secret|refresh[_-]?token)",
    re.I,
)
_TOKEN_RE = re.compile(
    r"(?i)\b(?:gho|ghp|ghu|ghr|github_pat)_[A-Za-z0-9_\-]{8,}\b"
    r"|\bBearer\s+[A-Za-z0-9._\-]{12,}\b"
)
def _audit_safe(value: Any, *, limit: int = 12000) -> str:
    """Persist tool telemetry without persisting obvious credentials."""
    def scrub(obj: Any) -> Any:
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                if _SECRET_KEY_RE.search(str(k)):
                    out[str(k)] = "[REDACTED]"
                else:
                    out[str(k)] = scrub(v)
            return out
        if isinstance(obj, list):
            return [scrub(v) for v in obj]
        return obj
    try:
        text = json.dumps(scrub(value), ensure_ascii=False) if not isinstance(value, str) else value
    except Exception:
        text = str(value)
    return _TOKEN_RE.sub("[REDACTED_TOKEN]", text)[:limit]


CapabilityHandler = Callable[[commands.Context, dict[str, Any]], Awaitable[str] | str]
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


@dataclass(slots=True)
class Capability:
    name: str
    description: str
    handler: CapabilityHandler
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}}
    )
    category: str = "general"
    source: str = "unknown"
    guild_only: bool = False
    destructive: bool = False

    def openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class CapabilityRegistry:
    """Process-local registry of AI-callable operations.

    A registry belongs to one Bot instance.  Cogs should register in
    ``cog_load`` and call ``unregister_source`` from ``cog_unload`` so hot reloads
    cannot leave stale handlers behind.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._items: dict[str, Capability] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        handler: CapabilityHandler,
        parameters: dict[str, Any] | None = None,
        category: str = "general",
        source: str = "unknown",
        guild_only: bool = False,
        destructive: bool = False,
        replace: bool = False,
    ) -> Capability:
        name = str(name or "").strip()
        if not _NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid capability name: {name!r}")
        if not callable(handler):
            raise TypeError(f"Capability {name!r} handler is not callable")
        if name in self._items and not replace:
            previous = self._items[name]
            raise ValueError(
                f"Capability {name!r} is already registered by {previous.source!r}"
            )

        schema = parameters or {"type": "object", "properties": {}}
        if not isinstance(schema, dict) or schema.get("type") != "object":
            raise ValueError(f"Capability {name!r} parameters must be an object schema")

        capability = Capability(
            name=name,
            description=str(description or "").strip(),
            handler=handler,
            parameters=schema,
            category=str(category or "general").strip() or "general",
            source=str(source or "unknown").strip() or "unknown",
            guild_only=bool(guild_only),
            destructive=bool(destructive),
        )
        self._items[name] = capability
        log.info(
            "Registered AI capability %s (source=%s category=%s)",
            capability.name,
            capability.source,
            capability.category,
        )
        return capability

    def unregister(self, name: str) -> bool:
        capability = self._items.pop(name, None)
        if capability is None:
            return False
        log.info("Unregistered AI capability %s", name)
        return True

    def unregister_source(self, source: str) -> int:
        names = [
            name for name, capability in self._items.items()
            if capability.source == source
        ]
        for name in names:
            self.unregister(name)
        return len(names)

    def get(self, name: str) -> Capability | None:
        return self._items.get(name)

    def available(self, ctx: commands.Context) -> list[Capability]:
        result: list[Capability] = []
        for capability in self._items.values():
            if capability.guild_only and ctx.guild is None:
                continue
            result.append(capability)
        return sorted(result, key=lambda item: (item.category, item.name))

    def openai_tools(self, ctx: commands.Context) -> list[dict[str, Any]]:
        return [capability.openai_tool() for capability in self.available(ctx)]

    def describe(self, ctx: commands.Context, query: str = "") -> str:
        query = str(query or "").strip().casefold()
        lines: list[str] = []
        for capability in self.available(ctx):
            searchable = (
                f"{capability.name} {capability.category} "
                f"{capability.description} {capability.source}"
            ).casefold()
            if query and query not in searchable:
                continue
            marker = " destructive" if capability.destructive else ""
            lines.append(
                f"{capability.name} [{capability.category}{marker}] — "
                f"{capability.description}"
            )
        if not lines:
            return (
                f"No registered capabilities matched {query!r}."
                if query else "No AI capabilities are registered."
            )
        return "Registered TweakBot capabilities:\n" + "\n".join(lines[:200])

    async def execute(
        self,
        ctx: commands.Context,
        name: str,
        args: dict[str, Any] | None = None,
    ) -> str:
        capability = self.get(name)
        if capability is None:
            return f"Unknown capability {name!r}."
        if capability.guild_only and ctx.guild is None:
            return f"Capability {name!r} only works inside a server."
        if capability.destructive and not bool(getattr(config, "AI_DESTRUCTIVE_TOOLS_ENABLED", False)):
            return (
                f"Capability {name!r} is blocked because AI destructive tools "
                "are disabled. Enable AI_DESTRUCTIVE_TOOLS_ENABLED explicitly "
                "if you want the model to execute destructive operations."
            )

        payload = args if isinstance(args, dict) else {}
        try:
            result = capability.handler(ctx, payload)
            if inspect.isawaitable(result):
                result = await result
            text = str(result if result is not None else "Capability completed.")[:12000]
            if getattr(self.bot, "db", None):
                try:
                    await self.bot.db.log_ai_tool_event(
                        user_id=int(ctx.author.id),
                        guild_id=int(ctx.guild.id) if ctx.guild else 0,
                        channel_id=int(ctx.channel.id),
                        capability=capability.name,
                        arguments=_audit_safe(payload),
                        result=_audit_safe(text),
                    )
                except Exception:
                    log.exception("Could not persist tool event for %s", capability.name)
            return text
        except commands.CommandError as exc:
            log.warning("Capability %s command error: %s", name, exc)
            return f"Capability {name!r} failed: {type(exc).__name__}: {exc}"[:1200]
        except Exception as exc:
            log.exception("Capability %s failed", name)
            return f"Capability {name!r} failed: {type(exc).__name__}: {exc}"[:1200]

    def __len__(self) -> int:
        return len(self._items)
