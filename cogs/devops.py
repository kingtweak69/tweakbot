"""High-level GitHub ↔ Railway DevOps capabilities for TweakBot."""
from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from discord.ext import commands

import config
from cogs.github import _GITHUB_TASK_TOKEN, _clean_branch, _clean_repo
from utils.workspace import WorkspaceError, run_guarded, safe_path, workspace_dir

SOURCE = "devops"
MAX_LOG_LINES = 120
MAX_COMMIT_FILE_BYTES = 8_000_000


class DevOps(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        r = self.bot.capabilities
        r.register(
            name="railway_status_read",
            description="Return machine-readable status/config/source for the requester's selected Railway service using their linked Railway OAuth.",
            parameters={"type":"object","properties":{}},
            handler=self._railway_status,
            category="devops",
            source=SOURCE,
        )
        r.register(
            name="railway_logs_read",
            description="Read runtime or build logs for the requester's selected Railway deployment so the agent can diagnose failures.",
            parameters={"type":"object","properties":{"kind":{"type":"string"},"limit":{"type":"integer","minimum":1,"maximum":120}}},
            handler=self._railway_logs,
            category="devops",
            source=SOURCE,
        )
        r.register(
            name="railway_variable_keys",
            description="List Railway variable names for the selected service without exposing their values.",
            parameters={"type":"object","properties":{}},
            handler=self._railway_variable_keys,
            category="devops",
            source=SOURCE,
        )
        r.register(
            name="workspace_commit_github",
            description="Commit all tested changes from a guarded workspace back to its source GitHub branch using the requester's existing GitHub OAuth.",
            parameters={"type":"object","properties":{"workspace_id":{"type":"string"},"message":{"type":"string"}},"required":["workspace_id","message"]},
            handler=self._workspace_commit,
            category="devops",
            source=SOURCE,
            destructive=True,
        )
        r.register(
            name="devops_deploy_repo",
            description=(
                "Connect/deploy a GitHub repository on Railway using existing TweakBot commands and the requester's OAuth. "
                "Can use the current Railway target or optionally select/create a project/service."
            ),
            parameters={
                "type":"object",
                "properties":{
                    "repo":{"type":"string"},
                    "branch":{"type":"string"},
                    "project":{"type":"string"},
                    "create_project":{"type":"boolean"},
                    "service":{"type":"string"},
                    "create_service":{"type":"boolean"},
                },
                "required":["repo"],
            },
            handler=self._deploy_repo,
            category="devops",
            source=SOURCE,
            destructive=True,
        )
        r.register(
            name="devops_ship_workspace",
            description="Commit a guarded workspace to GitHub, then deploy that exact commit SHA to the currently selected Railway service.",
            parameters={"type":"object","properties":{"workspace_id":{"type":"string"},"message":{"type":"string"}},"required":["workspace_id","message"]},
            handler=self._ship_workspace,
            category="devops",
            source=SOURCE,
            destructive=True,
        )
        r.register(
            name="devops_diagnose_deployment",
            description="Collect Railway status plus recent runtime/build logs for diagnosis without changing anything.",
            parameters={"type":"object","properties":{"log_lines":{"type":"integer","minimum":1,"maximum":120}}},
            handler=self._diagnose,
            category="devops",
            source=SOURCE,
        )

    async def cog_unload(self) -> None:
        self.bot.capabilities.unregister_source(SOURCE)

    def _railway(self):
        cog = self.bot.get_cog("Railway")
        if cog is None:
            raise RuntimeError("Railway cog is not loaded.")
        return cog

    async def _railway_status(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        rw = self._railway()
        client = await rw._client(ctx)
        target = await rw._target(ctx, service=True)
        instance = await rw._service_instance(client, target)
        latest = instance.get("latestDeployment") or await rw._latest_deployment(client, target)
        result = {
            "project": {"id": target.project_id, "name": target.project_name},
            "environment": {"id": target.environment_id, "name": target.environment_name},
            "service": {"id": target.service_id, "name": target.service_name or instance.get("serviceName")},
            "source": await rw._service_source(client, target),
            "deployment": latest,
            "buildCommand": instance.get("buildCommand"),
            "startCommand": instance.get("startCommand"),
            "rootDirectory": instance.get("rootDirectory"),
            "dockerfilePath": instance.get("dockerfilePath"),
            "healthcheckPath": instance.get("healthcheckPath"),
            "region": instance.get("region"),
            "numReplicas": instance.get("numReplicas"),
            "restartPolicyType": instance.get("restartPolicyType"),
        }
        return json.dumps(result, ensure_ascii=False, indent=2)[:18000]

    async def _railway_logs(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        rw = self._railway()
        kind = str(args.get("kind") or "run").strip().casefold()
        build = kind in {"build", "builds"}
        if kind not in {"run", "runtime", "deploy", "build", "builds"}:
            return "Log kind must be `run` or `build`."
        limit = max(1, min(int(args.get("limit") or 40), MAX_LOG_LINES))
        client = await rw._client(ctx)
        target = await rw._target(ctx, service=True)
        deployment_id = str((await rw._latest_deployment(client, target))["id"])
        field = "buildLogs" if build else "deploymentLogs"
        data = await client.graphql(
            f"""
            query Logs($deploymentId: String!, $limit: Int) {{
              {field}(deploymentId: $deploymentId, limit: $limit) {{
                timestamp message severity
              }}
            }}
            """,
            {"deploymentId": deployment_id, "limit": limit},
        )
        entries = data.get(field) or []
        lines = [
            f"{entry.get('timestamp','')} [{entry.get('severity','')}] {str(entry.get('message') or '').rstrip()}"
            for entry in entries
            if isinstance(entry, dict)
        ]
        body = f"{field} deployment={deployment_id}\n" + "\n".join(lines)
        body = await rw._redact_log_secrets(client, target, body)
        return body[-20000:]

    async def _railway_variable_keys(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        rw = self._railway()
        client = await rw._client(ctx)
        target = await rw._target(ctx, service=True)
        variables = await rw._variables(client, target, unrendered=True)
        keys = sorted(str(k) for k in variables.keys())
        return "Railway variable keys (values intentionally hidden):\n" + "\n".join(keys)

    async def _workspace_changes(self, root: Path) -> list[tuple[str, bytes | None, str]]:
        code, tracked = await run_guarded(
            root, ["git", "diff", "--no-renames", "--name-status", "HEAD", "--", "."], timeout=30
        )
        if code != 0:
            raise WorkspaceError(tracked or "Could not inspect tracked workspace changes.")
        code, untracked = await run_guarded(
            root, ["git", "ls-files", "--others", "--exclude-standard"], timeout=30
        )
        if code != 0:
            raise WorkspaceError(untracked or "Could not inspect untracked workspace files.")

        states: dict[str, str] = {}
        for line in tracked.splitlines():
            if not line.strip() or "\t" not in line:
                continue
            status_code, path = line.split("\t", 1)
            states[path.strip()] = "D" if status_code.startswith("D") else "M"
        for path in untracked.splitlines():
            path = path.strip()
            if path:
                states[path] = "A"

        files: list[tuple[str, bytes | None, str]] = []
        for path, state_code in sorted(states.items()):
            if path == ".tweakbot-workspace.json" or path.startswith((".home/", ".tmp/", ".git/")):
                continue
            if state_code == "D":
                files.append((path, None, "100644"))
                continue
            file_path = safe_path(root, path)
            if not file_path.is_file():
                continue
            if file_path.stat().st_size > MAX_COMMIT_FILE_BYTES:
                raise WorkspaceError(f"Changed file `{path}` exceeds the {MAX_COMMIT_FILE_BYTES // 1_000_000} MB commit limit.")
            mode = "100755" if file_path.stat().st_mode & stat.S_IXUSR else "100644"
            files.append((path, file_path.read_bytes(), mode))
        return files

    async def _workspace_commit_impl(self, ctx: commands.Context, workspace_id: str, message: str) -> tuple[str, str, str]:
        root = workspace_dir(workspace_id, ctx.author.id)
        if not root.is_dir():
            raise WorkspaceError("Workspace does not exist.")
        metadata_path = root / ".tweakbot-workspace.json"
        if not metadata_path.is_file():
            raise WorkspaceError("Workspace metadata is missing.")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        repo = _clean_repo(str(metadata.get("repo") or ""))
        branch = _clean_branch(str(metadata.get("branch") or "main"))
        message = str(message or "").strip()
        if not message:
            raise WorkspaceError("Commit message is required.")
        if branch in config.GITHUB_PROTECTED_BRANCHES and not config.GITHUB_ALLOW_PROTECTED_BRANCH_COMMITS:
            raise WorkspaceError(f"Direct commits to protected branch `{branch}` are disabled by TweakBot config.")
        files = await self._workspace_changes(root)
        if not files:
            raise WorkspaceError("Workspace has no changes to commit.")

        gh = self.bot.get_cog("GitHub")
        if gh is None:
            raise WorkspaceError("GitHub cog is not loaded.")
        token = await gh._user_token(ctx.author.id)
        if not token:
            raise WorkspaceError("Link GitHub first with `gh login`.")
        marker = _GITHUB_TASK_TOKEN.set(token)
        try:
            commit = await gh.client.commit_files(repo, branch, message, files)
        finally:
            _GITHUB_TASK_TOKEN.reset(marker)
        sha = str(commit.get("sha") or "")
        if not sha:
            raise WorkspaceError("GitHub did not return the new commit SHA.")
        return repo, branch, sha

    async def _workspace_commit(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        repo, branch, sha = await self._workspace_commit_impl(
            ctx, str(args.get("workspace_id") or ""), str(args.get("message") or "")
        )
        return f"Committed workspace to `{repo}@{branch}` as `{sha}`."

    async def _run_command(self, ctx: commands.Context, command: str) -> str:
        result = await self.bot.capabilities.execute(ctx, "run_bot_command", {"command": command})
        lowered = result.casefold()
        if " ran but failed" in lowered or lowered.startswith("unknown tweakbot command") or "command bridge failed" in lowered:
            raise RuntimeError(result)
        return result

    async def _deploy_repo(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        repo = _clean_repo(str(args.get("repo") or ""))
        branch = _clean_branch(str(args.get("branch") or "main"))
        project = str(args.get("project") or "").strip()
        service = str(args.get("service") or "").strip()
        create_project = bool(args.get("create_project", False))
        create_service = bool(args.get("create_service", False))
        steps: list[str] = []

        # Verify repository access/readability before touching Railway.
        inspect_result = await self.bot.capabilities.execute(ctx, "repo_inspect", {"repo":repo,"branch":branch})
        if inspect_result.startswith("Unknown capability") or "failed:" in inspect_result.casefold():
            raise RuntimeError(inspect_result)
        steps.append("Repository verified.")

        if project:
            cmd = f"railway project create {project}" if create_project else f"railway select {project}"
            await self._run_command(ctx, cmd)
            steps.append(f"Railway project {'created' if create_project else 'selected'}: {project}")

        if service:
            if create_service:
                await self._run_command(ctx, f"railway service create-github {service} {repo} {branch}")
                steps.append(f"Railway GitHub service created: {service}")
            else:
                await self._run_command(ctx, f"railway service select {service}")
                await self._run_command(ctx, f"railway service connect-repo {repo} {branch}")
                steps.append(f"Railway service selected/connected: {service}")
        else:
            await self._run_command(ctx, f"railway service connect-repo {repo} {branch}")
            steps.append("Current Railway service connected to repository.")

        await self._run_command(ctx, "railway deploy")
        steps.append("Deployment requested.")
        status = await self._railway_status(ctx, {})
        return "\n".join(steps) + "\n\nCurrent status:\n" + status

    async def _ship_workspace(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        repo, branch, sha = await self._workspace_commit_impl(
            ctx, str(args.get("workspace_id") or ""), str(args.get("message") or "")
        )
        await self._run_command(ctx, f"railway deploy {sha}")
        return (
            f"Committed `{repo}@{branch}` as `{sha}` and requested Railway deployment of that exact commit.\n"
            + await self._railway_status(ctx, {})
        )

    async def _diagnose(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        limit = max(1, min(int(args.get("log_lines") or 50), MAX_LOG_LINES))
        status = await self._railway_status(ctx, {})
        runtime = await self._railway_logs(ctx, {"kind":"run","limit":limit})
        try:
            build = await self._railway_logs(ctx, {"kind":"build","limit":limit})
        except Exception as exc:
            build = f"Build logs unavailable: {type(exc).__name__}: {exc}"
        return ("RAILWAY STATUS\n" + status + "\n\nRUNTIME LOGS\n" + runtime + "\n\nBUILD LOGS\n" + build)[-30000:]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(DevOps(bot))
