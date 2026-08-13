"""
Fun cog — counting game, last letter game, and misc fun commands.
"""
import logging
import random

import discord
from discord.ext import commands

from utils.helpers import error_embed, success_embed, info_embed

log = logging.getLogger("cogs.fun")

COUNTING_REACTIONS = {"correct": "✅", "wrong": "❌", "save": "🛡️"}


class Fun(commands.Cog):
    """🎲 Fun games and commands."""

    def __init__(self, bot):
        self.bot = bot

    # ── Counting game ──────────────────────────────────────────────────────────

    @commands.group(name="counting", invoke_without_command=True)
    @commands.guild_only()
    async def counting(self, ctx: commands.Context):
        """View counting game status."""
        row = await self.bot.db.get_counting(ctx.guild.id)
        if not row or not row["channel_id"]:
            return await ctx.send(embed=info_embed("Counting is not set up. Use `counting setup <channel>`."))
        channel = ctx.guild.get_channel(row["channel_id"])
        e = discord.Embed(title="🔢 Counting Game", color=discord.Color.blurple())
        e.add_field(name="Channel", value=channel.mention if channel else "Unknown")
        e.add_field(name="Current Count", value=str(row["count"]))
        e.add_field(name="High Score", value=str(row["high_score"]))
        await ctx.send(embed=e)

    @counting.command(name="setup", usage="counting setup <channel>")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def counting_setup(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the counting channel."""
        await self.bot.db.set_counting(ctx.guild.id, channel.id)
        await ctx.send(embed=success_embed(f"Counting channel set to {channel.mention}. Start counting from 1!"))

    @counting.command(name="reset", usage="counting reset")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def counting_reset(self, ctx: commands.Context):
        """Reset the count back to 0."""
        await self.bot.db.reset_counting(ctx.guild.id)
        await ctx.send(embed=success_embed("Counting reset to 0."))

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        # ── Counting ───────────────────────────────────────────────────────────
        row = await self.bot.db.get_counting(message.guild.id)
        if row and row["channel_id"] == message.channel.id:
            await self._handle_counting(message, row)

        # ── Last letter ────────────────────────────────────────────────────────
        ll_row = await self.bot.db.get_last_letter(message.guild.id)
        if ll_row and ll_row["channel_id"] == message.channel.id and ll_row["active"]:
            await self._handle_last_letter(message, ll_row)

    async def _handle_counting(self, message: discord.Message, row):
        content = message.content.strip()
        if not content.isdigit():
            return

        count = int(content)
        expected = row["count"] + 1
        last_user = row["last_user"]

        # No double-counting
        if message.author.id == last_user and expected > 1:
            await message.add_reaction(COUNTING_REACTIONS["wrong"])
            await self.bot.db.reset_counting(message.guild.id)
            await message.channel.send(
                embed=discord.Embed(
                    description=f"❌ {message.author.mention} ruined the count at **{row['count']}**! "
                                f"You can't count twice in a row.\n🔄 Count reset to 0. Start again from 1!",
                    color=discord.Color.red()
                )
            )
            return

        if count == expected:
            new_high = max(row["high_score"], count)
            await self.bot.db.update_counting(message.guild.id, count, message.author.id, new_high)
            await message.add_reaction(COUNTING_REACTIONS["correct"])
            if count > row["high_score"] and count > 1:
                await message.channel.send(
                    embed=discord.Embed(
                        description=f"🏆 New high score! **{count}** — keep going!",
                        color=discord.Color.gold()
                    ),
                    delete_after=10
                )
        else:
            await message.add_reaction(COUNTING_REACTIONS["wrong"])
            await self.bot.db.reset_counting(message.guild.id)
            await message.channel.send(
                embed=discord.Embed(
                    description=f"❌ {message.author.mention} ruined the count at **{row['count']}**! "
                                f"Next was `{expected}` but got `{count}`.\n"
                                f"🔄 Count reset to 0. Start again from 1!",
                    color=discord.Color.red()
                )
            )

    async def _handle_last_letter(self, message: discord.Message, row):
        word = message.content.strip().lower()

        # Must be a single word
        if not word.isalpha():
            return

        last_word = row["last_word"]
        last_user = row["last_user"]

        if last_word:
            # Can't go twice in a row
            if message.author.id == last_user:
                await message.add_reaction("❌")
                await message.channel.send(
                    embed=error_embed(f"{message.author.mention} can't go twice in a row!"),
                    delete_after=5
                )
                return

            if word[0] != last_word[-1]:
                await message.add_reaction("❌")
                await message.channel.send(
                    embed=error_embed(
                        f"'{word}' doesn't start with `{last_word[-1].upper()}`! "
                        f"The last word was **{last_word}**."
                    ),
                    delete_after=8
                )
                return

        await self.bot.db.update_last_letter(message.guild.id, word, message.author.id)
        await message.add_reaction("✅")

    @commands.group(name="lastletter", aliases=["ll"], invoke_without_command=True)
    @commands.guild_only()
    async def lastletter(self, ctx: commands.Context):
        """View last letter game status."""
        row = await self.bot.db.get_last_letter(ctx.guild.id)
        if not row or not row["channel_id"]:
            return await ctx.send(embed=info_embed("Last letter is not set up. Use `lastletter setup <channel>`."))
        channel = ctx.guild.get_channel(row["channel_id"])
        e = discord.Embed(title="🔤 Last Letter Game", color=discord.Color.blurple())
        e.add_field(name="Channel", value=channel.mention if channel else "Unknown")
        e.add_field(name="Last Word", value=row["last_word"] or "None (start with any word!)")
        e.add_field(name="Status", value="Active ✅" if row["active"] else "Inactive ❌")
        await ctx.send(embed=e)

    @lastletter.command(name="setup", usage="lastletter setup <channel>")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def ll_setup(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the last letter channel."""
        await self.bot.db.set_last_letter_channel(ctx.guild.id, channel.id)
        await ctx.send(embed=success_embed(f"Last letter channel set to {channel.mention}. Start the game!"))

    @lastletter.command(name="reset", usage="lastletter reset")
    @commands.guild_only()
    @commands.has_permissions(manage_channels=True)
    async def ll_reset(self, ctx: commands.Context):
        """Reset the last letter game."""
        await self.bot.db._db.execute(
            "UPDATE last_letter SET last_word = NULL, last_user = 0 WHERE guild_id = ?",
            (ctx.guild.id,)
        )
        await self.bot.db._db.commit()
        await ctx.send(embed=success_embed("Last letter game reset."))

    # ── Misc fun ───────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="roll", usage="roll [NdN]")
    async def roll(self, ctx: commands.Context, dice: str = "1d6"):
        """Roll dice. Format: 2d6, 1d20, etc."""
        try:
            n, sides = map(int, dice.lower().split("d"))
            if n < 1 or sides < 1 or n > 100:
                raise ValueError
        except Exception:
            return await ctx.send(embed=error_embed("Format: `2d6`, `1d20`, etc. Max 100 dice."))

        rolls = [random.randint(1, sides) for _ in range(n)]
        total = sum(rolls)
        result = " + ".join(str(r) for r in rolls) if n > 1 else str(total)
        await ctx.send(embed=discord.Embed(
            title=f"🎲 Rolled {dice}",
            description=f"**{result}** = **{total}**" if n > 1 else f"**{total}**",
            color=discord.Color.blurple()
        ))

    @commands.hybrid_command(name="flip", aliases=["coinflip"])
    async def flip(self, ctx: commands.Context):
        """Flip a coin."""
        result = random.choice(["Heads 🪙", "Tails 🪙"])
        await ctx.send(embed=discord.Embed(title="Coin Flip", description=f"**{result}**", color=discord.Color.gold()))

    @commands.hybrid_command(name="8ball", usage="8ball <question>")
    async def eightball(self, ctx: commands.Context, *, question: str):
        """Ask the magic 8-ball."""
        responses = [
            "It is certain.", "It is decidedly so.", "Without a doubt.",
            "Yes, definitely.", "You may rely on it.", "As I see it, yes.",
            "Most likely.", "Outlook good.", "Yes.", "Signs point to yes.",
            "Reply hazy, try again.", "Ask again later.", "Better not tell you now.",
            "Cannot predict now.", "Concentrate and ask again.",
            "Don't count on it.", "My reply is no.", "My sources say no.",
            "Outlook not so good.", "Very doubtful."
        ]
        e = discord.Embed(color=discord.Color.purple())
        e.add_field(name="Question", value=question)
        e.add_field(name="🎱 Answer", value=random.choice(responses))
        await ctx.send(embed=e)

    @commands.hybrid_command(name="choose", usage="choose <option1> | <option2> ...")
    async def choose(self, ctx: commands.Context, *, options: str):
        """Choose between options (separate with `|`)."""
        choices = [o.strip() for o in options.split("|") if o.strip()]
        if len(choices) < 2:
            return await ctx.send(embed=error_embed("Provide at least 2 options separated by `|`."))
        picked = random.choice(choices)
        await ctx.send(embed=discord.Embed(
            title="🤔 I choose...",
            description=f"**{picked}**",
            color=discord.Color.blurple()
        ))

    @commands.hybrid_command(name="poll", usage="poll <question> | <option1> | <option2> ...")
    @commands.guild_only()
    async def poll(self, ctx: commands.Context, *, text: str):
        """Create a poll with up to 9 options."""
        parts = [p.strip() for p in text.split("|") if p.strip()]
        if len(parts) < 2:
            return await ctx.send(embed=error_embed("Format: `poll Question | Option 1 | Option 2 ...`"))

        question, *options = parts
        if len(options) > 9:
            return await ctx.send(embed=error_embed("Max 9 options."))

        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]
        lines = [f"{number_emojis[i]} {opt}" for i, opt in enumerate(options)]
        e = discord.Embed(title=f"📊 {question}", description="\n".join(lines), color=discord.Color.blurple())
        e.set_footer(text=f"Poll by {ctx.author}", icon_url=ctx.author.display_avatar.url)
        msg = await ctx.send(embed=e)

        for i in range(len(options)):
            await msg.add_reaction(number_emojis[i])

    @commands.hybrid_command(name="rps", usage="rps <rock|paper|scissors>")
    async def rps(self, ctx: commands.Context, choice: str):
        """Play Rock Paper Scissors against the bot."""
        choice = choice.lower()
        valid = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
        if choice not in valid:
            return await ctx.send(embed=error_embed("Choose `rock`, `paper`, or `scissors`."))

        bot_choice = random.choice(list(valid.keys()))
        wins = {"rock": "scissors", "paper": "rock", "scissors": "paper"}

        if bot_choice == choice:
            result, color = "It's a tie!", discord.Color.yellow()
        elif wins[choice] == bot_choice:
            result, color = "You win! 🎉", discord.Color.green()
        else:
            result, color = "You lose! 😢", discord.Color.red()

        e = discord.Embed(title="Rock Paper Scissors", color=color)
        e.add_field(name="You", value=f"{valid[choice]} {choice.capitalize()}")
        e.add_field(name="Bot", value=f"{valid[bot_choice]} {bot_choice.capitalize()}")
        e.add_field(name="Result", value=f"**{result}**", inline=False)
        await ctx.send(embed=e)


async def setup(bot):
    await bot.add_cog(Fun(bot))
