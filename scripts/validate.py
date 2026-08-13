"""Fast, dependency-free regression checks for the Discord bot.

Run this from ``artifacts/discord-bot`` with ``python scripts/validate.py``.
It intentionally avoids importing the bot, so it can run before optional voice
and AI dependencies are installed.
"""
from __future__ import annotations

import ast
import sys
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

REQUIRED_ENV_KEYS = {
    "DISCORD_TOKEN",
    "OWNER_IDS",
    "STRICT_COG_LOADING",
    "ENABLE_OWNER_EVAL",
    "AI_AUTO_RESPOND",
    "AI_MODERATION_TOOLS_ENABLED",
    "AI_MUSIC_TOOLS_ENABLED",
    "AI_COMMAND_TOOLS_ENABLED",
    "AI_MENTION_COOLDOWN_SECONDS",
    "AI_HISTORY_PAIRS",
    "AI_MAX_INPUT_CHARS",
    "GITHUB_TOKEN",
    "GITHUB_PROTECTED_BRANCHES",
    "GITHUB_ALLOW_PROTECTED_BRANCH_COMMITS",
    "GITHUB_ALLOW_PR_MERGE",
    "LOG_MESSAGE_CONTENT",
    "LOG_ATTACHMENT_NAMES",
}


def _decorator_name(decorator: ast.expr) -> tuple[str, str] | None:
    """Return (owner, decorator) for a call such as commands.command(...)."""
    if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
        return None
    owner = decorator.func.value
    if isinstance(owner, ast.Name):
        return owner.id, decorator.func.attr
    return None


def _keyword_value(decorator: ast.Call, name: str):
    for keyword in decorator.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    return None


def _aliases(decorator: ast.Call) -> list[str]:
    for keyword in decorator.keywords:
        if keyword.arg != "aliases" or not isinstance(keyword.value, (ast.List, ast.Tuple)):
            continue
        values = []
        for item in keyword.value.elts:
            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                values.append(item.value)
        return values
    return []


def _top_level_command_names(tree: ast.AST) -> list[tuple[str, int]]:
    """Collect command names registered directly through commands.* decorators."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            descriptor = _decorator_name(decorator)
            if descriptor not in {
                ("commands", "command"),
                ("commands", "hybrid_command"),
                ("commands", "group"),
                ("commands", "hybrid_group"),
            }:
                continue
            assert isinstance(decorator, ast.Call)
            name = _keyword_value(decorator, "name") or node.name
            if not isinstance(name, str):
                continue
            found.append((name, node.lineno))
            for alias in _aliases(decorator):
                found.append((alias, node.lineno))
    return found


def _parse_sources() -> tuple[list[str], dict[str, list[str]]]:
    failures: list[str] = []
    commands: dict[str, list[str]] = defaultdict(list)
    for path in sorted(ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as exc:
            failures.append(f"Syntax error in {path.relative_to(ROOT)}:{exc.lineno}: {exc.msg}")
            continue
        for name, line in _top_level_command_names(tree):
            commands[name].append(f"{path.relative_to(ROOT)}:{line}")
    return failures, commands


def _check_env_example() -> list[str]:
    path = ROOT / ".env.example"
    if not path.is_file():
        return ["Missing .env.example"]
    keys = {
        line.split("=", 1)[0].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    missing = sorted(REQUIRED_ENV_KEYS - keys)
    return [f".env.example is missing: {', '.join(missing)}"] if missing else []


def _check_hardening_guards() -> list[str]:
    checks = {
        ROOT / "bot.py": ("discord.Intents.all()", False),
        ROOT / "cogs" / "admin.py": ("ENABLE_OWNER_EVAL", True),
        ROOT / "cogs" / "logging.py": ("LOG_MESSAGE_CONTENT", True),
        ROOT / "cogs" / "github.py": ("GITHUB_ALLOW_PROTECTED_BRANCH_COMMITS", True),
    }
    failures: list[str] = []
    for path, (needle, should_exist) in checks.items():
        contents = path.read_text(encoding="utf-8")
        exists = needle in contents
        if exists != should_exist:
            expectation = "contain" if should_exist else "not contain"
            failures.append(f"{path.relative_to(ROOT)} should {expectation} {needle!r}")
    return failures


def main() -> int:
    failures, commands = _parse_sources()
    duplicates = {name: locations for name, locations in commands.items() if len(locations) > 1}
    for name, locations in sorted(duplicates.items()):
        failures.append(f"Duplicate top-level command or alias {name!r}: {', '.join(locations)}")
    failures.extend(_check_env_example())
    failures.extend(_check_hardening_guards())

    if failures:
        print("Validation failed:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(f"Validation passed: {len(commands)} top-level command names/aliases across source files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
