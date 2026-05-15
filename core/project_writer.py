from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"


DEFAULT_SECURITY = {
    "blocked_paths": [],
    "allowed_methods": ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"],
    "rate_limit_per_minute": 120,
    "max_connections": 1000,
    "max_request_body_bytes": 1048576,
    "upstream_timeout_seconds": 30,
}


DEFAULT_DEMO_BACKEND = {
    "enabled": True,
    "generate_placeholder_files": True,
    "overwrite_existing": False,
}


VALID_BALANCING_ALGORITHMS = {
    "round_robin",
    "random",
    "weighted_round_robin",
    "least_connections",
    "ip_hash",
}


def rust_string(value: Any) -> str:
    return json.dumps(str(value))


def normalize_path(path: Any) -> str:
    path = str(path or "/").strip()

    if not path.startswith("/"):
        path = "/" + path

    path = re.sub(r"[^a-zA-Z0-9/_\-.]", "", path)

    if not path:
        return "/"

    if not path.startswith("/"):
        path = "/" + path

    if len(path) > 1:
        path = path.rstrip("/")

    return path


def normalize_port(value: Any, default: int = 9000) -> int:
    try:
        port = int(value)
    except Exception:
        return default

    if 1 <= port <= 65535:
        return port

    return default


def normalize_upstream_address(value: Any) -> str:
    upstream = str(value or "127.0.0.1:3000").strip()

    upstream = upstream.replace("http://", "")
    upstream = upstream.replace("https://", "")
    upstream = upstream.rstrip("/")

    if "/" in upstream:
        upstream = upstream.split("/", 1)[0]

    if ":" not in upstream:
        return "127.0.0.1:3000"

    host, port_text = upstream.rsplit(":", 1)

    host = host.strip()
    port = normalize_port(port_text, default=3000)

    if host == "localhost":
        host = "127.0.0.1"

    if not re.fullmatch(r"[a-zA-Z0-9_.-]+", host):
        host = "127.0.0.1"

    return f"{host}:{port}"


def normalize_balancing(value: Any) -> str:
    text = str(value or "round_robin").strip().lower()
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

    normalized = aliases.get(text, "round_robin")

    if normalized not in VALID_BALANCING_ALGORITHMS:
        return "round_robin"

    return normalized


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


def normalize_static_route(route: dict[str, Any]) -> dict[str, Any]:
    path = normalize_path(
        route.get("path")
        or route.get("prefix")
        or route.get("route")
        or "/"
    )

    fixed = dict(route)
    fixed["path"] = path
    fixed["type"] = "static"
    fixed["root"] = str(
        route.get("root")
        or route.get("dir")
        or route.get("directory")
        or "public"
    )
    fixed["index"] = str(route.get("index") or "index.html")
    fixed["balancing"] = "round_robin"
    fixed["upstreams"] = []

    fixed.pop("upstream", None)
    fixed.pop("backend", None)
    fixed.pop("backends", None)
    fixed.pop("backend_upstreams", None)
    fixed.pop("algorithm", None)
    fixed.pop("lb_algorithm", None)
    fixed.pop("load_balancing", None)
    fixed.pop("strategy", None)
    fixed.pop("target", None)
    fixed.pop("url", None)

    return fixed


def normalize_upstream_item(item: Any) -> dict[str, Any]:
    if isinstance(item, str):
        return {
            "address": normalize_upstream_address(item),
            "weight": 1,
        }

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
        "address": "127.0.0.1:3000",
        "weight": 1,
    }


def normalize_route(route: dict[str, Any]) -> dict[str, Any]:
    if is_static_route(route):
        return normalize_static_route(route)

    path = normalize_path(
        route.get("path")
        or route.get("prefix")
        or route.get("route")
        or "/"
    )

    balancing = normalize_balancing(
        route.get("balancing")
        or route.get("load_balancing")
        or route.get("lb_algorithm")
        or route.get("algorithm")
        or route.get("strategy")
        or "round_robin"
    )

    upstreams_raw = route.get("upstreams")

    if upstreams_raw is None:
        if route.get("backends") is not None:
            upstreams_raw = route.get("backends")
        else:
            upstream = route.get("upstream") or route.get("backend") or "127.0.0.1:3000"
            upstreams_raw = [upstream]

    if not isinstance(upstreams_raw, list):
        upstreams_raw = [upstreams_raw]

    upstreams: list[dict[str, Any]] = []
    seen_addresses: set[str] = set()

    for item in upstreams_raw:
        upstream = normalize_upstream_item(item)
        address = upstream["address"]

        if address not in seen_addresses:
            upstreams.append(upstream)
            seen_addresses.add(address)
        else:
            for existing in upstreams:
                if existing["address"] == address:
                    existing["weight"] = max(
                        int(existing.get("weight", 1)),
                        int(upstream.get("weight", 1)),
                    )
                    break

    if not upstreams:
        upstreams = [
            {
                "address": "127.0.0.1:3000",
                "weight": 1,
            }
        ]

    if len(upstreams) > 1 and any(int(item.get("weight", 1)) != 1 for item in upstreams):
        balancing = "weighted_round_robin"

    fixed = dict(route)
    fixed["path"] = path
    fixed["balancing"] = balancing
    fixed["upstreams"] = upstreams
    fixed["upstream"] = upstreams[0]["address"]
    fixed["backend"] = upstreams[0]["address"]

    fixed.pop("algorithm", None)
    fixed.pop("lb_algorithm", None)
    fixed.pop("load_balancing", None)
    fixed.pop("strategy", None)
    fixed.pop("backends", None)
    fixed.pop("backend_upstreams", None)
    fixed.pop("address", None)
    fixed.pop("target", None)
    fixed.pop("url", None)

    return fixed


def route_upstream_addresses(route: dict[str, Any]) -> list[str]:
    if is_static_route(route):
        return []

    normalized = normalize_route(route)
    addresses: list[str] = []

    for upstream in normalized.get("upstreams", []):
        if not isinstance(upstream, dict):
            upstream = normalize_upstream_item(upstream)

        address = normalize_upstream_address(
            upstream.get("address")
            or upstream.get("upstream")
            or upstream.get("backend")
            or "127.0.0.1:3000"
        )

        if address not in addresses:
            addresses.append(address)

    if not addresses and normalized.get("upstream"):
        addresses.append(normalize_upstream_address(normalized.get("upstream")))

    return addresses


def merge_routes_for_generation(routes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered_paths: list[str] = []
    merged: dict[str, dict[str, Any]] = {}
    upstreams_by_path: dict[str, list[dict[str, Any]]] = {}

    for route in routes:
        if not isinstance(route, dict):
            continue

        normalized = normalize_route(route)
        path = normalized["path"]

        if path not in merged:
            merged[path] = normalized
            upstreams_by_path[path] = []
            ordered_paths.append(path)

        if normalized.get("type") == "static":
            merged[path] = normalized
            upstreams_by_path[path] = []
            continue

        if merged[path].get("type") == "static":
            continue

        if normalized.get("balancing"):
            merged[path]["balancing"] = normalized["balancing"]

        for upstream in normalized.get("upstreams", []):
            if not isinstance(upstream, dict):
                upstream = normalize_upstream_item(upstream)

            address = normalize_upstream_address(upstream.get("address"))
            weight = int(upstream.get("weight", 1) or 1)

            if weight < 1:
                weight = 1

            existing = next(
                (
                    item
                    for item in upstreams_by_path[path]
                    if item["address"] == address
                ),
                None,
            )

            if existing is None:
                upstreams_by_path[path].append(
                    {
                        "address": address,
                        "weight": weight,
                    }
                )
            else:
                existing["weight"] = max(
                    int(existing.get("weight", 1)),
                    weight,
                )

    output: list[dict[str, Any]] = []

    for path in ordered_paths:
        route = dict(merged[path])

        if route.get("type") == "static":
            output.append(route)
            continue

        upstreams = upstreams_by_path[path]

        if not upstreams:
            upstreams = [
                {
                    "address": normalize_upstream_address(route.get("upstream")),
                    "weight": 1,
                }
            ]

        balancing = normalize_balancing(route.get("balancing"))

        if len(upstreams) > 1 and any(int(item.get("weight", 1)) != 1 for item in upstreams):
            balancing = "weighted_round_robin"

        route["path"] = path
        route["upstreams"] = upstreams
        route["upstream"] = upstreams[0]["address"]
        route["backend"] = upstreams[0]["address"]
        route["balancing"] = balancing

        output.append(route)

    return output


def collect_expected_upstream_addresses(config: dict[str, Any]) -> list[str]:
    addresses: list[str] = []

    routes = config.get("routes") or []

    if not isinstance(routes, list):
        return addresses

    for route in routes:
        if not isinstance(route, dict):
            continue

        for address in route_upstream_addresses(route):
            if address not in addresses:
                addresses.append(address)

    return addresses


def assert_rendered_rust_contains_upstreams(
    *,
    config: dict[str, Any],
    main_rs: str,
) -> None:
    missing = []

    for address in collect_expected_upstream_addresses(config):
        if address not in main_rs:
            missing.append(address)

    if missing:
        raise RuntimeError(
            "Generated Rust is missing upstream address(es): "
            + ", ".join(missing)
        )


def normalize_security(config: dict[str, Any]) -> dict[str, Any]:
    security = dict(DEFAULT_SECURITY)

    incoming = config.get("security")

    if isinstance(incoming, dict):
        security.update(incoming)

    blocked_paths = security.get("blocked_paths") or []

    if not isinstance(blocked_paths, list):
        blocked_paths = []

    security["blocked_paths"] = []

    for path in blocked_paths:
        if isinstance(path, str):
            fixed_path = normalize_path(path)

            if fixed_path not in security["blocked_paths"]:
                security["blocked_paths"].append(fixed_path)

    allowed_methods = security.get("allowed_methods") or []

    if not isinstance(allowed_methods, list):
        allowed_methods = []

    methods = []
    valid_methods = {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}

    for method in allowed_methods:
        if isinstance(method, str):
            upper = method.strip().upper()

            if upper in valid_methods and upper not in methods:
                methods.append(upper)

    security["allowed_methods"] = methods

    numeric_defaults = {
        "rate_limit_per_minute": 120,
        "max_connections": 1000,
        "max_request_body_bytes": 1048576,
        "upstream_timeout_seconds": 30,
    }

    for key, default in numeric_defaults.items():
        try:
            value = int(security.get(key, default))
        except Exception:
            value = default

        if value < 0:
            value = default

        security[key] = value

    return security


def normalize_config_for_generation(config: dict[str, Any]) -> dict[str, Any]:
    fixed = dict(config)

    fixed["port"] = normalize_port(
        fixed.get("port") or fixed.get("listen_port") or fixed.get("proxy_port"),
        default=9000,
    )

    routes = fixed.get("routes") or []
    normalized_routes: list[dict[str, Any]] = []

    if isinstance(routes, list):
        raw_routes = [route for route in routes if isinstance(route, dict)]
        normalized_routes = merge_routes_for_generation(raw_routes)

    if not normalized_routes:
        normalized_routes = [
            normalize_static_route(
                {
                    "path": "/",
                    "type": "static",
                    "root": "public",
                    "index": "index.html",
                }
            )
        ]

    fixed["routes"] = normalized_routes
    fixed["security"] = normalize_security(fixed)

    if "demo_backend" not in fixed:
        fixed["demo_backend"] = dict(DEFAULT_DEMO_BACKEND)

    return fixed


def route_path_to_dirname(route_path: str) -> str:
    route_path = normalize_path(route_path)

    if route_path == "/":
        return ""

    return route_path.strip("/")


def get_route_upstream(route: dict[str, Any]) -> str:
    addresses = route_upstream_addresses(route)

    if not addresses:
        return ""

    return addresses[0]


def get_route_upstreams_display(route: dict[str, Any]) -> str:
    if is_static_route(route):
        return f"static:{route.get('root', 'public')}"

    addresses = route_upstream_addresses(route)

    if not addresses:
        return "127.0.0.1:3000"

    return ", ".join(addresses)


def demo_backend_settings(config: dict[str, Any]) -> dict[str, Any]:
    incoming = config.get("demo_backend")

    if not isinstance(incoming, dict):
        return dict(DEFAULT_DEMO_BACKEND)

    settings = dict(DEFAULT_DEMO_BACKEND)
    settings.update(incoming)

    settings["enabled"] = bool(settings.get("enabled", True))
    settings["generate_placeholder_files"] = bool(
        settings.get("generate_placeholder_files", True)
    )
    settings["overwrite_existing"] = bool(settings.get("overwrite_existing", False))

    return settings


def render_demo_route_index_html(
    *,
    route_path: str,
    upstream: str,
    port: int,
    project_name: str = "AI Pingora Gateway",
) -> str:
    safe_route = html.escape(route_path)
    safe_upstream = html.escape(upstream)
    safe_project = html.escape(project_name)

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{safe_project} - {safe_route}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
  <main>
    <h1>✅ Route works: <code>{safe_route}</code></h1>
    <p>This is demo backend content generated for local testing.</p>
    <p>Upstream(s): <code>{safe_upstream}</code></p>
    <p>Proxy: <code>http://127.0.0.1:{port}{safe_route}</code></p>
  </main>
</body>
</html>
"""


def render_demo_home_index_html(
    *,
    config: dict[str, Any],
    port: int,
    project_name: str = "AI Pingora Gateway",
) -> str:
    safe_project = html.escape(project_name)

    links = []

    for route in config.get("routes", []):
        if not isinstance(route, dict):
            continue

        route_path = normalize_path(route.get("path") or route.get("prefix") or route.get("route"))

        if route_path == "/":
            continue

        href = route_path if route_path.endswith("/") else f"{route_path}/"
        safe_href = html.escape(href)
        safe_route = html.escape(route_path)
        upstream = html.escape(get_route_upstreams_display(route))

        links.append(
            f"""
            <li>
              <a href="{safe_href}"><code>{safe_route}</code></a>
              <span>→ <code>{upstream}</code></span>
            </li>
            """
        )

    links_html = "\n".join(links) if links else "<li>No extra routes generated yet.</li>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{safe_project}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
  <main>
    <h1>✅ AI Pingora Gateway is running</h1>
    <p>Live URL: <code>http://127.0.0.1:{port}</code></p>
    <h2>Available routes</h2>
    <ul>{links_html}</ul>
  </main>
</body>
</html>
"""


def write_text_if_allowed(path: Path, content: str, *, overwrite: bool) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and not overwrite:
        return False

    path.write_text(content, encoding="utf-8")
    return True


def write_json_if_allowed(path: Path, data: dict[str, Any], *, overwrite: bool) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.exists() and not overwrite:
        return False

    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return True


def write_default_public_files(project_path: Path, config: dict[str, Any]) -> None:
    routes = config.get("routes") or []

    if not any(isinstance(route, dict) and is_static_route(route) for route in routes):
        return

    for route in routes:
        if not isinstance(route, dict) or not is_static_route(route):
            continue

        root = str(route.get("root") or "public")
        index = str(route.get("index") or "index.html")

        public_dir = project_path / root
        public_dir.mkdir(parents=True, exist_ok=True)

        index_path = public_dir / index

        if index_path.exists():
            continue

        port = int(config.get("port") or 8090)

        index_path.write_text(
            f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>AI Pingora Webserver</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{
      font-family: system-ui, sans-serif;
      margin: 40px;
      background: #f9fafb;
      color: #111827;
    }}
    main {{
      max-width: 800px;
      background: white;
      border: 1px solid #e5e7eb;
      border-radius: 16px;
      padding: 32px;
      box-shadow: 0 10px 25px rgba(0,0,0,0.06);
    }}
    code {{
      background: #f3f4f6;
      padding: 2px 6px;
      border-radius: 6px;
    }}
  </style>
</head>
<body>
  <main>
    <h1>✅ AI Pingora Webserver is running</h1>
    <p>This page is served directly by the generated Pingora webserver.</p>
    <p>Live URL: <code>http://127.0.0.1:{port}/</code></p>
  </main>
</body>
</html>
""",
            encoding="utf-8",
        )


def write_demo_backend_files(
    config: dict[str, Any],
    project_dir: str | Path,
) -> dict[str, Any]:
    project_path = Path(project_dir).resolve()
    settings = demo_backend_settings(config)

    result: dict[str, Any] = {
        "enabled": settings["enabled"],
        "project_dir": str(project_path),
        "created": [],
        "skipped": [],
    }

    if not settings["enabled"]:
        return result

    if not settings["generate_placeholder_files"]:
        return result

    overwrite = settings["overwrite_existing"]

    routes = config.get("routes") or []
    port = int(config.get("port") or config.get("listen_port") or config.get("proxy_port") or 9000)

    if not isinstance(routes, list):
        return result

    for route in routes:
        if not isinstance(route, dict):
            continue

        if is_static_route(route):
            continue

        route_path = normalize_path(route.get("path") or route.get("prefix") or route.get("route"))
        upstream = get_route_upstreams_display(route)

        dirname = route_path_to_dirname(route_path)
        target_dir = project_path / dirname if dirname else project_path

        index_path = target_dir / "index.html"
        metadata_path = target_dir / "route.json"

        if route_path == "/":
            index_html = render_demo_home_index_html(
                config=config,
                port=port,
            )
        else:
            index_html = render_demo_route_index_html(
                route_path=route_path,
                upstream=upstream,
                port=port,
            )

        metadata = {
            "route": route_path,
            "upstream": get_route_upstream(route),
            "upstreams": route_upstream_addresses(route),
            "balancing": normalize_balancing(route.get("balancing")),
            "generated_by": "AI Pingora Gateway demo backend writer",
            "note": "This file is safe to replace with your own backend/static content.",
            "route_config": route,
        }

        wrote_index = write_text_if_allowed(index_path, index_html, overwrite=overwrite)
        wrote_metadata = write_json_if_allowed(metadata_path, metadata, overwrite=overwrite)

        if wrote_index or wrote_metadata:
            result["created"].append(
                {
                    "route": route_path,
                    "dir": str(target_dir),
                    "index": str(index_path),
                    "metadata": str(metadata_path),
                }
            )
        else:
            result["skipped"].append(
                {
                    "route": route_path,
                    "dir": str(target_dir),
                    "reason": "files already exist",
                }
            )

    return result


def render_cargo_toml() -> str:
    return """[package]
name = "generated-pingora-proxy"
version = "0.1.0"
edition = "2021"

[dependencies]
async-trait = "0.1"
bytes = "1"
pingora = { version = "0.8.0", features = ["proxy"] }
"""


def render_route_configs(config: dict[str, Any]) -> str:
    routes = config["routes"]
    rendered_routes = []

    for route in routes:
        normalized_route = normalize_route(route)

        route_type = str(normalized_route.get("type") or "proxy").lower()
        root = str(normalized_route.get("root") or "public")
        index = str(normalized_route.get("index") or "index.html")

        upstreams = normalized_route.get("upstreams") or []

        rendered_upstreams = []

        for upstream in upstreams:
            if not isinstance(upstream, dict):
                upstream = normalize_upstream_item(upstream)

            address = normalize_upstream_address(upstream.get("address"))
            weight = int(upstream.get("weight", 1) or 1)

            if weight < 1:
                weight = 1

            rendered_upstreams.append(
                f"""UpstreamConfig {{
            address: {rust_string(address)}.to_string(),
            weight: {weight},
        }}"""
            )

        upstreams_rs = ",\n        ".join(rendered_upstreams)

        rendered_routes.append(
            f"""RouteConfig {{
        path: {rust_string(normalized_route["path"])}.to_string(),
        route_type: {rust_string(route_type)}.to_string(),
        root: {rust_string(root)}.to_string(),
        index: {rust_string(index)}.to_string(),
        balancing: {rust_string(normalized_route.get("balancing", "round_robin"))}.to_string(),
        upstreams: vec![
        {upstreams_rs}
        ],
    }}"""
        )

    return ",\n    ".join(rendered_routes)


def render_security_config(config: dict[str, Any]) -> str:
    security = config["security"]

    blocked_paths = ", ".join(
        f"{rust_string(path)}.to_string()" for path in security.get("blocked_paths", [])
    )

    allowed_methods = ", ".join(
        f"{rust_string(method)}.to_string()" for method in security.get("allowed_methods", [])
    )

    return f"""SecurityConfig {{
        blocked_paths: vec![{blocked_paths}],
        allowed_methods: vec![{allowed_methods}],
        rate_limit_per_minute: {int(security.get("rate_limit_per_minute", 120))},
        max_connections: {int(security.get("max_connections", 1000))},
        max_request_body_bytes: {int(security.get("max_request_body_bytes", 1048576))},
        upstream_timeout_seconds: {int(security.get("upstream_timeout_seconds", 30))},
    }}"""


def render_main_rs(
    config: dict[str, Any],
    routes_override: list[dict[str, Any]] | None = None,
) -> str:
    config = normalize_config_for_generation(config)

    if routes_override is not None:
        override_config = dict(config)
        override_config["routes"] = routes_override
        config = normalize_config_for_generation(override_config)

    port = int(config["port"])
    routes_rs = render_route_configs(config)
    security_rs = render_security_config(config)

    return f"""use async_trait::async_trait;
use bytes::Bytes;
use pingora::http::ResponseHeader;
use pingora::prelude::*;
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::atomic::{{AtomicUsize, Ordering}};
use std::sync::{{Arc, Mutex}};
use std::time::{{Duration, Instant, SystemTime, UNIX_EPOCH}};

#[derive(Clone, Debug)]
struct UpstreamConfig {{
    address: String,
    weight: usize,
}}

#[derive(Clone, Debug)]
struct RouteConfig {{
    path: String,
    route_type: String,
    root: String,
    index: String,
    balancing: String,
    upstreams: Vec<UpstreamConfig>,
}}

#[derive(Clone, Debug)]
struct SecurityConfig {{
    blocked_paths: Vec<String>,
    allowed_methods: Vec<String>,
    rate_limit_per_minute: usize,
    max_connections: usize,
    max_request_body_bytes: usize,
    upstream_timeout_seconds: u64,
}}

#[derive(Debug)]
struct RateBucket {{
    window_start: Instant,
    count: usize,
}}

#[derive(Debug)]
struct RequestContext {{}}

#[derive(Clone)]
struct GeneratedProxy {{
    routes: Arc<Vec<RouteConfig>>,
    counters: Arc<Vec<AtomicUsize>>,
    upstream_loads: Arc<Vec<Vec<AtomicUsize>>>,
    security: SecurityConfig,
    rate_limiter: Arc<Mutex<HashMap<String, RateBucket>>>,
}}

fn content_type_for_path(path: &PathBuf) -> &'static str {{
    match path.extension().and_then(|value| value.to_str()).unwrap_or("") {{
        "html" | "htm" => "text/html; charset=utf-8",
        "css" => "text/css; charset=utf-8",
        "js" => "application/javascript; charset=utf-8",
        "json" => "application/json; charset=utf-8",
        "png" => "image/png",
        "jpg" | "jpeg" => "image/jpeg",
        "gif" => "image/gif",
        "svg" => "image/svg+xml",
        "txt" => "text/plain; charset=utf-8",
        "ico" => "image/x-icon",
        _ => "application/octet-stream",
    }}
}}

fn safe_static_path(root: &str, relative_path: &str, index: &str) -> Option<PathBuf> {{
    let mut path = PathBuf::from(root);

    let clean_relative = relative_path.trim_start_matches('/');

    if clean_relative.is_empty() {{
        path.push(index);
        return Some(path);
    }}

    for part in clean_relative.split('/') {{
        if part.is_empty() {{
            continue;
        }}

        if part == "." || part == ".." || part.contains('\\\\') {{
            return None;
        }}

        path.push(part);
    }}

    if relative_path.ends_with('/') {{
        path.push(index);
    }}

    Some(path)
}}

impl GeneratedProxy {{
    fn new(routes: Vec<RouteConfig>, security: SecurityConfig) -> Self {{
        let counters = routes
            .iter()
            .map(|_| AtomicUsize::new(0))
            .collect::<Vec<_>>();

        let upstream_loads = routes
            .iter()
            .map(|route| {{
                route
                    .upstreams
                    .iter()
                    .map(|_| AtomicUsize::new(0))
                    .collect::<Vec<_>>()
            }})
            .collect::<Vec<_>>();

        Self {{
            routes: Arc::new(routes),
            counters: Arc::new(counters),
            upstream_loads: Arc::new(upstream_loads),
            security,
            rate_limiter: Arc::new(Mutex::new(HashMap::new())),
        }}
    }}

    fn match_route_index(&self, path: &str) -> Option<usize> {{
        let mut best_match: Option<(usize, usize)> = None;

        for (index, route) in self.routes.iter().enumerate() {{
            let route_path = route.path.as_str();

            let matched = if route_path == "/" {{
                true
            }} else {{
                path == route_path || path.starts_with(&format!("{{}}/", route_path.trim_end_matches('/')))
            }};

            if matched {{
                let score = route_path.len();

                match best_match {{
                    Some((_, best_score)) if best_score >= score => {{}}
                    _ => best_match = Some((index, score)),
                }}
            }}
        }}

        best_match.map(|(index, _)| index)
    }}

    async fn serve_static_route(
        &self,
        session: &mut Session,
        route: &RouteConfig,
        request_path: &str,
    ) -> Result<bool> {{
        let relative_path = if route.path == "/" {{
            request_path
        }} else {{
            request_path
                .strip_prefix(route.path.trim_end_matches('/'))
                .unwrap_or("")
        }};

        let file_path = match safe_static_path(&route.root, relative_path, &route.index) {{
            Some(path) => path,
            None => {{
                session.respond_error(403).await?;
                return Ok(true);
            }}
        }};

        let body = match std::fs::read(&file_path) {{
            Ok(value) => value,
            Err(_) => {{
                session.respond_error(404).await?;
                return Ok(true);
            }}
        }};

        let mut header = ResponseHeader::build(200, None)?;
        header.insert_header("content-type", content_type_for_path(&file_path))?;
        header.insert_header("content-length", body.len().to_string())?;

        session.write_response_header(Box::new(header), false).await?;

        if session.req_header().method.as_str().eq_ignore_ascii_case("HEAD") {{
            session.write_response_body(None, true).await?;
        }} else {{
            session
                .write_response_body(Some(Bytes::from(body)), true)
                .await?;
        }}

        Ok(true)
    }}

    fn select_upstream(&self, route_index: usize, session: &Session) -> Option<String> {{
        let route = self.routes.get(route_index)?;

        if route.upstreams.is_empty() {{
            return None;
        }}

        let len = route.upstreams.len();

        let selected = match route.balancing.as_str() {{
            "random" => self.select_random_index(route_index, len),
            "weighted_round_robin" => self.select_weighted_round_robin_index(route_index, route),
            "least_connections" => self.select_least_connections_index(route_index, len),
            "ip_hash" => self.select_ip_hash_index(session, len),
            _ => self.select_round_robin_index(route_index, len),
        }};

        Some(route.upstreams[selected].address.clone())
    }}

    fn select_round_robin_index(&self, route_index: usize, len: usize) -> usize {{
        let counter = self.counters[route_index].fetch_add(1, Ordering::Relaxed);
        counter % len
    }}

    fn select_random_index(&self, route_index: usize, len: usize) -> usize {{
        let counter = self.counters[route_index].fetch_add(1, Ordering::Relaxed);

        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_else(|_| Duration::from_secs(0))
            .subsec_nanos() as usize;

        let mixed = counter
            .wrapping_mul(1_103_515_245usize)
            .wrapping_add(12_345usize)
            .wrapping_add(nanos)
            .wrapping_add(route_index.wrapping_mul(2_654_435_761usize));

        mixed % len
    }}

    fn select_weighted_round_robin_index(&self, route_index: usize, route: &RouteConfig) -> usize {{
        let total_weight: usize = route
            .upstreams
            .iter()
            .map(|upstream| upstream.weight.max(1))
            .sum();

        if total_weight == 0 {{
            return self.select_round_robin_index(route_index, route.upstreams.len());
        }}

        let counter = self.counters[route_index].fetch_add(1, Ordering::Relaxed);
        let mut ticket = counter % total_weight;

        for (index, upstream) in route.upstreams.iter().enumerate() {{
            let weight = upstream.weight.max(1);

            if ticket < weight {{
                return index;
            }}

            ticket -= weight;
        }}

        0
    }}

    fn select_least_connections_index(&self, route_index: usize, len: usize) -> usize {{
        let loads = match self.upstream_loads.get(route_index) {{
            Some(value) => value,
            None => return self.select_round_robin_index(route_index, len),
        }};

        let mut best_index = 0usize;
        let mut best_load = usize::MAX;

        for index in 0..len {{
            let load = loads
                .get(index)
                .map(|counter| counter.load(Ordering::Relaxed))
                .unwrap_or(usize::MAX);

            if load < best_load {{
                best_load = load;
                best_index = index;
            }}
        }}

        if let Some(counter) = loads.get(best_index) {{
            counter.fetch_add(1, Ordering::Relaxed);
        }}

        best_index
    }}

    fn select_ip_hash_index(&self, session: &Session, len: usize) -> usize {{
        let key = client_key(session);
        let mut hash: usize = 2_166_136_261usize;

        for byte in key.as_bytes() {{
            hash ^= *byte as usize;
            hash = hash.wrapping_mul(16_777_619usize);
        }}

        hash % len
    }}

    fn is_blocked_path(&self, path: &str) -> bool {{
        for blocked in &self.security.blocked_paths {{
            if path == blocked || path.starts_with(&format!("{{}}/", blocked.trim_end_matches('/'))) {{
                return true;
            }}
        }}

        false
    }}

    fn method_allowed(&self, method: &str) -> bool {{
        if self.security.allowed_methods.is_empty() {{
            return true;
        }}

        self.security
            .allowed_methods
            .iter()
            .any(|allowed| allowed.eq_ignore_ascii_case(method))
    }}

    fn check_rate_limit(&self, client_key: &str) -> bool {{
        let limit = self.security.rate_limit_per_minute;

        if limit == 0 {{
            return true;
        }}

        let mut limiter = match self.rate_limiter.lock() {{
            Ok(guard) => guard,
            Err(_) => return true,
        }};

        let now = Instant::now();

        let bucket = limiter.entry(client_key.to_string()).or_insert(RateBucket {{
            window_start: now,
            count: 0,
        }});

        if now.duration_since(bucket.window_start) >= Duration::from_secs(60) {{
            bucket.window_start = now;
            bucket.count = 0;
        }}

        if bucket.count >= limit {{
            return false;
        }}

        bucket.count += 1;
        true
    }}
}}

fn parse_upstream(upstream: &str) -> (String, u16) {{
    let cleaned = upstream
        .trim()
        .trim_start_matches("http://")
        .trim_start_matches("https://")
        .trim_end_matches('/');

    let mut parts = cleaned.rsplitn(2, ':');

    let port_str = parts.next().unwrap_or("80");
    let host = parts.next().unwrap_or(cleaned);

    let port = port_str.parse::<u16>().unwrap_or(80);

    (host.to_string(), port)
}}

fn client_key(session: &Session) -> String {{
    match session.client_addr() {{
        Some(addr) => {{
            let raw = format!("{{:?}}", addr);

            if raw.starts_with('[') {{
                match raw.rfind("]:") {{
                    Some(index) => raw[..index + 1].to_string(),
                    None => raw,
                }}
            }} else {{
                match raw.rfind(':') {{
                    Some(index) => raw[..index].to_string(),
                    None => raw,
                }}
            }}
        }}
        None => "unknown".to_string(),
    }}
}}

#[async_trait]
impl ProxyHttp for GeneratedProxy {{
    type CTX = RequestContext;

    fn new_ctx(&self) -> Self::CTX {{
        RequestContext {{}}
    }}

    async fn request_filter(
        &self,
        session: &mut Session,
        _ctx: &mut Self::CTX,
    ) -> Result<bool> {{
        let path = session.req_header().uri.path().to_string();
        let method = session.req_header().method.as_str().to_uppercase();

        if !self.method_allowed(&method) {{
            session.respond_error(405).await?;
            return Ok(true);
        }}

        if self.is_blocked_path(&path) {{
            session.respond_error(403).await?;
            return Ok(true);
        }}

        if session.req_header().headers.get("upgrade").is_some() {{
            session.respond_error(400).await?;
            return Ok(true);
        }}

        if let Some(value) = session.req_header().headers.get("content-length") {{
            if let Ok(text) = value.to_str() {{
                if let Ok(length) = text.parse::<usize>() {{
                    if length > self.security.max_request_body_bytes {{
                        session.respond_error(413).await?;
                        return Ok(true);
                    }}
                }}
            }}
        }}

        let key = client_key(session);

        if !self.check_rate_limit(&key) {{
            session.respond_error(429).await?;
            return Ok(true);
        }}

        let route_index = match self.match_route_index(&path) {{
            Some(index) => index,
            None => {{
                session.respond_error(404).await?;
                return Ok(true);
            }}
        }};

        let route = match self.routes.get(route_index) {{
            Some(value) => value.clone(),
            None => {{
                session.respond_error(404).await?;
                return Ok(true);
            }}
        }};

        if route.route_type == "static" {{
            return self.serve_static_route(session, &route, &path).await;
        }}

        Ok(false)
    }}

    async fn upstream_peer(
        &self,
        session: &mut Session,
        _ctx: &mut Self::CTX,
    ) -> Result<Box<HttpPeer>> {{
        let path = session.req_header().uri.path();

        let route_index = self.match_route_index(path).unwrap_or(0);

        let upstream = self
            .select_upstream(route_index, session)
            .unwrap_or_else(|| "127.0.0.1:3000".to_string());

        let (host, port) = parse_upstream(upstream.as_str());

        let peer = Box::new(HttpPeer::new(
            (host.as_str(), port),
            false,
            String::new(),
        ));

        Ok(peer)
    }}
}}

fn build_routes() -> Vec<RouteConfig> {{
    vec![
    {routes_rs}
    ]
}}

fn build_security() -> SecurityConfig {{
    {security_rs}
}}

fn main() {{
    let routes = build_routes();
    let security = build_security();

    let proxy = GeneratedProxy::new(routes, security);

    let mut server = Server::new(None).unwrap();
    server.bootstrap();

    let mut service = http_proxy_service(&server.configuration, proxy);
    service.add_tcp("0.0.0.0:{port}");

    server.add_service(service);

    println!("🚀 Starting Pingora proxy on port {port}");

    server.run_forever();
}}
"""


def write_project(
    config: dict[str, Any],
    project_dir: str | Path | None = None,
) -> Path:
    project_path = Path(project_dir) if project_dir is not None else PROJECT_DIR
    project_path = project_path.resolve()

    src_dir = project_path / "src"
    src_dir.mkdir(parents=True, exist_ok=True)

    normalized_config = normalize_config_for_generation(config)

    write_default_public_files(project_path, normalized_config)

    cargo_toml = render_cargo_toml()
    main_rs = render_main_rs(normalized_config)

    assert_rendered_rust_contains_upstreams(
        config=normalized_config,
        main_rs=main_rs,
    )

    (project_path / "Cargo.toml").write_text(cargo_toml, encoding="utf-8")
    (src_dir / "main.rs").write_text(main_rs, encoding="utf-8")

    with (project_path / "config.json").open("w", encoding="utf-8") as f:
        json.dump(normalized_config, f, indent=2)

    try:
        demo_result = write_demo_backend_files(normalized_config, project_path)

        if demo_result.get("enabled"):
            created = demo_result.get("created", [])
            skipped = demo_result.get("skipped", [])

            if created:
                print(f"✅ Demo backend files created: {len(created)} route file set(s)")

            if skipped:
                print(f"ℹ️ Demo backend files skipped: {len(skipped)} existing route file set(s)")
    except Exception as exc:
        print(f"⚠️ Demo backend file generation skipped: {exc}")

    print("✅ Project generated successfully")
    print(f"📁 Folder: {project_path}")

    return project_path