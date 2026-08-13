"""Persistent autonomous jobs for TweakBot.

Jobs are stored in PostgreSQL and executed through the runtime capability
registry.  A Railway restart therefore pauses rather than destroys a task.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any

import discord
from discord.ext import commands

import config
from utils.capabilities import _audit_safe
from utils.ai import AIError, registry as ai_registry

log = logging.getLogger("cogs.agent_jobs")

SOURCE = "agent_jobs"
DEFAULT_MAX_STEPS = max(0, int(getattr(config, "AGENT_JOB_MAX_STEPS", 0)))
MAX_ACTIVE_PER_USER = max(1, int(getattr(config, "AGENT_MAX_ACTIVE_JOBS", 3)))
POLL_SECONDS = max(1.0, float(getattr(config, "AGENT_JOB_POLL_SECONDS", 3.0)))
JOB_MAX_TOKENS = max(128, int(getattr(config, "AGENT_JOB_MAX_TOKENS", 1200)))
COMPACT_AFTER_MESSAGES = max(12, int(getattr(config, "AGENT_JOB_COMPACT_AFTER_MESSAGES", 48)))
COMPACT_AFTER_CHARS = max(12000, int(getattr(config, "AGENT_JOB_COMPACT_AFTER_CHARS", 60000)))
COMPACT_KEEP_ROUNDS = max(1, int(getattr(config, "AGENT_JOB_COMPACT_KEEP_ROUNDS", 6)))
COMPACT_SUMMARY_CHARS = max(4000, int(getattr(config, "AGENT_JOB_COMPACT_SUMMARY_CHARS", 18000)))

_AGENT_CONTROL_NAMES = {
    "start_agent_job",
    "agent_job_status",
    "agent_job_cancel",
    "agent_job_resume",
    "list_agent_jobs",
}

JOB_SYSTEM_PROMPT = """You are TweakBot's persistent job executor.

Execute the user's goal by using the supplied capabilities. Work incrementally and
use tool results as ground truth. Never invent success. Existing Discord
permissions, OAuth accounts, confirmations, cooldowns, and command checks remain
authoritative.

When the goal is fully complete and no more tool calls are needed, reply with:
DONE: <concise final result>

If progress cannot continue without new information from the user, reply with:
NEEDS_INPUT: <exact information needed>

If the goal is impossible after using the available capabilities, reply with:
FAILED: <specific reason>

Do not create another persistent agent job from inside a job.
""".strip()


class AgentJobs(commands.Cog):
    """Postgres-backed persistent jobs executed through bot.capabilities."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._wake = asyncio.Event()
        self._worker: asyncio.Task | None = None

    async def cog_load(self) -> None:
        registry = self.bot.capabilities
        registry.register(
            name="start_agent_job",
            description=(
                "Create a persistent autonomous task that survives bot/Railway restarts. "
                "Use this for multi-step work the user explicitly wants TweakBot to keep "
                "working on beyond the current reply."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "goal": {"type": "string"},
                    "max_steps": {
                        "type": "integer",
                        "minimum": 0,
                        "description": "Optional capability-step limit. 0 means no configured limit.",
                    },
                },
                "required": ["goal"],
            },
            handler=self._cap_start,
            category="agent-control",
            source=SOURCE,
        )
        registry.register(
            name="agent_job_status",
            description="Read the status and latest result of one of the requester's persistent jobs.",
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "integer"}},
                "required": ["job_id"],
            },
            handler=self._cap_status,
            category="agent-control",
            source=SOURCE,
        )
        registry.register(
            name="list_agent_jobs",
            description="List the requester's recent persistent jobs.",
            parameters={"type": "object", "properties": {}},
            handler=self._cap_list,
            category="agent-control",
            source=SOURCE,
        )
        registry.register(
            name="agent_job_cancel",
            description="Cancel one of the requester's queued/running persistent jobs.",
            parameters={
                "type": "object",
                "properties": {"job_id": {"type": "integer"}},
                "required": ["job_id"],
            },
            handler=self._cap_cancel,
            category="agent-control",
            source=SOURCE,
            destructive=True,
        )
        registry.register(
            name="agent_job_resume",
            description="Resume a persistent job that is waiting for user input.",
            parameters={
                "type": "object",
                "properties": {
                    "job_id": {"type": "integer"},
                    "input": {"type": "string"},
                },
                "required": ["job_id"],
            },
            handler=self._cap_resume,
            category="agent-control",
            source=SOURCE,
        )
        self._worker = asyncio.create_task(self._worker_loop(), name="tweakbot-agent-jobs")

    async def cog_unload(self) -> None:
        self.bot.capabilities.unregister_source(SOURCE)
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    async def _create_job(
        self,
        ctx: commands.Context,
        goal: str,
        max_steps: int | None = None,
    ) -> int:
        if not self.bot.db:
            raise RuntimeError("Database is unavailable.")
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("A job goal is required.")
        if len(goal) > 12000:
            raise ValueError("Job goal is too long.")

        active = await self.bot.db.count_active_agent_jobs(ctx.author.id)
        if active >= MAX_ACTIVE_PER_USER:
            raise RuntimeError(
                f"You already have {active} active jobs; the current limit is {MAX_ACTIVE_PER_USER}."
            )

        limit = DEFAULT_MAX_STEPS if max_steps is None else max(0, int(max_steps))
        job_id = await self.bot.db.create_agent_job(
            user_id=ctx.author.id,
            guild_id=ctx.guild.id if ctx.guild else None,
            channel_id=ctx.channel.id,
            message_id=ctx.message.id,
            goal=goal,
            max_steps=limit,
        )
        self._wake.set()
        return job_id

    async def _cap_start(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        try:
            job_id = await self._create_job(
                ctx,
                str(args.get("goal") or ""),
                int(args["max_steps"]) if args.get("max_steps") is not None else None,
            )
            return f"Persistent agent job #{job_id} queued. It will survive a bot restart."
        except Exception as exc:
            return f"Could not create persistent job: {type(exc).__name__}: {exc}"[:1000]

    async def _cap_status(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        return await self._status_text(ctx, int(args.get("job_id") or 0))

    async def _cap_list(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        if not self.bot.db:
            return "Database is unavailable."
        rows = await self.bot.db.list_agent_jobs(ctx.author.id, 20)
        if not rows:
            return "You have no persistent agent jobs."
        lines = [
            f"#{row['id']} [{row['status']}] steps={row['step_count']} — {str(row['goal'])[:120]}"
            for row in rows
        ]
        return "Recent agent jobs:\n" + "\n".join(lines)

    async def _cap_cancel(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        if not self.bot.db:
            return "Database is unavailable."
        job_id = int(args.get("job_id") or 0)
        if await self.bot.db.cancel_agent_job(job_id, ctx.author.id):
            return f"Cancelled agent job #{job_id}."
        return f"Agent job #{job_id} was not cancellable or does not belong to you."

    async def _cap_resume(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        job_id = int(args.get("job_id") or 0)
        return await self._resume_job(ctx, job_id, str(args.get("input") or ""))

    async def _status_text(self, ctx: commands.Context, job_id: int) -> str:
        if not self.bot.db:
            return "Database is unavailable."
        row = await self.bot.db.get_agent_job(job_id)
        if not row:
            return f"Agent job #{job_id} does not exist."
        if int(row["user_id"]) != ctx.author.id and not await self.bot.is_owner(ctx.author):
            return "That job does not belong to you."
        text = (
            f"Agent job #{job_id}: {row['status']}\n"
            f"Goal: {row['goal']}\n"
            f"Steps: {row['step_count']}"
        )
        if row["result"]:
            text += f"\nResult: {row['result']}"
        if row["last_error"]:
            text += f"\nLast error: {row['last_error']}"
        return text[:12000]

    async def _resume_job(self, ctx: commands.Context, job_id: int, user_input: str) -> str:
        if not self.bot.db:
            return "Database is unavailable."
        row = await self.bot.db.get_agent_job(job_id)
        if not row or int(row["user_id"]) != ctx.author.id:
            return f"Agent job #{job_id} does not exist or does not belong to you."
        if str(row["status"]) != "needs_input":
            return f"Agent job #{job_id} is {row['status']}, not waiting for input."

        try:
            state = json.loads(row["state_json"] or "[]")
        except json.JSONDecodeError:
            state = []
        if user_input.strip():
            state.append({"role": "user", "content": user_input.strip()[:12000]})
            await self.bot.db.update_agent_job_state(
                job_id,
                state_json=json.dumps(state, ensure_ascii=False),
                step_count=int(row["step_count"]),
                last_error=None,
            )

        resumed = await self.bot.db.resume_agent_job(
            job_id,
            ctx.author.id,
            guild_id=ctx.guild.id if ctx.guild else None,
            channel_id=ctx.channel.id,
            message_id=ctx.message.id,
        )
        if resumed:
            self._wake.set()
            return f"Agent job #{job_id} resumed."
        return f"Agent job #{job_id} could not be resumed."

    @staticmethod
    def _state_size_chars(state: list[dict[str, Any]]) -> int:
        """Cheap context-size proxy that does not require a tokenizer."""
        total = 0
        for item in state:
            try:
                total += len(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
            except (TypeError, ValueError):
                total += len(str(item))
        return total

    @staticmethod
    def _compact_excerpt(value: Any, limit: int) -> str:
        """Keep useful head/tail detail from large tool results and arguments."""
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        else:
            try:
                text = json.dumps(value, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                text = str(value)
        text = text.strip()
        if len(text) <= limit:
            return text
        # Keep both ends: IDs, SHAs, URLs and final error summaries often land at
        # opposite ends of a tool result.
        head = max(1, (limit - 48) // 2)
        tail = max(1, limit - head - 48)
        return f"{text[:head]}\n...[{len(text) - head - tail} chars compacted]...\n{text[-tail:]}"

    @classmethod
    def _local_compaction_summary(cls, older: list[dict[str, Any]]) -> str:
        """Lossily summarize old model context; canonical full steps stay in DB."""
        lines: list[str] = [
            "Earlier job progress was compacted to keep context bounded.",
            "Full capability arguments/results remain persisted in agent_job_steps.",
        ]
        for item in older:
            role = str(item.get("role") or "unknown")
            if role == "system" and str(item.get("content") or "").startswith(
                "COMPACTED JOB HISTORY"
            ):
                prior = cls._compact_excerpt(item.get("content"), 5000)
                if prior:
                    lines.append(f"PRIOR COMPACTION:\n{prior}")
                continue
            if role == "user":
                text = cls._compact_excerpt(item.get("content"), 1800)
                if text:
                    lines.append(f"USER INPUT: {text}")
                continue
            if role == "assistant":
                text = cls._compact_excerpt(item.get("content"), 1200)
                if text:
                    lines.append(f"ASSISTANT: {text}")
                for call in item.get("tool_calls") or []:
                    function = call.get("function") or {}
                    name = str(function.get("name") or "unknown")
                    args = cls._compact_excerpt(function.get("arguments") or "{}", 1000)
                    lines.append(f"TOOL CALL {name}: {args}")
                continue
            if role == "tool":
                call_id = str(item.get("tool_call_id") or "")
                result = cls._compact_excerpt(item.get("content"), 1800)
                lines.append(f"TOOL RESULT {call_id or '(unknown id)'}: {result}")
                continue
            text = cls._compact_excerpt(item.get("content"), 1000)
            if text:
                lines.append(f"{role.upper()}: {text}")

        text = "\n\n".join(lines)
        if len(text) <= COMPACT_SUMMARY_CHARS:
            return text
        # Prefer the newest compacted history while retaining the explanation.
        header = "\n\n".join(lines[:2])
        room = max(1000, COMPACT_SUMMARY_CHARS - len(header) - 80)
        return (
            header
            + "\n\n...[older compacted history omitted; canonical steps remain in PostgreSQL]...\n\n"
            + text[-room:]
        )[:COMPACT_SUMMARY_CHARS]

    @classmethod
    def _compaction_cutoff(cls, state: list[dict[str, Any]]) -> int | None:
        """Choose a boundary that never separates assistant tool_calls from tools."""
        if len(state) <= 3:
            return None

        starts = [
            index
            for index, item in enumerate(state)
            if index >= 2
            and item.get("role") == "assistant"
            and bool(item.get("tool_calls"))
        ]
        if not starts:
            return None

        # Normally preserve the most recent N complete capability rounds.
        candidate_pos = max(0, len(starts) - COMPACT_KEEP_ROUNDS)
        cutoff = starts[candidate_pos]

        # If that suffix is itself too large, retain fewer recent rounds until it
        # fits roughly half the trigger budget. Never split a tool-call round.
        target_suffix = max(8000, COMPACT_AFTER_CHARS // 2)
        for start in starts[candidate_pos:]:
            suffix_size = cls._state_size_chars(state[start:])
            if suffix_size <= target_suffix:
                cutoff = start
                break
        else:
            cutoff = starts[-1]

        # There must be something meaningful to compact beyond the base
        # system+goal entries.
        return cutoff if cutoff > 2 else None

    @classmethod
    def _maybe_compact_state(
        cls, state: list[dict[str, Any]]
    ) -> tuple[list[dict[str, Any]], bool]:
        size = cls._state_size_chars(state)
        if len(state) < COMPACT_AFTER_MESSAGES and size < COMPACT_AFTER_CHARS:
            return state, False

        cutoff = cls._compaction_cutoff(state)
        if cutoff is None:
            return state, False

        base = state[:2]
        older = state[2:cutoff]
        recent = state[cutoff:]
        summary = cls._local_compaction_summary(older)
        compacted = base + [
            {
                "role": "system",
                "content": "COMPACTED JOB HISTORY\n" + summary,
            }
        ] + recent

        # Do not replace the state unless compaction actually made it smaller.
        if cls._state_size_chars(compacted) >= size:
            return state, False
        return compacted, True

    async def _worker_loop(self) -> None:
        await self.bot.wait_until_ready()
        if not self.bot.db:
            log.error("Agent job worker cannot start: database unavailable")
            return

        recovered = await self.bot.db.recover_agent_jobs()
        if recovered:
            log.warning("Recovered %d interrupted agent job(s) after restart.", recovered)

        while not self.bot.is_closed():
            try:
                job = await self.bot.db.claim_next_agent_job()
                if job:
                    await self._run_job(job)
                    continue
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Persistent agent worker loop failed")
                await asyncio.sleep(POLL_SECONDS)

    async def _context_for_job(self, job) -> commands.Context:
        channel = self.bot.get_channel(int(job["channel_id"]))
        if channel is None:
            channel = await self.bot.fetch_channel(int(job["channel_id"]))
        if not hasattr(channel, "fetch_message"):
            raise RuntimeError("The job channel cannot fetch the originating message.")
        message = await channel.fetch_message(int(job["message_id"]))
        if message.author.id != int(job["user_id"]):
            raise RuntimeError("The job's originating message no longer belongs to its creator.")
        return await self.bot.get_context(message)

    def _job_tools(self, ctx: commands.Context) -> list[dict[str, Any]]:
        tools = self.bot.capabilities.openai_tools(ctx)
        return [
            tool for tool in tools
            if tool.get("function", {}).get("name") not in _AGENT_CONTROL_NAMES
        ]

    async def _notify(self, channel_id: int, text: str) -> None:
        try:
            channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
            await channel.send(
                text[:1950],
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            log.exception("Could not post agent job update to channel %s", channel_id)

    async def _run_job(self, job) -> None:
        assert self.bot.db is not None
        job_id = int(job["id"])
        try:
            ctx = await self._context_for_job(job)
        except Exception as exc:
            detail = f"Cannot reconstruct Discord context: {type(exc).__name__}: {exc}"
            await self.bot.db.finish_agent_job(
                job_id, status="needs_input", last_error=detail
            )
            await self._notify(
                int(job["channel_id"]),
                f"Agent job #{job_id} needs input: {detail}",
            )
            return

        try:
            state = json.loads(job["state_json"] or "[]")
            if not isinstance(state, list):
                state = []
        except json.JSONDecodeError:
            state = []

        if not state:
            state = [
                {"role": "system", "content": JOB_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"JOB #{job_id}\nGOAL:\n{job['goal']}",
                },
            ]

        step_count = int(job["step_count"] or 0)
        max_steps = int(job["max_steps"] or 0)
        tools = self._job_tools(ctx)

        if not tools:
            await self.bot.db.finish_agent_job(
                job_id,
                status="failed",
                last_error="No executable capabilities are registered for this context.",
            )
            return

        try:
            while True:
                current = await self.bot.db.get_agent_job(job_id)
                if not current or current["status"] == "cancelled":
                    return
                if max_steps and step_count >= max_steps:
                    await self.bot.db.finish_agent_job(
                        job_id,
                        status="failed",
                        last_error=f"Configured job step limit ({max_steps}) reached.",
                    )
                    await self._notify(
                        int(job["channel_id"]),
                        f"Agent job #{job_id} stopped after reaching its configured {max_steps}-step limit.",
                    )
                    return

                state, compacted = self._maybe_compact_state(state)
                if compacted:
                    await self.bot.db.update_agent_job_state(
                        job_id,
                        state_json=json.dumps(state, ensure_ascii=False),
                        step_count=step_count,
                    )
                    log.info(
                        "Compacted model-facing transcript for agent job #%s to %d messages / %d chars",
                        job_id,
                        len(state),
                        self._state_size_chars(state),
                    )

                completion = await ai_registry.chat(
                    state,
                    model=getattr(config, "OPENAI_MODEL", "") or getattr(config, "AI_MODEL", ""),
                    tools=tools,
                    max_tokens=JOB_MAX_TOKENS,
                )
                message = completion.message or {}
                tool_calls = message.get("tool_calls") or []

                if not tool_calls:
                    text = (completion.text or "").strip()
                    upper = text.upper()
                    if upper.startswith("NEEDS_INPUT:"):
                        result = text.split(":", 1)[1].strip()
                        await self.bot.db.finish_agent_job(
                            job_id, status="needs_input", result=_audit_safe(result)
                        )
                        await self._notify(
                            int(job["channel_id"]),
                            f"Agent job #{job_id} needs input: {_audit_safe(result, limit=1500)}",
                        )
                        return
                    if upper.startswith("FAILED:"):
                        result = text.split(":", 1)[1].strip()
                        await self.bot.db.finish_agent_job(
                            job_id, status="failed", last_error=_audit_safe(result)
                        )
                        await self._notify(
                            int(job["channel_id"]),
                            f"Agent job #{job_id} failed: {_audit_safe(result, limit=1500)}",
                        )
                        return

                    result = text.split(":", 1)[1].strip() if upper.startswith("DONE:") else text
                    result = result or "Completed."
                    await self.bot.db.finish_agent_job(
                        job_id, status="completed", result=_audit_safe(result)
                    )
                    await self._notify(
                        int(job["channel_id"]),
                        f"Agent job #{job_id} completed: {_audit_safe(result, limit=1500)}",
                    )
                    return

                assistant_entry = {
                    "role": "assistant",
                    "content": message.get("content"),
                    "tool_calls": tool_calls,
                }
                state.append(assistant_entry)

                for call in tool_calls:
                    current = await self.bot.db.get_agent_job(job_id)
                    if not current or current["status"] == "cancelled":
                        return

                    function = call.get("function") or {}
                    name = str(function.get("name") or "")
                    raw_args = function.get("arguments") or "{}"
                    try:
                        args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                    except json.JSONDecodeError:
                        args = {}
                    if not isinstance(args, dict):
                        args = {}

                    outcome = await self.bot.capabilities.execute(ctx, name, args)
                    step_count += 1
                    await self.bot.db.add_agent_job_step(
                        job_id=job_id,
                        step_index=step_count,
                        capability=name or "unknown",
                        arguments=_audit_safe(args),
                        result=_audit_safe(outcome),
                    )
                    state.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.get("id"),
                            "content": _audit_safe(outcome),
                        }
                    )
                    await self.bot.db.update_agent_job_state(
                        job_id,
                        state_json=json.dumps(state, ensure_ascii=False),
                        step_count=step_count,
                    )

        except asyncio.CancelledError:
            raise
        except (AIError, Exception) as exc:
            detail = f"{type(exc).__name__}: {exc}"[:1500]
            log.exception("Agent job #%s failed", job_id)
            await self.bot.db.finish_agent_job(
                job_id, status="failed", last_error=detail
            )
            await self._notify(
                int(job["channel_id"]),
                f"Agent job #{job_id} failed: {detail}",
            )

    @commands.group(name="job", aliases=["agentjob", "jobs"], invoke_without_command=True)
    async def job(self, ctx: commands.Context) -> None:
        """Persistent autonomous job controls."""
        await ctx.send(
            f"Use `{ctx.clean_prefix}job start <goal>`, `job list`, `job status <id>`, "
            "`job cancel <id>`, or `job resume <id> [input]`."
        )

    @job.command(name="start")
    async def job_start(self, ctx: commands.Context, *, goal: str) -> None:
        job_id = await self._create_job(ctx, goal)
        await ctx.send(f"Persistent agent job #{job_id} queued.")

    @job.command(name="list")
    async def job_list(self, ctx: commands.Context) -> None:
        await ctx.send(await self._cap_list(ctx, {}))

    @job.command(name="status")
    async def job_status(self, ctx: commands.Context, job_id: int) -> None:
        await ctx.send(await self._status_text(ctx, job_id))

    @job.command(name="cancel")
    async def job_cancel(self, ctx: commands.Context, job_id: int) -> None:
        await ctx.send(await self._cap_cancel(ctx, {"job_id": job_id}))

    @job.command(name="resume")
    async def job_resume(self, ctx: commands.Context, job_id: int, *, input: str = "") -> None:
        await ctx.send(await self._resume_job(ctx, job_id, input))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AgentJobs(bot))
