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
    upstream = upstream.replace("localhost", "127.0.0.1")
    upstream = upstream.rstrip("/")

    if "/" in upstream:
        upstream = upstream.split("/", 1)[0]

    if ":" not in upstream:
        return "127.0.0.1:3000"

    host, port_text = upstream.rsplit(":", 1)

    host = host.strip()
    port = normalize_port(port_text, default=3000)

    if host not in {"127.0.0.1", "0.0.0.0", "host.docker.internal"}:
        host = "127.0.0.1"

    return f"{host}:{port}"


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
    path = normalize_path(route.get("path") or route.get("prefix") or route.get("route"))

    balancing = str(route.get("balancing") or route.get("strategy") or "round_robin").strip()

    if balancing != "round_robin":
        balancing = "round_robin"

    upstreams_raw = route.get("upstreams")

    if upstreams_raw is None:
        upstream = route.get("upstream") or "127.0.0.1:3000"
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

    if not upstreams:
        upstreams = [
            {
                "address": "127.0.0.1:3000",
                "weight": 1,
            }
        ]

    fixed = dict(route)
    fixed["path"] = path
    fixed["balancing"] = balancing
    fixed["upstreams"] = upstreams

    # Backward compatibility for older validator/agents.
    fixed["upstream"] = upstreams[0]["address"]

    return fixed


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
    seen_paths: dict[str, dict[str, Any]] = {}

    if isinstance(routes, list):
        for route in routes:
            if not isinstance(route, dict):
                continue

            normalized = normalize_route(route)

            # Last duplicate path wins.
            seen_paths[normalized["path"]] = normalized

    normalized_routes = list(seen_paths.values())

    if not normalized_routes:
        normalized_routes = [
            normalize_route(
                {
                    "path": "/",
                    "upstream": "127.0.0.1:3000",
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
    upstream = route.get("upstream")

    if upstream:
        return str(upstream)

    upstreams = route.get("upstreams")

    if isinstance(upstreams, list) and upstreams:
        first = upstreams[0]

        if isinstance(first, dict):
            return str(first.get("address") or first.get("upstream") or "127.0.0.1:3000")

        return str(first)

    return "127.0.0.1:3000"


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
  <style>
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 40px;
      line-height: 1.5;
      color: #111827;
      background: #f9fafb;
    }}
    main {{
      max-width: 760px;
      background: white;
      border: 1px solid #e5e7eb;
      border-radius: 16px;
      padding: 28px;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.06);
    }}
    code {{
      background: #f3f4f6;
      padding: 2px 6px;
      border-radius: 6px;
    }}
    a {{
      color: #2563eb;
      font-weight: 700;
      text-decoration: none;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .ok {{
      color: #047857;
      font-weight: 700;
    }}
  </style>
</head>
<body>
  <main>
    <h1 class="ok">✅ Route works: <code>{safe_route}</code></h1>
    <p>This is demo backend content generated for local testing.</p>

    <h2>Route</h2>
    <p><code>{safe_route}</code></p>

    <h2>Upstream</h2>
    <p><code>{safe_upstream}</code></p>

    <h2>Proxy</h2>
    <p><code>http://127.0.0.1:{port}{safe_route}</code></p>

    <p><a href="/">← Back to route index</a></p>

    <p>You can replace this file with your own backend/static content later.</p>
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
        upstream = html.escape(get_route_upstream(route))

        links.append(
            f"""
            <li>
              <a href="{safe_href}"><code>{safe_route}</code></a>
              <span>→ <code>{upstream}</code></span>
            </li>
            """
        )

    links_html = "\n".join(links) if links else "<li>No routes generated yet.</li>"

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>{safe_project}</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      margin: 40px;
      line-height: 1.5;
      color: #111827;
      background: #f9fafb;
    }}
    main {{
      max-width: 860px;
      background: white;
      border: 1px solid #e5e7eb;
      border-radius: 16px;
      padding: 28px;
      box-shadow: 0 10px 25px rgba(0, 0, 0, 0.06);
    }}
    code {{
      background: #f3f4f6;
      padding: 2px 6px;
      border-radius: 6px;
    }}
    li {{
      margin: 12px 0;
    }}
    a {{
      font-weight: 700;
      color: #2563eb;
      text-decoration: none;
    }}
    a:hover {{
      text-decoration: underline;
    }}
    .ok {{
      color: #047857;
      font-weight: 700;
    }}
    .muted {{
      color: #6b7280;
    }}
  </style>
</head>
<body>
  <main>
    <h1 class="ok">✅ AI Pingora Gateway is running</h1>
    <p>Live URL: <code>http://127.0.0.1:{port}</code></p>

    <h2>Available routes</h2>
    <ul>
      {links_html}
    </ul>

    <p class="muted">Click any route above to test the generated backend placeholder page.</p>
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

    has_root_route = any(
        isinstance(route, dict)
        and normalize_path(route.get("path") or route.get("prefix") or route.get("route")) == "/"
        for route in routes
    )

    for route in routes:
        if not isinstance(route, dict):
            continue

        route_path = normalize_path(route.get("path") or route.get("prefix") or route.get("route"))
        upstream = get_route_upstream(route)

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
            "upstream": upstream,
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

    # Create a root route index file only when "/" exists in the config.
    # Without a "/" route, Pingora correctly returns 404 for the homepage.
    if has_root_route:
        root_index_path = project_path / "index.html"

        root_html = render_demo_home_index_html(
            config=config,
            port=port,
        )

        wrote_root = write_text_if_allowed(root_index_path, root_html, overwrite=overwrite)

        if wrote_root:
            result["created"].append(
                {
                    "route": "/",
                    "dir": str(project_path),
                    "index": str(root_index_path),
                    "metadata": str(project_path / "route.json"),
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
pingora = { version = "0.8.0", features = ["proxy"] }
"""


def render_route_configs(config: dict[str, Any]) -> str:
    routes = config["routes"]
    rendered_routes = []

    for route in routes:
        upstreams = route.get("upstreams") or [
            {
                "address": route.get("upstream", "127.0.0.1:3000"),
                "weight": 1,
            }
        ]

        rendered_upstreams = []

        for upstream in upstreams:
            address = upstream["address"]
            weight = int(upstream.get("weight", 1))

            rendered_upstreams.append(
                f"""UpstreamConfig {{
            address: {rust_string(address)}.to_string(),
            weight: {weight},
        }}"""
            )

        upstreams_rs = ",\n        ".join(rendered_upstreams)

        rendered_routes.append(
            f"""RouteConfig {{
        path: {rust_string(route["path"])}.to_string(),
        balancing: {rust_string(route.get("balancing", "round_robin"))}.to_string(),
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
use pingora::prelude::*;
use std::collections::HashMap;
use std::sync::atomic::{{AtomicUsize, Ordering}};
use std::sync::{{Arc, Mutex}};
use std::time::{{Duration, Instant}};

#[derive(Clone, Debug)]
struct UpstreamConfig {{
    address: String,
    weight: usize,
}}

#[derive(Clone, Debug)]
struct RouteConfig {{
    path: String,
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
    security: SecurityConfig,
    rate_limiter: Arc<Mutex<HashMap<String, RateBucket>>>,
}}

impl GeneratedProxy {{
    fn new(routes: Vec<RouteConfig>, security: SecurityConfig) -> Self {{
        let counters = routes
            .iter()
            .map(|_| AtomicUsize::new(0))
            .collect::<Vec<_>>();

        Self {{
            routes: Arc::new(routes),
            counters: Arc::new(counters),
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

    fn select_upstream(&self, route_index: usize) -> Option<String> {{
        let route = self.routes.get(route_index)?;

        if route.upstreams.is_empty() {{
            return None;
        }}

        let counter = self.counters[route_index].fetch_add(1, Ordering::Relaxed);
        let selected = counter % route.upstreams.len();

        Some(route.upstreams[selected].address.clone())
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
        Some(addr) => format!("{{:?}}", addr),
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

        if self.match_route_index(&path).is_none() {{
            session.respond_error(404).await?;
            return Ok(true);
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
            .select_upstream(route_index)
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

    cargo_toml = render_cargo_toml()
    main_rs = render_main_rs(normalized_config)

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