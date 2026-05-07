from pathlib import Path

from core.project_writer import render_main_rs

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROJECT_DIR = PROJECT_ROOT / "generated-pingora-proxy"

PROXY_MEMORY_LIMIT = "512m"
BACKEND_MEMORY_LIMIT = "256m"
PROXY_CPUS = "1.0"
BACKEND_CPUS = "0.5"


def get_backend_ports(config: dict) -> list[int]:
    ports = []

    for route in config["routes"]:
        upstream = route["upstream"]
        port = int(upstream.split(":")[1])
        ports.append(port)

    return sorted(set(ports))


def backend_service_name(port: int) -> str:
    return f"backend-{port}"


def build_compose_routes(config: dict) -> list[dict]:
    """
    In Docker Compose, each backend runs in its own container.

    So the proxy must use Docker service names instead of 127.0.0.1.

    Example:
    127.0.0.1:3000 -> backend-3000:3000
    """

    compose_routes = []

    for route in config["routes"]:
        original_upstream = route["upstream"]
        backend_port = int(original_upstream.split(":")[1])
        service_name = backend_service_name(backend_port)

        compose_routes.append(
            {
                "path": route["path"],
                "upstream": f"{service_name}:{backend_port}",
            }
        )

    return compose_routes


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

    services.append(
        f"""  proxy:
    build:
      context: .
      dockerfile: Dockerfile.proxy
    container_name: generated-pingora-proxy-{proxy_port}
    restart: unless-stopped
    mem_limit: {PROXY_MEMORY_LIMIT}
    cpus: "{PROXY_CPUS}"
    ports:
      - "{proxy_port}:{proxy_port}"
    depends_on:"""
    )

    for port in backend_ports:
        services.append(
            f"""      {backend_service_name(port)}:
        condition: service_healthy"""
        )

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
    command: python -m http.server {port} --bind 0.0.0.0
    healthcheck:
      test: ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:{port}/', timeout=2)"]
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