from __future__ import annotations

import copy
import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


DEFAULT_BALANCING = "round_robin"

SUPPORTED_BALANCING = {
    "round_robin",
    "weighted_round_robin",
}


def normalize_path(path: Any) -> str:
    text = str(path or "/").strip()

    if not text:
        return "/"

    if not text.startswith("/"):
        text = "/" + text

    if len(text) > 1:
        text = text.rstrip("/")

    return text


def normalize_algorithm(value: Any) -> str:
    text = str(value or DEFAULT_BALANCING).strip().lower()
    text = text.replace("-", "_").replace(" ", "_")

    if text in {"roundrobin", "rr"}:
        return "round_robin"

    if text in {"weightedroundrobin", "wrr"}:
        return "weighted_round_robin"

    if text not in SUPPORTED_BALANCING:
        return DEFAULT_BALANCING

    return text


def normalize_upstream_address(value: Any) -> str:
    if isinstance(value, dict):
        if value.get("address"):
            return normalize_upstream_address(value["address"])

        if value.get("upstream"):
            return normalize_upstream_address(value["upstream"])

        if value.get("backend"):
            return normalize_upstream_address(value["backend"])

        host = value.get("host") or value.get("hostname") or "127.0.0.1"
        port = value.get("port")

        if port is not None:
            return normalize_upstream_address(f"{host}:{port}")

    if isinstance(value, int):
        return f"127.0.0.1:{value}"

    text = str(value or "").strip()

    if not text:
        raise ValueError("Upstream address is empty.")

    if re.fullmatch(r"\d{2,5}", text):
        return f"127.0.0.1:{text}"

    text = re.sub(r"^\s*backend\s+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^\s*upstream\s+", "", text, flags=re.IGNORECASE)

    if "://" in text:
        parsed = urlparse(text)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port

        if port is None:
            raise ValueError(f"Upstream URL is missing a port: {text}")

        text = f"{host}:{port}"

    if "/" in text:
        text = text.split("/", 1)[0]

    if text.startswith("localhost:"):
        text = "127.0.0.1:" + text.split(":", 1)[1]

    if ":" not in text:
        raise ValueError(f"Upstream must be host:port or port only: {value}")

    host, port_text = text.rsplit(":", 1)

    if not host:
        host = "127.0.0.1"

    if not re.fullmatch(r"\d{1,5}", port_text):
        raise ValueError(f"Invalid upstream port: {value}")

    port = int(port_text)

    if port < 1 or port > 65535:
        raise ValueError(f"Upstream port out of range: {value}")

    return f"{host}:{port}"


def upstream_entry(address: Any, weight: int = 1) -> Dict[str, Any]:
    return {
        "address": normalize_upstream_address(address),
        "weight": int(weight or 1),
    }


def _route_uses_backend_alias(route: Dict[str, Any]) -> bool:
    return "backend" in route


def _config_uses_backend_alias(config: Dict[str, Any]) -> bool:
    for route in config.get("routes", []) or []:
        if isinstance(route, dict) and "backend" in route:
            return True

    return False


def extract_upstream_addresses(route: Dict[str, Any]) -> List[str]:
    addresses: List[str] = []

    def add(value: Any) -> None:
        try:
            address = normalize_upstream_address(value)
        except Exception:
            return

        if address not in addresses:
            addresses.append(address)

    if route.get("upstream") is not None:
        add(route.get("upstream"))

    for key in ("upstreams", "backends", "backend_upstreams"):
        values = route.get(key)

        if isinstance(values, list):
            for item in values:
                add(item)

    if route.get("backend") is not None:
        add(route.get("backend"))

    if route.get("address") is not None:
        add(route.get("address"))

    return addresses


def get_route(config: Dict[str, Any], path: str) -> Optional[Dict[str, Any]]:
    wanted = normalize_path(path)

    for route in config.get("routes", []) or []:
        if normalize_path(route.get("path")) == wanted:
            return route

    return None


def set_route_upstreams(
    route: Dict[str, Any],
    addresses: Iterable[Any],
    balancing: Optional[str] = None,
) -> Dict[str, Any]:
    preserve_backend_alias = _route_uses_backend_alias(route)

    normalized: List[str] = []

    for address in addresses:
        item = normalize_upstream_address(address)

        if item not in normalized:
            normalized.append(item)

    if not normalized:
        route.pop("upstream", None)
        route.pop("backend", None)
        route.pop("upstreams", None)
        route.pop("balancing", None)
        return route

    route["upstream"] = normalized[0]

    if preserve_backend_alias:
        route["backend"] = normalized[0]

    if len(normalized) == 1:
        route.pop("upstreams", None)

        if route.get("balancing") in {DEFAULT_BALANCING, None}:
            route.pop("balancing", None)

        return route

    route["balancing"] = normalize_algorithm(
        balancing or route.get("balancing") or route.get("algorithm")
    )
    route["upstreams"] = [upstream_entry(address) for address in normalized]

    return route


def normalize_route(route: Dict[str, Any]) -> Dict[str, Any]:
    normalized = copy.deepcopy(route)
    normalized["path"] = normalize_path(normalized.get("path", "/"))

    addresses = extract_upstream_addresses(normalized)

    if addresses:
        set_route_upstreams(
            normalized,
            addresses,
            balancing=normalized.get("balancing") or normalized.get("algorithm"),
        )

    normalized.pop("algorithm", None)
    normalized.pop("backends", None)
    normalized.pop("backend_upstreams", None)
    normalized.pop("address", None)

    return normalized


def merge_duplicate_routes(config: Dict[str, Any]) -> Dict[str, Any]:
    repaired = copy.deepcopy(config or {})
    routes = repaired.get("routes") or []

    if not isinstance(routes, list):
        repaired["routes"] = []
        return repaired

    ordered_paths: List[str] = []
    grouped: Dict[str, Dict[str, Any]] = {}
    grouped_addresses: Dict[str, List[str]] = {}

    for raw_route in routes:
        if not isinstance(raw_route, dict):
            continue

        route = normalize_route(raw_route)
        path = normalize_path(route.get("path"))

        if path not in grouped:
            grouped[path] = route
            grouped_addresses[path] = []
            ordered_paths.append(path)
        else:
            if route.get("balancing"):
                grouped[path]["balancing"] = route["balancing"]

            if _route_uses_backend_alias(route):
                grouped[path].setdefault("backend", route.get("backend"))

        for address in extract_upstream_addresses(route):
            if address not in grouped_addresses[path]:
                grouped_addresses[path].append(address)

    merged_routes: List[Dict[str, Any]] = []

    for path in ordered_paths:
        route = grouped[path]
        addresses = grouped_addresses[path]

        route["path"] = path

        if addresses:
            set_route_upstreams(
                route,
                addresses,
                balancing=route.get("balancing") or DEFAULT_BALANCING,
            )

        merged_routes.append(route)

    repaired["routes"] = merged_routes

    return repaired


def add_route_or_backend(
    config: Dict[str, Any],
    path: str,
    backend: Any,
    as_backend: bool = False,
) -> Tuple[Dict[str, Any], bool, str]:
    updated = merge_duplicate_routes(config)
    preserve_backend_alias = _config_uses_backend_alias(updated)

    path = normalize_path(path)
    address = normalize_upstream_address(backend)
    routes = updated.setdefault("routes", [])

    route = get_route(updated, path)

    if route is None:
        new_route = {
            "path": path,
            "upstream": address,
        }

        if preserve_backend_alias:
            new_route["backend"] = address

        routes.append(new_route)

        return merge_duplicate_routes(updated), True, f"Added route: {path} -> {address}"

    existing = extract_upstream_addresses(route)

    if address in existing:
        if as_backend:
            return (
                updated,
                False,
                f"Duplicate backend ignored: {path} already includes {address}",
            )

        return (
            updated,
            False,
            f"Duplicate route ignored: {path} already exists -> {address}",
        )

    if as_backend:
        if preserve_backend_alias and "backend" not in route:
            route["backend"] = route.get("upstream") or existing[0]

        existing.append(address)

        set_route_upstreams(
            route,
            existing,
            balancing=route.get("balancing") or DEFAULT_BALANCING,
        )

        return updated, True, f"Added backend to {path}: {address}"

    route["upstream"] = address

    if preserve_backend_alias or "backend" in route:
        route["backend"] = address

    route.pop("upstreams", None)
    route.pop("balancing", None)

    return updated, True, f"Updated route: {path} -> {address}"


def remove_route(config: Dict[str, Any], path: str) -> Tuple[Dict[str, Any], bool, str]:
    updated = merge_duplicate_routes(config)
    path = normalize_path(path)
    routes = updated.get("routes", [])

    new_routes = [
        route for route in routes if normalize_path(route.get("path")) != path
    ]

    if len(new_routes) == len(routes):
        return updated, False, f"Route already absent: {path}"

    updated["routes"] = new_routes
    return updated, True, f"Removed route: {path}"


def remove_backend_from_route(
    config: Dict[str, Any],
    path: str,
    backend: Any,
) -> Tuple[Dict[str, Any], bool, str]:
    updated = merge_duplicate_routes(config)
    path = normalize_path(path)
    address = normalize_upstream_address(backend)
    route = get_route(updated, path)

    if route is None:
        return updated, False, f"Route already absent: {path}"

    existing = extract_upstream_addresses(route)

    if address not in existing:
        return updated, False, f"Backend already absent from {path}: {address}"

    remaining = [item for item in existing if item != address]

    if not remaining:
        return remove_route(updated, path)

    set_route_upstreams(
        route,
        remaining,
        balancing=route.get("balancing") or DEFAULT_BALANCING,
    )

    return updated, True, f"Removed backend from {path}: {address}"


def set_route_algorithm(
    config: Dict[str, Any],
    path: str,
    algorithm: Any,
) -> Tuple[Dict[str, Any], bool, str]:
    original = copy.deepcopy(config)
    updated = merge_duplicate_routes(config)
    path = normalize_path(path)
    algorithm_name = normalize_algorithm(algorithm)
    route = get_route(updated, path)

    if route is None:
        return updated, False, f"Route already absent: {path}"

    addresses = extract_upstream_addresses(route)

    if len(addresses) <= 1:
        before_algorithm = route.get("balancing")
        route["balancing"] = algorithm_name

        changed = before_algorithm != algorithm_name or config_changed(original, updated)

        if not changed:
            return updated, False, f"Algorithm already set for {path}: {algorithm_name}"

        return updated, True, f"Set {path} algorithm to {algorithm_name}"

    before_algorithm = route.get("balancing")

    set_route_upstreams(
        route,
        addresses,
        balancing=algorithm_name,
    )

    changed = before_algorithm != algorithm_name or config_changed(original, updated)

    if not changed:
        return updated, False, f"Algorithm already set for {path}: {algorithm_name}"

    return updated, True, f"Set {path} algorithm to {algorithm_name}"


def replace_route_upstreams(
    config: Dict[str, Any],
    path: str,
    backends: Iterable[Any],
    algorithm: Any = DEFAULT_BALANCING,
) -> Tuple[Dict[str, Any], bool, str]:
    updated = merge_duplicate_routes(config)
    preserve_backend_alias = _config_uses_backend_alias(updated)

    path = normalize_path(path)
    addresses = [normalize_upstream_address(item) for item in backends]

    unique: List[str] = []

    for address in addresses:
        if address not in unique:
            unique.append(address)

    if not unique:
        return updated, False, f"No valid backends provided for {path}"

    route = get_route(updated, path)

    if route is None:
        route = {
            "path": path,
            "upstream": unique[0],
        }

        if preserve_backend_alias:
            route["backend"] = unique[0]

        updated.setdefault("routes", []).append(route)

    before = extract_upstream_addresses(route)
    before_algorithm = route.get("balancing")
    algorithm_name = normalize_algorithm(algorithm)

    set_route_upstreams(route, unique, balancing=algorithm_name)

    after = extract_upstream_addresses(route)
    after_algorithm = route.get("balancing")

    if before == after and before_algorithm == after_algorithm:
        return updated, False, f"Load balancer already configured for {path}"

    return (
        updated,
        True,
        f"Configured load balancer for {path}: {', '.join(unique)}",
    )


def config_changed(before: Dict[str, Any], after: Dict[str, Any]) -> bool:
    return json.dumps(before, sort_keys=True) != json.dumps(after, sort_keys=True)
