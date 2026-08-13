"""Globally prevent TweakBot from sending Discord embeds.

Some legacy cogs still build :class:`discord.Embed` objects.  Rewriting every
command would be brittle, so this module converts embed payloads into readable
plain text at Discord's send/edit boundary.  Files, views, polls, stickers,
mentions and delete-after behavior are preserved.
"""
from __future__ import annotations

from typing import Any, Iterable

import discord
from discord.ext import commands

_INSTALLED = False
_MAX_MESSAGE_LENGTH = 2000


def embed_to_text(embed: discord.Embed) -> str:
    """Render one embed as readable plain Discord text."""
    parts: list[str] = []

    author = getattr(embed, "author", None)
    if author and getattr(author, "name", None):
        parts.append(str(author.name).strip())

    if embed.title:
        parts.append(str(embed.title).strip())
    if embed.description:
        parts.append(str(embed.description).strip())

    for field in embed.fields:
        name = str(field.name or "").strip()
        value = str(field.value or "").strip()
        if name and value:
            parts.append(f"{name}\n{value}")
        elif name or value:
            parts.append(name or value)

    footer = getattr(embed, "footer", None)
    if footer and getattr(footer, "text", None):
        parts.append(str(footer.text).strip())

    return "\n\n".join(part for part in parts if part).strip()


def _embed_texts(items: Iterable[Any]) -> list[str]:
    result: list[str] = []
    for item in items:
        if isinstance(item, discord.Embed):
            rendered = embed_to_text(item)
            if rendered:
                result.append(rendered)
    return result


def _merge_content(content: Any, kwargs: dict[str, Any]) -> str | None:
    chunks: list[str] = []
    if content is not None:
        value = str(content).strip()
        if value:
            chunks.append(value)

    embed = kwargs.pop("embed", None)
    if isinstance(embed, discord.Embed):
        rendered = embed_to_text(embed)
        if rendered:
            chunks.append(rendered)

    embeds = kwargs.pop("embeds", None)
    if embeds:
        chunks.extend(_embed_texts(embeds))

    if chunks:
        return "\n\n".join(chunks)[:_MAX_MESSAGE_LENGTH]

    # A content-less message is valid when it carries another payload.
    payload_keys = ("file", "files", "view", "poll", "stickers")
    if any(kwargs.get(key) is not None for key in payload_keys):
        return None
    return "No details were provided."


def install_plaintext_output() -> None:
    """Install the conversion layer exactly once, before cogs are loaded."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    original_messageable_send = discord.abc.Messageable.send
    original_context_send = commands.Context.send
    original_message_edit = discord.Message.edit

    async def messageable_send(self, content=None, **kwargs):
        content = _merge_content(content, kwargs)
        return await original_messageable_send(self, content=content, **kwargs)

    async def context_send(self, content=None, **kwargs):
        content = _merge_content(content, kwargs)
        return await original_context_send(self, content=content, **kwargs)

    async def message_edit(self, **kwargs):
        if "embed" in kwargs or "embeds" in kwargs:
            existing = kwargs.pop("content", None)
            kwargs["content"] = _merge_content(existing, kwargs)
        return await original_message_edit(self, **kwargs)

    discord.abc.Messageable.send = messageable_send
    commands.Context.send = context_send
    discord.Message.edit = message_edit
