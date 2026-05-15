from __future__ import annotations

import copy
import re
from typing import Any


VALID_METHODS = {
    "GET",
    "POST",
    "PUT",
    "PATCH",
    "DELETE",
    "OPTIONS",
    "HEAD",
}


VALID_BALANCING_ALGORITHMS = {
    "round_robin",
    "random",
    "weighted_round_robin",
    "least_connections",
    "ip_hash",
}


def normalize_path(path: Any) -> str:
    text = str(path or "/").strip()

    if not text:
        return "/"

    if not text.startswith("/"):
        text = "/" + text

    text = re.sub(r"[^a-zA-Z0-9/_\-.]", "", text)

    if not text:
        return "/"

    if not text.startswith("/"):
        text = "/" + text

    if len(text) > 1:
        text = text.rstrip("/")

    return text


def normalize_port(value: Any, default: int = 3000) -> int:
    try:
        port = int(value)
    except Exception:
        return default

    if 1 <= port <= 65535:
        return port

    return default


def normalize_upstream_address(value: Any) -> str:
    text = str(value or "127.0.0.1:3000").strip()

    text = text.replace("http://", "")
    text = text.replace("https://", "")
    text = text.rstrip("/")

    if "/" in text:
        text = text.split("/", 1)[0]

    if ":" not in text:
        port = normalize_port(text, default=3000)
        return f"127.0.0.1:{port}"

    host, port_text = text.rsplit(":", 1)

    host = host.strip()
    port = normalize_port(port_text, default=3000)

    if host == "localhost":
        host = "127.0.0.1"

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", host):
        host = "127.0.0.1"

    return f"{host}:{port}"


def is_static_route(route: dict[str, Any]) -> bool:
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


def normalize_static_route(route: dict[str, Any], path: str) -> dict[str, Any]:
    normalized = copy.deepcopy(route)

    normalized["path"] = path
    normalized["type"] = "static"
    normalized["root"] = str(
        route.get("root")
        or route.get("dir")
        or route.get("directory")
        or "public"
    )
    normalized["index"] = str(route.get("index") or "index.html")

    normalized.pop("upstream", None)
    normalized.pop("backend", None)
    normalized.pop("upstreams", None)
    normalized.pop("backends", None)
    normalized.pop("backend_upstreams", None)
    normalized.pop("balancing", None)
    normalized.pop("algorithm", None)
    normalized.pop("lb_algorithm", None)
    normalized.pop("load_balancing", None)
    normalized.pop("strategy", None)
    normalized.pop("target", None)
    normalized.pop("url", None)

    return normalized


def split_upstream_values(value: Any) -> list[Any]:
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
            item.strip()
            for item in text.split(",")
            if item.strip()
        ]

    return [text]


def normalize_upstream_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        address = (
            item.get("address")
            or item.get("upstream")
            or item.get("backend")
            or item.get("target")
            or item.get("url")
            or "127.0.0.1:3000"
        )

        try:
            weight = int(item.get("weight", 1))
        except Exception:
            weight = 1

        if weight < 1:
            weight = 1

        return {
            "address": normalize_upstream_address(address),
            "weight": weight,
        }

    return {
        "address": normalize_upstream_address(item),
        "weight": 1,
    }


def extract_route_upstreams(route: dict[str, Any]) -> list[dict[str, Any]]:
    if is_static_route(route):
        return []

    raw_items: list[Any] = []

    if isinstance(route.get("upstreams"), list):
        raw_items.extend(route["upstreams"])

    if isinstance(route.get("backends"), list):
        raw_items.extend(route["backends"])

    if isinstance(route.get("backend_upstreams"), list):
        raw_items.extend(route["backend_upstreams"])

    raw_items.extend(split_upstream_values(route.get("upstream")))
    raw_items.extend(split_upstream_values(route.get("backend")))
    raw_items.extend(split_upstream_values(route.get("target")))

    upstreams: list[dict[str, Any]] = []
    seen: set[str] = set()

    for item in raw_items:
        if isinstance(item, str) and "," in item:
            for part in split_upstream_values(item):
                upstream = normalize_upstream_item(part)
                address = upstream["address"]

                if address not in seen:
                    upstreams.append(upstream)
                    seen.add(address)

            continue

        upstream = normalize_upstream_item(item)
        address = upstream["address"]

        if address not in seen:
            upstreams.append(upstream)
            seen.add(address)
        else:
            for existing in upstreams:
                if existing["address"] == address:
                    existing["weight"] = max(
                        int(existing.get("weight", 1)),
                        int(upstream.get("weight", 1)),
                    )
                    break

    if not upstreams:
        upstreams.append(
            {
                "address": "127.0.0.1:3000",
                "weight": 1,
            }
        )

    return upstreams


def upstream_addresses(route: dict[str, Any]) -> list[str]:
    if is_static_route(route):
        return []

    addresses: list[str] = []

    for upstream in extract_route_upstreams(route):
        address = upstream["address"]

        if address not in addresses:
            addresses.append(address)

    return addresses


def normalize_balancing(value: Any, upstream_count: int = 1) -> str | None:
    text = str(value or "").strip().lower()
    text = text.replace("-", "_").replace(" ", "_")

    aliases = {
        "rr": "round_robin",
        "roundrobin": "round_robin",
        "round_robin": "round_robin",

        "rand": "random",
        "random": "random",

        "wrr": "weighted_round_robin",
        "weighted": "weighted_round_robin",
        "weighted_rr": "weighted_round_robin",
        "weighted_roundrobin": "weighted_round_robin",
        "weighted_round_robin": "weighted_round_robin",

        "lc": "least_connections",
        "least_conn": "least_connections",
        "leastconn": "least_connections",
        "least_connection": "least_connections",
        "least_connections": "least_connections",

        "iphash": "ip_hash",
        "ip_hash": "ip_hash",
        "source_ip_hash": "ip_hash",
        "sticky": "ip_hash",
        "sticky_session": "ip_hash",
    }

    normalized = aliases.get(text)

    if normalized in VALID_BALANCING_ALGORITHMS:
        return normalized

    if upstream_count > 1:
        return "round_robin"

    return None


def _route_declares_weights(upstreams: list[dict[str, Any]]) -> bool:
    return any(int(item.get("weight", 1)) != 1 for item in upstreams)


def _format_upstreams_for_route(
    upstreams: list[dict[str, Any]],
    balancing: str | None,
) -> list[str] | list[dict[str, Any]]:
    if balancing == "weighted_round_robin":
        return [
            {
                "address": item["address"],
                "weight": int(item.get("weight", 1)),
            }
            for item in upstreams
        ]

    return [item["address"] for item in upstreams]


def normalize_route(route: dict[str, Any]) -> dict[str, Any]:
    path = normalize_path(
        route.get("path")
        or route.get("prefix")
        or route.get("route")
        or "/"
    )

    if is_static_route(route):
        return normalize_static_route(route, path)

    upstreams = extract_route_upstreams(route)
    addresses = [item["address"] for item in upstreams]
    first_address = addresses[0]

    requested_balancing = (
        route.get("balancing")
        or route.get("algorithm")
        or route.get("lb_algorithm")
        or route.get("load_balancing")
    )

    balancing = normalize_balancing(
        requested_balancing,
        upstream_count=len(addresses),
    )

    if _route_declares_weights(upstreams) and len(addresses) > 1:
        balancing = "weighted_round_robin"

    normalized = copy.deepcopy(route)
    normalized["path"] = path
    normalized["upstream"] = first_address
    normalized["backend"] = first_address

    if len(addresses) > 1:
        normalized["balancing"] = balancing or "round_robin"
        normalized["upstreams"] = _format_upstreams_for_route(
            upstreams,
            normalized["balancing"],
        )
    else:
        normalized.pop("upstreams", None)
        normalized.pop("balancing", None)

    normalized.pop("algorithm", None)
    normalized.pop("lb_algorithm", None)
    normalized.pop("load_balancing", None)
    normalized.pop("strategy", None)
    normalized.pop("backends", None)
    normalized.pop("backend_upstreams", None)
    normalized.pop("target", None)
    normalized.pop("url", None)

    return normalized


def merge_duplicate_routes(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered_paths: list[str] = []
    by_path: dict[str, dict[str, Any]] = {}
    upstreams_by_path: dict[str, list[dict[str, Any]]] = {}

    for route in routes:
        if not isinstance(route, dict):
            continue

        normalized = normalize_route(route)
        path = normalized["path"]

        if path not in by_path:
            by_path[path] = normalized
            upstreams_by_path[path] = []
            ordered_paths.append(path)

        if normalized.get("type") == "static":
            by_path[path] = normalized
            upstreams_by_path[path] = []
            continue

        if by_path[path].get("type") == "static":
            continue

        for upstream in extract_route_upstreams(normalized):
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
                    int(existing.get("weight", 1)),
                    int(upstream.get("weight", 1)),
                )

    output: list[dict[str, Any]] = []

    for path in ordered_paths:
        route = copy.deepcopy(by_path[path])

        if route.get("type") == "static":
            output.append(route)
            continue

        upstreams = upstreams_by_path[path]

        if not upstreams:
            upstreams = [
                {
                    "address": route.get("upstream", "127.0.0.1:3000"),
                    "weight": 1,
                }
            ]

        addresses = [item["address"] for item in upstreams]

        route["path"] = path
        route["upstream"] = addresses[0]
        route["backend"] = addresses[0]

        balancing = normalize_balancing(
            route.get("balancing")
            or route.get("algorithm")
            or route.get("lb_algorithm")
            or route.get("load_balancing"),
            upstream_count=len(addresses),
        )

        if _route_declares_weights(upstreams) and len(addresses) > 1:
            balancing = "weighted_round_robin"

        if len(addresses) > 1:
            route["balancing"] = balancing or "round_robin"
            route["upstreams"] = _format_upstreams_for_route(
                upstreams,
                route["balancing"],
            )
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

        output.append(route)

    return output


def normalize_security(config: dict[str, Any]) -> dict[str, Any]:
    incoming = config.get("security")

    if not isinstance(incoming, dict):
        incoming = {}

    security: dict[str, Any] = {}

    methods = incoming.get("allowed_methods")

    if isinstance(methods, list):
        allowed = []

        for method in methods:
            upper = str(method).strip().upper()

            if upper in VALID_METHODS and upper not in allowed:
                allowed.append(upper)

        security["allowed_methods"] = allowed or [
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "OPTIONS",
            "HEAD",
        ]
    else:
        security["allowed_methods"] = [
            "GET",
            "POST",
            "PUT",
            "PATCH",
            "DELETE",
            "OPTIONS",
            "HEAD",
        ]

    blocked_paths = incoming.get("blocked_paths")

    security["blocked_paths"] = []

    if isinstance(blocked_paths, list):
        for path in blocked_paths:
            fixed = normalize_path(path)

            if fixed not in security["blocked_paths"]:
                security["blocked_paths"].append(fixed)

    numeric_defaults = {
        "rate_limit_per_minute": 120,
        "max_connections": 1000,
        "max_request_body_bytes": 1048576,
        "upstream_timeout_seconds": 30,
    }

    for key, default in numeric_defaults.items():
        try:
            value = int(incoming.get(key, default))
        except Exception:
            value = default

        if value < 0:
            value = default

        security[key] = value

    return security


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("Config must be a dictionary.")

    validated = copy.deepcopy(config)

    try:
        port = int(
            validated.get("port")
            or validated.get("listen_port")
            or validated.get("proxy_port")
            or 8088
        )
    except Exception:
        port = 8088

    if not (1 <= port <= 65535):
        raise ValueError(f"Invalid gateway port: {port}")

    validated["port"] = port

    routes = validated.get("routes")

    if not isinstance(routes, list):
        raise ValueError("Config must contain routes as a list.")

    normalized_routes = []

    for route in routes:
        if not isinstance(route, dict):
            continue

        normalized_routes.append(normalize_route(route))

    if not normalized_routes:
        raise ValueError("Config must contain at least one valid route.")

    validated["routes"] = merge_duplicate_routes(normalized_routes)
    validated["security"] = normalize_security(validated)

    return validated


def get_upstream_port(upstream: Any) -> int:
    address = normalize_upstream_address(upstream)
    port_text = address.rsplit(":", 1)[-1]
    return int(port_text)


__all__ = [
    "validate_config",
    "get_upstream_port",
    "normalize_path",
    "normalize_upstream_address",
    "normalize_route",
    "merge_duplicate_routes",
    "extract_route_upstreams",
    "upstream_addresses",
    "normalize_balancing",
    "VALID_BALANCING_ALGORITHMS",
    "is_static_route",
]