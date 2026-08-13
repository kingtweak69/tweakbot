"""
Imaging cog — image editing, AI image generation, photo animation, video extension.

Every PIL operation runs in a worker thread. Doing them inline blocks the
event loop, which stalls the heartbeat and gets the bot disconnected mid-blur.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import uuid

import aiohttp
import discord
from discord.ext import commands, tasks
from PIL import Image, ImageEnhance, ImageFilter

import config
from utils import video as vid
from utils.helpers import error_embed, info_embed, success_embed

log = logging.getLogger("cogs.imaging")

# A crafted 20KB PNG can claim to be 60000x60000 and OOM the container.
Image.MAX_IMAGE_PIXELS = 50_000_000
MAX_SOURCE_DIMENSION = 8000
MAX_IMAGE_BYTES = 20 * 1024 * 1024

POLL_INTERVAL_SECONDS = 20
JOB_TIMEOUT_MINUTES = 20


def _incident() -> str:
    return uuid.uuid4().hex[:8]


class Imaging(commands.Cog):
    """🎨 Image editing, generation, and animation."""

    def __init__(self, bot):
        self.bot = bot
        self.session: aiohttp.ClientSession | None = None
        self._ai_client = None
        self._provider: vid.VideoProvider | None = None

        if getattr(config, "OPENAI_API_KEY", None):
            try:
                from openai import AsyncOpenAI
                self._ai_client = AsyncOpenAI(api_key=config.OPENAI_API_KEY)
            except Exception as exc:
                log.warning("OpenAI client unavailable: %s", exc)

    async def cog_load(self):
        self.session = aiohttp.ClientSession()
        self._provider = vid.build_provider(
            getattr(config, "VIDEO_PROVIDER", "kling"),
            getattr(config, "VIDEO_API_KEY", "") or "",
            self.session,
        )
        if self._provider:
            self.poll_jobs.start()
        else:
            log.info("No video provider configured — animate/extend disabled.")
        if not vid.ffmpeg_available():
            log.warning("ffmpeg not found — extend will be unavailable.")

    async def cog_unload(self):
        if self.poll_jobs.is_running():
            self.poll_jobs.cancel()
        if self.session and not self.session.closed:
            await self.session.close()

    # ── Shared helpers ────────────────────────────────────────────────────────

    async def _fail(self, ctx: commands.Context, message: str, exc: Exception | None = None):
        if exc is None:
            return await ctx.send(embed=error_embed(message))
        ref = _incident()
        log.error("%s [%s]: %s", message, ref, exc, exc_info=True)
        await ctx.send(embed=error_embed(f"{message} Reference: `{ref}`"))

    async def _attachment(self, ctx: commands.Context, kind: str = "image") -> discord.Attachment | None:
        """Find an attachment on the message or the one it replies to."""
        attachments = list(ctx.message.attachments)
        if not attachments and ctx.message.reference and ctx.message.reference.message_id:
            try:
                ref = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                attachments = list(ref.attachments)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

        if not attachments:
            await ctx.send(embed=error_embed(f"Attach {'an' if kind == 'image' else 'a'} {kind}, or reply to a message with one."))
            return None

        att = attachments[0]
        if not att.content_type or not att.content_type.startswith(f"{kind}/"):
            await ctx.send(embed=error_embed(f"That attachment isn't {'an' if kind == 'image' else 'a'} {kind}."))
            return None
        return att

    async def _load_image(self, ctx: commands.Context) -> Image.Image | None:
        att = await self._attachment(ctx, "image")
        if not att:
            return None
        if att.size > MAX_IMAGE_BYTES:
            await ctx.send(embed=error_embed("That image is too large (20MB max)."))
            return None
        try:
            data = await att.read()
        except discord.HTTPException as exc:
            await self._fail(ctx, "Could not download that image.", exc)
            return None

        def _open() -> Image.Image:
            img = Image.open(io.BytesIO(data))
            if img.width > MAX_SOURCE_DIMENSION or img.height > MAX_SOURCE_DIMENSION:
                raise ValueError("dimensions too large")
            img.load()
            return img.convert("RGBA")

        try:
            return await asyncio.to_thread(_open)
        except ValueError:
            await ctx.send(embed=error_embed(f"That image is too big — max {MAX_SOURCE_DIMENSION}px on a side."))
        except Image.DecompressionBombError:
            await ctx.send(embed=error_embed("That image is too large to process safely."))
        except Exception as exc:
            await self._fail(ctx, "Could not read that image.", exc)
        return None

    def _limit(self, ctx: commands.Context) -> int:
        """Actual upload ceiling for this guild — never hardcode this."""
        if ctx.guild:
            return ctx.guild.filesize_limit
        return 10 * 1024 * 1024

    async def _send_image(self, ctx: commands.Context, img: Image.Image, filename: str):
        def _encode() -> bytes:
            buf = io.BytesIO()
            img.save(buf, format="PNG", optimize=True)
            return buf.getvalue()

        try:
            data = await asyncio.to_thread(_encode)
        except Exception as exc:
            return await self._fail(ctx, "Could not encode the result.", exc)

        if len(data) > self._limit(ctx):
            def _jpeg() -> bytes:
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=85, optimize=True)
                return buf.getvalue()
            data = await asyncio.to_thread(_jpeg)
            filename = filename.rsplit(".", 1)[0] + ".jpg"

        if len(data) > self._limit(ctx):
            return await ctx.send(embed=error_embed("The result is too large to upload here."))

        await ctx.send(file=discord.File(io.BytesIO(data), filename=filename))

    async def _edit(self, ctx: commands.Context, fn, filename: str):
        """Load, transform in a thread, send. Every edit command routes through this."""
        img = await self._load_image(ctx)
        if img is None:
            return
        async with ctx.typing():
            try:
                result = await asyncio.to_thread(fn, img)
            except Exception as exc:
                return await self._fail(ctx, "That edit failed.", exc)
            await self._send_image(ctx, result, filename)

    # ── Image editing ─────────────────────────────────────────────────────────

    @commands.command(name="resize", usage="resize <width> <height>")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def resize(self, ctx: commands.Context, width: int, height: int):
        """Resize an attached image."""
        if not (1 <= width <= 4096 and 1 <= height <= 4096):
            return await ctx.send(embed=error_embed("Dimensions must be between 1 and 4096."))
        await self._edit(ctx, lambda i: i.resize((width, height), Image.LANCZOS), "resized.png")

    @commands.command(name="crop", usage="crop <x> <y> <width> <height>")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def crop(self, ctx: commands.Context, x: int, y: int, width: int, height: int):
        """Crop an image."""
        if width < 1 or height < 1:
            return await ctx.send(embed=error_embed("Width and height must be positive."))

        def _crop(img: Image.Image) -> Image.Image:
            left = max(0, min(x, img.width - 1))
            top = max(0, min(y, img.height - 1))
            right = min(img.width, left + width)
            bottom = min(img.height, top + height)
            return img.crop((left, top, right, bottom))

        await self._edit(ctx, _crop, "cropped.png")

    @commands.command(name="rotate", usage="rotate <degrees>")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def rotate(self, ctx: commands.Context, degrees: int):
        """Rotate an image."""
        await self._edit(ctx, lambda i: i.rotate(degrees % 360, expand=True), "rotated.png")

    @commands.command(name="imageflip", aliases=["imgflip"], usage="imageflip <horizontal|vertical>")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def imageflip(self, ctx: commands.Context, direction: str = "horizontal"):
        """Flip an image."""
        d = direction.lower()
        if d in ("h", "horizontal"):
            mode = Image.FLIP_LEFT_RIGHT
        elif d in ("v", "vertical"):
            mode = Image.FLIP_TOP_BOTTOM
        else:
            return await ctx.send(embed=error_embed("Direction must be `horizontal` or `vertical`."))
        await self._edit(ctx, lambda i: i.transpose(mode), "flipped.png")

    @commands.command(name="grayscale", aliases=["greyscale", "bw"], usage="grayscale")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def grayscale(self, ctx: commands.Context):
        """Convert an image to grayscale."""
        await self._edit(ctx, lambda i: i.convert("L").convert("RGBA"), "grayscale.png")

    @commands.command(name="blur", usage="blur [radius]")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def blur(self, ctx: commands.Context, radius: float = 3.0):
        """Gaussian blur an image."""
        r = max(0.1, min(20.0, radius))
        await self._edit(ctx, lambda i: i.filter(ImageFilter.GaussianBlur(radius=r)), "blurred.png")

    @commands.command(name="sharpen", usage="sharpen")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def sharpen(self, ctx: commands.Context):
        """Sharpen an image."""
        await self._edit(ctx, lambda i: i.filter(ImageFilter.SHARPEN), "sharpened.png")

    @commands.command(name="invert", usage="invert")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def invert(self, ctx: commands.Context):
        """Invert an image's colors."""

        def _invert(img: Image.Image) -> Image.Image:
            r, g, b, a = img.split()
            flip = lambda c: c.point(lambda v: 255 - v)  # noqa: E731
            return Image.merge("RGBA", (flip(r), flip(g), flip(b), a))

        await self._edit(ctx, _invert, "inverted.png")

    @commands.command(name="brightness", usage="brightness <factor>")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def brightness(self, ctx: commands.Context, factor: float):
        """Adjust brightness. 1.0 = original."""
        f = max(0.0, min(5.0, factor))
        await self._edit(ctx, lambda i: ImageEnhance.Brightness(i.convert("RGB")).enhance(f), "brightness.png")

    @commands.command(name="contrast", usage="contrast <factor>")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def contrast(self, ctx: commands.Context, factor: float):
        """Adjust contrast. 1.0 = original."""
        f = max(0.0, min(5.0, factor))
        await self._edit(ctx, lambda i: ImageEnhance.Contrast(i.convert("RGB")).enhance(f), "contrast.png")

    @commands.command(name="pixelate", usage="pixelate [size]")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def pixelate(self, ctx: commands.Context, size: int = 10):
        """Pixelate an image."""
        s = max(2, min(100, size))

        def _pixelate(img: Image.Image) -> Image.Image:
            w, h = img.size
            small = img.resize((max(1, w // s), max(1, h // s)), Image.NEAREST)
            return small.resize((w, h), Image.NEAREST)

        await self._edit(ctx, _pixelate, "pixelated.png")

    @commands.command(name="thumbnail", usage="thumbnail")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def thumbnail(self, ctx: commands.Context):
        """Make a 128×128 thumbnail."""

        def _thumb(img: Image.Image) -> Image.Image:
            copy = img.copy()
            copy.thumbnail((128, 128), Image.LANCZOS)
            return copy

        await self._edit(ctx, _thumb, "thumbnail.png")

    # ── AI image ──────────────────────────────────────────────────────────────

    @commands.hybrid_command(name="imagine", aliases=["dalle"], usage="imagine <prompt>")
    @commands.guild_only()
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def imagine(self, ctx: commands.Context, *, prompt: str):
        """Generate an AI image from a text prompt."""
        if not self._ai_client:
            return await ctx.send(embed=error_embed("Image generation needs `OPENAI_API_KEY` set."))

        cost = getattr(config, "IMAGE_COST_CENTS", 4)
        if not await self._spend_ok(ctx, cost):
            return

        async with ctx.typing():
            try:
                response = await self._ai_client.images.generate(
                    model="dall-e-3", prompt=prompt, n=1, size="1024x1024", quality="standard",
                )
            except Exception as exc:
                return await self._fail(ctx, "Image generation failed.", exc)

            await self.bot.db.add_media_spend(ctx.guild.id, cost)
            item = response.data[0]
            try:
                dest = os.path.join(tempfile.gettempdir(), f"gen_{uuid.uuid4().hex}.png")
                await vid.download(self.session, item.url, dest, MAX_IMAGE_BYTES)
                with open(dest, "rb") as f:
                    data = f.read()
                os.unlink(dest)
            except Exception as exc:
                return await self._fail(ctx, "Could not download the generated image.", exc)

            e = discord.Embed(title="🎨 Generated Image", color=discord.Color.blurple())
            e.add_field(name="Prompt", value=prompt[:1000], inline=False)
            revised = getattr(item, "revised_prompt", None)
            if revised and revised != prompt:
                e.add_field(name="Revised", value=revised[:1000], inline=False)
            e.set_image(url="attachment://generated.png")
            e.set_footer(text=f"Requested by {ctx.author}", icon_url=ctx.author.display_avatar.url)
            await ctx.send(embed=e, file=discord.File(io.BytesIO(data), filename="generated.png"))

    @commands.command(name="describe", usage="describe")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def describe(self, ctx: commands.Context):
        """Describe an attached image."""
        if not self._ai_client:
            return await ctx.send(embed=error_embed("Vision needs `OPENAI_API_KEY` set."))
        att = await self._attachment(ctx, "image")
        if not att:
            return

        async with ctx.typing():
            try:
                response = await self._ai_client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this image in detail."},
                            {"type": "image_url", "image_url": {"url": att.url}},
                        ],
                    }],
                    max_tokens=512,
                )
            except Exception as exc:
                return await self._fail(ctx, "Vision request failed.", exc)

            description = (response.choices[0].message.content or "")[:4000]
            e = discord.Embed(title="🔍 Image Description", description=description, color=discord.Color.blurple())
            e.set_thumbnail(url=att.url)
            await ctx.send(embed=e)

    # ── Spend guard ───────────────────────────────────────────────────────────

    async def _spend_ok(self, ctx: commands.Context, cents: int) -> bool:
        """Daily budget check. Without this a public bot will drain your card."""
        cap = getattr(config, "DAILY_MEDIA_CAP_CENTS", 500)
        if cap <= 0:
            return True
        spent = await self.bot.db.get_media_spend(ctx.guild.id)
        if spent + cents > cap:
            await ctx.send(embed=error_embed(
                f"This server has hit its daily media budget (${cap / 100:.2f}). Resets at midnight UTC."
            ))
            return False
        return True

    # ── Video: animate ────────────────────────────────────────────────────────

    @commands.command(name="animate", usage="animate [prompt]")
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def animate(self, ctx: commands.Context, *, prompt: str = ""):
        """Animate an attached photo into a short video clip."""
        if not self._provider:
            return await ctx.send(embed=error_embed("Video generation isn't configured on this bot."))

        att = await self._attachment(ctx, "image")
        if not att:
            return

        duration = self._provider.clamp_duration(getattr(config, "VIDEO_DURATION_SECONDS", 5))
        cost = self._provider.estimate_cents(duration)
        if not await self._spend_ok(ctx, cost):
            return

        try:
            job = await self._provider.animate(att.url, prompt or "Subtle natural motion.", duration)
        except vid.VideoError as exc:
            return await ctx.send(embed=error_embed(str(exc)))
        except Exception as exc:
            return await self._fail(ctx, "Could not submit that job.", exc)

        await self.bot.db.add_media_spend(ctx.guild.id, job.cost_cents)
        await self.bot.db.create_video_job(
            provider=job.provider, external_id=job.external_id, guild_id=ctx.guild.id,
            channel_id=ctx.channel.id, user_id=ctx.author.id, kind="animate",
            prompt=prompt[:1000], source_url=att.url, parent_id=None,
        )

        await ctx.send(embed=info_embed(
            f"🎬 Animating your image — {duration}s clip, roughly {duration * 60}s to a few minutes.\n"
            f"I'll ping you here when it's done. Estimated cost: ${job.cost_cents / 100:.2f}"
        ))

    # ── Video: extend ─────────────────────────────────────────────────────────

    @commands.command(name="extend", usage="extend [prompt]")
    @commands.guild_only()
    @commands.cooldown(1, 60, commands.BucketType.user)
    async def extend(self, ctx: commands.Context, *, prompt: str = ""):
        """
        Extend an attached video. Pulls the last frame, generates a continuation,
        and stitches the two together. Works on any video, not just ones I made.
        """
        if not self._provider:
            return await ctx.send(embed=error_embed("Video generation isn't configured on this bot."))
        if not vid.ffmpeg_available():
            return await ctx.send(embed=error_embed("ffmpeg isn't installed on this host — `extend` is unavailable."))

        att = await self._attachment(ctx, "video")
        if not att:
            return
        if att.size > vid.MAX_VIDEO_BYTES:
            return await ctx.send(embed=error_embed("That video is too large to process."))

        duration = self._provider.clamp_duration(getattr(config, "VIDEO_DURATION_SECONDS", 5))
        cost = self._provider.estimate_cents(duration)
        if not await self._spend_ok(ctx, cost):
            return

        work = tempfile.mkdtemp(prefix="extend_")
        src = os.path.join(work, "source.mp4")
        frame = os.path.join(work, "last.jpg")

        async with ctx.typing():
            try:
                await vid.download(self.session, att.url, src, vid.MAX_VIDEO_BYTES)
                await vid.extract_last_frame(src, frame)
            except vid.VideoError as exc:
                return await ctx.send(embed=error_embed(str(exc)))
            except Exception as exc:
                return await self._fail(ctx, "Could not read that video.", exc)

            # The provider needs a URL it can fetch, so the frame goes up to
            # Discord first and we hand over the CDN link.
            try:
                carrier = await ctx.send(
                    embed=info_embed("📎 Preparing the continuation frame..."),
                    file=discord.File(frame, filename="frame.jpg"),
                )
                frame_url = carrier.attachments[0].url
            except Exception as exc:
                return await self._fail(ctx, "Could not stage the frame.", exc)

            try:
                job = await self._provider.animate(
                    frame_url, prompt or "Continue the motion naturally from this frame.", duration
                )
            except vid.VideoError as exc:
                return await ctx.send(embed=error_embed(str(exc)))
            except Exception as exc:
                return await self._fail(ctx, "Could not submit that job.", exc)

        await self.bot.db.add_media_spend(ctx.guild.id, job.cost_cents)
        await self.bot.db.create_video_job(
            provider=job.provider, external_id=job.external_id, guild_id=ctx.guild.id,
            channel_id=ctx.channel.id, user_id=ctx.author.id, kind="extend",
            prompt=prompt[:1000], source_url=att.url, parent_id=None,
        )

        await carrier.edit(embed=info_embed(
            f"🎬 Generating a {duration}s continuation, then stitching it onto your clip.\n"
            f"I'll ping you here when it's done. Estimated cost: ${job.cost_cents / 100:.2f}"
        ))

    # ── Background poller ─────────────────────────────────────────────────────

    @tasks.loop(seconds=POLL_INTERVAL_SECONDS)
    async def poll_jobs(self):
        """Video jobs take minutes, so commands submit and this delivers."""
        if not self._provider:
            return
        try:
            jobs = await self.bot.db.get_pending_video_jobs(JOB_TIMEOUT_MINUTES)
        except Exception as exc:
            log.error("Could not load pending video jobs: %s", exc)
            return

        for job in jobs:
            try:
                await self._advance(job)
            except Exception as exc:
                log.error("Job %s blew up [%s]: %s", job["external_id"], _incident(), exc, exc_info=True)
                await self.bot.db.finish_video_job(job["id"], "failed", None)

    @poll_jobs.before_loop
    async def _before_poll(self):
        await self.bot.wait_until_ready()

    async def _advance(self, job):
        result = await self._provider.poll(job["external_id"])
        if result.status == "pending":
            return

        channel = self.bot.get_channel(job["channel_id"])
        mention = f"<@{job['user_id']}>"

        if result.status == "failed":
            await self.bot.db.finish_video_job(job["id"], "failed", None)
            if channel:
                await channel.send(f"{mention}", embed=error_embed(
                    f"Your `{job['kind']}` job failed: {result.error or 'unknown error'}"
                ))
            return

        await self.bot.db.finish_video_job(job["id"], "done", result.url)
        if not channel:
            return

        work = tempfile.mkdtemp(prefix="deliver_")
        try:
            clip = os.path.join(work, "clip.mp4")
            await vid.download(self.session, result.url, clip, vid.MAX_VIDEO_BYTES)

            final = clip
            if job["kind"] == "extend" and job["source_url"]:
                src = os.path.join(work, "source.mp4")
                joined = os.path.join(work, "joined.mp4")
                try:
                    await vid.download(self.session, job["source_url"], src, vid.MAX_VIDEO_BYTES)
                    final = await vid.concat_videos([src, clip], joined)
                except vid.VideoError as exc:
                    log.warning("Stitch failed, sending continuation alone: %s", exc)

            await self._deliver(channel, mention, final, result.url, job["kind"])
        except Exception as exc:
            log.error("Delivery failed [%s]: %s", _incident(), exc, exc_info=True)
            if channel:
                await channel.send(f"{mention} Your clip is ready but I couldn't attach it: {result.url}")
        finally:
            for name in os.listdir(work):
                try:
                    os.unlink(os.path.join(work, name))
                except OSError:
                    pass
            try:
                os.rmdir(work)
            except OSError:
                pass

    async def _deliver(self, channel, mention: str, path: str, fallback_url: str, kind: str):
        limit = channel.guild.filesize_limit if channel.guild else 10 * 1024 * 1024
        size = os.path.getsize(path)

        if size > limit:
            shrunk = os.path.join(os.path.dirname(path), "small.mp4")
            result = await vid.shrink_to_fit(path, shrunk, limit)
            if result:
                path, size = result, os.path.getsize(result)

        label = "Extended clip" if kind == "extend" else "Animated clip"
        if size > limit:
            return await channel.send(
                f"{mention}", embed=info_embed(
                    f"✅ {label} is ready, but it's too large to upload here.\n[Download it]({fallback_url})\n"
                    "Provider links usually expire within a couple of weeks."
                )
            )
        await channel.send(f"{mention} ✅ {label} ready.", file=discord.File(path, filename="result.mp4"))

    # ── Asset fetchers ────────────────────────────────────────────────────────

    @commands.hybrid_command(name="avatar", usage="avatar [member]")
    async def avatar(self, ctx: commands.Context, member: discord.Member = None):
        """Get a member's avatar."""
        member = member or ctx.author
        e = discord.Embed(title=f"{member.display_name}'s Avatar", color=discord.Color.blurple())
        e.set_image(url=member.display_avatar.url)
        e.add_field(name="PNG", value=f"[Link]({member.display_avatar.with_format('png').url})")
        e.add_field(name="WEBP", value=f"[Link]({member.display_avatar.with_format('webp').url})")
        await ctx.send(embed=e)

    @commands.hybrid_command(name="servericon", usage="servericon")
    @commands.guild_only()
    async def servericon(self, ctx: commands.Context):
        """Get the server's icon."""
        if not ctx.guild.icon:
            return await ctx.send(embed=error_embed("This server has no icon."))
        e = discord.Embed(title=f"{ctx.guild.name}'s Icon", color=discord.Color.blurple())
        e.set_image(url=ctx.guild.icon.url)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="serverbanner", usage="serverbanner")
    @commands.guild_only()
    async def serverbanner(self, ctx: commands.Context):
        """Get the server's banner."""
        if not ctx.guild.banner:
            return await ctx.send(embed=error_embed("This server has no banner."))
        e = discord.Embed(title=f"{ctx.guild.name}'s Banner", color=discord.Color.blurple())
        e.set_image(url=ctx.guild.banner.url)
        await ctx.send(embed=e)

    @commands.hybrid_command(name="userbanner", usage="userbanner [member]")
    async def userbanner(self, ctx: commands.Context, member: discord.Member = None):
        """Get a user's profile banner."""
        member = member or ctx.author
        try:
            user = await self.bot.fetch_user(member.id)
        except discord.HTTPException:
            return await ctx.send(embed=error_embed("Could not fetch that user."))
        if not user.banner:
            return await ctx.send(embed=error_embed(f"{member.display_name} has no banner."))
        e = discord.Embed(title=f"{member.display_name}'s Banner", color=discord.Color.blurple())
        e.set_image(url=user.banner.url)
        await ctx.send(embed=e)


async def setup(bot):
    await bot.add_cog(Imaging(bot))
