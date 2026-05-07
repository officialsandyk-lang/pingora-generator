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


def edge_health_ok(config: dict[str, Any], timeout_seconds: int = 15) -> bool:
    public_port = public_port_from_config(config)
    url = f"http://127.0.0.1:{public_port}/__edge_health"

    deadline = time.time() + timeout_seconds

    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if int(response.status) == 200:
                    return True
        except urllib.error.HTTPError as exc:
            if int(exc.code) == 200:
                return True
        except Exception:
            pass

        time.sleep(1)

    return False


def switch_traffic_to(color: str, config: dict[str, Any]) -> str:
    if color not in COLORS:
        raise ValueError(f"Invalid color: {color}")

    reload_edge_router(color, config)

    if not edge_health_ok(config):
        raise TrafficSwitchError(
            f"Edge router did not become healthy after switching to {color}"
        )

    return live_url_from_config(config)