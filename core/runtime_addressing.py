from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from typing import Any, Dict, List


LOCAL_MODE = "local"
DOCKER_HOST_MODE = "docker_host"
COMPOSE_SERVICES_MODE = "compose_services"
KUBERNETES_MODE = "kubernetes"
CLOUD_MODE = "cloud"
PRESERVE_MODE = "preserve"

SUPPORTED_RUNTIME_MODES = {
    "auto",
    "local",
    "docker",
    "docker_host",
    "docker-host",
    "host_docker",
    "compose",
    "docker_compose",
    "compose_services",
    "compose-services",
    "compose_owned_backends",
    "kubernetes",
    "k8s",
    "cloud",
    "preserve",
}

LOOPBACK_HOSTS = {
    "127.0.0.1",
    "localhost",
    "0.0.0.0",
}

DOCKER_HOSTS = {
    "host.docker.internal",
}


class RuntimeAddressingError(ValueError):
    pass


@dataclass(frozen=True)
class ParsedAddress:
    raw: str
    host: str | None
    port: int
    was_bare_port: bool = False

    @property
    def address(self) -> str:
        host = self.host or "127.0.0.1"
        return f"{host}:{self.port}"


def normalize_path(path: Any) -> str:
    text = str(path or "/").strip()

    if not text:
        return "/"

    if not text.startswith("/"):
        text = "/" + text

    if len(text) > 1:
        text = text.rstrip("/")

    return text


def normalize_runtime_mode(
    runtime_mode: str | None = "auto",
    *,
    use_docker: bool = False,
    use_docker_compose: bool = False,
    use_predeploy_sandbox: bool = False,
    prompt: str | None = None,
) -> str:
    raw = str(runtime_mode or "auto").strip().lower().replace(" ", "_")

    if raw not in SUPPORTED_RUNTIME_MODES:
        raw = "auto"

    if raw == "local":
        return LOCAL_MODE

    if raw in {"docker", "docker_host", "docker-host", "host_docker"}:
        return DOCKER_HOST_MODE

    if raw in {
        "compose",
        "docker_compose",
        "compose_services",
        "compose-services",
        "compose_owned_backends",
    }:
        return COMPOSE_SERVICES_MODE

    if raw in {"kubernetes", "k8s"}:
        return KUBERNETES_MODE

    if raw == "cloud":
        return CLOUD_MODE

    if raw == "preserve":
        return PRESERVE_MODE

    prompt_text = (prompt or "").lower()

    if re.search(r"\blocal\b|\bcargo\s+run\b|\bwithout\s+docker\b", prompt_text):
        return LOCAL_MODE

    if re.search(r"\bkubernetes\b|\bk8s\b|\bsvc\.cluster\.local\b", prompt_text):
        return KUBERNETES_MODE

    if re.search(r"\bcloud\b|\binternal\s+dns\b|\bservice\s+dns\b", prompt_text):
        return CLOUD_MODE

    if re.search(r"\bcompose\b|\bservice\s+names?\b|\bgenerated\s+backends?\b", prompt_text):
        return COMPOSE_SERVICES_MODE

    if re.search(r"\bdocker\b|\bcontainer\b|\bblue/green\b|\bblue-green\b", prompt_text):
        return DOCKER_HOST_MODE

    # Current main.py default path uses Docker/blue-green.
    if use_docker or use_docker_compose or use_predeploy_sandbox:
        return DOCKER_HOST_MODE

    return LOCAL_MODE


def _raw_address_from_value(value: Any) -> str:
    if isinstance(value, dict):
        value = (
            value.get("address")
            or value.get("upstream")
            or value.get("backend")
            or value.get("target")
            or value.get("url")
        )

    text = str(value or "").strip()

    text = text.replace("http://", "")
    text = text.replace("https://", "")
    text = text.rstrip("/")

    if "/" in text:
        text = text.split("/", 1)[0]

    return text


def parse_address(value: Any) -> ParsedAddress:
    text = _raw_address_from_value(value)

    if not text:
        raise RuntimeAddressingError("Upstream address is empty.")

    if re.fullmatch(r"\d{1,5}", text):
        port = int(text)

        if port < 1 or port > 65535:
            raise RuntimeAddressingError(f"Port out of range: {text}")

        return ParsedAddress(
            raw=text,
            host=None,
            port=port,
            was_bare_port=True,
        )

    if ":" not in text:
        raise RuntimeAddressingError(
            f"Upstream must be port or host:port, got: {text}"
        )

    host, port_text = text.rsplit(":", 1)
    host = host.strip()
    port_text = port_text.strip()

    if not host:
        host = "127.0.0.1"

    if not re.fullmatch(r"\d{1,5}", port_text):
        raise RuntimeAddressingError(f"Invalid upstream port: {text}")

    port = int(port_text)

    if port < 1 or port > 65535:
        raise RuntimeAddressingError(f"Port out of range: {text}")

    if not re.fullmatch(r"[A-Za-z0-9_.-]+", host):
        raise RuntimeAddressingError(f"Invalid upstream host: {text}")

    if host == "localhost":
        host = "127.0.0.1"

    return ParsedAddress(
        raw=text,
        host=host,
        port=port,
        was_bare_port=False,
    )


def is_loopback(parsed: ParsedAddress) -> bool:
    return parsed.was_bare_port or (parsed.host or "").lower() in LOOPBACK_HOSTS


def is_docker_host(parsed: ParsedAddress) -> bool:
    return (parsed.host or "").lower() in DOCKER_HOSTS


def is_generated_backend_service(parsed: ParsedAddress) -> bool:
    host = parsed.host or ""
    return host == f"backend-{parsed.port}"


def is_explicit_service_hostname(parsed: ParsedAddress) -> bool:
    """
    Explicit service/DNS names should be preserved.

    Examples:
    - users-v1.default.svc.cluster.local:8080
    - users.internal.company.net:8080
    - backend-9101:9101
    - api:8080

    Not explicit:
    - bare 9101
    - 127.0.0.1:9101
    - localhost:9101
    - host.docker.internal:9101
    """

    if parsed.was_bare_port:
        return False

    host = (parsed.host or "").lower()

    if host in LOOPBACK_HOSTS:
        return False

    if host in DOCKER_HOSTS:
        return False

    return True


def backend_service_name(port: int) -> str:
    return f"backend-{port}"


def resolve_upstream_address(
    address: Any,
    *,
    runtime_mode: str,
) -> str:
    parsed = parse_address(address)

    if runtime_mode == PRESERVE_MODE:
        return parsed.address

    # Explicit DNS/service names are user intent. Preserve them in every mode.
    if is_explicit_service_hostname(parsed):
        return parsed.address

    if runtime_mode == LOCAL_MODE:
        return f"127.0.0.1:{parsed.port}"

    if runtime_mode == DOCKER_HOST_MODE:
        return f"host.docker.internal:{parsed.port}"

    if runtime_mode == COMPOSE_SERVICES_MODE:
        return f"{backend_service_name(parsed.port)}:{parsed.port}"

    if runtime_mode in {KUBERNETES_MODE, CLOUD_MODE}:
        raise RuntimeAddressingError(
            "Kubernetes/cloud runtime requires explicit service DNS upstreams. "
            f"Do not use bare/loopback backend {parsed.raw!r}; use something like "
            f"users-v1.default.svc.cluster.local:{parsed.port} or users.internal:{parsed.port}."
        )

    return parsed.address


def resolve_upstream_item(
    item: Any,
    *,
    runtime_mode: str,
) -> dict[str, Any]:
    if isinstance(item, dict):
        original = (
            item.get("address")
            or item.get("upstream")
            or item.get("backend")
            or item.get("target")
            or item.get("url")
        )

        resolved = dict(item)
        resolved["address"] = resolve_upstream_address(
            original,
            runtime_mode=runtime_mode,
        )

        try:
            weight = int(resolved.get("weight", 1))
        except Exception:
            weight = 1

        if weight < 1:
            weight = 1

        resolved["weight"] = weight

        resolved.pop("upstream", None)
        resolved.pop("backend", None)
        resolved.pop("target", None)
        resolved.pop("url", None)

        return resolved

    return {
        "address": resolve_upstream_address(item, runtime_mode=runtime_mode),
        "weight": 1,
    }


def _extract_route_upstream_items(route: dict[str, Any]) -> list[Any]:
    items: list[Any] = []

    upstreams = route.get("upstreams")

    if isinstance(upstreams, list):
        items.extend(upstreams)

    backends = route.get("backends")

    if isinstance(backends, list):
        items.extend(backends)

    if not items:
        if route.get("upstream") is not None:
            items.append(route["upstream"])
        elif route.get("backend") is not None:
            items.append(route["backend"])

    return items


def _dedupe_upstreams(upstreams: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    output: list[dict[str, Any]] = []

    for upstream in upstreams:
        address = str(upstream.get("address") or "").strip()

        if not address:
            continue

        if address in seen:
            continue

        seen.add(address)
        output.append(upstream)

    return output


def _addresses_from_items(items: list[Any]) -> list[str]:
    addresses: list[str] = []

    for item in items:
        try:
            address = parse_address(item).address

            if address not in addresses:
                addresses.append(address)
        except Exception:
            continue

    return addresses


def resolve_route_addresses(
    route: dict[str, Any],
    *,
    runtime_mode: str,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    resolved_route = copy.deepcopy(route)
    changes: list[dict[str, str]] = []

    resolved_route["path"] = normalize_path(
        resolved_route.get("path")
        or resolved_route.get("prefix")
        or resolved_route.get("route")
        or "/"
    )

    original_items = _extract_route_upstream_items(route)
    before_addresses = _addresses_from_items(original_items)

    if original_items:
        resolved_upstreams = [
            resolve_upstream_item(item, runtime_mode=runtime_mode)
            for item in original_items
        ]
        resolved_upstreams = _dedupe_upstreams(resolved_upstreams)

        if resolved_upstreams:
            resolved_route["upstreams"] = resolved_upstreams
            resolved_route["upstream"] = resolved_upstreams[0]["address"]

            if "backend" in resolved_route:
                resolved_route["backend"] = resolved_upstreams[0]["address"]

            if len(resolved_upstreams) > 1:
                resolved_route["balancing"] = (
                    resolved_route.get("balancing")
                    or resolved_route.get("algorithm")
                    or "round_robin"
                )

    resolved_route.pop("algorithm", None)
    resolved_route.pop("backends", None)
    resolved_route.pop("backend_upstreams", None)

    after_addresses = _addresses_from_items(resolved_route.get("upstreams", []))

    for before, after in zip(before_addresses, after_addresses):
        if before != after:
            changes.append(
                {
                    "path": str(resolved_route["path"]),
                    "from": before,
                    "to": after,
                }
            )

    return resolved_route, changes


def resolve_runtime_addresses(
    config: Dict[str, Any],
    *,
    runtime_mode: str | None = "auto",
    use_docker: bool = False,
    use_docker_compose: bool = False,
    use_predeploy_sandbox: bool = False,
    prompt: str | None = None,
    add_metadata: bool = True,
) -> Dict[str, Any]:
    """
    Return a new config with upstreams resolved for the target runtime.

    This function never mutates the input config.

    Runtime behavior:
    - local:
        9101 / 127.0.0.1:9101 -> 127.0.0.1:9101

    - docker_host:
        9101 / 127.0.0.1:9101 -> host.docker.internal:9101

    - compose_services:
        9101 / 127.0.0.1:9101 -> backend-9101:9101

    - kubernetes/cloud:
        requires explicit service DNS, for example users-v1.default.svc.cluster.local:8080

    - explicit hostname:
        preserved in every mode
    """

    resolved_mode = normalize_runtime_mode(
        runtime_mode,
        use_docker=use_docker,
        use_docker_compose=use_docker_compose,
        use_predeploy_sandbox=use_predeploy_sandbox,
        prompt=prompt,
    )

    resolved = copy.deepcopy(config or {})
    routes = resolved.get("routes") or []

    if not isinstance(routes, list):
        routes = []

    resolved_routes: list[dict[str, Any]] = []
    all_changes: list[dict[str, str]] = []

    for route in routes:
        if not isinstance(route, dict):
            continue

        resolved_route, changes = resolve_route_addresses(
            route,
            runtime_mode=resolved_mode,
        )
        resolved_routes.append(resolved_route)
        all_changes.extend(changes)

    resolved["routes"] = resolved_routes

    if add_metadata:
        metadata = resolved.get("metadata")

        if not isinstance(metadata, dict):
            metadata = {}

        metadata["runtime_addressing"] = {
            "mode": resolved_mode,
            "changed": bool(all_changes),
            "changes": all_changes,
        }

        resolved["metadata"] = metadata

    return resolved


def format_runtime_addressing_summary(config: Dict[str, Any]) -> str:
    metadata = config.get("metadata")

    if not isinstance(metadata, dict):
        return "Runtime addressing: no metadata available."

    info = metadata.get("runtime_addressing")

    if not isinstance(info, dict):
        return "Runtime addressing: no changes."

    mode = info.get("mode", "unknown")
    changes = info.get("changes") or []

    if not changes:
        return f"Runtime addressing: mode={mode}, no upstream rewrites."

    lines = [f"Runtime addressing: mode={mode}"]

    for change in changes:
        lines.append(
            f"- {change.get('path')}: {change.get('from')} -> {change.get('to')}"
        )

    return "\n".join(lines)


__all__ = [
    "LOCAL_MODE",
    "DOCKER_HOST_MODE",
    "COMPOSE_SERVICES_MODE",
    "KUBERNETES_MODE",
    "CLOUD_MODE",
    "PRESERVE_MODE",
    "RuntimeAddressingError",
    "normalize_runtime_mode",
    "resolve_upstream_address",
    "resolve_route_addresses",
    "resolve_runtime_addresses",
    "format_runtime_addressing_summary",
]
