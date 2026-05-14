from __future__ import annotations

import time
import urllib.error
import urllib.request
from typing import Any

from core.deployment_state import COLORS
from core.edge_router_runner import reload_edge_router
from core.edge_router_writer import live_url_from_config, public_port_from_config


class TrafficSwitchError(RuntimeError):
    pass


def _http_status_ok(url: str, timeout: int = 2) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= int(response.status) < 500
    except urllib.error.HTTPError as exc:
        # 403 proves the gateway is alive for blocked paths.
        # 404 proves the gateway is alive but route is missing.
        # For health, accept any HTTP response below 500.
        return int(exc.code) < 500
    except Exception:
        return False


def _candidate_health_paths(config: dict[str, Any]) -> list[str]:
    paths = ["/__edge_health", "/"]

    routes = config.get("routes") or []

    if isinstance(routes, list):
        for route in routes:
            if not isinstance(route, dict):
                continue

            path = str(
                route.get("path")
                or route.get("prefix")
                or route.get("route")
                or ""
            ).strip()

            if not path:
                continue

            if not path.startswith("/"):
                path = "/" + path

            if path != "/" and not path.endswith("/"):
                path = path + "/"

            if path not in paths:
                paths.append(path)

    return paths


def edge_health_ok(config: dict[str, Any], timeout_seconds: int = 15) -> bool:
    """
    Health check for both supported deployment shapes.

    Shape A: real blue/green edge router
      http://127.0.0.1:8088/__edge_health -> 200

    Shape B: direct generated proxy on public port
      http://127.0.0.1:8088/ or /users/ -> any non-5xx response

    This prevents local/docker direct-proxy mode from failing only because
    /__edge_health does not exist.
    """

    public_port = public_port_from_config(config)
    deadline = time.time() + timeout_seconds

    candidate_urls = [
        f"http://127.0.0.1:{public_port}{path}"
        for path in _candidate_health_paths(config)
    ]

    last_urls = ", ".join(candidate_urls)

    while time.time() < deadline:
        for url in candidate_urls:
            if _http_status_ok(url, timeout=2):
                return True

        time.sleep(1)

    print("")
    print("❌ Gateway health check failed.")
    print("Checked URLs:")
    for url in candidate_urls:
        print(f"- {url}")

    return False


def switch_traffic_to(color: str, config: dict[str, Any]) -> str:
    if color not in COLORS:
        raise ValueError(f"Invalid color: {color}")

    try:
        reload_edge_router(color, config)
    except Exception as exc:
        print("")
        print("⚠️ Edge router reload failed or is not available.")
        print(f"Reason: {exc}")
        print("Continuing with direct gateway health check...")

    if not edge_health_ok(config):
        raise TrafficSwitchError(
            f"Gateway did not become healthy after switching to {color}"
        )

    return live_url_from_config(config)
