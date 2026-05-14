from __future__ import annotations

import copy
import re
from typing import Any, Dict, Iterable, List

try:
    from langsmith import traceable
except Exception:
    def traceable(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator


HTTP_METHODS = [
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "HEAD",
]

DEFAULT_BLOCKED_PATHS = [
    "/.env",
    "/.git",
    "/admin.php",
    "/phpmyadmin",
    "/wp-admin",
    "/wp-login.php",
]

DEFAULT_SECURITY = {
    "allowed_methods": HTTP_METHODS,
    "blocked_paths": DEFAULT_BLOCKED_PATHS,
    "rate_limit_per_minute": 120,
    "max_connections": 1000,
    "max_request_body_bytes": 1048576,
    "upstream_timeout_seconds": 30,
}


def _dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    output: List[str] = []

    for item in items:
        text = str(item).strip()

        if not text:
            continue

        key = text.lower()

        if key in seen:
            continue

        seen.add(key)
        output.append(text)

    return output


def _normalize_path(path: Any) -> str:
    text = str(path or "").strip()

    if not text:
        return ""

    if not text.startswith("/"):
        text = "/" + text

    if len(text) > 1:
        text = text.rstrip("/")

    return text


def _normalize_methods(methods: Any) -> List[str]:
    if not isinstance(methods, list):
        return list(HTTP_METHODS)

    output: List[str] = []

    for method in methods:
        upper = str(method).strip().upper()

        if upper in HTTP_METHODS and upper not in output:
            output.append(upper)

    return output or list(HTTP_METHODS)


def _normalize_blocked_paths(paths: Any) -> List[str]:
    if not isinstance(paths, list):
        return []

    output: List[str] = []

    for path in paths:
        fixed = _normalize_path(path)

        if fixed and fixed not in output:
            output.append(fixed)

    return output


def _parse_blocked_paths_from_prompt(prompt: str | None) -> List[str]:
    if not prompt:
        return []

    found: List[str] = []

    patterns = [
        r"\b(?:block|deny)\s+(?P<paths>.*?)(?=\.|\bonly\s+allow\b|\bset\s+rate\b|\bmax\s+request\b|\bmax\s+connections\b|\bupstream\s+timeout\b|$)",
        r"\bkeep\s+(?P<paths>.*?)\s+blocked\b",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, prompt, flags=re.IGNORECASE | re.DOTALL):
            text = match.group("paths")
            paths = re.findall(r"/[A-Za-z0-9_.\-/]+", text)

            for path in paths:
                fixed = _normalize_path(path)

                if fixed and fixed not in found:
                    found.append(fixed)

    return found


def _parse_allowed_methods_from_prompt(prompt: str | None) -> List[str] | None:
    """
    Parse phrases like:

    - only allow GET and POST
    - only allow GET, POST, PUT, PATCH, DELETE, OPTIONS, and HEAD

    Important:
    This must not stop at the word "and".
    Older parsing returned only ["GET"] for long method lists.
    """

    if not prompt:
        return None

    match = re.search(
        r"\bonly\s+allow\s+(?P<methods>.*?)(?=\.|\bblock\b|\bdeny\b|\bset\s+rate\b|\bmax\s+request\b|\bmax\s+connections\b|\bupstream\s+timeout\b|$)",
        prompt,
        flags=re.IGNORECASE | re.DOTALL,
    )

    if not match:
        return None

    segment = match.group("methods")

    methods: List[str] = []

    for method in re.findall(
        r"\b(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\b",
        segment,
        flags=re.IGNORECASE,
    ):
        upper = method.upper()

        if upper not in methods:
            methods.append(upper)

    return methods or None


def _parse_int_from_prompt(prompt: str | None, patterns: list[str]) -> int | None:
    if not prompt:
        return None

    for pattern in patterns:
        match = re.search(pattern, prompt, flags=re.IGNORECASE)

        if not match:
            continue

        try:
            value = int(match.group("value"))
        except Exception:
            continue

        if value >= 0:
            return value

    return None


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
    except Exception:
        return default

    if number < 0:
        return default

    return number


def _base_security(config: Dict[str, Any]) -> Dict[str, Any]:
    incoming = config.get("security")

    if not isinstance(incoming, dict):
        incoming = {}

    security = copy.deepcopy(DEFAULT_SECURITY)

    # Preserve existing generated/user config first.
    if "allowed_methods" in incoming:
        security["allowed_methods"] = _normalize_methods(incoming.get("allowed_methods"))

    if "blocked_paths" in incoming:
        security["blocked_paths"] = _dedupe(
            _normalize_blocked_paths(incoming.get("blocked_paths")) + DEFAULT_BLOCKED_PATHS
        )

    for key in (
        "rate_limit_per_minute",
        "max_connections",
        "max_request_body_bytes",
        "upstream_timeout_seconds",
    ):
        if key in incoming:
            security[key] = _positive_int(incoming.get(key), int(DEFAULT_SECURITY[key]))

    return security


@traceable(name="security_agent_enforce_security", run_type="chain")
def enforce_security(
    config: Dict[str, Any],
    prompt: str | None = None,
) -> Dict[str, Any]:
    """
    Deterministic security enforcement.

    Rules:
    - Preserve existing security config.
    - Add default dangerous blocked paths.
    - Apply prompt security intent if present.
    - Correctly parse long allowed-method lists.
    """

    updated = copy.deepcopy(config or {})
    security = _base_security(updated)

    prompt_blocked_paths = _parse_blocked_paths_from_prompt(prompt)
    if prompt_blocked_paths:
        security["blocked_paths"] = _dedupe(
            list(security.get("blocked_paths", [])) + prompt_blocked_paths + DEFAULT_BLOCKED_PATHS
        )
    else:
        security["blocked_paths"] = _dedupe(
            list(security.get("blocked_paths", [])) + DEFAULT_BLOCKED_PATHS
        )

    prompt_methods = _parse_allowed_methods_from_prompt(prompt)
    if prompt_methods:
        security["allowed_methods"] = prompt_methods
    else:
        security["allowed_methods"] = _normalize_methods(security.get("allowed_methods"))

    rate_limit = _parse_int_from_prompt(
        prompt,
        [
            r"\brate\s+limit\s+to\s+(?P<value>\d+)",
            r"\brate\s+limit\s+(?:of\s+)?(?P<value>\d+)",
            r"\b(?P<value>\d+)\s+requests\s+per\s+minute",
        ],
    )

    if rate_limit is not None:
        security["rate_limit_per_minute"] = rate_limit

    max_body = _parse_int_from_prompt(
        prompt,
        [
            r"\bmax\s+request\s+body\s+to\s+(?P<value>\d+)",
            r"\bmax\s+body\s+to\s+(?P<value>\d+)",
            r"\brequest\s+body\s+limit\s+to\s+(?P<value>\d+)",
        ],
    )

    if max_body is not None:
        security["max_request_body_bytes"] = max_body

    max_connections = _parse_int_from_prompt(
        prompt,
        [
            r"\bmax\s+connections\s+to\s+(?P<value>\d+)",
            r"\bconnection\s+limit\s+to\s+(?P<value>\d+)",
        ],
    )

    if max_connections is not None:
        security["max_connections"] = max_connections

    upstream_timeout = _parse_int_from_prompt(
        prompt,
        [
            r"\bupstream\s+timeout\s+to\s+(?P<value>\d+)",
            r"\bbackend\s+timeout\s+to\s+(?P<value>\d+)",
            r"\btimeout\s+to\s+(?P<value>\d+)\s+seconds",
        ],
    )

    if upstream_timeout is not None:
        security["upstream_timeout_seconds"] = upstream_timeout

    # Final normalization.
    security["allowed_methods"] = _normalize_methods(security.get("allowed_methods"))
    security["blocked_paths"] = _dedupe(
        _normalize_blocked_paths(security.get("blocked_paths")) + DEFAULT_BLOCKED_PATHS
    )

    security["rate_limit_per_minute"] = _positive_int(
        security.get("rate_limit_per_minute"),
        int(DEFAULT_SECURITY["rate_limit_per_minute"]),
    )
    security["max_connections"] = _positive_int(
        security.get("max_connections"),
        int(DEFAULT_SECURITY["max_connections"]),
    )
    security["max_request_body_bytes"] = _positive_int(
        security.get("max_request_body_bytes"),
        int(DEFAULT_SECURITY["max_request_body_bytes"]),
    )
    security["upstream_timeout_seconds"] = _positive_int(
        security.get("upstream_timeout_seconds"),
        int(DEFAULT_SECURITY["upstream_timeout_seconds"]),
    )

    updated["security"] = security

    print("✅ Security check passed")

    return updated
