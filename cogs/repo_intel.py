"""Read-only repository intelligence capabilities for TweakBot."""
from __future__ import annotations

import base64
import json
from pathlib import PurePosixPath
from typing import Any

from discord.ext import commands

from cogs.github import (
    GitHubAPIError,
    _GITHUB_TASK_TOKEN,
    _clean_branch,
    _clean_path,
    _clean_repo,
    _repo_endpoint,
)

SOURCE = "repo_intel"
MAX_TEXT = 18000
MAX_TREE_ITEMS = 400


class RepoIntel(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        r = self.bot.capabilities
        r.register(
            name="repo_inspect",
            description="Inspect a GitHub repository's metadata, language, default branch, and root files.",
            parameters={
                "type":"object",
                "properties":{"repo":{"type":"string"},"branch":{"type":"string"}},
                "required":["repo"],
            },
            handler=self._inspect,
            category="repository",
            source=SOURCE,
        )
        r.register(
            name="repo_tree",
            description="Read a recursive GitHub repository tree for code/navigation analysis.",
            parameters={
                "type":"object",
                "properties":{
                    "repo":{"type":"string"},
                    "branch":{"type":"string"},
                    "path_prefix":{"type":"string"},
                },
                "required":["repo"],
            },
            handler=self._tree,
            category="repository",
            source=SOURCE,
        )
        r.register(
            name="repo_read_file",
            description="Read a UTF-8 text file from a GitHub repository, including private repos linked by OAuth.",
            parameters={
                "type":"object",
                "properties":{
                    "repo":{"type":"string"},
                    "path":{"type":"string"},
                    "branch":{"type":"string"},
                },
                "required":["repo","path"],
            },
            handler=self._read_file,
            category="repository",
            source=SOURCE,
        )
        r.register(
            name="repo_search_code",
            description="Search code inside one GitHub repository and return matching paths/snippets metadata.",
            parameters={
                "type":"object",
                "properties":{"repo":{"type":"string"},"query":{"type":"string"}},
                "required":["repo","query"],
            },
            handler=self._search_code,
            category="repository",
            source=SOURCE,
        )
        r.register(
            name="repo_compare",
            description="Compare two GitHub refs/branches and summarize changed files and commit counts.",
            parameters={
                "type":"object",
                "properties":{
                    "repo":{"type":"string"},
                    "base":{"type":"string"},
                    "head":{"type":"string"},
                },
                "required":["repo","base","head"],
            },
            handler=self._compare,
            category="repository",
            source=SOURCE,
        )
        r.register(
            name="repo_detect_stack",
            description="Detect a repository's likely languages, package managers, runtime, test/build files, and deployment files.",
            parameters={
                "type":"object",
                "properties":{"repo":{"type":"string"},"branch":{"type":"string"}},
                "required":["repo"],
            },
            handler=self._detect_stack,
            category="repository",
            source=SOURCE,
        )

    async def cog_unload(self) -> None:
        self.bot.capabilities.unregister_source(SOURCE)

    async def _github(self, ctx: commands.Context):
        cog = self.bot.get_cog("GitHub")
        if cog is None:
            raise RuntimeError("GitHub cog is not loaded.")
        token = await cog._user_token(ctx.author.id)
        marker = _GITHUB_TASK_TOKEN.set(token)
        return cog, bool(token), marker

    async def _done(self, marker) -> None:
        _GITHUB_TASK_TOKEN.reset(marker)

    async def _inspect(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        repo = _clean_repo(str(args.get("repo") or ""))
        branch_arg = str(args.get("branch") or "").strip()
        cog, authed, marker = await self._github(ctx)
        try:
            meta = await cog.client.get_repo(repo, authenticated=authed)
            branch = _clean_branch(branch_arg or str(meta.get("default_branch") or "main"))
            root = await cog.client.list_files(repo, "", branch, authenticated=authed)
            root_items = root if isinstance(root, list) else [root]
            files = [
                f"{item.get('type','?'):4} {item.get('path') or item.get('name')}"
                for item in root_items[:80]
                if isinstance(item, dict)
            ]
            data = {
                "repo": meta.get("full_name") or repo,
                "private": bool(meta.get("private")),
                "default_branch": meta.get("default_branch"),
                "language": meta.get("language"),
                "size_kb": meta.get("size"),
                "archived": bool(meta.get("archived")),
                "description": meta.get("description"),
                "branch_inspected": branch,
            }
            return json.dumps(data, ensure_ascii=False, indent=2) + "\nRoot:\n" + "\n".join(files)
        finally:
            await self._done(marker)

    async def _tree(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        repo = _clean_repo(str(args.get("repo") or ""))
        branch = _clean_branch(str(args.get("branch") or "main"))
        prefix = _clean_path(str(args.get("path_prefix") or "")) if args.get("path_prefix") else ""
        cog, authed, marker = await self._github(ctx)
        try:
            endpoint = f"{_repo_endpoint(repo)}/git/trees/{branch}"
            data = await cog.client.request(
                "GET", endpoint, params={"recursive": 1}, authenticated=authed
            )
            tree = data.get("tree") if isinstance(data, dict) else []
            lines: list[str] = []
            for item in tree or []:
                if not isinstance(item, dict):
                    continue
                path = str(item.get("path") or "")
                if prefix and not (path == prefix or path.startswith(prefix.rstrip("/") + "/")):
                    continue
                lines.append(f"{item.get('type','?'):4} {path} {item.get('size','')}")
                if len(lines) >= MAX_TREE_ITEMS:
                    break
            truncated = bool(isinstance(data, dict) and data.get("truncated")) or len(lines) >= MAX_TREE_ITEMS
            header = f"Tree {repo}@{branch}" + (" (truncated)" if truncated else "")
            return (header + "\n" + "\n".join(lines))[:MAX_TEXT]
        finally:
            await self._done(marker)

    async def _read_file(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        repo = _clean_repo(str(args.get("repo") or ""))
        path = _clean_path(str(args.get("path") or ""))
        branch = _clean_branch(str(args.get("branch") or "main"))
        cog, authed, marker = await self._github(ctx)
        try:
            data = await cog.client.get_file(repo, path, branch, authenticated=authed)
            encoded = data.get("content") if isinstance(data, dict) else None
            if not encoded or data.get("encoding") != "base64":
                return "GitHub did not return inline file content; the file may be too large or non-file content."
            raw = base64.b64decode(encoded)
            if b"\x00" in raw:
                return f"{path} is binary ({len(raw)} bytes)."
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return f"{path} is not UTF-8 text ({len(raw)} bytes)."
            header = f"FILE {repo}@{branch}:{path}\nSHA {data.get('sha')}\n"
            return (header + text)[:MAX_TEXT]
        finally:
            await self._done(marker)

    async def _search_code(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        repo = _clean_repo(str(args.get("repo") or ""))
        query = str(args.get("query") or "").strip()
        if not query:
            return "A code-search query is required."
        cog, authed, marker = await self._github(ctx)
        try:
            if not authed:
                return "GitHub code search requires a linked GitHub account."
            data = await cog.client.request(
                "GET",
                "/search/code",
                params={"q": f"{query} repo:{repo}", "per_page": 30},
                authenticated=True,
            )
            items = data.get("items") if isinstance(data, dict) else []
            lines = [
                f"{item.get('path')} — {item.get('html_url')}"
                for item in items or []
                if isinstance(item, dict)
            ]
            return (f"Code search `{query}` in {repo}: {len(lines)} match(es)\n" + "\n".join(lines))[:MAX_TEXT]
        finally:
            await self._done(marker)

    async def _compare(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        repo = _clean_repo(str(args.get("repo") or ""))
        base = _clean_branch(str(args.get("base") or ""))
        head = _clean_branch(str(args.get("head") or ""))
        cog, authed, marker = await self._github(ctx)
        try:
            data = await cog.client.compare(repo, base, head, authenticated=authed)
            files = data.get("files") if isinstance(data, dict) else []
            lines = [
                f"{f.get('status','?')} {f.get('filename')} +{f.get('additions',0)} -{f.get('deletions',0)}"
                for f in (files or [])[:100]
                if isinstance(f, dict)
            ]
            return (
                f"{repo} {base}...{head}: status={data.get('status')} ahead={data.get('ahead_by')} "
                f"behind={data.get('behind_by')} commits={data.get('total_commits')}\n"
                + "\n".join(lines)
            )[:MAX_TEXT]
        finally:
            await self._done(marker)

    async def _detect_stack(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        repo = _clean_repo(str(args.get("repo") or ""))
        branch = _clean_branch(str(args.get("branch") or "main"))
        cog, authed, marker = await self._github(ctx)
        try:
            data = await cog.client.request(
                "GET",
                f"{_repo_endpoint(repo)}/git/trees/{branch}",
                params={"recursive": 1},
                authenticated=authed,
            )
            tree = data.get("tree") if isinstance(data, dict) else []
            paths = {str(item.get("path") or "") for item in tree or [] if isinstance(item, dict)}
            basenames = {PurePosixPath(path).name for path in paths}
            evidence: dict[str, list[str]] = {}

            def hit(label: str, candidates: set[str]):
                found = sorted(candidate for candidate in candidates if candidate in paths or candidate in basenames)
                if found:
                    evidence[label] = found[:20]

            hit("python", {"requirements.txt","pyproject.toml","setup.py","Pipfile","poetry.lock"})
            hit("node", {"package.json","pnpm-lock.yaml","yarn.lock","package-lock.json","bun.lockb"})
            hit("rust", {"Cargo.toml","Cargo.lock"})
            hit("go", {"go.mod","go.sum"})
            hit("java", {"pom.xml","build.gradle","build.gradle.kts"})
            hit("dotnet", {"global.json"})
            hit("docker", {"Dockerfile","docker-compose.yml","compose.yml"})
            hit("railway", {"railway.json","railway.toml","nixpacks.toml"})
            hit("github_actions", {path for path in paths if path.startswith(".github/workflows/")})
            hit("tests", {path for path in paths if path.startswith(("tests/","test/","__tests__/")) or PurePosixPath(path).name.startswith("test_")})
            return json.dumps({"repo":repo,"branch":branch,"evidence":evidence}, ensure_ascii=False, indent=2)[:MAX_TEXT]
        finally:
            await self._done(marker)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RepoIntel(bot))
