from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any


DEFAULT_DEMO_BACKEND = {
    "enabled": True,
    "generate_placeholder_files": True,
    "overwrite_existing": False,
}


def normalize_path(path: Any) -> str:
    path = str(path or "/").strip()

    if not path.startswith("/"):
        path = "/" + path

    path = re.sub(r"[^a-zA-Z0-9/_-]", "", path)

    if not path:
        return "/"

    if not path.startswith("/"):
        path = "/" + path

    return path


def route_path_to_dirname(route_path: str) -> str:
    """
    Convert:
      "/"        -> ""
      "/users"  -> "users"
      "/api/v1" -> "api/v1"
    """
    route_path = normalize_path(route_path)

    if route_path == "/":
        return ""

    return route_path.strip("/")


def get_route_upstream(route: dict[str, Any]) -> str:
    """
    Supports old and new config shapes:

      {"upstream": "127.0.0.1:3000"}

    and:

      {
        "upstreams": [
          {"address": "127.0.0.1:3000"}
        ]
      }
    """
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


def render_index_html(
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

    <p>You can replace this file with your own backend/static content later.</p>
  </main>
</body>
</html>
"""


def render_route_metadata(
    *,
    route_path: str,
    upstream: str,
    route: dict[str, Any],
) -> dict[str, Any]:
    return {
        "route": route_path,
        "upstream": upstream,
        "generated_by": "AI Pingora Gateway demo backend writer",
        "note": "This file is safe to replace with your own backend/static content.",
        "route_config": route,
    }


def write_text_if_allowed(path: Path, content: str, *, overwrite: bool) -> bool:
    """
    Returns True if file was written.
    Returns False if skipped because file already exists.
    """
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
    """
    Create route placeholder files for local demo/testing.

    Example:
      route /users   -> generated-pingora-proxy/users/index.html
      route /orders  -> generated-pingora-proxy/orders/index.html
      route /         -> generated-pingora-proxy/index.html

    This does not overwrite existing files unless configured.

    Config flag:

      {
        "demo_backend": {
          "enabled": true,
          "generate_placeholder_files": true,
          "overwrite_existing": false
        }
      }
    """
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

        route_path = normalize_path(route.get("path") or route.get("prefix") or route.get("route"))
        upstream = get_route_upstream(route)

        dirname = route_path_to_dirname(route_path)
        target_dir = project_path / dirname if dirname else project_path

        index_path = target_dir / "index.html"
        metadata_path = target_dir / "route.json"

        index_html = render_index_html(
            route_path=route_path,
            upstream=upstream,
            port=port,
        )

        metadata = render_route_metadata(
            route_path=route_path,
            upstream=upstream,
            route=route,
        )

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