from __future__ import annotations

import copy
import re
from typing import Any, Dict, Iterable, List, Tuple

try:
    from langsmith import traceable
except Exception:
    def traceable(*args, **kwargs):
        def decorator(fn):
            return fn
        return decorator

from core.lb_config import (
    DEFAULT_BALANCING,
    add_route_or_backend,
    config_changed,
    extract_upstream_addresses,
    merge_duplicate_routes,
    normalize_algorithm,
    normalize_path,
    normalize_upstream_address,
    remove_backend_from_route,
    remove_route,
    replace_route_upstreams,
    set_route_algorithm,
)


HTTP_METHODS = {
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "HEAD",
}


BACKEND_TOKEN = (
    r"(?:\d{2,5}|"
    r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0|host\.docker\.internal|[A-Za-z0-9_.-]+):\d{2,5})"
)


def _dedupe(items: Iterable[str]) -> List[str]:
    seen = set()
    output = []

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


def _append_summary(summary: List[str], item: str) -> None:
    item = str(item).strip()

    if not item:
        return

    if item not in summary:
        summary.append(item)


def _extract_backend_list(text: str) -> List[str]:
    candidates = re.findall(
        BACKEND_TOKEN,
        text,
        flags=re.IGNORECASE,
    )

    normalized = []

    for candidate in candidates:
        try:
            address = normalize_upstream_address(candidate)
        except Exception:
            continue

        if address not in normalized:
            normalized.append(address)

    return normalized


def _backend_candidates(backend: str) -> List[str]:
    """
    Support removing either local or Docker-host forms.

    Examples:
    - 9104 -> 127.0.0.1:9104 and host.docker.internal:9104
    - 127.0.0.1:9104 -> 127.0.0.1:9104
    - host.docker.internal:9104 -> host.docker.internal:9104
    """

    backend = str(backend or "").strip()
    candidates: List[str] = []

    def add(value: str) -> None:
        try:
            normalized = normalize_upstream_address(value)
        except Exception:
            return

        if normalized not in candidates:
            candidates.append(normalized)

    add(backend)

    if re.fullmatch(r"\d{2,5}", backend):
        add(f"host.docker.internal:{backend}")

    return candidates


def _remove_backend_candidates_from_route(
    config: Dict[str, Any],
    path: str,
    backend: str,
) -> Tuple[Dict[str, Any], bool, str]:
    updated = config
    messages: List[str] = []
    changed_any = False

    for candidate in _backend_candidates(backend):
        updated, changed, message = remove_backend_from_route(
            updated,
            path,
            candidate,
        )

        messages.append(message)
        changed_any = changed_any or changed

        if changed:
            return updated, True, message

    return updated, False, messages[-1] if messages else f"Backend already absent from {path}: {backend}"


def _remove_backend_candidates_from_any_route(
    config: Dict[str, Any],
    backend: str,
) -> Tuple[Dict[str, Any], bool, str]:
    updated = merge_duplicate_routes(config)
    candidates = _backend_candidates(backend)

    if not candidates:
        return updated, False, f"Invalid backend: {backend}"

    routes = updated.get("routes") or []

    for route in routes:
        if not isinstance(route, dict):
            continue

        path = normalize_path(route.get("path") or "/")
        addresses = extract_upstream_addresses(route)

        for candidate in candidates:
            if candidate in addresses:
                return remove_backend_from_route(updated, path, candidate)

    return updated, False, f"Backend already absent: {', '.join(candidates)}"


def _parse_balanced_across(prompt: str) -> List[Tuple[str, List[str]]]:
    matches: List[Tuple[str, List[str]]] = []

    patterns = [
        r"(?P<path>/[A-Za-z0-9_.\-/]+)\s+(?:balanced|load[- ]?balanced|balance)\s+(?:across|between|over)\s+backends?\s+(?P<backends>[^.;]+)",
        r"(?:balance|load[- ]?balance)\s+(?P<path>/[A-Za-z0-9_.\-/]+)\s+(?:across|between|over)\s+backends?\s+(?P<backends>[^.;]+)",
        r"set\s+(?P<path>/[A-Za-z0-9_.\-/]+)\s+backends?\s+to\s+(?P<backends>[^.;]+)",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, prompt, flags=re.IGNORECASE):
            path = normalize_path(match.group("path"))
            backends = _extract_backend_list(match.group("backends"))

            if backends:
                matches.append((path, backends))

    return matches


def _apply_lb_updates(
    config: Dict[str, Any],
    prompt: str,
    summary: List[str],
) -> Tuple[Dict[str, Any], bool, bool]:
    updated = config
    changed_any = False
    understood = False

    for path, backends in _parse_balanced_across(prompt):
        updated, changed, message = replace_route_upstreams(
            updated,
            path,
            backends,
            algorithm=DEFAULT_BALANCING,
        )

        understood = True
        changed_any = changed_any or changed
        _append_summary(summary, message)

    add_backend_pattern = (
        r"\badd\s+(?:backend|upstream)\s+"
        rf"(?P<backend>{BACKEND_TOKEN})"
        r"\s+(?:to|for)\s+"
        r"(?P<path>/[A-Za-z0-9_.\-/]+)"
    )

    for match in re.finditer(add_backend_pattern, prompt, flags=re.IGNORECASE):
        updated, changed, message = add_route_or_backend(
            updated,
            match.group("path"),
            match.group("backend"),
            as_backend=True,
        )

        understood = True
        changed_any = changed_any or changed
        _append_summary(summary, message)

    # Strong backend-removal parser: exact intended form.
    remove_backend_from_route_pattern = (
        r"\bremove\s+(?:backend|upstream)\s+"
        rf"(?P<backend>{BACKEND_TOKEN})"
        r"\s+(?:from|for)\s+"
        r"(?P<path>/[A-Za-z0-9_.\-/]+)"
    )

    for match in re.finditer(remove_backend_from_route_pattern, prompt, flags=re.IGNORECASE):
        updated, changed, message = _remove_backend_candidates_from_route(
            updated,
            match.group("path"),
            match.group("backend"),
        )

        understood = True
        changed_any = changed_any or changed
        _append_summary(summary, message)

    # Optional shorthand:
    # "remove backend 9104"
    # removes that backend from whichever route contains it.
    remove_backend_anywhere_pattern = (
        r"\bremove\s+(?:backend|upstream)\s+"
        rf"(?P<backend>{BACKEND_TOKEN})"
        r"(?:\s*)$"
    )

    for match in re.finditer(remove_backend_anywhere_pattern, prompt.strip(), flags=re.IGNORECASE):
        updated, changed, message = _remove_backend_candidates_from_any_route(
            updated,
            match.group("backend"),
        )

        understood = True
        changed_any = changed_any or changed
        _append_summary(summary, message)

    algorithm_pattern = (
        r"\bset\s+"
        r"(?P<path>/[A-Za-z0-9_.\-/]+)"
        r"\s+(?:algorithm|balancing|load[- ]?balancing(?:\s+algorithm)?)\s+to\s+"
        r"(?P<algorithm>round\s*robin|round_robin|weighted\s*round\s*robin|weighted_round_robin)"
    )

    for match in re.finditer(algorithm_pattern, prompt, flags=re.IGNORECASE):
        algorithm = normalize_algorithm(match.group("algorithm"))

        updated, changed, message = set_route_algorithm(
            updated,
            match.group("path"),
            algorithm,
        )

        understood = True
        changed_any = changed_any or changed
        _append_summary(summary, message)

    return updated, changed_any, understood


def _apply_route_updates(
    config: Dict[str, Any],
    prompt: str,
    summary: List[str],
) -> Tuple[Dict[str, Any], bool, bool]:
    updated = config
    changed_any = False
    understood = False

    consumed_spans = []

    # Mark backend-removal phrases as consumed so they never become "remove /backend".
    backend_remove_patterns = [
        (
            r"\bremove\s+(?:backend|upstream)\s+"
            rf"{BACKEND_TOKEN}"
            r"\s+(?:from|for)\s+"
            r"/[A-Za-z0-9_.\-/]+"
        ),
        (
            r"\bremove\s+(?:backend|upstream)\s+"
            rf"{BACKEND_TOKEN}"
            r"(?:\s*)$"
        ),
    ]

    for pattern in backend_remove_patterns:
        for match in re.finditer(pattern, prompt.strip(), flags=re.IGNORECASE):
            consumed_spans.append(match.span())

    def is_consumed(start: int, end: int) -> bool:
        return any(start >= a and end <= b for a, b in consumed_spans)

    add_route_pattern = (
        r"\badd\s+"
        r"(?P<path>/[A-Za-z0-9_.\-/]+)"
        r"\s+(?:to\s+)?backend\s+"
        rf"(?P<backend>{BACKEND_TOKEN})"
    )

    for match in re.finditer(add_route_pattern, prompt, flags=re.IGNORECASE):
        if is_consumed(*match.span()):
            continue

        updated, changed, message = add_route_or_backend(
            updated,
            match.group("path"),
            match.group("backend"),
            as_backend=False,
        )

        understood = True
        changed_any = changed_any or changed
        _append_summary(summary, message)

    update_route_pattern = (
        r"\b(?:update|change|set)\s+"
        r"(?P<path>/[A-Za-z0-9_.\-/]+)"
        r"\s+(?:to\s+)?backend\s+"
        rf"(?P<backend>{BACKEND_TOKEN})"
    )

    for match in re.finditer(update_route_pattern, prompt, flags=re.IGNORECASE):
        updated, changed, message = add_route_or_backend(
            updated,
            match.group("path"),
            match.group("backend"),
            as_backend=False,
        )

        understood = True
        changed_any = changed_any or changed
        _append_summary(summary, message)

    remove_route_pattern = (
        r"\bremove\s+"
        r"(?!backend\b|upstream\b)"
        r"(?P<path>/?[A-Za-z0-9_.\-/]+)"
    )

    for match in re.finditer(remove_route_pattern, prompt, flags=re.IGNORECASE):
        if is_consumed(*match.span()):
            continue

        updated, changed, message = remove_route(updated, match.group("path"))

        understood = True
        changed_any = changed_any or changed
        _append_summary(summary, message)

    return updated, changed_any, understood


def _security(config: Dict[str, Any]) -> Dict[str, Any]:
    security = config.setdefault("security", {})

    security.setdefault(
        "allowed_methods",
        ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    )
    security.setdefault("blocked_paths", [])
    security.setdefault("rate_limit_per_minute", 120)
    security.setdefault("max_connections", 1000)
    security.setdefault("max_request_body_bytes", 1048576)
    security.setdefault("upstream_timeout_seconds", 30)

    return security


def _extract_paths(text: str) -> List[str]:
    paths = re.findall(r"/[A-Za-z0-9_.\-/]+", text)
    cleaned = []

    for path in paths:
        path = normalize_path(path)

        if path not in cleaned:
            cleaned.append(path)

    return cleaned


def _apply_security_updates(
    config: Dict[str, Any],
    prompt: str,
    summary: List[str],
) -> Tuple[Dict[str, Any], bool, bool]:
    updated = config
    changed_any = False
    understood = False

    block_matches = list(
        re.finditer(
            r"\b(?:block|deny)\s+(?P<paths>(?:/[A-Za-z0-9_.\-/]+(?:\s*(?:,|and)\s*)?)+)",
            prompt,
            flags=re.IGNORECASE,
        )
    )

    keep_blocked_matches = list(
        re.finditer(
            r"\bkeep\s+(?P<paths>(?:/[A-Za-z0-9_.\-/]+(?:\s*(?:,|and)\s*)?)+)\s+blocked",
            prompt,
            flags=re.IGNORECASE,
        )
    )

    method_match = re.search(
        r"\bonly\s+allow\s+(?P<methods>[A-Z,\s]+(?:and\s+[A-Z]+)?)",
        prompt,
        flags=re.IGNORECASE,
    )

    numeric_updates = [
        (
            "rate_limit_per_minute",
            r"\brate\s+limit\s+to\s+(?P<value>\d+)",
            "Security changed: rate limit",
        ),
        (
            "max_request_body_bytes",
            r"\bmax\s+request\s+body\s+to\s+(?P<value>\d+)",
            "Security changed: max request body",
        ),
        (
            "max_connections",
            r"\bmax\s+connections\s+to\s+(?P<value>\d+)",
            "Security changed: max connections",
        ),
        (
            "upstream_timeout_seconds",
            r"\bupstream\s+timeout\s+to\s+(?P<value>\d+)",
            "Security changed: upstream timeout",
        ),
    ]

    numeric_matches = [
        (key, re.search(pattern, prompt, flags=re.IGNORECASE), label)
        for key, pattern, label in numeric_updates
    ]

    security_touched = bool(
        block_matches
        or keep_blocked_matches
        or method_match
        or any(match for _, match, _ in numeric_matches)
    )

    if not security_touched:
        return updated, False, False

    security = _security(updated)

    for match in block_matches:
        paths = _extract_paths(match.group("paths"))

        if not paths:
            continue

        before = list(security.get("blocked_paths", []))
        security["blocked_paths"] = _dedupe(before + paths)

        understood = True

        if security["blocked_paths"] != before:
            changed_any = True
            _append_summary(summary, f"Security changed: blocked {', '.join(paths)}")
        else:
            _append_summary(summary, "Security unchanged: blocked paths already present")

    for match in keep_blocked_matches:
        paths = _extract_paths(match.group("paths"))

        if not paths:
            continue

        before = list(security.get("blocked_paths", []))
        security["blocked_paths"] = _dedupe(before + paths)

        understood = True

        if security["blocked_paths"] != before:
            changed_any = True
            _append_summary(summary, f"Security changed: kept blocked {', '.join(paths)}")
        else:
            _append_summary(summary, "Security unchanged: requested blocked paths already present")

    if method_match:
        raw = method_match.group("methods")
        methods = [
            item.upper()
            for item in re.findall(
                r"\b(GET|POST|PUT|PATCH|DELETE|OPTIONS|HEAD)\b",
                raw,
                flags=re.IGNORECASE,
            )
        ]

        methods = [method for method in methods if method in HTTP_METHODS]

        if methods:
            before = list(security.get("allowed_methods", []))
            security["allowed_methods"] = _dedupe(methods)

            understood = True

            if security["allowed_methods"] != before:
                changed_any = True
                _append_summary(
                    summary,
                    f"Security changed: allowed methods {', '.join(security['allowed_methods'])}",
                )
            else:
                _append_summary(summary, "Security unchanged: allowed methods already set")

    for key, match, label in numeric_matches:
        if not match:
            continue

        value = int(match.group("value"))
        before = security.get(key)

        understood = True

        if before != value:
            security[key] = value
            changed_any = True
            _append_summary(summary, f"{label}: {value}")
        else:
            _append_summary(summary, f"Security unchanged: {key} already {value}")

    return updated, changed_any, understood


@traceable(name="config_update_agent", run_type="chain")
def apply_config_update(
    current_config: Dict[str, Any],
    prompt: str,
) -> Dict[str, Any]:
    raw_before = copy.deepcopy(current_config or {})
    before = merge_duplicate_routes(copy.deepcopy(raw_before))
    updated = copy.deepcopy(raw_before)

    prompt = prompt or ""
    summary: List[str] = []

    updated, lb_changed, lb_understood = _apply_lb_updates(updated, prompt, summary)
    updated, route_changed, route_understood = _apply_route_updates(updated, prompt, summary)
    updated, security_changed, security_understood = _apply_security_updates(updated, prompt, summary)

    updated = merge_duplicate_routes(updated)

    understood = bool(lb_understood or route_understood or security_understood)

    changed = bool(
        lb_changed
        or route_changed
        or security_changed
        or config_changed(before, updated)
    )

    if not summary:
        summary = ["No effective config changes detected."]

    return {
        "config": updated,
        "changed": changed,
        "understood": understood,
        "change_summary": _dedupe(summary),
    }
