"""Public HTTP API server.

Turns TweakBot into an OpenAI-compatible API server on its own domain, so
OpenClaw, Open-Cowork, the Forge, or any OpenAI SDK client can point at it
and use every service registered in `utils/ai.py` — including the council,
exposed as a single virtual model.

Not a cog. `bot.py` calls `await server.start(bot)` in setup_hook and
`await server.stop()` in close.

    base_url = https://your-app.up.railway.app/v1
    api_key  = <TWEAKBOT_API_TOKEN>
    model    = forge:~anthropic/claude-fable-latest
    model    = tweakbot-forge      # runs the whole council, returns one answer

Routes:
    GET  /health                 liveness, no auth
    GET  /v1/models              every service:model pair, plus the council
    POST /v1/chat/completions    OpenAI-compatible, streaming emulated

Environment variables:
    TWEAKBOT_API_TOKEN       required — comma-separated list allowed
    PORT                     injected by Railway, defaults to 8080
    GATEWAY_ENABLED=true
    GATEWAY_COUNCIL_MODEL=tweakbot-forge
    GATEWAY_RATE_LIMIT=30    requests per token per minute
    GATEWAY_MAX_CONCURRENT=8

Without TWEAKBOT_API_TOKEN the gateway still starts and serves /health, but
every /v1 route returns 503. That is deliberate: an unauthenticated public
endpoint in front of your upstream quota is an open proxy, and someone will
find it.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
import uuid

from aiohttp import web

from utils.ai import AIError, registry

log = logging.getLogger("utils.server")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


GATEWAY_ENABLED = _env("GATEWAY_ENABLED", "true").lower() not in ("false", "0", "no")
PORT = _env_int("API_PORT", 8080)
COUNCIL_MODEL = _env("GATEWAY_COUNCIL_MODEL", "tweakbot-forge")
RATE_LIMIT = _env_int("GATEWAY_RATE_LIMIT", 30)
MAX_CONCURRENT = _env_int("GATEWAY_MAX_CONCURRENT", 8)

TOKENS = {t.strip() for t in _env("TWEAKBOT_API_TOKEN").split(",") if t.strip()}


def _json_error(message: str, status: int, kind: str = "invalid_request_error"):
    return web.json_response(
        {"error": {"message": message, "type": kind, "code": status}},
        status=status,
    )


def _completion_body(model: str, text: str, extra: dict | None = None) -> dict:
    body = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
    }

    if extra:
        body["tweakbot"] = extra

    return body


class APIServer:
    """Serves the AI registry over HTTP on the bot's public domain."""

    def __init__(self):
        self.bot = None
        self.runner: web.AppRunner | None = None
        self._semaphore = asyncio.Semaphore(MAX_CONCURRENT)
        self._hits: dict[str, collections.deque] = {}

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self, bot):
        """Bind the port. Safe to call once from setup_hook."""
        if not GATEWAY_ENABLED:
            log.info("API server disabled by GATEWAY_ENABLED.")
            return

        if self.runner is not None:
            log.warning("API server already running; ignoring duplicate start.")
            return

        self.bot = bot

        app = web.Application(middlewares=[self._auth_middleware])
        app.router.add_get("/health", self.handle_health)
        app.router.add_get("/", self.handle_health)
        app.router.add_get("/v1/models", self.handle_models)
        app.router.add_post("/v1/chat/completions", self.handle_chat)

        self.runner = web.AppRunner(app, access_log=None)

        try:
            await self.runner.setup()
            await web.TCPSite(self.runner, "0.0.0.0", PORT).start()
        except OSError as exc:
            log.error("API server could not bind port %s: %s", PORT, exc)
            self.runner = None
            return

        if TOKENS:
            log.info("API server listening on 0.0.0.0:%s with %d token(s).", PORT, len(TOKENS))
        else:
            log.warning(
                "API server listening on 0.0.0.0:%s but TWEAKBOT_API_TOKEN is unset — "
                "all /v1 routes will return 503.",
                PORT,
            )

    async def stop(self):
        """Release the port. Called from Bot.close()."""
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner = None
            log.info("API server stopped.")

    # ── auth and rate limiting ───────────────────────────────────────────

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        if request.path in ("/health", "/"):
            return await handler(request)

        if not TOKENS:
            return _json_error(
                "This gateway has no API token configured and is refusing "
                "requests. Set TWEAKBOT_API_TOKEN.",
                503,
                "service_unavailable",
            )

        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""

        if token not in TOKENS:
            return _json_error("Invalid API key.", 401, "authentication_error")

        if not self._allow(token):
            return _json_error(
                f"Rate limit exceeded ({RATE_LIMIT}/min).", 429, "rate_limit_error"
            )

        request["token"] = token
        return await handler(request)

    def _allow(self, token: str) -> bool:
        now = time.monotonic()
        hits = self._hits.setdefault(token, collections.deque())

        while hits and now - hits[0] > 60:
            hits.popleft()

        if len(hits) >= RATE_LIMIT:
            return False

        hits.append(now)
        return True

    # ── handlers ─────────────────────────────────────────────────────────

    async def handle_health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "bot": str(self.bot.user) if self.bot and self.bot.user else None,
                "guilds": len(self.bot.guilds) if self.bot else 0,
                "services": sorted(registry.services),
                "authenticated": bool(TOKENS),
            }
        )

    async def handle_models(self, request: web.Request) -> web.Response:
        entries = []

        for name, service in sorted(registry.services.items()):
            if service.default_model:
                entries.append(f"{name}:{service.default_model}")

            try:
                for model in await registry.models(name):
                    entries.append(f"{name}:{model}")
            except AIError as exc:
                log.warning("Could not list models for %s: %s", name, exc)

        if registry.council_members():
            entries.append(COUNCIL_MODEL)

        seen = sorted(set(entries))

        return web.json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": model,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "tweakbot",
                    }
                    for model in seen
                ],
            }
        )

    async def handle_chat(self, request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return _json_error("Request body must be JSON.", 400)

        messages = payload.get("messages")

        if not isinstance(messages, list) or not messages:
            return _json_error("`messages` is required.", 400)

        model = str(payload.get("model") or "").strip()
        wants_stream = bool(payload.get("stream"))

        passthrough = {
            key: payload[key]
            for key in ("temperature", "max_tokens", "top_p", "tools")
            if key in payload
        }

        try:
            async with self._semaphore:
                if model == COUNCIL_MODEL:
                    text, extra = await self._run_council(messages, passthrough)
                else:
                    text, extra = await self._run_single(model, messages, passthrough)

        except AIError as exc:
            return _json_error(str(exc), 502, "upstream_error")

        except asyncio.TimeoutError:
            return _json_error("Upstream timed out.", 504, "upstream_error")

        except Exception:
            log.exception("API request failed.")
            return _json_error("Internal error.", 500, "server_error")

        if wants_stream:
            return await self._stream(request, model or "tweakbot", text)

        return web.json_response(_completion_body(model or "tweakbot", text, extra))

    async def _run_single(
        self,
        model: str,
        messages: list,
        passthrough: dict,
    ) -> tuple[str, dict]:
        service, resolved = registry.resolve(model) if model else (
            registry.get(),
            registry.get().default_model,
        )

        completion = await registry.chat(
            messages,
            service=service.name,
            model=resolved,
            **passthrough,
        )

        return completion.text, {
            "service": completion.service,
            "model": completion.model,
            "elapsed": round(completion.elapsed, 2),
        }

    async def _run_council(self, messages: list, passthrough: dict) -> tuple[str, dict]:
        system = next(
            (m.get("content", "") for m in messages if m.get("role") == "system"),
            "",
        )
        prompt = next(
            (
                m.get("content", "")
                for m in reversed(messages)
                if m.get("role") == "user"
            ),
            "",
        )

        if not prompt:
            raise AIError("The council needs at least one user message.")

        final, answers, failures = await registry.council(
            prompt,
            system=system,
            **passthrough,
        )

        return final.text, {
            "council": [a.label for a in answers],
            "failed": [name for name, _ in failures],
            "aggregator": final.label,
        }

    async def _stream(
        self,
        request: web.Request,
        model: str,
        text: str,
    ) -> web.StreamResponse:
        """Emulated SSE — one chunk then [DONE].

        The upstream answer is already complete by the time we get here, so
        this exists purely so clients that require `stream: true` don't break.
        """
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await response.prepare(request)

        chunk = {
            "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": None,
                }
            ],
        }

        done = {
            "id": chunk["id"],
            "object": "chat.completion.chunk",
            "created": chunk["created"],
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }

        await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
        await response.write(f"data: {json.dumps(done)}\n\n".encode())
        await response.write(b"data: [DONE]\n\n")
        await response.write_eof()

        return response


# Shared instance. bot.py starts and stops this one.
server = APIServer()
