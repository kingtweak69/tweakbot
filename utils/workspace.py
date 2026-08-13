"""Guarded persistent guarded repository workspaces for TweakBot.

This is deliberately *not* an arbitrary shell.  It provides path-safe file
operations plus a small set of build/test commands executed with a scrubbed
environment and resource/time limits.
"""
from __future__ import annotations

import asyncio
import difflib
import os
import re
import resource
import shutil
import tempfile
import uuid
from pathlib import Path, PurePosixPath
from typing import Iterable

WORKSPACE_ROOT = Path(os.getenv("AGENT_WORKSPACE_ROOT", "/data/tweakbot-workspaces"))
MAX_FILE_BYTES = max(64_000, int(os.getenv("AGENT_WORKSPACE_MAX_FILE_BYTES", "2000000")))
MAX_OUTPUT = max(4000, int(os.getenv("AGENT_WORKSPACE_MAX_OUTPUT", "20000")))
DEFAULT_TIMEOUT = max(5, int(os.getenv("AGENT_WORKSPACE_TIMEOUT", "90")))
_ID_RE = re.compile(r"^(?P<uid>\d+)-(?P<token>[a-f0-9]{12})$")


class WorkspaceError(RuntimeError):
    pass


def new_workspace_id(user_id: int) -> str:
    return f"{int(user_id)}-{uuid.uuid4().hex[:12]}"


def workspace_dir(workspace_id: str, user_id: int) -> Path:
    match = _ID_RE.fullmatch(workspace_id or "")
    if not match or int(match.group("uid")) != int(user_id):
        raise WorkspaceError("Invalid workspace id or workspace does not belong to this user.")
    root = WORKSPACE_ROOT.resolve()
    path = (root / workspace_id).resolve()
    if root not in path.parents:
        raise WorkspaceError("Unsafe workspace path.")
    return path


def safe_path(root: Path, relative: str) -> Path:
    relative = (relative or "").replace("\\", "/").strip("/")
    p = PurePosixPath(relative)
    if not relative or p.is_absolute() or any(part in {"", ".", ".."} for part in p.parts):
        raise WorkspaceError("Unsafe or empty workspace path.")
    target = (root / Path(*p.parts)).resolve()
    if root.resolve() not in target.parents:
        raise WorkspaceError("Workspace path escapes the workspace root.")
    return target


def scrubbed_env(root: Path) -> dict[str, str]:
    home = root / ".home"
    home.mkdir(exist_ok=True)
    keep = {
        "PATH": os.getenv("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "LANG": os.getenv("LANG", "C.UTF-8"),
        "LC_ALL": os.getenv("LC_ALL", "C.UTF-8"),
        "HOME": str(home),
        "TMPDIR": str(root / ".tmp"),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "CI": "1",
    }
    Path(keep["TMPDIR"]).mkdir(exist_ok=True)
    return keep


def _limits() -> None:
    # Best-effort process limits; the parent bot remains untouched.
    try:
        resource.setrlimit(resource.RLIMIT_CPU, (60, 65))
        resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024 * 1024, 128 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))
        # 2 GiB virtual memory ceiling for child processes.
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024**3, 2 * 1024**3))
    except Exception:
        pass


async def run_guarded(
    root: Path,
    argv: list[str],
    *,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int, str]:
    if not argv or any("\x00" in part for part in argv):
        raise WorkspaceError("Invalid command arguments.")
    executable = shutil.which(argv[0], path=scrubbed_env(root)["PATH"])
    if not executable:
        raise WorkspaceError(f"Required executable `{argv[0]}` is not installed.")

    proc = await asyncio.create_subprocess_exec(
        executable,
        *argv[1:],
        cwd=str(root),
        env=scrubbed_env(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        preexec_fn=_limits if os.name == "posix" else None,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=max(1, timeout))
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        return 124, f"Timed out after {timeout}s."
    text = stdout.decode("utf-8", errors="replace")
    if len(text) > MAX_OUTPUT:
        text = text[-MAX_OUTPUT:]
        text = "[output truncated to final bytes]\n" + text
    return int(proc.returncode or 0), text


def list_files(root: Path, limit: int = 500) -> list[str]:
    ignored = {".git", ".home", ".tmp", "node_modules", ".venv", "venv", "__pycache__"}
    result: list[str] = []
    for path in root.rglob("*"):
        rel = path.relative_to(root)
        if any(part in ignored for part in rel.parts):
            continue
        if path.is_file():
            result.append(rel.as_posix())
            if len(result) >= limit:
                break
    return sorted(result)


def read_text(root: Path, relative: str) -> str:
    path = safe_path(root, relative)
    if not path.is_file():
        raise WorkspaceError(f"File `{relative}` does not exist.")
    if path.stat().st_size > MAX_FILE_BYTES:
        raise WorkspaceError(f"File `{relative}` exceeds the workspace text limit.")
    raw = path.read_bytes()
    if b"\x00" in raw:
        raise WorkspaceError(f"File `{relative}` is binary.")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceError(f"File `{relative}` is not UTF-8 text.") from exc


def write_text(root: Path, relative: str, content: str) -> None:
    raw = content.encode("utf-8")
    if len(raw) > MAX_FILE_BYTES:
        raise WorkspaceError("Replacement file exceeds the workspace text limit.")
    path = safe_path(root, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)


def replace_text(root: Path, relative: str, old: str, new: str, replace_all: bool = False) -> int:
    text = read_text(root, relative)
    if not old:
        raise WorkspaceError("The text to replace cannot be empty.")
    count = text.count(old)
    if count == 0:
        raise WorkspaceError("Exact text to replace was not found.")
    if count > 1 and not replace_all:
        raise WorkspaceError(f"Exact text appears {count} times; set replace_all=true or provide a more specific match.")
    updated = text.replace(old, new) if replace_all else text.replace(old, new, 1)
    write_text(root, relative, updated)
    return count if replace_all else 1


def search_text(root: Path, query: str, limit: int = 100) -> list[str]:
    query_cf = query.casefold()
    results: list[str] = []
    for rel in list_files(root, limit=2000):
        try:
            text = read_text(root, rel)
        except WorkspaceError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if query_cf in line.casefold():
                results.append(f"{rel}:{lineno}: {line[:300]}")
                if len(results) >= limit:
                    return results
    return results


async def init_baseline_git(root: Path) -> None:
    if not shutil.which("git"):
        return
    commands = [
        ["git", "init", "-q"],
        ["git", "config", "user.email", "tweakbot@localhost"],
        ["git", "config", "user.name", "TweakBot Workspace"],
        ["git", "add", "-A"],
        ["git", "commit", "-qm", "workspace baseline"],
    ]
    for argv in commands:
        code, output = await run_guarded(root, argv, timeout=30)
        if code != 0 and argv[1] not in {"commit"}:
            raise WorkspaceError(f"Could not initialize workspace baseline: {output[:500]}")


async def git_diff(root: Path) -> str:
    if not (root / ".git").exists():
        return "No local Git baseline is available."
    code, output = await run_guarded(root, ["git", "diff", "--no-ext-diff", "--", "."], timeout=30)
    if code not in {0, 1}:
        raise WorkspaceError(output or "git diff failed")
    return output or "No workspace changes."
