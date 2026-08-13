"""
GitHub integration for TweakBot.

Public browsing works without authentication. Private repository reads and all
write operations use the Discord caller's linked GitHub OAuth token.
"""

import asyncio
import base64
import contextvars
import io
import json
import logging
import re
import time
import zipfile
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote

import aiohttp
import discord
from discord.ext import commands

import config
from utils.credentials import CredentialVault
from utils.helpers import error_embed, info_embed, success_embed


log = logging.getLogger("cogs.github")

GITHUB_API = "https://api.github.com"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)
MAX_LIST_ITEMS = 25
MAX_COMMIT_BYTES = 900_000
MAX_ZIP_BYTES = 20_000_000
MAX_ZIP_FILES = 250
MAX_ZIP_FILE_BYTES = 8_000_000
MAX_ZIP_TOTAL_BYTES = 40_000_000
MAX_ZIP_COMPRESSION_RATIO = 150
GITHUB_OAUTH_SCOPES = "repo workflow read:user delete_repo"

_GITHUB_TASK_TOKEN: contextvars.ContextVar[str] = contextvars.ContextVar(
    "github_task_token", default=""
)
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
REPO_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
USERNAME_RE = re.compile(r"^[A-Za-z0-9-]{1,39}$")


class GitHubAPIError(RuntimeError):
    """A clean, user-facing GitHub API error."""

    def __init__(self, status: int, message: str):
        self.status = status
        self.message = message
        super().__init__(f"GitHub returned {status}: {message}")


def _truncate(value: Any, length: int) -> str:
    text = str(value or "")
    return text if len(text) <= length else f"{text[:length - 1]}…"


def _repo_endpoint(repo: str) -> str:
    repo = repo.strip().strip("/")
    if not REPO_RE.fullmatch(repo):
        raise ValueError("Repository must be written as `owner/repo`.")
    owner, name = repo.split("/", 1)
    return f"/repos/{quote(owner, safe='')}/{quote(name, safe='')}"


def _clean_repo(repo: str) -> str:
    _repo_endpoint(repo)
    return repo.strip().strip("/")


def _clean_repo_name(name: str) -> str:
    name = name.strip()
    if (
        not REPO_NAME_RE.fullmatch(name)
        or name in {".", ".."}
        or name.lower().endswith(".git")
    ):
        raise ValueError(
            "Repository name must be 1–100 characters and contain only "
            "letters, numbers, `.`, `_`, or `-`."
        )
    return name


def _clean_path(path: str) -> str:
    path = path.strip().strip("/")
    if not path:
        return ""
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise ValueError("Path cannot contain empty, `.` or `..` segments.")
    return path


def _clean_destination(path: str) -> str:
    path = path.strip()
    if path in {"", ".", "/"}:
        return ""
    return _clean_path(path)


def _clean_branch(branch: str) -> str:
    branch = branch.strip()
    invalid_chars = {" ", "~", "^", ":", "?", "*", "[", "\\"}
    if (
        not branch
        or len(branch) > 255
        or branch.startswith(("/", ".", "-"))
        or branch.endswith(("/", ".", ".lock"))
        or ".." in branch
        or "@{" in branch
        or any(character in branch for character in invalid_chars)
        or any(ord(character) < 32 for character in branch)
    ):
        raise ValueError("Branch name is invalid.")
    return branch


def _clean_tag(tag: str) -> str:
    tag = tag.strip()
    if not tag or len(tag) > 255 or any(ord(character) < 32 for character in tag):
        raise ValueError("Release tag is invalid.")
    return tag


def _clean_username(username: str) -> str:
    username = username.strip().lstrip("@")
    if not USERNAME_RE.fullmatch(username):
        raise ValueError("GitHub username is invalid.")
    return username


def _safe_archive_path(name: str) -> PurePosixPath:
    if not name or "\x00" in name or "\\" in name:
        raise ValueError("ZIP contains an invalid file path.")
    candidate = PurePosixPath(name)
    if candidate.is_absolute() or any(
        part in {"", ".", ".."} for part in candidate.parts
    ):
        raise ValueError(f"Unsafe ZIP path: `{_truncate(name, 120)}`.")
    if candidate.parts and ":" in candidate.parts[0]:
        raise ValueError(f"Unsafe ZIP path: `{_truncate(name, 120)}`.")
    return candidate


def _read_zip_entries(
    archive: bytes,
    destination: str,
    *,
    strip_root: bool,
) -> list[tuple[str, bytes, str]]:
    """Safely extract ZIP members into Git tree entries."""
    if not archive:
        raise ValueError("The attached ZIP file is empty.")
    if len(archive) > MAX_ZIP_BYTES:
        raise ValueError(
            f"ZIP attachment exceeds the {MAX_ZIP_BYTES // 1_000_000} MB limit."
        )

    destination = _clean_destination(destination)
    prepared: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
    total_size = 0

    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            for info in bundle.infolist():
                if info.is_dir():
                    continue
                if info.flag_bits & 0x1:
                    raise ValueError("Password-protected ZIP files are not supported.")

                member = _safe_archive_path(info.filename)
                if member.parts[0] == "__MACOSX" or member.name == ".DS_Store":
                    continue
                # Never try to commit embedded VCS metadata from uploaded archives.
                # GitHub rejects paths such as `.git/objects/...` as malformed tree paths.
                if any(part in {".git", ".hg", ".svn"} for part in member.parts):
                    continue

                unix_type = (info.external_attr >> 16) & 0o170000
                if unix_type == 0o120000:
                    raise ValueError(
                        f"ZIP symlinks are not allowed: `{_truncate(info.filename, 120)}`."
                    )
                if info.file_size > MAX_ZIP_FILE_BYTES:
                    raise ValueError(
                        f"`{_truncate(info.filename, 120)}` exceeds the "
                        f"{MAX_ZIP_FILE_BYTES // 1_000_000} MB per-file limit."
                    )

                total_size += info.file_size
                if total_size > MAX_ZIP_TOTAL_BYTES:
                    raise ValueError(
                        f"Extracted ZIP contents exceed the "
                        f"{MAX_ZIP_TOTAL_BYTES // 1_000_000} MB limit."
                    )
                if info.file_size and info.compress_size == 0:
                    raise ValueError(
                        f"Suspicious ZIP member: `{_truncate(info.filename, 120)}`."
                    )
                if (
                    info.compress_size
                    and info.file_size / info.compress_size
                    > MAX_ZIP_COMPRESSION_RATIO
                ):
                    raise ValueError(
                        f"Suspicious compression ratio in "
                        f"`{_truncate(info.filename, 120)}`."
                    )

                prepared.append((info, member))
                if len(prepared) > MAX_ZIP_FILES:
                    raise ValueError(f"ZIP contains more than {MAX_ZIP_FILES} files.")

            if not prepared:
                raise ValueError("ZIP contains no usable files.")

            root_component: str | None = None
            if strip_root:
                first_parts = {member.parts[0] for _, member in prepared}
                if len(first_parts) == 1 and all(
                    len(member.parts) > 1 for _, member in prepared
                ):
                    root_component = next(iter(first_parts))
                else:
                    raise ValueError(
                        "`--strip-root` requires every file to be inside one "
                        "shared top-level folder."
                    )

            entries: list[tuple[str, bytes, str]] = []
            seen: set[str] = set()
            for info, member in prepared:
                relative_parts = member.parts[1:] if root_component else member.parts
                relative = PurePosixPath(*relative_parts)
                target = PurePosixPath(destination, relative) if destination else relative
                target_path = _clean_path(target.as_posix())
                if target_path in seen:
                    raise ValueError(
                        f"ZIP creates the same repository path twice: `{target_path}`."
                    )
                seen.add(target_path)

                content = bundle.read(info)
                if len(content) != info.file_size:
                    raise ValueError(
                        f"Could not fully read `{_truncate(info.filename, 120)}`."
                    )
                unix_mode = (info.external_attr >> 16) & 0o777
                git_mode = "100755" if unix_mode & 0o111 else "100644"
                entries.append((target_path, content, git_mode))

            return entries
    except zipfile.BadZipFile as exc:
        raise ValueError("The attachment is not a valid ZIP file.") from exc


def _language_for(path: str) -> str:
    suffix = PurePosixPath(path).suffix.lower()
    return {
        ".py": "py", ".js": "js", ".ts": "ts", ".tsx": "tsx",
        ".json": "json", ".md": "md", ".yml": "yaml", ".yaml": "yaml",
        ".toml": "toml", ".sh": "bash", ".html": "html", ".css": "css",
        ".rs": "rs", ".go": "go", ".java": "java", ".cpp": "cpp",
        ".c": "c", ".sql": "sql",
    }.get(suffix, "")


class GitHubClient:
    """Small async wrapper around the GitHub REST API."""

    def __init__(self, token: str):
        self.token = token.strip()

    def _headers(self, authenticated: bool) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "TweakBot/2.0",
        }
        if authenticated:
            token = _GITHUB_TASK_TOKEN.get() or self.token
            if not token:
                raise GitHubAPIError(401, "Link GitHub first with `gh login`.")
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def request(
        self,
        method: str,
        endpoint: str,
        *,
        params: dict[str, str | int] | None = None,
        payload: dict[str, Any] | None = None,
        authenticated: bool = False,
    ) -> Any:
        try:
            async with aiohttp.ClientSession(
                timeout=REQUEST_TIMEOUT,
                headers=self._headers(authenticated),
            ) as session:
                async with session.request(
                    method,
                    f"{GITHUB_API}{endpoint}",
                    params=params,
                    json=payload,
                ) as response:
                    raw = await response.text()
                    try:
                        data = json.loads(raw) if raw else {}
                    except json.JSONDecodeError:
                        data = raw

                    if response.status >= 400:
                        message = data.get("message") if isinstance(data, dict) else raw
                        if (
                            response.status == 403
                            and response.headers.get("X-RateLimit-Remaining") == "0"
                        ):
                            message = "GitHub API rate limit reached. Try again later."
                        raise GitHubAPIError(
                            response.status,
                            _truncate(message or "Request failed.", 300),
                        )
                    return data
        except GitHubAPIError:
            raise
        except asyncio.TimeoutError as exc:
            raise GitHubAPIError(504, "GitHub did not respond in time.") from exc
        except aiohttp.ClientError as exc:
            raise GitHubAPIError(503, "Could not reach GitHub.") from exc

    async def get_authenticated_user(self) -> dict[str, Any]:
        return await self.request("GET", "/user", authenticated=True)

    async def list_public_repos(self, username: str) -> list[dict[str, Any]]:
        username = _clean_username(username)
        return await self.request(
            "GET",
            f"/users/{quote(username, safe='')}/repos",
            params={"per_page": MAX_LIST_ITEMS, "sort": "updated", "type": "owner"},
        )

    async def list_my_repos(self) -> list[dict[str, Any]]:
        return await self.request(
            "GET",
            "/user/repos",
            params={
                "per_page": MAX_LIST_ITEMS,
                "sort": "updated",
                "affiliation": "owner,collaborator,organization_member",
            },
            authenticated=True,
        )

    async def get_repo(self, repo: str, *, authenticated: bool = False) -> dict[str, Any]:
        return await self.request("GET", _repo_endpoint(repo), authenticated=authenticated)

    async def list_files(
        self,
        repo: str,
        path: str = "",
        branch: str = "main",
        *,
        authenticated: bool = False,
    ) -> list[dict[str, Any]] | dict[str, Any]:
        path = _clean_path(path)
        branch = _clean_branch(branch)
        endpoint = f"{_repo_endpoint(repo)}/contents"
        if path:
            endpoint += f"/{quote(path, safe='/')}"
        return await self.request(
            "GET", endpoint, params={"ref": branch}, authenticated=authenticated
        )

    async def get_file(
        self,
        repo: str,
        path: str,
        branch: str = "main",
        *,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        data = await self.list_files(repo, path, branch, authenticated=authenticated)
        if isinstance(data, list):
            raise GitHubAPIError(400, "That path is a directory. Use `github files` instead.")
        return data

    async def create_branch(self, repo: str, new_branch: str, source_branch: str) -> dict[str, Any]:
        repo = _clean_repo(repo)
        new_branch = _clean_branch(new_branch)
        source_branch = _clean_branch(source_branch)
        source = await self.request(
            "GET",
            f"{_repo_endpoint(repo)}/git/ref/heads/{quote(source_branch, safe='')}",
            authenticated=True,
        )
        source_sha = source.get("object", {}).get("sha")
        if not source_sha:
            raise GitHubAPIError(422, f"GitHub did not return a commit SHA for `{source_branch}`.")
        return await self.request(
            "POST",
            f"{_repo_endpoint(repo)}/git/refs",
            payload={"ref": f"refs/heads/{new_branch}", "sha": source_sha},
            authenticated=True,
        )

    async def compare(
        self,
        repo: str,
        base: str,
        head: str,
        *,
        authenticated: bool = False,
    ) -> dict[str, Any]:
        repo = _clean_repo(repo)
        base = _clean_branch(base)
        head = _clean_branch(head)
        return await self.request(
            "GET",
            f"{_repo_endpoint(repo)}/compare/{quote(base, safe='')}...{quote(head, safe='')}",
            authenticated=authenticated,
        )

    async def list_issues(
        self,
        repo: str,
        state: str = "open",
        *,
        authenticated: bool = False,
    ) -> list[dict[str, Any]]:
        if state not in {"open", "closed", "all"}:
            raise ValueError("State must be `open`, `closed`, or `all`.")
        return await self.request(
            "GET",
            f"{_repo_endpoint(repo)}/issues",
            params={"state": state, "per_page": 10, "sort": "updated"},
            authenticated=authenticated,
        )

    async def list_pull_requests(
        self,
        repo: str,
        state: str = "open",
        *,
        authenticated: bool = False,
    ) -> list[dict[str, Any]]:
        if state not in {"open", "closed", "all"}:
            raise ValueError("State must be `open`, `closed`, or `all`.")
        return await self.request(
            "GET",
            f"{_repo_endpoint(repo)}/pulls",
            params={"state": state, "per_page": 10, "sort": "updated", "direction": "desc"},
            authenticated=authenticated,
        )

    async def upsert_file(
        self,
        repo: str,
        path: str,
        branch: str,
        message: str,
        content: bytes,
    ) -> tuple[dict[str, Any], bool]:
        repo = _clean_repo(repo)
        path = _clean_path(path)
        branch = _clean_branch(branch)
        if not path:
            raise ValueError("A file path is required.")
        if not message.strip():
            raise ValueError("Commit message cannot be empty.")
        if len(content) > MAX_COMMIT_BYTES:
            raise ValueError("Attachment is too large for the GitHub Contents API (max 900 KB).")

        existing_sha = None
        created = True
        try:
            current = await self.get_file(repo, path, branch, authenticated=True)
            existing_sha = current.get("sha")
            created = False
        except GitHubAPIError as exc:
            if exc.status != 404:
                raise

        payload: dict[str, Any] = {
            "message": _truncate(message.strip(), 250),
            "content": base64.b64encode(content).decode("ascii"),
            "branch": branch,
        }
        if existing_sha:
            payload["sha"] = existing_sha

        result = await self.request(
            "PUT",
            f"{_repo_endpoint(repo)}/contents/{quote(path, safe='/')}",
            payload=payload,
            authenticated=True,
        )
        return result, created

    async def commit_files(
        self,
        repo: str,
        branch: str,
        message: str,
        files: list[tuple[str, bytes | None, str]],
    ) -> dict[str, Any]:
        """Commit multiple files atomically through GitHub's Git Data API."""
        repo = _clean_repo(repo)
        branch = _clean_branch(branch)
        message = message.strip()
        if not message:
            raise ValueError("Commit message cannot be empty.")
        if not files:
            raise ValueError("No files were supplied for the commit.")

        ref = await self.request(
            "GET",
            f"{_repo_endpoint(repo)}/git/ref/heads/{quote(branch, safe='')}",
            authenticated=True,
        )
        parent_sha = ref.get("object", {}).get("sha")
        if not parent_sha:
            raise GitHubAPIError(422, f"GitHub did not return the head commit for `{branch}`.")

        parent_commit = await self.request(
            "GET",
            f"{_repo_endpoint(repo)}/git/commits/{quote(parent_sha, safe='')}",
            authenticated=True,
        )
        base_tree_sha = parent_commit.get("tree", {}).get("sha")
        if not base_tree_sha:
            raise GitHubAPIError(422, "GitHub did not return the branch tree SHA.")

        semaphore = asyncio.Semaphore(4)

        async def create_blob(file_path: str, content: bytes | None, mode: str) -> dict[str, Any]:
            if content is None:
                # Git Data API deletes a path when a base-tree entry has sha=null.
                return {"path": file_path, "mode": mode, "type": "blob", "sha": None}
            async with semaphore:
                blob = await self.request(
                    "POST",
                    f"{_repo_endpoint(repo)}/git/blobs",
                    payload={
                        "content": base64.b64encode(content).decode("ascii"),
                        "encoding": "base64",
                    },
                    authenticated=True,
                )
            blob_sha = blob.get("sha")
            if not blob_sha:
                raise GitHubAPIError(422, f"GitHub did not create a blob for `{file_path}`.")
            return {"path": file_path, "mode": mode, "type": "blob", "sha": blob_sha}

        tree_entries = await asyncio.gather(
            *(create_blob(file_path, content, mode) for file_path, content, mode in files)
        )
        tree = await self.request(
            "POST",
            f"{_repo_endpoint(repo)}/git/trees",
            payload={"base_tree": base_tree_sha, "tree": tree_entries},
            authenticated=True,
        )
        tree_sha = tree.get("sha")
        if not tree_sha:
            raise GitHubAPIError(422, "GitHub did not create the replacement tree.")

        commit = await self.request(
            "POST",
            f"{_repo_endpoint(repo)}/git/commits",
            payload={"message": _truncate(message, 250), "tree": tree_sha, "parents": [parent_sha]},
            authenticated=True,
        )
        commit_sha = commit.get("sha")
        if not commit_sha:
            raise GitHubAPIError(422, "GitHub did not create the commit.")

        await self.request(
            "PATCH",
            f"{_repo_endpoint(repo)}/git/refs/heads/{quote(branch, safe='')}",
            payload={"sha": commit_sha, "force": False},
            authenticated=True,
        )
        return commit

    async def create_repository(
        self,
        name: str,
        description: str = "",
        *,
        private: bool = True,
        auto_init: bool = True,
    ) -> dict[str, Any]:
        name = _clean_repo_name(name)
        return await self.request(
            "POST",
            "/user/repos",
            payload={
                "name": name,
                "description": _truncate(description.strip(), 350),
                "private": private,
                "auto_init": auto_init,
            },
            authenticated=True,
        )

    async def delete_repository(self, repo: str) -> None:
        await self.request("DELETE", _repo_endpoint(_clean_repo(repo)), authenticated=True)

    async def create_issue(self, repo: str, title: str, body: str) -> dict[str, Any]:
        repo = _clean_repo(repo)
        if not title.strip():
            raise ValueError("Issue title cannot be empty.")
        return await self.request(
            "POST",
            f"{_repo_endpoint(repo)}/issues",
            payload={"title": _truncate(title.strip(), 256), "body": body.strip()},
            authenticated=True,
        )

    async def create_pull_request(
        self, repo: str, head: str, base: str, title: str, body: str
    ) -> dict[str, Any]:
        repo = _clean_repo(repo)
        head = _clean_branch(head)
        base = _clean_branch(base)
        if not title.strip():
            raise ValueError("Pull request title cannot be empty.")
        return await self.request(
            "POST",
            f"{_repo_endpoint(repo)}/pulls",
            payload={"title": _truncate(title.strip(), 256), "head": head, "base": base, "body": body.strip()},
            authenticated=True,
        )

    async def merge_pull_request(self, repo: str, pull_number: int, method: str = "squash") -> dict[str, Any]:
        repo = _clean_repo(repo)
        if pull_number <= 0:
            raise ValueError("Pull request number must be positive.")
        if method not in {"merge", "squash", "rebase"}:
            raise ValueError("Merge method must be `merge`, `squash`, or `rebase`.")
        result = await self.request(
            "PUT",
            f"{_repo_endpoint(repo)}/pulls/{pull_number}/merge",
            payload={"merge_method": method},
            authenticated=True,
        )
        if not result.get("merged"):
            raise GitHubAPIError(409, _truncate(result.get("message") or "GitHub could not merge that pull request.", 300))
        return result

    async def list_workflow_runs(
        self, repo: str, branch: str = "", *, authenticated: bool = False
    ) -> dict[str, Any]:
        repo = _clean_repo(repo)
        params: dict[str, str | int] = {"per_page": 10}
        if branch:
            params["branch"] = _clean_branch(branch)
        return await self.request(
            "GET", f"{_repo_endpoint(repo)}/actions/runs", params=params, authenticated=authenticated
        )

    async def rerun_workflow(self, repo: str, run_id: int) -> None:
        repo = _clean_repo(repo)
        if run_id <= 0:
            raise ValueError("Workflow run ID must be positive.")
        await self.request(
            "POST", f"{_repo_endpoint(repo)}/actions/runs/{run_id}/rerun", authenticated=True
        )

    async def create_release(self, repo: str, tag: str, title: str, body: str) -> dict[str, Any]:
        repo = _clean_repo(repo)
        tag = _clean_tag(tag)
        if not title.strip():
            raise ValueError("Release title cannot be empty.")
        return await self.request(
            "POST",
            f"{_repo_endpoint(repo)}/releases",
            payload={"tag_name": tag, "name": _truncate(title.strip(), 256), "body": body.strip()},
            authenticated=True,
        )


class GitHub(commands.Cog):
    """Per-user GitHub OAuth, browsing, and repository workflows."""

    def __init__(self, bot):
        self.bot = bot
        self.client = GitHubClient(config.GITHUB_TOKEN)
        self.vault = CredentialVault()
        self._task_tokens: dict[int, contextvars.Token] = {}

    async def _user_token(self, user_id: int) -> str:
        if self.vault:
            data = await self.vault.get(user_id, "github")
            if data:
                return str(data.get("access_token") or "")
        if user_id in config.OWNER_IDS:
            return config.GITHUB_TOKEN
        return ""

    async def _use_user_token(self, ctx: commands.Context) -> bool:
        return bool(await self._user_token(ctx.author.id))

    async def cog_before_invoke(self, ctx: commands.Context):
        token = await self._user_token(ctx.author.id)
        self._task_tokens[id(ctx)] = _GITHUB_TASK_TOKEN.set(token)
        write_commands = {
            "create", "delete", "myrepos", "branch", "commit", "unzip",
            "prcreate", "merge", "issue", "release", "rerun",
        }
        if ctx.command and ctx.command.name in write_commands and not token:
            raise commands.CheckFailure("Link your GitHub account first with `gh login`.")

    async def cog_after_invoke(self, ctx: commands.Context):
        marker = self._task_tokens.pop(id(ctx), None)
        if marker is not None:
            _GITHUB_TASK_TOKEN.reset(marker)

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure):
            await ctx.send(embed=error_embed(str(error)))
            return
        raise error

    async def _audit_write(self, ctx: commands.Context, action: str, detail: str):
        if not self.bot.db or not ctx.guild:
            return
        try:
            await self.bot.db.log_action(
                ctx.guild.id, action, ctx.author.id, ctx.author.id, detail
            )
        except Exception:
            log.warning("Could not write GitHub audit entry for %s", action, exc_info=True)

    @staticmethod
    def _github_embed(title: str, description: str = "", color: discord.Color | None = None) -> discord.Embed:
        return discord.Embed(
            title=f"🐙 {title}",
            description=description,
            color=color or discord.Color.dark_grey(),
        )

    async def _send_api_error(self, ctx: commands.Context, exc: Exception):
        if isinstance(exc, (GitHubAPIError, ValueError)):
            await ctx.send(embed=error_embed(str(exc)))
        else:
            log.error("Unexpected GitHub cog error: %s", exc, exc_info=True)
            await ctx.send(embed=error_embed("GitHub operation failed unexpectedly."))

    @staticmethod
    def _repo_lines(repos: list[dict[str, Any]]) -> str:
        if not repos:
            return "No repositories found."
        lines = []
        for repo in repos[:MAX_LIST_ITEMS]:
            visibility = "🔒" if repo.get("private") else "📦"
            description = _truncate(repo.get("description") or "No description", 90)
            lines.append(f"{visibility} [`{repo['full_name']}`]({repo['html_url']}) — {description}")
        return "\n".join(lines)

    @staticmethod
    def _list_lines(items: list[dict[str, Any]]) -> str:
        if not items:
            return "Nothing here."
        return "\n".join(
            f"{'📁' if item.get('type') == 'dir' else '📄'} "
            f"`{item.get('name', item.get('path', 'unknown'))}`"
            for item in items[:MAX_LIST_ITEMS]
        )

    @staticmethod
    def _diff_lines(comparison: dict[str, Any]) -> str:
        files = comparison.get("files") or []
        lines = [
            f"Commits: `{comparison.get('total_commits', 0)}` · "
            f"Ahead: `{comparison.get('ahead_by', 0)}` · "
            f"Behind: `{comparison.get('behind_by', 0)}`"
        ]
        for changed_file in files[:15]:
            lines.append(
                f"`{changed_file.get('status', 'changed')}` "
                f"{_truncate(changed_file.get('filename', 'unknown'), 90)} "
                f"`+{changed_file.get('additions', 0)}/-{changed_file.get('deletions', 0)}`"
            )
        if len(files) > 15:
            lines.append(f"…and {len(files) - 15} more changed file(s).")
        return _truncate("\n".join(lines), 3_500)

    @staticmethod
    def _workflow_lines(runs: list[dict[str, Any]]) -> str:
        if not runs:
            return "No matching workflow runs."
        lines = []
        for run in runs[:10]:
            state = run.get("conclusion") or run.get("status") or "unknown"
            name = _truncate(run.get("name") or run.get("display_title") or "Workflow", 70)
            url = run.get("html_url")
            label = f"[{name}]({url})" if url else name
            lines.append(f"`{state}` {label} · `{run.get('id', '?')}`")
        return "\n".join(lines)

    async def _send_file(self, ctx: commands.Context, repo: str, path: str, branch: str, *, authenticated: bool):
        file_data = await self.client.get_file(repo, path, branch, authenticated=authenticated)
        encoded = file_data.get("content")
        if not encoded or file_data.get("encoding") != "base64":
            raise GitHubAPIError(422, "GitHub did not return inline file content. Use a file smaller than 1 MB.")
        try:
            raw = base64.b64decode(encoded)
        except Exception as exc:
            raise GitHubAPIError(422, "GitHub returned unreadable file content.") from exc

        url = file_data.get("html_url") or f"https://github.com/{repo}/blob/{branch}/{path}"
        embed = self._github_embed(
            f"{repo} / {path}",
            f"Branch: `{branch}` · [Open on GitHub]({url})",
            discord.Color.blurple(),
        )
        filename = PurePosixPath(path).name or "github-file"
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = ""
        if text and "\x00" not in text and len(text) <= 1_700:
            preview = text.replace("```", "`\u200b``")
            await ctx.send(content=f"```{_language_for(path)}\n{preview}\n```", embed=embed)
        else:
            await ctx.send(embed=embed, file=discord.File(io.BytesIO(raw), filename=filename))

    async def _get_commit_attachment(self, ctx: commands.Context) -> discord.Attachment | None:
        attachments = ctx.message.attachments
        if not attachments and ctx.message.reference:
            try:
                referenced = await ctx.channel.fetch_message(ctx.message.reference.message_id)
                attachments = referenced.attachments
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                pass
        return attachments[0] if attachments else None

    @commands.hybrid_group(name="github", aliases=["gh"], invoke_without_command=True)
    async def github(self, ctx: commands.Context):
        prefix = await self.bot._get_prefix(self.bot, ctx.message)
        if isinstance(prefix, list):
            prefix = prefix[0]
        e = self._github_embed(
            "GitHub command deck",
            "Public browsing works without login. Link your own GitHub account for private repositories and authorized writes.",
            discord.Color.blurple(),
        )
        e.add_field(name="Account", value=(
            f"`{prefix}gh login` · connect your GitHub account\n"
            f"`{prefix}gh account` · show the linked account\n"
            f"`{prefix}gh logout` · remove the linked account"
        ), inline=False)
        e.add_field(name="Browse", value=(
            f"`{prefix}gh repos <user>`\n"
            f"`{prefix}gh repo <owner/repo>`\n"
            f"`{prefix}gh files <owner/repo> [path] [branch]`\n"
            f"`{prefix}gh file <owner/repo> <path> [branch]`\n"
            f"`{prefix}gh issues <owner/repo> [open|closed|all]`\n"
            f"`{prefix}gh prs <owner/repo> [open|closed|all]`\n"
            f"`{prefix}gh diff <repo> <base> <head>` · `{prefix}gh runs <repo> [branch]`"
        ), inline=False)
        e.add_field(name="Authenticated account deck", value=(
            f"`{prefix}gh myrepos`\n"
            f"`{prefix}gh create <name> <public|private> [description]`\n"
            f"`{prefix}gh delete <owner/repo> <owner/repo>`\n"
            f"`{prefix}gh branch <repo> <new> [from]`\n"
            f"`{prefix}gh commit <repo> <path> <branch> <message>` + attach file\n"
            f"`{prefix}gh unzip <repo> <destination|.> <branch> [--strip-root] <message>` + attach ZIP\n"
            f"`{prefix}gh prcreate <repo> <head> <base> <title> | <body>`\n"
            f"`{prefix}gh merge <repo> <pr-number> [merge|squash|rebase]`\n"
            f"`{prefix}gh issue <repo> <title> | <body>`\n"
            f"`{prefix}gh release <repo> <tag> <title> | <notes>`\n"
            f"`{prefix}gh rerun <repo> <workflow-run-id>`"
        ), inline=False)
        await ctx.send(embed=e)

    @github.command(name="login", usage="github login")
    @commands.cooldown(1, 30, commands.BucketType.user)
    async def login(self, ctx: commands.Context):
        if not config.GITHUB_OAUTH_CLIENT_ID:
            return await ctx.send(embed=error_embed(
                "GitHub login is not configured. Set `GITHUB_OAUTH_CLIENT_ID` and enable Device Flow on the GitHub OAuth app."
            ))
        if not self.vault:
            return await ctx.send(embed=error_embed(
                "Ephemeral account linking is unavailable."
            ))

        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            async with session.post(
                "https://github.com/login/device/code",
                data={"client_id": config.GITHUB_OAUTH_CLIENT_ID, "scope": GITHUB_OAUTH_SCOPES},
                headers={"Accept": "application/json"},
            ) as response:
                data = await response.json()

        device_code = data.get("device_code")
        user_code = data.get("user_code")
        url = data.get("verification_uri")
        if not device_code or not user_code or not url:
            return await ctx.send(embed=error_embed(
                f"GitHub did not start device login: {data.get('error_description') or data}"
            ))

        e = self._github_embed(
            "Connect your GitHub account",
            f"[Open GitHub authorization]({url})\n\n"
            f"Enter code: **`{user_code}`**\n\n"
            f"This code expires in `{data.get('expires_in', 900)}` seconds.",
            discord.Color.blurple(),
        )
        e.set_footer(text="Requested scopes: repo, workflow, read:user, delete_repo")
        await ctx.send(embed=e)

        interval = max(5, int(data.get("interval", 5)))
        deadline = time.monotonic() + int(data.get("expires_in", 900))
        token_data = None
        async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
            while time.monotonic() < deadline:
                await asyncio.sleep(interval)
                async with session.post(
                    "https://github.com/login/oauth/access_token",
                    data={
                        "client_id": config.GITHUB_OAUTH_CLIENT_ID,
                        "device_code": device_code,
                        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    },
                    headers={"Accept": "application/json"},
                ) as response:
                    result = await response.json()
                if result.get("access_token"):
                    token_data = result
                    break
                error = result.get("error")
                if error == "slow_down":
                    interval += 5
                elif error not in {"authorization_pending", "slow_down"}:
                    return await ctx.send(embed=error_embed(
                        f"GitHub login failed: {result.get('error_description') or error}"
                    ))

        if not token_data:
            return await ctx.send(embed=error_embed(
                "GitHub login expired before authorization completed. Run `gh login` again."
            ))

        access_token = token_data["access_token"]
        client = GitHubClient(access_token)
        marker = _GITHUB_TASK_TOKEN.set(access_token)
        try:
            profile = await client.get_authenticated_user()
        finally:
            _GITHUB_TASK_TOKEN.reset(marker)

        # OAuth credentials exist only in this bot process. No token or account
        # identity is written to the database.
        await self.vault.put(ctx.author.id, "github", {
            "access_token": access_token,
            "scope": token_data.get("scope", ""),
            "login": str(profile.get("login") or ""),
            "account_id": str(profile.get("id") or ""),
            "html_url": str(profile.get("html_url") or "https://github.com"),
        })
        await ctx.send(embed=success_embed(
            f"Connected as [`{profile.get('login')}`]({profile.get('html_url') or 'https://github.com'}). "
            "Your OAuth session is RAM-only and will be erased when TweakBot restarts.",
            title="🐙 GitHub connected",
        ))

    async def _revoke_oauth_token(self, access_token: str) -> bool:
        """Ask GitHub to revoke the OAuth token, then let it die in RAM."""
        if not access_token:
            return False
        try:
            async with aiohttp.ClientSession(timeout=REQUEST_TIMEOUT) as session:
                async with session.post(
                    f"{GITHUB_API}/credentials/revoke",
                    headers={
                        "Accept": "application/vnd.github+json",
                        "X-GitHub-Api-Version": "2026-03-10",
                        "Content-Type": "application/json",
                    },
                    json={"credentials": [access_token]},
                ) as response:
                    return response.status in {200, 202, 204}
        except Exception:
            log.warning("GitHub token revocation request failed", exc_info=True)
            return False

    @github.command(name="logout", usage="github logout")
    async def logout(self, ctx: commands.Context):
        data = await self.vault.get(ctx.author.id, "github") if self.vault else None
        token = str((data or {}).get("access_token") or "")
        revoked = await self._revoke_oauth_token(token) if token else False
        if self.vault:
            await self.vault.delete(ctx.author.id, "github")
        status = (
            "The GitHub OAuth token was also submitted for provider-side revocation."
            if revoked else
            "The local session was erased; provider-side revocation was unavailable or failed. "
            "You can revoke TweakBot from GitHub's Authorized OAuth Apps settings."
        )
        await ctx.send(embed=success_embed(
            "Your GitHub OAuth session was erased from RAM. Nothing was persisted by TweakBot.\n\n" + status,
            title="🐙 GitHub disconnected",
        ))

    @github.command(name="account", aliases=["whoami"], usage="github account")
    async def account(self, ctx: commands.Context):
        data = await self.vault.get(ctx.author.id, "github") if self.vault else None
        if not data:
            return await ctx.send(embed=info_embed("No GitHub session is active. Use `gh login`."))
        await ctx.send(embed=info_embed(
            f"Current GitHub session: `{data.get('login') or 'authenticated'}`\n"
            "Session storage: **RAM only**\n"
            "Persistence: **none**",
            title="🐙 GitHub account",
        ))

    @github.command(name="status", usage="github status")
    async def status(self, ctx: commands.Context):
        linked = bool(await self._user_token(ctx.author.id))
        await ctx.send(embed=self._github_embed(
            "Integration status",
            f"Public browsing: **ready**\n"
            f"Your linked account: **{'ready' if linked else 'not linked'}**\n"
            f"OAuth scopes requested: `repo workflow read:user delete_repo`\n"
            f"Pull-request merging: **{'enabled' if config.GITHUB_ALLOW_PR_MERGE else 'blocked'}**",
            discord.Color.green() if linked else discord.Color.orange(),
        ))

    @github.command(name="repos", usage="github repos <username>")
    async def repos(self, ctx: commands.Context, username: str):
        try:
            username = _clean_username(username)
            repos = await self.client.list_public_repos(username)
            await ctx.send(embed=self._github_embed(f"{username}'s repositories", self._repo_lines(repos), discord.Color.blurple()))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="repo", usage="github repo <owner/repo>")
    async def repo(self, ctx: commands.Context, repo: str):
        try:
            repo = _clean_repo(repo)
            data = await self.client.get_repo(repo, authenticated=await self._use_user_token(ctx))
            e = self._github_embed(data["full_name"], _truncate(data.get("description") or "No description.", 1_000), discord.Color.blurple())
            e.url = data.get("html_url")
            e.add_field(name="Stars", value=f"⭐ {data.get('stargazers_count', 0):,}")
            e.add_field(name="Forks", value=f"🍴 {data.get('forks_count', 0):,}")
            e.add_field(name="Open issues", value=f"⚠️ {data.get('open_issues_count', 0):,}")
            e.add_field(name="Default branch", value=f"`{data.get('default_branch', 'main')}`")
            e.add_field(name="Visibility", value="Private" if data.get("private") else "Public")
            await ctx.send(embed=e)
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="files", usage="github files <owner/repo> [path] [branch]")
    async def files(self, ctx: commands.Context, repo: str, path: str = "", branch: str = "main"):
        try:
            repo = _clean_repo(repo)
            data = await self.client.list_files(repo, _clean_path(path), _clean_branch(branch), authenticated=await self._use_user_token(ctx))
            if isinstance(data, dict):
                return await ctx.send(embed=info_embed(f"`{path}` is a file. Use `{ctx.prefix}gh file {repo} {path} {branch}`."))
            await ctx.send(embed=self._github_embed(f"{repo}/{path}", self._list_lines(data), discord.Color.blurple()))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="file", usage="github file <owner/repo> <path> [branch]")
    async def file(self, ctx: commands.Context, repo: str, path: str, branch: str = "main"):
        try:
            await self._send_file(ctx, _clean_repo(repo), _clean_path(path), _clean_branch(branch), authenticated=await self._use_user_token(ctx))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="issues", usage="github issues <owner/repo> [open|closed|all]")
    async def issues(self, ctx: commands.Context, repo: str, state: str = "open"):
        try:
            repo = _clean_repo(repo)
            entries = [item for item in await self.client.list_issues(repo, state.lower(), authenticated=await self._use_user_token(ctx)) if "pull_request" not in item]
            lines = [f"#{item['number']} [{_truncate(item['title'], 90)}]({item['html_url']})" for item in entries]
            await ctx.send(embed=self._github_embed(f"Issues · {repo}", "\n".join(lines) or "No matching issues.", discord.Color.orange()))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="prs", aliases=["pulls"], usage="github prs <owner/repo> [open|closed|all]")
    async def prs(self, ctx: commands.Context, repo: str, state: str = "open"):
        try:
            repo = _clean_repo(repo)
            entries = await self.client.list_pull_requests(repo, state.lower(), authenticated=await self._use_user_token(ctx))
            lines = [f"#{item['number']} [{_truncate(item['title'], 90)}]({item['html_url']})" for item in entries]
            await ctx.send(embed=self._github_embed(f"Pull requests · {repo}", "\n".join(lines) or "No matching pull requests.", discord.Color.green()))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="diff", usage="github diff <owner/repo> <base> <head>")
    async def diff(self, ctx: commands.Context, repo: str, base: str, head: str):
        try:
            repo = _clean_repo(repo)
            comparison = await self.client.compare(repo, base, head, authenticated=await self._use_user_token(ctx))
            await ctx.send(embed=self._github_embed(f"Diff · {repo}", self._diff_lines(comparison), discord.Color.blurple()))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="runs", aliases=["actions"], usage="github runs <owner/repo> [branch]")
    async def runs(self, ctx: commands.Context, repo: str, branch: str = ""):
        try:
            repo = _clean_repo(repo)
            response = await self.client.list_workflow_runs(repo, branch, authenticated=await self._use_user_token(ctx))
            await ctx.send(embed=self._github_embed(f"Workflow runs · {repo}", self._workflow_lines(response.get("workflow_runs") or []), discord.Color.dark_teal()))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="create", aliases=["repocreate", "newrepo"], usage="github create <name> <public|private> [description]")
    async def create_repo(self, ctx: commands.Context, name: str, visibility: str = "private", *, description: str = ""):
        try:
            visibility = visibility.lower()
            if visibility not in {"public", "private"}:
                raise ValueError("Visibility must be `public` or `private`.")
            repository = await self.client.create_repository(_clean_repo_name(name), description, private=visibility == "private")
            await ctx.send(embed=success_embed(f"Created [`{repository.get('full_name')}`]({repository.get('html_url')}).", title="🐙 GitHub repository created"))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="delete", aliases=["repodelete", "deleterepo"], usage="github delete <owner/repo> <owner/repo>")
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def delete_repo(self, ctx: commands.Context, repo: str, *, confirmation: str = ""):
        try:
            repo = _clean_repo(repo)
            confirmation = confirmation.strip().strip("/")
            if confirmation.casefold() != repo.casefold():
                return await ctx.send(embed=error_embed(
                    "Repository deletion is permanent.\n\n"
                    "Enter the complete repository name twice:\n"
                    f"`{ctx.prefix}gh delete {repo} {repo}`"
                ))

            metadata = await self.client.get_repo(repo, authenticated=True)
            canonical_name = metadata.get("full_name") or repo
            permissions = metadata.get("permissions") or {}
            if not permissions.get("admin"):
                raise GitHubAPIError(403, "Your linked GitHub account does not have administrator permission for this repository.")
            if canonical_name.casefold() != repo.casefold():
                raise GitHubAPIError(409, f"GitHub resolved that repository as `{canonical_name}`. Run the command again using that name.")

            await self.client.delete_repository(canonical_name)
            await ctx.send(embed=success_embed(f"Permanently deleted `{canonical_name}`.", title="🐙 GitHub repository deleted"))
            await self._audit_write(ctx, "github_repo_delete", canonical_name)
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="myrepos", usage="github myrepos")
    async def myrepos(self, ctx: commands.Context):
        try:
            repos = await self.client.list_my_repos()
            await ctx.send(embed=self._github_embed("Your GitHub repositories", self._repo_lines(repos), discord.Color.blurple()))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="branch", usage="github branch <owner/repo> <new_branch> [from_branch]")
    async def branch(self, ctx: commands.Context, repo: str, new_branch: str, from_branch: str = ""):
        try:
            repo = _clean_repo(repo)
            if not from_branch:
                metadata = await self.client.get_repo(repo, authenticated=True)
                from_branch = metadata.get("default_branch") or "main"
            await self.client.create_branch(repo, new_branch, from_branch)
            await ctx.send(embed=success_embed(f"Created `{new_branch}` from `{from_branch}` on `{repo}`.", title="🐙 GitHub branch created"))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="commit", usage="github commit <owner/repo> <path> <branch> <message> + attach a file")
    async def commit(self, ctx: commands.Context, repo: str, path: str, branch: str, *, message: str):
        try:
            repo = _clean_repo(repo)
            branch = _clean_branch(branch)
            if branch in config.GITHUB_PROTECTED_BRANCHES and not config.GITHUB_ALLOW_PROTECTED_BRANCH_COMMITS:
                return await ctx.send(embed=error_embed(f"Direct commits to `{branch}` are disabled."))
            attachment = await self._get_commit_attachment(ctx)
            if not attachment:
                return await ctx.send(embed=error_embed("Attach the file to commit, or reply to a message containing it."))
            result, created = await self.client.upsert_file(repo, path, branch, message, await attachment.read())
            action = "Created" if created else "Updated"
            await ctx.send(embed=success_embed(f"{action} `{path}` on `{repo}@{branch}`.", title="🐙 GitHub commit landed"))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="unzip", aliases=["zipextract", "extractzip"], usage="github unzip <owner/repo> <destination|.> <branch> [--strip-root] <message> + attach ZIP")
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def unzip(self, ctx: commands.Context, repo: str, destination: str, branch: str, *, message: str):
        try:
            repo = _clean_repo(repo)
            destination = _clean_destination(destination)
            branch = _clean_branch(branch)
            strip_root = False
            message = message.strip()
            if message == "--strip-root" or message.startswith("--strip-root "):
                strip_root = True
                message = message[len("--strip-root"):].strip()
            if not message:
                raise ValueError("Commit message cannot be empty.")
            if branch in config.GITHUB_PROTECTED_BRANCHES and not config.GITHUB_ALLOW_PROTECTED_BRANCH_COMMITS:
                return await ctx.send(embed=error_embed(f"Direct commits to `{branch}` are disabled."))

            attachment = await self._get_commit_attachment(ctx)
            if not attachment:
                return await ctx.send(embed=error_embed("Attach a `.zip` file, or reply to a message containing one."))
            if not attachment.filename.lower().endswith(".zip"):
                return await ctx.send(embed=error_embed("The attachment must be a `.zip` file."))
            if attachment.size > MAX_ZIP_BYTES:
                return await ctx.send(embed=error_embed(f"ZIP attachment exceeds the {MAX_ZIP_BYTES // 1_000_000} MB limit."))

            archive = await attachment.read()
            entries = await asyncio.to_thread(_read_zip_entries, archive, destination, strip_root=strip_root)
            commit = await self.client.commit_files(repo, branch, message, entries)
            commit_sha = commit.get("sha", "")
            location = f"`/{destination}`" if destination else "repository root"
            await ctx.send(embed=success_embed(
                f"Extracted **{len(entries)}** file(s) into {location} on `{repo}@{branch}`.\nCommit: `{commit_sha[:7] if commit_sha else 'created'}`",
                title="🐙 GitHub ZIP extracted",
            ))
            await self._audit_write(ctx, "github_unzip", f"{repo}:{destination or '/'} ({branch}) {len(entries)} files")
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="prcreate", aliases=["openpr"], usage="github prcreate <owner/repo> <head> <base> <title> | <body>")
    async def prcreate(self, ctx: commands.Context, repo: str, head: str, base: str, *, text: str):
        try:
            title, _, body = text.partition("|")
            pr = await self.client.create_pull_request(repo, head, base, title, body)
            await ctx.send(embed=success_embed(f"Opened [#{pr['number']} · {_truncate(pr['title'], 150)}]({pr['html_url']})", title="🐙 GitHub pull request created"))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="merge", usage="github merge <owner/repo> <pr-number> [merge|squash|rebase]")
    async def merge(self, ctx: commands.Context, repo: str, pull_number: int, method: str = "squash"):
        if not config.GITHUB_ALLOW_PR_MERGE:
            return await ctx.send(embed=error_embed("Pull-request merging is disabled."))
        try:
            result = await self.client.merge_pull_request(repo, pull_number, method.lower())
            await ctx.send(embed=success_embed(f"Merged pull request `#{pull_number}` in `{repo}`. Commit: `{result.get('sha', 'unknown')}`.", title="🐙 GitHub pull request merged"))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="issue", usage="github issue <owner/repo> <title> | <body>")
    async def issue(self, ctx: commands.Context, repo: str, *, text: str):
        try:
            title, separator, body = text.partition("|")
            if not separator:
                return await ctx.send(embed=error_embed("Use: `title | issue body`"))
            issue = await self.client.create_issue(repo, title, body)
            await ctx.send(embed=success_embed(f"Opened [#{issue['number']} · {_truncate(issue['title'], 150)}]({issue['html_url']})", title="🐙 GitHub issue created"))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="release", usage="github release <owner/repo> <tag> <title> | <notes>")
    async def release(self, ctx: commands.Context, repo: str, tag: str, *, text: str):
        try:
            title, _, body = text.partition("|")
            release = await self.client.create_release(repo, tag, title, body)
            await ctx.send(embed=success_embed(f"Published [{_truncate(release['name'], 150)}]({release['html_url']}) as `{release['tag_name']}`.", title="🐙 GitHub release created"))
        except Exception as exc:
            await self._send_api_error(ctx, exc)

    @github.command(name="rerun", usage="github rerun <owner/repo> <workflow-run-id>")
    async def rerun(self, ctx: commands.Context, repo: str, run_id: int):
        try:
            await self.client.rerun_workflow(repo, run_id)
            await ctx.send(embed=success_embed(f"Requested a re-run for workflow run `{run_id}` in `{repo}`.", title="🐙 GitHub Actions re-run requested"))
        except Exception as exc:
            await self._send_api_error(ctx, exc)


async def setup(bot):
    await bot.add_cog(GitHub(bot))
