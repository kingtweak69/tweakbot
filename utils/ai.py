"""Provider-agnostic API layer.

One client that can talk to any OpenAI-compatible endpoint — the Forge,
OpenClaw, Open-Cowork, ACE Music, a local Ollama, whatever comes next —
plus a raw passthrough for endpoints that aren't chat-shaped at all.

Services are declared entirely in environment variables, so adding a new
backend never requires a code change:

    AI_SERVICES=forge,openclaw,cowork
    AI_DEFAULT_SERVICE=forge

    AI_SERVICE_FORGE_BASE_URL=https://tweakomputer.example.ts.net
    AI_SERVICE_FORGE_KEY=sk-...
    AI_SERVICE_FORGE_MODEL=~anthropic/claude-fable-latest
    AI_SERVICE_FORGE_TIMEOUT=120

    AI_SERVICE_OPENCLAW_BASE_URL=https://...
    AI_SERVICE_OPENCLAW_KEY=...
    AI_SERVICE_OPENCLAW_MODEL=...

Council fan-out (used by the Forge) is declared the same way, as
`service:model` pairs:

    AI_COUNCIL=forge:~openai/gpt-latest,forge:~x-ai/grok-latest
    AI_COUNCIL_AGGREGATOR=forge:~anthropic/claude-fable-latest

If no AI_SERVICES are declared, a `default` service is synthesised from the
legacy OPENAI_BASE_URL / OPENAI_API_KEY / AI_MODEL variables, so existing
cogs keep working untouched.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

import aiohttp

log = logging.getLogger("utils.ai")

DEFAULT_TIMEOUT = 120.0
DEFAULT_COUNCIL_TIMEOUT = 45.0
MAX_ERROR_DETAIL = 400


class AIError(RuntimeError):
    """Any failure talking to a configured service."""


@dataclass
class Service:
    name: str
    base_url: str
    api_key: str = ""
    default_model: str = ""
    timeout: float = DEFAULT_TIMEOUT
    auth_header: str = "Authorization"
    auth_prefix: str = "Bearer "
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    def headers(self) -> dict[str, str]:
        headers = dict(self.extra_headers)

        if self.api_key:
            headers[self.auth_header] = f"{self.auth_prefix}{self.api_key}"

        return headers

    def url(self, path: str) -> str:
        if path.startswith("http"):
            return path

        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"


@dataclass
class Completion:
    """A single model's answer, plus what it cost in wall time."""

    service: str
    model: str
    text: str
    message: dict[str, Any]
    raw: dict[str, Any]
    elapsed: float

    @property
    def label(self) -> str:
        return f"{self.service}:{self.model}"


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _load_services() -> dict[str, Service]:
    """Build the registry from AI_SERVICE_<NAME>_* variables."""
    services: dict[str, Service] = {}

    declared = [n.strip() for n in _env("AI_SERVICES").split(",") if n.strip()]

    for name in declared:
        key = name.upper().replace("-", "_")
        base_url = _env(f"AI_SERVICE_{key}_BASE_URL")

        if not base_url:
            log.warning("Service %r declared but has no BASE_URL; skipping.", name)
            continue

        raw_headers = _env(f"AI_SERVICE_{key}_HEADERS")
        extra: dict[str, str] = {}

        if raw_headers:
            try:
                extra = json.loads(raw_headers)
            except json.JSONDecodeError:
                log.warning("Service %r has malformed _HEADERS JSON; ignoring.", name)

        services[name.lower()] = Service(
            name=name.lower(),
            base_url=base_url,
            api_key=_env(f"AI_SERVICE_{key}_KEY"),
            default_model=_env(f"AI_SERVICE_{key}_MODEL"),
            timeout=_env_float(f"AI_SERVICE_{key}_TIMEOUT", DEFAULT_TIMEOUT),
            auth_header=_env(f"AI_SERVICE_{key}_AUTH_HEADER", "Authorization"),
            auth_prefix=_env(f"AI_SERVICE_{key}_AUTH_PREFIX", "Bearer "),
            extra_headers=extra,
        )

    # Legacy fallback so nothing that already works stops working.
    if not services:
        base_url = _env("OPENAI_BASE_URL") or "https://api.openai.com"
        services["default"] = Service(
            name="default",
            base_url=base_url,
            api_key=_env("OPENAI_API_KEY"),
            default_model=_env("AI_MODEL", "gpt-4o"),
            timeout=_env_float("AI_TIMEOUT", DEFAULT_TIMEOUT),
        )

    return services


class AIRegistry:
    """Holds every configured service and one shared HTTP session."""

    def __init__(self):
        self.services: dict[str, Service] = _load_services()
        self._session: aiohttp.ClientSession | None = None

        default = _env("AI_DEFAULT_SERVICE").lower()

        if default and default in self.services:
            self.default_service = default
        else:
            self.default_service = next(iter(self.services), "")

    # ── plumbing ─────────────────────────────────────────────────────────

    async def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()

        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def reload(self):
        """Re-read the environment. Lets `$api reload` pick up new services."""
        self.services = _load_services()
        default = _env("AI_DEFAULT_SERVICE").lower()
        self.default_service = (
            default if default in self.services else next(iter(self.services), "")
        )

    def get(self, name: str = "") -> Service:
        name = (name or self.default_service).lower()
        service = self.services.get(name)

        if service is None:
            known = ", ".join(sorted(self.services)) or "none configured"
            raise AIError(f"Unknown service {name!r}. Configured: {known}")

        if not service.configured:
            raise AIError(f"Service {name!r} has no base URL set.")

        return service

    def resolve(self, target: str) -> tuple[Service, str]:
        """Split a `service:model` string. Either half may be omitted."""
        if ":" in target and not target.startswith("http"):
            service_name, _, model = target.partition(":")
        else:
            service_name, model = "", target

        service = self.get(service_name)

        return service, (model.strip() or service.default_model)

    # ── generic transport ────────────────────────────────────────────────

    async def request(
        self,
        service: Service | str,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        data: Any = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
        expect: str = "json",
    ) -> Any:
        """Call any path on a service. Use this for non-chat endpoints."""
        if isinstance(service, str):
            service = self.get(service)

        session = await self.session()
        headers = service.headers()

        if json_body is not None:
            headers["Content-Type"] = "application/json"

        try:
            async with session.request(
                method,
                service.url(path),
                json=json_body,
                data=data,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout or service.timeout),
            ) as response:
                body = await response.read()

                if response.status == 401:
                    raise AIError(f"{service.name} rejected the API key.")

                if response.status == 404:
                    raise AIError(
                        f"{service.name} has no route at {path!r} (HTTP 404). "
                        "Check the endpoint shape."
                    )

                if response.status == 429:
                    raise AIError(f"{service.name} is rate limiting.")

                if response.status >= 400:
                    detail = body.decode("utf-8", errors="replace")[:MAX_ERROR_DETAIL]
                    raise AIError(
                        f"{service.name} returned HTTP {response.status}: {detail}"
                    )

        except asyncio.TimeoutError as exc:
            raise AIError(f"{service.name} timed out.") from exc

        except aiohttp.ClientError as exc:
            raise AIError(f"Could not reach {service.name}: {exc}") from exc

        if expect == "bytes":
            return body

        if expect == "text":
            return body.decode("utf-8", errors="replace")

        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise AIError(f"{service.name} returned a malformed response.") from exc

    # ── chat ─────────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        service: str = "",
        model: str = "",
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        **extra: Any,
    ) -> Completion:
        """One OpenAI-compatible chat completion."""
        svc = self.get(service)
        model = model or svc.default_model

        if not model:
            raise AIError(f"No model set for service {svc.name!r}.")

        payload: dict[str, Any] = {"model": model, "messages": messages, **extra}

        if tools:
            payload["tools"] = tools

        if temperature is not None:
            payload["temperature"] = temperature

        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        started = time.monotonic()
        data = await self.request(
            svc,
            "POST",
            "/v1/chat/completions",
            json_body=payload,
            timeout=timeout,
        )
        elapsed = time.monotonic() - started

        choices = data.get("choices") or []

        if not choices:
            raise AIError(f"{svc.name} returned no choices.")

        message = choices[0].get("message") or {}
        content = message.get("content")

        if isinstance(content, list):
            content = "".join(
                part.get("text", "") for part in content if isinstance(part, dict)
            )

        return Completion(
            service=svc.name,
            model=model,
            text=(content or "").strip(),
            message=message,
            raw=data,
            elapsed=elapsed,
        )

    async def models(self, service: str = "") -> list[str]:
        data = await self.request(self.get(service), "GET", "/v1/models")
        entries = data.get("data") if isinstance(data, dict) else data

        if not isinstance(entries, list):
            return []

        return [
            str(entry.get("id"))
            for entry in entries
            if isinstance(entry, dict) and entry.get("id")
        ]

    async def ping(self, service: str = "", model: str = "") -> Completion:
        """Cheapest possible round trip, for health checks."""
        return await self.chat(
            [{"role": "user", "content": "ping"}],
            service=service,
            model=model,
            max_tokens=8,
            timeout=30,
        )

    # ── council fan-out ──────────────────────────────────────────────────

    def council_members(self) -> list[str]:
        return [m.strip() for m in _env("AI_COUNCIL").split(",") if m.strip()]

    def council_aggregator(self) -> str:
        return _env("AI_COUNCIL_AGGREGATOR") or self.default_service

    async def fanout(
        self,
        messages: list[dict[str, Any]],
        targets: Iterable[str],
        *,
        timeout: float | None = None,
        **extra: Any,
    ) -> tuple[list[Completion], list[tuple[str, str]]]:
        """Ask several models at once. Returns (successes, failures).

        Every target gets its own timeout, and one dead upstream never takes
        the whole turn down — whoever answers in time is what you work with.
        """
        targets = list(targets)
        limit = timeout or _env_float("AI_COUNCIL_TIMEOUT", DEFAULT_COUNCIL_TIMEOUT)

        async def _one(target: str) -> Completion:
            svc, model = self.resolve(target)
            return await asyncio.wait_for(
                self.chat(
                    messages,
                    service=svc.name,
                    model=model,
                    timeout=limit,
                    **extra,
                ),
                timeout=limit + 5,
            )

        results = await asyncio.gather(
            *(_one(target) for target in targets),
            return_exceptions=True,
        )

        good: list[Completion] = []
        bad: list[tuple[str, str]] = []

        for target, result in zip(targets, results):
            if isinstance(result, Completion):
                good.append(result)
            elif isinstance(result, asyncio.TimeoutError):
                bad.append((target, "timed out"))
            elif isinstance(result, BaseException):
                bad.append((target, str(result)[:160]))

        return good, bad

    async def council(
        self,
        prompt: str,
        *,
        system: str = "",
        targets: Iterable[str] | None = None,
        aggregator: str = "",
        **extra: Any,
    ) -> tuple[Completion, list[Completion], list[tuple[str, str]]]:
        """Fan out to the council, then have one model synthesise the answers."""
        targets = list(targets or self.council_members())

        if not targets:
            raise AIError("No council members configured. Set AI_COUNCIL.")

        messages: list[dict[str, Any]] = []

        if system:
            messages.append({"role": "system", "content": system})

        messages.append({"role": "user", "content": prompt})

        answers, failures = await self.fanout(messages, targets, **extra)

        if not answers:
            detail = "; ".join(f"{name}: {why}" for name, why in failures)
            raise AIError(f"Every council member failed. {detail}")

        transcript = "\n\n".join(
            f"### {completion.label}\n{completion.text}" for completion in answers
        )

        synthesis_prompt = (
            "Several models answered the same question. Write the single best "
            "answer, keeping what they agree on, resolving contradictions on "
            "the merits, and dropping anything unsupported. Do not mention "
            "the other models or that multiple answers existed.\n\n"
            f"QUESTION:\n{prompt}\n\nANSWERS:\n{transcript}"
        )

        svc, model = self.resolve(aggregator or self.council_aggregator())

        final = await self.chat(
            ([{"role": "system", "content": system}] if system else [])
            + [{"role": "user", "content": synthesis_prompt}],
            service=svc.name,
            model=model,
        )

        return final, answers, failures


# Shared instance. Import this, don't build your own.
registry = AIRegistry()
