from __future__ import annotations

import json
import os
import re
from copy import deepcopy
from typing import Any

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

try:
    from langsmith import traceable
except Exception:
    def traceable(*args: Any, **kwargs: Any):
        def decorator(fn):
            return fn

        return decorator

try:
    from langchain_openai import ChatOpenAI
    from langchain_core.prompts import ChatPromptTemplate
except Exception:
    ChatOpenAI = None
    ChatPromptTemplate = None


ROUTE_TOKEN_PATTERN = r"(/[A-Za-z0-9._~\-/]*|[A-Za-z0-9][A-Za-z0-9._~\-/]*)"

RESERVED_DELETE_TARGETS = {
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

ADD_OR_UPDATE_ROUTE_RE = re.compile(
    rf"\b(?P<verb>add|create|route|map|update|change|set)\s+"
    rf"(?:(?:route|path|endpoint)\s+)?"
    rf"(?P<path>{ROUTE_TOKEN_PATTERN})\s+"
    rf"(?:to|->|=>|at)\s+"
    rf"(?:(?:backend|upstream|server)\s+)?"
    rf"(?P<target>(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0)?(?::)?\d+)",
    re.IGNORECASE,
)

BARE_ROUTE_TO_BACKEND_RE = re.compile(
    rf"(?<!\w)(?P<path>/[A-Za-z0-9._~\-/]*)\s+"
    rf"(?:to|->|=>)\s+"
    rf"(?:(?:backend|upstream|server)\s+)?"
    rf"(?P<target>(?:https?://)?(?:localhost|127\.0\.0\.1|0\.0\.0\.0)?(?::)?\d+)",
    re.IGNORECASE,
)

REMOVE_ROUTE_RE = re.compile(
    rf"\b(?P<verb>remove|drop|unroute|decommission)\s+"
    rf"(?:(?:route|path|endpoint)\s+)?"
    rf"(?P<path>{ROUTE_TOKEN_PATTERN})\b",
    re.IGNORECASE,
)

STOP_ROUTING_RE = re.compile(
    rf"\bstop\s+routing\s+(?:to\s+)?(?P<path>{ROUTE_TOKEN_PATTERN})\b",
    re.IGNORECASE,
)

BLOCK_PATH_RE = re.compile(
    r"\b(?:block|deny|forbid)\s+(?P<paths>[^,.]+)",
    re.IGNORECASE,
)

ONLY_ALLOW_METHODS_RE = re.compile(
    r"\bonly\s+allow\s+(?P<methods>[A-Z,\s/andor]+)",
    re.IGNORECASE,
)

RATE_LIMIT_RE = re.compile(
    r"\brate\s+limit\s+(?:to\s+)?(?P<value>\d+)\s*(?:requests?|reqs?)?\s*(?:per\s+minute|/min|rpm)?",
    re.IGNORECASE,
)

MAX_BODY_RE = re.compile(
    r"\bmax(?:imum)?\s+request\s+body\s+(?:to\s+)?(?P<value>\d+)\s*(?:bytes?)?",
    re.IGNORECASE,
)

MAX_CONNECTIONS_RE = re.compile(
    r"\bmax(?:imum)?\s+connections?\s+(?:to\s+)?(?P<value>\d+)",
    re.IGNORECASE,
)

UPSTREAM_TIMEOUT_RE = re.compile(
    r"\bupstream\s+timeout\s+(?:to\s+)?(?P<value>\d+)\s*(?:seconds?|secs?|s)?",
    re.IGNORECASE,
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


def normalize_backend_target(value: Any) -> str:
    raw = str(value or "").strip().strip("`'\".,;: ")

    if not raw:
        return ""

    if raw.isdigit():
        return f"127.0.0.1:{raw}"

    if re.fullmatch(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d+", raw):
        return raw

    if re.fullmatch(r":\d+", raw):
        return f"127.0.0.1{raw}"

    if re.fullmatch(r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d+", raw):
        return raw

    return raw


def _extract_port(target: str) -> str:
    match = re.search(r"(\d+)$", str(target or ""))
    return match.group(1) if match else ""


def _make_target_like_template(target: str, template: Any = None) -> Any:
    port = _extract_port(target)

    if not port:
        return target

    if isinstance(template, int):
        return int(port)

    if isinstance(template, str):
        stripped = template.strip()

        if stripped.isdigit():
            return port

        if stripped.startswith("http://"):
            return f"http://127.0.0.1:{port}"

        if stripped.startswith("https://"):
            return f"https://127.0.0.1:{port}"

        host_match = re.search(r"(localhost|127\.0\.0\.1|0\.0\.0\.0)", stripped)
        if host_match:
            return f"{host_match.group(1)}:{port}"

    return f"127.0.0.1:{port}"


def _get_routes_container(config: dict[str, Any]) -> tuple[Any, str]:
    if "routes" in config:
        return config["routes"], "routes"

    for parent_key in ("gateway", "proxy", "load_balancer", "app"):
        parent = config.get(parent_key)
        if isinstance(parent, dict) and "routes" in parent:
            return parent["routes"], f"{parent_key}.routes"

    config["routes"] = []
    return config["routes"], "routes"


def _set_routes_container(config: dict[str, Any], key_path: str, routes: Any) -> None:
    if key_path == "routes":
        config["routes"] = routes
        return

    parent_key, _, child_key = key_path.partition(".")

    if parent_key not in config or not isinstance(config[parent_key], dict):
        config[parent_key] = {}

    config[parent_key][child_key] = routes


def _get_route_path(route: Any) -> str:
    if isinstance(route, str):
        return normalize_route_path(route)

    if isinstance(route, dict):
        for key in ("path", "prefix", "route", "route_path", "match_path", "endpoint"):
            value = route.get(key)
            if isinstance(value, str) and value.strip():
                return normalize_route_path(value)

    return ""


def _path_key_for_route(route: dict[str, Any]) -> str:
    for key in ("path", "prefix", "route", "route_path", "match_path", "endpoint"):
        if key in route:
            return key

    return "path"


def _backend_key_for_route(route: dict[str, Any]) -> str:
    for key in (
        "backend",
        "upstream",
        "target",
        "origin",
        "server",
        "upstream_url",
        "backend_url",
        "backend_address",
    ):
        if key in route:
            return key

    if "backend_port" in route:
        return "backend_port"

    if "port" in route:
        return "port"

    return "backend"


def _build_route_from_template(path: str, target: str, routes: list[Any]) -> dict[str, Any]:
    template = next((route for route in routes if isinstance(route, dict)), None)

    if not template:
        return {
            "path": path,
            "backend": normalize_backend_target(target),
        }

    new_route = deepcopy(template)

    path_key = _path_key_for_route(new_route)
    backend_key = _backend_key_for_route(new_route)

    old_backend = new_route.get(backend_key)

    new_route[path_key] = path
    new_route[backend_key] = _make_target_like_template(target, old_backend)

    for key in ("name", "id"):
        if key in new_route:
            new_route[key] = path.strip("/") or "root"

    return new_route


def _dedupe_summary(summary: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []

    for item in summary:
        text = str(item).strip()
        key = text.lower()

        if not text or key in seen:
            continue

        seen.add(key)
        out.append(text)

    return out


def _dedupe_routes(routes: list[str]) -> list[str]:
    out: list[str] = []

    for route in routes:
        normalized = normalize_route_path(route)
        if normalized and normalized not in out:
            out.append(normalized)

    return out


def _dedupe_additions(additions: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str]] = []

    for verb, path, target in additions:
        normalized_path = normalize_route_path(path)
        normalized_target = normalize_backend_target(target)
        key = (normalized_path, normalized_target)

        if not normalized_path or not normalized_target or key in seen:
            continue

        seen.add(key)
        out.append((verb, normalized_path, normalized_target))

    return out


def _parse_route_additions(prompt: str) -> list[tuple[str, str, str]]:
    parsed: list[tuple[str, str, str]] = []

    for regex in (ADD_OR_UPDATE_ROUTE_RE, BARE_ROUTE_TO_BACKEND_RE):
        for match in regex.finditer(prompt or ""):
            verb = match.groupdict().get("verb") or "add"
            path = normalize_route_path(match.group("path"))
            target = normalize_backend_target(match.group("target"))

            if not path or not target:
                continue

            parsed.append((verb.lower(), path, target))

    return _dedupe_additions(parsed)


def _parse_route_removals(prompt: str) -> list[str]:
    removed: list[str] = []

    for regex in (REMOVE_ROUTE_RE, STOP_ROUTING_RE):
        for match in regex.finditer(prompt or ""):
            raw = match.group("path").strip().strip("`'\".,;: ")

            if raw.lower() in RESERVED_DELETE_TARGETS:
                continue

            path = normalize_route_path(raw)

            if path and path not in removed:
                removed.append(path)

    return removed


def _json_from_llm_text(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()

    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.IGNORECASE).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()

    try:
        value = json.loads(cleaned)
        return value if isinstance(value, dict) else {}
    except Exception:
        pass

    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return {}

    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _langchain_enabled() -> bool:
    if os.getenv("ENABLE_LANGCHAIN_UPDATE_AGENT", "true").lower() in {"0", "false", "no"}:
        return False

    if ChatOpenAI is None or ChatPromptTemplate is None:
        return False

    if not os.getenv("OPENAI_API_KEY"):
        return False

    return True


@traceable(name="config_update_agent_langchain_parse", run_type="llm")
def _parse_update_with_langchain(
    current_config: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    """
    LangChain parser for natural-language updates.

    LangSmith traces this automatically when:
      LANGSMITH_TRACING=true
      LANGSMITH_API_KEY=...
      LANGSMITH_PROJECT=ai-pingora-generator
      LANGSMITH_ENDPOINT=https://api.smith.langchain.com
    """

    if not _langchain_enabled():
        return {}

    model_name = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")

    llm = ChatOpenAI(
        model=model_name,
        temperature=0,
    )

    prompt_template = ChatPromptTemplate.from_messages(
        [
            (
                "system",
                """
You are a strict JSON parser for an AI-powered Pingora gateway update system.

Return JSON only. No markdown. No explanation.

Extract only explicit user intent. Do not invent routes, ports, paths, methods, or security policies.

Important safety rule:
- Do NOT interpret delete, destroy, purge, wipe, or erase as route removal.
- The CLI safety layer rewrites confirmed destructive commands to "remove /route".
- Only remove routes when the prompt uses safe words like remove, drop, unroute, decommission, or stop routing.

Return this exact JSON shape:
{
  "remove_routes": [],
  "upsert_routes": [
    {
      "path": "/example",
      "backend": "127.0.0.1:9000"
    }
  ],
  "block_paths": [],
  "allowed_methods": null,
  "rate_limit_per_minute": null,
  "max_request_body_bytes": null,
  "max_connections": null,
  "upstream_timeout_seconds": null
}

Rules:
- Normalize bare route names like analytics to /analytics.
- Normalize bare backend ports like 9001 to 127.0.0.1:9001.
- If a field is not explicitly requested, use null or [].
- Preserve existing config unless the prompt explicitly changes it.
""".strip(),
            ),
            (
                "human",
                """
Current active config:
{current_config_json}

User update prompt:
{prompt}
""".strip(),
            ),
        ]
    )

    chain = prompt_template | llm

    response = chain.invoke(
        {
            "current_config_json": json.dumps(current_config, indent=2, sort_keys=True),
            "prompt": prompt,
        }
    )

    return _json_from_llm_text(getattr(response, "content", response))


def _coerce_llm_operations(ops: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(ops, dict):
        return {}

    remove_routes = _dedupe_routes(
        [
            str(route)
            for route in ops.get("remove_routes", [])
            if isinstance(route, str)
        ]
    )

    upsert_routes: list[tuple[str, str, str]] = []

    raw_upserts = ops.get("upsert_routes", [])
    if isinstance(raw_upserts, list):
        for item in raw_upserts:
            if not isinstance(item, dict):
                continue

            path = normalize_route_path(str(item.get("path", "")))
            backend = normalize_backend_target(item.get("backend", ""))

            if path and backend:
                upsert_routes.append(("add", path, backend))

    block_paths = _dedupe_routes(
        [
            str(path)
            for path in ops.get("block_paths", [])
            if isinstance(path, str)
        ]
    )

    allowed_methods = ops.get("allowed_methods")
    if isinstance(allowed_methods, list):
        allowed_methods = [
            str(method).upper()
            for method in allowed_methods
            if str(method).upper() in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
        ]
        allowed_methods = list(dict.fromkeys(allowed_methods))
    else:
        allowed_methods = None

    def maybe_int(key: str) -> int | None:
        value = ops.get(key)
        if value is None:
            return None

        try:
            return int(value)
        except Exception:
            return None

    return {
        "remove_routes": remove_routes,
        "upsert_routes": _dedupe_additions(upsert_routes),
        "block_paths": block_paths,
        "allowed_methods": allowed_methods,
        "rate_limit_per_minute": maybe_int("rate_limit_per_minute"),
        "max_request_body_bytes": maybe_int("max_request_body_bytes"),
        "max_connections": maybe_int("max_connections"),
        "upstream_timeout_seconds": maybe_int("upstream_timeout_seconds"),
    }


def _extract_paths_from_text(text: str) -> list[str]:
    paths: list[str] = []

    ignored = {
        "and",
        "or",
        "keep",
        "blocked",
        "only",
        "allow",
        "set",
        "rate",
        "limit",
        "max",
        "request",
        "body",
        "connections",
        "upstream",
        "timeout",
        "to",
        "backend",
    }

    for token in re.findall(ROUTE_TOKEN_PATTERN, text or ""):
        raw = str(token).strip().strip("`'\".,;: ")

        if not raw or raw.lower() in ignored:
            continue

        path = normalize_route_path(raw)

        if path and path not in paths:
            paths.append(path)

    return paths


def _parse_methods(raw_methods: str) -> list[str]:
    methods: list[str] = []

    for token in re.split(r"[\s,/]+|and|or", raw_methods or "", flags=re.IGNORECASE):
        method = token.strip().upper()

        if method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}:
            methods.append(method)

    return list(dict.fromkeys(methods))


def _ensure_security(config: dict[str, Any]) -> dict[str, Any]:
    security = config.setdefault("security", {})

    if not isinstance(security, dict):
        security = {}
        config["security"] = security

    return security


def _apply_route_removals(
    config: dict[str, Any],
    requested_routes: list[str],
) -> tuple[dict[str, Any], list[str], bool]:
    updated = deepcopy(config)
    summary: list[str] = []
    changed = False

    requested_routes = _dedupe_routes(requested_routes)

    if not requested_routes:
        return updated, summary, changed

    routes, key_path = _get_routes_container(updated)

    if isinstance(routes, dict):
        existing = {
            normalize_route_path(path): path
            for path in routes.keys()
        }

        for requested in requested_routes:
            path = normalize_route_path(requested)
            original_key = existing.get(path)

            if original_key is not None:
                routes.pop(original_key, None)
                summary.append(f"Removed route: {path}")
                changed = True
            else:
                summary.append(f"Route already absent: {path}")

        _set_routes_container(updated, key_path, routes)
        return updated, summary, changed

    if isinstance(routes, list):
        for requested in requested_routes:
            path = normalize_route_path(requested)
            before = len(routes)

            routes = [
                route
                for route in routes
                if _get_route_path(route) != path
            ]

            if len(routes) < before:
                summary.append(f"Removed route: {path}")
                changed = True
            else:
                summary.append(f"Route already absent: {path}")

        _set_routes_container(updated, key_path, routes)
        return updated, summary, changed

    for requested in requested_routes:
        summary.append(f"Route already absent: {normalize_route_path(requested)}")

    return updated, summary, changed


def _apply_route_additions(
    config: dict[str, Any],
    additions: list[tuple[str, str, str]],
) -> tuple[dict[str, Any], list[str], bool]:
    updated = deepcopy(config)
    summary: list[str] = []
    changed = False

    additions = _dedupe_additions(additions)

    if not additions:
        return updated, summary, changed

    routes, key_path = _get_routes_container(updated)

    if isinstance(routes, dict):
        for _verb, path, target in additions:
            existing_key = next(
                (
                    original_key
                    for original_key in routes.keys()
                    if normalize_route_path(original_key) == path
                ),
                None,
            )

            new_target = normalize_backend_target(target)

            if existing_key is None:
                routes[path] = new_target
                summary.append(f"Added route: {path} -> {new_target}")
                changed = True
                continue

            current_target = normalize_backend_target(routes[existing_key])

            if current_target == new_target:
                summary.append(f"Route already existed: {path} -> {new_target}")
            else:
                routes[existing_key] = new_target
                summary.append(f"Updated route: {path} -> {new_target}")
                changed = True

        _set_routes_container(updated, key_path, routes)
        return updated, summary, changed

    if not isinstance(routes, list):
        routes = []

    for _verb, path, target in additions:
        existing_index = None
        existing_route = None

        for index, route in enumerate(routes):
            if _get_route_path(route) == path:
                existing_index = index
                existing_route = route
                break

        if existing_index is None:
            new_route = _build_route_from_template(path, target, routes)
            routes.append(new_route)
            summary.append(f"Added route: {path} -> {normalize_backend_target(target)}")
            changed = True
            continue

        if not isinstance(existing_route, dict):
            summary.append(f"Route already existed: {path}")
            continue

        backend_key = _backend_key_for_route(existing_route)
        current_target = normalize_backend_target(existing_route.get(backend_key))
        new_target = normalize_backend_target(
            _make_target_like_template(target, existing_route.get(backend_key))
        )

        if current_target == new_target:
            summary.append(f"Route already existed: {path} -> {new_target}")
            continue

        updated_route = deepcopy(existing_route)
        updated_route[backend_key] = _make_target_like_template(
            target,
            existing_route.get(backend_key),
        )
        routes[existing_index] = updated_route

        summary.append(f"Updated route: {path} -> {new_target}")
        changed = True

    _set_routes_container(updated, key_path, routes)
    return updated, summary, changed


def _apply_security_updates(
    config: dict[str, Any],
    prompt: str,
    llm_ops: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str], bool]:
    updated = deepcopy(config)
    security = _ensure_security(updated)
    changed = False
    changes: list[str] = []

    llm_ops = llm_ops or {}

    blocked_paths = security.get("blocked_paths", [])
    if not isinstance(blocked_paths, list):
        blocked_paths = []

    original_blocked = list(blocked_paths)

    for match in BLOCK_PATH_RE.finditer(prompt or ""):
        fragment = match.group("paths")
        for path in _extract_paths_from_text(fragment):
            if path not in blocked_paths:
                blocked_paths.append(path)

    for path in llm_ops.get("block_paths", []) or []:
        path = normalize_route_path(path)
        if path and path not in blocked_paths:
            blocked_paths.append(path)

    if blocked_paths != original_blocked:
        security["blocked_paths"] = blocked_paths
        changes.append("blocked paths updated")
        changed = True

    methods_match = ONLY_ALLOW_METHODS_RE.search(prompt or "")
    methods = _parse_methods(methods_match.group("methods")) if methods_match else []

    if not methods and isinstance(llm_ops.get("allowed_methods"), list):
        methods = llm_ops["allowed_methods"]

    if methods and security.get("allowed_methods") != methods:
        security["allowed_methods"] = methods
        changes.append("allowed methods updated")
        changed = True

    rate_value = None
    rate_match = RATE_LIMIT_RE.search(prompt or "")
    if rate_match:
        rate_value = int(rate_match.group("value"))
    elif llm_ops.get("rate_limit_per_minute") is not None:
        rate_value = int(llm_ops["rate_limit_per_minute"])

    if rate_value is not None:
        for key in ("rate_limit_per_minute", "rate_limit_rpm"):
            if security.get(key) != rate_value:
                security[key] = rate_value
                changed = True

        changes.append(f"rate limit set to {rate_value}/min")

    body_value = None
    body_match = MAX_BODY_RE.search(prompt or "")
    if body_match:
        body_value = int(body_match.group("value"))
    elif llm_ops.get("max_request_body_bytes") is not None:
        body_value = int(llm_ops["max_request_body_bytes"])

    if body_value is not None:
        for key in ("max_request_body_bytes", "max_body_bytes"):
            if security.get(key) != body_value:
                security[key] = body_value
                changed = True

        changes.append(f"max request body set to {body_value} bytes")

    connections_value = None
    connections_match = MAX_CONNECTIONS_RE.search(prompt or "")
    if connections_match:
        connections_value = int(connections_match.group("value"))
    elif llm_ops.get("max_connections") is not None:
        connections_value = int(llm_ops["max_connections"])

    if connections_value is not None:
        if security.get("max_connections") != connections_value:
            security["max_connections"] = connections_value
            changed = True

        changes.append(f"max connections set to {connections_value}")

    timeout_value = None
    timeout_match = UPSTREAM_TIMEOUT_RE.search(prompt or "")
    if timeout_match:
        timeout_value = int(timeout_match.group("value"))
    elif llm_ops.get("upstream_timeout_seconds") is not None:
        timeout_value = int(llm_ops["upstream_timeout_seconds"])

    if timeout_value is not None:
        for key in ("upstream_timeout_seconds", "upstream_timeout"):
            if security.get(key) != timeout_value:
                security[key] = timeout_value
                changed = True

        changes.append(f"upstream timeout set to {timeout_value}s")

    summary = [f"Security changed: {', '.join(_dedupe_summary(changes))}"] if changes else []
    return updated, summary, changed


@traceable(name="config_update_agent", run_type="chain")
def apply_config_update(
    current_config: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    """
    Main update agent.

    Uses:
    1. deterministic parsing for critical route/security updates
    2. LangChain parser for flexible natural-language extraction
    3. LangSmith tracing through @traceable and ChatOpenAI
    """

    if not isinstance(current_config, dict):
        current_config = {}

    prompt = prompt or ""
    updated = deepcopy(current_config)

    deterministic_removals = _parse_route_removals(prompt)
    deterministic_additions = _parse_route_additions(prompt)

    llm_ops = _coerce_llm_operations(
        _parse_update_with_langchain(current_config, prompt)
    )

    route_removals = _dedupe_routes(
        deterministic_removals + llm_ops.get("remove_routes", [])
    )

    route_additions = _dedupe_additions(
        deterministic_additions + llm_ops.get("upsert_routes", [])
    )

    updated, removal_summary, removal_changed = _apply_route_removals(
        updated,
        route_removals,
    )

    updated, addition_summary, addition_changed = _apply_route_additions(
        updated,
        route_additions,
    )

    updated, security_summary, security_changed = _apply_security_updates(
        updated,
        prompt,
        llm_ops=llm_ops,
    )

    understood = bool(
        route_removals
        or route_additions
        or security_summary
        or llm_ops.get("block_paths")
        or llm_ops.get("allowed_methods")
    )

    changed = removal_changed or addition_changed or security_changed

    change_summary = _dedupe_summary(
        removal_summary + addition_summary + security_summary
    )

    if not change_summary:
        change_summary = ["No effective config changes detected."]

    return {
        "config": updated,
        "change_summary": change_summary,
        "changed": changed,
        "understood": understood,
        "langchain_enabled": _langchain_enabled(),
    }


def update_config_from_prompt(
    current_config: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    return apply_config_update(current_config, prompt)


def update_config_agent(
    current_config: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    return apply_config_update(current_config, prompt)


def config_update_agent(
    current_config: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    return apply_config_update(current_config, prompt)


def run_config_update_agent(
    current_config: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    return apply_config_update(current_config, prompt)


def update_config(
    current_config: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    return apply_config_update(current_config, prompt)


def apply_update(
    current_config: dict[str, Any],
    prompt: str,
) -> dict[str, Any]:
    return apply_config_update(current_config, prompt)


if __name__ == "__main__":
    sample_config = {
        "routes": [
            {"path": "/", "backend": "127.0.0.1:3000"},
            {"path": "/analytics", "backend": "127.0.0.1:9400"},
        ],
        "security": {
            "blocked_paths": ["/private", "/internal"],
            "allowed_methods": ["GET", "POST"],
        },
    }

    sample_prompt = "remove analytics, add /inventory to backend 9001, block /secret and /debug"

    print(
        json.dumps(
            apply_config_update(sample_config, sample_prompt),
            indent=2,
        )
    )