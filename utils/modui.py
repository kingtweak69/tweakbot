"""
Shared UI and permission helpers for the moderation, security, and backup cogs.
"""
from __future__ import annotations

import datetime

import discord


def utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def clip(value: str, limit: int = 1024) -> str:
    value = value or ""
    if len(value) <= limit:
        return value or "*none*"
    return value[: limit - 1] + "…"


def chunk_field(embed: discord.Embed, name: str, items: list[str], sep: str = "\n", max_fields: int = 4):
    """Spread a long list across as many 1024-char fields as it needs."""
    if not items:
        return
    blocks: list[str] = []
    current = ""
    for item in items:
        candidate = f"{current}{sep}{item}" if current else item
        if len(candidate) > 1024:
            blocks.append(current)
            current = item
        else:
            current = candidate
    if current:
        blocks.append(current)

    for index, block in enumerate(blocks[:max_fields]):
        embed.add_field(name=name if index == 0 else f"{name} (cont.)", value=block, inline=False)
    if len(blocks) > max_fields:
        embed.add_field(name=f"{name} (truncated)", value=f"…and {len(blocks) - max_fields} more block(s).", inline=False)


class ConfirmView(discord.ui.View):
    """Yes/no buttons scoped to one user and one message."""

    def __init__(self, author_id: int, timeout: float = 30.0, confirm_label: str = "Confirm"):
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: bool | None = None
        self.confirm.label = confirm_label

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return False
        return True

    def _finish(self):
        for child in self.children:
            child.disabled = True
        self.stop()

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        self._finish()
        await interaction.response.edit_message(view=self)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        self._finish()
        await interaction.response.edit_message(view=self)


def hierarchy_block(
    guild: discord.Guild,
    actor: discord.Member,
    target: discord.Member,
    action: str = "act on",
) -> str | None:
    """
    Why this action can't proceed, or None if it can.

    The original code only checked the invoker's role. It never checked the
    bot's, so half the "valid" targets came back as Forbidden mid-loop.
    """
    if target.id == actor.id:
        return f"you can't {action} yourself"
    if target.id == guild.owner_id:
        return "target is the server owner"
    if target.id == guild.me.id:
        return f"I can't {action} myself"
    if actor.id != guild.owner_id and target.top_role >= actor.top_role:
        return "their role is equal to or above yours"
    if target.top_role >= guild.me.top_role:
        return "their role is above mine"
    return None
