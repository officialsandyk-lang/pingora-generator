from __future__ import annotations

from pathlib import Path
from typing import Any

from core.deployment_state import COLORS, PROJECT_STORE_DIR

EDGE_ROUTER_DIR = PROJECT_STORE_DIR / "edge-router"
EDGE_NETWORK = "pingora-edge-net"
EDGE_CONTAINER_NAME = "pingora-edge-router"


def public_port_from_config(config: dict[str, Any]) -> int:
    """
    Get the user-defined public port.

    Supported config keys:
      - port
      - listen_port
      - proxy_port
      - public_port

    Example:
      {"port": 8088} -> edge router binds host port 8088
    """
    for key in ("port", "listen_port", "proxy_port", "public_port"):
        value = config.get(key)

        if value is None or value == "":
            continue

        try:
            port = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid port value for '{key}': {value!r}")

        if port < 1 or port > 65535:
            raise ValueError(f"Port must be between 1 and 65535, got: {port}")

        return port

    # Default only if user did not provide a port.
    return 9000


def internal_port_from_config(config: dict[str, Any]) -> int:
    """
    Get the port used inside the blue/green Pingora containers.

    By default, the generated Pingora app listens on the same port as the
    user-defined public port.

    Optional override keys:
      - internal_port
      - container_port
      - service_port
    """
    for key in ("internal_port", "container_port", "service_port"):
        value = config.get(key)

        if value is None or value == "":
            continue

        try:
            port = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"Invalid internal port value for '{key}': {value!r}")

        if port < 1 or port > 65535:
            raise ValueError(f"Internal port must be between 1 and 65535, got: {port}")

        return port

    return public_port_from_config(config)


def public_bind_host_from_config(config: dict[str, Any]) -> str:
    """
    Host interface for the public edge router port.

    Default:
      0.0.0.0 means accessible from host/network depending on Docker/WSL setup.

    Optional config keys:
      - bind_host
      - host
      - public_host

    For local-only binding, config can use:
      {"bind_host": "127.0.0.1"}
    """
    value = (
        config.get("bind_host")
        or config.get("host")
        or config.get("public_host")
        or "0.0.0.0"
    )

    return str(value).strip()


def live_url_from_config(config: dict[str, Any]) -> str:
    """
    User-facing live URL based on the user-defined public port.
    """
    public_port = public_port_from_config(config)

    # Use localhost for user display, even if Docker binds 0.0.0.0.
    return f"http://127.0.0.1:{public_port}"


def write_edge_router_files(active_color: str, config: dict[str, Any]) -> Path:
    """
    Write the Nginx edge router files.

    The edge router owns the user-defined public port.
    Blue/green Pingora containers run behind it on the Docker network.

    Example:
      user config port: 8088

      host:8088
        -> pingora-edge-router
        -> pingora-blue:8088 or pingora-green:8088
    """
    if active_color not in COLORS:
        raise ValueError(f"Invalid active color: {active_color}")

    EDGE_ROUTER_DIR.mkdir(parents=True, exist_ok=True)

    public_port = public_port_from_config(config)
    internal_port = internal_port_from_config(config)
    bind_host = public_bind_host_from_config(config)

    upstream_host = f"pingora-{active_color}"

    nginx_conf = f"""upstream active_pingora {{
    server {upstream_host}:{internal_port} max_fails=3 fail_timeout=10s;
    keepalive 64;
}}

server {{
    listen 80;
    server_name _;

    location = /__edge_health {{
        add_header Content-Type text/plain;
        return 200 "ok\\n";
    }}

    location / {{
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_connect_timeout 5s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;

        proxy_pass http://active_pingora;
    }}
}}
"""

    compose_yml = f"""services:
  edge-router:
    image: nginx:1.27-alpine
    container_name: {EDGE_CONTAINER_NAME}
    restart: unless-stopped
    ports:
      - "{bind_host}:{public_port}:80"
    volumes:
      - ./default.conf:/etc/nginx/conf.d/default.conf:ro
    networks:
      - {EDGE_NETWORK}

networks:
  {EDGE_NETWORK}:
    external: true
"""

    nginx_conf_path = EDGE_ROUTER_DIR / "default.conf"
    compose_path = EDGE_ROUTER_DIR / "docker-compose.yml"

    nginx_conf_path.write_text(nginx_conf, encoding="utf-8")
    compose_path.write_text(compose_yml, encoding="utf-8")

    return compose_path