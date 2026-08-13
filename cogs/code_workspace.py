"""Guarded persistent code workspaces exposed as TweakBot capabilities."""
from __future__ import annotations

import json
import os
import shutil
import stat
import sys
import re
import zipfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import aiohttp
from discord.ext import commands

from cogs.github import GITHUB_API, _GITHUB_TASK_TOKEN, _clean_branch, _clean_repo, _repo_endpoint
from utils.workspace import (
    WORKSPACE_ROOT,
    WorkspaceError,
    git_diff,
    init_baseline_git,
    list_files,
    new_workspace_id,
    read_text,
    replace_text,
    run_guarded,
    safe_path,
    search_text,
    workspace_dir,
    write_text,
)

SOURCE = "code_workspace"
MAX_ARCHIVE_BYTES = max(5_000_000, int(os.getenv("AGENT_WORKSPACE_MAX_ARCHIVE_BYTES", "50000000")))
MAX_EXTRACTED_BYTES = max(20_000_000, int(os.getenv("AGENT_WORKSPACE_MAX_EXTRACTED_BYTES", "150000000")))
MAX_FILES = max(100, int(os.getenv("AGENT_WORKSPACE_MAX_FILES", "5000")))


class CodeWorkspace(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
        if getattr(self.bot, "db", None):
            existing = {
                p.name for p in WORKSPACE_ROOT.iterdir()
                if p.is_dir() and re.fullmatch(r"\d+-[a-f0-9]{12}", p.name)
            }
            await self.bot.db.prune_missing_agent_workspaces(existing)
        r = self.bot.capabilities
        r.register(name="workspace_create", description="Create a guarded persistent workspace from a GitHub repository for editing/testing before any GitHub write.", parameters={"type":"object","properties":{"repo":{"type":"string"},"branch":{"type":"string"}},"required":["repo"]}, handler=self._create, category="workspace", source=SOURCE)
        r.register(name="workspace_files", description="List files in one of the requester's persistent code workspaces.", parameters={"type":"object","properties":{"workspace_id":{"type":"string"}},"required":["workspace_id"]}, handler=self._files, category="workspace", source=SOURCE)
        r.register(name="workspace_read", description="Read a UTF-8 text file from a persistent code workspace.", parameters={"type":"object","properties":{"workspace_id":{"type":"string"},"path":{"type":"string"}},"required":["workspace_id","path"]}, handler=self._read, category="workspace", source=SOURCE)
        r.register(name="workspace_search", description="Search text across a persistent code workspace.", parameters={"type":"object","properties":{"workspace_id":{"type":"string"},"query":{"type":"string"}},"required":["workspace_id","query"]}, handler=self._search, category="workspace", source=SOURCE)
        r.register(name="workspace_replace", description="Apply an exact text replacement to a workspace file. Fails on ambiguous matches unless replace_all=true.", parameters={"type":"object","properties":{"workspace_id":{"type":"string"},"path":{"type":"string"},"old":{"type":"string"},"new":{"type":"string"},"replace_all":{"type":"boolean"}},"required":["workspace_id","path","old","new"]}, handler=self._replace, category="workspace", source=SOURCE, destructive=True)
        r.register(name="workspace_write", description="Create or completely replace a UTF-8 text file inside a guarded workspace.", parameters={"type":"object","properties":{"workspace_id":{"type":"string"},"path":{"type":"string"},"content":{"type":"string"}},"required":["workspace_id","path","content"]}, handler=self._write, category="workspace", source=SOURCE, destructive=True)
        r.register(name="workspace_run_checks", description="Run a guarded known build/test check in a workspace. Supported: auto, python-compile, pytest, npm-test, npm-build, pnpm-test, pnpm-build.", parameters={"type":"object","properties":{"workspace_id":{"type":"string"},"check":{"type":"string"}},"required":["workspace_id"]}, handler=self._checks, category="workspace", source=SOURCE)
        r.register(name="workspace_diff", description="Show the workspace's local Git diff against its downloaded baseline.", parameters={"type":"object","properties":{"workspace_id":{"type":"string"}},"required":["workspace_id"]}, handler=self._diff, category="workspace", source=SOURCE)
        r.register(name="workspace_discard", description="Delete one of the requester's persistent code workspaces.", parameters={"type":"object","properties":{"workspace_id":{"type":"string"}},"required":["workspace_id"]}, handler=self._discard, category="workspace", source=SOURCE, destructive=True)
        r.register(name="workspace_list", description="List the requester's persistent code workspaces.", parameters={"type":"object","properties":{}}, handler=self._list, category="workspace", source=SOURCE)

    async def cog_unload(self) -> None:
        self.bot.capabilities.unregister_source(SOURCE)

    def _root(self, ctx: commands.Context, args: dict[str, Any]) -> Path:
        root = workspace_dir(str(args.get("workspace_id") or ""), ctx.author.id)
        if not root.is_dir():
            raise WorkspaceError("Workspace does not exist (it may have been discarded or lost on a container redeploy).")
        return root

    async def _download_repo_zip(self, ctx: commands.Context, repo: str, branch: str) -> bytes:
        gh = self.bot.get_cog("GitHub")
        if gh is None:
            raise WorkspaceError("GitHub cog is not loaded.")
        token = await gh._user_token(ctx.author.id)
        marker = _GITHUB_TASK_TOKEN.set(token)
        try:
            headers = gh.client._headers(bool(token))
            timeout = aiohttp.ClientTimeout(total=60)
            url = f"{GITHUB_API}{_repo_endpoint(repo)}/zipball/{quote(branch, safe='')}"
            async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status >= 400:
                        text = await response.text()
                        raise WorkspaceError(f"GitHub archive download failed (HTTP {response.status}): {text[:300]}")
                    data = await response.read()
            if len(data) > MAX_ARCHIVE_BYTES:
                raise WorkspaceError(f"Repository archive exceeds {MAX_ARCHIVE_BYTES // 1_000_000} MB workspace limit.")
            return data
        finally:
            _GITHUB_TASK_TOKEN.reset(marker)

    @staticmethod
    def _extract_zip(data: bytes, root: Path) -> int:
        total = 0
        count = 0
        with zipfile.ZipFile(BytesIO(data)) as zf:
            infos = zf.infolist()
            if len(infos) > MAX_FILES + 100:
                raise WorkspaceError("Repository contains too many archive entries for a guarded workspace.")
            roots = set()
            for info in infos:
                p = PurePosixPath(info.filename)
                if p.parts:
                    roots.add(p.parts[0])
            strip = next(iter(roots)) if len(roots) == 1 else None
            for info in infos:
                if info.is_dir():
                    continue
                mode = (info.external_attr >> 16) & 0o170000
                if mode == stat.S_IFLNK:
                    continue
                p = PurePosixPath(info.filename)
                parts = list(p.parts)
                if strip and parts and parts[0] == strip:
                    parts = parts[1:]
                if not parts or any(part in {"", ".", ".."} for part in parts):
                    continue
                if any(part in {".git", ".hg", ".svn"} for part in parts):
                    continue
                rel = PurePosixPath(*parts).as_posix()
                dest = safe_path(root, rel)
                total += int(info.file_size or 0)
                count += 1
                if total > MAX_EXTRACTED_BYTES:
                    raise WorkspaceError("Extracted repository exceeds workspace size limit.")
                if count > MAX_FILES:
                    raise WorkspaceError("Repository exceeds workspace file-count limit.")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(info))
        return count

    async def _create(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        repo = _clean_repo(str(args.get("repo") or ""))
        branch = _clean_branch(str(args.get("branch") or "main"))
        workspace_id = new_workspace_id(ctx.author.id)
        root = workspace_dir(workspace_id, ctx.author.id)
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        try:
            archive = await self._download_repo_zip(ctx, repo, branch)
            count = self._extract_zip(archive, root)
            (root / ".tweakbot-workspace.json").write_text(
                json.dumps({"repo":repo,"branch":branch,"user_id":ctx.author.id}, indent=2),
                encoding="utf-8",
            )
            await init_baseline_git(root)
            if getattr(self.bot, "db", None):
                await self.bot.db.register_agent_workspace(
                    workspace_id, ctx.author.id, ctx.guild.id if ctx.guild else None,
                    repo, branch, str(root),
                )
            return f"Workspace `{workspace_id}` created from {repo}@{branch} with {count} files. It is stored under `{WORKSPACE_ROOT}` and survives restarts/redeploys when that path is a persistent volume."
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise

    async def _files(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        root = self._root(ctx, args)
        files = [p for p in list_files(root, 600) if p != ".tweakbot-workspace.json"]
        return (f"Workspace files ({len(files)} shown):\n" + "\n".join(files))[:20000]

    async def _read(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        root = self._root(ctx, args)
        path = str(args.get("path") or "")
        return (f"FILE {path}\n" + read_text(root, path))[:20000]

    async def _search(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        root = self._root(ctx, args)
        query = str(args.get("query") or "").strip()
        if not query:
            return "A search query is required."
        results = search_text(root, query, 120)
        return (f"Workspace search `{query}`: {len(results)} match(es)\n" + "\n".join(results))[:20000]

    async def _replace(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        root = self._root(ctx, args)
        count = replace_text(
            root,
            str(args.get("path") or ""),
            str(args.get("old") or ""),
            str(args.get("new") or ""),
            bool(args.get("replace_all", False)),
        )
        if getattr(self.bot, "db", None):
            await self.bot.db.touch_agent_workspace(str(args.get("workspace_id") or ""), ctx.author.id)
        return f"Applied {count} replacement(s) in `{args.get('path')}`."

    async def _write(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        root = self._root(ctx, args)
        path = str(args.get("path") or "")
        write_text(root, path, str(args.get("content") or ""))
        if getattr(self.bot, "db", None):
            await self.bot.db.touch_agent_workspace(str(args.get("workspace_id") or ""), ctx.author.id)
        return f"Wrote `{path}`."

    async def _checks(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        root = self._root(ctx, args)
        check = str(args.get("check") or "auto").strip().casefold()
        allowed = {"auto","python-compile","pytest","npm-test","npm-build","pnpm-test","pnpm-build"}
        if check not in allowed:
            return f"Unsupported check `{check}`. Supported: {', '.join(sorted(allowed))}."

        commands_to_run: list[list[str]] = []
        if check == "auto":
            if (root / "requirements.txt").exists() or (root / "pyproject.toml").exists() or any(root.glob("*.py")):
                commands_to_run.append([sys.executable, "-m", "compileall", "-q", "."])
                if (root / "pytest.ini").exists() or (root / "tests").exists():
                    commands_to_run.append([sys.executable, "-m", "pytest", "-q"])
            if (root / "package.json").exists():
                if (root / "pnpm-lock.yaml").exists():
                    commands_to_run.append(["pnpm", "run", "build", "--if-present"])
                else:
                    commands_to_run.append(["npm", "run", "build", "--if-present"])
        elif check == "python-compile": commands_to_run = [[sys.executable, "-m", "compileall", "-q", "."]]
        elif check == "pytest": commands_to_run = [[sys.executable, "-m", "pytest", "-q"]]
        elif check == "npm-test": commands_to_run = [["npm", "test", "--", "--runInBand"]]
        elif check == "npm-build": commands_to_run = [["npm", "run", "build"]]
        elif check == "pnpm-test": commands_to_run = [["pnpm", "test"]]
        elif check == "pnpm-build": commands_to_run = [["pnpm", "run", "build"]]

        if not commands_to_run:
            return "No known guarded checks were detected for this workspace."
        sections: list[str] = []
        all_ok = True
        for argv in commands_to_run:
            code, output = await run_guarded(root, argv)
            all_ok = all_ok and code == 0
            sections.append(f"$ {' '.join(argv)}\nexit={code}\n{output}")
            if code != 0:
                break
        return (("PASS\n" if all_ok else "FAIL\n") + "\n\n".join(sections))[:20000]

    async def _list(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        if not getattr(self.bot, "db", None):
            return "Database is unavailable."
        rows = await self.bot.db.list_agent_workspaces(ctx.author.id, 50)
        if not rows:
            return "No persistent workspaces."
        lines = []
        for row in rows:
            root = Path(str(row["root_path"]))
            state = "available" if root.is_dir() else "missing from persistent storage"
            lines.append(
                f"`{row['workspace_id']}` — {row['repo']}@{row['branch']} · {state}"
            )
        return "Your persistent workspaces:\n" + "\n".join(lines)

    async def _diff(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        root = self._root(ctx, args)
        return (await git_diff(root))[:20000]

    async def _discard(self, ctx: commands.Context, args: dict[str, Any]) -> str:
        root = self._root(ctx, args)
        workspace_id = str(args.get("workspace_id") or "")
        shutil.rmtree(root, ignore_errors=True)
        if getattr(self.bot, "db", None):
            await self.bot.db.discard_agent_workspace(workspace_id, ctx.author.id)
        return f"Discarded workspace `{workspace_id}`."


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CodeWorkspace(bot))
