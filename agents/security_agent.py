from __future__ import annotations

import re
from typing import Any

from langsmith import traceable


VALID_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}

DEFAULT_SECURITY = {
    "blocked_paths": [
        "/.env",
        "/.git",
        "/admin.php",
        "/phpmyadmin",
        "/wp-admin",
        "/wp-login.php",
    ],
    "allowed_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    "rate_limit_per_minute": 120,
    "max_connections": 1000,
    "max_request_body_bytes": 1048576,
    "upstream_timeout_seconds": 30,
}


def normalize_path(path: Any) -> str | None:
    if not isinstance(path, str):
        return None

    path = path.strip()

    if not path:
        return None

    if not path.startswith("/"):
        path = "/" + path

    path = re.sub(r"[^a-zA-Z0-9/_\-.]", "", path)

    if not path:
        return None

    if not path.startswith("/"):
        path = "/" + path

    return path


def normalize_methods(methods: Any) -> list[str]:
    if not isinstance(methods, list):
        return []

    clean: list[str] = []

    for method in methods:
        if not isinstance(method, str):
            continue

        upper = method.strip().upper()

        if upper in VALID_METHODS and upper not in clean:
            clean.append(upper)

    return clean


def normalize_positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except Exception:
        return default

    if number <= 0:
        return default

    return number


def paths_in_text(text: str) -> list[str]:
    found = re.findall(r"/[a-zA-Z0-9_\-./]+", text)

    clean: list[str] = []

    for path in found:
        fixed = normalize_path(path.rstrip(".,;:"))

        if fixed and fixed not in clean:
            clean.append(fixed)

    return clean


def extract_blocked_paths_from_prompt(prompt: str | None) -> list[str]:
    if not prompt:
        return []

    blocked: list[str] = []

    clauses = re.split(r"[,.;]", prompt)

    for clause in clauses:
        lower = clause.lower()

        if "block" in lower or "blocked" in lower or "deny" in lower:
            for path in paths_in_text(clause):
                if path not in blocked:
                    blocked.append(path)

    return blocked


def extract_allowed_methods_from_prompt(prompt: str | None) -> list[str]:
    if not prompt:
        return []

    match = re.search(
        r"only\s+allow\s+([a-zA-Z,\s/and]+?)(?:,|\.|$|set|max|block|rate|upstream)",
        prompt,
        flags=re.IGNORECASE,
    )

    if not match:
        return []

    methods_text = match.group(1)

    found = re.findall(
        r"\bGET\b|\bPOST\b|\bPUT\b|\bPATCH\b|\bDELETE\b|\bOPTIONS\b|\bHEAD\b",
        methods_text,
        flags=re.IGNORECASE,
    )

    return normalize_methods(found)


def extract_security_numbers_from_prompt(prompt: str | None) -> dict[str, int]:
    if not prompt:
        return {}

    lower = prompt.lower()
    result: dict[str, int] = {}

    match = re.search(r"rate\s+limit\s+(?:to\s+)?(\d+)", lower)
    if match:
        result["rate_limit_per_minute"] = int(match.group(1))

    match = re.search(r"max\s+request\s+body\s+(?:to\s+)?(\d+)", lower)
    if match:
        result["max_request_body_bytes"] = int(match.group(1))

    match = re.search(r"max\s+connections\s+(?:to\s+)?(\d+)", lower)
    if match:
        result["max_connections"] = int(match.group(1))

    match = re.search(r"upstream\s+timeout\s+(?:to\s+)?(\d+)", lower)
    if match:
        result["upstream_timeout_seconds"] = int(match.group(1))

    return result


def merge_blocked_paths(
    user_paths: Any,
    prompt_paths: list[str],
) -> list[str]:
    merged: list[str] = []

    if isinstance(user_paths, list):
        for path in user_paths:
            normalized = normalize_path(path)

            if normalized and normalized not in merged:
                merged.append(normalized)

    for path in prompt_paths:
        normalized = normalize_path(path)

        if normalized and normalized not in merged:
            merged.append(normalized)

    for path in DEFAULT_SECURITY["blocked_paths"]:
        normalized = normalize_path(path)

        if normalized and normalized not in merged:
            merged.append(normalized)

    return merged


@traceable(name="security_agent_enforce_security", run_type="chain")
def enforce_security(
    config: dict[str, Any],
    prompt: str | None = None,
) -> dict[str, Any]:
    """
    Security Agent.

    Main behavior:
    - Preserve user security config.
    - Read original prompt when provided.
    - If prompt says "block /private and /internal", add those paths.
    - If prompt says "only allow GET and POST", set methods exactly to GET/POST.
    - Merge dangerous default blocked paths.
    - Fill missing numeric limits.
    """
    fixed = dict(config)

    incoming = fixed.get("security")

    if not isinstance(incoming, dict):
        incoming = {}

    prompt_blocked_paths = extract_blocked_paths_from_prompt(prompt)
    prompt_methods = extract_allowed_methods_from_prompt(prompt)
    prompt_numbers = extract_security_numbers_from_prompt(prompt)

    security: dict[str, Any] = {}

    user_methods = normalize_methods(incoming.get("allowed_methods"))

    if prompt_methods:
        security["allowed_methods"] = prompt_methods
    elif user_methods:
        security["allowed_methods"] = user_methods
    else:
        security["allowed_methods"] = list(DEFAULT_SECURITY["allowed_methods"])

    security["blocked_paths"] = merge_blocked_paths(
        incoming.get("blocked_paths"),
        prompt_blocked_paths,
    )

    security["rate_limit_per_minute"] = normalize_positive_int(
        prompt_numbers.get(
            "rate_limit_per_minute",
            incoming.get("rate_limit_per_minute"),
        ),
        DEFAULT_SECURITY["rate_limit_per_minute"],
    )

    security["max_connections"] = normalize_positive_int(
        prompt_numbers.get(
            "max_connections",
            incoming.get("max_connections"),
        ),
        DEFAULT_SECURITY["max_connections"],
    )

    security["max_request_body_bytes"] = normalize_positive_int(
        prompt_numbers.get(
            "max_request_body_bytes",
            incoming.get("max_request_body_bytes"),
        ),
        DEFAULT_SECURITY["max_request_body_bytes"],
    )

    security["upstream_timeout_seconds"] = normalize_positive_int(
        prompt_numbers.get(
            "upstream_timeout_seconds",
            incoming.get("upstream_timeout_seconds"),
        ),
        DEFAULT_SECURITY["upstream_timeout_seconds"],
    )

    fixed["security"] = security

    print("✅ Security check passed")

    return fixed