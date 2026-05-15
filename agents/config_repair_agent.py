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
    normalize_path,
    normalize_upstream_address,
)


BACKEND_TOKEN = (
    r"(?:(?:localhost|127\.0\.0\.1|0\.0\.0\.0|host\.docker\.internal|[A-Za-z0-9_.-]+):\d{2,5}"
    r"|\b\d{2,5}\b)"
)

VALID_BALANCING_ALGORITHMS = {
    "round_robin",
    "random",
    "weighted_round_robin",
    "least_connections",
    "ip_hash",
}


# ---------------------------------------------------------------------
# Static webserver intent
# ---------------------------------------------------------------------


def _prompt_wants_static_webserver(prompt: str | None) -> bool:
    text = str(prompt or "").lower()

    return any(
        marker in text
        for marker in [
            "webserver",
            "web server",
            "static site",
            "static website",
            "serve static",
            "serve files",
            "serve public",
            "serving public",
            "file server",
            "static web",
        ]
    )


def _prompt_explicitly_proxies_root(prompt: str | None) -> bool:
    """
    Do not convert "/" to static if the user clearly wants "/" proxied/LB'd.
    """

    text = str(prompt or "").lower()

    patterns = [
        r"/\s+to\s+backend",
        r"/\s+to\s+upstream",
        r"/\s+proxy\s+to",
        r"/\s+proxied\s+to",
        r"/\s+balanced\s+across",
        r"/\s+load[- ]?balanced",
        r"/\s+using\s+(?:round\s+robin|weighted\s+round\s+robin|random|ip\s+hash|least\s+connections?)",
    ]

    return any(re.search(pattern, text) for pattern in patterns)


def _static_root_from_prompt(prompt: str | None) -> str:
    text = str(prompt or "")

    patterns = [
        r"(?:serving|serve)\s+(?P<root>[A-Za-z0-9_./-]+)\s+at\s+/",
        r"(?:from|root)\s+(?P<root>[A-Za-z0-9_./-]+)",
        r"static\s+files\s+from\s+(?P<root>[A-Za-z0-9_./-]+)",
        r"public\s+folder\s+(?P<root>[A-Za-z0-9_./-]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)

        if match:
            root = match.group("root").strip().strip("\"'")

            if root:
                return root

    return "public"


def _is_static_route(route: Dict[str, Any]) -> bool:
    route_type = str(
        route.get("type")
        or route.get("kind")
        or route.get("mode")
        or ""
    ).strip().lower()

    return route_type in {
        "static",
        "web",
        "webserver",
        "web_server",
        "file_server",
        "files",
    }


def _normalize_static_route(route: Dict[str, Any]) -> Dict[str, Any]:
    path = normalize_path(
        route.get("path")
        or route.get("prefix")
        or route.get("route")
        or "/"
    )

    fixed = copy.deepcopy(route)
    fixed["path"] = path
    fixed["type"] = "static"
    fixed["root"] = str(
        route.get("root")
        or route.get("dir")
        or route.get("directory")
        or "public"
    )
    fixed["index"] = str(route.get("index") or "index.html")

    fixed.pop("upstream", None)
    fixed.pop("backend", None)
    fixed.pop("upstreams", None)
    fixed.pop("backends", None)
    fixed.pop("backend_upstreams", None)
    fixed.pop("balancing", None)
    fixed.pop("algorithm", None)
    fixed.pop("lb_algorithm", None)
    fixed.pop("load_balancing", None)
    fixed.pop("strategy", None)
    fixed.pop("target", None)
    fixed.pop("url", None)

    return fixed


def _ensure_static_webserver_route(
    config: Dict[str, Any],
    prompt: str | None,
) -> Dict[str, Any]:
    """
    If the user asks for a webserver/static server, create a root static route.

    Does not override explicit:
      / to backend 127.0.0.1:9000
      / balanced across ...
    """

    if not _prompt_wants_static_webserver(prompt):
        return config

    if _prompt_explicitly_proxies_root(prompt):
        return config

    repaired = copy.deepcopy(config)

    routes = repaired.get("routes")

    if not isinstance(routes, list):
        repaired["routes"] = []
        routes = repaired["routes"]

    root = _static_root_from_prompt(prompt)

    root_route = None

    for route in routes:
        if not isinstance(route, dict):
            continue

        route_path = normalize_path(
            route.get("path")
            or route.get("prefix")
            or route.get("route")
            or "/"
        )

        if route_path == "/":
            root_route = route
            break

    if root_route is None:
        root_route = {}
        routes.insert(0, root_route)

    root_route["path"] = "/"
    root_route["type"] = "static"
    root_route["root"] = root
    root_route["index"] = "index.html"

    root_route.pop("upstream", None)
    root_route.pop("backend", None)
    root_route.pop("upstreams", None)
    root_route.pop("backends", None)
    root_route.pop("backend_upstreams", None)
    root_route.pop("balancing", None)
    root_route.pop("algorithm", None)
    root_route.pop("lb_algorithm", None)
    root_route.pop("load_balancing", None)
    root_route.pop("strategy", None)
    root_route.pop("target", None)
    root_route.pop("url", None)

    return repaired


# ---------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------


def _dedupe_strings(values: Iterable[str]) -> List[str]:
    output: List[str] = []
    seen: set[str] = set()

    for value in values:
        text = str(value or "").strip()

        if not text:
            continue

        key = text.lower()

        if key in seen:
            continue

        seen.add(key)
        output.append(text)

    return output


def _normalize_algorithm(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    text = text.replace("-", "_").replace(" ", "_")

    aliases = {
        "rr": "round_robin",
        "roundrobin": "round_robin",
        "round_robin": "round_robin",

        "random": "random",
        "rand": "random",

        "weighted": "weighted_round_robin",
        "weighted_rr": "weighted_round_robin",
        "weighted_roundrobin": "weighted_round_robin",
        "weighted_round_robin": "weighted_round_robin",
        "wrr": "weighted_round_robin",

        "least_connection": "least_connections",
        "least_connections": "least_connections",
        "least_conn": "least_connections",
        "leastconn": "least_connections",
        "lc": "least_connections",

        "ip_hash": "ip_hash",
        "iphash": "ip_hash",
        "source_ip_hash": "ip_hash",
        "sticky": "ip_hash",
        "sticky_session": "ip_hash",
    }

    normalized = aliases.get(text)

    if normalized in VALID_BALANCING_ALGORITHMS:
        return normalized

    return None


def _infer_balancing_algorithm_from_text(text: str | None) -> str | None:
    value = str(text or "").lower()
    value = value.replace("_", " ").replace("-", " ")

    if "weighted round robin" in value:
        return "weighted_round_robin"

    if "least connections" in value or "least connection" in value:
        return "least_connections"

    if "random" in value:
        return "random"

    if "round robin" in value:
        return "round_robin"

    if "ip hash" in value or "sticky" in value:
        return "ip_hash"

    return None


def _route_algorithm(route: Dict[str, Any]) -> str | None:
    return _normalize_algorithm(
        route.get("balancing")
        or route.get("algorithm")
        or route.get("lb_algorithm")
        or route.get("load_balancing")
        or route.get("strategy")
    )


def _normalize_weight(value: Any, default: int = 1) -> int:
    try:
        weight = int(value)
    except Exception:
        weight = default

    if weight < 1:
        weight = 1

    return weight


def _normalize_upstream_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        address = (
            item.get("address")
            or item.get("upstream")
            or item.get("backend")
            or item.get("target")
            or item.get("url")
            or "127.0.0.1:3000"
        )

        return {
            "address": normalize_upstream_address(address),
            "weight": _normalize_weight(item.get("weight", 1)),
        }

    return {
        "address": normalize_upstream_address(item),
        "weight": 1,
    }


def _split_upstream_values(value: Any) -> list[Any]:
    if value is None:
        return []

    if isinstance(value, list):
        return value

    if isinstance(value, tuple):
        return list(value)

    text = str(value).strip()

    if not text:
        return []

    if "," in text:
        return [
            part.strip()
            for part in text.split(",")
            if part.strip()
        ]

    return [text]


def _extract_backend_list(text: str) -> List[str]:
    candidates = re.findall(
        BACKEND_TOKEN,
        text or "",
        flags=re.IGNORECASE,
    )

    normalized: List[str] = []

    for candidate in candidates:
        try:
            address = normalize_upstream_address(candidate)
        except Exception:
            continue

        if address not in normalized:
            normalized.append(address)

    return normalized


def _extract_weighted_backend_items(text: str) -> list[dict[str, Any]]:
    """
    Parses:

      127.0.0.1:9101 weight 5,
      127.0.0.1:9102 weight 2,
      127.0.0.1:9103 weight 1
    """

    items: list[dict[str, Any]] = []

    pattern = re.compile(
        rf"(?P<backend>{BACKEND_TOKEN})"
        r"(?:\s+(?:weight|weighted|w)\s+(?P<weight>\d+))?",
        flags=re.IGNORECASE,
    )

    seen: set[str] = set()

    for match in pattern.finditer(text or ""):
        backend = match.group("backend")

        try:
            address = normalize_upstream_address(backend)
        except Exception:
            continue

        if address in seen:
            continue

        seen.add(address)

        weight = _normalize_weight(match.group("weight") or 1)

        items.append(
            {
                "address": address,
                "weight": weight,
            }
        )

    return items


def _extract_route_field_upstreams(route: Dict[str, Any]) -> List[dict[str, Any]]:
    if _is_static_route(route):
        return []

    raw_items: list[Any] = []

    for key in ("upstreams", "backends", "backend_upstreams"):
        value = route.get(key)

        if isinstance(value, list):
            raw_items.extend(value)
        elif value:
            raw_items.append(value)

    for key in ("upstream", "backend", "target"):
        raw_items.extend(_split_upstream_values(route.get(key)))

    output: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in raw_items:
        if isinstance(item, str) and "," in item:
            parts = _split_upstream_values(item)
        else:
            parts = [item]

        for part in parts:
            try:
                fixed = _normalize_upstream_item(part)
            except Exception:
                continue

            address = fixed["address"]

            if address not in seen:
                seen.add(address)
                output.append(fixed)
            else:
                for existing in output:
                    if existing["address"] == address:
                        existing["weight"] = max(
                            int(existing.get("weight", 1)),
                            int(fixed.get("weight", 1)),
                        )
                        break

    return output


def _format_upstreams_for_algorithm(
    upstreams: list[dict[str, Any]],
    algorithm: str | None,
) -> list[str] | list[dict[str, Any]]:
    if algorithm == "weighted_round_robin":
        return [
            {
                "address": item["address"],
                "weight": _normalize_weight(item.get("weight", 1)),
            }
            for item in upstreams
        ]

    return [item["address"] for item in upstreams]


def _route_declares_weights(upstreams: list[dict[str, Any]]) -> bool:
    return any(_normalize_weight(item.get("weight", 1)) != 1 for item in upstreams)


def _normalize_route(route: Dict[str, Any]) -> Dict[str, Any]:
    if _is_static_route(route):
        return _normalize_static_route(route)

    path = normalize_path(
        route.get("path")
        or route.get("prefix")
        or route.get("route")
        or "/"
    )

    upstreams = _extract_route_field_upstreams(route)

    if not upstreams:
        upstreams = [
            {
                "address": "127.0.0.1:3000",
                "weight": 1,
            }
        ]

    algorithm = _route_algorithm(route)

    if len(upstreams) > 1 and _route_declares_weights(upstreams):
        algorithm = "weighted_round_robin"

    if len(upstreams) > 1 and not algorithm:
        algorithm = DEFAULT_BALANCING

    fixed = copy.deepcopy(route)
    fixed["path"] = path
    fixed["upstream"] = upstreams[0]["address"]
    fixed["backend"] = upstreams[0]["address"]

    if len(upstreams) > 1:
        fixed["balancing"] = algorithm or DEFAULT_BALANCING
        fixed["upstreams"] = _format_upstreams_for_algorithm(upstreams, fixed["balancing"])
    else:
        fixed.pop("upstreams", None)
        fixed.pop("balancing", None)

    fixed.pop("algorithm", None)
    fixed.pop("lb_algorithm", None)
    fixed.pop("load_balancing", None)
    fixed.pop("strategy", None)
    fixed.pop("backends", None)
    fixed.pop("backend_upstreams", None)
    fixed.pop("target", None)
    fixed.pop("url", None)

    return fixed


def _ensure_config_shape(config: Dict[str, Any]) -> Dict[str, Any]:
    repaired = copy.deepcopy(config or {})

    if "port" not in repaired:
        repaired["port"] = 8088

    try:
        repaired["port"] = int(repaired["port"])
    except Exception:
        repaired["port"] = 8088

    if "routes" not in repaired or not isinstance(repaired.get("routes"), list):
        repaired["routes"] = []

    normalized_routes = []

    for route in repaired.get("routes", []):
        if not isinstance(route, dict):
            continue

        normalized_routes.append(_normalize_route(route))

    repaired["routes"] = normalized_routes

    if "security" in repaired and repaired["security"] is None:
        repaired["security"] = {}

    return repaired


def _merge_duplicate_routes(config: Dict[str, Any]) -> Dict[str, Any]:
    repaired = copy.deepcopy(config)
    routes = repaired.get("routes") or []

    ordered_paths: list[str] = []
    route_by_path: dict[str, dict[str, Any]] = {}
    upstreams_by_path: dict[str, list[dict[str, Any]]] = {}

    if not isinstance(routes, list):
        repaired["routes"] = []
        return repaired

    for route in routes:
        if not isinstance(route, dict):
            continue

        normalized = _normalize_route(route)
        path = normalized["path"]

        if path not in route_by_path:
            route_by_path[path] = normalized
            upstreams_by_path[path] = []
            ordered_paths.append(path)

        if normalized.get("type") == "static":
            route_by_path[path] = normalized
            upstreams_by_path[path] = []
            continue

        if route_by_path[path].get("type") == "static":
            continue

        current_algorithm = _route_algorithm(normalized)

        if current_algorithm:
            route_by_path[path]["balancing"] = current_algorithm

        for upstream in _extract_route_field_upstreams(normalized):
            address = upstream["address"]

            existing = next(
                (
                    item
                    for item in upstreams_by_path[path]
                    if item["address"] == address
                ),
                None,
            )

            if existing is None:
                upstreams_by_path[path].append(upstream)
            else:
                existing["weight"] = max(
                    _normalize_weight(existing.get("weight", 1)),
                    _normalize_weight(upstream.get("weight", 1)),
                )

    merged_routes: list[dict[str, Any]] = []

    for path in ordered_paths:
        route = copy.deepcopy(route_by_path[path])

        if route.get("type") == "static":
            merged_routes.append(route)
            continue

        upstreams = upstreams_by_path[path]

        if not upstreams:
            upstreams = [
                {
                    "address": route.get("upstream", "127.0.0.1:3000"),
                    "weight": 1,
                }
            ]

        algorithm = _route_algorithm(route)

        if len(upstreams) > 1 and _route_declares_weights(upstreams):
            algorithm = "weighted_round_robin"

        if len(upstreams) > 1 and not algorithm:
            algorithm = DEFAULT_BALANCING

        route["path"] = path
        route["upstream"] = upstreams[0]["address"]
        route["backend"] = upstreams[0]["address"]

        if len(upstreams) > 1:
            route["balancing"] = algorithm or DEFAULT_BALANCING
            route["upstreams"] = _format_upstreams_for_algorithm(upstreams, route["balancing"])
        else:
            route.pop("upstreams", None)
            route.pop("balancing", None)

        route.pop("algorithm", None)
        route.pop("lb_algorithm", None)
        route.pop("load_balancing", None)
        route.pop("strategy", None)
        route.pop("backends", None)
        route.pop("backend_upstreams", None)
        route.pop("target", None)
        route.pop("url", None)

        merged_routes.append(route)

    repaired["routes"] = merged_routes
    return repaired


# ---------------------------------------------------------------------
# Prompt load-balancer intent
# ---------------------------------------------------------------------


def _balanced_clause_stop_pattern() -> str:
    return (
        r"(?="
        r"\s*,\s*(?:and\s+)?/[A-Za-z0-9_.\-/]+\s+"
        r"(?:balanced|load[- ]?balanced|load\s+balance|balance|to\s+backend|to\s+backends|backend|using)"
        r"|"
        r"\s+and\s+/[A-Za-z0-9_.\-/]+\s+"
        r"(?:balanced|load[- ]?balanced|load\s+balance|balance|to\s+backend|to\s+backends|backend|using)"
        r"|"
        r"\.\s*"
        r"|"
        r"\bBlock\b"
        r"|"
        r"\bOnly\s+allow\b"
        r"|"
        r"\bSet\s+rate\b"
        r"|"
        r"\bset\s+rate\b"
        r"|"
        r"$"
        r")"
    )


def _parse_balanced_routes_from_prompt(
    prompt: str | None,
) -> List[Tuple[str, List[Any], str]]:
    if not prompt:
        return []

    matches: List[Tuple[str, List[Any], str]] = []
    stop = _balanced_clause_stop_pattern()

    patterns = [
        # "/api balanced across backends 9101, 9102, 9103 using random"
        rf"(?P<path>/[A-Za-z0-9_.\-/]*)\s+"
        rf"(?:balanced|load[- ]?balanced|balance)\s+"
        rf"(?:across|between|over)\s+(?:backends?\s+)?"
        rf"(?P<backends>.*?){stop}",

        # "balance /api across 9101, 9102, 9103 using random"
        rf"(?:balance|load[- ]?balance)\s+"
        rf"(?P<path>/[A-Za-z0-9_.\-/]*)\s+"
        rf"(?:across|between|over)\s+(?:backends?\s+)?"
        rf"(?P<backends>.*?){stop}",

        # "load balance /api across 9101, 9102, 9103 using random"
        rf"(?:load\s+balance|load[- ]?balance)\s+"
        rf"(?P<path>/[A-Za-z0-9_.\-/]*)\s+"
        rf"(?:across|between|over)\s+(?:backends?\s+)?"
        rf"(?P<backends>.*?){stop}",

        # "set /api backends to 9101, 9102, 9103"
        rf"set\s+"
        rf"(?P<path>/[A-Za-z0-9_.\-/]*)\s+"
        rf"backends?\s+to\s+"
        rf"(?P<backends>.*?){stop}",

        # "/api using random across 9101, 9102, 9103"
        rf"(?P<path>/[A-Za-z0-9_.\-/]*)\s+"
        rf"using\s+"
        rf"(?P<algorithm>weighted\s+round\s+robin|round\s+robin|least\s+connections?|ip\s+hash|random|sticky)\s+"
        rf"(?:across|between|over)\s+(?:backends?\s+)?"
        rf"(?P<backends>.*?){stop}",

        # "with /api using random across 9101, 9102, 9103"
        rf"with\s+"
        rf"(?P<path>/[A-Za-z0-9_.\-/]*)\s+"
        rf"using\s+"
        rf"(?P<algorithm>weighted\s+round\s+robin|round\s+robin|least\s+connections?|ip\s+hash|random|sticky)\s+"
        rf"(?:across|between|over)\s+(?:backends?\s+)?"
        rf"(?P<backends>.*?){stop}",
    ]

    for pattern in patterns:
        for match in re.finditer(pattern, prompt, flags=re.IGNORECASE | re.DOTALL):
            path = normalize_path(match.group("path") or "/")
            backend_text = match.group("backends")

            algorithm = (
                _infer_balancing_algorithm_from_text(match.groupdict().get("algorithm"))
                or _infer_balancing_algorithm_from_text(backend_text)
                or _infer_balancing_algorithm_from_text(prompt)
                or DEFAULT_BALANCING
            )

            if algorithm == "weighted_round_robin":
                backends: List[Any] = _extract_weighted_backend_items(backend_text)
            else:
                backends = _extract_backend_list(backend_text)

            if len(backends) >= 2:
                item = (path, backends, algorithm)

                if item not in matches:
                    matches.append(item)

    return matches


def _set_route_upstreams(
    config: Dict[str, Any],
    path: str,
    upstreams: List[Any],
    algorithm: str,
) -> Dict[str, Any]:
    repaired = copy.deepcopy(config)

    routes = repaired.get("routes")

    if not isinstance(routes, list):
        repaired["routes"] = []
        routes = repaired["routes"]

    normalized_path = normalize_path(path)
    normalized_upstreams = [_normalize_upstream_item(item) for item in upstreams]

    if len(normalized_upstreams) < 2:
        return repaired

    if algorithm == "weighted_round_robin":
        route_upstreams: list[str] | list[dict[str, Any]] = [
            {
                "address": item["address"],
                "weight": _normalize_weight(item.get("weight", 1)),
            }
            for item in normalized_upstreams
        ]
    else:
        route_upstreams = [item["address"] for item in normalized_upstreams]

    target_route = None

    for route in routes:
        if not isinstance(route, dict):
            continue

        route_path = normalize_path(
            route.get("path")
            or route.get("prefix")
            or route.get("route")
            or "/"
        )

        if route_path == normalized_path:
            target_route = route
            break

    if target_route is None:
        target_route = {"path": normalized_path}
        routes.append(target_route)

    target_route["path"] = normalized_path
    target_route["upstream"] = normalized_upstreams[0]["address"]
    target_route["backend"] = normalized_upstreams[0]["address"]
    target_route["upstreams"] = route_upstreams
    target_route["balancing"] = algorithm

    target_route.pop("type", None)
    target_route.pop("root", None)
    target_route.pop("index", None)
    target_route.pop("algorithm", None)
    target_route.pop("lb_algorithm", None)
    target_route.pop("load_balancing", None)
    target_route.pop("strategy", None)
    target_route.pop("backends", None)
    target_route.pop("backend_upstreams", None)
    target_route.pop("target", None)
    target_route.pop("url", None)

    return repaired


def _current_backend_count(config: Dict[str, Any], path: str) -> int:
    target_path = normalize_path(path)

    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        route_path = normalize_path(
            route.get("path")
            or route.get("prefix")
            or route.get("route")
            or "/"
        )

        if route_path == target_path:
            return len(_extract_route_field_upstreams(route))

    return 0


def _current_balancing(config: Dict[str, Any], path: str) -> str | None:
    target_path = normalize_path(path)

    for route in config.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        route_path = normalize_path(
            route.get("path")
            or route.get("prefix")
            or route.get("route")
            or "/"
        )

        if route_path == target_path:
            return _route_algorithm(route)

    return None


def _force_route_algorithm(
    config: Dict[str, Any],
    path: str,
    algorithm: str,
) -> Dict[str, Any]:
    repaired = copy.deepcopy(config)
    target_path = normalize_path(path)

    for route in repaired.get("routes", []) or []:
        if not isinstance(route, dict):
            continue

        if _is_static_route(route):
            continue

        route_path = normalize_path(
            route.get("path")
            or route.get("prefix")
            or route.get("route")
            or "/"
        )

        if route_path == target_path:
            route["balancing"] = algorithm

    return repaired


def _apply_prompt_load_balancer_intent(
    config: Dict[str, Any],
    prompt: str | None,
) -> Dict[str, Any]:
    repaired = copy.deepcopy(config)

    for path, backends, algorithm in _parse_balanced_routes_from_prompt(prompt):
        current_count = _current_backend_count(repaired, path)
        current_algorithm = _current_balancing(repaired, path)

        should_update_backends = len(backends) > current_count
        should_update_algorithm = algorithm and algorithm != current_algorithm

        if should_update_backends or should_update_algorithm:
            repaired = _set_route_upstreams(
                repaired,
                path,
                backends,
                algorithm=algorithm or DEFAULT_BALANCING,
            )

        if algorithm:
            repaired = _force_route_algorithm(repaired, path, algorithm)

    return repaired


def _restore_prompt_algorithms_after_merge(
    config: Dict[str, Any],
    prompt: str | None,
) -> Dict[str, Any]:
    repaired = copy.deepcopy(config)

    for path, _backends, algorithm in _parse_balanced_routes_from_prompt(prompt):
        if algorithm:
            repaired = _force_route_algorithm(repaired, path, algorithm)

    return repaired


@traceable(name="config_repair_agent", run_type="chain")
def repair_config(
    config: Dict[str, Any],
    prompt: str | None = None,
) -> Dict[str, Any]:
    repaired = _ensure_config_shape(config)

    repaired = _ensure_static_webserver_route(repaired, prompt)

    repaired = _merge_duplicate_routes(repaired)
    repaired = _restore_prompt_algorithms_after_merge(repaired, prompt)

    repaired = _apply_prompt_load_balancer_intent(repaired, prompt)

    repaired = _merge_duplicate_routes(repaired)
    repaired = _restore_prompt_algorithms_after_merge(repaired, prompt)

    repaired = _apply_prompt_load_balancer_intent(repaired, prompt)

    repaired = _merge_duplicate_routes(repaired)
    repaired = _restore_prompt_algorithms_after_merge(repaired, prompt)

    repaired = _ensure_static_webserver_route(repaired, prompt)

    return repaired