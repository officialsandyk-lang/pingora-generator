from __future__ import annotations

import json
import re
import shlex
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


CONFIRM_FLAGS = {"--confirm", "--yes", "-y"}
DRY_RUN_FLAGS = {"--dry-run"}

RESERVED_DESTRUCTIVE_TARGETS = {
    "backend",
    "backends",
    "upstream",
    "upstreams",
    "server",
    "servers",
    "database",
    "db",
    "data",
    "logs",
    "log",
    "certificate",
    "cert",
    "certs",
    "key",
    "keys",
    "secret",
    "secrets",
    "policy",
    "policies",
}

ROUTE_TOKEN_PATTERN = r"(/[A-Za-z0-9._~\-/]+|[A-Za-z0-9][A-Za-z0-9._~\-/]*)"

DESTRUCTIVE_ROUTE_RE = re.compile(
    rf"\b(?P<verb>delete|destroy|purge|wipe|erase)\s+"
    rf"(?:(?:route|path|endpoint)\s+)?"
    rf"(?P<route>{ROUTE_TOKEN_PATTERN})\b",
    re.IGNORECASE,
)

SAFE_REMOVE_ROUTE_RE = re.compile(
    rf"\b(?P<verb>remove|unroute)\s+"
    rf"(?:(?:route|path|endpoint)\s+)?"
    rf"(?P<route>{ROUTE_TOKEN_PATTERN})\b",
    re.IGNORECASE,
)

STOP_ROUTING_RE = re.compile(
    rf"\bstop\s+routing\s+(?:to\s+)?(?P<route>{ROUTE_TOKEN_PATTERN})\b",
    re.IGNORECASE,
)

ANY_DESTRUCTIVE_WORD_RE = re.compile(
    r"\b(delete|destroy|purge|wipe|erase)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedUpdateCommand:
    prompt: str
    confirm: bool = False
    dry_run: bool = False


@dataclass(frozen=True)
class DestructiveIntent:
    detected: bool
    routes: list[str]
    generic_targets: list[str]
    reason: str


def parse_update_cli_args(argv: list[str]) -> ParsedUpdateCommand:
    confirm = any(arg in CONFIRM_FLAGS for arg in argv)
    dry_run = any(arg in DRY_RUN_FLAGS for arg in argv)

    prompt_parts = [
        arg
        for arg in argv
        if arg not in CONFIRM_FLAGS and arg not in DRY_RUN_FLAGS
    ]

    return ParsedUpdateCommand(
        prompt=" ".join(prompt_parts).strip(),
        confirm=confirm,
        dry_run=dry_run,
    )


def normalize_route_path(value: str) -> str:
    path = (value or "").strip().strip("`'\".,;: ")

    if not path:
        return ""

    if not path.startswith("/"):
        path = "/" + path

    while "//" in path:
        path = path.replace("//", "/")

    if len(path) > 1:
        path = path.rstrip("/")

    return path


def detect_destructive_intent(prompt: str) -> DestructiveIntent:
    prompt = prompt or ""
    routes: list[str] = []
    generic_targets: list[str] = []

    for match in DESTRUCTIVE_ROUTE_RE.finditer(prompt):
        raw_target = match.group("route").strip().strip("`'\".,;: ")
        raw_lower = raw_target.lower()

        if raw_lower in RESERVED_DESTRUCTIVE_TARGETS:
            if raw_target not in generic_targets:
                generic_targets.append(raw_target)
            continue

        route = normalize_route_path(raw_target)
        if route and route not in routes:
            routes.append(route)

    if not routes and ANY_DESTRUCTIVE_WORD_RE.search(prompt):
        generic_targets.append("destructive update command")

    detected = bool(routes or generic_targets)

    return DestructiveIntent(
        detected=detected,
        routes=routes,
        generic_targets=generic_targets,
        reason=(
            "Destructive update command detected."
            if detected
            else ""
        ),
    )


def normalize_safe_route_prompt(prompt: str) -> str:
    """
    Converts:
      remove analytics       -> remove /analytics
      remove route analytics -> remove /analytics
      stop routing to users  -> remove /users

    Does not normalize delete/destroy/purge/wipe/erase.
    Those are handled by the destructive confirmation gate.
    """

    def remove_replacer(match: re.Match) -> str:
        verb = match.group("verb").lower()
        route = normalize_route_path(match.group("route"))
        return f"{verb} {route}"

    def stop_routing_replacer(match: re.Match) -> str:
        route = normalize_route_path(match.group("route"))
        return f"remove {route}"

    prompt = SAFE_REMOVE_ROUTE_RE.sub(remove_replacer, prompt or "")
    prompt = STOP_ROUTING_RE.sub(stop_routing_replacer, prompt)
    return prompt


def rewrite_confirmed_destructive_prompt(prompt: str, intent: DestructiveIntent) -> str:
    """
    After --confirm, route deletion becomes explicit route removal.

    Converts:
      delete analytics -> remove /analytics

    This keeps the gateway operation route-scoped and avoids any wording that
    could later be interpreted as deleting backend data.
    """

    def destructive_replacer(match: re.Match) -> str:
        raw_target = match.group("route").strip().strip("`'\".,;: ")

        if raw_target.lower() in RESERVED_DESTRUCTIVE_TARGETS:
            return match.group(0)

        route = normalize_route_path(raw_target)
        return f"remove {route}"

    rewritten = DESTRUCTIVE_ROUTE_RE.sub(destructive_replacer, prompt or "")
    return normalize_safe_route_prompt(rewritten)


def extract_route_removal_requests(prompt: str) -> list[str]:
    normalized_prompt = normalize_safe_route_prompt(prompt or "")
    routes: list[str] = []

    for match in SAFE_REMOVE_ROUTE_RE.finditer(normalized_prompt):
        route = normalize_route_path(match.group("route"))
        if route and route not in routes:
            routes.append(route)

    return routes


def _ignore_backup_dirs(_dir: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in {
            "target",
            ".git",
            ".venv",
            "__pycache__",
            ".pytest_cache",
            "node_modules",
        }
    }


def create_safety_backup(project_root: str | Path | None = None) -> Path:
    """
    Backs up generated gateway artifacts before a confirmed destructive update.

    This intentionally does not copy .env files, because they may contain keys.
    """

    root = Path(project_root or Path.cwd()).resolve()
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_dir = root / "backups" / f"destructive-update-{timestamp}"
    backup_dir.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []

    generated_project = root / "generated-pingora-proxy"
    if generated_project.exists():
        shutil.copytree(
            generated_project,
            backup_dir / "generated-pingora-proxy",
            ignore=_ignore_backup_dirs,
            dirs_exist_ok=True,
        )
        copied.append("generated-pingora-proxy")

    candidate_files = [
        "active_config.json",
        "gateway_config.json",
        "config.json",
        "bluegreen_state.json",
        "gateway_state.json",
        ".gateway_state.json",
    ]

    for filename in candidate_files:
        source = root / filename
        if source.exists() and source.is_file():
            shutil.copy2(source, backup_dir / filename)
            copied.append(filename)

    manifest = {
        "created_at": timestamp,
        "project_root": str(root),
        "copied": copied,
        "note": "Safety backup created before confirmed destructive gateway update.",
    }

    (backup_dir / "MANIFEST.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )

    return backup_dir


def format_destructive_warning(
    command: ParsedUpdateCommand,
    intent: DestructiveIntent,
) -> str:
    quoted_prompt = shlex.quote(command.prompt)

    lines = [
        "⚠️ Destructive command detected",
        "",
    ]

    if intent.routes:
        lines.append("Requested destructive route operation(s):")
        for route in intent.routes:
            lines.append(f"  - delete {route}")
        lines.append("")

    if intent.generic_targets:
        lines.append("Requested destructive target(s):")
        for target in intent.generic_targets:
            lines.append(f"  - {target}")
        lines.append("")

    lines.extend(
        [
            "No changes were applied.",
            "",
            "This gateway operation will not delete backend data, but it can stop traffic",
            "from reaching the selected route(s). A backup is required before continuing.",
            "",
            "To continue, run:",
            f"  python update.py {quoted_prompt} --confirm",
            "",
            "Safer route-only alternatives:",
        ]
    )

    for route in intent.routes:
        lines.append(f'  python update.py "remove {route}"')
        lines.append(f'  python update.py "block {route}"')

    if not intent.routes:
        lines.append('  python update.py "remove /route-name"')
        lines.append('  python update.py "block /route-name"')

    return "\n".join(lines)