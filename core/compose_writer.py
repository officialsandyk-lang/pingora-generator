from pathlib import Path
from urllib.parse import urlparse

from core.project_writer import render_main_rs

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"

PROXY_MEMORY_LIMIT = "512m"
BACKEND_MEMORY_LIMIT = "256m"
PROXY_CPUS = "1.0"
BACKEND_CPUS = "0.5"


DEMO_BACKEND_SERVER = r'''#!/usr/bin/env python3
import argparse
import json
from datetime import datetime, UTC
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class DemoBackendHandler(BaseHTTPRequestHandler):
    server_version = "PingoraDemoBackend/1.0"

    def log_message(self, fmt, *args):
        print(
            json.dumps(
                {
                    "time": datetime.now(UTC).isoformat(),
                    "backend": self.server.backend_name,
                    "client": self.client_address[0],
                    "request": self.requestline,
                    "message": fmt % args,
                }
            ),
            flush=True,
        )

    def _read_body(self):
        length = int(self.headers.get("content-length", "0") or "0")
        if length <= 0:
            return b""
        return self.rfile.read(length)

    def _send_response(self, status=200, body="", content_type="text/plain; charset=utf-8"):
        body_bytes = body.encode("utf-8")

        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(body_bytes)))
        self.send_header("x-demo-backend", self.server.backend_name)
        self.end_headers()

        if self.command != "HEAD":
            self.wfile.write(body_bytes)

    def _handle_any(self):
        self._read_body()

        if self.path.startswith("/health"):
            self._send_response(
                200,
                f"OK\nMARKER_BACKEND={self.server.backend_name}\n",
            )
            return

        body = (
            f"Demo backend response\n"
            f"MARKER_BACKEND={self.server.backend_name}\n"
            f"METHOD={self.command}\n"
            f"PATH={self.path}\n"
        )

        self._send_response(200, body)

    def do_GET(self):
        self._handle_any()

    def do_HEAD(self):
        self._handle_any()

    def do_POST(self):
        self._handle_any()

    def do_PUT(self):
        self._handle_any()

    def do_PATCH(self):
        self._handle_any()

    def do_DELETE(self):
        self._handle_any()

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("allow", "GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD")
        self.send_header("x-demo-backend", self.server.backend_name)
        self.end_headers()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--name", required=True)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("0.0.0.0", args.port), DemoBackendHandler)
    server.backend_name = args.name

    print(
        json.dumps(
            {
                "event": "demo_backend_started",
                "backend": args.name,
                "port": args.port,
            }
        ),
        flush=True,
    )

    server.serve_forever()


if __name__ == "__main__":
    main()
'''


def normalize_upstream(upstream: str) -> str:
    """
    Normalizes values like:

    127.0.0.1:9000
    localhost:9000
    host.docker.internal:9000
    http://127.0.0.1:9000/
    https://example.com:9000/path

    into:

    host:port
    """

    upstream = str(upstream).strip()

    if not upstream:
        raise ValueError("Empty upstream value")

    if upstream.startswith("http://") or upstream.startswith("https://"):
        parsed = urlparse(upstream)
        upstream = parsed.netloc
    else:
        upstream = upstream.split("/", 1)[0]

    upstream = upstream.replace("localhost", "127.0.0.1")
    upstream = upstream.rstrip("/")

    if ":" not in upstream:
        raise ValueError(f"Invalid upstream, expected host:port: {upstream}")

    return upstream


def upstream_port(upstream: str) -> int:
    upstream = normalize_upstream(upstream)

    try:
        return int(upstream.rsplit(":", 1)[1])
    except ValueError as exc:
        raise ValueError(f"Invalid upstream port in: {upstream}") from exc


def route_upstreams(route: dict) -> list[str]:
    """
    Canonical rule:

    route["upstreams"] is the real backend pool.
    route["upstream"] is only a compatibility fallback / first backend.
    """

    upstreams = route.get("upstreams")

    if isinstance(upstreams, list) and upstreams:
        return [normalize_upstream(u) for u in upstreams if str(u).strip()]

    upstream = route.get("upstream")

    if upstream:
        return [normalize_upstream(upstream)]

    raise ValueError(f"Route has no upstream or upstreams[]: {route}")


def get_backend_ports(config: dict) -> list[int]:
    ports = []

    for route in config["routes"]:
        for upstream in route_upstreams(route):
            ports.append(upstream_port(upstream))

    return sorted(set(ports))


def backend_service_name(port: int) -> str:
    return f"backend-{port}"


def build_compose_routes(config: dict) -> list[dict]:
    """
    In Docker Compose, each backend runs in its own container.

    So the proxy must use Docker service names instead of localhost,
    127.0.0.1, or host.docker.internal.

    Example:

    127.0.0.1:3000 -> backend-3000:3000

    For load-balanced routes, preserve the whole upstreams[] pool:

    ["127.0.0.1:9101", "127.0.0.1:9102"]
    ->
    ["backend-9101:9101", "backend-9102:9102"]
    """

    compose_routes = []

    for route in config["routes"]:
        original_upstreams = route_upstreams(route)

        compose_upstreams = []

        for upstream in original_upstreams:
            port = upstream_port(upstream)
            service_name = backend_service_name(port)
            compose_upstreams.append(f"{service_name}:{port}")

        compose_route = dict(route)
        compose_route["upstream"] = compose_upstreams[0]
        compose_route["upstreams"] = compose_upstreams

        if len(compose_upstreams) > 1:
            compose_route["balancing"] = route.get("balancing", "round_robin")

        compose_routes.append(compose_route)

    return compose_routes


def write_demo_backend_file():
    demo_file = PROJECT_DIR / "demo_backend_server.py"
    demo_file.write_text(DEMO_BACKEND_SERVER)


def write_compose_proxy_source(config: dict):
    """
    Rewrites src/main.rs for Docker Compose mode while preserving
    all generated Pingora security logic from project_writer.render_main_rs().
    """

    compose_routes = build_compose_routes(config)

    main_rs = render_main_rs(
        config=config,
        routes_override=compose_routes,
    )

    (PROJECT_DIR / "src" / "main.rs").write_text(main_rs)


def write_compose_files(config: dict):
    proxy_port = config["port"]
    backend_ports = get_backend_ports(config)

    write_demo_backend_file()
    write_compose_proxy_source(config)

    dockerfile_proxy = """FROM rust:1-bookworm

RUN apt-get update && apt-get install -y \\
    build-essential \\
    cmake \\
    pkg-config \\
    libssl-dev \\
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY . .

RUN cargo build --release

CMD ["./target/release/generated-pingora-proxy"]
"""

    services = []

    proxy_service = f"""  proxy:
    build:
      context: .
      dockerfile: Dockerfile.proxy
    container_name: generated-pingora-proxy-{proxy_port}
    restart: unless-stopped
    mem_limit: {PROXY_MEMORY_LIMIT}
    cpus: "{PROXY_CPUS}"
    ports:
      - "{proxy_port}:{proxy_port}\""""

    if backend_ports:
        proxy_service += "\n    depends_on:"

        for port in backend_ports:
            proxy_service += f"""
      {backend_service_name(port)}:
        condition: service_healthy"""

    services.append(proxy_service)

    for port in backend_ports:
        service_name = backend_service_name(port)

        services.append(
            f"""
  {service_name}:
    image: python:3.12-slim
    container_name: {service_name}
    restart: unless-stopped
    mem_limit: {BACKEND_MEMORY_LIMIT}
    cpus: "{BACKEND_CPUS}"
    working_dir: /app
    volumes:
      - .:/app:ro
    command: python /app/demo_backend_server.py --port {port} --name {service_name}
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}/health', timeout=2)"]
      interval: 2s
      timeout: 3s
      retries: 30"""
        )

    compose_yml = "services:\n" + "\n".join(services) + "\n"

    dockerignore = """.git
__pycache__
*.pyc
target
"""

    (PROJECT_DIR / "Dockerfile.proxy").write_text(dockerfile_proxy)
    (PROJECT_DIR / "docker-compose.yml").write_text(compose_yml)
    (PROJECT_DIR / ".dockerignore").write_text(dockerignore)

    print("✅ Docker Compose files generated")