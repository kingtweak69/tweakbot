"""
Music cog — Lavalink via Wavelink 3.x.

Adds a live "player controller": a single embed with buttons that updates
itself while a track plays (progress bar, elapsed time, queue count) and
rebuilds itself whenever a new track starts.

Play, queue, skip, seek, loop, shuffle, filters, and more.
Falls back to a "no Lavalink" notice if connection fails.
"""
import asyncio
import logging
import random
from collections import deque

import discord
import wavelink
from discord.ext import commands, tasks

from utils.helpers import error_embed, success_embed, info_embed

log = logging.getLogger("cogs.music")

LAVALINK_CONNECT_RETRIES = 3
CONTROLLER_REFRESH_SECONDS = 12
IDLE_DISCONNECT_SECONDS = 300

BAR_LENGTH = 22
BAR_FILL = "\u2501"      # ━
BAR_EMPTY = "\u2500"     # ─
BAR_KNOB = "\u25c9"      # ◉

COLOR_PLAYING = discord.Color.from_str("#5865F2")
COLOR_PAUSED = discord.Color.from_str("#FAA61A")
COLOR_IDLE = discord.Color.from_str("#4F545C")


def is_in_vc():
    async def predicate(ctx: commands.Context):
        if not ctx.author.voice:
            raise commands.CheckFailure("You must be in a voice channel to use music commands.")
        return True
    return commands.check(predicate)


def bot_in_vc():
    async def predicate(ctx: commands.Context):
        if not ctx.voice_client:
            raise commands.CheckFailure("I'm not connected to a voice channel.")
        return True
    return commands.check(predicate)


class MusicPlayer(wavelink.Player):
    """Player that remembers where it was summoned from and its controller message."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.home: discord.abc.Messageable | None = None
        self.controller: discord.Message | None = None
        self.controller_view: "PlayerController | None" = None
        self.autoplay = wavelink.AutoPlayMode.partial

        # AI DJ state. This is intentionally per-player/per-guild so two servers
        # can run completely different sessions at the same time.
        self.dj_enabled: bool = False
        self.dj_style: str = ""
        self.dj_energy: int = 7
        self.dj_target_queue: int = 3
        self.dj_recent: deque[str] = deque(maxlen=24)
        self.dj_lock = asyncio.Lock()

    async def teardown_controller(self):
        """Delete the old controller message and kill its view."""
        if self.controller_view:
            self.controller_view.stop()
            self.controller_view = None
        if self.controller:
            try:
                await self.controller.delete()
            except discord.HTTPException:
                pass
            self.controller = None


class PlayerController(discord.ui.View):
    """Button panel attached to the now-playing embed."""

    def __init__(self, cog: "Music", player: MusicPlayer):
        super().__init__(timeout=None)
        self.cog = cog
        self.player = player
        self._sync_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not self.player or not self.player.connected:
            await interaction.response.send_message("The player is gone.", ephemeral=True)
            return False
        if not interaction.user.voice or interaction.user.voice.channel != self.player.channel:
            await interaction.response.send_message(
                "You need to be in my voice channel to use these.", ephemeral=True
            )
            return False
        return True

    def _sync_buttons(self):
        """Keep button labels/styles in sync with player state."""
        self.play_pause.emoji = "\u25b6\ufe0f" if self.player.paused else "\u23f8\ufe0f"
        self.play_pause.style = discord.ButtonStyle.success if self.player.paused else discord.ButtonStyle.secondary

        mode = self.player.queue.mode
        if mode == wavelink.QueueMode.loop:
            self.loop_toggle.emoji = "\U0001f502"
            self.loop_toggle.style = discord.ButtonStyle.primary
        elif mode == wavelink.QueueMode.loop_all:
            self.loop_toggle.emoji = "\U0001f501"
            self.loop_toggle.style = discord.ButtonStyle.primary
        else:
            self.loop_toggle.emoji = "\U0001f501"
            self.loop_toggle.style = discord.ButtonStyle.secondary

        self.autoplay_toggle.style = (
            discord.ButtonStyle.primary
            if self.player.autoplay == wavelink.AutoPlayMode.enabled
            else discord.ButtonStyle.secondary
        )

    async def refresh(self, interaction: discord.Interaction):
        self._sync_buttons()
        await interaction.response.edit_message(
            embed=self.cog.build_now_playing(self.player), view=self
        )

    # ── Row 0 ──────────────────────────────────────────────────────────────────

    @discord.ui.button(emoji="\u23ee\ufe0f", style=discord.ButtonStyle.secondary, row=0)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button):
        history = self.player.queue.history
        if len(history) < 2:
            return await interaction.response.send_message("No previous track.", ephemeral=True)
        # history[-1] is the current track, so grab the one before it
        prev = history[-2]
        self.player.queue.put_at(0, prev)
        await interaction.response.defer()
        await self.player.skip(force=True)

    @discord.ui.button(emoji="\u23f8\ufe0f", style=discord.ButtonStyle.secondary, row=0)
    async def play_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.pause(not self.player.paused)
        await self.refresh(interaction)

    @discord.ui.button(emoji="\u23ed\ufe0f", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await self.player.skip(force=True)

    @discord.ui.button(emoji="\u23f9\ufe0f", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.queue.clear()
        self.player.queue.reset()
        await interaction.response.defer()
        await self.player.teardown_controller()
        await self.player.disconnect()

    # ── Row 1 ──────────────────────────────────────────────────────────────────

    @discord.ui.button(emoji="\U0001f509", style=discord.ButtonStyle.secondary, row=1)
    async def vol_down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.set_volume(max(0, self.player.volume - 10))
        await self.refresh(interaction)

    @discord.ui.button(emoji="\U0001f50a", style=discord.ButtonStyle.secondary, row=1)
    async def vol_up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.player.set_volume(min(200, self.player.volume + 10))
        await self.refresh(interaction)

    @discord.ui.button(emoji="\U0001f501", style=discord.ButtonStyle.secondary, row=1)
    async def loop_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        mode = self.player.queue.mode
        if mode == wavelink.QueueMode.normal:
            self.player.queue.mode = wavelink.QueueMode.loop
        elif mode == wavelink.QueueMode.loop:
            self.player.queue.mode = wavelink.QueueMode.loop_all
        else:
            self.player.queue.mode = wavelink.QueueMode.normal
        await self.refresh(interaction)

    @discord.ui.button(emoji="\U0001f500", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.player.queue.count < 2:
            return await interaction.response.send_message(
                "Need at least 2 queued tracks to shuffle.", ephemeral=True
            )
        self.player.queue.shuffle()
        await self.refresh(interaction)

    @discord.ui.button(emoji="\u2728", style=discord.ButtonStyle.secondary, row=1)
    async def autoplay_toggle(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.player.autoplay == wavelink.AutoPlayMode.enabled:
            self.player.autoplay = wavelink.AutoPlayMode.partial
        else:
            self.player.autoplay = wavelink.AutoPlayMode.enabled
        await self.refresh(interaction)

    # ── Row 2 ──────────────────────────────────────────────────────────────────

    @discord.ui.button(label="Queue", emoji="\U0001f4dc", style=discord.ButtonStyle.secondary, row=2)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        tracks = list(self.player.queue)
        if not tracks:
            return await interaction.response.send_message("The queue is empty.", ephemeral=True)
        lines = [
            f"`{i}.` [{_trim(t.title)}]({t.uri}) \u2014 `{_fmt_duration(t.length)}`"
            for i, t in enumerate(tracks[:15], 1)
        ]
        if len(tracks) > 15:
            lines.append(f"\n*...and {len(tracks) - 15} more*")
        e = discord.Embed(
            title="\U0001f3b6 Up Next",
            description="\n".join(lines),
            color=COLOR_PLAYING,
        )
        e.set_footer(text=f"{len(tracks)} tracks | {_fmt_duration(sum(t.length for t in tracks))} total")
        await interaction.response.send_message(embed=e, ephemeral=True)


class Music(commands.Cog):
    """\U0001f3b5 Music player powered by Lavalink."""

    def __init__(self, bot):
        self.bot = bot
        self._connected = False

    def _register_capabilities(self) -> None:
        import config
        if not getattr(config, "AI_MUSIC_TOOLS_ENABLED", True):
            return

        registry = self.bot.capabilities
        source = "music"
        definitions = [
            (
                "dj_start",
                "Start autonomous DJ mode in the requester's voice channel.",
                {
                    "type": "object",
                    "properties": {
                        "style": {"type": "string"},
                        "energy": {"type": "integer", "minimum": 1, "maximum": 10},
                    },
                },
                lambda ctx, a: self.tool_dj_start(
                    ctx,
                    style=str(a.get("style") or "open format"),
                    energy=int(a.get("energy") or 7),
                ),
            ),
            ("dj_stop", "Turn off autonomous DJ selection while preserving human requests.", {"type":"object","properties":{}}, lambda ctx, a: self.tool_dj_stop(ctx)),
            ("dj_set_style", "Change the active DJ genre, era, artist direction, or mood.", {"type":"object","properties":{"style":{"type":"string"}},"required":["style"]}, lambda ctx, a: self.tool_dj_style(ctx, str(a.get("style") or ""))),
            ("dj_set_energy", "Change the active DJ energy from 1 to 10.", {"type":"object","properties":{"energy":{"type":"integer","minimum":1,"maximum":10}},"required":["energy"]}, lambda ctx, a: self.tool_dj_energy(ctx, int(a.get("energy") or 7))),
            ("dj_request", "Put a requested song or artist at the front of the DJ queue.", {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}, lambda ctx, a: self.tool_dj_request(ctx, str(a.get("query") or ""))),
            ("dj_status", "Read the current DJ style, energy, track, and queue state.", {"type":"object","properties":{}}, lambda ctx, a: self.tool_dj_status(ctx)),
            ("music_play", "Play or queue a song from a title, artist, search query, or URL.", {"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}, lambda ctx, a: self.tool_music_play(ctx, str(a.get("query") or ""))),
            ("music_skip", "Skip the current song, optionally multiple tracks.", {"type":"object","properties":{"count":{"type":"integer","minimum":1,"maximum":20}}}, lambda ctx, a: self.tool_music_skip(ctx, int(a.get("count") or 1))),
            ("music_pause", "Pause voice music playback.", {"type":"object","properties":{}}, lambda ctx, a: self.tool_music_pause(ctx)),
            ("music_resume", "Resume paused voice music playback.", {"type":"object","properties":{}}, lambda ctx, a: self.tool_music_resume(ctx)),
            ("music_volume", "Set Discord music volume from 0 to 200 percent.", {"type":"object","properties":{"volume":{"type":"integer","minimum":0,"maximum":200}},"required":["volume"]}, lambda ctx, a: self.tool_music_volume(ctx, int(a.get("volume") or 100))),
            ("music_stop", "Stop all music, clear the queue, disable DJ mode, and disconnect.", {"type":"object","properties":{}}, lambda ctx, a: self.tool_music_stop(ctx)),
        ]
        for name, description, schema, handler in definitions:
            registry.register(
                name=name,
                description=description,
                parameters=schema,
                handler=handler,
                category="music",
                source=source,
                guild_only=True,
            )

    async def cog_load(self):
        self._register_capabilities()
        import config
        self.refresh_controllers.start()
        for attempt in range(1, LAVALINK_CONNECT_RETRIES + 1):
            try:
                node = wavelink.Node(
                    uri=f"{'https' if config.LAVALINK_SECURE else 'http'}://{config.LAVALINK_HOST}:{config.LAVALINK_PORT}",
                    password=config.LAVALINK_PASSWORD,
                )
                await wavelink.Pool.connect(nodes=[node], client=self.bot, cache_capacity=100)
                self._connected = True
                log.info("Connected to Lavalink at %s:%s", config.LAVALINK_HOST, config.LAVALINK_PORT)
                return
            except Exception as exc:
                log.warning("Lavalink connect attempt %d failed: %s", attempt, exc)
                if attempt < LAVALINK_CONNECT_RETRIES:
                    await asyncio.sleep(3)
        log.warning("Lavalink unavailable — music commands will be disabled.")

    async def cog_unload(self):
        self.bot.capabilities.unregister_source("music")
        self.refresh_controllers.cancel()

    def _check_lavalink(self, ctx: commands.Context) -> bool:
        return self._connected

    async def _get_player(self, ctx: commands.Context) -> MusicPlayer | None:
        if not self._check_lavalink(ctx):
            await ctx.send(embed=error_embed("Lavalink is not connected. Music is unavailable."))
            return None
        player: MusicPlayer = ctx.voice_client
        if not player:
            if not ctx.author.voice:
                await ctx.send(embed=error_embed("Join a voice channel first."))
                return None
            player = await ctx.author.voice.channel.connect(cls=MusicPlayer, self_deaf=True)
            try:
                player.inactive_timeout = IDLE_DISCONNECT_SECONDS
            except Exception:
                pass
        player.home = ctx.channel
        return player

    # ── The display ────────────────────────────────────────────────────────────

    def build_now_playing(self, player: MusicPlayer) -> discord.Embed:
        """The live player embed."""
        track = player.current
        if not track:
            return discord.Embed(
                title="\u23f9\ufe0f Nothing Playing",
                description="Queue something with `play`.",
                color=COLOR_IDLE,
            )

        pos, dur = player.position, track.length
        bar = _progress_bar(pos, dur)
        state = "\u23f8\ufe0f Paused" if player.paused else "\u25b6\ufe0f Now Playing"

        e = discord.Embed(
            title=f"{state}",
            description=(
                f"### [{_trim(track.title, 60)}]({track.uri})\n"
                f"**{track.author or 'Unknown'}**\n\n"
                f"{bar}\n"
                f"`{_fmt_duration(pos)}` {' ' * 12} `{_fmt_duration(dur)}`"
            ),
            color=COLOR_PAUSED if player.paused else COLOR_PLAYING,
        )

        if track.artwork:
            e.set_thumbnail(url=track.artwork)

        e.add_field(name="Volume", value=f"{_volume_icon(player.volume)} {player.volume}%")
        e.add_field(name="Loop", value=_loop_status(player))
        e.add_field(name="Queue", value=f"{player.queue.count} track(s)")

        if player.dj_enabled:
            style = player.dj_style or "open format"
            e.add_field(
                name="AI DJ",
                value=f"🎛️ {style} · energy {player.dj_energy}/10",
                inline=False,
            )
        elif player.autoplay == wavelink.AutoPlayMode.enabled:
            e.add_field(name="Autoplay", value="\u2728 On", inline=False)

        nxt = player.queue[0] if player.queue.count else None
        if nxt:
            e.add_field(name="Up Next", value=f"[{_trim(nxt.title, 50)}]({nxt.uri})", inline=False)

        requester = _requester(player.guild, track)
        if requester:
            e.set_footer(text=f"Requested by {requester}", icon_url=requester.display_avatar.url)
        elif track.recommended:
            e.set_footer(text="Autoplay recommendation")

        return e

    async def send_controller(self, player: MusicPlayer):
        """Replace the old controller with a fresh one in the home channel."""
        if not player.home:
            return
        await player.teardown_controller()
        view = PlayerController(self, player)
        try:
            msg = await player.home.send(embed=self.build_now_playing(player), view=view)
        except discord.HTTPException as exc:
            log.warning("Could not send controller: %s", exc)
            return
        player.controller = msg
        player.controller_view = view

    @tasks.loop(seconds=CONTROLLER_REFRESH_SECONDS)
    async def refresh_controllers(self):
        """Tick the progress bar on every live controller."""
        for player in list(wavelink.Pool.get_node().players.values()) if wavelink.Pool.nodes else []:
            if not isinstance(player, MusicPlayer):
                continue
            if not player.controller or not player.current or player.paused:
                continue
            try:
                player.controller_view._sync_buttons()
                await player.controller.edit(
                    embed=self.build_now_playing(player), view=player.controller_view
                )
            except discord.NotFound:
                player.controller = None
            except discord.HTTPException as exc:
                log.debug("Controller refresh failed: %s", exc)

    @refresh_controllers.before_loop
    async def _before_refresh(self):
        await self.bot.wait_until_ready()

    # ── AI DJ engine ───────────────────────────────────────────────────────────

    @staticmethod
    def _dj_track_key(track: wavelink.Playable | None) -> str:
        if track is None:
            return ""
        identifier = str(getattr(track, "identifier", "") or "").strip()
        if identifier:
            return identifier
        uri = str(getattr(track, "uri", "") or "").strip()
        if uri:
            return uri
        return f"{getattr(track, 'author', '')}|{getattr(track, 'title', '')}".casefold()

    @staticmethod
    def _dj_generated(track: wavelink.Playable) -> bool:
        extras = getattr(track, "extras", None)
        return bool(getattr(extras, "dj_auto", False))

    @staticmethod
    def _dj_energy_words(energy: int) -> str:
        if energy <= 2:
            return "very chill mellow laid back"
        if energy <= 4:
            return "chill smooth relaxed"
        if energy <= 6:
            return "groovy mid energy"
        if energy <= 8:
            return "high energy upbeat"
        return "peak energy hard aggressive"

    def _drop_dj_generated_queue(self, player: MusicPlayer) -> int:
        """Remove only tracks TweakBot selected automatically; keep human requests."""
        removed = 0
        for track in list(player.queue):
            if not self._dj_generated(track):
                continue
            try:
                player.queue.remove(track)
                removed += 1
            except ValueError:
                pass
        return removed

    def _dj_candidate_ok(self, player: MusicPlayer, track: wavelink.Playable) -> bool:
        # Avoid search results that are probably full-length mixes/streams. A DJ
        # request can still explicitly queue those with dj_request/music_play.
        length = int(getattr(track, "length", 0) or 0)
        if length and (length < 45_000 or length > 12 * 60_000):
            return False
        if bool(getattr(track, "is_stream", False)):
            return False

        key = self._dj_track_key(track)
        if not key or key in player.dj_recent:
            return False
        if self._dj_track_key(player.current) == key:
            return False
        if any(self._dj_track_key(queued) == key for queued in player.queue):
            return False
        return True

    async def _dj_search(self, player: MusicPlayer, *, requester_id: int = 0) -> list[wavelink.Playable]:
        style = (player.dj_style or "popular music").strip()
        energy = self._dj_energy_words(player.dj_energy)
        suffix = random.choice(("radio", "hits", "songs", "playlist", "mix"))
        query = f"{style} {energy} {suffix}".strip()
        try:
            results = await wavelink.Playable.search(query)
        except Exception:
            log.exception("AI DJ search failed for %r", query)
            return []

        if not results:
            return []
        candidates = list(results.tracks if isinstance(results, wavelink.Playlist) else results)
        # Searches are ranked, so keep the useful head of the result set but do
        # not deterministically pick #1 every time.
        candidates = candidates[:15]
        random.shuffle(candidates)
        accepted: list[wavelink.Playable] = []
        for track in candidates:
            if not self._dj_candidate_ok(player, track):
                continue
            track.extras = {
                "requester": requester_id,
                "dj_auto": True,
                "dj_style": player.dj_style,
                "dj_energy": player.dj_energy,
            }
            accepted.append(track)
        return accepted

    async def _dj_refill(self, player: MusicPlayer, *, requester_id: int = 0) -> int:
        """Keep a small style-aware priority queue ahead of Wavelink recommendations."""
        if not player.dj_enabled or not player.connected:
            return 0

        async with player.dj_lock:
            needed = max(0, player.dj_target_queue - player.queue.count)
            if needed <= 0:
                return 0

            added = 0
            # Two varied searches are enough to refill without hammering Lavalink.
            for _ in range(2):
                if added >= needed:
                    break
                candidates = await self._dj_search(player, requester_id=requester_id)
                for track in candidates:
                    if added >= needed:
                        break
                    # Recheck after earlier candidates were inserted.
                    if not self._dj_candidate_ok(player, track):
                        continue
                    await player.queue.put_wait(track)
                    added += 1

            return added

    async def _dj_seed_next(self, player: MusicPlayer, *, requester_id: int = 0, count: int = 2) -> int:
        """Put fresh style-aware DJ tracks at the front without deleting human requests."""
        candidates = await self._dj_search(player, requester_id=requester_id)
        chosen = candidates[:max(0, count)]
        # put_at(0) reverses order, so insert in reverse to preserve chosen order.
        for track in reversed(chosen):
            player.queue.put_at(0, track)
        return len(chosen)

    async def tool_dj_start(self, ctx: commands.Context, style: str = "", energy: int = 7) -> str:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return "DJ mode only works inside a server."
        if not ctx.author.voice:
            return "Join a voice channel first."

        player = await self._get_player(ctx)
        if not player:
            return "Lavalink is unavailable or I could not join voice."

        player.dj_enabled = True
        player.dj_style = str(style or player.dj_style or "open format").strip()[:180]
        player.dj_energy = max(1, min(10, int(energy or 7)))
        player.queue.mode = wavelink.QueueMode.normal
        player.autoplay = wavelink.AutoPlayMode.enabled

        # Throw away stale recommendation state when beginning a new DJ brief.
        try:
            player.auto_queue.clear()
        except Exception:
            pass

        if not player.playing and not player.queue.count:
            await self._dj_refill(player, requester_id=ctx.author.id)
        elif player.queue.count < player.dj_target_queue:
            await self._dj_refill(player, requester_id=ctx.author.id)

        if not player.playing and player.queue.count:
            await player.play(player.queue.get(), populate=True, max_populate=5)

        await self._touch_controller(player)
        return (
            f"DJ mode started in {player.channel.name}. Style: {player.dj_style}; "
            f"energy: {player.dj_energy}/10; queued: {player.queue.count}."
        )

    async def tool_dj_stop(self, ctx: commands.Context) -> str:
        player = ctx.voice_client
        if not isinstance(player, MusicPlayer):
            return "I am not connected to voice."
        player.dj_enabled = False
        player.autoplay = wavelink.AutoPlayMode.partial
        removed = self._drop_dj_generated_queue(player)
        try:
            player.auto_queue.clear()
        except Exception:
            pass
        await self._touch_controller(player)
        return f"DJ mode stopped. Removed {removed} automatically selected queued track(s); human requests were kept."

    async def tool_dj_style(self, ctx: commands.Context, style: str) -> str:
        player = ctx.voice_client
        if not isinstance(player, MusicPlayer) or not player.dj_enabled:
            return "DJ mode is not running."
        style = str(style or "").strip()
        if not style:
            return "Give me a style, genre, era, artist direction, or mood."
        player.dj_style = style[:180]
        removed = self._drop_dj_generated_queue(player)
        try:
            player.auto_queue.clear()
        except Exception:
            pass
        added = await self._dj_seed_next(player, requester_id=ctx.author.id, count=2)
        await self._dj_refill(player, requester_id=ctx.author.id)
        await self._touch_controller(player)
        return f"DJ style changed to {player.dj_style}. Replaced {removed} old DJ pick(s) and seeded {added} new one(s)."

    async def tool_dj_energy(self, ctx: commands.Context, energy: int) -> str:
        player = ctx.voice_client
        if not isinstance(player, MusicPlayer) or not player.dj_enabled:
            return "DJ mode is not running."
        player.dj_energy = max(1, min(10, int(energy)))
        removed = self._drop_dj_generated_queue(player)
        try:
            player.auto_queue.clear()
        except Exception:
            pass
        added = await self._dj_seed_next(player, requester_id=ctx.author.id, count=2)
        await self._dj_refill(player, requester_id=ctx.author.id)
        await self._touch_controller(player)
        return f"DJ energy set to {player.dj_energy}/10. Refreshed {removed} old pick(s) with {added} new seed(s)."

    async def tool_dj_request(self, ctx: commands.Context, query: str) -> str:
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return "Music requests only work inside a server."
        if not ctx.author.voice:
            return "Join a voice channel first."
        player = await self._get_player(ctx)
        if not player:
            return "Lavalink is unavailable or I could not join voice."

        query = str(query or "").strip()
        if not query:
            return "No song request was provided."
        results = await wavelink.Playable.search(query)
        if not results:
            return f"No results for {query}."
        track = results.tracks[0] if isinstance(results, wavelink.Playlist) else results[0]
        track.extras = {"requester": ctx.author.id, "dj_request": True}
        player.queue.put_at(0, track)
        if not player.playing:
            await player.play(player.queue.get(), populate=player.dj_enabled, max_populate=5)
        return f"Requested track queued next: {track.title} by {track.author or 'Unknown'}."

    async def tool_dj_status(self, ctx: commands.Context) -> str:
        player = ctx.voice_client
        if not isinstance(player, MusicPlayer):
            return "I am not connected to voice."
        if not player.dj_enabled:
            return f"DJ mode is off. {player.queue.count} track(s) are queued."
        current = getattr(player.current, "title", None) or "nothing"
        return (
            f"DJ mode is on. Style: {player.dj_style or 'open format'}; "
            f"energy: {player.dj_energy}/10; now playing: {current}; "
            f"standard queue: {player.queue.count}; recommendation queue: {player.auto_queue.count}."
        )

    async def tool_music_play(self, ctx: commands.Context, query: str) -> str:
        # Normal conversational play request; unlike a DJ auto-pick it is kept
        # when DJ mode is stopped or restyled.
        if not ctx.guild or not isinstance(ctx.author, discord.Member):
            return "Music only works inside a server."
        if not ctx.author.voice:
            return "Join a voice channel first."
        player = await self._get_player(ctx)
        if not player:
            return "Lavalink is unavailable or I could not join voice."
        query = str(query or "").strip()
        if not query:
            return "No song was provided."
        results = await wavelink.Playable.search(query)
        if not results:
            return f"No results for {query}."
        track = results.tracks[0] if isinstance(results, wavelink.Playlist) else results[0]
        track.extras = {"requester": ctx.author.id}
        await player.queue.put_wait(track)
        if not player.playing:
            await player.play(player.queue.get(), populate=player.dj_enabled, max_populate=5)
        return f"Queued {track.title} by {track.author or 'Unknown'}."

    async def tool_music_skip(self, ctx: commands.Context, count: int = 1) -> str:
        player = ctx.voice_client
        if not isinstance(player, MusicPlayer) or not player.current:
            return "Nothing is playing."
        count = max(1, min(20, int(count or 1)))
        removed = 0
        for _ in range(count - 1):
            if not player.queue.count:
                break
            player.queue.get()
            removed += 1
        await player.skip(force=True)
        return f"Skipped {removed + 1} track(s)."

    async def tool_music_pause(self, ctx: commands.Context) -> str:
        player = ctx.voice_client
        if not isinstance(player, MusicPlayer) or not player.current:
            return "Nothing is playing."
        await player.pause(True)
        await self._touch_controller(player)
        return "Playback paused."

    async def tool_music_resume(self, ctx: commands.Context) -> str:
        player = ctx.voice_client
        if not isinstance(player, MusicPlayer) or not player.current:
            return "Nothing is loaded."
        await player.pause(False)
        await self._touch_controller(player)
        return "Playback resumed."

    async def tool_music_volume(self, ctx: commands.Context, volume: int) -> str:
        player = ctx.voice_client
        if not isinstance(player, MusicPlayer):
            return "I am not connected to voice."
        volume = max(0, min(200, int(volume)))
        await player.set_volume(volume)
        await self._touch_controller(player)
        return f"Volume set to {volume}%."

    async def tool_music_stop(self, ctx: commands.Context) -> str:
        player = ctx.voice_client
        if not isinstance(player, MusicPlayer):
            return "I am not connected to voice."
        player.dj_enabled = False
        player.queue.clear()
        player.queue.reset()
        try:
            player.auto_queue.clear()
        except Exception:
            pass
        await player.teardown_controller()
        await player.disconnect()
        return "Playback stopped and I disconnected from voice."

    # Command fallbacks. Natural-language requests use the tool_* methods above.

    @commands.hybrid_command(name="dj", usage="dj [style]")
    @commands.guild_only()
    @is_in_vc()
    async def dj(self, ctx: commands.Context, *, style: str = "open format"):
        """Start autonomous AI DJ mode. Use djenergy to change energy."""
        result = await self.tool_dj_start(ctx, style=style, energy=7)
        await ctx.send(embed=success_embed(f"🎛️ {result}"))

    @commands.hybrid_command(name="djoff", aliases=["stopdj"], usage="djoff")
    @commands.guild_only()
    async def djoff(self, ctx: commands.Context):
        """Stop AI DJ selection but keep human requests playing."""
        result = await self.tool_dj_stop(ctx)
        await ctx.send(embed=success_embed(result))

    @commands.hybrid_command(name="djstyle", usage="djstyle <style>")
    @commands.guild_only()
    async def djstyle(self, ctx: commands.Context, *, style: str):
        """Change the active DJ direction without stopping the current song."""
        result = await self.tool_dj_style(ctx, style)
        await ctx.send(embed=success_embed(result))

    @commands.hybrid_command(name="djenergy", usage="djenergy <1-10>")
    @commands.guild_only()
    async def djenergy(self, ctx: commands.Context, energy: int):
        """Set DJ energy from 1 (mellow) to 10 (peak)."""
        result = await self.tool_dj_energy(ctx, energy)
        await ctx.send(embed=success_embed(result))

    @commands.hybrid_command(name="djrequest", aliases=["request"], usage="djrequest <song>")
    @commands.guild_only()
    @is_in_vc()
    async def djrequest(self, ctx: commands.Context, *, query: str):
        """Put a human request at the front of the DJ queue."""
        result = await self.tool_dj_request(ctx, query)
        await ctx.send(embed=success_embed(result))

    @commands.hybrid_command(name="djstatus", usage="djstatus")
    @commands.guild_only()
    async def djstatus(self, ctx: commands.Context):
        """Show the current AI DJ brief and queue state."""
        result = await self.tool_dj_status(ctx)
        await ctx.send(embed=info_embed(result))

    # ── Playback ───────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="play", aliases=["p"], usage="play <query or URL>")
    @commands.guild_only()
    @is_in_vc()
    async def play(self, ctx: commands.Context, *, query: str):
        """Play a track or add it to the queue."""
        player = await self._get_player(ctx)
        if not player:
            return

        async with ctx.typing():
            tracks = await wavelink.Playable.search(query)
            if not tracks:
                return await ctx.send(embed=error_embed(f"No results for `{query}`."))

        if isinstance(tracks, wavelink.Playlist):
            for t in tracks.tracks:
                t.extras = {"requester": ctx.author.id}
                await player.queue.put_wait(t)
            await ctx.send(embed=success_embed(
                f"\U0001f4cb Added **{tracks.name}** \u2014 `{len(tracks.tracks)}` tracks."
            ))
        else:
            track = tracks[0]
            track.extras = {"requester": ctx.author.id}
            await player.queue.put_wait(track)
            if player.playing:
                await ctx.send(embed=success_embed(
                    f"\U0001f3b5 Queued **{_trim(track.title)}** \u2014 position `{player.queue.count}`."
                ))

        if not player.playing:
            await player.play(player.queue.get())

    @commands.hybrid_command(name="playnext", aliases=["pn"], usage="playnext <query>")
    @commands.guild_only()
    @is_in_vc()
    async def playnext(self, ctx: commands.Context, *, query: str):
        """Play next — insert a track at the front of the queue."""
        player = await self._get_player(ctx)
        if not player:
            return

        tracks = await wavelink.Playable.search(query)
        if not tracks:
            return await ctx.send(embed=error_embed(f"No results for `{query}`."))

        track = tracks[0] if not isinstance(tracks, wavelink.Playlist) else tracks.tracks[0]
        track.extras = {"requester": ctx.author.id}
        player.queue.put_at(0, track)
        await ctx.send(embed=success_embed(f"**{_trim(track.title)}** queued to play next."))
        if not player.playing:
            await player.play(player.queue.get())

    @commands.hybrid_command(name="pause", usage="pause")
    @commands.guild_only()
    @bot_in_vc()
    async def pause(self, ctx: commands.Context):
        """Pause playback."""
        player: MusicPlayer = ctx.voice_client
        if player.paused:
            return await ctx.send(embed=error_embed("Already paused."))
        await player.pause(True)
        await self._touch_controller(player)
        await ctx.send(embed=success_embed("\u23f8\ufe0f Paused."))

    @commands.hybrid_command(name="skip", aliases=["s", "next"], usage="skip [count]")
    @commands.guild_only()
    @bot_in_vc()
    async def skip(self, ctx: commands.Context, count: int = 1):
        """Skip one or more tracks."""
        player: MusicPlayer = ctx.voice_client
        if not player.current:
            return await ctx.send(embed=error_embed("Nothing is playing."))

        for _ in range(max(0, count - 1)):
            if player.queue.count:
                player.queue.get()

        await player.skip(force=True)
        await ctx.send(embed=success_embed(f"\u23ed\ufe0f Skipped {count} track(s)."))

    @commands.hybrid_command(name="stop", usage="stop")
    @commands.guild_only()
    @bot_in_vc()
    async def stop(self, ctx: commands.Context):
        """Stop playback and clear the queue."""
        player: MusicPlayer = ctx.voice_client
        player.queue.clear()
        player.queue.reset()
        await player.teardown_controller()
        await player.disconnect()
        await ctx.send(embed=success_embed("\u23f9\ufe0f Stopped and disconnected."))

    @commands.hybrid_command(name="disconnect", aliases=["dc", "leave"], usage="disconnect")
    @commands.guild_only()
    @bot_in_vc()
    async def disconnect(self, ctx: commands.Context):
        """Disconnect from voice."""
        player: MusicPlayer = ctx.voice_client
        player.queue.clear()
        await player.teardown_controller()
        await player.disconnect()
        await ctx.send(embed=success_embed("\U0001f44b Disconnected."))

    # ── Queue ──────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="queue", aliases=["q"], usage="queue [page]")
    @commands.guild_only()
    @bot_in_vc()
    async def queue_cmd(self, ctx: commands.Context, page: int = 1):
        """View the current queue."""
        player: MusicPlayer = ctx.voice_client

        tracks = list(player.queue)
        if not tracks and not player.current:
            return await ctx.send(embed=info_embed("The queue is empty."))

        per_page = 10
        pages = max(1, (len(tracks) + per_page - 1) // per_page)
        page = max(1, min(page, pages))
        start = (page - 1) * per_page
        slice_ = tracks[start:start + per_page]

        e = discord.Embed(title="\U0001f3b6 Queue", color=COLOR_PLAYING, timestamp=discord.utils.utcnow())

        if player.current:
            e.add_field(
                name="\u25b6\ufe0f Now Playing",
                value=(
                    f"[{_trim(player.current.title)}]({player.current.uri})\n"
                    f"{_progress_bar(player.position, player.current.length, 14)} "
                    f"`{_fmt_duration(player.position)}/{_fmt_duration(player.current.length)}`"
                ),
                inline=False,
            )

        if slice_:
            lines = []
            for i, t in enumerate(slice_, start=start + 1):
                lines.append(f"`{i}.` [{_trim(t.title)}]({t.uri}) \u2014 `{_fmt_duration(t.length)}`")
            e.add_field(name=f"Up Next (page {page}/{pages})", value="\n".join(lines), inline=False)

        total_dur = sum(t.length for t in tracks)
        e.set_footer(text=f"{len(tracks)} tracks | {_fmt_duration(total_dur)} total")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="clearqueue", aliases=["cq"], usage="clearqueue")
    @commands.guild_only()
    @bot_in_vc()
    async def clearqueue(self, ctx: commands.Context):
        """Clear the queue without stopping playback."""
        player: MusicPlayer = ctx.voice_client
        count = player.queue.count
        player.queue.clear()
        await ctx.send(embed=success_embed(f"Cleared `{count}` track(s) from the queue."))

    @commands.hybrid_command(name="shuffle", usage="shuffle")
    @commands.guild_only()
    @bot_in_vc()
    async def shuffle(self, ctx: commands.Context):
        """Shuffle the queue."""
        player: MusicPlayer = ctx.voice_client
        if player.queue.count < 2:
            return await ctx.send(embed=error_embed("Need at least 2 tracks to shuffle."))
        player.queue.shuffle()
        await ctx.send(embed=success_embed("\U0001f500 Queue shuffled."))

    # ── Seek & Volume ──────────────────────────────────────────────────────────

    @commands.hybrid_command(name="volume", aliases=["vol"], usage="volume <0-200>")
    @commands.guild_only()
    @bot_in_vc()
    async def volume(self, ctx: commands.Context, vol: int):
        """Set playback volume (0–200%)."""
        if not 0 <= vol <= 200:
            return await ctx.send(embed=error_embed("Volume must be between 0 and 200."))
        player: MusicPlayer = ctx.voice_client
        await player.set_volume(vol)
        await self._touch_controller(player)
        await ctx.send(embed=success_embed(f"\U0001f50a Volume set to `{vol}%`."))

    # ── Loop ───────────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="loop", usage="loop [track|queue|off]")
    @commands.guild_only()
    @bot_in_vc()
    async def loop(self, ctx: commands.Context, mode: str = None):
        """Toggle loop mode: track, queue, or off."""
        player: MusicPlayer = ctx.voice_client

        if mode is None or mode.lower() == "off":
            player.queue.mode = wavelink.QueueMode.normal
            msg = "\U0001f501 Loop disabled."
        elif mode.lower() in ("track", "one"):
            player.queue.mode = wavelink.QueueMode.loop
            msg = "\U0001f502 Looping current track."
        elif mode.lower() in ("queue", "all"):
            player.queue.mode = wavelink.QueueMode.loop_all
            msg = "\U0001f501 Looping entire queue."
        else:
            return await ctx.send(embed=error_embed("Valid modes: `track`, `queue`, `off`."))

        await self._touch_controller(player)
        await ctx.send(embed=success_embed(msg))

    # ── Filters ────────────────────────────────────────────────────────────────

    @commands.command(name="bassboost", usage="bassboost [level 0-5]")
    @commands.guild_only()
    @bot_in_vc()
    async def bassboost(self, ctx: commands.Context, level: int = 3):
        """Apply a bass boost filter."""
        player: MusicPlayer = ctx.voice_client
        level = max(0, min(5, level))
        gain = level * 0.08
        filters: wavelink.Filters = player.filters
        filters.equalizer.set(bands=[
            {"band": 0, "gain": gain},
            {"band": 1, "gain": gain * 0.85},
            {"band": 2, "gain": gain * 0.5},
        ])
        await player.set_filters(filters)
        if level == 0:
            await ctx.send(embed=success_embed("Bass boost removed."))
        else:
            await ctx.send(embed=success_embed(f"\U0001f3b8 Bass boost level `{level}`."))

    # ── Auto-play ──────────────────────────────────────────────────────────────

    # ── Helpers ────────────────────────────────────────────────────────────────

    async def _touch_controller(self, player: MusicPlayer):
        """Edit the existing controller in place after a state change."""
        if not player.controller or not player.controller_view:
            return
        try:
            player.controller_view._sync_buttons()
            await player.controller.edit(
                embed=self.build_now_playing(player), view=player.controller_view
            )
        except discord.HTTPException:
            pass

    # ── Lavalink events ────────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload):
        log.info("Wavelink node ready: %s", payload.node.identifier)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload):
        player = payload.player
        if not isinstance(player, MusicPlayer):
            return

        if player.dj_enabled and payload.track is not None:
            key = self._dj_track_key(payload.track)
            if key:
                player.dj_recent.append(key)
            # Wavelink handles actual track advancement. We only keep its priority
            # queue stocked so there is never a competing manual play() here.
            asyncio.create_task(self._dj_refill(player))

        await self.send_controller(player)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload):
        # AutoPlay (partial/enabled) advances the queue itself — do NOT play here
        # or every track gets skipped. Only clean up when the player goes idle.
        player = payload.player
        if not isinstance(player, MusicPlayer):
            return
        await asyncio.sleep(1)
        if not player.playing and not player.queue.count:
            await player.teardown_controller()

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload):
        log.warning("Track exception: %s", payload.exception)
        player = payload.player
        if isinstance(player, MusicPlayer) and player.home:
            try:
                await player.home.send(embed=error_embed("Track failed to play, skipping."))
            except discord.HTTPException:
                pass

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload):
        log.warning("Track stuck, skipping.")
        player = payload.player
        if player:
            await player.skip(force=True)

    @commands.Cog.listener()
    async def on_wavelink_inactive_player(self, player: wavelink.Player):
        if isinstance(player, MusicPlayer):
            player.dj_enabled = False
            if player.home:
                try:
                    await player.home.send(embed=info_embed("Left the channel after 5 minutes of silence."))
                except discord.HTTPException:
                    pass
            await player.teardown_controller()
        await player.disconnect()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """Leave when the channel empties out."""
        player = member.guild.voice_client
        if not isinstance(player, MusicPlayer) or not player.channel:
            return
        humans = [m for m in player.channel.members if not m.bot]
        if not humans:
            player.dj_enabled = False
            await player.teardown_controller()
            await player.disconnect()


# ── Utilities ─────────────────────────────────────────────────────────────────

def _trim(text: str, limit: int = 45) -> str:
    text = (text or "Unknown").replace("[", "(").replace("]", ")")
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def _fmt_duration(ms: int) -> str:
    if not ms:
        return "0:00"
    s = ms // 1000
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _progress_bar(position: int, duration: int, length: int = BAR_LENGTH) -> str:
    if not duration:
        return BAR_EMPTY * length
    ratio = min(1.0, max(0.0, position / duration))
    filled = int(ratio * (length - 1))
    return BAR_FILL * filled + BAR_KNOB + BAR_EMPTY * (length - filled - 1)


def _volume_icon(vol: int) -> str:
    if vol == 0:
        return "\U0001f507"
    if vol < 50:
        return "\U0001f509"
    return "\U0001f50a"


def _requester(guild: discord.Guild | None, track: wavelink.Playable):
    if not guild:
        return None
    extras = getattr(track, "extras", None)
    rid = getattr(extras, "requester", None)
    return guild.get_member(rid) if rid else None


def _parse_time(s: str) -> int | None:
    """Parse time string like 1:30, 90, 1m30s into milliseconds."""
    try:
        if ":" in s:
            parts = s.split(":")
            if len(parts) == 2:
                return (int(parts[0]) * 60 + int(parts[1])) * 1000
            elif len(parts) == 3:
                return (int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])) * 1000
        elif s.endswith("s") or s.endswith("m"):
            from utils.helpers import parse_duration
            td = parse_duration(s)
            return int(td.total_seconds() * 1000) if td else None
        else:
            return int(s) * 1000
    except Exception:
        return None


def _loop_status(player: wavelink.Player) -> str:
    mode = player.queue.mode
    if mode == wavelink.QueueMode.loop:
        return "\U0001f502 Track"
    if mode == wavelink.QueueMode.loop_all:
        return "\U0001f501 Queue"
    return "Off"


async def setup(bot):
    await bot.add_cog(Music(bot))
